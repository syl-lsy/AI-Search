"""unit 层：degraded_writer 节点（M2 批次 4——ARCH §4.2 degraded_writer 行 / DEV §5.4
五步闭环）。

钉住的语义（冻结契约 + 盘点钉点，逐条对应测试）：
- **条件边隔离（ARCH §4.2:475-480）**：guards 条件边两出口——degraded_reason 已置
  （deep 图内结构性仅 timeout|budget 两值：guards 置位 + reflect._pre_trip 同源）→
  degraded_writer；None → writer。正常轮结构性不可达 degraded_writer；取消不经路由
  （guards 入口 ensure_not_canceled 先于路由抛 TurnCanceledSignal——ADR-010 取消不进
  降级：done(finish=canceled) 零 error 保留已产出，不调用 degraded_writer）；
- **降级路径直通 END（ARCH §4.2:480 DG --> E）**：不进 finalize——引用越界剔除由本
  节点内联承载（_normalize_citations 复用 finalize 单一事实源，有效性=账本 ref_no
  集合——与 finalize 同语义）；write done 帧由本节点自发（裸帧零 extra——降级报告
  非分节正文，批次 3 reflect done 裸帧先例）；
- **一次 LLM 调用 + 降级包络（ARCH §4.2:514「超时包络 30s」）**：ainvoke 单调用
  （非逐字流——盘点 decisions[4]，不扩 _messages_gate），asyncio.timeout 包络 =
  **min(30, max(remaining−15, 5))**（guards trip 判据=remaining ≤ 30+15 使本节点总有
  窗口；包络随剩余收缩并有 5s 下限兜底，非恒等 30s）；**thinking
  off**（包络约束下最大化产出概率——一次性压缩产出不需要思维链，偏离 deep 档
  on 缺省属包络驱动决策，盘点钉）；调用失败/超时/空输出 → 纯机械来源速览兜底
  （零 LLM——[n]=ref_no ≤5 条 + fast digest 同款转义纪律）+ errors[] 留痕，不终止；
- **「研究未完成」显著标注（REQ-014/A2；ARCH :514）**：机械前置说明行（代码常量
  ——fast digest _DIGEST_PREAMBLE 先例，零 LLM 不可被模型吞掉）+ 提示词 reason_note
  注入（模型如实呈现缺口）；前端显著标注另由 turns.status=degraded 承载（api.md
  §2.5:262 DegradedBanner——本标注为内容内 belt-and-suspenders）；
- **已收集内容降级报告**：材料面= subagent_reports + 来源元数据（fuse_top_n 截断）
  ——复用 writer 装配器单一事实源；不读 outline（降级报告按材料组织非按大纲成文）；
- **事件面（盘点 decisions[4]）**：stage(write,running,label=撰写降级报告) →
  emit_fallback_token 整块单事件（publish-only 不落库——turn_service.py:2301-2307
  既有翻译面零改动，fast digest v1.59 先例）→ stage(write,done) 裸帧；
- **degraded_reason 贯通（api.md §7.2:802-803/:845）**：本节点零写 degraded_reason/
  degraded（D2——降级单一事实源=guards 置位；TurnRunner _finish_completed 分流
  done(finish=degraded, degraded_reason=同值) 零 error——turn_service.py:1441-1454
  批次 1 已接，真实图贯通归本批 TurnRunner 级测试锁死）；
- usage_total 终值回填（注入面可选——finalize 同款；本路径无 finalize，guards 快照
  之后由本节点补终值）；update 键 ⊆ DeepState.__annotations__（R14①）；
- 提示词走 Nacos 键 deep.degraded_writer（通用纪律「提示词走 Nacos 键位 {graph}.{node}」
  ——六占位符 {reason_note, query_lang, cite_rules, memory_block, subagent_reports_block,
  sources_block}；契约增量行随本批落 nacos-config.md §1，版本 bump 归收官环节）。
"""

import asyncio
from typing import Any

import pytest

