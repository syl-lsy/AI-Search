"""unit 层：planner 节点（M2 批次 2——ARCH §4.2 planner 行 / DEV §5.4 五步闭环）。

钉住的语义（冻结契约，逐条对应测试）：
- 强模型经共享工厂（`app/agent/llm.py` build_chat_model + state.chat_model_key →
  轮内映射快照 chat.options，OQ-11 同源）`with_structured_output(PlannerOutput,
  method="function_calling")`（S15 基元，同 fast_rewrite 先例）；
- 结构化输出契约（nacos-config.md §1 deep.planner 行）：{subtopics≤spec.deep.
  max_subagents, outline:[{section,intent}], query_type(7 类), time_range,
  memory_directive}——**deep 零波及**：不产出不消费 answerable_from_knowledge /
  search_strategy（nacos §1 v1.20/v1.27 钉死；导航型判 factoid）；
- history 指代消解（ARCH §4.2 冻结要求）：initial_state.history 正序经
  「历史：{{history}} / 问题：{{query}}」注入（占位符 M2 批次 2 契约增量）；
- 失败语义（ARCH §6.2 P1，不终止）：校验失败 → 修复回喂 1 次 → 仍失败走机制兜底
  单子课题（factoid 化，显式常量）；传输失败 → 直接机制兜底；**兜底不置 DeepState.
  degraded**（不属 api.md §7.2 degraded_reason 枚举任一语义——fast_rewrite 2026-11-13
  B 裁决同款），留痕面 = errors[] + stage done.extra.fallback；
- TurnDeadline 包络（spec.deep.llm_timeout_s，缺省 60）：主调用 asyncio.timeout 包络，
  修复回喂前查剩余预算（耗尽即跳过回喂直入兜底）；
- ensure_not_canceled 入口接线（R14② 跨子图取消感知，同 fast 五节点先例）；
- 事件：stage(plan, running→done)——running 由 TurnRunner 图前发布（M2 批次 1，
  §4.3 无重复 seq），节点只发 done 且恒带 extra.subtopics=N（api.md §7.4
  「stage(plan,done,extra.subtopics=3)」钉死形态）；
- state 写入与 DeepState 声明一致（R14① 投影键已就位）：仅
  subtopics/outline/query_type/time_range（兜底追加 errors）。
"""

from typing import Any

import httpx
import pytest
from langchain_core.exceptions import OutputParserException

from app.agent.nodes import deep_planner
from app.agent.nodes.deep_planner import planner

_SPEC: dict[str, Any] = {
    "deep": {
        "max_subagents": 3,
        "llm_timeout_s": 60,
        "turn_timeout_s": 300,
        "retry": {"max_attempts": 1, "initial_interval_s": 0.01, "backoff_factor": 2.0, "jitter": False},
    }
}

_STATE: dict[str, Any] = {
    "query": "pgvector 和 Milvus 个人知识库场景怎么选？",
    "query_lang": "zh",
    "history": [{"q": "什么是向量数据库", "a_head": "向量数据库是…"}],
    "spec": _SPEC,
}

#: 成功 fixture（结构化输出，S15；unit 全 mock 零网络）
_OK_OUTPUT: dict[str, Any] = {
    "subtopics": [
        "pgvector 在个人知识库场景的能力与生态",
        "Milvus 在个人知识库场景的能力与生态",
    ],
    "outline": [
        {"section": "背景与选型要素", "intent": "个人知识库场景的技术诉求"},
        {"section": "两库对比", "intent": "对称覆盖 pgvector 与 Milvus 的关键维度"},
        {"section": "选型建议", "intent": "按场景给出结论"},
    ],
    "query_type": "comparison",
    "time_range": None,
    "memory_directive": None,
}


class _FakeChain:
    """结构化输出链假体：ainvoke 按脚本逐次弹出 dict/异常；记录收到的 prompt。"""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.prompts: list[str] = []

    async def ainvoke(self, prompt: Any, config: Any = None) -> Any:
        self.prompts.append(str(prompt))
        if not self.script:  # 脚本耗尽即失败（防被测代码静默多调）
            raise AssertionError("FakeChain 脚本耗尽：被测代码调用次数超出预期")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeModel:
    """模型假体：with_structured_output 返回注入的链（记录 schema 与 method）。"""

    def __init__(self, chain: _FakeChain) -> None:
        self._chain = chain
        self.alias_seen: str | None = None
        self.schema_seen: Any = None
        self.method_seen: str | None = None

    def with_structured_output(
        self, schema: Any, *, method: str | None = None, **kw: Any
    ) -> _FakeChain:
        self.schema_seen = schema
        self.method_seen = method
        return self._chain


