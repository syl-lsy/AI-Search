"""integration 层：TurnRunner 两阶段执行（B22 批次 2 第四段，真库 requires_db）。

契约锚点（见各用例 docstring）：
- plan §3.4 两阶段时序：阶段 1 跑到门→审批行落库→turn 置 awaiting_approval→协程
  释放（SSE 连接保持仅心跳——零 done 零 close）；裁决后按持久化载荷重新发起阶段 2
  （turn_deadline 重建——阶段 2 独立完整包络；KB-only 注入门内 TopK 并集零 kb Send）；
- api.md §7.2：approval_requested（落库子集、发布时序=落库后）+ approval_resolved
  （submit/timeout/cancel 三路径）；
- §2.7 v1.37：awaiting 取消直接终态 canceled(user)+审批行 canceled；
- api.md §2.5 v1.37：awaiting_ms 单列 + duration_ms 扣除 awaiting 时长；
- data-model §1.20/R30：gate_payload.schema_version 不符→轮 failed+明确 error；
- data-model §4 v1.23：孤儿清理豁免 awaiting（running/queued 照清）。

图形态：真 fast 拓扑（build_fast_graph）+ LLM 节点桩（rewrite/synthesis/web_search/
kb_search/merge_dedupe/fuse_rerank 打桩，kb_gate/approval_wait/路由器为真实现）；
检索面经 configurable 假体注入（scheduler._kb_retriever monkeypatch）。
"""

import asyncio
import copy
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg2
import pytest

from app.agent.guards import TurnCanceledSignal
from app.agent.graphs import fast as fast_module
from app.agent.graphs.fast import build_fast_graph
from app.config_center.defaults import BUILTIN_DEFAULTS
from app.core.config import common_settings
from app.db.session import get_session_factory
from app.exception.conflict import ApprovalConflictException
from app.main import _ORPHAN_TURN_STATUSES, _cleanup_orphan_turns
from app.services.event_bus import EventBus
from app.services.turn_service import ThreadScheduler, get_turn_detail

pytestmark = pytest.mark.requires_db

_USER_ID = common_settings.default_user_id
_SPEC_DATA_ID = "ai_search.agent.spec.json"
_MAPPING_DATA_ID = "ai_search.model.mapping.json"
_MCP_DATA_ID = "ai_search.agent.mcp.json"
_THREAD_ID = uuid.uuid4()
_TURN_ID = uuid.uuid4()


# ---- 记录器与桩节点 ----


class _Recorder:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.web_calls = 0
        self.kb_calls = 0
        self.merged_candidates: list[dict[str, Any]] = []
        self.approval_notes: list[Any] = []  # 评审 F1/F4：synthesis 桩捕获 state.approval_note
        self.cancel_at_synthesis = False  # 评审 F2 残余：阶段 2 流内 TurnCanceledSignal 驱动面


def _build_patched_graph(recorder: _Recorder, queries: list[str]):
    """真 fast 拓扑 + LLM 节点桩：build_fast_graph 前 monkeypatch fast 模块级节点名
    （build_fast_graph 引用模块命名空间——patch 后编译即持有桩）。

    _stub_rewrite 发生产同款自身 done 帧（extra.strategy——fast_rewrite.py:712
    形态，评审 R2 验证分叉消除：生产 rewrite 先合档、门节点第二帧被
    _absorb_stage 丢弃的时序在测试中逐字节复现）。"""

    from app.agent.graphs.signals import emit_stage

    async def _stub_rewrite(state: Any, config: Any = None) -> dict[str, Any]:
        emit_stage("rewrite", "done", extra={"strategy": "web"})
        return {
            "rewritten_queries": list(queries),
            "query_type": "factoid",
            "search_strategy": "web",
        }

    async def _stub_web(state: Any, config: Any = None) -> dict[str, Any]:
        recorder.web_calls += 1
        return {"candidates": [], "errors": []}

    async def _stub_kb(state: Any, config: Any = None) -> dict[str, Any]:
        recorder.kb_calls += 1
        return {"candidates": [], "errors": []}

    async def _stub_merge(state: Any, config: Any = None) -> dict[str, Any]:
        recorder.merged_candidates.extend(state.get("candidates") or [])
        return {"fused": [], "images_raw": []}

    async def _stub_fuse(state: Any, config: Any = None) -> dict[str, Any]:
        return {"fused": list(state.get("fused") or [])}

    async def _stub_synthesis(state: Any, config: Any = None) -> dict[str, Any]:
        recorder.approval_notes.append(state.get("approval_note"))
        if recorder.cancel_at_synthesis:
            raise TurnCanceledSignal("user")
        return {"answer_md": "# 两阶段答案"}

    original = {
        name: getattr(fast_module, name)
        for name in ("rewrite", "web_search", "kb_search", "merge_dedupe", "fuse_rerank", "synthesis")
    }
    fast_module.rewrite = _stub_rewrite
    fast_module.web_search = _stub_web
    fast_module.kb_search = _stub_kb
    fast_module.merge_dedupe = _stub_merge
    fast_module.fuse_rerank = _stub_fuse
    fast_module.synthesis = _stub_synthesis
    try:
        graph = build_fast_graph()
    finally:
        for name, fn in original.items():
            setattr(fast_module, name, fn)
    return graph


