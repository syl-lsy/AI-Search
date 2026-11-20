"""integration：deep reflect 节点真 LLM 结构冒烟（requires_llm——chat-standard 经
LiteLLM 网关；M2 批次 3 reflect 节点五步闭环 DoD 2）。

口径（DEV §4 integration / §5.4 DoD 2，同 test_deep_planner_node.py 先例）：
- **断言结构不断言文案**——gaps 为 list 且每条含 gap/query 字符串与 origin ∈ {web,kb}
  （M2 只产 web；kb 取向保留待 supplement 跳过——批次 3 supplement 闭环语义）；
  缺口判定/补搜方向质量归 M4 评测线；
- 走**真 deep 子图 astream**（经主图壳同款调用面）：节点内 get_stream_writer 在
  runnable context 内，stage(reflect, running/done) 信号一并取证（api.md §7.4:866
  形态——running 仅在进补搜轮时发射、label=反思第 n/{rounds} 轮 + detail=补搜方向；
  回路退出发 done）；
- planner/research 段以桩替代（结构化 fixture + 零派发桩——链注入 + build_chat_model
  桩），reflect 为本测试唯一真依赖面；
- 提示词消费真资产中心快照（L1/L4 均可；deep.reflect 零占位符——新旧模板同文，
  无发布次序依赖）；网关/chat-standard 别名不可达时 skip（记录到 skipped，不算失败）。
"""

from typing import Any

import httpx
import pytest

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


async def test_reflect_real_llm_structure_via_deep_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真 LLM 冒烟（经 deep 子图直跑）：stage(reflect, done) 帧可达 + running 帧形态
    （若有）符合 §7.4 + gaps 结构合法 + 零 degraded（兜底不置 degraded 契约）。"""
    skip_reason = _chat_alias_ready()
    if skip_reason:
        pytest.skip(skip_reason)

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from app.agent.graphs.deep import graph as deep_graph
    from app.agent.nodes import deep_planner as deep_planner_module
    from app.agent.nodes import deep_research as deep_research_module
    from app.agent.state import DeepState
    from app.agent.tools.registry import ReadChain, SearchChain
    from app.config_center.defaults import SPEC_CONFIG

    class _PlanChain:
        async def ainvoke(self, prompt: Any, config: Any = None) -> dict[str, Any]:
            return {
                "subtopics": ["pgvector 在个人知识库场景的能力与边界", "Milvus 同场景对比"],
                "outline": [
                    {"section": "背景与选型要素", "intent": "个人知识库场景的技术诉求"},
                    {"section": "两库能力对比", "intent": "对称覆盖 pgvector 与 Milvus"},
                    {"section": "选型建议", "intent": "按场景给出结论"},
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

    class _ResearchMainModel(GenericFakeChatModel):
        """派发桩：单帧纯文本收尾（零 task 调用）——bind_tools 返回 self（探针同款）。"""

        def bind_tools(self, tools: Any, **kwargs: Any) -> "_ResearchMainModel":  # noqa: ANN201
            return self

    monkeypatch.setattr(deep_planner_module, "build_chat_model", lambda alias, **kw: _PlannerModel())
    monkeypatch.setattr(
        deep_research_module,
        "build_chat_model",
        lambda alias, **kw: _ResearchMainModel(messages=iter([AIMessage(content="零派发汇总收尾。")])),
    )
    monkeypatch.setattr(
        deep_research_module,
        "_subagent_model",
        lambda alias, index: _ResearchMainModel(messages=iter([AIMessage(content="保留。")])),
    )

    state: dict[str, Any] = {
        "query": "pgvector 和 Milvus 个人知识库场景怎么选？",
        "query_lang": "zh",
        "spec": SPEC_CONFIG,  # 真实内置默认 spec（reflect_rounds=2 / max=3 / llm_timeout_s=60 全套）
        "chat_model_key": "qwen",
    }
    class _ZeroCandidateRegistry:
        """M2 批次 3 增量：supplement 已接真——真 reflect 判 gaps 非空会进补搜。
        本测试唯一真依赖面是 reflect，补搜段以零候选假 registry 替代（零外网调用；
        其真实依赖行为由 test_deep_supplement_node.py / requires_mcp 层承担）。"""

        async def search(self, query: Any, source: Any, **kw: Any) -> list[dict[str, Any]]:
            return []

    config = {
        "configurable": {
            "thread_id": "it-reflect-th",
            "turn_id": "it-reflect-turn",
            "model_mapping": {"chat": {"options": {"qwen": "chat-standard"}, "default": "qwen"}},
            "search_chain": SearchChain(sources=["tavily"], backup=None, degraded=False, detail="tavily"),
            "read_chain": ReadChain(sources=["firecrawl"], backup=None, degraded=False, detail="firecrawl"),
            "tools_registry": _ZeroCandidateRegistry(),
        }
    }

    reflect_signals: list[dict[str, Any]] = []
    final: dict[str, Any] = {}
    # 子图直跑（无 subgraphs=True）：顶层流为 (stream_mode, chunk) 二元组
    async for stream_mode, chunk in deep_graph.astream(
        state, config=config, stream_mode=["custom", "updates"]
    ):
        if stream_mode == "custom" and chunk.get("type") == "stage":
            if chunk.get("stage_key") == "reflect":
                reflect_signals.append(chunk)
        elif stream_mode == "updates":
            for node_update in chunk.values():
                if isinstance(node_update, dict):
                    final.update(node_update)

    # reflect 帧可达（真依赖面冒烟的最小判据——判 sufficient 或进补搜均有帧）
    assert reflect_signals, "reflect 零信号帧——节点未执行或事件面缺失"
    assert [s["status"] for s in reflect_signals][-1] == "done"  # 回路退出帧恒 done
    running = [s for s in reflect_signals if s["status"] == "running"]
    for frame in running:  # api.md §7.4:866 running 形态（label=反思第 n/{rounds} 轮 + detail）
        assert isinstance(frame.get("label"), str) and frame["label"].startswith("反思第")
        assert "/" in frame["label"] and frame["label"].endswith("轮")
        assert isinstance(frame.get("detail"), str) and frame["detail"]
    # running 帧数 ≤ 有效补搜轮数（SPEC_CONFIG deep.reflect_rounds=2）
    assert len(running) <= int(SPEC_CONFIG["deep"]["reflect_rounds"])

    # gaps 结构（断言结构不断言文案；兜底/零缺口形态 = 空数组合法）
    assert isinstance(final.get("gaps"), list)
    for gap in final["gaps"]:
        assert isinstance(gap, dict)
        assert isinstance(gap.get("gap"), str)
        assert isinstance(gap.get("query"), str)
        assert gap.get("origin") in {"web", "kb"}  # M2 归一后值域（web|kb）
    # 回路面：reflect_round ∈ [0, reflect_rounds]（supplement 桩递增方）
    if "reflect_round" in final:
        assert 0 <= int(final["reflect_round"]) <= int(SPEC_CONFIG["deep"]["reflect_rounds"])
    # 零 degraded（兜底不置 degraded——批次 3 收敛口径）；state 无未声明字段
    assert "degraded" not in final and "degraded_reason" not in final
    assert set(final) <= set(DeepState.__annotations__)
