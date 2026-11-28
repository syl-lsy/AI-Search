"""integration 层：B22 HITL 审批服务事务面（真库 requires_db；批次 2 第一段）。

覆盖点（契约锚点见各用例 docstring）：
- open_approval：turns 置 awaiting_approval 与审批行 pending 写入**同事务**
  （data-model.md §1.20 实施时序注 :650）；
- decide_approval：条件 UPDATE（`WHERE status='awaiting_approval'`）先到先得
  （api.md §2.13；沿 regenerate rowcount 守卫先例）——裁决竞态两形态：
  ① 重复裁决（同 choice 幂等 / 异 choice 409008）；② 超时与裁决竞态（扫描
  收口先行 → 人为裁决 409008「超时后再到」）；
- collect_expired_approvals：惰性过期判定（on_timeout=hybrid 缺省 → choice=both、
  decided_by=timeout，api.md §7.4:1082；审批行 status='expired'，data-model.md
  §1.20 status 注）；只收口到期行，未到期/已收口零触碰。
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg2
import pytest

from app.core.config import common_settings
from app.db.session import get_session_factory
from app.exception import BadRequestException, NotFoundException
from app.exception.conflict import ApprovalConflictException
from app.services.event_bus import EventBus
from app.services.turn_service import ThreadScheduler

pytestmark = pytest.mark.requires_db

_USER_ID = common_settings.default_user_id
_THREAD_ID = uuid.uuid4()
_TURN_ID = uuid.uuid4()


def _connect(info: Any):
    return psycopg2.connect(
        host=info.host,
        port=int(info.port),
        user=info.user,
        password=info.password,
        dbname=info.name,
        connect_timeout=5,
    )


def _seed(info: Any, *, status: str = "running") -> None:
    """插入 thread+turn（参数绑定，零 SQL 拼接）；无 sources/events（审批面不需要）。"""
    conn = _connect(info)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO threads (id, user_id, title, mode_summary, turn_count) "
                "VALUES (%s, %s, %s, %s, 1)",
                (str(_THREAD_ID), _USER_ID, "HITL 审批测试线程", "fast"),
            )
            cursor.execute(
                "INSERT INTO turns (id, thread_id, user_id, mode, status, query, "
                "rewritten_queries, chat_model_key, model_name, timing, usage, query_lang) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb, %s::jsonb, %s)",
                (
                    str(_TURN_ID),
                    str(_THREAD_ID),
                    _USER_ID,
                    "fast",
                    status,
                    "知识库里有没有 RRF 融合的资料？",
                    '["知识库 RRF 融合检索"]',
                    "qwen",
                    "chat-standard",
                    "{}",
                    "{}",
                    "zh",
                ),
            )
        conn.commit()
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


def _approval_row(info: Any) -> dict[str, Any] | None:
    conn = _connect(info)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT status, verdict, choice, decided_by, gate_payload, "
                "       hits_summary, previews, responded_at IS NOT NULL "
                "FROM turn_approvals WHERE turn_id = %s",
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
                "hits_summary",
                "previews",
                "responded",
            )
            return dict(zip(keys, row))
    finally:
        conn.close()


def _scheduler() -> ThreadScheduler:
    return ThreadScheduler(bus=EventBus(), session_factory=get_session_factory())


@pytest.fixture
def awaiting_candidate(migrated_db) -> Any:
    """running 态轮（可进审批门）；用后随 truncate CASCADE 清理。"""
    _seed(migrated_db, status="running")
    yield migrated_db


# ---- open_approval：同事务开行（§1.20 :650 实施时序注）----


async def test_open_approval_marks_awaiting_and_creates_pending_row(
    awaiting_candidate: Any,
) -> None:
    """开行 = turns→awaiting_approval + 审批行 pending 一事务两写（§1.20 :650）。"""
    now = datetime.now(UTC)
    result = await _scheduler().open_approval(
        str(_TURN_ID),
        user_id=_USER_ID,
        verdict="has_hit",
        hits_summary=[{"query_index": 0, "top_k_count": 2, "top1_score": 0.51}],
        previews=[
            {
                "kb_doc_id": "d1",
                "chunk_id": "c1",
                "title": "检索融合",
                "heading_path": ["KB", "RRF"],
                "excerpt": "RRF 融合……",
                "query_index": 0,
            }
        ],
        gate_payload={
            "schema_version": 1,
            "rewritten_queries": ["知识库 RRF 融合检索"],
            "query_type": "factoid",
            "time_range": None,
            "query_lang": "zh",
            "kb_candidates": [],
        },
        expires_at=now + timedelta(seconds=120),
        requested_at=now,
    )
    assert result["turn_status"] == "awaiting_approval"
    assert _turn_status(awaiting_candidate) == "awaiting_approval"
    row = _approval_row(awaiting_candidate)
    assert row is not None
    assert row["status"] == "pending"
    assert row["verdict"] == "has_hit"
    assert row["choice"] is None and row["decided_by"] is None
    assert row["gate_payload"]["schema_version"] == 1
    assert row["responded"] is False


async def test_open_approval_rejects_non_running_turn(migrated_db: Any) -> None:
    """开行前置=turn running（阶段 1 图执行中的内部不变式）；violation 即拒。"""
    _seed(migrated_db, status="completed")
    with pytest.raises(RuntimeError, match="running"):
        await _scheduler().open_approval(
            str(_TURN_ID),
            user_id=_USER_ID,
            verdict="has_hit",
            hits_summary=[],
            previews=[],
            gate_payload={"schema_version": 1},
            expires_at=datetime.now(UTC) + timedelta(seconds=60),
        )


# ---- decide_approval：条件 UPDATE 先到先得（api.md §2.13）----


async def test_decide_first_submission_wins(awaiting_candidate: Any) -> None:
    """首次裁决：turn→running、审批行 approved/decided_by=user/responded_at 落定。"""
    _seed_open(awaiting_candidate)
    result = await _scheduler().decide_approval(
        str(_TURN_ID), user_id=_USER_ID, choice="kb_only", note=None
    )
    assert result == {
        "turn_id": str(_TURN_ID),
        "status": "running",
        "choice": "kb_only",
        "idempotent": False,
    }
    assert _turn_status(awaiting_candidate) == "running"
    row = _approval_row(awaiting_candidate)
    assert row is not None
    assert row["status"] == "approved"
    assert row["choice"] == "kb_only"
    assert row["decided_by"] == "user"
    assert row["responded"] is True


async def test_decide_same_choice_idempotent(awaiting_candidate: Any) -> None:
    """幂等（api.md §2.13）：同轮同 choice 重复提交 → 已记录裁决、零重复落库。"""
    _seed_open(awaiting_candidate)
    scheduler = _scheduler()
    first = await scheduler.decide_approval(
        str(_TURN_ID), user_id=_USER_ID, choice="kb_only", note=None
    )
    first_responded_at = _responded_at(awaiting_candidate)
    second = await scheduler.decide_approval(
        str(_TURN_ID), user_id=_USER_ID, choice="kb_only", note=None
    )
    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert second["choice"] == "kb_only"
    assert _responded_at(awaiting_candidate) == first_responded_at  # 零重复落库


async def test_decide_conflict_on_different_choice(awaiting_candidate: Any) -> None:
    """裁决竞态形态一（重复裁决）：已按 kb_only 裁决后再提 web_only → 409008。"""
    _seed_open(awaiting_candidate)
    scheduler = _scheduler()
    await scheduler.decide_approval(
        str(_TURN_ID), user_id=_USER_ID, choice="kb_only", note=None
    )
    with pytest.raises(ApprovalConflictException):
        await scheduler.decide_approval(
            str(_TURN_ID), user_id=_USER_ID, choice="web_only", note=None
        )


async def test_decide_conflict_on_non_awaiting_turn(awaiting_candidate: Any) -> None:
    """非 awaiting 轮调用审批 → 409008（无审批行的 running 轮 / 终态轮同码）。"""
    # awaiting_candidate fixture 已播种 running 轮（无审批行）
    scheduler = _scheduler()
    with pytest.raises(ApprovalConflictException):
        await scheduler.decide_approval(
            str(_TURN_ID), user_id=_USER_ID, choice="both", note=None
        )
    # 终态轮（completed）同码——非 awaiting 域
    _reseed_status(awaiting_candidate, "completed")
    with pytest.raises(ApprovalConflictException):
        await scheduler.decide_approval(
            str(_TURN_ID), user_id=_USER_ID, choice="both", note=None
        )


async def test_decide_unknown_turn_404001(awaiting_candidate: Any) -> None:
    """归属校验（id+user 双条件）：未知轮 → 404001（不泄漏存在性）。"""
    with pytest.raises(NotFoundException):
        await _scheduler().decide_approval(
            str(uuid.uuid4()), user_id=_USER_ID, choice="both", note=None
        )


async def test_decide_illegal_choice_400001(awaiting_candidate: Any) -> None:
    """choice 非法枚举 → 400001（api.md §2.13 错误面；服务层防御性校验）。"""
    _seed_open(awaiting_candidate)
    with pytest.raises(BadRequestException):
        await _scheduler().decide_approval(
            str(_TURN_ID), user_id=_USER_ID, choice="hybrid", note=None
        )


async def test_decide_note_over_limit_400001(awaiting_candidate: Any) -> None:
    """note 超 500 字（note_max_chars 契约上限）→ 400001。"""
    _seed_open(awaiting_candidate)
    with pytest.raises(BadRequestException):
        await _scheduler().decide_approval(
            str(_TURN_ID), user_id=_USER_ID, choice="custom", note="长" * 501
        )


class _FakeSpecSnapshot:
    """spec 快照假体（ThreadScheduler spec_snapshot 缝——payload 直给）。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload


async def test_decide_honors_note_max_chars_configured_key(
    awaiting_candidate: Any,
) -> None:
    """评审遗留④（批次 5 清偿）：hitl.note_max_chars 服务层真实消费——配置 100
    时 150 字 note 拒收 400001（原实现硬编码 500 键不消费）、90 字放行；缺省
    500 行为不变（G3——上一用例 501 字拒收即缺省面回归锚）。"""
    _seed_open(awaiting_candidate)
    scheduler = ThreadScheduler(
        bus=EventBus(),
        session_factory=get_session_factory(),
        spec_snapshot=_FakeSpecSnapshot({"hitl": {"note_max_chars": 100}}),
    )
    with pytest.raises(BadRequestException):
        await scheduler.decide_approval(
            str(_TURN_ID), user_id=_USER_ID, choice="custom", note="长" * 150
        )
    row = _approval_row(awaiting_candidate)
    assert row is not None and row["status"] == "pending"  # 拒收轮零触碰
    await scheduler.decide_approval(
        str(_TURN_ID), user_id=_USER_ID, choice="custom", note="短" * 90
    )
    row = _approval_row(awaiting_candidate)
    assert row is not None and row["status"] == "approved"


# ---- 裁决竞态形态二：超时收口 vs 人为裁决（先到先得）----