class _FakeRetriever:
    """门内检索假体：逐子问题返回命中（chunk x 双通道同命）。"""

    def __init__(self) -> None:
        self.calls = 0

    def _rows(self) -> list[dict[str, Any]]:
        return [
            {
                "chunk_id": "x",
                "kb_doc_id": "d1",
                "content": "KB 命中内容（RRF 融合面）",
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


class _FakeRerank:
    async def __call__(self, query: str, documents: list[str], *, model_alias: str, timeout_s: float) -> list[float | None]:
        return [0.9] * len(documents)


# ---- 调度器工厂与播种 ----


def _make_scheduler(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
    retriever: Any,
    *,
    queries: list[str],
    hitl_overrides: dict[str, Any] | None = None,
) -> ThreadScheduler:
    graph = _build_patched_graph(recorder, queries)
    scheduler = ThreadScheduler(
        bus=EventBus(), session_factory=get_session_factory(), graph=graph
    )
    spec = copy.deepcopy(BUILTIN_DEFAULTS[_SPEC_DATA_ID])
    spec["hitl"]["enabled"] = True
    spec["hitl"]["shadow"] = False
    if hitl_overrides:
        spec["hitl"].update(hitl_overrides)
    spec["kb"]["es_enabled"] = True
    spec["kb"]["embedding_enabled"] = True
    monkeypatch.setattr(
        scheduler,
        "_capture_spec_payload",
        lambda: spec,
    )
    monkeypatch.setattr(
        scheduler,
        "_capture_mapping_payload",
        lambda: BUILTIN_DEFAULTS[_MAPPING_DATA_ID],
    )
    monkeypatch.setattr(
        scheduler,
        "_capture_mcp_payload",
        lambda: BUILTIN_DEFAULTS[_MCP_DATA_ID],
    )
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
                (str(_THREAD_ID), _USER_ID, "两阶段测试线程", "fast"),
            )
            cursor.execute(
                "INSERT INTO turns (id, thread_id, user_id, mode, status, query, "
                "rewritten_queries, chat_model_key, model_name, timing, usage, "
                "query_lang, started_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, %s::jsonb, %s, %s)",
                (
                    str(_TURN_ID),
                    str(_THREAD_ID),
                    _USER_ID,
                    "fast",
                    "running",
                    "知识库里有 RRF 融合的资料吗？",
                    '["知识库 RRF 融合"]',
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
            keys = (
                "status",
                "answer_md",
                "cancel_reason",
                "error",
                "duration_ms",
                "finished_at",
                "created_at",
                "usage",
            )
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
                "SELECT status, verdict, choice, decided_by, gate_payload, previews, "
                "requested_at, expires_at, responded_at FROM turn_approvals "
                "WHERE turn_id = %s",
                (str(_TURN_ID),),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            keys = (
                "status",
                "verdict",
                "choice",
                "decided_by",
                "gate_payload",
                "previews",
                "requested_at",
                "expires_at",
                "responded_at",
            )
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
def two_phase_env(monkeypatch: pytest.MonkeyPatch, migrated_db: Any) -> Any:
    """播种 running 轮 + 桩图调度器（门命中形态）；用后随 truncate 清理。"""
    _seed(migrated_db)
    recorder = _Recorder()
    retriever = _FakeRetriever()
    scheduler = _make_scheduler(
        monkeypatch, recorder, retriever, queries=["知识库 RRF 融合"]
    )
    monkeypatch.setattr("app.agent.nodes.kb_gate.rerank_via_gateway", _FakeRerank())
    yield {
        "info": migrated_db,
        "recorder": recorder,
        "retriever": retriever,
        "scheduler": scheduler,
    }


# ---- 两阶段全流程（kb_only：零 Send + 门内候选注入）----


async def test_two_phase_full_flow_kb_only(two_phase_env: dict[str, Any]) -> None:
    """plan §3.4 全流程：阶段 1 → awaiting + approval_requested 落库（零 done）；
    裁决 kb_only → 阶段 2 完成（零 web/kb Send、门内候选注入 merge_dedupe、
    done(completed)）；awaiting_ms 单列 + duration_ms 扣除等待段。"""
    info = two_phase_env["info"]
    scheduler = two_phase_env["scheduler"]
    await _run_phase1(scheduler, info)

    row = _turn_row(info)
    assert row is not None and row["status"] == "awaiting_approval"
    assert row["answer_md"] is None and row["finished_at"] is None  # 协程释放零终态
    approval = _approval(info)
    assert approval is not None
    assert approval["status"] == "pending" and approval["verdict"] == "has_hit"
    assert approval["gate_payload"]["schema_version"] == 1
    assert approval["expires_at"] > approval["requested_at"]
    events = _events(info)
    requested = [p for t, p in events if t == "approval_requested"]
    assert len(requested) == 1
    assert requested[0]["verdict"] == "has_hit"
    assert requested[0]["previews"] and len(requested[0]["previews"][0]["excerpt"]) <= 200
    assert not [t for t, _ in events if t == "done"]  # 阶段 1 零 done（连接保持仅心跳）

    # 裁决 kb_only → 阶段 2
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
    assert row["answer_md"] == "# 两阶段答案"
    recorder = two_phase_env["recorder"]
    assert recorder.web_calls == 0 and recorder.kb_calls == 0  # 零 Send（约束④）
    assert [c["chunk_id"] for c in recorder.merged_candidates] == ["x"]  # 门内候选注入
    done = [p for t, p in _events(info) if t == "done"]
    assert len(done) == 1 and done[0]["finish"] == "completed"
    # awaiting_ms 单列 + duration 扣除（wall > duration ≥ 0——等待段不入系统时长）
    async with get_session_factory()() as session:
        detail = await get_turn_detail(session, str(_TURN_ID), user_id=_USER_ID)
    assert detail.awaiting_ms is not None and detail.awaiting_ms >= 0
    wall_ms = int(
        (row["finished_at"] - row["created_at"]).total_seconds() * 1000
    )
    assert row["duration_ms"] is not None
    assert row["duration_ms"] < wall_ms  # duration_ms 扣除 awaiting 时长
    assert done[0]["duration_ms"] == row["duration_ms"]


async def test_two_phase_web_only_resume(two_phase_env: dict[str, Any]) -> None:
    """web_only：阶段 2 web Send×N（现状形态零 kb Send）、门内候选不注入。"""
    info = two_phase_env["info"]
    scheduler = two_phase_env["scheduler"]
    await _run_phase1(scheduler, info)
    await scheduler.decide_approval(
        str(_TURN_ID), user_id=_USER_ID, choice="web_only", note=None
    )
    await scheduler.resume_turn(str(_TURN_ID))

    row = _turn_row(info)
    assert row is not None and row["status"] == "completed"
    recorder = two_phase_env["recorder"]
    assert recorder.web_calls == 1 and recorder.kb_calls == 0
    assert recorder.merged_candidates == []  # KB 候选丢弃（web 直达不注入）


async def test_two_phase_both_resume_hybrid(two_phase_env: dict[str, Any]) -> None:
    """both：阶段 2 与常规形态同构（web+kb 双 Send——api.md §7.4:1077 零 kb Send
    仅 kb_only 形态）。"""
    info = two_phase_env["info"]
    scheduler = two_phase_env["scheduler"]
    await _run_phase1(scheduler, info)
    await scheduler.decide_approval(
        str(_TURN_ID), user_id=_USER_ID, choice="both", note=None
    )
    await scheduler.resume_turn(str(_TURN_ID))

    row = _turn_row(info)
    assert row is not None and row["status"] == "completed"
    recorder = two_phase_env["recorder"]
    assert recorder.web_calls == 1 and recorder.kb_calls == 1


# ---- 超时 hybrid 收口 → 阶段 2 照常 ----


async def test_timeout_sweep_collects_then_resume_hybrid(
    two_phase_env: dict[str, Any],
) -> None:
    """超时收口（on_timeout=hybrid 缺省）：expires_at 过期 → 扫描收口
    choice=both/decided_by=timeout/approval expired → resume 完成 both 形态。"""
    import asyncio as _asyncio

    info = two_phase_env["info"]
    scheduler = two_phase_env["scheduler"]
    await _run_phase1(scheduler, info)
    # 人为把等待窗拨到已过期（真库快路径）
    conn = _connect(info)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE turn_approvals SET expires_at = %s WHERE turn_id = %s",
                (datetime.now(UTC) - timedelta(seconds=1), str(_TURN_ID)),
            )
        conn.commit()
    finally:
        conn.close()

    collected = await scheduler.collect_expired_approvals(
        now=datetime.now(UTC) + timedelta(minutes=2)
    )
    assert collected == [
        {
            "turn_id": str(_TURN_ID),
            "thread_id": str(_THREAD_ID),
            "choice": "both",
            "decided_by": "timeout",
            "on_timeout": "hybrid",
        }
    ]
    approval = _approval(info)
    assert approval is not None
    assert approval["status"] == "expired" and approval["decided_by"] == "timeout"
    resolved = [p for t, p in _events(info) if t == "approval_resolved"]
    assert resolved and resolved[0]["resolved_by"] == "timeout"
    await _asyncio.sleep(0)
    await scheduler.resume_turn(str(_TURN_ID))

    row = _turn_row(info)
    assert row is not None and row["status"] == "completed"
    recorder = two_phase_env["recorder"]
    assert recorder.web_calls == 1 and recorder.kb_calls == 1  # both=现状 hybrid 形态


# ---- awaiting 取消：直接终态 + 审批行 canceled + 两帧齐发 ----


async def test_cancel_awaiting_direct_finalize(two_phase_env: dict[str, Any]) -> None:
    """§2.7 v1.37：awaiting 取消=直接落库收尾 canceled(user)（无协程不登记标志）+
    审批行 canceled + approval_resolved(resolved_by=cancel) → done(canceled) 关通道。"""
    info = two_phase_env["info"]
    scheduler = two_phase_env["scheduler"]
    await _run_phase1(scheduler, info)

    result = await scheduler.cancel_turn(str(_TURN_ID), user_id=_USER_ID)
    assert result == {"turn_id": str(_TURN_ID), "status": "canceling"}

    row = _turn_row(info)
    assert row is not None and row["status"] == "canceled"
    assert row["cancel_reason"] == "user"
    approval = _approval(info)
    assert approval is not None
    assert approval["status"] == "canceled" and approval["decided_by"] == "system"
    events = _events(info)
    resolved = [p for t, p in events if t == "approval_resolved"]
    assert resolved and resolved[0]["resolved_by"] == "cancel"
    assert "choice" not in resolved[0]  # cancel 收口无 choice 键（api.md §7.4:1083）
    done = [p for t, p in events if t == "done"]
    assert done and done[0]["finish"] == "canceled" and done[0]["cancel_reason"] == "user"


# ---- 多标签页竞态：条件 UPDATE 先到先得 + 幂等后再触发零副作用 ----


async def test_multi_tab_race_and_resume_idempotent(
    two_phase_env: dict[str, Any],
) -> None:
    """R37 竞态面：标签页 A 裁决 kb_only 赢→阶段 2 完成；标签页 B 后到裁决（异
    choice）→409008；重复 resume → 零副作用（单 done）；非 awaiting 再裁决→409008。"""
    info = two_phase_env["info"]
    scheduler = two_phase_env["scheduler"]
    await _run_phase1(scheduler, info)
    await scheduler.decide_approval(
        str(_TURN_ID), user_id=_USER_ID, choice="kb_only", note=None
    )
    with pytest.raises(ApprovalConflictException):
        await scheduler.decide_approval(
            str(_TURN_ID), user_id=_USER_ID, choice="web_only", note=None
        )
    await scheduler.resume_turn(str(_TURN_ID))
    # 重复 resume 零副作用（单 done 不变量）
    assert await scheduler.resume_turn(str(_TURN_ID)) is None
    assert len([t for t, _ in _events(info) if t == "done"]) == 1
    # 完成后同 choice 再提交=幂等 200（多标签页收敛，api.md §2.13 幂等条）
    repeat = await scheduler.decide_approval(
        str(_TURN_ID), user_id=_USER_ID, choice="kb_only", note=None
    )
    assert repeat["idempotent"] is True
    # 异 choice 再提交=409008
    with pytest.raises(ApprovalConflictException):
        await scheduler.decide_approval(
            str(_TURN_ID), user_id=_USER_ID, choice="web_only", note=None
        )


# ---- R30：gate_payload 版本不符 → 轮 failed + 明确 error ----


async def test_resume_schema_version_mismatch_fails_turn(
    two_phase_env: dict[str, Any],
) -> None:
    """R30/data-model §1.20：读不懂的 gate_payload 版本 → 该轮 failed + 明确
    error（不猜）+ error 事件落库（无 done——零内容直接关闭）。"""
    info = two_phase_env["info"]
    scheduler = two_phase_env["scheduler"]
    await _run_phase1(scheduler, info)
    conn = _connect(info)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE turn_approvals SET gate_payload = %s::jsonb WHERE turn_id = %s",
                ('{"schema_version": 99, "rewritten_queries": ["q"]}', str(_TURN_ID)),
            )
        conn.commit()
    finally:
        conn.close()
    await scheduler.decide_approval(
        str(_TURN_ID), user_id=_USER_ID, choice="kb_only", note=None
    )
    await scheduler.resume_turn(str(_TURN_ID))

    row = _turn_row(info)
    assert row is not None and row["status"] == "failed"
    assert row["error"] is not None and "版本" in row["error"]["message"]
    events = _events(info)
    assert [p for t, p in events if t == "error"]
    assert not [t for t, _ in events if t == "done"]
    # 评审 medium#2：failed 终态同样扣除 awaiting 时长（api.md §2.5 扣除口径不限
    # completed——R30 轮在阶段 2 启动前 failed，等待段不入系统时长）
    assert row["finished_at"] is not None and row["duration_ms"] is not None
    wall_ms = int((row["finished_at"] - row["created_at"]).total_seconds() * 1000)
    assert row["duration_ms"] < wall_ms


# ---- 孤儿清理豁免 awaiting（running/queued 照清）----


async def test_orphan_cleanup_exempts_awaiting(two_phase_env: dict[str, Any]) -> None:
    """data-model §4 v1.23：启动钩子孤儿清理 running/queued→failed、awaiting 豁免
    （合法挂起态——由审批超时扫描器收口）。"""
    assert _ORPHAN_TURN_STATUSES == ("running", "queued")  # 常量守卫（豁免=不加法）
    info = two_phase_env["info"]
    scheduler = two_phase_env["scheduler"]
    await _run_phase1(scheduler, info)
    # 对照组：再播一条 running 孤儿
    other_id = uuid.uuid4()
    conn = _connect(info)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO turns (id, thread_id, user_id, mode, status, query, "
                "rewritten_queries, chat_model_key, model_name, timing, usage, query_lang) "
                "VALUES (%s, %s, %s, 'fast', 'running', 'q', '[]'::jsonb, 'qwen', "
                "'chat-standard', '{}'::jsonb, '{}'::jsonb, 'zh')",
                (str(other_id), str(_THREAD_ID), _USER_ID),
            )
        conn.commit()
    finally:
        conn.close()

    await _cleanup_orphan_turns()

    row = _turn_row(info)
    assert row is not None and row["status"] == "awaiting_approval"  # 豁免
    conn = _connect(info)
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT status FROM turns WHERE id = %s", (str(other_id),))
            assert cursor.fetchone()[0] == "failed"  # 照清
    finally:
        conn.close()


# ---- 占线判定：awaiting 计入 409001 未终态集（api.md §2.5 占线①）----


async def test_awaiting_counts_as_occupied_for_conflict(
    two_phase_env: dict[str, Any],
) -> None:
    """同线程单 in-flight 不变式（api.md §2.5 awaiting 语义①）：awaiting 轮占线——
    reject 策略下新提交 409001（busy 集含 awaiting）。"""
    info = two_phase_env["info"]
    scheduler = two_phase_env["scheduler"]
    await _run_phase1(scheduler, info)
    new_turn_id = uuid.uuid4()
    async with get_session_factory()() as session:
        from app.db.models import Thread
        import sqlalchemy as sa

        await session.execute(
            sa.select(Thread).where(Thread.id == _THREAD_ID).with_for_update()
        )
        from app.exception.conflict import ConflictException

        with pytest.raises(ConflictException):
            await scheduler.resolve_conflict_on_submit(
                session,
                thread_id=str(_THREAD_ID),
                turn_id=str(new_turn_id),
                on_conflict="reject",
            )


# ---- on_timeout=cancel 分支（补裁决尾巴：collect 读 spec 不硬钉 hybrid）----


async def test_timeout_on_timeout_cancel_branch(
    monkeypatch: pytest.MonkeyPatch, migrated_db: Any
) -> None:
    """hitl.on_timeout=cancel（nacos §3 :355 枚举 cancel 分支）：扫描收口=审批行
    canceled（decided_by=timeout）+ 轮收口 canceled + done 帧关通道；resume 零副作用
    （canceled 非 running——扫描 resume 调用对收口轮自然 no-op）。"""
    _seed(migrated_db)
    recorder = _Recorder()
    retriever = _FakeRetriever()
    scheduler = _make_scheduler(
        monkeypatch,
        recorder,
        retriever,
        queries=["知识库 审批门"],
        hitl_overrides={"on_timeout": "cancel"},
    )
    monkeypatch.setattr("app.agent.nodes.kb_gate.rerank_via_gateway", _FakeRerank())
    await _run_phase1(scheduler, migrated_db)
    assert _turn_status(migrated_db) == "awaiting_approval"

    conn = _connect(migrated_db)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE turn_approvals SET expires_at = %s WHERE turn_id = %s",
                (datetime.now(UTC) - timedelta(seconds=1), str(_TURN_ID)),
            )
        conn.commit()
    finally:
        conn.close()

    collected = await scheduler.collect_expired_approvals(
        now=datetime.now(UTC) + timedelta(minutes=2)
    )
    assert len(collected) == 1
    assert collected[0]["turn_id"] == str(_TURN_ID)
    assert collected[0]["on_timeout"] == "cancel"

    row = _turn_row(migrated_db)
    assert row is not None and row["status"] == "canceled"
    # cancel_reason=timeout（api.md §2.5 v1.38 第 4 值——审批等待超时静默收口，
    # nacos §3 hitl.on_timeout=cancel「静默收尾 canceled(timeout)」的执行面）
    assert row["cancel_reason"] == "timeout"
    approval = _approval(migrated_db)
    assert approval is not None
    assert approval["status"] == "canceled" and approval["decided_by"] == "timeout"
    events = _events(migrated_db)
    resolved = [p for t, p in events if t == "approval_resolved"]
    assert resolved and resolved[0]["decided_by"] == "timeout"
    assert resolved[0]["resolved_by"] == "timeout"
    assert "choice" not in resolved[0]  # cancel 收口无 choice 键（api.md §7.4:1083 同款）
    done = [p for t, p in events if t == "done"]
    assert done and done[0]["finish"] == "canceled" and done[0]["cancel_reason"] == "timeout"
    # resume 对已收口轮 no-op（扫描 resume 调用的自然防护）
    assert await scheduler.resume_turn(str(_TURN_ID)) is None


