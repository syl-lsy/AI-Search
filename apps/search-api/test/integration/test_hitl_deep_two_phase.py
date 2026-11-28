"""integration 层：deep 两阶段执行（B22 批次 4 第一段，真库 requires_db）。

契约锚点（见各用例断言面）：
- plan §3.3：planner 后门节点（deep_enabled 独立灰度——spec enabled=false 仅
  deep_enabled=true 即进门，约束①互不牵动）；has_hit → awaiting + approval_requested
  SSE 帧（复用批次 2 open/_finish_awaiting/发布时序——模式无关面零改动消费）；
- 阶段 2 planner 产物恢复（R30 兼容扩展，ARCH §4.2 分期注）：resume 注入
  subtopics/outline（自 gate_payload.planner）→ research/writer 消费面重建；
  planner 零重跑（api.md §7.4:1723「阶段 2 无 stage(plan)」）；
- G3：deep_enabled=false（含 enabled=true 仅 fast 键形态）= 现状逐字节——轮一次
  跑完、零审批行、零 awaiting（存量 deep 测试零改动由既有套件承担）。

图形态：真 deep 拒绝重复跑——真 deep 拓扑（build_deep_graph，含批次 4 门两节点+
三条件边）+ LLM 节点桩（planner/research/reflect/supplement/guards/writer/finalize
模块级打桩，deep 门/路由器/approval_wait 为真实现）；检索面经
scheduler._kb_retriever monkeypatch 注入（批次 2 同款姿势）。
"""

import copy
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg2
import pytest

from app.agent.graphs import deep as deep_mod
from app.agent.graphs.deep import build_deep_graph
from app.config_center.defaults import BUILTIN_DEFAULTS
from app.core.config import common_settings
from app.db.session import get_session_factory
from app.services.event_bus import EventBus
from app.services.turn_service import ThreadScheduler, get_turn_detail

pytestmark = pytest.mark.requires_db

_USER_ID = common_settings.default_user_id
_SPEC_DATA_ID = "ai_search.agent.spec.json"
_MAPPING_DATA_ID = "ai_search.model.mapping.json"
_MCP_DATA_ID = "ai_search.agent.mcp.json"
_THREAD_ID = uuid.uuid4()
_TURN_ID = uuid.uuid4()

_SUBTOPICS = ["子课题一：KB 语料探针", "子课题二：两阶段恢复"]
_OUTLINE = [
    {"section": "S1", "intent": "KB 语料探针形态"},
    {"section": "S2", "intent": "两阶段恢复形态"},
]

# ---- SQL：一律 %s 参数绑定（cursor.execute 两参式——项目铁律）----


class _Recorder:
    """轮内节点桩调用记录（planner 零重跑 / 恢复面断言源）。"""

    def __init__(self) -> None:
        self.planner_calls = 0
        self.research_calls = 0
        self.research_subtopics: list[list[str]] = []
        self.writer_outline: list[list[dict[str, Any]]] = []


