"""integration：web_search 真源小样本冒烟（requires_search——真 Tavily/BoCha/AnySearch MCP）。

口径（DEV §4 integration / §5.4 DoD 2）：真实 MCP 连接 + 真实查询，**断言结构不断言内容**
（candidates：url/title/snippet/score/origin 键集与类型；数量 ≤ top_k）。凭证缺失逐源
skip（.env 凭证组）。真源走公网存在瞬态（限流/抖动/单次超时）——调用面带**有界重试**
（3 次退避 2s，仅瞬态类），仍失败按真实失败计（不静默 skip——凭证在场而持续失败=集成
回归）；空结果集同样重试（搜索引擎对样本 query 偶发零结果非集成信号）。
"""

import asyncio
from typing import Any

import pytest

from app.agent.tools.registry import ToolRegistry

pytestmark = pytest.mark.requires_search

#: 真源瞬态重试参数（有界；退避 2s × 3 次 ≈ 覆盖限流窗口/单请求抖动）
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_S = 2.0

#: 真源查询小样本（有信息量的常见查询，不断言内容只断结构）
_QUERY = "LangGraph Send API 用法"
_ZH_QUERY = "向量数据库选型"


def _source_ready(name: str) -> str | None:
    """单源凭证探活；缺失原因（None=就绪）。值绝不打印。"""
    from app.config_center.defaults import MCP_CONFIG
    from app.core.config import mcp_settings

    entry = MCP_CONFIG["servers"].get(name) or {}
    if entry.get("enabled") is not True:
        return f"{name} 在契约初值中 enabled=false"
    auth = entry.get("auth")
    if auth:
        secret = getattr(mcp_settings, str(auth).lower(), None)
        if secret is None or not secret.get_secret_value():
            return f"缺凭证：{auth} 为空"
    for pointer in (entry.get("command_env"), entry.get("url_env")):
        if pointer:
            from app.agent.tools.registry import ToolRegistry

            if not ToolRegistry._resolve_pointer(str(pointer)):  # noqa: SLF001 —— 探针复用解析器
                return f"缺连接指针：{pointer} 为空"
    return None


def _registry() -> Any:
    from app.config_center.defaults import MCP_CONFIG

    registry = ToolRegistry()
    registry.bind(MCP_CONFIG)
    return registry


async def _search_with_retry(registry: Any, query: str, source: str, *, top_k: int = 3) -> list[dict[str, Any]]:
    """有界重试的真源调用：瞬态失败/空结果退避重试，连续失败按真实失败上抛。"""
    last_exc: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            candidates = await registry.search(query, source, top_k=top_k)
        except Exception as exc:  # noqa: BLE001 —— 传输/超时/出参异常均属可重试瞬态
            last_exc = exc
        else:
            if candidates:
                return candidates
            last_exc = RuntimeError("空结果集")
        if attempt < _RETRY_ATTEMPTS - 1:
            await asyncio.sleep(_RETRY_BACKOFF_S)
    raise AssertionError(
        f"{source} 连续 {_RETRY_ATTEMPTS} 次调用未得候选（最后一次：{last_exc!r}）"
    ) from last_exc


def _assert_candidates_structure(candidates: list[dict[str, Any]], source: str) -> None:
    """结构断言（integration 口径：断 structure 不断言内容）。

    A6 适配（批次 7 前置小修——测试承认数据面可选键，非放宽）：候选键集由严格五键
    相等改为「五键恒在 + 图片键可选增量」（api.md §7.2 v1.12：SearXNG img_src 主
    通道逐条透传、Tavily query_images 池挂载首候选；五键之外其余键仍禁入）。
    """
    base_keys = {"url", "title", "snippet", "score", "origin"}
    image_keys = {"img_src", "img_width", "img_height", "query_images"}
    assert isinstance(candidates, list) and candidates, f"{source} 应返回非空候选"
    for item in candidates:
        assert base_keys <= set(item) and set(item) <= base_keys | image_keys
        assert isinstance(item["url"], str) and item["url"].startswith("http")
        assert isinstance(item["title"], str) and item["title"]
        assert isinstance(item["snippet"], str)
        assert isinstance(item["score"], (int, float))
        assert item["origin"] == source
        # 图片键形态（可选键——上游带图才出现；畸形即回归红）
        if "img_src" in item:
            assert isinstance(item["img_src"], str) and item["img_src"]
        for dim in ("img_width", "img_height"):
            if dim in item:
                assert isinstance(item[dim], (int, float)) and not isinstance(item[dim], bool)
        if "query_images" in item:
            assert isinstance(item["query_images"], list)
            for entry in item["query_images"]:
                assert isinstance(entry, dict)
                assert isinstance(entry.get("url"), str) and entry["url"]


async def test_tavily_real_search_structure() -> None:
    """Tavily 真调用（primary_search）：time_range 透传小样本。"""
    ready = _source_ready("tavily")
    if ready:
        pytest.skip(ready)
    candidates = await _search_with_retry(_registry(), _QUERY, "tavily")
    _assert_candidates_structure(candidates, "tavily")
    assert len(candidates) <= 3


async def test_bocha_real_search_structure() -> None:
    """BoCha 真调用（chinese_search，stdio 官方包）：中文 query 小样本。"""
    ready = _source_ready("bocha")
    if ready:
        pytest.skip(ready)
    candidates = await _search_with_retry(_registry(), _ZH_QUERY, "bocha")
    _assert_candidates_structure(candidates, "bocha")


async def test_anysearch_real_search_structure() -> None:
    """AnySearch 真调用（fallback_search_secondary——备位真装载路径验证）。"""
    ready = _source_ready("anysearch")
    if ready:
        pytest.skip(ready)
    candidates = await _search_with_retry(_registry(), _QUERY, "anysearch")
    _assert_candidates_structure(candidates, "anysearch")


async def test_searxng_real_search_structure_or_skip() -> None:
    """SearXNG 真调用（fallback_search 链首）：SEARXNG_* 三件套在场才跑（wrapper 子进程）。"""
    ready = _source_ready("searxng")
    if ready:
        pytest.skip(ready)
    candidates = await _search_with_retry(_registry(), _QUERY, "searxng")
    _assert_candidates_structure(candidates, "searxng")


# ---- A6 images 数据面（批次 7 前置小修）：真源图片字段透传（live 条件用例）----

#: 带图倾向查询（视觉主体题——提高上游返回图片的概率；断言仍为结构面，不强求在场）
_IMAGE_QUERY = "长城 航拍照片"


async def test_tavily_live_accepts_include_images_and_pool_passthrough() -> None:
    """Tavily 真调用（A6 次级通道）：include_images=true 参数被真工具接受（拒收→
    guarded 失败→本测红=参数面回归）；出参图片键结构经 _assert_candidates_structure
    键集白名单校验（带图与否属上游波动，不强求在场）。"""
    ready = _source_ready("tavily")
    if ready:
        pytest.skip(ready)
    candidates = await _search_with_retry(_registry(), _IMAGE_QUERY, "tavily")
    _assert_candidates_structure(candidates, "tavily")  # 含 query_images/img_src 键形态校验


async def test_searxng_live_img_src_passthrough_or_skip() -> None:
    """SearXNG 真调用（A6 主通道）：逐条 img_src 透传为候选键（键形态白名单校验；
    带图与否属上游引擎波动，不强求在场）。SEARXNG_* 三件套缺失 skip（套件惯例）。"""
    ready = _source_ready("searxng")
    if ready:
        pytest.skip(ready)
    candidates = await _search_with_retry(_registry(), _IMAGE_QUERY, "searxng")
    _assert_candidates_structure(candidates, "searxng")