async def test_decide_lazily_expires_overdue_pending_row(
    awaiting_candidate: Any,
) -> None:
    """评审遗留⑤（批次 5 清偿）：decide 惰性过期——审批行 pending 且已过
    expires_at 而扫描器未及（30s 窗口）时，人为裁决不得照常推进：approval 端点
    即惰性判定点（approval_timeout_sweep 模块口径「惰性判定为主」），行按
    on_timeout 缺省 hybrid 先行收口（expired/choice=both/decided_by=timeout）+
    轮回 running，裁决落 409008「超时后再到的人为裁决」（api.md §2.13）。"""
    _seed_open(awaiting_candidate, expires_in=-60)  # 已过期 60s（扫描器未跑）
    scheduler = _scheduler()
    with pytest.raises(ApprovalConflictException):
        await scheduler.decide_approval(
            str(_TURN_ID), user_id=_USER_ID, choice="both", note=None
        )
    assert _turn_status(awaiting_candidate) == "running"  # hybrid 缺省：轮续跑
    row = _approval_row(awaiting_candidate)
    assert row is not None
    assert (
        row["status"] == "expired"
        and row["decided_by"] == "timeout"
        and row["choice"] == "both"
    )


async def test_timeout_collect_then_user_decide_conflicts(
    awaiting_candidate: Any,
) -> None:
    """扫描收口先行（choice=both/decided_by=timeout/审批行 expired）→ 用户后到裁决
    （含同 choice）→ 409008「超时后再到的人为裁决」（api.md §2.13）。"""
    _seed_open(awaiting_candidate, expires_in=60)
    collected = await _scheduler().collect_expired_approvals(
        now=datetime.now(UTC) + timedelta(seconds=120)
    )
    assert len(collected) == 1
    assert collected[0]["turn_id"] == str(_TURN_ID)
    assert collected[0]["choice"] == "both"  # on_timeout=hybrid 缺省策略值
    assert collected[0]["decided_by"] == "timeout"
    assert _turn_status(awaiting_candidate) == "running"
    row = _approval_row(awaiting_candidate)
    assert row is not None
    assert row["status"] == "expired" and row["decided_by"] == "timeout"
    with pytest.raises(ApprovalConflictException):
        await _scheduler().decide_approval(
            str(_TURN_ID), user_id=_USER_ID, choice="both", note=None
        )