def _build_patched_deep_graph(recorder: _Recorder, subtopics: list[str]):
    """真 deep 拓扑 + LLM 节点桩（deep.py from-import + add_node 持有模块命名空间
    引用——patch 后编译即持有桩，test_deep_reflection_loop.py:149-150 先例姿势）。
    deep 门/三路由器/approval_wait 为真实现（被测面）。"""

    from app.agent.graphs.signals import emit_stage

    async def _stub_planner(state: Any, config: Any = None) -> dict[str, Any]:
        recorder.planner_calls += 1
        emit_stage("plan", "done", extra={"subtopics": len(subtopics)})
        return {
            "subtopics": list(subtopics),
            "outline": copy.deepcopy(_OUTLINE),
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
        return {"gaps": [], "reflect_round": 1}

    async def _stub_guards(state: Any, config: Any = None) -> dict[str, Any]:
        return {}  # 无 trip（degraded_reason 缺席 → route_guards → writer）

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
    deep_mod.research = _stub_research
    deep_mod.reflect = _stub_reflect
    deep_mod.supplement_node = _stub_research  # 不可达（gaps 恒空）——桩防误入
    deep_mod.guards_node = _stub_guards
    deep_mod.writer_node = _stub_writer
    deep_mod.finalize_node = _stub_finalize
    try:
        graph = build_deep_graph()
    finally:
        for name, fn in original.items():
            setattr(deep_mod, name, fn)
    return graph


class _FakeRetriever:
    """门内检索假体：逐子课题返回命中（chunk x/y 双通道同命）。"""

    def __init__(self) -> None:
        self.calls = 0

    def _rows(self) -> list[dict[str, Any]]:
        return [
            {
                "chunk_id": "x" if self.calls % 2 == 1 else "y",
                "kb_doc_id": "d1",
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


class _EmptyRetriever:
    """零召回假体（no_hit → research 继续形态）。"""

    async def bm25_search(self, query: str, *, user_id: Any, k: int) -> list[dict[str, Any]]:
        return []

    async def vector_search(self, query_text: str, *, user_id: Any, k: int) -> list[dict[str, Any]]:
        return []


class _FailingRetriever:
    """双通道全败假体（R38 回落形态）。"""

    async def bm25_search(self, query: str, *, user_id: Any, k: int) -> list[dict[str, Any]]:
        raise ValueError("es down")

    async def vector_search(self, query_text: str, *, user_id: Any, k: int) -> list[dict[str, Any]]:
        raise ValueError("pg down")


class _FakeRerank:
    async def __call__(
        self, query: str, documents: list[str], *, model_alias: str, timeout_s: float
    ) -> list[float | None]:
        return [0.9] * len(documents)


def _make_scheduler(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
    retriever: Any,
    *,
    subtopics: list[str] | None = None,
    deep_enabled: bool = True,
    hitl_overrides: dict[str, Any] | None = None,
) -> ThreadScheduler:
    subtopics = subtopics if subtopics is not None else _SUBTOPICS
    graph = _build_patched_deep_graph(recorder, subtopics)
    scheduler = ThreadScheduler(
        bus=EventBus(), session_factory=get_session_factory(), graph=graph
    )
    spec = copy.deepcopy(BUILTIN_DEFAULTS[_SPEC_DATA_ID])
    spec["hitl"]["enabled"] = False  # fast 键关——互不牵动（约束①）
    spec["hitl"]["deep_enabled"] = deep_enabled
    spec["hitl"]["shadow"] = False
    if hitl_overrides:
        spec["hitl"].update(hitl_overrides)
    spec["kb"]["es_enabled"] = True
    spec["kb"]["embedding_enabled"] = True
    monkeypatch.setattr(scheduler, "_capture_spec_payload", lambda: spec)
    monkeypatch.setattr(
        scheduler, "_capture_mapping_payload", lambda: BUILTIN_DEFAULTS[_MAPPING_DATA_ID]
    )
    monkeypatch.setattr(scheduler, "_capture_mcp_payload", lambda: BUILTIN_DEFAULTS[_MCP_DATA_ID])
    monkeypatch.setattr(scheduler, "_kb_retriever", lambda: retriever)
    return scheduler


def _connect(info: Any):
    return psycopg2.connect(
        host=info.host,
        port=int(info.port),
        user=info.user,
        password=info.password,
        dbname=info.name,
        connect_timeout=5,
    )


def _seed(info: Any) -> None:
    conn = _connect(info)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO threads (id, user_id, title, mode_summary, turn_count) "
                "VALUES (%s, %s, %s, %s, 1)",
                (str(_THREAD_ID), _USER_ID, "deep 两阶段测试线程", "deep"),
            )
            cursor.execute(
                "INSERT INTO turns (id, thread_id, user_id, mode, status, query, "
                "rewritten_queries, chat_model_key, model_name, timing, usage, "
                "query_lang, started_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    str(_TURN_ID),
                    str(_THREAD_ID),
                    _USER_ID,
                    "deep",
                    "running",
                    "深度研究一下知识库审批门",
                    "[]",
                    "qwen",
                    "chat-standard",
                    "{}",
                    "{}",
                    "zh",
                    datetime.now(UTC),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _turn_row(info: Any) -> dict[str, Any] | None:
    conn = _connect(info)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT status, answer_md, cancel_reason, error, duration_ms, "
                "finished_at, created_at, usage FROM turns WHERE id = %s",
                (str(_TURN_ID),),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            keys = ("status", "answer_md", "cancel_reason", "error", "duration_ms", "finished_at", "created_at", "usage")
            return dict(zip(keys, row))
    finally:
        conn.close()


def _turn_status(info: Any) -> str | None:
    conn = _connect(info)
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT status FROM turns WHERE id = %s", (str(_TURN_ID),))
            row = cursor.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def _approval(info: Any) -> dict[str, Any] | None:
    conn = _connect(info)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT status, verdict, choice, decided_by, gate_payload, previews "
                "FROM turn_approvals WHERE turn_id = %s",
                (str(_TURN_ID),),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            keys = ("status", "verdict", "choice", "decided_by", "gate_payload", "previews")
            return dict(zip(keys, row))
    finally:
        conn.close()


def _events(info: Any) -> list[tuple[str, dict[str, Any]]]:
    conn = _connect(info)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT event_type, payload FROM turn_events WHERE turn_id = %s "
                "ORDER BY seq ASC",
                (str(_TURN_ID),),
            )
            return [(r[0], r[1]) for r in cursor.fetchall()]
    finally:
        conn.close()


async def _run_phase1(scheduler: ThreadScheduler, info: Any) -> None:
    async with get_session_factory()() as session:
        from app.db.models import Turn

        row = await session.get(Turn, _TURN_ID)
        assert row is not None
        await scheduler._execute_turn(row)


@pytest.fixture
def deep_two_phase_env(monkeypatch: pytest.MonkeyPatch, migrated_db: Any) -> Any:
    """播种 running 深轮 + 桩图调度器（deep_enabled 门命中形态）。"""
    _seed(migrated_db)
    recorder = _Recorder()
    retriever = _FakeRetriever()
    scheduler = _make_scheduler(monkeypatch, recorder, retriever)
    monkeypatch.setattr("app.agent.nodes.kb_gate.rerank_via_gateway", _FakeRerank())
    return {
        "info": migrated_db,
        "recorder": recorder,
        "retriever": retriever,
        "scheduler": scheduler,
    }


# ---- 两阶段全流程：planner 产物持久化 + 阶段 2 恢复 ----


async def test_deep_two_phase_full_flow_kb_only(
    deep_two_phase_env: dict[str, Any],
) -> None:
    """plan §3.3 deep 全流程：阶段 1（planner→门命中→awaiting）+ approval_requested
    落库（零 done）；裁决 kb_only → 阶段 2 **planner 零重跑**（阶段 2 无 stage(plan)）
    + subtopics/outline 自 gate_payload.planner 重建（research/writer 消费面）→
    done(completed)。"""
    info = deep_two_phase_env["info"]
    scheduler = deep_two_phase_env["scheduler"]
    await _run_phase1(scheduler, info)

    row = _turn_row(info)
    assert row is not None and row["status"] == "awaiting_approval"
    assert row["answer_md"] is None and row["finished_at"] is None  # 协程释放零终态
    approval = _approval(info)
    assert approval is not None
    assert approval["status"] == "pending" and approval["verdict"] == "has_hit"
    payload = approval["gate_payload"]
    assert payload["schema_version"] == 1  # R30 兼容扩展——版本不变
    assert payload["planner"] == {"subtopics": _SUBTOPICS, "outline": _OUTLINE}
    assert payload["rewritten_queries"] == _SUBTOPICS
    assert payload["kb_candidates"], "门内候选在载荷"
    events = _events(info)
    requested = [p for t, p in events if t == "approval_requested"]
    assert len(requested) == 1
    assert requested[0]["verdict"] == "has_hit"
    assert requested[0]["previews"] and len(requested[0]["previews"][0]["excerpt"]) <= 200
    assert not [t for t, _ in events if t == "done"]  # 阶段 1 零 done（连接保持仅心跳）
    assert deep_two_phase_env["recorder"].research_calls == 0  # 门命中=research 未跑
    # 阶段 1 plan 帧形态：running（TurnRunner 图前首帧）+ planner 自帧 done +
    # 门判定帧 done（同 key 多次合法 §7.2）= 恰 3 帧
    plan_frames = [
        p for t, p in events if t == "stage" and p.get("stage_key") == "plan"
    ]
    assert len(plan_frames) == 3
    assert sum(1 for p in plan_frames if p.get("status") == "running") == 1
    assert sum(1 for p in plan_frames if p.get("status") == "done") == 2

    # 裁决 kb_only → 阶段 2（planner 产物恢复）
    decision = await scheduler.decide_approval(
        str(_TURN_ID), user_id=_USER_ID, choice="kb_only", note=None
    )
    assert decision["idempotent"] is False
    resolved = [p for t, p in _events(info) if t == "approval_resolved"]
    assert resolved and resolved[0]["resolved_by"] == "submit"
    assert resolved[0]["choice"] == "kb_only"

    await scheduler.resume_turn(str(_TURN_ID))

    row = _turn_row(info)
    assert row is not None and row["status"] == "completed"
    assert row["answer_md"] == "# 深度报告"
    recorder = deep_two_phase_env["recorder"]
    # planner 零重跑（阶段 2 无 stage(plan)——planner 产物自载荷恢复）
    assert recorder.planner_calls == 1
    assert recorder.research_calls == 1  # 阶段 2 恰一次（阶段 1 门命中未跑）
    assert recorder.research_subtopics == [_SUBTOPICS]  # subtopics 恢复进 research 面
    assert recorder.writer_outline == [_OUTLINE]  # outline 恢复进 writer 面
    done = [p for t, p in _events(info) if t == "done"]
    assert len(done) == 1 and done[0]["finish"] == "completed"
    # 阶段 2 无 stage(plan) 发射（api.md §7.4:1723）——plan 帧维持阶段 1 的 3 帧
    # 零新增（resume 直达 research）
    stage_keys = [p.get("stage_key") for t, p in _events(info) if t == "stage"]
    assert stage_keys.count("plan") == 3
    assert "rewrite" not in stage_keys  # deep 轮零 rewrite 帧（mode 感知 merge 面）


async def test_deep_two_phase_web_only_resume(
    deep_two_phase_env: dict[str, Any],
) -> None:
    """web_only：阶段 2 同样跳过 planner、subtopics 恢复（choice 只改 research
    消费面语义——桩工具归第二段，本段断言恢复机制面）。"""
    info = deep_two_phase_env["info"]
    scheduler = deep_two_phase_env["scheduler"]
    await _run_phase1(scheduler, info)
    await scheduler.decide_approval(
        str(_TURN_ID), user_id=_USER_ID, choice="web_only", note=None
    )
    await scheduler.resume_turn(str(_TURN_ID))

    row = _turn_row(info)
    assert row is not None and row["status"] == "completed"
    recorder = deep_two_phase_env["recorder"]
    assert recorder.planner_calls == 1
    assert recorder.research_calls == 1
    assert recorder.research_subtopics == [_SUBTOPICS]


# ---- G3：deep_enabled=false 逐字节现状 ----


async def test_deep_gate_closed_byte_identical(
    monkeypatch: pytest.MonkeyPatch, migrated_db: Any
) -> None:
    """G3：deep_enabled=false（缺省灰度态）→ 门分支不可达——轮一次跑完 completed、
    零审批行、零 awaiting、零 approval_* 事件。"""
    _seed(migrated_db)
    recorder = _Recorder()
    scheduler = _make_scheduler(monkeypatch, recorder, _FakeRetriever(), deep_enabled=False)
    monkeypatch.setattr("app.agent.nodes.kb_gate.rerank_via_gateway", _FakeRerank())
    await _run_phase1(scheduler, migrated_db)

    row = _turn_row(migrated_db)
    assert row is not None and row["status"] == "completed"
    assert row["answer_md"] == "# 深度报告"
    assert _approval(migrated_db) is None  # 零审批行
    event_types = [t for t, _ in _events(migrated_db)]
    assert "approval_requested" not in event_types
    assert "approval_resolved" not in event_types
    assert row["status"] != "awaiting_approval"
    assert recorder.planner_calls == 1 and recorder.research_calls == 1  # 现状主干照跑


async def test_deep_enabled_independent_of_enabled(
    monkeypatch: pytest.MonkeyPatch, migrated_db: Any
) -> None:
    """约束①互不牵动（integration 面）：enabled=true 仅 fast 键——deep 轮不进门
    （deep_enabled=false），轮照常完成、零审批行。"""
    _seed(migrated_db)
    recorder = _Recorder()
    scheduler = _make_scheduler(
        monkeypatch,
        recorder,
        _FakeRetriever(),
        deep_enabled=False,
        hitl_overrides={"enabled": True},
    )
    monkeypatch.setattr("app.agent.nodes.kb_gate.rerank_via_gateway", _FakeRerank())
    await _run_phase1(scheduler, migrated_db)

    row = _turn_row(migrated_db)
    assert row is not None and row["status"] == "completed"
    assert _approval(migrated_db) is None
    assert recorder.planner_calls == 1 and recorder.research_calls == 1


# ---- web 直达（no_hit）与 shadow 审计行（deep 面）----


async def test_deep_no_hit_audit_row(
    monkeypatch: pytest.MonkeyPatch, migrated_db: Any
) -> None:
    """零命中 → research 同一执行继续（图一次跑完不中断）+ no_hit 轻量审计行
    （R28 事后追查依据；模式无关消费面零改动）。"""
    _seed(migrated_db)
    recorder = _Recorder()
    scheduler = _make_scheduler(monkeypatch, recorder, _EmptyRetriever())
    monkeypatch.setattr("app.agent.nodes.kb_gate.rerank_via_gateway", _FakeRerank())
    await _run_phase1(scheduler, migrated_db)

    row = _turn_row(migrated_db)
    assert row is not None and row["status"] == "completed"  # 图一次跑完
    approval = _approval(migrated_db)
    assert approval is not None
    assert approval["status"] == "no_hit" and approval["verdict"] == "no_hit"
    assert approval["gate_payload"] is None
    assert recorder.planner_calls == 1 and recorder.research_calls == 1  # research 继续
    events = _events(migrated_db)
    assert not [t for t, _ in events if t == "approval_requested"]  # 零弹窗


async def test_deep_shadow_audit_row(
    monkeypatch: pytest.MonkeyPatch, migrated_db: Any
) -> None:
    """shadow：门照常检索落留痕、行为一律现状（research 照跑）+ shadow 审计行
    （hits_summary/previews 落库供噪声地板 T 标定）。"""
    _seed(migrated_db)
    recorder = _Recorder()
    scheduler = _make_scheduler(
        monkeypatch, recorder, _FakeRetriever(), hitl_overrides={"shadow": True}
    )
    monkeypatch.setattr("app.agent.nodes.kb_gate.rerank_via_gateway", _FakeRerank())
    await _run_phase1(scheduler, migrated_db)

    row = _turn_row(migrated_db)
    assert row is not None and row["status"] == "completed"  # 行为现状
    conn = _connect(migrated_db)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT status, verdict, previews FROM turn_approvals "
                "WHERE turn_id = %s",
                (str(_TURN_ID),),
            )
            audit_row = cursor.fetchone()
    finally:
        conn.close()
    assert audit_row is not None
    assert audit_row[0] == "shadow" and audit_row[1] == "has_hit"
    assert audit_row[2]  # previews 落库（T 标定输入）


# ---- R38：通道全败回落现状（research 照跑、零审批行）----


async def test_deep_channel_failed_falls_back(
    monkeypatch: pytest.MonkeyPatch, migrated_db: Any
) -> None:
    """R38（deep 面）：任一子课题全部通道失败=「不知道」→ 回落现状 hybrid、
    research 照跑、零审批行、零 awaiting（不判无）。"""
    _seed(migrated_db)
    recorder = _Recorder()
    scheduler = _make_scheduler(monkeypatch, recorder, _FailingRetriever())
    monkeypatch.setattr("app.agent.nodes.kb_gate.rerank_via_gateway", _FakeRerank())
    await _run_phase1(scheduler, migrated_db)

    row = _turn_row(migrated_db)
    assert row is not None and row["status"] == "completed"
    assert _approval(migrated_db) is None  # 回落现状=无审批行
    assert recorder.research_calls == 1
    assert not [t for t, _ in _events(migrated_db) if t == "approval_requested"]


# ---- timing 面：plan 条目 extra.hitl 合并（mode 感知 merge——零伪 rewrite 条目）----


async def test_deep_timing_merges_hitl_into_plan_stage(
    deep_two_phase_env: dict[str, Any],
) -> None:
    """_merge_gate_hitl_extra mode 感知（批次 4 接线点）：deep 门轮判定面合并进
    已合档 plan 条目（浅合并保 planner 自帧 subtopics 键）——**零伪 rewrite 条目**；
    回看时间线 plan 恰一条。"""
    from test.integration.conftest import await_until

    info = deep_two_phase_env["info"]
    scheduler = deep_two_phase_env["scheduler"]
    await _run_phase1(scheduler, info)
    await scheduler.decide_approval(
        str(_TURN_ID), user_id=_USER_ID, choice="both", note=None
    )
    await scheduler.resume_turn(str(_TURN_ID))
    await await_until(
        lambda: _turn_status(info) == "completed", description="阶段 2 完成"
    )
    async with get_session_factory()() as session:
        detail = await get_turn_detail(session, str(_TURN_ID), user_id=_USER_ID)
    stage_keys = [s.stage_key for s in detail.stages]
    assert "rewrite" not in stage_keys  # 零伪 rewrite 条目（mode 感知 merge）
    assert stage_keys.count("plan") == 1  # 零重复条目
    plan_stage = detail.stages[0]
    assert plan_stage.stage_key == "plan"
    assert plan_stage.extra is not None
    assert plan_stage.extra.get("subtopics") == 2  # planner 自帧键保留（浅合并）
    hitl_extra = plan_stage.extra.get("hitl")
    assert hitl_extra is not None
    assert hitl_extra["verdict"] == "has_hit"
    assert hitl_extra["gate"] == "triggered"
    assert isinstance(hitl_extra["gate_ms"], int)