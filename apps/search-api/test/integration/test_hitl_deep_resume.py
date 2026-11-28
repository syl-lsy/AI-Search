"""integration 层：TurnRunner deep resume 适配收口（B22 批次 4 第三段，真库
requires_db）。

契约锚点（见各用例断言面）：
- 600s 独立包络重建（nacos §3 deep.turn_timeout_s=600 v1.40 + v1.50 分段注；R31）：
  阶段 1/阶段 2 各自完整包络——TurnDeadline 每阶段新建（awaiting 不占协程不占轮
  预算），捕获面=TurnDeadline 构造序 [600, 600]；
- 超时 hybrid / 取消 / 多标签页三路径对齐批次 2 语义（模式无关消费面）：
  collect_expired_approvals（choice=both/decided_by=timeout → resume both 形态）、
  §2.7 awaiting 取消（直接终态 canceled(user)+审批行 canceled+两帧）、R37 竞态
  （条件 UPDATE 先到先得 + 幂等 200 + 异 choice 409008 + 重复 resume 零副作用）；
- 工具桩化注入面贯通（第二段 R36 的端到端证明）：resume 注入 approval_choice →
  deep 图 START 条件入口 → research 节点 state → resolve_tool_scope → 真契约工具
  桩化生效（kb_only：web_search 桩注/kb_search live；零命中：kb_search 桩 plan
  逐字——research 桩内调真工具，零物理外呼面：web 侧仅桩态调用、kb 侧假体）。

图形态：真 deep 拓扑 + LLM 节点桩（同 test_hitl_deep_two_phase 姿势）；工具桩化
用例的 research 为「真工具探针」桩（构造真 _Deps + _build_contract_tools 后调用）。
复用 test_hitl_deep_two_phase 的播种/断言 helper（同包跨模块导入）。
"""

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import app.services.turn_service as ts_mod
from app.agent.graphs import deep as deep_mod
from app.agent.graphs.deep import build_deep_graph
from app.agent.nodes import deep_research
from app.config_center.defaults import BUILTIN_DEFAULTS
from app.db.session import get_session_factory
from app.exception.conflict import ApprovalConflictException
from test.integration import test_hitl_deep_two_phase as tp

pytestmark = pytest.mark.requires_db

_USER_ID = tp._USER_ID
_SPEC_DATA_ID = tp._SPEC_DATA_ID


# ---- 工具探针桩（真契约工具调用——R36 注入面贯通证明）----


class _ProbeRecorder(tp._Recorder):
    """探针记录器：捕获 research 内解析的作用域与真工具返回。"""

    def __init__(self) -> None:
        super().__init__()
        self.captured: dict[str, Any] = {}