def _install(
    monkeypatch: pytest.MonkeyPatch, chain: _FakeChain
) -> tuple[_FakeModel, list[tuple[str, str, dict[str, Any]]]]:
    """注入模型假体 + stage 发射记录器；返回 (假模型, stage 记录) 供断言。"""
    model = _FakeModel(chain)
    monkeypatch.setattr(deep_planner, "build_chat_model", lambda alias: model)
    emitted: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        deep_planner, "emit_stage", lambda key, status, **kw: emitted.append((key, status, kw))
    )
    return model, emitted


def _config(mapping: dict[str, Any] | None = None) -> dict[str, Any]:
    """TurnRunner 注入形态的 RunnableConfig（configurable 三键）。"""
    return {
        "configurable": {
            "thread_id": "th-1",
            "turn_id": "t-1",
            "model_mapping": mapping
            if mapping is not None
            else {"chat": {"options": {"qwen": "chat-standard"}, "default": "qwen"}},
        }
    }


# ---- 成功路径：透传 + max_subagents 夹取 + 事件 payload ----


async def test_planner_success_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """合法输出：subtopics/outline/query_type/time_range 四键产出；无 degraded/errors；
    outline 落成 [{section,intent}] 普通字典结构（writer 批次 4 消费形态）。"""
    chain = _FakeChain([dict(_OK_OUTPUT)])
    _install(monkeypatch, chain)

    update = await planner({**_STATE}, _config())

    assert update["query_type"] == "comparison"
    assert update["time_range"] is None
    assert update["subtopics"] == _OK_OUTPUT["subtopics"]
    assert update["outline"] == _OK_OUTPUT["outline"]
    assert all(isinstance(item, dict) for item in update["outline"])
    # 兜底不置 degraded（2026-11-13 B 裁决同款：成功路径更不置）
    assert "degraded" not in update
    assert "errors" not in update


async def test_planner_writes_only_deepstate_declared_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """state 写入与 DeepState 声明一致（§5.4 DoD：无未声明字段）。"""
    chain = _FakeChain([dict(_OK_OUTPUT)])
    _install(monkeypatch, chain)

    update = await planner({**_STATE}, _config())

    from app.agent.state import DeepState

    assert set(update) <= set(DeepState.__annotations__)


async def test_planner_clamps_subtopics_to_spec_max_subagents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """subtopics ≤ spec.deep.max_subagents（上限截断；读轮内 spec 快照——OQ-11 同源）。"""
    spec = {**_SPEC, "deep": {**_SPEC["deep"], "max_subagents": 2}}
    chain = _FakeChain([
        {**_OK_OUTPUT, "subtopics": ["子课题一", "子课题二", "子课题三", "子课题四", "子课题五"]}
    ])
    _install(monkeypatch, chain)

    update = await planner({**_STATE, "spec": spec}, _config())

    assert update["subtopics"] == ["子课题一", "子课题二"]


async def test_planner_dedupes_subtopics_before_clamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同义重复先去重再夹取（保序）——提示词拆分纪律「禁止同义重复子课题」的机制面。"""
    chain = _FakeChain([
        {**_OK_OUTPUT, "subtopics": ["pgvector 选型", "pgvector 选型", "Milvus 选型"]}
    ])
    _install(monkeypatch, chain)

    update = await planner({**_STATE}, _config())

    assert update["subtopics"] == ["pgvector 选型", "Milvus 选型"]


async def test_planner_stage_events_done_only_with_subtopics_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stage(plan, done, extra.subtopics=N)（api.md §7.4 钉死形态）；running 不双发
    （归 TurnRunner 图前发布——M2 批次 1 同 rewrite 先例）。"""
    chain = _FakeChain([dict(_OK_OUTPUT)])
    _, emitted = _install(monkeypatch, chain)

    await planner({**_STATE}, _config())

    assert [(key, status) for key, status, _ in emitted] == [("plan", "done")]
    assert emitted[-1][2] == {"extra": {"subtopics": 2}}