from app.agent.guards import TurnCanceledSignal
from app.agent.nodes import deep_degraded_writer
from app.agent.nodes.deep_degraded_writer import degraded_writer
from app.agent.nodes.deep_guards import route_guards


_OUTLINE: list[dict[str, str]] = [
    {"section": "背景", "intent": "问题背景"},
    {"section": "分析", "intent": "证据综合"},
]
_REPORTS: list[dict[str, Any]] = [
    {"subtopic": "pgvector 能力", "summary": "pgvector 适合轻量场景。", "failed": False},
    {"subtopic": "Milvus 能力", "summary": "", "failed": True},
]


def _sources(count: int) -> list[dict[str, Any]]:
    return [
        {"url": f"https://s.example/{i}", "title": f"来源{i}", "snippet": f"片段{i}", "ref_no": i}
        for i in range(1, count + 1)
    ]


_STATE: dict[str, Any] = {
    "query": "pgvector 和 Milvus 怎么选？",
    "query_lang": "zh",
    "spec": {"deep": {"fuse_top_n": 10}},
    "chat_model_key": "qwen",
    "outline": list(_OUTLINE),
    "subagent_reports": list(_REPORTS),
    "collected_sources": _sources(2),
    "degraded_reason": "budget",
}


class _FakeMessage:
    """AIMessage 假体（ainvoke 返回形态——_text_of 按 .content 读）。"""

    def __init__(self, content: Any) -> None:
        self.content = content


class _FakeInvokeModel:
    """chat 模型假体：ainvoke 按脚本产出消息/异常；记录 prompt/config/kwargs。"""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.prompts: list[Any] = []
        self.kwargs: list[dict[str, Any]] = []

    async def ainvoke(self, prompt: Any, config: Any = None, **kw: Any) -> Any:
        self.prompts.append(prompt)
        self.kwargs.append(dict(kw))
        if not self.script:
            raise AssertionError("FakeInvokeModel 脚本耗尽：ainvoke 调用次数超出预期")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple):  # (text, pre_sleep)——包络超时注入形态
            await asyncio.sleep(item[1])
            return _FakeMessage(item[0])
        return _FakeMessage(item)


def _install(
    monkeypatch: pytest.MonkeyPatch,
    script: list[Any],
) -> tuple[_FakeInvokeModel, list[tuple[str, str, dict[str, Any]]], list[str]]:
    """注入模型假体 + 信号记录器；返回 (模型, stage 帧, fallback_token 记录)。"""
    model = _FakeInvokeModel(script)
    monkeypatch.setattr(deep_degraded_writer, "build_chat_model", lambda alias, **kw: model)
    frames: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        deep_degraded_writer,
        "emit_stage",
        lambda key, status, **kw: frames.append((key, status, kw)),
    )
    tokens: list[str] = []
    monkeypatch.setattr(
        deep_degraded_writer, "emit_fallback_token", lambda delta: tokens.append(delta)
    )
    return model, frames, tokens


def _state_with(**overrides: Any) -> dict[str, Any]:
    state = {**_STATE}
    state.update(overrides)
    return state


def _config(**extra: Any) -> dict[str, Any]:
    configurable: dict[str, Any] = {"thread_id": "th-1", "turn_id": "t-1"}
    configurable.update(extra)
    return {"configurable": configurable}


# ==== 条件边（ARCH §4.2:475-480——guards 两出口）====


def test_route_guards_trips_to_degraded_writer() -> None:
    """degraded_reason 已置（timeout/budget——deep 图内结构性两值）→ degraded_writer。"""
    assert route_guards({"degraded_reason": "budget"}) == "degraded_writer"
    assert route_guards({"degraded_reason": "timeout"}) == "degraded_writer"


def test_route_guards_none_to_writer() -> None:
    """未 trip（degraded_reason=None）→ writer（正常轮结构性不可达 degraded_writer）。"""
    assert route_guards({}) == "writer"
    assert route_guards({"degraded_reason": None}) == "writer"


