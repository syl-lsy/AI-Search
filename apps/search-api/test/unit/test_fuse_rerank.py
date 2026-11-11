"""unit 层：fuse_rerank 节点（B3 节点 4/5——ARCH §4.1 fuse_rerank 行 / §4.3 TTFT）。

钉住的语义（冻结契约，逐条对应测试）：
- `fused = kb 命中 ? rerank(candidates)[:topN] : candidates[:topN]`（ARCH §4.1）：
  无 KB 标记（B3 常态，kb_search 未接入）→ **零 rerank 调用**直通截断（保护 30s 预算，
  rerank 目标 ≤2s 不变）；KB 命中 → 按 relevance 降序取 topN；
- rerank 失败（异常/超时）→ 按 origin 得分降序排序兜底 + degraded=True + errors 记录 +
  stage(search, done) detail 标注（§6.2「降级必须出现在 stage 事件 detail 与
  done.degraded」——fuse_rerank 无独立 stage_key，复用 search key 不新增枚举值）；
- fuse_top_n=10（nacos-config §3 fast.fuse_top_n，spec 快照读取缺省回落 10）；
  rerank 超时 = spec kb.rerank_timeout_s（缺省 2s，§3）；模型别名 = 轮内映射快照
  model.mapping.rerank（OQ-11 同源，缺省回落内置默认）；
- snippet 截断 ≤500 chars（§4.3 TTFT 手段，M1 冻结现值，单测锁死）；
- sources 事件发射点 = fuse_rerank（最终集定序后 ref_no 首现冻结——merge 产出的去重集
  经 topN 截断/rerank 重排后才是终态来源面）；
- 节点纯函数：rerank 客户端可 mock；零 DB/EventBus。
"""

import asyncio
import json
from typing import Any

import httpx

import pytest

from app.agent.nodes import fuse_rerank as fuse_module
from app.agent.nodes.fuse_rerank import (
    SNIPPET_MAX_CHARS,
    fuse_rerank,
    has_kb_hit,
    rerank_via_gateway,
    truncate_snippets,
)


def _cand(
    url: str,
    *,
    origin: list[str] | str = "tavily",
    score: float = 0.9,
    snippet: str = "s",
) -> dict[str, Any]:
    return {"url": url, "title": f"t:{url}", "snippet": snippet, "score": score, "origin": origin}


def _spec(top_n: int = 10, rerank_timeout: int = 2) -> dict[str, Any]:
    return {"fast": {"fuse_top_n": top_n}, "kb": {"rerank_timeout_s": rerank_timeout}}


def _config(mapping: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "configurable": {
            "model_mapping": mapping
            if mapping is not None
            else {"rerank": "qwen3-rerank", "chat": {"options": {}}}
        }
    }