# ---- 评审修复面：high#1 打断 SSE 两帧 / high#2 审计行 / medium#3 usage / #4 timing ----


class _EmptyRetriever:
    """零召回假体（web 直达形态——no_hit 审计行驱动）。"""

    async def bm25_search(self, query: str, *, user_id: Any, k: int) -> list[dict[str, Any]]:
        return []

    async def vector_search(self, query_text: str, *, user_id: Any, k: int) -> list[dict[str, Any]]:
        return []


async def test_interrupt_awaiting_publishes_conflict_sse(
    monkeypatch: pytest.MonkeyPatch, migrated_db: Any
) -> None:
    """评审 high#1/high#5：interrupt 打断 awaiting 轮——请求事务收尾（canceled
    superseded+审批行 canceled）+ 提交后 SSE 两帧（approval_resolved
    resolved_by=conflict + done(canceled, cancel_reason=superseded)）经
    schedule_turn 入口 flush 发布；新轮抢占标记登记（裁决 A）。"""
    _seed(migrated_db)
    recorder = _Recorder()
    scheduler = _make_scheduler(
        monkeypatch, recorder, _FakeRetriever(), queries=["知识库 审批门"]
    )
    monkeypatch.setattr("app.agent.nodes.kb_gate.rerank_via_gateway", _FakeRerank())
    await _run_phase1(scheduler, migrated_db)

    # 新轮（interrupt 打断者）
    new_turn_id = uuid.uuid4()
    async with get_session_factory()() as session:
        from app.db.models import Turn

        session.add(
            Turn(
                id=new_turn_id,
                thread_id=_THREAD_ID,
                user_id=uuid.UUID(_USER_ID),
                mode="fast",
                status="pending",
                query="打断性问题",
                chat_model_key="qwen",
                model_name="chat-standard",
            )
        )
        await session.commit()

    import sqlalchemy as sa

    async with get_session_factory()() as session:
        from app.db.models import Thread

        await session.execute(
            sa.select(Thread).where(Thread.id == _THREAD_ID).with_for_update()
        )
        decision = await scheduler.resolve_conflict_on_submit(
            session,
            thread_id=str(_THREAD_ID),
            turn_id=str(new_turn_id),
            on_conflict="interrupt",
        )
        from app.services.turn_service import SubmitDecision

        assert decision is SubmitDecision.INTERRUPT
        await session.commit()

    # 事务后：awaiting 轮已收尾（canceled superseded）+审批行 canceled——SSE 未发
    row = _turn_row(migrated_db)
    assert row is not None and row["status"] == "canceled"
    assert row["cancel_reason"] == "superseded"
    approval = _approval(migrated_db)
    assert approval is not None and approval["status"] == "canceled"
    assert not [p for t, p in _events(migrated_db) if t in ("approval_resolved", "done")]

    # schedule_turn 入口 flush（POST BackgroundTask 同窗）→ 两帧落库
    await scheduler._flush_deferred_awaiting_sse()
    events = _events(migrated_db)
    resolved = [p for t, p in events if t == "approval_resolved"]
    assert resolved and resolved[0]["resolved_by"] == "conflict"
    assert resolved[0]["decided_by"] == "system"
    done = [p for t, p in events if t == "done"]
    assert done and done[0]["finish"] == "canceled"
    assert done[0]["cancel_reason"] == "superseded"


