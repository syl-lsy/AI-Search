"""integration：反思循环真跑 2 轮验证（批次 3 闭环条件 plan:96「反思循环真跑 2 轮」/
plan:163「真轮出现反思第 1/2 轮事件」的 integration 承载）。

**2 轮反思的确定性构造**（真 LLM 面不可控判定的稳定化）：research 零派发桩（零报告
零候选）+ 补搜零候选假 registry → reflect 真 LLM 两轮评审材料恒空（大纲有节、材料
无据）→ 强缺口信号下持续判 gaps → rounds=2 耗尽 → guards 出口收敛。真依赖面：
- **真模型**：reflect 真 LLM 多轮判定（chat-standard 经 LiteLLM 网关，.env.test
  LITELLM_* 通道——requires_llm 探活 skip 形态）；
- **真库**：.env.test DB_ 组测试库连接探活（migrated_db fixture——建库+alembic head
  +pgvector；deep 图节点零 DB 面纯函数边界〔盘点钉——节点零 DB/EventBus〕，真库
  承载在 TurnRunner 落库层/e2e，本用例以真库连通为环境前提断言）；
- **真检索编排**：supplement 真节点走真 registry.search 注入面（零候选假体——搜索
  源极端形态合法，真实源依赖归 requires_mcp 层）。

断言（ask 1 / api.md §7.4:866 + 盘点钉 ①）：
- 事件序列相对序：…→stage(reflect,running,label=反思第 n/rounds 轮,detail=补搜方向)
  →stage(search,running)→stage(search,done)→…→stage(reflect,done)→stage(write,…)
  →write done 全链（同 key 多帧合法 §7.2:694；label 分母形态=契约钉）；
- reflect_round ≤ max_reflect_rounds=3（回路有界）；
- 回路正常收敛（writer 收口 answer_md——批次 4 接真；零 error 帧；轮数耗尽后 guards 出口——
  gaps 保留诊断面不阻断）；
- 补搜 query 登记（真 LLM 产 gaps 的 query 经 supplement 发出并回写 searched_queries）。
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


class _PlanChain:
    """planner 桩：三节大纲（对照材料恒空 → 强缺口信号场景）。"""

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
            "safety": "none",
        }


class _PlannerModel:
    def with_structured_output(
        self, schema: Any, *, method: str | None = None, **kw: Any
    ) -> _PlanChain:
        return _PlanChain()


class _ZeroCandidateRegistry:
    """registry.search 假体：任意 (query, source) 返回零候选（搜索源极端形态——
    反思材料恒空构造的补搜面；真源依赖归 requires_mcp 层）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def search(self, query: Any, source: Any, **kw: Any) -> list[dict[str, Any]]:
        self.calls += 1
        return []


_FAILED_RESEARCH_UPDATE: dict[str, Any] = {
    "subagent_reports": [
        {
            "subtopic": "pgvector 在个人知识库场景的能力与边界",
            "summary": "",
            "hard_facts": [],
            "quotes": [],
            "sources": [],
            "failed": True,
        },
        {
            "subtopic": "Milvus 同场景对比",
            "summary": "",
            "hard_facts": [],
            "quotes": [],
            "sources": [],
            "failed": True,
        },
    ],
    "visited_urls": [],
    "searched_queries": [],
    "snapshots": [],
    "collected_sources": [],
    "errors": [
        {"stage": "research", "reason": "research_agent_failed", "error": "NotImplementedError"}
    ],
}


async def _failed_research_stub(state: Any, config: Any = None) -> dict[str, Any]:
    """research 整节点替换：失败报告形态（材料块显式「[研究失败] 子课题：无可用
    摘要」强缺口信号——2 轮反思的确定性构造面；实证=真 LLM 两轮持续判 gaps）。"""
    return dict(_FAILED_RESEARCH_UPDATE)