def _build_probe_graph(recorder: _ProbeRecorder, subtopics: list[str]):
    """真 deep 拓扑 + LLM 节点桩；research=真工具探针（state 作用域 → 真
    _Deps/_build_contract_tools → 调用捕获）。物理安全面：web_search 仅在
    kb_only（桩态）下调用；kb_search 假体 retriever（零外呼）。"""

    from app.agent.graphs.signals import emit_stage
    from langchain_core.runnables import RunnableConfig

    async def _stub_planner(state: Any, config: Any = None) -> dict[str, Any]:
        recorder.planner_calls += 1
        emit_stage("plan", "done", extra={"subtopics": len(subtopics)})
        return {
            "subtopics": list(subtopics),
            "outline": copy.deepcopy(tp._OUTLINE),
            "query_type": "factoid",
            "time_range": None,
        }

    async def _probe_research(state: Any, config: RunnableConfig | None = None) -> dict[str, Any]:
        # 注解=RunnableConfig：langgraph 按签名注解决定 config 注入（Any 不注入——
        # 本探针需 configurable.kb_retriever 真注入面）
        recorder.research_calls += 1
        recorder.research_subtopics.append(list(state.get("subtopics") or []))
        scope = deep_research.resolve_tool_scope(state)
        recorder.captured["scope"] = scope
        recorder.captured["approval_choice"] = state.get("approval_choice")
        cfg = (config or {}).get("configurable") or {}
        deps = deep_research._Deps(
            registry=cfg.get("tools_registry"),
            search_chain=cfg.get("search_chain"),
            read_chain=cfg.get("read_chain"),
            spec=state.get("spec") or {},
            recorder=deep_research._Recorder(list(state.get("subtopics") or [])),
            user_id=str(state.get("user_id") or ""),
            thread_id=str(state.get("thread_id") or ""),
            turn_id=str(state.get("turn_id") or ""),
            kb_retriever=cfg.get("kb_retriever"),
            tool_scope=scope,
        )
        tools = {t.name: t for t in deep_research._build_contract_tools(deps)}
        if scope == "kb_only":
            # 物理安全面：web_search 仅桩态调用（stub 短路零外呼）；live 形态不探
            recorder.captured["web"] = await tools["web_search"].ainvoke({"query": "探针检索"})
        recorder.captured["kb"] = await tools["kb_search"].ainvoke({"query": "探针检索"})
        return {
            "subagent_reports": [],
            "visited_urls": [],
            "searched_queries": [],
            "collected_sources": [],
            "snapshots": [],
            "errors": [],
        }

    async def _stub_reflect(state: Any, config: Any = None) -> dict[str, Any]:
        return {"gaps": [], "reflect_round": 1}

    async def _stub_guards(state: Any, config: Any = None) -> dict[str, Any]:
        return {}

    async def _stub_writer(state: Any, config: Any = None) -> dict[str, Any]:
        recorder.writer_outline.append(list(state.get("outline") or []))
        return {"report_md": "# 深度报告"}

    async def _stub_finalize(state: Any, config: Any = None) -> dict[str, Any]:
        emit_stage("write", "done")
        return {"answer_md": "# 深度报告", "usage_total": 0}

    original = {
        name: getattr(deep_mod, name)
        for name in (
            "planner",
            "research",
            "reflect",
            "supplement_node",
            "guards_node",
            "writer_node",
            "finalize_node",
        )
    }
    deep_mod.planner = _stub_planner
    deep_mod.research = _probe_research
    deep_mod.reflect = _stub_reflect
    deep_mod.supplement_node = _stub_reflect  # 不可达（gaps 恒空）——桩防误入
    deep_mod.guards_node = _stub_guards
    deep_mod.writer_node = _stub_writer
    deep_mod.finalize_node = _stub_finalize
    try:
        graph = build_deep_graph()
    finally:
        for name, fn in original.items():
            setattr(deep_mod, name, fn)
    return graph


def _make_probe_scheduler(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _ProbeRecorder,
    retriever: Any,
) -> Any:
    """探针调度器：复用 tp._make_scheduler 的 spec 捕获/假体注入，仅换探针图。"""
    subtopics = list(tp._SUBTOPICS)
    graph = _build_probe_graph(recorder, subtopics)
    scheduler = ts_mod.ThreadScheduler(
        bus=tp.EventBus(), session_factory=get_session_factory(), graph=graph
    )
    spec = copy.deepcopy(BUILTIN_DEFAULTS[_SPEC_DATA_ID])
    spec["hitl"]["enabled"] = False  # fast 键关——互不牵动
    spec["hitl"]["deep_enabled"] = True
    spec["hitl"]["shadow"] = False
    spec["kb"]["es_enabled"] = True
    spec["kb"]["embedding_enabled"] = True
    monkeypatch.setattr(scheduler, "_capture_spec_payload", lambda: spec)
    monkeypatch.setattr(
        scheduler, "_capture_mapping_payload", lambda: BUILTIN_DEFAULTS[tp._MAPPING_DATA_ID]
    )
    monkeypatch.setattr(scheduler, "_capture_mcp_payload", lambda: BUILTIN_DEFAULTS[tp._MCP_DATA_ID])
    monkeypatch.setattr(scheduler, "_kb_retriever", lambda: retriever)
    return scheduler