# ---- collect_expired_approvals：惰性过期判定边界 ----


async def test_collect_only_due_rows(awaiting_candidate: Any) -> None:
    """只收口到期行：未到期零触碰（行仍 pending、turn 仍 awaiting）。"""
    _seed_open(awaiting_candidate, expires_in=60)
    collected = await _scheduler().collect_expired_approvals(now=datetime.now(UTC))
    assert collected == []
    assert _turn_status(awaiting_candidate) == "awaiting_approval"
    row = _approval_row(awaiting_candidate)
    assert row is not None and row["status"] == "pending"


async def test_collect_skips_concurrently_decided(awaiting_candidate: Any) -> None:
    """先到先得（裁决 vs 扫描另一向）：用户裁决先行 → 扫描零收口、行保持 approved。"""
    _seed_open(awaiting_candidate, expires_in=60)
    scheduler = _scheduler()
    await scheduler.decide_approval(
        str(_TURN_ID), user_id=_USER_ID, choice="web_only", note=None
    )
    collected = await scheduler.collect_expired_approvals(
        now=datetime.now(UTC) + timedelta(seconds=120)
    )
    assert collected == []
    row = _approval_row(awaiting_candidate)
    assert row is not None
    assert row["status"] == "approved" and row["choice"] == "web_only"


# ---- helpers ----


def _seed_open(info: Any, *, expires_in: int = 120) -> None:
    """把已播种的 running 轮推进到 awaiting + pending 审批行（直连 DB，单测快路径）。"""
    now = datetime.now(UTC)
    conn = _connect(info)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE turns SET status = 'awaiting_approval' WHERE id = %s",
                (str(_TURN_ID),),
            )
            cursor.execute(
                "INSERT INTO turn_approvals (id, turn_id, verdict, gate_payload, "
                "requested_at, expires_at) "
                "VALUES (%s, %s, %s, %s::jsonb, %s, %s)",
                (
                    str(uuid.uuid4()),
                    str(_TURN_ID),
                    "has_hit",
                    '{"schema_version": 1, "rewritten_queries": ["q"], "kb_candidates": []}',
                    now,
                    now + timedelta(seconds=expires_in),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _reseed_status(info: Any, status: str) -> None:
    """覆盖轮状态（非 awaiting 轮负向用例的直连 DB 快路径）。"""
    conn = _connect(info)
    try:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE turns SET status = %s WHERE id = %s", (status, str(_TURN_ID)))
        conn.commit()
    finally:
        conn.close()


def _responded_at(info: Any) -> Any:
    conn = _connect(info)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT responded_at FROM turn_approvals WHERE turn_id = %s",
                (str(_TURN_ID),),
            )
            return cursor.fetchone()[0]
    finally:
        conn.close()