async def test_planner_memory_directive_extra_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """memory_directive（M5 通道）不写 state（DeepState 无该字段），仅经 done.extra 留档。"""
    chain = _FakeChain([{**_OK_OUTPUT, "memory_directive": "记住：偏好官方文档源"}])
    _, emitted = _install(monkeypatch, chain)

    update = await planner({**_STATE}, _config())

    assert "memory_directive" not in update
    assert emitted[-1][2] == {
        "extra": {"subtopics": 2, "memory_directive": "记住：偏好官方文档源"}
    }


async def test_planner_prompt_renders_query_history_and_type_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """渲染锁死：prompt 含 query/历史 Q:A（正序）/type_rules 内置全文/max_subagents 渲染值
    （ARCH §4.2 指代消解注入面；占位符契约集 M2 批次 2 增量）。"""
    chain = _FakeChain([dict(_OK_OUTPUT)])
    _install(monkeypatch, chain)

    await planner({**_STATE}, _config())

    prompt = chain.prompts[0]
    assert _STATE["query"] in prompt
    assert "历史：" in prompt and "问题：" in prompt
    assert "Q: 什么是向量数据库" in prompt and "A: 向量数据库是…" in prompt
    assert "参数可答判定" in prompt  # type_rules 内置全文注入
    assert "不超过3个子课题" in prompt  # max_subagents=3 渲染进拆分纪律句


async def test_planner_empty_history_and_missing_spec_renders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空历史/缺 spec（Studio 手填形态）→ 渲染不炸（变量以空串/内置默认提供）。"""
    chain = _FakeChain([dict(_OK_OUTPUT)])
    _install(monkeypatch, chain)

    state = {"query": "x", "query_lang": "zh"}  # 无 history / spec
    update = await planner(state, _config())  # type: ignore[arg-type]

    assert update["subtopics"] == _OK_OUTPUT["subtopics"]  # 缺省 max_subagents=3 不截断


async def test_planner_schema_method_is_function_calling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S15 基元：with_structured_output(PlannerOutput, method="function_calling")。"""
    chain = _FakeChain([dict(_OK_OUTPUT)])
    model, _ = _install(monkeypatch, chain)

    await planner({**_STATE}, _config())

    assert model.method_seen == "function_calling"
    assert model.schema_seen is deep_planner.PlannerOutput


async def test_planner_receives_chat_alias_from_mapping_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """强模型别名 = state.chat_model_key → 轮内映射快照 chat.options（OQ-11 同源），
    工厂按解析别名构建（app/agent/llm.py 共享工厂）。"""
    seen: dict[str, Any] = {}
    chain = _FakeChain([dict(_OK_OUTPUT)])

    def _spy(alias: str) -> _FakeModel:
        seen["alias"] = alias
        return _FakeModel(chain)

    monkeypatch.setattr(deep_planner, "build_chat_model", _spy)
    monkeypatch.setattr(deep_planner, "emit_stage", lambda key, status, **kw: None)

    await planner(
        {**_STATE, "chat_model_key": "glm"},
        _config({"chat": {"options": {"qwen": "chat-standard", "glm": "chat-standard-glm"}, "default": "qwen"}}),
    )

    assert seen["alias"] == "chat-standard-glm"


# ---- 取消感知（R14② 入口接线）----


async def test_planner_canceled_at_entry_raises_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """入口 pop 命中取消标志 → TurnCanceledSignal（取消非故障）；LLM 零调用。"""
    from app.agent.guards import TurnCanceledSignal

    chain = _FakeChain([dict(_OK_OUTPUT)])
    _install(monkeypatch, chain)
    flags = {"t-1": "superseded"}
    config = {"configurable": {"turn_id": "t-1", "cancel_flags": flags}}

    with pytest.raises(TurnCanceledSignal) as exc_info:
        await planner({**_STATE}, config)

    assert exc_info.value.reason == "superseded"
    assert flags == {}  # pop 即感知+清理
    assert chain.prompts == []  # 取消在 LLM 调用前感知


# ---- 校验失败：修复回喂 1 次 → 机制兜底单子课题 ----