class _UuidKbRetriever:
    """探针 KB 假体：tp._FakeRetriever 变体——kb_doc_id 用合法 UUID（探针走真
    kb_search → 真候选账本 → 真 sources 落库面，kb_doc_id 列 UUID 约束）。"""

    def __init__(self) -> None:
        self.calls = 0

    def _rows(self) -> list[dict[str, Any]]:
        self.calls += 1
        return [
            {
                "chunk_id": "x" if self.calls % 2 == 1 else "y",
                "kb_doc_id": "00000000-0000-4000-8000-0000000000d1",
                "content": "KB 命中内容（deep 门面）",
                "score": 0.8,
                "heading_path": ["文档x", "命中X"],
                "node_id": None,
                "channel": "bm25" if self.calls % 2 == 1 else "vector",
            }
        ]

    async def bm25_search(self, query: str, *, user_id: Any, k: int) -> list[dict[str, Any]]:
        self.calls += 1
        return list(self._rows())

    async def vector_search(self, query_text: str, *, user_id: Any, k: int) -> list[dict[str, Any]]:
        self.calls += 1
        return list(self._rows())


# ---- 600s 独立包络重建（R31/nacos v1.50 分段注）----


async def test_deep_spec_timeout_600_and_deadline_rebuilt_per_phase(
    monkeypatch: pytest.MonkeyPatch, migrated_db: Any
) -> None:
    """deep.turn_timeout_s=600（defaults.py:196 v1.40）+ TurnDeadline 每阶段新建：
    阶段 1 与阶段 2 各自构造 [600, 600]——阶段 2 独立完整包络（awaiting 不占轮
    预算，plan §3.3/ARCH §6.2 分段语义；若阶段 2 复用阶段 1 deadline 则捕获数
    为 1 且第二段剩余预算为负）。"""
    assert BUILTIN_DEFAULTS[_SPEC_DATA_ID]["deep"]["turn_timeout_s"] == 600
    tp._seed(migrated_db)
    recorder = tp._Recorder()
    scheduler = tp._make_scheduler(monkeypatch, recorder, tp._FakeRetriever())
    monkeypatch.setattr("app.agent.nodes.kb_gate.rerank_via_gateway", tp._FakeRerank())

    captured: list[float] = []
    real_deadline = ts_mod.TurnDeadline

    class _RecordingDeadline(real_deadline):
        def __init__(self, timeout_s: float, **kw: Any) -> None:
            captured.append(timeout_s)
            super().__init__(timeout_s, **kw)

    monkeypatch.setattr(ts_mod, "TurnDeadline", _RecordingDeadline)

    await tp._run_phase1(scheduler, migrated_db)
    assert tp._turn_status(migrated_db) == "awaiting_approval"
    assert captured == [600]  # 阶段 1 恰一次构造（600s 包络）

    await scheduler.decide_approval(
        str(tp._TURN_ID), user_id=_USER_ID, choice="kb_only", note=None
    )
    await scheduler.resume_turn(str(tp._TURN_ID))
    assert tp._turn_status(migrated_db) == "completed"
    assert captured == [600, 600]  # 阶段 2 独立重建（非复用/非继承剩余预算）


# ---- 超时 hybrid：扫描收口 → resume both 形态 ----