async def test_reflection_loop_two_rounds_real_llm(
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Any,
) -> None:
    """2 轮反思真跑（强缺口构造）：失败报告+零候选补搜 → 真 LLM 两轮持续判 gaps →
    rounds=2 耗尽 → guards 出口收敛。硬断言：running 帧数=2、reflect_round=2、
    事件序列全链相对序、gaps 保留（诊断面）、零 error。"""
    skip_reason = _chat_alias_ready()
    if skip_reason:
        pytest.skip(skip_reason)
    assert migrated_db is not None and migrated_db.name == "ai_search_test_db"

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    import app.agent.graphs.deep as deep_mod
    from app.agent.tools.registry import ReadChain, SearchChain
    from app.config_center.defaults import SPEC_CONFIG

    class _ResearchMainModel(GenericFakeChatModel):
        def bind_tools(self, tools: Any, **kwargs: Any) -> "_ResearchMainModel":  # noqa: ANN201
            return self

    monkeypatch.setattr(deep_mod, "research", _failed_research_stub)
    monkeypatch.setattr(deep_mod, "graph", deep_mod.build_deep_graph())

    deep_graph = deep_mod.graph
    deep_planner_module = __import__(
        "app.agent.nodes.deep_planner", fromlist=["build_chat_model"]
    )
    deep_research_module = __import__(
        "app.agent.nodes.deep_research", fromlist=["build_chat_model"]
    )
    monkeypatch.setattr(deep_planner_module, "build_chat_model", lambda alias, **kw: _PlannerModel())
    # reflect 不桩（真 LLM——本测试唯一模型依赖面）
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
        "spec": SPEC_CONFIG,  # 真实内置默认 spec（reflect_rounds=2 / max=3 全套）
        "chat_model_key": "qwen",
    }
    config = {
        "configurable": {
            "thread_id": "it-loop2-th",
            "turn_id": "it-loop2-turn",
            "model_mapping": {"chat": {"options": {"qwen": "chat-standard"}, "default": "qwen"}},
            "search_chain": SearchChain(
                sources=["tavily", "bocha"], backup=None, degraded=False, detail="tavily"
            ),
            "read_chain": ReadChain(sources=["firecrawl"], backup=None, degraded=False, detail="firecrawl"),
            "tools_registry": _ZeroCandidateRegistry(),
        }
    }

    stages: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    final: dict[str, Any] = {}
    async for stream_mode, chunk in deep_graph.astream(
        state, config=config, stream_mode=["custom", "updates"]
    ):
        if stream_mode == "custom" and isinstance(chunk, dict):
            if chunk.get("type") == "stage":
                stages.append(chunk)
            elif chunk.get("type") == "error":
                errors.append(chunk)
        elif stream_mode == "updates":
            for node_update in chunk.values():
                if isinstance(node_update, dict):
                    final.update(node_update)

    # ---- 事件序列全链（api.md §7.4:866 相对序；ask 断言面）----
    full_sequence = [(s["stage_key"], s["status"]) for s in stages]
    # plan done → [reflect running → search running/done… ×2 轮] → reflect done →
    # write running(label=撰写第 1 节) →（分节 label 推进——批次 4 writer 接真）→ write done
    assert full_sequence[0] == ("plan", "done")
    assert full_sequence[-1] == ("write", "done")
    reflect_idx = [i for i, (k, _) in enumerate(full_sequence) if k == "reflect"]
    search_idx = [i for i, (k, _) in enumerate(full_sequence) if k == "search"]
    write_idx = [i for i, (k, _) in enumerate(full_sequence) if k == "write"]
    assert len(reflect_idx) == 3 and len(search_idx) >= 5 and len(write_idx) >= 2  # 3 判定 2 轮
    assert reflect_idx[0] < search_idx[0] < reflect_idx[-1] < write_idx[0]  # 相对序贯穿

    # writer 段（批次 4 接真——api.md §7.4:867 形态）：首帧 running(label=撰写第 x 节)
    # → done(extra.sections=N)；真 LLM 报告分节推进帧数不定（≥1 running）
    write_frames = [stages[i] for i in write_idx]
    assert write_frames[0]["status"] == "running"
    assert write_frames[0]["label"].startswith("撰写第")
    assert write_frames[-1]["status"] == "done"
    assert int((write_frames[-1].get("extra") or {}).get("sections", 0)) >= 1

    # reflect 段：恰 2 个 running（反思第 1/2、2/2 轮——真 LLM 两轮持续判 gaps）
    reflect_frames = [stages[i] for i in reflect_idx]
    running = [s for s in reflect_frames if s["status"] == "running"]
    assert len(running) == 2  # 构造生效：真 LLM 两轮持续判缺口
    assert running[0]["label"] == "反思第 1/2 轮" and running[1]["label"] == "反思第 2/2 轮"
    for frame in running:
        assert frame.get("detail")  # detail=补搜方向（真 LLM 产 gaps 的 query 串联）
    assert reflect_frames[-1]["status"] == "done"  # 回路退出帧恒 done
    # 相对序：每轮 running 后有 search running（supplement 补发——盘点钉 ①）
    for frame_index in (stages.index(running[0]), stages.index(running[1])):
        after = stages[frame_index + 1:]
        next_reflect = next((s for s in after if s["stage_key"] == "reflect"), None)
        end = stages.index(next_reflect) if next_reflect is not None else len(stages)
        segment = stages[frame_index + 1 : end]
        assert any(
            s["stage_key"] == "search" and s["status"] == "running" for s in segment
        )

    # ---- 回路有界 + 收敛（ask 断言面）----
    assert final.get("reflect_round") == 2  # 递增方=supplement（两轮补搜）
    assert int(final["reflect_round"]) <= int(SPEC_CONFIG["deep"]["max_reflect_rounds"]) == 3
    assert final.get("answer_md")  # writer 收口（真 LLM 报告——批次 4 接真）
    assert errors == []  # 零 error 帧（轮数耗尽=guards 出口正常收敛非故障）
    assert len(final.get("gaps") or []) > 0  # 轮数耗尽形态 gaps 保留（诊断面不阻断）
    for gap in final["gaps"]:
        assert gap.get("origin") in {"web", "kb"}
        assert isinstance(gap.get("query"), str) and gap.get("query")
    # 补搜 query 登记：仅「实际补搜过的轮」的 query 入 searched_queries（未发不登记
    # ——deep_supplement 钉死语义；轮数耗尽/退出判定的产出 gaps 不经 supplement，
    # 其 query 不登记——保留 gaps 与 searched 的包含关系不成立）
    assert len(final.get("searched_queries") or []) > 0  # 补搜轮 query 已登记
    assert "degraded" not in final and "degraded_reason" not in final
    from app.agent.state import DeepState

    assert set(final) <= set(DeepState.__annotations__)