async def test_validation_failure_repairs_once_then_mechanism_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """校验失败 → 错误文本回喂修复 1 次 → 仍败 → 机制兜底单子课题（factoid 化）：
    subtopics=[原始 query]、outline 单节、不置 degraded、errors[] 留痕、
    done.extra.fallback=mechanism。"""
    bad = {**_OK_OUTPUT, "query_type": "opinion"}  # 枚举外值（坏例警示禁止形态）
    chain = _FakeChain([bad, bad])
    _, emitted = _install(monkeypatch, chain)

    update = await planner({**_STATE}, _config())

    assert len(chain.prompts) == 2  # 初始调用 + 修复回喂恰好 1 次
    assert "校验" in chain.prompts[1] or "validation" in chain.prompts[1].lower()
    assert update["subtopics"] == [_STATE["query"]]  # 兜底单子课题
    assert update["outline"] == [
        {"section": _STATE["query"], "intent": f"直接回答：{_STATE['query']}"}
    ]
    assert update["query_type"] == "factoid"  # factoid 化（显式常量）
    assert update["time_range"] is None
    # 兜底不置 DeepState.degraded（不属 api.md §7.2 degraded_reason 枚举任一语义）
    assert "degraded" not in update
    assert update["errors"] == [{"stage": "plan", "reason": "structured_output_invalid"}]
    assert [(key, status) for key, status, _ in emitted] == [("plan", "done")]
    assert emitted[-1][2] == {"extra": {"subtopics": 1, "fallback": "mechanism"}}


async def test_validation_failure_repaired_successfully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """修复回喂成功路径：第二次调用合法 → 正常产出（夹取照走），零兜底痕迹。"""
    bad = {**_OK_OUTPUT, "subtopics": []}  # subtopics 空 → 校验失败
    fixed = {
        **_OK_OUTPUT,
        "subtopics": ["修复后的子课题一", "修复后的子课题二"],
    }
    chain = _FakeChain([bad, fixed])
    _install(monkeypatch, chain)

    update = await planner({**_STATE}, _config())

    assert update["subtopics"] == ["修复后的子课题一", "修复后的子课题二"]
    assert "errors" not in update
    assert "degraded" not in update


async def test_all_blank_subtopics_treated_as_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全空白 subtopics 视同结构化输出校验失败（同 fast_rewrite _extract 口径）→ 回喂。"""
    bad = {**_OK_OUTPUT, "subtopics": ["   ", "\t"]}
    chain = _FakeChain([bad, dict(_OK_OUTPUT)])
    _install(monkeypatch, chain)

    update = await planner({**_STATE}, _config())

    assert len(chain.prompts) == 2  # 初始 + 修复回喂
    assert update["subtopics"] == _OK_OUTPUT["subtopics"]  # 修复后正常产出
    assert "errors" not in update


async def test_repair_skipped_when_budget_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TurnDeadline 包络：修复回喂前查剩余预算——耗尽即跳过回喂直入兜底（单调用收口）。"""
    captured: list[float] = []

    class _ExhaustedDeadline:
        def __init__(self, budget_s: float) -> None:
            captured.append(budget_s)

        def remaining_s(self) -> float:
            return 0.0

    real_deadline = deep_planner.TurnDeadline
    monkeypatch.setattr(deep_planner, "TurnDeadline", _ExhaustedDeadline)
    chain = _FakeChain([{**_OK_OUTPUT, "query_type": "opinion"}, dict(_OK_OUTPUT)])
    _, emitted = _install(monkeypatch, chain)

    update = await planner({**_STATE}, _config())

    assert captured == [60.0]  # 包络 = spec.deep.llm_timeout_s（ask 钉死形态）
    assert len(chain.prompts) == 1  # 预算耗尽 → 修复回喂跳过（直入兜底）
    assert update["query_type"] == "factoid"
    assert emitted[-1][2]["extra"]["fallback"] == "mechanism"


async def test_deadline_default_is_60_when_spec_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """spec.deep.llm_timeout_s 缺失 → TurnDeadline 包络回落契约缺省 60s（§6.2）。"""
    captured: list[float] = []
    real_deadline = deep_planner.TurnDeadline

    def _factory(budget_s: float) -> Any:
        captured.append(budget_s)
        return real_deadline(budget_s)

    monkeypatch.setattr(deep_planner, "TurnDeadline", _factory)
    chain = _FakeChain([dict(_OK_OUTPUT)])
    _install(monkeypatch, chain)

    await planner({**_STATE, "spec": {"deep": {"max_subagents": 3}}}, _config())

    assert captured == [60.0]


# ---- 传输失败：直接机制兜底（planner 单调用面，无传输重试循环）----