async def test_deep_timeout_sweep_then_resume_hybrid(
    monkeypatch: pytest.MonkeyPatch, migrated_db: Any
) -> None:
    """deep awaiting 超时（on_timeout=hybrid 缺省）：expires_at 过期 → 扫描收口
    （choice=both/decided_by=timeout/审批行 expired）→ resume 阶段 2 完成 both
    形态——模式无关消费面在 deep 轮的对齐证明（批次 2 语义零改动）。"""
    tp._seed(migrated_db)
    recorder = tp._Recorder()
    scheduler = tp._make_scheduler(monkeypatch, recorder, tp._FakeRetriever())
    monkeypatch.setattr("app.agent.nodes.kb_gate.rerank_via_gateway", tp._FakeRerank())
    await tp._run_phase1(scheduler, migrated_db)
    assert tp._turn_status(migrated_db) == "awaiting_approval"

    conn = tp._connect(migrated_db)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE turn_approvals SET expires_at = %s WHERE turn_id = %s",
                (datetime.now(UTC) - timedelta(seconds=1), str(tp._TURN_ID)),
            )
        conn.commit()
    finally:
        conn.close()

    collected = await scheduler.collect_expired_approvals(
        now=datetime.now(UTC) + timedelta(minutes=2)
    )
    assert len(collected) == 1
    assert collected[0]["turn_id"] == str(tp._TURN_ID)
    assert collected[0]["choice"] == "both"
    assert collected[0]["decided_by"] == "timeout"
    assert collected[0]["on_timeout"] == "hybrid"
    approval = tp._approval(migrated_db)
    assert approval is not None
    assert approval["status"] == "expired" and approval["decided_by"] == "timeout"

    await scheduler.resume_turn(str(tp._TURN_ID))
    row = tp._turn_row(migrated_db)
    assert row is not None and row["status"] == "completed"
    assert recorder.planner_calls == 1 and recorder.research_calls == 1  # 阶段 2 照常恢复
    done = [p for t, p in tp._events(migrated_db) if t == "done"]
    assert len(done) == 1 and done[0]["finish"] == "completed"


# ---- awaiting 取消：直接终态（§2.7 v1.37 批次 2 语义 deep 对齐）----


async def test_deep_cancel_awaiting_finalize(
    monkeypatch: pytest.MonkeyPatch, migrated_db: Any
) -> None:
    """deep awaiting 取消=直接落库收尾 canceled(user)（无协程不登记标志）+ 审批行
    canceled（decided_by=system）+ approval_resolved(resolved_by=cancel，零 choice
    键）+ done(canceled, cancel_reason=user) 关通道；重复 resume 零副作用。"""
    tp._seed(migrated_db)
    recorder = tp._Recorder()
    scheduler = tp._make_scheduler(monkeypatch, recorder, tp._FakeRetriever())
    monkeypatch.setattr("app.agent.nodes.kb_gate.rerank_via_gateway", tp._FakeRerank())
    await tp._run_phase1(scheduler, migrated_db)
    assert tp._turn_status(migrated_db) == "awaiting_approval"

    result = await scheduler.cancel_turn(str(tp._TURN_ID), user_id=_USER_ID)
    assert result == {"turn_id": str(tp._TURN_ID), "status": "canceling"}

    row = tp._turn_row(migrated_db)
    assert row is not None and row["status"] == "canceled"
    assert row["cancel_reason"] == "user"
    approval = tp._approval(migrated_db)
    assert approval is not None
    assert approval["status"] == "canceled" and approval["decided_by"] == "system"
    events = tp._events(migrated_db)
    resolved = [p for t, p in events if t == "approval_resolved"]
    assert resolved and resolved[0]["resolved_by"] == "cancel"
    assert "choice" not in resolved[0]  # cancel 收口零 choice 键（api.md §7.4:1083）
    done = [p for t, p in events if t == "done"]
    assert done and done[0]["finish"] == "canceled" and done[0]["cancel_reason"] == "user"
    assert await scheduler.resume_turn(str(tp._TURN_ID)) is None  # 零副作用
    assert recorder.research_calls == 0  # 取消轮零阶段 2


# ---- 多标签页竞态：条件 UPDATE 先到先得 + 幂等 + 重复 resume 零副作用 ----


