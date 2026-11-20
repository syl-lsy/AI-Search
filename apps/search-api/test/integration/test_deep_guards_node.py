"""integration：deep guards 节点真图注入面（M2 批次 3 guards 五步闭环 DoD 2/3）。

真 deep 子图直跑（脚本化 reflect——零 LLM 零网络；integration 真依赖面=图装配与
TurnRunner 同形态的注入通路），四个用例分面：
1. **预算熔断注入**：configurable 注入超限 usage_collector（真 UsageCollector
   假体形态）→ guards trip budget → final.degraded_reason="budget"（**条件边进
   degraded_writer**——批次 4 接线，ARCH §4.2:475-480 两出口；TurnRunner 分流
   done(degraded) 零 error）；
2. **硬超时注入**：turn_deadline 假体 remaining ≤ 30s → trip timeout；双条件同时
   （超限 collector + 低剩余 deadline）→ timeout 优先（轮级硬超时最后防线语义）；
3. **无 trip 直通**：正常 collector/deadline → 零 degraded_reason（usage_total
   快照仍写——检查点留痕面）；
4. **取消中途注入**（plan:163 图内面）：registry 假体在补搜执行中（「中途」）
   往进程级 cancel_flags dict 引用写标志（R14 注入身份——与 TurnRunner
   `_cancel_flags` 同一 dict 引用）→ 下一个节点边界 ensure_not_canceled pop 命中
   → TurnCanceledSignal 穿透重抛出图（取消非故障）；**零 error 帧 + 抛出前已产出
   updates 保留**（ADR-010——不进降级不产降级报告；TurnRunner 层
   done(finish=canceled) 收尾由 test_turn_service.py:500-527 中途注入面已锁）。
"""

import asyncio
from typing import Any

import pytest

from app.agent.guards import TurnCanceledSignal


class _ScriptedReflectChain:
    """reflect 结构化输出脚本链：首判 gaps（进补搜轮）→ 次判 sufficient（退出）。"""

    def __init__(self) -> None:
        self.script: list[dict[str, Any]] = [
            {"sufficient": False, "gaps": [{"gap": "g1", "query": "补搜方向", "origin": "web"}]},
            {"sufficient": True, "gaps": []},
        ]

    async def ainvoke(self, prompt: Any, config: Any = None) -> dict[str, Any]:
        return self.script.pop(0) if self.script else {"sufficient": True, "gaps": []}


class _ScriptedReflectModel:
    def __init__(self, chain: _ScriptedReflectChain) -> None:
        self._chain = chain

    def with_structured_output(
        self, schema: Any, *, method: str | None = None, **kw: Any
    ) -> _ScriptedReflectChain:
        return self._chain


class _PlanChain:
    async def ainvoke(self, prompt: Any, config: Any = None) -> dict[str, Any]:
        return {
            "subtopics": ["s1"],
            "outline": [{"section": "背景", "intent": "诉求"}],
            "query_type": "factoid",
            "time_range": None,
            "memory_directive": None,
            "safety": "none",
        }


class _PlannerModel:
    def with_structured_output(
        self, schema: Any, *, method: str | None = None, **kw: Any
    ) -> _PlanChain:
        return _PlanChain()


class _ScriptedRegistry:
    """registry.search 假体：固定候选（零外网）；可挂 hook（取消中途注入用）。"""

    def __init__(self, on_search: Any = None) -> None:
        self._on_search = on_search

    async def search(self, query: Any, source: Any, **kw: Any) -> list[dict[str, Any]]:
        if self._on_search is not None:
            self._on_search()
        return [
            {"url": "https://a.dev/1", "title": "A1", "snippet": "s1", "score": 0.9, "origin": source},
            {"url": "https://b.dev/2", "title": "B2", "snippet": "s2", "score": 0.8, "origin": source},
        ]