async def test_reflection_loop_real_llm_converges_within_rounds(
    monkeypatch: pytest.MonkeyPatch,
    migrated_db: Any,
) -> None:
    """反思循环真跑：真 LLM reflect（材料恒空构造）→ 事件序列全链 + round ≤ max
    + 回路收敛。真库前提断言（migrated_db 连通）；网关不可达 skip。"""
    skip_reason = _chat_alias_ready()
    if skip_reason:
        pytest.skip(skip_reason)
    # 真库前提（migrated_db fixture 已建库+alembic head——连接探活断言）
    assert migrated_db is not None and migrated_db.name == "ai_search_test_db"

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from app.agent.graphs.deep import graph as deep_graph
    from app.agent.nodes import deep_planner as deep_planner_module
    from app.agent.nodes import deep_research as deep_research_module
    from app.agent.state import DeepState
    from app.agent.tools.registry import ReadChain, SearchChain
    from app.config_center.defaults import SPEC_CONFIG

    class _ResearchMainModel(GenericFakeChatModel):
        def bind_tools(self, tools: Any, **kwargs: Any) -> "_ResearchMainModel":  # noqa: ANN201
            return self

    monkeypatch.setattr(deep_planner_module, "build_chat_model", lambda alias, **kw: _PlannerModel())
    # reflect 不桩（真 LLM——本测试唯一模型依赖面）
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
        "spec": SPEC_CONFIG,  # 真实内置默认 spec（reflect_rounds=2 / max=3 全套）
        "chat_model_key": "qwen",
    }
    config = {
        "configurable": {
            "thread_id": "it-loop-th",
            "turn_id": "it-loop-turn",
            "model_mapping": {"chat": {"options": {"qwen": "chat-standard"}, "default": "qwen"}},
            "search_chain": SearchChain(
                sources=["tavily", "bocha"], backup=None, degraded=False, detail="tavily"
            ),
            "read_chain": ReadChain(sources=["firecrawl"], backup=None, degraded=False, detail="firecrawl"),
            "tools_registry": _ZeroCandidateRegistry(),  # 零候选——材料恒空构造
        }
    }

    stages: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    final: dict[str, Any] = {}
    async for stream_mode, chunk in deep_graph.astream(
        state, config=config, stream_mode=["custom", "updates"]
    ):
        if stream_mode == "custom" and isinstance(chunk, dict):
            if chunk.get("type") == "stage":
                stages.append(chunk)
            elif chunk.get("type") == "error":
                errors.append(chunk)
        elif stream_mode == "updates":
            for node_update in chunk.values():
                if isinstance(node_update, dict):
                    final.update(node_update)

    # ---- 事件序列全链（api.md §7.4:866 相对序 + §7.2:694 同 key 多帧）----
    keys = [s["stage_key"] for s in stages]
    assert keys[0] == "plan" and "search" in keys and "reflect" in keys and "write" in keys
    assert keys[-1] == "write" and stages[-1]["status"] == "done"  # writer 收口=回路收敛

    # reflect 段形态：running 帧（label=反思第 n/rounds 轮 + detail=补搜方向）→ done 收口帧
    reflect_frames = [s for s in stages if s["stage_key"] == "reflect"]
    assert reflect_frames, "reflect 零帧——节点未执行"
    assert reflect_frames[-1]["status"] == "done"  # 回路退出帧恒 done
    running = [s for s in reflect_frames if s["status"] == "running"]
    assert running, "零补搜轮——材料恒空构造下真 reflect 应至少判一轮缺口（如实记录实测轮数）"
    for frame in running:
        assert isinstance(frame.get("label"), str) and frame["label"].startswith("反思第")
        assert "/" in frame["label"] and frame["label"].endswith("轮")  # 分母形态=契约钉
        assert isinstance(frame.get("detail"), str) and frame["detail"]  # 补搜方向
    # running 帧数 ≤ 有效轮数（reflect_rounds=2——每轮补搜至多一个 running 帧）
    assert len(running) <= int(SPEC_CONFIG["deep"]["reflect_rounds"])

    # 相对序：每个 reflect running 之后、下个 reflect running/done 之前有 search running
    # （补搜段——supplement 真节点补发，盘点钉 ① 相对序）
    for index, frame in enumerate(reflect_frames):
        if frame["status"] != "running":
            continue
        after = stages[stages.index(frame) + 1:]
        next_reflect = next(
            (s for s in after if s["stage_key"] == "reflect"), None
        )
        segment = after if next_reflect is None else after[: stages.index(next_reflect) - stages.index(frame)]
        assert any(
            s["stage_key"] == "search" and s["status"] == "running" for s in segment
        ), f"reflect running 后无 search running 帧（第 {index + 1} 轮）"

    # ---- 回路有界 + 收敛 ----
    assert "reflect_round" in final  # supplement 递增方（进过补搜）
    assert 0 <= int(final["reflect_round"]) <= int(SPEC_CONFIG["deep"]["max_reflect_rounds"])
    assert final.get("answer_md")  # writer 收口（真 LLM 报告——批次 4 接真）
    assert errors == []  # 零 error 帧（轮数耗尽=guards 出口正常收敛非故障）
    assert isinstance(final.get("gaps"), list)  # 轮数耗尽形态 gaps 保留（诊断面）
    for gap in final["gaps"]:
        assert gap.get("origin") in {"web", "kb"}  # M2 归一后值域
        assert isinstance(gap.get("query"), str)
    # 补搜 query 登记：仅「实际补搜过的轮」的 query 入 searched_queries（未发不登记
    # ——deep_supplement 钉死语义；轮数耗尽/退出判定的产出 gaps 不经 supplement，
    # 其 query 不登记——保留 gaps 与 searched 的包含关系不成立）
    assert len(final.get("searched_queries") or []) > 0  # 补搜轮 query 已登记
    # 零 degraded（补搜轮零熔断——collector/deadline 无注入面）；state 键纪律
    assert "degraded" not in final and "degraded_reason" not in final
    assert set(final) <= set(DeepState.__annotations__)