async def test_web_direct_and_shadow_audit_rows(
    monkeypatch: pytest.MonkeyPatch, migrated_db: Any
) -> None:
    """评审 high#2：no_hit（web 直达审计行——R28 事后追查依据）与 shadow（观测态
    行——T 标定输入）两类审批行落库；2.5 投影 no_hit 形态可达。"""
    _seed(migrated_db)
    recorder = _Recorder()
    scheduler = _make_scheduler(
        monkeypatch, recorder, _EmptyRetriever(), queries=["知识库 审批门"]
    )
    monkeypatch.setattr("app.agent.nodes.kb_gate.rerank_via_gateway", _FakeRerank())
    await _run_phase1(scheduler, migrated_db)
    # web 直达：图一次跑完（turn completed）+ 审计行 no_hit 轻量无载荷
    assert _turn_status(migrated_db) == "completed"
    approval = _approval(migrated_db)
    assert approval is not None
    assert approval["status"] == "no_hit" and approval["verdict"] == "no_hit"
    assert approval["gate_payload"] is None
    async with get_session_factory()() as session:
        detail = await get_turn_detail(session, str(_TURN_ID), user_id=_USER_ID)
    assert detail.approval == {"status": "no_hit", "verdict": "no_hit"}

    # shadow 形态：行为现状 hybrid（web+kb 双 Send）+ shadow 审计行（T 标定输入）
    hit_retriever = _FakeRetriever()  # 默认 _rows()=命中行（逐调用交替通道）

    _seed2_thread = uuid.uuid4()
    conn = _connect(migrated_db)
    turn2 = uuid.uuid4()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO threads (id, user_id, title, mode_summary, turn_count) "
                "VALUES (%s, %s, %s, %s, 1)",
                (str(_seed2_thread), _USER_ID, "shadow 测试线程", "fast"),
            )
            cursor.execute(
                "INSERT INTO turns (id, thread_id, user_id, mode, status, query, "
                "rewritten_queries, chat_model_key, model_name, timing, usage, "
                "query_lang, started_at) "
                "VALUES (%s, %s, %s, 'fast', 'running', 'q', %s::jsonb, 'qwen', "
                "'chat-standard', '{}'::jsonb, '{}'::jsonb, 'zh', %s)",
                (
                    str(turn2),
                    str(_seed2_thread),
                    _USER_ID,
                    '["知识库 审批门"]',
                    datetime.now(UTC),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    scheduler2 = _make_scheduler(
        monkeypatch,
        _Recorder(),
        hit_retriever,
        queries=["知识库 审批门"],
        hitl_overrides={"shadow": True},
    )
    monkeypatch.setattr("app.agent.nodes.kb_gate.rerank_via_gateway", _FakeRerank())
    async with get_session_factory()() as session:
        from app.db.models import Turn

        row2 = await session.get(Turn, turn2)
        assert row2 is not None
        await scheduler2._execute_turn(row2)
    assert _turn_status(migrated_db) == "completed"  # shadow 行为现状 hybrid
    conn = _connect(migrated_db)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT status, verdict, previews FROM turn_approvals WHERE turn_id = %s",
                (str(turn2),),
            )
            shadow_row = cursor.fetchone()
    finally:
        conn.close()
    assert shadow_row[0] == "shadow" and shadow_row[1] == "has_hit"
    assert shadow_row[2]  # previews 落库（T 标定/弹窗率观测输入）


async def test_usage_restored_across_phases(
    two_phase_env: dict[str, Any],
) -> None:
    """评审 medium#3：阶段 1 usage 快照（awaiting 预写 turns.usage）→ 阶段 2
    restore_totals 回灌 → done.usage/§2.5 usage 承载两阶段合计（桩节点零 LLM——
    以人为预写快照驱动回灌路径，restore 纯注入语义由本用例端到端锁定）。"""
    info = two_phase_env["info"]
    scheduler = two_phase_env["scheduler"]
    await _run_phase1(scheduler, info)
    conn = _connect(info)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE turns SET usage = %s::jsonb WHERE id = %s",
                ('{"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130}', str(_TURN_ID)),
            )
        conn.commit()
    finally:
        conn.close()
    await scheduler.decide_approval(
        str(_TURN_ID), user_id=_USER_ID, choice="kb_only", note=None
    )
    await scheduler.resume_turn(str(_TURN_ID))
    row = _turn_row(info)
    assert row is not None and row["status"] == "completed"
    usage = row["usage"]
    assert usage.get("total_tokens") == 130  # 阶段 1 快照回灌后保留
    events = _events(info)
    done = [p for t, p in events if t == "done"]
    assert done[0]["usage"].get("total_tokens") == 130