class _FakeCollector:
    """UsageCollector 假体（total() 返回脚本化值——轮级累计的注入形态）。"""

    def __init__(self, total: dict[str, int]) -> None:
        self._total = total

    def total(self) -> dict[str, int]:
        return dict(self._total)


class _FakeDeadline:
    """TurnDeadline 假体（remaining_s() 返回脚本化值——轮级锚点注入形态）。"""

    def __init__(self, remaining: float) -> None:
        self._remaining = remaining

    def remaining_s(self) -> float:
        return self._remaining


async def _run_deep_graph(
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """真 deep 子图直跑（planner/research 桩、reflect 脚本链、writer 流式假体、
    degraded_writer 单调用假体、supplement/guards/finalize 真节点——批次 4 全接真后
    全 LLM 面零真模型）。

    返回 (stage 帧, custom 帧, 终态 updates 合并)。"""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage, AIMessageChunk

    from app.agent.graphs.deep import graph as deep_graph
    from app.agent.nodes import deep_degraded_writer as deep_degraded_module
    from app.agent.nodes import deep_planner as deep_planner_module
    from app.agent.nodes import deep_reflect as deep_reflect_module
    from app.agent.nodes import deep_research as deep_research_module
    from app.agent.nodes import deep_writer as deep_writer_module

    class _ResearchMainModel(GenericFakeChatModel):
        def bind_tools(self, tools: Any, **kwargs: Any) -> "_ResearchMainModel":  # noqa: ANN201
            return self

    class _WriterModel:
        """writer 流式假体（批次 4 接真）：两片报告正文（零网络——unit mock 纪律）。"""

        async def astream(self, prompt: Any, config: Any = None, **kw: Any) -> Any:
            for piece in ("## 摘要\n\n要点[1]。\n\n", "## 结论\n\n收束[1]。"):
                yield AIMessageChunk(content=piece)

    class _DegradedModel:
        """degraded_writer 单调用假体（批次 4 接真）：阶段性正文（零网络）。"""

        async def ainvoke(self, prompt: Any, config: Any = None, **kw: Any) -> Any:
            return AIMessage(content="阶段性发现：已收集材料要点。")

    reflect_chain = _ScriptedReflectChain()  # 闭包单例——每次 build_chat_model 引用同一链
    monkeypatch.setattr(deep_planner_module, "build_chat_model", lambda alias, **kw: _PlannerModel())
    monkeypatch.setattr(
        deep_reflect_module, "build_chat_model", lambda alias, **kw: _ScriptedReflectModel(reflect_chain)
    )
    monkeypatch.setattr(deep_writer_module, "build_chat_model", lambda alias, **kw: _WriterModel())
    monkeypatch.setattr(
        deep_degraded_module, "build_chat_model", lambda alias, **kw: _DegradedModel()
    )
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
        "query": "guards 注入实测",
        "query_lang": "zh",
        "spec": {"deep": {"reflect_rounds": 2, "max_reflect_rounds": 3, "token_budget": 500000, "turn_timeout_s": 300}},
        "chat_model_key": "qwen",
    }
    stages: list[dict[str, Any]] = []
    custom: list[dict[str, Any]] = []
    final: dict[str, Any] = {}
    async for stream_mode, chunk in deep_graph.astream(
        state, config=config, stream_mode=["custom", "updates"]
    ):
        if stream_mode == "custom" and isinstance(chunk, dict):
            custom.append(chunk)
            if chunk.get("type") == "stage":
                stages.append(chunk)
        elif stream_mode == "updates":
            for node_update in chunk.values():
                if isinstance(node_update, dict):
                    final.update(node_update)
    return stages, custom, final