async def test_transport_failure_falls_back_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """传输类失败（网络/超时）→ 不回喂、直接兜底单子课题（deep 轮整预算兜底归批次 3
    guards；节点面不终止）；extra.fallback=degraded + errors[] 留痕。"""
    request = httpx.Request("POST", "http://litellm/v1")
    chain = _FakeChain([httpx.ConnectError("refused", request=request)])
    _, emitted = _install(monkeypatch, chain)

    update = await planner({**_STATE}, _config())

    assert len(chain.prompts) == 1  # 无修复回喂（传输失败不属校验族）
    assert update["subtopics"] == [_STATE["query"]]
    assert update["query_type"] == "factoid"
    assert "degraded" not in update  # 兜底不置 degraded（同机制兜底口径）
    assert update["errors"] == [{"stage": "plan", "reason": "llm_call_failed"}]
    assert emitted[-1][2] == {"extra": {"subtopics": 1, "fallback": "degraded"}}


async def test_transport_timeout_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """单调用包络超时（asyncio.timeout → TimeoutError）→ 同传输兜底路径（不挂死节点）。"""
    import asyncio

    async def _hang(prompt: Any, config: Any = None) -> Any:
        await asyncio.sleep(10)

    class _HangingChain:
        async def ainvoke(self, prompt: Any, config: Any = None) -> Any:
            await _hang(prompt, config)

    model = _FakeModel(_HangingChain())
    monkeypatch.setattr(deep_planner, "build_chat_model", lambda alias: model)
    emitted: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        deep_planner, "emit_stage", lambda key, status, **kw: emitted.append((key, status, kw))
    )
    spec = {**_SPEC, "deep": {**_SPEC["deep"], "llm_timeout_s": 0.01}}

    update = await planner({**_STATE, "spec": spec}, _config())

    assert update["query_type"] == "factoid"
    assert update["errors"] == [{"stage": "plan", "reason": "llm_call_failed"}]
    assert emitted[-1][2]["extra"]["fallback"] == "degraded"


# ---- 兜底形态边界 ----


async def test_fallback_with_blank_query_still_terminates_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """query 为空（Studio 手填残缺形态）→ 兜底零子课题不炸（不伪造内容）。"""
    chain = _FakeChain([{**_OK_OUTPUT, "query_type": "opinion"}, {**_OK_OUTPUT, "query_type": "opinion"}])
    _install(monkeypatch, chain)

    update = await planner({**_STATE, "query": ""}, _config())

    assert update["subtopics"] == []
    assert update["outline"] == []
    assert update["query_type"] == "factoid"
    assert update["errors"] == [{"stage": "plan", "reason": "structured_output_invalid"}]


def test_fallback_constants_are_explicit() -> None:
    """机制兜底显式常量（ARCH §4.1 v1.4 澄清同款：兜底值显式、不经 spec 夹取）。"""
    assert deep_planner.FALLBACK_QUERY_TYPE == "factoid"
    assert deep_planner.DEFAULT_MAX_SUBAGENTS == 3


# ---- schema 形状（deep 零波及锁面）----


def test_planner_output_query_type_has_seven_values() -> None:
    """输出枚举 7 类形状锁死（nacos-config §1 deep.planner 行；无 navigational——
    判导航型输出 factoid，v1.27 钉死；other 恒兜底位）。"""
    import typing

    args = typing.get_args(deep_planner.PlannerOutput.model_fields["query_type"].annotation)
    assert args == (
        "factoid", "list", "comparison", "multi_facet", "temporal", "how_to", "other",
    )


def test_planner_output_rejects_navigational_and_illegal_values() -> None:
    """navigational 与枚举外值一律 ValidationError（坏例警示禁止形态；修复回喂承载）。"""
    from pydantic import ValidationError

    base = {"subtopics": ["s"], "outline": [{"section": "a", "intent": "b"}]}
    with pytest.raises(ValidationError):
        deep_planner.PlannerOutput.model_validate({**base, "query_type": "navigational"})
    with pytest.raises(ValidationError):
        deep_planner.PlannerOutput.model_validate({**base, "query_type": "opinion"})