async def test_two_phase_timing_merge_preserves_rewrite_segment(
    two_phase_env: dict[str, Any],
) -> None:
    """评审 medium#4(b)：带审批轮终态 timing.stages 保留阶段 1 段（rewrite+判定帧
    extra.hitl——gate_ms 观测面）追加阶段 2 段，回看时间线与非门轮一致；
    medium#4(a)：kb_only search 段由恢复入口 running/done 配对合档。"""
    from test.integration.conftest import await_until

    info = two_phase_env["info"]
    scheduler = two_phase_env["scheduler"]
    await _run_phase1(scheduler, info)
    await scheduler.decide_approval(
        str(_TURN_ID), user_id=_USER_ID, choice="kb_only", note=None
    )
    await scheduler.resume_turn(str(_TURN_ID))
    await await_until(
        lambda: _turn_status(info) == "completed", description="阶段 2 完成"
    )
    async with get_session_factory()() as session:
        detail = await get_turn_detail(session, str(_TURN_ID), user_id=_USER_ID)
    stage_keys = [s.stage_key for s in detail.stages]
    assert stage_keys == ["rewrite", "search"]  # 阶段 1 保留 + 阶段 2 search 合档追加
    # 生产形态（R2 修复验证）：rewrite 桩自帧先合档（extra.strategy）→ 门节点第二帧
    # 被丢弃 → TurnRunner 补写 extra.hitl 进已合档条目——生产时序下 gate_ms 观测面
    # 可达且零重复条目（_absorb_stage 丢弃路径被 TurnRunner 侧补写覆盖）
    rewrite_stage = detail.stages[0]
    assert rewrite_stage.extra is not None
    assert rewrite_stage.extra.get("strategy") == "web"  # 桩自帧键保留（浅合并）
    hitl_extra = rewrite_stage.extra.get("hitl")
    assert hitl_extra is not None
    assert hitl_extra["verdict"] == "has_hit"
    assert hitl_extra["gate"] == "triggered"
    assert isinstance(hitl_extra["gate_ms"], int)  # 门段延迟观测面（2.5 可达）
    assert stage_keys.count("rewrite") == 1  # 零重复条目