def _base_config(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    from app.agent.tools.registry import ReadChain, SearchChain

    configurable: dict[str, Any] = {
        "thread_id": "it-guards-th",
        "turn_id": "it-guards-turn",
        "model_mapping": {"chat": {"options": {"qwen": "chat-standard"}, "default": "qwen"}},
        "search_chain": SearchChain(
            sources=["tavily", "bocha"], backup=None, degraded=False, detail="tavily"
        ),
        "read_chain": ReadChain(sources=["firecrawl"], backup=None, degraded=False, detail="firecrawl"),
    }
    if extra:
        configurable.update(extra)
    return {"configurable": configurable}


async def test_budget_trip_overwrites_nothing_and_degrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预算熔断注入：usage_total=600000 ≥ 500000 → degraded_reason="budget" 置位。

    评审修复 1 后的语义（发现 2——reflect 预 trip）：**首轮 reflect 判定前即收敛**
    （reflect 读同一注入面 → gaps=[]+degraded_reason=budget → route 走 guards 出口
    ——零 LLM 调用零补搜，supplement 不运行故 reflect_round 不入 state），guards 幂
    等（已置不覆写）→ **条件边进 degraded_writer**（批次 4 接线——ARCH §4.2:475-480
    两出口；正常轮 writer/finalize 路径结构性不可达）；零 error 帧（图内熔断零 error
    ——api.md:845 v1.21 注）；零 degraded 布尔（D2）。"""
    registry = _ScriptedRegistry()
    config = _base_config(
        {"tools_registry": registry, "usage_collector": _FakeCollector({"total_tokens": 600000})}
    )
    stages, custom, final = await _run_deep_graph(monkeypatch, config)

    assert final["degraded_reason"] == "budget"
    assert final["usage_total"] == 600000  # guards 检查点快照 + degraded_writer 终值回填
    assert "degraded" not in final  # D2：guards/degraded_writer 不写 degraded 布尔
    assert final.get("answer_md", "").startswith("⚠️ 研究未完成")  # 降级报告显著标注
    assert "阶段性发现" in final["answer_md"]  # degraded_writer 假体正文（writer 被隔离）
    assert "reflect_round" not in final  # 预 trip 收敛——supplement 未运行（递增方不在场）
    assert [f["type"] for f in custom if f.get("type") == "error"] == []  # 零 error 帧
    assert [f["stage_key"] for f in stages if f.get("stage_key") == "search" and "subagents" not in (f.get("extra") or {})] == []  # 零补搜帧
    # 降级路径判别面：write done 裸帧零 extra（writer/finalize 路径的 done 带 sections）
    done_frames = [f for f in stages if f.get("stage_key") == "write" and f.get("status") == "done"]
    assert len(done_frames) == 1 and "extra" not in done_frames[0]
    # fallback_token 整块单事件（盘点 decisions[4]——publish-only 不落库）
    assert [f for f in custom if f.get("type") == "fallback_token"]


async def test_timeout_trip_and_priority_over_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """硬超时注入：remaining=5s ≤ 30s → timeout；双条件（超限 collector）同时
    满足 → timeout 优先（轮级硬超时=最后防线语义）；条件边进 degraded_writer
    （批次 4 接线）。"""
    registry = _ScriptedRegistry()
    config = _base_config(
        {
            "tools_registry": registry,
            "usage_collector": _FakeCollector({"total_tokens": 600000}),
            "turn_deadline": _FakeDeadline(5.0),
        }
    )
    stages, _, final = await _run_deep_graph(monkeypatch, config)

    assert final["degraded_reason"] == "timeout"
    assert final["usage_total"] == 600000
    assert final.get("answer_md", "").startswith("⚠️ 研究未完成")  # 降级报告显著标注
    done_frames = [f for f in stages if f.get("stage_key") == "write" and f.get("status") == "done"]
    assert len(done_frames) == 1 and "extra" not in done_frames[0]  # 降级路径裸 done


async def test_no_trip_completes_with_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无 trip 直通：充足预算 + 充足 deadline → 零 degraded_reason；usage_total
    快照仍写（检查点留痕面——trip 与否均写）。"""
    registry = _ScriptedRegistry()
    config = _base_config(
        {
            "tools_registry": registry,
            "usage_collector": _FakeCollector({"total_tokens": 100000}),
            "turn_deadline": _FakeDeadline(240.0),
        }
    )
    _, custom, final = await _run_deep_graph(monkeypatch, config)

    assert "degraded_reason" not in final
    assert final["usage_total"] == 100000
    assert final.get("answer_md")
    assert [f["type"] for f in custom if f.get("type") == "error"] == []


async def test_cancel_midflight_preserves_output_and_zero_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消中途注入（plan:163 图内面）：registry 补搜执行中往进程级 cancel_flags
    dict **引用**写标志（与 TurnRunner _cancel_flags 同一引用——R14 注入身份）→
    下一个节点边界 pop 命中 → TurnCanceledSignal 穿透重抛出图；**零 error 帧 +
    抛出前已产出 updates 保留**（ADR-010——取消不进降级不产降级报告；pop 即感知+
    清理与 TurnRunner 同语义）。"""
    cancel_flags: dict[str, str] = {}  # 进程级引用（TurnRunner _cancel_flags 同形态）

    def inject_cancel_midflight() -> None:
        # 「中途」= 补搜 search 执行中登记（下个节点边界=reflect 重入时感知）
        cancel_flags["it-guards-turn"] = "user"

    registry = _ScriptedRegistry(on_search=inject_cancel_midflight)
    config = _base_config({"tools_registry": registry, "cancel_flags": cancel_flags})

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from app.agent.graphs.deep import graph as deep_graph
    from app.agent.nodes import deep_planner as deep_planner_module
    from app.agent.nodes import deep_reflect as deep_reflect_module
    from app.agent.nodes import deep_research as deep_research_module

    class _ResearchMainModel(GenericFakeChatModel):
        def bind_tools(self, tools: Any, **kwargs: Any) -> "_ResearchMainModel":  # noqa: ANN201
            return self

    reflect_chain = _ScriptedReflectChain()  # 闭包单例——每次 build_chat_model 引用同一链
    monkeypatch.setattr(deep_planner_module, "build_chat_model", lambda alias, **kw: _PlannerModel())
    monkeypatch.setattr(
        deep_reflect_module, "build_chat_model", lambda alias, **kw: _ScriptedReflectModel(reflect_chain)
    )
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
        "query": "guards 取消中途注入实测",
        "query_lang": "zh",
        "spec": {"deep": {"reflect_rounds": 2, "max_reflect_rounds": 3}},
        "chat_model_key": "qwen",
    }
    custom: list[dict[str, Any]] = []
    updates_seen: list[dict[str, Any]] = []

    async def consume() -> None:
        async for stream_mode, chunk in deep_graph.astream(
            state, config=config, stream_mode=["custom", "updates"]
        ):
            if stream_mode == "custom" and isinstance(chunk, dict):
                custom.append(chunk)
            elif stream_mode == "updates":
                for node_update in chunk.values():
                    if isinstance(node_update, dict):
                        updates_seen.append(node_update)

    with pytest.raises(TurnCanceledSignal) as exc_info:
        await asyncio.wait_for(consume(), timeout=30)

    assert exc_info.value.reason == "user"  # 取消非故障（穿透重抛）
    assert cancel_flags == {}  # pop 即感知+清理（TurnRunner _cancel_flags 同语义）
    assert [f["type"] for f in custom if f.get("type") == "error"] == []  # 零 error 帧
    # 已产出保留：抛出前 supplement 的补搜产出已在 updates 流（collect_url 入册）
    produced_urls = {
        item["url"]
        for update in updates_seen
        for item in (update.get("collected_sources") or [])
        if isinstance(item, dict)
    }
    assert produced_urls, "取消前补搜产出应保留在 updates 流（ADR-010 保留已产出）"
    # 零降级形态：抛出前无 degraded_reason 置位（取消不进降级）
    assert all("degraded_reason" not in update for update in updates_seen)