async def test_deep_multi_tab_race_and_idempotent(
    monkeypatch: pytest.MonkeyPatch, migrated_db: Any
) -> None:
    """R37 竞态面（批次 2 语义 deep 对齐）：标签页 A 裁决 kb_only 赢→阶段 2 完成；
    标签页 B 后到异 choice 裁决→409008（ApprovalConflictException）；完成后同
    choice 再提交=幂等 200；重复 resume 零副作用（单 done 不变量）。"""
    tp._seed(migrated_db)
    recorder = tp._Recorder()
    scheduler = tp._make_scheduler(monkeypatch, recorder, tp._FakeRetriever())
    monkeypatch.setattr("app.agent.nodes.kb_gate.rerank_via_gateway", tp._FakeRerank())
    await tp._run_phase1(scheduler, migrated_db)

    decision = await scheduler.decide_approval(
        str(tp._TURN_ID), user_id=_USER_ID, choice="kb_only", note=None
    )
    assert decision["idempotent"] is False
    with pytest.raises(ApprovalConflictException):
        await scheduler.decide_approval(
            str(tp._TURN_ID), user_id=_USER_ID, choice="web_only", note=None
        )

    await scheduler.resume_turn(str(tp._TURN_ID))
    row = tp._turn_row(migrated_db)
    assert row is not None and row["status"] == "completed"
    assert recorder.planner_calls == 1 and recorder.research_calls == 1
    assert len([t for t, _ in tp._events(migrated_db) if t == "done"]) == 1

    assert await scheduler.resume_turn(str(tp._TURN_ID)) is None  # 重复 resume 零副作用
    assert len([t for t, _ in tp._events(migrated_db) if t == "done"]) == 1
    repeat = await scheduler.decide_approval(
        str(tp._TURN_ID), user_id=_USER_ID, choice="kb_only", note=None
    )
    assert repeat["idempotent"] is True  # 同 choice 幂等 200
    with pytest.raises(ApprovalConflictException):
        await scheduler.decide_approval(
            str(tp._TURN_ID), user_id=_USER_ID, choice="web_only", note=None
        )


# ---- 工具桩化注入面贯通（第二段 R36 的端到端证明）----


async def test_deep_resume_tool_stub_kb_only(
    monkeypatch: pytest.MonkeyPatch, migrated_db: Any
) -> None:
    """kb_only 裁决 → 阶段 2 research 内：TurnRunner 注入 approval_choice → 图
    START 条件入口直达 research → resolve_tool_scope=kb_only → 真契约工具面——
    web_search 桩注「仅限知识库」（桩短路零物理）、kb_search live 命中行（不被
    桩化）。planner 零重跑（planner 产物恢复链路同段一）。"""
    tp._seed(migrated_db)
    recorder = _ProbeRecorder()
    scheduler = _make_probe_scheduler(monkeypatch, recorder, _UuidKbRetriever())
    monkeypatch.setattr("app.agent.nodes.kb_gate.rerank_via_gateway", tp._FakeRerank())
    await tp._run_phase1(scheduler, migrated_db)
    assert tp._turn_status(migrated_db) == "awaiting_approval"
    assert recorder.captured == {}  # 阶段 1 门命中=research 未跑（零探针）

    await scheduler.decide_approval(
        str(tp._TURN_ID), user_id=_USER_ID, choice="kb_only", note=None
    )
    await scheduler.resume_turn(str(tp._TURN_ID))

    row = tp._turn_row(migrated_db)
    assert row is not None and row["status"] == "completed", (
        f"captured={recorder.captured} error={row.get('error') if row else None}"
    )
    assert recorder.planner_calls == 1
    assert recorder.research_calls == 1
    assert recorder.captured["approval_choice"] == "kb_only"  # 注入贯通（state 可读）
    assert recorder.captured["scope"] == "kb_only"  # 作用域解析面
    assert "仅限知识库" in recorder.captured["web"]  # web_search 桩注
    assert "[1]" in recorder.captured["kb"]  # kb_search live 命中行（真路径）