# ---- 评审第 1 轮修复面：F1/F4 note 接线 / F3 cancel-flag 竞态 drain / F2 duration 扣除 ----


async def test_two_phase_note_reaches_synthesis_state(
    two_phase_env: dict[str, Any],
) -> None:
    """评审 high#1/medium#4（api.md §2.13「服务端只作用户补充约束进 synthesis
    用户角色」）：decide(note) → resume 载荷携带 note → 阶段 2 initial_state
    approval_note → synthesis 节点 state 可读（桩图捕获断言）；note sanitize
    归 decide_approval（R35，上一段已测）。"""
    info = two_phase_env["info"]
    scheduler = two_phase_env["scheduler"]
    await _run_phase1(scheduler, info)

    decision = await scheduler.decide_approval(
        str(_TURN_ID), user_id=_USER_ID, choice="custom", note="聚焦 RAG 检索面"
    )
    assert decision["idempotent"] is False
    await scheduler.resume_turn(str(_TURN_ID))

    row = _turn_row(info)
    assert row is not None and row["status"] == "completed"
    # 桩 synthesis 捕获的 state.approval_note（TurnRunner→图 state 注入面）
    assert two_phase_env["recorder"].approval_notes == ["聚焦 RAG 检索面"]


async def test_cancel_flag_race_drained_at_awaiting(
    monkeypatch: pytest.MonkeyPatch, two_phase_env: dict[str, Any]
) -> None:
    """评审 medium#3：cancel-flag 竞态收口——_finish_awaiting 首次 pop 与
    open_approval 提交之间到达的 cancel（轮仍 running→登记标志路径）在 awaiting
    后无人消费、resume 后被误消费收口已裁决轮。修复=open_approval 提交后二次
    drain：有标志→按 §2.7 awaiting 分支同语义直接收口（审批行 canceled+轮
    canceled(cancel_reason=标志原因)+两帧，且**零 approval_requested**——挂起
    从未对用户生效）。"""
    info = two_phase_env["info"]
    scheduler = two_phase_env["scheduler"]
    # 竞态注入：open_approval 调用瞬间登记取消标志（模拟首次 pop 后、提交窗内到达）
    original_open = scheduler.open_approval

    async def _racing_open(*args: Any, **kwargs: Any) -> dict[str, Any]:
        scheduler._cancel_flags[str(_TURN_ID)] = "user"
        return await original_open(*args, **kwargs)

    monkeypatch.setattr(scheduler, "open_approval", _racing_open)

    await _run_phase1(scheduler, info)

    row = _turn_row(info)
    assert row is not None and row["status"] == "canceled"
    assert row["cancel_reason"] == "user"
    approval = _approval(info)
    assert approval is not None and approval["status"] == "canceled"
    events = _events(info)
    resolved = [p for t, p in events if t == "approval_resolved"]
    assert resolved and resolved[0]["resolved_by"] == "cancel"
    done = [p for t, p in events if t == "done"]
    assert done and done[0]["finish"] == "canceled" and done[0]["cancel_reason"] == "user"
    # 挂起从未对用户生效：零 approval_requested（drain 先于快照预写与事件发布）
    assert not [p for t, p in events if t == "approval_requested"]