def test_planner_output_has_no_fast_only_fields() -> None:
    """deep 零波及 schema 锁面（nacos §1 v1.20/v1.27 钉死）：不声明
    answerable_from_knowledge / search_strategy 字段（B13 增 `safety` 三值字段，
    缺省 none——与 fast 零波及口径不冲突：safety 为输入安全初判，双节点对称产出）。"""
    fields = set(deep_planner.PlannerOutput.model_fields)
    assert "answerable_from_knowledge" not in fields
    assert "search_strategy" not in fields
    assert fields == {
        "subtopics",
        "outline",
        "query_type",
        "safety",
        "time_range",
        "memory_directive",
    }


def test_planner_output_requires_outline_section_and_intent() -> None:
    """outline 每节必含 section 与 intent（ARCH §4.2 报告章节骨架契约）。"""
    from pydantic import ValidationError

    base = {"subtopics": ["s"], "query_type": "factoid"}
    with pytest.raises(ValidationError):
        deep_planner.PlannerOutput.model_validate({**base, "outline": [{"section": "a"}]})
    with pytest.raises(ValidationError):
        deep_planner.PlannerOutput.model_validate({**base, "outline": [{"intent": "b"}]})


def test_planner_output_time_range_enum() -> None:
    """time_range 枚举 ∪ {None}（透传子代理 web_search / Tavily time_range）。"""
    import typing

    annotation = deep_planner.PlannerOutput.model_fields["time_range"].annotation
    args = typing.get_args(annotation)
    assert set(typing.get_args(args[0])) == {"day", "week", "month", "year"}
    assert args[1] is type(None)


def test_extract_raises_validation_family_for_invalid() -> None:
    """_extract：裸 dict 非法形态 → 校验族异常（OutputParserException/ValidationError
    ——节点 except 按此族捕获进修复回喂；业务校验（全空白 subtopics）抛
    OutputParserException，pydantic 层非法枚举抛 ValidationError）。"""
    from pydantic import ValidationError

    bad_enum = {**_OK_OUTPUT, "query_type": "opinion"}
    with pytest.raises((OutputParserException, ValidationError)):
        deep_planner._extract(bad_enum)  # noqa: SLF001 —— 单测同构锁私有判定面
    bad_blank = {**_OK_OUTPUT, "subtopics": ["  ", "\t"]}
    assert isinstance(deep_planner._extract({**_OK_OUTPUT}), deep_planner.PlannerOutput)  # noqa: SLF001
    with pytest.raises(OutputParserException):
        deep_planner._extract(bad_blank)  # noqa: SLF001 —— 业务校验族


# ==== Layer 2 输入安全拦截（B13，fast_rewrite 对称同构）====
#
# 契约口径（逐条红锁，与 test_fast_rewrite 同款）：
# - `safety` 四值 none|danger|profanity|privacy，**缺省 none 灰度安全**——旧提示词（无本字段）
#   + 新代码 → 全量放行，存量轮零行为变化；
# - 仅成功路径（主调用/修复回喂）判读：初判 ∈ {danger, profanity, privacy} 先行短路（none
#   零判读——不触 load_safety_config，日志面零变化），再验 spec.safety enabled +
#   mode=hybrid；mode=lexicon / enabled=false 一律忽略 flag 照常返回；
# - 命中顺序：拒答话术（SafetyConfig + RunnableConfig 透传保观测）→
#   stage(plan, failed, detail="安全拦截", extra={safety, layer="planner"}) →
#   ContentRejectedError（不发 done——TurnRunner 捕获后走拒绝轮收束）；
# - 机制兜底/传输兜底路径不判 safety（Layer 1 pre_route 已前置覆盖）。

#: spec.safety 节（hybrid 形态，键位与 defaults.py SPEC_CONFIG safety 节同源）
_SAFETY_HYBRID: dict[str, Any] = {
    "enabled": True,
    "mode": "hybrid",
    "danger_terms": ["制造炸弹"],
    "profanity_terms": ["傻逼"],
    "grey_rules": [],
    "classify_timeout_ms": 2000,
    "reply_timeout_ms": 4000,
    "reply_max_chars": 500,
}

_SPEC_SAFETY: dict[str, Any] = {**_SPEC, "safety": _SAFETY_HYBRID}