async def test_deep_no_hit_same_run_kb_stub_plan_wording(
    monkeypatch: pytest.MonkeyPatch, migrated_db: Any
) -> None:
    """门判零命中（图不终结、research 同一执行继续）：kb_gate_action=web_only →
    resolve_tool_scope=no_hit → kb_search 桩 plan §3.3 :225 逐字；轮一次跑完
    completed + no_hit 轻量审计行（零 awaiting 零弹窗）。"""
    tp._seed(migrated_db)
    recorder = _ProbeRecorder()
    scheduler = _make_probe_scheduler(monkeypatch, recorder, tp._EmptyRetriever())
    monkeypatch.setattr("app.agent.nodes.kb_gate.rerank_via_gateway", tp._FakeRerank())
    await tp._run_phase1(scheduler, migrated_db)

    row = tp._turn_row(migrated_db)
    assert row is not None and row["status"] == "completed"  # 图一次跑完
    approval = tp._approval(migrated_db)
    assert approval is not None
    assert approval["status"] == "no_hit" and approval["verdict"] == "no_hit"
    assert recorder.planner_calls == 1 and recorder.research_calls == 1
    assert recorder.captured["approval_choice"] is None  # 阶段 1 无裁决键
    assert recorder.captured["scope"] == "no_hit"  # 门动作解析面
    assert recorder.captured["kb"] == "（系统注：知识库无相关内容，请仅使用网络来源。）"


# ---- 第四段（评审前置修复①）：kb_only 下 reflect→supplement 真节点回路终止性 ----


class _LoopRecorder(tp._Recorder):
    def __init__(self) -> None:
        super().__init__()
        self.reflect_calls = 0


def _build_supplement_loop_graph(recorder: _LoopRecorder, subtopics: list[str]):
    """真 deep 拓扑 + supplement **真节点**（评审前置修复①被测面——kb_only 短路）+
    reflect 桩两轮形态（首轮产 web gap、次轮清空）。物理安全网：tools_registry 注入
    _FullChainRegistry 假体（短路若失效也不发真实外呼——search_calls 计数即断言面）。"""
    from app.agent.graphs.signals import emit_stage

    registry_holder: dict[str, Any] = {}

    async def _stub_planner(state: Any, config: Any = None) -> dict[str, Any]:
        recorder.planner_calls += 1
        emit_stage("plan", "done", extra={"subtopics": len(subtopics)})
        return {
            "subtopics": list(subtopics),
            "outline": copy.deepcopy(tp._OUTLINE),
            "query_type": "factoid",
            "time_range": None,
        }

    async def _stub_research(state: Any, config: Any = None) -> dict[str, Any]:
        recorder.research_calls += 1
        recorder.research_subtopics.append(list(state.get("subtopics") or []))
        return {
            "subagent_reports": [],
            "visited_urls": [],
            "searched_queries": [],
            "collected_sources": [],
            "snapshots": [],
            "errors": [],
        }

    async def _stub_reflect(state: Any, config: Any = None) -> dict[str, Any]:
        recorder.reflect_calls += 1
        if recorder.reflect_calls == 1:
            return {"gaps": [{"gap": "缺口", "query": "补搜词", "origin": "web"}]}
        return {"gaps": []}

    async def _stub_guards(state: Any, config: Any = None) -> dict[str, Any]:
        return {}

    async def _stub_writer(state: Any, config: Any = None) -> dict[str, Any]:
        recorder.writer_outline.append(list(state.get("outline") or []))
        return {"report_md": "# 深度报告"}

    async def _stub_finalize(state: Any, config: Any = None) -> dict[str, Any]:
        emit_stage("write", "done")
        return {"answer_md": "# 深度报告", "usage_total": 0}

    original = {
        name: getattr(deep_mod, name)
        for name in (
            "planner",
            "research",
            "reflect",
            "guards_node",
            "writer_node",
            "finalize_node",
        )
    }
    deep_mod.planner = _stub_planner
    deep_mod.research = _stub_research
    deep_mod.reflect = _stub_reflect
    deep_mod.guards_node = _stub_guards
    deep_mod.writer_node = _stub_writer
    deep_mod.finalize_node = _stub_finalize
    try:
        graph = build_deep_graph()
    finally:
        for name, fn in original.items():
            setattr(deep_mod, name, fn)
    registry_holder["graph"] = graph
    return graph


