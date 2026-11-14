"""integration：rewrite 节点真 LLM 结构冒烟（requires_llm——本地 light 经 LiteLLM 网关）。

口径（DEV §4 integration / §5.4 DoD 2）：
- **断言结构不断言文案**——rewritten_queries 非空字符串数组、query_type ∈ 7 类枚举、
  answerable 为布尔、time_range ∈ 枚举∪{None}、夹取域 1~4；答案措辞质量归 M4 评测线；
- 走**真 fast 子图 astream**（经主图壳同款调用面）：节点内 get_stream_writer 在
  runnable context 内，stage(rewrite, running→done) 信号序列一并取证（api.md §7.2）；
- 提示词消费真资产中心快照（L1/L4 均可）；网关/light 别名不可达时 skip（记录到
  skipped，不算失败）。
"""

from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.requires_llm

#: 快速主干头部（api.md §7.4 前两帧）
_EXPECTED_HEAD = [
    {"stage_key": "rewrite", "status": "running"},
    {"stage_key": "rewrite", "status": "done"},
]


def _light_alias_ready() -> str | None:
    """网关/light 别名探活；不可达原因字符串（None=就绪）。不打印任何凭据。"""
    from app.core.config import litellm_settings

    try:
        resp = httpx.get(
            str(litellm_settings.base_url).rstrip("/") + "/v1/models",
            headers={
                "Authorization": "Bearer " + litellm_settings.api_key.get_secret_value()
            },
            timeout=5,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 —— 探活失败统一 skip
        return f"LiteLLM 网关不可达（{type(exc).__name__}）"
    ids = {m.get("id") for m in resp.json().get("data", [])}
    if "qwen3.5-4b-ollama" not in ids:
        return "light 别名未装载（qwen3.5-4b-ollama 不在网关模型清单）"
    return None


async def test_rewrite_real_llm_structure_via_fast_graph() -> None:
    """真 LLM 冒烟（经 fast 子图）：rewrite 两阶段信号 + FastState 产出结构齐全。"""
    skip_reason = _light_alias_ready()
    if skip_reason:
        pytest.skip(skip_reason)

    from app.agent.graphs.fast import graph as fast_graph
    from app.agent.state import FastState
    from app.config_center.defaults import SPEC_CONFIG

    state: dict[str, Any] = {
        "query": "FastAPI 怎么配置 CORS 允许跨域请求？",
        "query_lang": "zh",
        "history": [{"q": "什么是 CORS", "a_head": "跨域资源共享（CORS）是浏览器机制…"}],
        "spec": SPEC_CONFIG,  # 真实内置默认 spec（type_query_map/retry/超时全套）
    }
    # 本冒烟只考 rewrite——搜索面以假体注入（web_search 拓扑在 rewrite 之后，
    # requires_llm 用例豁免 conftest 桩流假体，需自备零网搜索面）
    from app.agent.tools.registry import SearchChain

    class _StubSearchRegistry:
        def resolve(self, payload: Any, query_lang: str) -> SearchChain:
            return SearchChain(sources=["tavily"], backup=None, degraded=False, detail="stub")

        def credential_ready(self, server: str) -> bool:
            return True

        async def search(
            self, query: str, source: str, *, time_range: str | None = None, top_k: int = 5
        ) -> list[dict[str, Any]]:
            return [{"url": "https://stub.example/1", "title": "stub", "snippet": "s", "score": 0.9, "origin": source}]

    config = {
        "configurable": {
            "thread_id": "it-rewrite-th",
            "turn_id": "it-rewrite-turn",
            "model_mapping": {"light": "qwen3.5-4b-ollama"},
            "search_chain": _StubSearchRegistry().resolve({}, "zh"),
            "tools_registry": _StubSearchRegistry(),
        }
    }

    stage_signals: list[dict[str, Any]] = []
    final: dict[str, Any] = {}
    # 子图直跑（无 subgraphs=True）：顶层流为 (stream_mode, chunk) 二元组
    async for stream_mode, chunk in fast_graph.astream(
        state, config=config, stream_mode=["custom", "updates"]
    ):
        if stream_mode == "custom" and chunk.get("type") == "stage":
            if chunk.get("stage_key") == "rewrite":
                stage_signals.append(chunk)
        elif stream_mode == "updates":
            for node_update in chunk.values():
                final.update(node_update)

    # stage 信号：done 帧（running 已由 TurnRunner 图前发布——§4.3，不在子图事件流）
    assert [(s["stage_key"], s["status"]) for s in stage_signals] == [("rewrite", "done")]
    # FastState 产出结构（断言结构不断言文案）
    rewrite_keys = {
        "query_type",
        "answerable_from_knowledge",
        "search_strategy",
        "time_range",
        "rewritten_queries",
    }
    assert rewrite_keys <= set(final)
    assert final["search_strategy"] in {"direct", "verify", "web"}  # v1.20 三值枚举
    assert set(final) <= set(FastState.__annotations__)  # 无未声明字段
    queries = final["rewritten_queries"]
    assert isinstance(queries, list) and queries
    assert all(isinstance(q, str) and q.strip() for q in queries)
    assert 1 <= len(queries) <= 4  # 夹取域（nacos-config §3：1~4）
    assert final["query_type"] in {
        "factoid", "list", "comparison", "multi_facet", "temporal", "how_to", "other",
    }
    assert isinstance(final["answerable_from_knowledge"], bool)
    assert final["time_range"] in {"day", "week", "month", "year", None}