def _install_rejection_stub(
    monkeypatch: pytest.MonkeyPatch, reply: str = "温和拒答话术。"
) -> list[dict[str, Any]]:
    """拒答话术生成打桩（零网络）；返回调用记录供透传断言。"""
    calls: list[dict[str, Any]] = []

    async def _stub(
        category: str,
        query_lang: str,
        config: Any,
        runnable_config: Any = None,
    ) -> str:
        calls.append(
            {
                "category": category,
                "query_lang": query_lang,
                "config": config,
                "runnable_config": runnable_config,
            }
        )
        return reply

    monkeypatch.setattr(deep_planner, "generate_rejection_reply", _stub)
    return calls


def _install_safety_loader_recorder(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """load_safety_config 调用记录器（透传真实实现）——锁「兜底路径零判读」。"""
    calls: list[Any] = []
    real = deep_planner.load_safety_config

    def _recorder(spec: Any) -> Any:
        calls.append(spec)
        return real(spec)

    monkeypatch.setattr(deep_planner, "load_safety_config", _recorder)
    return calls


def test_planner_output_safety_default_is_none() -> None:
    """schema 缺省值锁死：PlannerOutput().safety == "none"（B13 灰度安全底线）；
    privacy 为合法四值之一（v1.31），pii 仍非本通道枚举（归 sanitize 通道）。"""
    from pydantic import ValidationError

    parsed = deep_planner.PlannerOutput.model_validate(dict(_OK_OUTPUT))
    assert parsed.safety == "none"
    accepted = deep_planner.PlannerOutput.model_validate(
        {**_OK_OUTPUT, "safety": "privacy"}
    )
    assert accepted.safety == "privacy"
    with pytest.raises(ValidationError):
        deep_planner.PlannerOutput.model_validate({**_OK_OUTPUT, "safety": "pii"})


async def test_legacy_output_without_safety_field_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """灰度锁：旧提示词 fixture（无 safety 字段）+ safety 已启用的 hybrid spec →
    解析缺省 none 照常返回、done 形态零变化、零判读零拦截（存量轮零行为变化——
    先行短路使日志面亦零变化：不触 load_safety_config，缺节告警不刷正常轮）。"""
    chain = _FakeChain([dict(_OK_OUTPUT)])
    _, emitted = _install(monkeypatch, chain)
    rejection_calls = _install_rejection_stub(monkeypatch)
    loader_calls = _install_safety_loader_recorder(monkeypatch)

    update = await planner({**_STATE, "spec": _SPEC_SAFETY}, _config())

    assert update["query_type"] == "comparison"
    assert update["subtopics"] == _OK_OUTPUT["subtopics"]
    assert emitted[-1][2] == {"extra": {"subtopics": 2}}  # done 形态零变化
    assert rejection_calls == []
    assert loader_calls == []  # 缺省 none → 先行短路，零配置判读


@pytest.mark.parametrize("category", ["danger", "profanity", "privacy"])
async def test_safety_hit_raises_content_rejected_with_stage_failed(
    monkeypatch: pytest.MonkeyPatch, category: str
) -> None:
    """命中（danger/profanity/privacy）→ ContentRejectedError 三字段齐备 + stage(plan, failed,
    detail="安全拦截") 形态钉死 + done 不发 + 透传（SafetyConfig/query_lang/
    RunnableConfig）保观测。"""
    from app.agent.guards import ContentRejectedError
    from app.agent.safety.config import SafetyConfig

    chain = _FakeChain([{**_OK_OUTPUT, "safety": category}])
    _, emitted = _install(monkeypatch, chain)
    rejection_calls = _install_rejection_stub(monkeypatch)
    runnable = _config()

    with pytest.raises(ContentRejectedError) as exc_info:
        await planner({**_STATE, "spec": _SPEC_SAFETY}, runnable)

    exc = exc_info.value
    assert exc.category == category
    assert exc.reply == "温和拒答话术。"
    assert exc.layer == "planner"  # deep 侧层标注（fast 侧为 rewrite）
    assert ContentRejectedError.code == "403002"
    # stage 形态：恰一条 failed（done 不发），detail/extra 钉死
    assert [(key, status) for key, status, _ in emitted] == [("plan", "failed")]
    assert emitted[-1][2] == {
        "detail": "安全拦截",
        "extra": {"safety": category, "layer": "planner"},
    }
    # 透传面：类别、语言（state 注入值）、SafetyConfig 实例、RunnableConfig 原样
    assert len(rejection_calls) == 1
    call = rejection_calls[0]
    assert call["category"] == category
    assert call["query_lang"] == "zh"
    assert isinstance(call["config"], SafetyConfig)
    assert call["config"].mode == "hybrid"
    assert call["runnable_config"] is runnable


async def test_safety_lexicon_mode_ignores_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mode=lexicon → 忽略 flag 照常返回（词表模式不消费模型初判，Layer 1 为准）。"""
    spec = {**_SPEC, "safety": {**_SAFETY_HYBRID, "mode": "lexicon"}}
    chain = _FakeChain([{**_OK_OUTPUT, "safety": "danger"}])
    _, emitted = _install(monkeypatch, chain)
    rejection_calls = _install_rejection_stub(monkeypatch)

    update = await planner({**_STATE, "spec": spec}, _config())

    assert update["query_type"] == "comparison"  # 正常产出
    assert update["subtopics"] == _OK_OUTPUT["subtopics"]
    assert [(key, status) for key, status, _ in emitted] == [("plan", "done")]
    assert rejection_calls == []


async def test_safety_disabled_ignores_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """enabled=false（回退门）→ 忽略 flag 照常返回（load_safety_config None 直通）。"""
    spec = {**_SPEC, "safety": {**_SAFETY_HYBRID, "enabled": False}}
    chain = _FakeChain([{**_OK_OUTPUT, "safety": "danger"}])
    _, emitted = _install(monkeypatch, chain)
    rejection_calls = _install_rejection_stub(monkeypatch)

    update = await planner({**_STATE, "spec": spec}, _config())

    assert update["query_type"] == "comparison"
    assert [(key, status) for key, status, _ in emitted] == [("plan", "done")]
    assert rejection_calls == []


async def test_safety_hit_on_repair_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """修复回喂成功路径命中同样触发（主调用校验失败 → 回喂产出带 flag → 拦截）。"""
    from app.agent.guards import ContentRejectedError

    bad = {**_OK_OUTPUT, "query_type": "opinion"}  # 枚举外值 → 进修复回喂
    chain = _FakeChain([bad, {**_OK_OUTPUT, "safety": "profanity"}])
    _, emitted = _install(monkeypatch, chain)
    rejection_calls = _install_rejection_stub(monkeypatch)

    with pytest.raises(ContentRejectedError) as exc_info:
        await planner({**_STATE, "spec": _SPEC_SAFETY}, _config())

    assert exc_info.value.layer == "planner"
    assert exc_info.value.category == "profanity"
    assert len(chain.prompts) == 2  # 主调用 + 修复回喂，拦截发生在回喂产出后
    assert [(key, status) for key, status, _ in emitted] == [("plan", "failed")]
    assert len(rejection_calls) == 1


async def test_mechanism_fallback_does_not_check_safety(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """机制兜底路径零判读（Layer 1 pre_route 已前置覆盖）——load_safety_config
    一次都不进、done 仍按兜底形态发。"""
    chain = _FakeChain([
        {**_OK_OUTPUT, "query_type": "opinion"},
        {**_OK_OUTPUT, "query_type": "opinion"},
    ])
    _, emitted = _install(monkeypatch, chain)
    rejection_calls = _install_rejection_stub(monkeypatch)
    loader_calls = _install_safety_loader_recorder(monkeypatch)

    update = await planner({**_STATE, "spec": _SPEC_SAFETY}, _config())

    assert update["errors"] == [{"stage": "plan", "reason": "structured_output_invalid"}]
    assert emitted[-1][2] == {"extra": {"subtopics": 1, "fallback": "mechanism"}}
    assert loader_calls == []  # 兜底路径零判读
    assert rejection_calls == []


async def test_transport_fallback_does_not_check_safety(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """传输兜底路径零判读（Layer 1 已前置覆盖）——直接机制兜底不拦截。"""
    request = httpx.Request("POST", "http://litellm/v1")
    chain = _FakeChain([httpx.ConnectError("refused", request=request)])
    _, emitted = _install(monkeypatch, chain)
    rejection_calls = _install_rejection_stub(monkeypatch)
    loader_calls = _install_safety_loader_recorder(monkeypatch)

    update = await planner({**_STATE, "spec": _SPEC_SAFETY}, _config())

    assert update["errors"] == [{"stage": "plan", "reason": "llm_call_failed"}]
    assert emitted[-1][2] == {"extra": {"subtopics": 1, "fallback": "degraded"}}
    assert loader_calls == []
    assert rejection_calls == []