def _make_loop_scheduler(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _LoopRecorder,
    retriever: Any,
) -> tuple[Any, Any]:
    subtopics = list(tp._SUBTOPICS)
    graph = _build_supplement_loop_graph(recorder, subtopics)
    from test.integration.test_deep_full_chain import _FullChainRegistry

    fake_registry = _FullChainRegistry()
    scheduler = ts_mod.ThreadScheduler(
        bus=tp.EventBus(),
        session_factory=get_session_factory(),
        graph=graph,
        tools_registry=fake_registry,
    )
    spec = copy.deepcopy(BUILTIN_DEFAULTS[_SPEC_DATA_ID])
    spec["hitl"]["enabled"] = False
    spec["hitl"]["deep_enabled"] = True
    spec["hitl"]["shadow"] = False
    spec["kb"]["es_enabled"] = True
    spec["kb"]["embedding_enabled"] = True
    monkeypatch.setattr(scheduler, "_capture_spec_payload", lambda: spec)
    monkeypatch.setattr(
        scheduler, "_capture_mapping_payload", lambda: BUILTIN_DEFAULTS[tp._MAPPING_DATA_ID]
    )
    monkeypatch.setattr(scheduler, "_capture_mcp_payload", lambda: BUILTIN_DEFAULTS[tp._MCP_DATA_ID])
    monkeypatch.setattr(scheduler, "_kb_retriever", lambda: retriever)
    return scheduler, fake_registry


async def test_deep_kb_only_supplement_loop_terminates(
    monkeypatch: pytest.MonkeyPatch, migrated_db: Any
) -> None:
    """kb_only 裁决下 reflect→supplement 回路（supplement 真节点）：首轮 web gap →
    supplement 短路（零外呼零 stage 帧、reflect_round+1）→ 次轮 gaps 清空 → guards
    → writer → completed——回路不空转（轮次帽+短路推进双保险）；物理安全网
    _FullChainRegistry.search_calls==0（短路失效即真实外呼的断言面）。"""
    tp._seed(migrated_db)
    recorder = _LoopRecorder()
    scheduler, fake_registry = _make_loop_scheduler(monkeypatch, recorder, tp._FakeRetriever())
    monkeypatch.setattr("app.agent.nodes.kb_gate.rerank_via_gateway", tp._FakeRerank())
    await tp._run_phase1(scheduler, migrated_db)
    assert tp._turn_status(migrated_db) == "awaiting_approval"

    await scheduler.decide_approval(
        str(tp._TURN_ID), user_id=_USER_ID, choice="kb_only", note=None
    )
    await scheduler.resume_turn(str(tp._TURN_ID))

    row = tp._turn_row(migrated_db)
    assert row is not None and row["status"] == "completed"  # 回路终止（不空转）
    assert row["answer_md"] == "# 深度报告"
    assert recorder.planner_calls == 1 and recorder.research_calls == 1
    assert recorder.reflect_calls == 2  # 缺口轮 + 清空轮（短路推进后收敛）
    assert fake_registry.search_calls == 0  # supplement 短路零外呼（安全网实证）
    stage_frames = [p for t, p in tp._events(migrated_db) if t == "stage"]
    stage_keys = [p.get("stage_key") for p in stage_frames]
    assert "search" not in stage_keys, f"stage_frames={stage_frames}"  # 短路轮零 stage 帧