def test_deep_graph_topology_guards_conditional_two_targets() -> None:
    """deep 图拓扑：guards 条件边两出口（writer/degraded_writer）+ degraded_writer
    直通 END（ARCH §4.2:480 DG --> E——降级路径不进 finalize）；无 guards→writer 直边。"""
    from app.agent.graphs.deep import graph as deep_graph

    nodes = set(deep_graph.get_graph().nodes)
    assert "degraded_writer" in nodes
    edges = {(e.source, e.target, e.conditional) for e in deep_graph.get_graph().edges}
    assert ("guards", "writer", True) in edges
    assert ("guards", "degraded_writer", True) in edges
    assert ("degraded_writer", "__end__", False) in edges
    assert ("guards", "writer", False) not in edges  # 直边已被条件边替换


# ==== 降级报告：一次 LLM 调用 + 显著标注 + 事件面 ====


async def test_degraded_report_preamble_and_llm_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单次 ainvoke 调用（非逐字流——盘点 decisions[4]）：answer=机械前置标注行
    （⚠️ 研究未完成——代码常量零 LLM 不可吞）+ 模型正文；emit_fallback_token 整块
    单事件恰好一次；write 帧=running(label=撰写降级报告)+done 裸帧（零 extra）。"""
    model, frames, tokens = _install(monkeypatch, ["已收集发现：pgvector 轻量。"])

    update = await degraded_writer(_state_with(), _config())

    assert model.prompts and len(model.prompts) == 1  # 一次调用
    assert update["answer_md"].startswith("⚠️ 研究未完成")
    assert "已收集发现：pgvector 轻量。" in update["answer_md"]
    assert update["report_md"] == update["answer_md"]
    assert tokens == [update["answer_md"]]  # 整块单事件（fast digest v1.59 先例）
    assert [(key, status) for key, status, _ in frames] == [("write", "running"), ("write", "done")]
    assert frames[0][2]["label"] == "撰写降级报告"
    assert frames[1][2] == {}  # done 裸帧（降级报告非分节正文——零 extra）
    assert "errors" not in update  # 调用成功零兜底痕迹
    assert "degraded" not in update and "degraded_reason" not in update  # D2


async def test_preamble_reason_labels_bilingual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """前置标注按 (query_lang, degraded_reason) 双语四态：timeout/budget × zh/en；
    未知 reason 防御回落「研究中断」。"""
    _install(monkeypatch, ["正文"])

    update = await degraded_writer(
        _state_with(degraded_reason="timeout"), _config()
    )
    assert "轮次超时" in update["answer_md"]

    update = await degraded_writer(
        _state_with(degraded_reason="budget", query_lang="en"), _config()
    )
    assert "Research incomplete" in update["answer_md"] and "budget" in update["answer_md"]

    update = await degraded_writer(
        _state_with(degraded_reason="timeout", query_lang="en"), _config()
    )
    assert "Research incomplete" in update["answer_md"] and "time limit" in update["answer_md"]

    update = await degraded_writer(
        _state_with(degraded_reason="source_failure"), _config()  # 防御形态
    )
    assert "研究中断" in update["answer_md"]


async def test_prompt_via_nacos_key_with_reason_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """deep.degraded_writer 键消费：六占位符全渲染（零 {{ 残留）；reason_note 注入
    中断原因；cite_rules=common.citation_rules；材料面复用 writer 装配器。"""
    model, _, _ = _install(monkeypatch, ["正文"])

    await degraded_writer(_state_with(), _config())

    prompt = model.prompts[0]
    assert "{{" not in prompt and "}}" not in prompt
    assert "轮次超时" not in prompt or "预算" in prompt or "中断" in prompt  # reason_note 形态
    assert "1. 编号对应：[n] 的 n 对应来源列表第 n 条" in prompt  # common.citation_rules
    assert "pgvector 适合轻量场景" in prompt  # subagent_reports_block
    assert "[1] 来源1" in prompt  # sources_block


async def test_sources_block_truncated_fuse_top_n(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """材料面 fuse_top_n 截断（与 writer 同款——spec.deep.fuse_top_n 缺省 10）。"""
    model, _, _ = _install(monkeypatch, ["正文"])

    await degraded_writer(_state_with(collected_sources=_sources(12)), _config())

    prompt = model.prompts[0]
    assert "[10] 来源10" in prompt and "[11]" not in prompt and "[12]" not in prompt


async def test_thinking_off_for_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """thinking off（30s 包络约束——一次性压缩产出不需要思维链；偏离 deep 档 on
    缺省属包络驱动决策）。"""
    captured: list[dict[str, Any]] = []
    model = _FakeInvokeModel([["正文"]])

    def _factory(alias: str, **kw: Any) -> Any:
        captured.append(dict(kw))
        return model

    monkeypatch.setattr(deep_degraded_writer, "build_chat_model", _factory)
    frames: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        deep_degraded_writer, "emit_stage", lambda key, status, **kw: frames.append((key, status, kw))
    )
    monkeypatch.setattr(deep_degraded_writer, "emit_fallback_token", lambda delta: None)

    await degraded_writer(_state_with(), _config())

    assert captured and captured[0].get("thinking") == "off"


async def test_envelope_clamped_to_remaining_minus_margin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批次 4 评审 P1——包络=min(ENVELOPE, remaining−MARGIN)（防御下限 5s）：
    注入 turn_deadline remaining=20s → 包络=max(20−15, 5)=5s；慢 LLM（sleep 100s）
    在包络内被截→机械速览兜底，耗时≈5s 而非 30s——外层轮级超时不再截杀降级段
    （api.md §7.2:845「图内熔断零 error」形态：慢调用撞满也由节点内包络先收口）。
    无注入面 → 固定 30s（Studio 直跑/单测形态不变）。"""
    import time

    model, frames, tokens = _install(monkeypatch, [("正文", 100.0)])  # 慢 LLM

    started = time.monotonic()
    update = await degraded_writer(
        _state_with(),
        _config(turn_deadline=type("_Remain20", (), {"remaining_s": lambda self: 20.0})()),
    )
    elapsed = time.monotonic() - started

    assert elapsed < 15  # 包络 5s 收口（固定 30s 包络下该断言红）
    assert update["answer_md"]  # 机械速览兜底产出（慢调用被包络截断）
    assert update["errors"] == [{"stage": "write", "reason": "degraded_writer_fallback"}]
    assert [(key, status) for key, status, _ in frames] == [
        ("write", "running"),
        ("write", "done"),
    ]


# ==== 30s 包络 + 兜底（ARCH §4.2:514「超时包络 30s」）====


async def test_envelope_timeout_falls_back_to_mechanical_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单调用包络超时（DEGRADED_WRITER_ENVELOPE_S）→ 纯机械来源速览兜底（零 LLM）：
    前置标注 + [n]=ref_no 来源行；errors[] 留痕不终止；fallback_token 照发
    （降级报告恒有产出——兜底不能无产出）。"""
    monkeypatch.setattr(deep_degraded_writer, "DEGRADED_WRITER_ENVELOPE_S", 0.05)
    _, frames, tokens = _install(monkeypatch, [("慢正文", 0.5)])

    update = await degraded_writer(_state_with(), _config())

    assert update["answer_md"].startswith("⚠️ 研究未完成")
    assert "[1] 来源1" in update["answer_md"]  # 机械速览（[n]=ref_no 账本）
    assert update["errors"] == [{"stage": "write", "reason": "degraded_writer_fallback"}]
    assert tokens == [update["answer_md"]]
    assert [(key, status) for key, status, _ in frames] == [("write", "running"), ("write", "done")]


async def test_llm_failure_falls_back_to_mechanical_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """调用失败（传输/网关异常）→ 机械速览兜底（不终止——降级路径恒有产出）。"""
    _, _, tokens = _install(monkeypatch, [RuntimeError("网关断连")])

    update = await degraded_writer(_state_with(), _config())

    assert update["answer_md"].startswith("⚠️ 研究未完成")
    assert "[2] 来源2" in update["answer_md"]
    assert update["errors"] == [{"stage": "write", "reason": "degraded_writer_fallback"}]
    assert tokens == [update["answer_md"]]


async def test_digest_fallback_caps_five_and_escapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """机械速览：≤5 条来源 + markdown 控制字符转义 + 半角 [n] 全角化（fast digest
    同款纪律——[n] 伪造防御）。"""
    _install(monkeypatch, [RuntimeError("fail")])
    hostile = [
        {"url": f"https://s.example/{i}", "title": f"标*题**{i}[9]", "snippet": f"片段{i}", "ref_no": i}
        for i in range(1, 8)
    ]

    update = await degraded_writer(_state_with(collected_sources=hostile), _config())

    digest = update["answer_md"]
    assert len([line for line in digest.splitlines() if line.startswith("- [")]) == 5
    assert "标\\*题" in digest and "［9］" in digest


# ==== 引用核对（degraded 路径不进 finalize——本节点内联承载，ARCH §4.2:480）====


async def test_in_node_citation_strip_full_ledger_validity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内联越界剔除（复用 finalize _normalize_citations 单一事实源）：正文 [99] 不在
    账本 → 剔除 + errors[] 留痕；合法 [1]/[2] 保留（有效性=账本 ref_no 集合——与
    finalize 同语义）。"""
    _install(monkeypatch, ["甲[1]乙[99]丙[2]。"])

    update = await degraded_writer(_state_with(), _config())

    assert "甲[1]乙丙[2]。" in update["answer_md"]
    assert "[99]" not in update["answer_md"]
    assert {"stage": "write", "reason": "citation_out_of_range", "dropped": 1} in update["errors"]


# ==== usage_total 终值回填 + 家族纪律 ====


async def test_usage_total_backfill_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """usage_total 终值回填（注入面可选——finalize 同款；本路径无 finalize，guards
    快照之后由本节点补终值）。"""

    class _FakeCollector:
        def total(self) -> dict[str, int]:
            return {"total_tokens": 23456}

    _install(monkeypatch, ["正文"])

    update = await degraded_writer(_state_with(), _config(usage_collector=_FakeCollector()))
    assert update["usage_total"] == 23456

    update = await degraded_writer(_state_with(), _config())
    assert "usage_total" not in update


async def test_canceled_at_entry_raises_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """入口取消感知（R14② 家族先例）——取消不进降级（ADR-010：命中即抛穿透，
    TurnRunner _finish_canceled 收尾零 error 零降级报告）。"""
    _, frames, tokens = _install(monkeypatch, [["正文"]])
    config = _config(cancel_flags={"t-1": "user"})

    with pytest.raises(TurnCanceledSignal) as exc_info:
        await degraded_writer(_state_with(), config)

    assert exc_info.value.reason == "user"
    assert frames == [] and tokens == []


async def test_writes_only_deepstate_declared_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """update 键 ⊆ DeepState.__annotations__（R14①）。"""
    _install(monkeypatch, ["正文"])

    update = await degraded_writer(_state_with(), _config())

    from app.agent.state import DeepState

    assert set(update) <= set(DeepState.__annotations__)


# ==== TurnRunner 级贯通（api.md §7.2:802-803/:845——真实图 + 真调度器）====


async def test_turnrunner_degraded_done_via_real_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """guards trip → 条件边 → degraded_writer → TurnRunner 分流 done(finish=degraded,
    degraded_reason=timeout) 零 error——api.md §7.2:803 枚举 + :845 v1.21 注（图内
    熔断零 error 走 done(finish=degraded)）真实图贯通；取消不进降级的反向面由
    guards 入口 ensure_not_canceled 承载（unit test_canceled_at_entry + guards
    integration 取消中途注入面已锁）。

    构造：spec.deep.turn_timeout_s=30 → TurnDeadline remaining ≤ 30s 包络 →
    reflect._pre_trip 即刻 trip timeout（批次 3 双检查点语义）；正常轮不可达面
    （writer 被条件边隔离）由 writer 假体「被调用即炸」锁死。"""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from app.agent.graphs.main import graph as main_graph
    from app.agent.nodes import deep_degraded_writer as degraded_module
    from app.agent.nodes import deep_planner as deep_planner_module
    from app.agent.nodes import deep_reflect as deep_reflect_module
    from app.agent.nodes import deep_research as deep_research_module
    from app.agent.nodes import deep_writer as deep_writer_module
    from test.unit.turn_fakes import (
        _MAPPING_PAYLOAD,
        _R14_SPEC,
        _R14FakeRegistry,
        _FakeSession,
        _FakeSessionFactory,
        _TurnRow,
        _bus,
        _drain_all,
        _snapshot,
    )
    from app.services.turn_service import ThreadScheduler

    class _PlanChain:
        async def ainvoke(self, prompt: Any, config: Any = None) -> dict[str, Any]:
            return {
                "subtopics": ["dgw 子课题一"],
                "outline": [{"section": "背景", "intent": "背景"}],
                "query_type": "multi_facet",
                "time_range": None,
                "memory_directive": None,
            }

    class _PlannerModel:
        def with_structured_output(
            self, schema: Any, *, method: str | None = None, **kw: Any
        ) -> _PlanChain:
            return _PlanChain()

    class _ReflectModel:
        def with_structured_output(self, schema: Any, **kw: Any) -> Any:
            raise AssertionError("reflect 应在判定前预检 trip（零 LLM 调用收敛）")

    class _ResearchMainModel(GenericFakeChatModel):
        def bind_tools(self, tools: Any, **kwargs: Any) -> "_ResearchMainModel":  # noqa: ANN201
            return self

    class _WriterMustNotRun:
        """正常轮 writer 假体：degraded 路径被条件边隔离——被调用即炸（结构性隔离锁）。"""

        async def astream(self, prompt: Any, config: Any = None, **kw: Any) -> Any:
            raise AssertionError("writer 不应在 guards trip 路径被调用（条件边隔离失效）")
            yield None  # pragma: no cover —— 使其成为 async generator 形态（不可达）

    class _DegradedModel:
        async def ainvoke(self, prompt: Any, config: Any = None, **kw: Any) -> Any:
            return _FakeMessage("阶段性发现：pgvector 轻量可用。")

    spec = {**_R14_SPEC, "deep": {**_R14_SPEC["deep"], "turn_timeout_s": 30}}
    monkeypatch.setattr(deep_planner_module, "build_chat_model", lambda alias, **kw: _PlannerModel())
    monkeypatch.setattr(deep_reflect_module, "build_chat_model", lambda alias, **kw: _ReflectModel())
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
    monkeypatch.setattr(
        deep_writer_module, "build_chat_model", lambda alias, **kw: _WriterMustNotRun()
    )
    monkeypatch.setattr(degraded_module, "build_chat_model", lambda alias, **kw: _DegradedModel())

    bus = _bus()
    turn = _TurnRow(status="running", mode="deep")
    session = _FakeSession(turn=turn)
    scheduler = ThreadScheduler(
        bus=bus,
        session_factory=_FakeSessionFactory(session),
        graph=main_graph,
        spec_snapshot=_snapshot(spec),
        mapping_snapshot=_snapshot(_MAPPING_PAYLOAD),
        tools_registry=_R14FakeRegistry(),
    )
    await scheduler._execute_turn(turn)

    published = await _drain_all(bus, str(turn.id))
    assert [e.event_type for e in published if e.event_type == "error"] == []  # 零 error（:845）
    done = published[-1]
    assert done.event_type == "done"
    assert done.data["finish"] == "degraded"
    assert done.data["degraded"] is True
    assert done.data["degraded_reason"] == "timeout"
    assert turn.status == "degraded"  # turns.status=degraded（DegradedBanner 数据源）
    assert turn.degraded_reason == "timeout"
    assert turn.answer_md and turn.answer_md.startswith("⚠️ 研究未完成")
    assert "阶段性发现" in turn.answer_md
    stages = [e.data.get("stage_key") for e in published if e.event_type == "stage"]
    assert "plan" in stages and "write" in stages
