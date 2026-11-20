"""integration：deep research 节点真 LLM 结构冒烟（requires_llm——chat-standard 经
LiteLLM 网关；M2 批次 2 research 节点五步闭环 DoD 2）。

口径（DEV §4 integration / §5.4 DoD 2，同 test_deep_planner_node.py 先例）：
- **走真 deep 子图 astream**（图内 runnable context——stage/sources 信号经
  get_stream_writer 一并取证；planner 打桩控规模为单子课题，research 消费真 LLM）；
- **断言结构不断言文案**——subagent_reports ≥1 条、六键齐全（subtopic/summary/
  hard_facts/quotes/sources/failed）、failed 为 bool、subtopic 命中子课题集（回传
  schema 面 = SummarySchema 契约 + AutoStrategy/围栏剥离兜底的实际生效形态——两种
  路径统一在门控捕获面兜住，本测试断言共同出口）；
- **工具调用行为**——searched_queries 非空（子代理真实发起了 web_search 工具调用；
  OQ-34 探针 light 三轮零工具调用直答 vs standard 工具环完整——本断言即该裁决的
  回归护栏面）、search running 帧带 extra.subagents；
- registry/读取链用确定性假体（搜索候选离线可复现）；save_snapshot 打桩（快照链的
  真库/真 MinIO 接线实测按计划归批次 2 闭环条件单独验证——此处隔离 infra 面，聚焦
  LLM 行为与回传 schema）；
- 网关/chat-standard 别名不可达时 skip（记录到 skipped，不算失败）。
"""

from typing import Any

import httpx
import pytest

from app.agent.tools.registry import ReadChain, SearchChain, ToolRegistry

pytestmark = pytest.mark.requires_llm


def _chat_alias_ready() -> str | None:
    """网关/chat-standard 别名探活；不可达原因字符串（None=就绪）。不打印任何凭据。"""
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
    if "chat-standard" not in ids:
        return "chat 别名未装载（chat-standard 不在网关模型清单）"
    return None


class _FakeRegistry(ToolRegistry):
    """确定性搜索假体：单源返回固定两候选（离线可复现——LLM 行为是唯一真依赖）。"""

    async def search(
        self, query: str, source: str, *, time_range: str | None = None, top_k: int = 5,
        retry_sink: dict[str, int] | None = None,
    ) -> list[dict[str, Any]]:
        return [
            {
                "url": "https://langchain-ai.github.io/langgraph/",
                "title": "LangGraph Overview",
                "snippet": "LangGraph 是低级编排框架，适合可控的多代理流程。",
                "score": 0.9,
                "origin": source,
            },
            {
                "url": "https://github.com/langchain-ai/deepagents",
                "title": "deepagents",
                "snippet": "deepagents 提供深度研究代理的中间件装配面。",
                "score": 0.8,
                "origin": source,
            },
        ]

    def credential_ready(self, server: str) -> bool:
        return True