def _stages(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(
        fuse_module,
        "emit_stage",
        lambda key, status, **kw: emitted.append({"stage_key": key, "status": status, **kw}),
    )
    return emitted


def _sources(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    batches: list[dict[str, Any]] = []
    monkeypatch.setattr(
        fuse_module,
        "emit_sources",
        lambda batch, total: batches.append({"batch": batch, "total": total}),
    )
    return batches


def _mock_reranker(monkeypatch: pytest.MonkeyPatch, scores: list[float] | Exception) -> list[list[str]]:
    """注入 rerank 客户端假体；记录每次收到的 documents。"""
    seen: list[list[str]] = []

    async def _fake(query: str, documents: list[str], *, model_alias: str, timeout_s: float) -> list[float]:
        seen.append(list(documents))
        if isinstance(scores, Exception):
            raise scores
        return scores

    monkeypatch.setattr(fuse_module, "rerank_via_gateway", _fake)
    return seen


# ---- 常态路径：无 KB 标记直通（零 rerank 调用）----


async def test_no_kb_hit_passes_through_with_topn_slice_and_no_rerank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无 KB 标记：candidates[:topN] 保 merge 序直通；rerank 客户端零调用（预算保护）。"""
    seen = _mock_reranker(monkeypatch, [0.99] * 15)  # 若被调用即失败（哨兵）
    candidates = [_cand(f"https://a/{i}", score=0.5 + i / 100) for i in range(15)]
    batches = _sources(monkeypatch)

    update = await fuse_rerank({"fused": candidates, "query": "q", "spec": _spec(top_n=10)}, _config())

    assert seen == []  # 无 KB 命中零 rerank（§4.1 双分支）
    assert len(update["fused"]) == 10  # fuse_top_n 截断
    assert [c["url"] for c in update["fused"]] == [f"https://a/{i}" for i in range(10)]  # 保 merge 序
    assert update["degraded"] is False and "errors" not in update
    assert batches[0]["total"] == 10  # sources 事件=最终集


async def test_fuse_top_n_read_from_spec_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """fuse_top_n 读轮内 spec 快照（OQ-11）：快照 top_n=3 即切 3。"""
    _mock_reranker(monkeypatch, [])
    _sources(monkeypatch)
    candidates = [_cand(f"https://a/{i}") for i in range(8)]

    update = await fuse_rerank(
        {"fused": candidates, "query": "q", "spec": _spec(top_n=3)}, _config()
    )

    assert len(update["fused"]) == 3


# ---- KB 命中分支：rerank 重排 ----


async def test_kb_hit_reranks_and_sorts_by_relevance(monkeypatch: pytest.MonkeyPatch) -> None:
    """KB 命中：rerank(query, docs) → relevance 降序取 topN；sources 按新序冻结 ref_no。"""
    candidates = [
        _cand("https://low/1", origin=["tavily"], score=0.9),
        _cand("https://kb/2", origin=["kb", "tavily"], score=0.5),  # KB 标记
        _cand("https://mid/3", origin=["bocha"], score=0.7),
    ]
    seen = _mock_reranker(monkeypatch, [0.1, 0.95, 0.5])  # 按 docs 输入序：low/kb/mid
    batches = _sources(monkeypatch)

    update = await fuse_rerank(
        {"fused": candidates, "query": "用户原始问题", "spec": _spec(top_n=2)}, _config()
    )

    assert seen == [["t:https://low/1", "t:https://kb/2", "t:https://mid/3"]]  # docs=标题行
    assert [c["url"] for c in update["fused"]] == ["https://kb/2", "https://mid/3"]  # relevance 降序 top2
    (batch,) = [b["batch"] for b in batches]
    assert [item["ref_no"] for item in batch] == [1, 2]  # 新序冻结
    assert update["degraded"] is False


# ---- rerank 失败：按 origin 得分排序兜底（不终止）----


async def test_rerank_failure_falls_back_to_score_sort_with_degradation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rerank 异常 → 按 origin 得分降序兜底 + degraded/errors + stage(search,done) detail 标注。"""
    candidates = [
        _cand("https://a/1", origin=["kb", "tavily"], score=0.5),
        _cand("https://b/2", origin=["kb"], score=0.95),
        _cand("https://c/3", origin=["kb"], score=0.7),
    ]
    seen = _mock_reranker(monkeypatch, RuntimeError("rerank 网关挂了"))
    stages = _stages(monkeypatch)
    batches = _sources(monkeypatch)

    update = await fuse_rerank(
        {"fused": candidates, "query": "q", "spec": _spec(top_n=2)}, _config()
    )

    assert seen != []  # rerank 确实被调用过
    assert [c["url"] for c in update["fused"]] == ["https://b/2", "https://c/3"]  # score 降序 top2
    assert update["degraded"] is True
    assert update["errors"] == [{"stage": "search", "reason": "rerank_fallback"}]
    done = [s for s in stages if s["status"] == "done"]
    assert done and "rerank" in done[-1]["detail"] and "降级" in done[-1]["detail"]
    assert done[-1]["stage_key"] == "search"  # 不新增 stage_key 枚举值


async def test_rerank_timeout_falls_back_identically(monkeypatch: pytest.MonkeyPatch) -> None:
    """rerank 超时与异常同构兜底（分级超时包络 TimeoutError → 兜底路径）。"""
    candidates = [_cand(f"https://a/{i}", origin=["kb"], score=0.9 - i / 10) for i in range(3)]
    _mock_reranker(monkeypatch, asyncio.TimeoutError())
    _stages(monkeypatch)
    _sources(monkeypatch)

    update = await fuse_rerank(
        {"fused": candidates, "query": "q", "spec": _spec()}, _config()
    )

    assert [c["url"] for c in update["fused"]] == ["https://a/0", "https://a/1", "https://a/2"]
    assert update["degraded"] is True


# ---- 空候选 / 截断 / 纯函数 ----


async def test_empty_candidates_emit_empty_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """空候选：空批次照发（不失败语义）；零 rerank。"""
    seen = _mock_reranker(monkeypatch, [])
    batches = _sources(monkeypatch)

    update = await fuse_rerank({"fused": [], "query": "q", "spec": _spec()}, _config())

    assert seen == [] and update["fused"] == []
    assert batches[0]["batch"] == [] and batches[0]["total"] == 0


def test_snippet_truncated_to_500_chars() -> None:
    """snippet 截断 ≤500 chars（§4.3 TTFT 手段，M1 冻结现值）。"""
    long_snippet = "长" * 600
    truncated = truncate_snippets([_cand("https://a/1", snippet=long_snippet)])
    assert len(truncated[0]["snippet"]) == SNIPPET_MAX_CHARS == 500
    assert truncate_snippets([_cand("https://a/1", snippet="短")])[0]["snippet"] == "短"


async def test_fused_output_snippets_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    """节点输出面（含 rerank 分支）snippet 全量 ≤500。"""
    candidates = [_cand("https://a/1", origin=["kb"], snippet="x" * 700)]
    _mock_reranker(monkeypatch, [0.9])
    _sources(monkeypatch)

    update = await fuse_rerank(
        {"fused": candidates, "query": "q", "spec": _spec()}, _config()
    )

    assert len(update["fused"][0]["snippet"]) == 500


def test_has_kb_hit_detection() -> None:
    """KB 标记判定：origin 列表含 kb 即命中；纯 web 源不命中。"""
    assert has_kb_hit([_cand("https://a/1", origin=["kb", "tavily"])]) is True
    assert has_kb_hit([_cand("https://a/1", origin=["tavily", "bocha"])]) is False
    assert has_kb_hit([]) is False


async def test_fuse_writes_only_faststate_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """state 写入 ⊆ FastState 声明（fused/errors/degraded）。"""
    _mock_reranker(monkeypatch, [])
    _sources(monkeypatch)

    update = await fuse_rerank({"fused": [], "query": "q", "spec": _spec()}, _config())

    from app.agent.state import FastState

    assert set(update) <= set(FastState.__annotations__)


# ---- rerank 客户端：LiteLLM /rerank 网关形态（httpx MockTransport）----


def test_rerank_via_gateway_request_and_score_mapping() -> None:
    """网关形态（S0 落档：cohere 兼容通道）：POST /rerank + Bearer 网关键；
    results[].relevance_score 按 index 映射回分数列表。值绝不进断言。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization", "")[:7]  # 仅前缀形态
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"results": [
                {"index": 2, "relevance_score": 0.88},
                {"index": 0, "relevance_score": 0.42},
            ]},
        )

    import httpx as _httpx

    real_client = _httpx.AsyncClient

    def _factory(**kwargs: Any) -> Any:
        kwargs["transport"] = _httpx.MockTransport(handler)
        return real_client(**kwargs)

    original = fuse_module.httpx.AsyncClient
    fuse_module.httpx.AsyncClient = _factory  # type: ignore[method-assign]
    try:
        scores = asyncio.run(rerank_via_gateway(
            "向量数据库选型",
            ["doc-zero", "doc-one", "doc-two"],
            model_alias="qwen3-rerank",
            timeout_s=2.0,
        ))
    finally:
        fuse_module.httpx.AsyncClient = original

    assert captured["url"].endswith("/rerank")
    assert captured["auth"].startswith("Bearer")
    assert captured["body"]["model"] == "qwen3-rerank"
    assert captured["body"]["query"] == "向量数据库选型"
    assert captured["body"]["documents"] == ["doc-zero", "doc-one", "doc-two"]
    assert scores == [0.42, None, 0.88]  # 按 docs index 对位（未返回的 index=None）


def test_rerank_via_gateway_raises_on_http_error() -> None:
    """网关非 2xx → 异常上抛（节点兜底路径触发面）。"""
    import httpx as _httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return _httpx.Response(502, text="bad gateway")

    real_client = _httpx.AsyncClient

    def _factory(**kwargs: Any) -> Any:
        kwargs["transport"] = _httpx.MockTransport(handler)
        return real_client(**kwargs)

    original = fuse_module.httpx.AsyncClient
    fuse_module.httpx.AsyncClient = _factory  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError):
            asyncio.run(rerank_via_gateway(
                "q", ["d"], model_alias="qwen3-rerank", timeout_s=2.0
            ))
    finally:
        fuse_module.httpx.AsyncClient = original