async def test_cancel_after_decide_deducts_awaiting(
    two_phase_env: dict[str, Any],
) -> None:
    """评审 medium#2（canceled 面）：裁决后、阶段 2 启动前登记的取消标志在
    resume 入口 pop→canceled 收尾——duration_ms 同样扣除 awaiting 时长
    （api.md §2.5 扣除口径不限 completed）。"""
    info = two_phase_env["info"]
    scheduler = two_phase_env["scheduler"]
    await _run_phase1(scheduler, info)
    await scheduler.decide_approval(
        str(_TURN_ID), user_id=_USER_ID, choice="kb_only", note=None
    )
    # 裁决后、resume 前到达的取消（轮 running→登记标志路径）
    scheduler._cancel_flags[str(_TURN_ID)] = "user"
    await scheduler.resume_turn(str(_TURN_ID))

    row = _turn_row(info)
    assert row is not None and row["status"] == "canceled"
    assert row["cancel_reason"] == "user"
    assert row["finished_at"] is not None and row["duration_ms"] is not None
    wall_ms = int((row["finished_at"] - row["created_at"]).total_seconds() * 1000)
    assert row["duration_ms"] < wall_ms


async def test_cancel_mid_stream_boundary_deducts_awaiting(
    monkeypatch: pytest.MonkeyPatch, two_phase_env: dict[str, Any]
) -> None:
    """评审第 2 轮 F2 残余（边界取消收尾面）：阶段 2 流内（astream 事件边界）取消
    ——_execute_turn 循环边界 break 后的 _finish_canceled（:3855-3862 一带）同样
    扣除 awaiting 时长（api.md §2.5 扣除口径不限 completed；带审批轮最常见取消
    路径）。驱动=包装 _publish_and_persist：首个阶段 2 事件到达时登记取消标志
    （下一事件边界即命中 break）。"""
    info = two_phase_env["info"]
    scheduler = two_phase_env["scheduler"]
    await _run_phase1(scheduler, info)
    await scheduler.decide_approval(
        str(_TURN_ID), user_id=_USER_ID, choice="kb_only", note=None
    )
    original_pub = scheduler._publish_and_persist

    async def _cancel_on_first_stage(ctx: Any, event_type: str, business: Any, **kw: Any) -> Any:
        if event_type == "stage" and scheduler._cancel_flags.get(str(_TURN_ID)) is None:
            # 流内到达的取消（阶段 2 已开跑）——首个事件边界触发登记
            scheduler._cancel_flags[str(_TURN_ID)] = "user"
        return await original_pub(ctx, event_type, business, **kw)

    monkeypatch.setattr(scheduler, "_publish_and_persist", _cancel_on_first_stage)
    await scheduler.resume_turn(str(_TURN_ID))

    row = _turn_row(info)
    assert row is not None and row["status"] == "canceled"
    assert row["cancel_reason"] == "user"
    assert row["finished_at"] is not None and row["duration_ms"] is not None
    wall_ms = int((row["finished_at"] - row["created_at"]).total_seconds() * 1000)
    assert row["duration_ms"] < wall_ms


async def test_cancel_signal_mid_stream_deducts_awaiting(
    two_phase_env: dict[str, Any],
) -> None:
    """评审第 2 轮 F2 残余（TurnCanceledSignal 收尾面）：阶段 2 节点边界感知上抛
    （guards ensure_not_canceled 形态——stub synthesis 按登记臂 Raise）→
    except TurnCanceledSignal 的 _finish_canceled（:3810-3817 一带）同样扣除
    awaiting 时长。"""
    info = two_phase_env["info"]
    scheduler = two_phase_env["scheduler"]
    await _run_phase1(scheduler, info)
    await scheduler.decide_approval(
        str(_TURN_ID), user_id=_USER_ID, choice="kb_only", note=None
    )
    two_phase_env["recorder"].cancel_at_synthesis = True
    await scheduler.resume_turn(str(_TURN_ID))

    row = _turn_row(info)
    assert row is not None and row["status"] == "canceled"
    assert row["cancel_reason"] == "user"
    assert row["finished_at"] is not None and row["duration_ms"] is not None
    wall_ms = int((row["finished_at"] - row["created_at"]).total_seconds() * 1000)
    assert row["duration_ms"] < wall_ms