async def test_research_real_llm_structure_via_deep_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真 LLM 冒烟（经 deep 子图直跑）：research 回传结构 + 工具调用行为 + 事件面。"""
    skip_reason = _chat_alias_ready()
    if skip_reason:
        pytest.skip(skip_reason)

    import asyncio

    from app.agent.graphs.deep import graph as deep_graph
    from app.agent.nodes import deep_planner as deep_planner_module
    from app.agent.nodes import deep_reflect as deep_reflect_module
    from app.agent.nodes import deep_research as deep_research_module
    from app.agent.state import DeepState

    # planner 打桩控规模：单子课题（research 消费真 LLM——本测试真依赖面）
    class _PlanChain:
        async def ainvoke(self, prompt: Any, config: Any = None) -> dict[str, Any]:
            return {
                "subtopics": ["LangGraph 与 deepagents 的定位差异与各自适用场景"],
                "outline": [
                    {"section": "定位差异", "intent": "两者的层次关系与分工"},
                    {"section": "适用场景", "intent": "各自适合的落地形态"},
                ],
                "query_type": "comparison",
                "time_range": None,
                "memory_directive": None,
            }

    class _PlannerModel:
        def with_structured_output(
            self, schema: Any, *, method: str | None = None, **kw: Any
        ) -> _PlanChain:
            return _PlanChain()

    async def fake_snapshot(**kwargs: Any) -> dict[str, Any]:  # infra 面隔离（见模块 docstring）
        return {
            "snapshot_id": "00000000-0000-0000-0000-00000000000b",
            "url": kwargs["url"],
            "url_hash": "h" * 64,
            "object_key": f"snapshots/it/{kwargs['url']}",
            "size_bytes": 2048,
            "reused": False,
            "oversize": False,
        }

    class _ReflectChain:
        """M2 批次 3 增量：deep 子图 reflect 已接真——本测试唯一真依赖面是 research，
        reflect 段以桩替代（首判 sufficient 回路退出；其真 LLM 行为由
        test_deep_reflect_node.py 冒烟承担）。"""

        async def ainvoke(self, prompt: Any, config: Any = None) -> dict[str, Any]:
            return {"sufficient": True, "gaps": []}

    class _ReflectModel:
        def with_structured_output(
            self, schema: Any, *, method: str | None = None, **kw: Any
        ) -> _ReflectChain:
            return _ReflectChain()

    monkeypatch.setattr(deep_planner_module, "build_chat_model", lambda alias, **kw: _PlannerModel())
    monkeypatch.setattr(deep_reflect_module, "build_chat_model", lambda alias, **kw: _ReflectModel())
    monkeypatch.setattr(deep_research_module, "save_snapshot", fake_snapshot)

    subtopic = "LangGraph 与 deepagents 的定位差异与各自适用场景"
    state: dict[str, Any] = {
        "query": "LangGraph 和 deepagents 是什么关系？各自适合什么场景？",
        "query_lang": "zh",
        "history": [],
        "spec": {
            "deep": {
                "max_subagents": 1,
                "read_min": 1,
                "read_max": 2,
                "summary_max_tokens": 1200,
                "recursion_limit": 25,
                "turn_timeout_s": 300,
            }
        },
        "turn_id": "it-research-turn",
        "thread_id": "it-research-th",
        "user_id": "00000000-0000-4000-8000-000000000001",
        "chat_model_key": "qwen",
    }
    config: dict[str, Any] = {
        "configurable": {
            "thread_id": "it-research-th",
            "turn_id": "it-research-turn",
            "model_mapping": {"chat": {"options": {"qwen": "chat-standard"}, "default": "qwen"}},
            "search_chain": SearchChain(sources=["tavily"], backup=None, degraded=False, detail="tavily"),
            "read_chain": ReadChain(sources=["firecrawl"], backup=None, degraded=False, detail="firecrawl"),
            "tools_registry": _FakeRegistry(),
        }
    }

    stages: list[dict[str, Any]] = []
    sources_signals: list[dict[str, Any]] = []
    final: dict[str, Any] = {}

    async def _run() -> None:
        # 子图直跑（无 subgraphs=True）：顶层流为 (stream_mode, chunk) 二元组
        async for stream_mode, chunk in deep_graph.astream(
            state, config=config, stream_mode=["custom", "updates"]
        ):
            if stream_mode == "custom":
                if chunk.get("type") == "stage":
                    stages.append(chunk)
                elif chunk.get("type") == "sources":
                    sources_signals.append(chunk)
            elif stream_mode == "updates":
                for node_update in chunk.values():
                    if isinstance(node_update, dict):
                        final.update(node_update)

    await asyncio.wait_for(_run(), timeout=240.0)

    # 事件面：plan done（planner 桩）+ search running 带 extra.subagents
    plan_done = next(s for s in stages if s.get("stage_key") == "plan" and s.get("status") == "done")
    assert (plan_done.get("extra") or {}).get("subtopics") == 1
    search_frames = [s for s in stages if s.get("stage_key") == "search"]
    assert search_frames and search_frames[0]["status"] == "running"
    subagents_extra = (search_frames[0].get("extra") or {}).get("subagents") or []
    assert subagents_extra and subagents_extra[0]["subtopic"]
    assert set(subagents_extra[0]) == {"subtopic", "status"}  # §7.2 钉死键面（name 未钉键已移除）

    # sources 面（若子代理发起搜索即有批次；ref_no 从 1 冻结）
    if sources_signals:
        all_items = [item for s in sources_signals for item in s["batch"]]
        assert [item["ref_no"] for item in all_items] == list(range(1, len(all_items) + 1))
        assert final["collected_sources"], "sources 批次已发但 collected_sources 为空"

    # state 面：键 ⊆ DeepState（无未声明字段）
    assert set(final) <= set(DeepState.__annotations__)
    # 回传 schema 面：≥1 条报告、六键齐全、形态合法（断言结构不断言文案）
    reports = final.get("subagent_reports") or []
    assert len(reports) >= 1
    for report in reports:
        assert set(report) == {"subtopic", "summary", "hard_facts", "quotes", "sources", "failed"}
        assert isinstance(report["failed"], bool)
        assert isinstance(report["summary"], str)
        if not report["failed"]:
            assert report["summary"].strip()
            assert isinstance(report["hard_facts"], list) and isinstance(report["quotes"], list)
            assert isinstance(report["sources"], list)
            assert all(isinstance(s, str) for s in report["sources"])
    assert any(r["subtopic"] == subtopic for r in reports)  # 子课题原文兜底面
    # 工具调用行为：子代理真实发起 web_search（OQ-34 standard 档工具环回归护栏）
    assert final.get("searched_queries"), "子代理未发起任何搜索（工具环缺失——OQ-34 裁决回归面）"
