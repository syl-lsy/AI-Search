"""unit 层：turn_service（B2）——ThreadScheduler 四策略 + TurnRunner 事件翻译管线。

钉住的语义（冻结契约，逐条对应测试）：决策矩阵全 16 分支（api.md §0 / ADR-010）；
409001 = reject 遇未终态轮 ∨ 任一策略队满（per-thread 深度 1，QUEUE_DEPTH_LIMIT）；
interrupt/rollback 打断非故障（绝不产生 error 事件）；TurnRunner 事件翻译（ARCH §6.1）：
stage/sources/images/done/error 以发布盖章的同一 seq 落 turn_events（payload=去 envelope
冗余字段后的业务字段），token/thinking 只发布不落库（ADR-003 第 4 条）；messages 帧按帧
读 .content → token/thinking（R8 口径）；终态先落 turns 再 publish done；失败路径
error(500001/500301) + turns.status=failed + close_channel（api.md §7.2「直接关闭」口径，
修 failed 轮清扫泄漏）；timeout 守卫 spec.turn_timeout_s 包 astream；cancel 两态 +
409002；resolve_chat_model 非法键 400001。
"""

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.config_center.cache import AssetSnapshot
from app.core.config import SseSettings
from app.db.models import Source, Turn, TurnEvent
from app.exception import (
    BadRequestException,
    ConflictException,
    NotFoundException,
    TurnTerminalException,
)
from app.services import turn_service
from app.services.event_bus import EventBus, SseEvent
from app.services.turn_service import (
    FINAL_STATUSES,
    QUEUE_DEPTH_LIMIT,
    SubmitDecision,
    ThreadScheduler,
    decide_on_submit,
    resolve_chat_model,
    seed_seq_from_db,
)


def _bus() -> EventBus:
    """真 EventBus（内存实现零网络）：seq 盖章/缓冲/订阅/清扫走生产行为。"""
    return EventBus(settings=SseSettings(event_ttl_minutes=30, buffer_max_events=20000))


# ---- 假体（零网络；语句对象由被测实现构造后传入，假体只按调用形态返回预设行集）----


class _FakeResult:
    """scalars/execute 结果假体：scalars() 链式返回行集。"""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> "_FakeResult":
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeSession:
    """最小 AsyncSession 假体：get/scalars/execute/add/commit 全记录。"""

    def __init__(self, *, turn: Any = None, rows: list[Any] | None = None,
                 execute_rows: list[Any] | None = None) -> None:
        self.turn = turn
        self.rows = rows or []
        self.execute_rows = execute_rows or []
        self.added: list[Any] = []
        self.commits = 0
        self.executed: list[Any] = []  # execute 收到的语句（FOR UPDATE 守卫断言用）

    async def get(self, model: Any, key: Any, *, with_for_update: bool = False) -> Any:
        return self.turn

    async def scalars(self, statement: Any) -> _FakeResult:
        return _FakeResult(self.rows)

    async def execute(self, statement: Any) -> _FakeResult:
        self.executed.append(statement)
        return _FakeResult(self.execute_rows)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class _FakeSessionFactory:
    """短事务假工厂：每次 async with 返回同一假会话（跨事务累积写入记录）。"""

    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def __call__(self) -> _FakeSession:
        return self._session


class _TurnRow:
    """最小 turns 行假体：暴露 TurnRunner 读写触碰的字段。"""

    def __init__(self, *, status: str = "running", mode: str = "fast",
                 thread_id: str = "0b1f3a2e-0000-4000-8000-000000000001",
                 user_id: str = "00000000-0000-4000-8000-000000000001") -> None:
        self.id = uuid.uuid4()
        self.thread_id = thread_id
        self.user_id = user_id
        self.status = status
        self.mode = mode
        self.query = "桩问题"
        self.created_at = datetime.now(UTC)
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.duration_ms: int | None = None
        self.answer_md: str | None = None
        self.error: dict[str, Any] | None = None
        self.cancel_reason: str | None = None
        self.timing: dict[str, Any] = {}
        self.usage: dict[str, Any] = {}


class _FakeGraph:
    """假主图：astream 按脚本产出 (namespace, stream_mode, chunk) 三元组；步间可插 delay。"""

    def __init__(self, steps: list[tuple[str, Any]], *, delay: float = 0.0) -> None:
        self._steps = steps
        self._delay = delay

    def astream(self, payload: Any, config: Any = None, stream_mode: Any = None,
                subgraphs: bool = False) -> Any:
        steps = self._steps
        delay = self._delay

        async def gen():
            for stream, chunk in steps:
                if delay:
                    await asyncio.sleep(delay)
                # updates 一律产顶层 ns（=()）——TurnRunner 仅吸收顶层 updates 最终 state
                namespace = () if stream == "updates" else (("fast:run",),)
                yield (namespace, stream, chunk)

        return gen()


class _BoomGraph(_FakeGraph):
    """异常图：astream 生成器抛 RuntimeError（失败路径用例）。"""

    def __init__(self) -> None:
        super().__init__([])

    def astream(self, payload: Any, config: Any = None, stream_mode: Any = None,
                subgraphs: bool = False) -> Any:
        async def gen():
            yield (("fast:run",), "custom", {"type": "stage", "stage_key": "rewrite", "status": "running"})
            raise RuntimeError("图运行爆炸")

        return gen()


class _MessageChunk:
    """最小 AIMessageChunk 假体：content + additional_kwargs.reasoning_content。"""

    def __init__(self, content: Any, *, reasoning: str | None = None) -> None:
        self.content = content
        self.additional_kwargs: dict[str, Any] = (
            {"reasoning_content": reasoning} if reasoning else {}
        )


# ---- 公共编排助手 ----

_SPEC_PAYLOAD: dict[str, Any] = {
    "turn": {"default_on_conflict": "reject"},
    "fast": {"turn_timeout_s": 5},
    "deep": {"turn_timeout_s": 8},
}

_MAPPING_PAYLOAD: dict[str, Any] = {
    "chat": {"options": {"qwen": "qwen3.5-4b-ollama", "glm": "glm-4.7"}, "default": "qwen"}
}

_SRC: dict[str, Any] = {
    "source_id": "11111111-1111-4111-8111-111111111111",
    "ref_no": 1,
    "source_type": "web",
    "title": "LangGraph 官方文档",
    "url": "https://langchain-ai.github.io/langgraph/",
    "snippet": "StateGraph 以节点与边编排。",
    "score": 0.91,
    "snapshot_available": False,
    "kb_doc_id": None,
}


def _snapshot(payload: dict[str, Any]) -> AssetSnapshot:
    return AssetSnapshot(
        data_id="ai_search.agent.spec.json",
        version=1,
        md5="m",
        updated_at="t",
        payload=payload,
    )


def _scheduler(bus: EventBus, factory: _FakeSessionFactory, graph: Any = None,
               spec: dict[str, Any] | None = None) -> ThreadScheduler:
    """构造注入假依赖的 ThreadScheduler（快照假体直构 AssetSnapshot）。"""
    return ThreadScheduler(
        bus=bus,
        session_factory=factory,
        graph=graph or _FakeGraph([]),
        spec_snapshot=_snapshot(spec or _SPEC_PAYLOAD),
        mapping_snapshot=_snapshot(_MAPPING_PAYLOAD),
    )


def _custom(payload: dict[str, Any]) -> tuple[str, Any]:
    return ("custom", payload)


def _updates(state: dict[str, "Any"]) -> tuple[str, Any]:
    return ("updates", state)


def _message(content: Any, *, reasoning: str | None = None) -> tuple[str, Any]:
    return ("messages", (_MessageChunk(content, reasoning=reasoning), {"langgraph_node": "synthesis"}))


# ---- 决策矩阵（16 分支穷举）----


@pytest.mark.parametrize(
    ("policy", "has_running", "has_queued", "expected"),
    [
        # reject：存在未终态轮（running/queued）→ 409001；空闲 → proceed
        ("reject", False, False, SubmitDecision.PROCEED),
        ("reject", True, False, SubmitDecision.REJECT),
        ("reject", False, True, SubmitDecision.REJECT),
        ("reject", True, True, SubmitDecision.REJECT),
        # enqueue：有 running 空位 → 入队；排队位已占 → 409001；空闲 → proceed
        ("enqueue", False, False, SubmitDecision.PROCEED),
        ("enqueue", True, False, SubmitDecision.ENQUEUE),
        ("enqueue", False, True, SubmitDecision.REJECT),  # 无 running 有 queued = 队满
        ("enqueue", True, True, SubmitDecision.REJECT),  # 排队位已满
        # interrupt：有 running → 打断；无 running 退化 enqueue 语义（队满 409001）
        ("interrupt", False, False, SubmitDecision.PROCEED),
        ("interrupt", True, False, SubmitDecision.INTERRUPT),
        ("interrupt", False, True, SubmitDecision.REJECT),
        ("interrupt", True, True, SubmitDecision.INTERRUPT),  # 既有 queued 不受影响、保持排队
        # rollback 同构
        ("rollback", False, False, SubmitDecision.PROCEED),
        ("rollback", True, False, SubmitDecision.ROLLBACK),
        ("rollback", False, True, SubmitDecision.REJECT),
        ("rollback", True, True, SubmitDecision.ROLLBACK),
    ],
)
def test_decision_matrix_all_sixteen_branches(
    policy: str, has_running: bool, has_queued: bool, expected: SubmitDecision
) -> None:
    assert decide_on_submit(policy, has_running=has_running, has_queued=has_queued) is expected


def test_queue_depth_limit_is_one() -> None:
    """排队深度上限 1 是代码常量（PRD OQ-20，非配置）。"""
    assert QUEUE_DEPTH_LIMIT == 1


# ---- resolve_conflict_on_submit：409001 / 状态推进 / 打断登记 ----


async def test_resolve_reject_raises_conflict_409001() -> None:
    """reject 策略 + 线程存在 running 轮 → ConflictException（409001）；turn 行随请求回滚。"""
    bus = _bus()
    session = _FakeSession(rows=[_TurnRow(status="running")])
    scheduler = _scheduler(bus, _FakeSessionFactory(session))
    with pytest.raises(ConflictException):
        await scheduler.resolve_conflict_on_submit(
            session, thread_id=str(uuid.uuid4()), turn_id=str(uuid.uuid4()), on_conflict="reject"
        )


async def test_resolve_default_policy_from_snapshot() -> None:
    """on_conflict 缺省取快照 turn.default_on_conflict（请求侧捕获为执行依据、不落库）。"""
    bus = _bus()
    session = _FakeSession(rows=[_TurnRow(status="running")])
    scheduler = _scheduler(bus, _FakeSessionFactory(session))
    with pytest.raises(ConflictException):  # 快照缺省 reject + 线程有 running → 409001
        await scheduler.resolve_conflict_on_submit(
            session, thread_id=str(uuid.uuid4()), turn_id=str(uuid.uuid4()), on_conflict=None
        )


async def test_resolve_proceed_promotes_to_running() -> None:
    """proceed（线程空闲）→ 本轮 pending→running（POST 响应 status=running，api.md §2.1）。"""
    bus = _bus()
    turn = _TurnRow(status="pending")
    session = _FakeSession(turn=turn, rows=[])
    scheduler = _scheduler(bus, _FakeSessionFactory(session))
    decision = await scheduler.resolve_conflict_on_submit(
        session, thread_id=turn.thread_id, turn_id=str(turn.id), on_conflict="enqueue"
    )
    assert decision is SubmitDecision.PROCEED
    assert turn.status == "running"
    assert turn.started_at is not None  # PROCEED 同事务盖章（data-model §1.3：图运行开始）


async def test_resolve_enqueue_sets_queued() -> None:
    """enqueue + 有 running 无 queued → 本轮置 queued（响应 status=queued）。"""
    bus = _bus()
    running = _TurnRow(status="running")
    turn = _TurnRow(status="pending")
    session = _FakeSession(turn=turn, rows=[running])
    scheduler = _scheduler(bus, _FakeSessionFactory(session))
    decision = await scheduler.resolve_conflict_on_submit(
        session, thread_id=turn.thread_id, turn_id=str(turn.id), on_conflict="enqueue"
    )
    assert decision is SubmitDecision.ENQUEUE
    assert turn.status == "queued"


async def test_resolve_interrupt_registers_flag_and_queues_new_turn() -> None:
    """interrupt：新轮置 queued + running 轮登记 superseded 取消标志（协作式打断）。"""
    bus = _bus()
    running = _TurnRow(status="running")
    turn = _TurnRow(status="pending")
    session = _FakeSession(turn=turn, rows=[running])
    scheduler = _scheduler(bus, _FakeSessionFactory(session))
    decision = await scheduler.resolve_conflict_on_submit(
        session, thread_id=turn.thread_id, turn_id=str(turn.id), on_conflict="interrupt"
    )
    assert decision is SubmitDecision.INTERRUPT
    assert turn.status == "queued"
    assert scheduler._cancel_flags[str(running.id)] == "superseded"


async def test_resolve_rollback_registers_rolled_back_flag() -> None:
    """rollback 同构打断：cancel_reason=rolled_back（内容回退语义由历史装配过滤承担）。"""
    bus = _bus()
    running = _TurnRow(status="running")
    turn = _TurnRow(status="pending")
    session = _FakeSession(turn=turn, rows=[running])
    scheduler = _scheduler(bus, _FakeSessionFactory(session))
    decision = await scheduler.resolve_conflict_on_submit(
        session, thread_id=turn.thread_id, turn_id=str(turn.id), on_conflict="rollback"
    )
    assert decision is SubmitDecision.ROLLBACK
    assert turn.status == "queued"
    assert scheduler._cancel_flags[str(running.id)] == "rolled_back"


async def test_resolve_locks_thread_row_before_adjudication() -> None:
    """裁决前 FOR UPDATE 锁线程行（评审 P2）：并发 POST 同线程串行化——
    READ COMMITTED 下无锁会让两个并发 POST 互相看不到对方未提交轮，双双
    PROCEED 击穿「每线程同时一轮」；idx_turns_running 非唯一索引不兜底。"""
    bus = _bus()
    session = _FakeSession(rows=[])
    scheduler = _scheduler(bus, _FakeSessionFactory(session))
    await scheduler.resolve_conflict_on_submit(
        session, thread_id=str(uuid.uuid4()), turn_id=str(uuid.uuid4()), on_conflict="reject"
    )
    assert any("FOR UPDATE" in str(stmt) for stmt in session.executed)


async def test_resolve_interrupt_registers_preempting_turn() -> None:
    """interrupt 登记线程级抢占轮（裁决 A）：新轮插队，既有 queued 顺延——
    次序约束由 _can_start 消费 _preempt_turns 标记实现（rollback 同构）。"""
    bus = _bus()
    running = _TurnRow(status="running")
    turn = _TurnRow(status="pending")
    session = _FakeSession(turn=turn, rows=[running])
    scheduler = _scheduler(bus, _FakeSessionFactory(session))
    await scheduler.resolve_conflict_on_submit(
        session, thread_id=turn.thread_id, turn_id=str(turn.id), on_conflict="interrupt"
    )
    assert scheduler._preempt_turns[turn.thread_id] == str(turn.id)


async def test_can_start_honors_preemption_over_fifo() -> None:
    """裁决 A 次序（评审 P2）：抢占轮 bypass FIFO 插队，既有 queued 轮顺延到
    抢占轮终态之后——标记清除（抢占轮终态）后恢复常规 FIFO。

    假体口径：_FakeSession 不复现 SQL 的自身排除（Turn.id != row.id），rows 按
    调用视角切换（视角 X = 排除 X 后 SQL 可见的其余行）。
    """
    bus = _bus()
    older = _TurnRow(status="queued")
    newer = _TurnRow(status="queued")
    older.created_at = datetime(2026, 11, 1, tzinfo=UTC)
    newer.created_at = datetime(2026, 11, 1, 0, 0, 1, tzinfo=UTC)
    session = _FakeSession(rows=[newer])  # 视角=older
    scheduler = _scheduler(bus, _FakeSessionFactory(session))

    # 无抢占标记：常规 FIFO——更早提交者先行
    assert await scheduler._can_start(older) is True
    session.rows = [older]  # 视角=newer
    assert await scheduler._can_start(newer) is False  # older 更早 → FIFO 阻塞

    # 抢占登记：新轮（newer）插队，既有 queued（older）顺延
    scheduler._preempt_turns[older.thread_id] = str(newer.id)
    assert await scheduler._can_start(newer) is True  # 抢占轮 bypass FIFO
    session.rows = [newer]
    assert await scheduler._can_start(older) is False  # 未终态抢占轮在场 → 顺延

    # 抢占轮终态：标记清除 → 恢复 FIFO
    scheduler._preempt_turns.clear()
    assert await scheduler._can_start(older) is True
    session.rows = [older]
    assert await scheduler._can_start(newer) is False


async def test_can_start_clears_stale_preempt_marker_when_turn_gone() -> None:
    """陈旧抢占标记自愈（评审 P2）：标记在 resolve 内登记（请求事务未提交），若该
    事务随后回滚（如 teardown commit 失败）新轮行消失，唯一清除点 _persist_terminal
    永不到达——_can_start 消费标记时须校验被引用轮仍在场且未终态，失配即清标记并
    按常规 FIFO 放行；否则线程既有 queued 轮被永久阻塞（0.2s 轮询空转至进程重启）。
    """
    bus = _bus()
    older = _TurnRow(status="queued")
    # 视角=older：线程内无其他未终态轮（被引用的抢占轮随事务回滚消失，查不到行）
    session = _FakeSession(rows=[])
    scheduler = _scheduler(bus, _FakeSessionFactory(session))
    scheduler._preempt_turns[older.thread_id] = str(uuid.uuid4())

    assert await scheduler._can_start(older) is True
    assert older.thread_id not in scheduler._preempt_turns  # 陈旧标记已清除


async def test_can_start_clears_stale_preempt_marker_when_turn_terminal() -> None:
    """被引用抢占轮已终态（清除点未跑到/晚于本次查询）同样视为陈旧：按 FIFO 放行。

    也锁定有效性判据取 status（而非「行是否出现在该查询结果里」）——终态行不构成
    启动阻塞，标记不得因错误判据滞留。
    """
    bus = _bus()
    older = _TurnRow(status="queued")
    canceled = _TurnRow(status="canceled")
    session = _FakeSession(rows=[canceled])
    scheduler = _scheduler(bus, _FakeSessionFactory(session))
    scheduler._preempt_turns[older.thread_id] = str(canceled.id)

    assert await scheduler._can_start(older) is True
    assert older.thread_id not in scheduler._preempt_turns


async def test_can_start_keeps_live_preempt_marker() -> None:
    """未终态抢占轮在场：标记保留，既有 queued 轮顺延——自愈逻辑不得误清活标记。"""
    bus = _bus()
    older = _TurnRow(status="queued")
    newer = _TurnRow(status="queued")
    session = _FakeSession(rows=[newer])
    scheduler = _scheduler(bus, _FakeSessionFactory(session))
    scheduler._preempt_turns[older.thread_id] = str(newer.id)

    assert await scheduler._can_start(older) is False
    assert scheduler._preempt_turns[older.thread_id] == str(newer.id)


async def test_schedule_turn_clears_marker_when_preempting_row_missing() -> None:
    """抢占轮行不存在（登记后请求事务回滚）：schedule_turn 早退前清掉指向本轮的
    标记——行永不出现即无终态清除点，否则同线程既有 queued 轮被永久阻塞。"""
    bus = _bus()
    vanished = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    session = _FakeSession(turn=None)
    scheduler = _scheduler(bus, _FakeSessionFactory(session))
    scheduler._preempt_turns[thread_id] = vanished

    await scheduler.schedule_turn(vanished)

    assert thread_id not in scheduler._preempt_turns


# ---- TurnRunner 事件翻译与落库同 seq ----


async def test_execute_turn_translates_and_persists_with_same_seq() -> None:
    """custom 信号一对一翻译：stage/sources/done 以发布盖章 seq 落 turn_events；
    token 只发布不落库；sources 同短事务写 sources 表；终态先落 turns 再发 done。"""
    bus = _bus()
    turn = _TurnRow(status="running")
    session = _FakeSession(turn=turn)
    factory = _FakeSessionFactory(session)
    graph = _FakeGraph([
        _custom({"type": "stage", "stage_key": "rewrite", "status": "running"}),
        _custom({"type": "stage", "stage_key": "rewrite", "status": "done"}),
        _custom({"type": "stage", "stage_key": "search", "status": "running"}),
        _custom({"type": "stage", "stage_key": "search", "status": "done"}),
        _custom({"type": "sources", "batch": [_SRC], "total": 1}),
        _custom({"type": "token", "delta": "答案"}),
        _updates({"write": {"answer_md": "答案", "sources": [_SRC]}}),
    ])
    scheduler = _scheduler(bus, factory, graph)

    await scheduler._execute_turn(turn)

    published = await _drain_all(bus, str(turn.id))
    types = [e.event_type for e in published]
    assert types == ["stage", "stage", "stage", "stage", "sources", "token", "done"]
    events = [obj for obj in session.added if isinstance(obj, TurnEvent)]
    assert [e.event_type for e in events] == ["stage", "stage", "stage", "stage", "sources", "done"]
    # token 不落库 ✓（上一行无 token）
    # 同 seq：每个落库事件的 seq 与发布盖章 seq 一致（按 event_type 配对）
    persisted_by_type: dict[str, list[int]] = {}
    for obj in events:
        persisted_by_type.setdefault(obj.event_type, []).append(obj.seq)
    for event in published:
        if event.event_type in ("stage", "sources", "done"):
            assert event.seq in persisted_by_type.get(event.event_type, [])
    # payload 去 envelope 冗余（无 seq/turn_id/ts/type 键，业务字段直存）
    stage_payload = next(e.payload for e in events if e.event_type == "stage")
    assert set(stage_payload) == {"stage_key", "status"}
    sources_event = next(e for e in published if e.event_type == "sources")
    assert sources_event.data["total"] == 1
    # sources 表写入：ref_no 语义 + score Decimal 化
    source_rows = [obj for obj in session.added if isinstance(obj, Source)]
    assert len(source_rows) == 1
    assert source_rows[0].ref_no == 1
    assert source_rows[0].score == Decimal("0.91")
    # source_id 单一事实源（data-model §1.5）：行 id 先定、回写事件 batch——
    # SSE 事件面与行 id 同源（桩固定值被覆写，不再出现在事件面）
    assert sources_event.data["batch"][0]["source_id"] == str(source_rows[0].id)
    assert sources_event.data["batch"][0]["source_id"] != _SRC["source_id"]
    assert {
        k: v for k, v in sources_event.data["batch"][0].items() if k != "source_id"
    } == {k: v for k, v in _SRC.items() if k != "source_id"}
    persisted_sources = [e for e in events if e.event_type == "sources"]
    assert persisted_sources[0].payload["batch"][0]["source_id"] == str(source_rows[0].id)
    # 终态：先落 turns 再发 done
    assert turn.status == "completed"
    assert turn.answer_md == "答案"
    assert turn.finished_at is not None
    assert turn.duration_ms is not None and turn.duration_ms >= 0
    stage_entries = turn.timing["stages"]
    assert [s["stage_key"] for s in stage_entries] == ["rewrite", "search"]
    assert stage_entries[0]["status"] == "done" and "duration_ms" in stage_entries[0]
    done = next(e for e in published if e.event_type == "done")
    assert done.data["finish"] == "completed"
    assert "cancel_reason" not in done.data
    assert done.data["source_count"] == 1


async def test_execute_turn_messages_chunks_to_token_and_thinking() -> None:
    """messages 帧按帧读 .content：str→token；text 块→token；thinking/reasoning→thinking；
    全部只发布不落库（R8 口径：禁止聚合后再过 content_blocks）。"""
    bus = _bus()
    turn = _TurnRow(status="running")
    session = _FakeSession(turn=turn)
    graph = _FakeGraph([
        _message("增量", reasoning="先想一步"),
        _message([{"type": "text", "text": "文段"}, {"type": "thinking", "thinking": "思"}]),
    ])
    scheduler = _scheduler(bus, _FakeSessionFactory(session), graph)

    await scheduler._execute_turn(turn)

    published = await _drain_all(bus, str(turn.id))
    # 帧内顺序：reasoning 先于 content（思维链在前）；帧 2 text 块先于 thinking 块；
    # 全部只发布不落库 + 正常完成仍发 done
    assert [e.event_type for e in published] == [
        "thinking", "token", "token", "thinking", "done",
    ]
    # token/thinking 不落库（done 恒落库——终态不变式）
    persisted_types = [obj.event_type for obj in session.added if isinstance(obj, TurnEvent)]
    assert persisted_types == ["done"]
    assert turn.status == "completed"
    assert turn.answer_md is None  # 无顶层 updates answer_md → 不写
    done = published[-1]
    assert done.data["finish"] == "completed" and done.data["source_count"] == 0


async def test_stage_absorbs_progress_detail_extra() -> None:
    """stage 合档吸收 §7.2 可选 progress/detail/extra（评审 P2）：此前被丢弃，
    B3 图一发即静默丢失——现在 timing.stages 条目保留三键，extra 经
    get_turn_detail 透传 §2.5 stages[].extra。"""
    bus = _bus()
    turn = _TurnRow(status="running")
    session = _FakeSession(turn=turn)
    graph = _FakeGraph([
        _custom({"type": "stage", "stage_key": "search", "status": "running"}),
        _custom({
            "type": "stage",
            "stage_key": "search",
            "status": "done",
            "progress": {"current": 2, "total": 3},
            "detail": "Tavily 2/3 · BoCha 1/3",
            "extra": {"subagents": [{"subtopic": "A", "status": "done"}]},
        }),
    ])
    scheduler = _scheduler(bus, _FakeSessionFactory(session), graph)

    await scheduler._execute_turn(turn)

    (entry,) = turn.timing["stages"]
    assert entry["stage_key"] == "search" and entry["status"] == "done"
    assert entry["progress"] == {"current": 2, "total": 3}
    assert entry["detail"] == "Tavily 2/3 · BoCha 1/3"
    assert entry["extra"] == {"subagents": [{"subtopic": "A", "status": "done"}]}


# ---- 取消两态（打断非故障：绝不产生 error 事件）----


async def test_cancel_running_before_start_finishes_canceled_immediately() -> None:
    """启动前已登记取消标志 → 循环首事件边界感知 → 零业务事件 + done(canceled)。"""
    bus = _bus()
    turn = _TurnRow(status="running")
    session = _FakeSession(turn=turn)
    graph = _FakeGraph([
        _custom({"type": "stage", "stage_key": "rewrite", "status": "running"}),
        _custom({"type": "stage", "stage_key": "rewrite", "status": "done"}),
    ])
    scheduler = _scheduler(bus, _FakeSessionFactory(session), graph)
    scheduler._cancel_flags[str(turn.id)] = "superseded"  # 打断登记（模拟 ThreadScheduler）

    await scheduler._execute_turn(turn)

    published = await _drain_all(bus, str(turn.id))
    assert [e.event_type for e in published] == ["done"]
    assert published[0].data["finish"] == "canceled"
    assert published[0].data["cancel_reason"] == "superseded"
    assert turn.status == "canceled"
    assert turn.cancel_reason == "superseded"
    assert turn.error is None  # 取消不是 error（不污染 error 列语义）


async def test_cancel_running_midflight_senses_at_event_boundary() -> None:
    """运行中登记取消 → 下一个事件边界感知；已产出事件保留、随后 done(canceled) 收尾。"""
    bus = _bus()
    turn = _TurnRow(status="running")
    session = _FakeSession(turn=turn)
    graph = _FakeGraph([
        _custom({"type": "stage", "stage_key": "rewrite", "status": "running"}),
        _custom({"type": "stage", "stage_key": "rewrite", "status": "done"}),
        _custom({"type": "stage", "stage_key": "search", "status": "running"}),
    ], delay=0.03)
    scheduler = _scheduler(bus, _FakeSessionFactory(session), graph)

    async def register_late() -> None:
        # 时序：事件间隔 0.03s（0.03s 处产出第 1 事件并翻译），0.045s 落在其间隔中点——
        # 第 2 个 chunk（0.06s）到达时循环顶检查感知（asyncio 单线程，间隔 15ms 裕量稳）
        await asyncio.sleep(0.045)
        scheduler._cancel_flags[str(turn.id)] = "superseded"

    register_task = asyncio.create_task(register_late())
    await scheduler._execute_turn(turn)
    await register_task

    published = await _drain_all(bus, str(turn.id))
    types = [e.event_type for e in published]
    assert "error" not in types  # 打断非故障
    assert types[0] == "stage" and types[-1] == "done"  # 已产出保留 + canceled 收尾
    done = next(e for e in published if e.event_type == "done")
    assert done.data["finish"] == "canceled" and done.data["cancel_reason"] == "superseded"
    assert turn.status == "canceled" and turn.cancel_reason == "superseded"


async def test_cancel_queued_finishes_immediately_then_409002() -> None:
    """queued 轮取消：立即出队+终态 canceled（无节点边界等待），再取消同轮 → 409002。"""
    bus = _bus()
    turn = _TurnRow(status="queued")
    session = _FakeSession(turn=turn)
    scheduler = _scheduler(bus, _FakeSessionFactory(session))

    result = await scheduler.cancel_turn(str(turn.id), user_id=turn.user_id, reason="user")
    assert result == {"turn_id": str(turn.id), "status": "canceling"}
    assert turn.status == "canceled" and turn.cancel_reason == "user"
    # 第二道感知（评审 P1）：登记后收口即清理——防调度器不再复跑时标志泄漏
    assert str(turn.id) not in scheduler._cancel_flags
    published = await _drain_all(bus, str(turn.id))
    done = next(e for e in published if e.event_type == "done")
    assert done.data["finish"] == "canceled" and done.data["cancel_reason"] == "user"

    with pytest.raises(TurnTerminalException):  # 终态轮再取消 → 409002（api.md §2.7）
        await scheduler.cancel_turn(str(turn.id), user_id=turn.user_id)


async def test_cancel_unknown_or_not_owned_turn_raises_404001() -> None:
    """cancel 归属校验：不存在/非本人轮 → NotFoundException（404001，未找到与越权同码）。"""
    bus = _bus()
    scheduler = _scheduler(bus, _FakeSessionFactory(_FakeSession(turn=None)))
    with pytest.raises(NotFoundException):
        await scheduler.cancel_turn(str(uuid.uuid4()), user_id="someone-else")


# ---- 终态竞态守卫（评审 P1：cancel 与调度/图收尾交错，杜绝双终态）----


async def test_terminal_race_loser_skips_done_and_keeps_canceled() -> None:
    """行已 canceled 时图收尾条件落库落败 → 不发 done(completed)、不改写 status。

    竞态形态：cancel_turn 对 queued 轮收尾 canceled 与调度 promote/执行交错——
    修复前 _persist_terminal 无条件覆写，产出 done(canceled)+done(completed)
    双终态事件且「用户取消后轮子照跑完」。
    """
    bus = _bus()
    turn = _TurnRow(status="canceled")
    session = _FakeSession(turn=turn)
    graph = _FakeGraph([
        _custom({"type": "stage", "stage_key": "rewrite", "status": "running"}),
        _custom({"type": "stage", "stage_key": "rewrite", "status": "done"}),
    ])
    scheduler = _scheduler(bus, _FakeSessionFactory(session), graph)

    await scheduler._execute_turn(turn)

    published = await _drain_all(bus, str(turn.id))
    assert "done" not in [e.event_type for e in published]
    assert turn.status == "canceled"  # 终态不被覆写
    assert turn.finished_at is None  # 落败方零写入


async def test_promote_running_rejects_terminal_row() -> None:
    """_promote_running 条件推进（评审 P1）：终态行不推 running——
    queued 取消收口后迟到的调度循环不得把 canceled 改回 running 再执行。"""
    bus = _bus()
    turn = _TurnRow(status="canceled")
    session = _FakeSession(turn=turn)
    scheduler = _scheduler(bus, _FakeSessionFactory(session))
    assert await scheduler._promote_running(str(turn.id)) is False
    assert turn.status == "canceled"


async def test_promote_running_advances_pending_and_stamps_started_at() -> None:
    """pending → running 推进成功：status/started_at 盖章（排队时长可观测）。"""
    bus = _bus()
    turn = _TurnRow(status="pending")
    session = _FakeSession(turn=turn)
    scheduler = _scheduler(bus, _FakeSessionFactory(session))
    assert await scheduler._promote_running(str(turn.id)) is True
    assert turn.status == "running"
    assert turn.started_at is not None


# ---- 失败路径 / timeout 守卫（close_channel 联动）----


async def test_execute_turn_exception_fails_with_500001_and_closes_channel() -> None:
    """图运行异常 → error 事件（500001 兜底）+ turns.status=failed + close_channel。"""
    bus = _bus()
    turn = _TurnRow(status="running")
    session = _FakeSession(turn=turn)
    scheduler = _scheduler(bus, _FakeSessionFactory(session), _BoomGraph())

    await scheduler._execute_turn(turn)

    published = await _drain_all(bus, str(turn.id))
    assert [e.event_type for e in published] == ["stage", "error", ]
    error_event = published[1]
    assert error_event.data["code"] == "500001"
    assert turn.status == "failed"
    assert turn.error is not None and turn.error["code"] == "500001"
    channel = bus._channels.get(str(turn.id))
    assert channel is not None and channel.terminal is True and channel.closed is True


async def test_execute_turn_timeout_guard_fails_with_500301() -> None:
    """spec.turn_timeout_s 包 astream：超时 → error(500301 硬超时熔断) + failed + close_channel。"""
    bus = _bus()
    turn = _TurnRow(status="running")
    session = _FakeSession(turn=turn)
    graph = _FakeGraph([_custom({"type": "token", "delta": "x"})], delay=0.5)
    scheduler = _scheduler(bus, _FakeSessionFactory(session), graph, spec={
        **_SPEC_PAYLOAD,
        "fast": {"turn_timeout_s": 0.05},
    })

    await scheduler._execute_turn(turn)

    published = await _drain_all(bus, str(turn.id))
    assert [e.event_type for e in published] == ["error"]
    assert published[0].data["code"] == "500301"
    assert turn.status == "failed"
    assert turn.error is not None and turn.error["code"] == "500301"
    channel = bus._channels.get(str(turn.id))
    assert channel is not None and channel.terminal is True


# ---- seed_seq_from_db（进程重启防 uq 撞库）----


async def test_seed_seq_from_db_seeds_bus_counter() -> None:
    """按 thread_id 取 turn_events max(seq) 逐线程种入；空库线程缺省不种（max=None 跳过）。"""
    bus = _bus()
    session = _FakeSession(execute_rows=[("th-1", 46), ("th-2", None)])
    monkey_turn_service(bus=bus, session=session)

    await turn_service.seed_seq_from_db()

    assert bus._thread_last_seq["th-1"] == 46
    assert "th-2" not in bus._thread_last_seq  # max=None 行跳过


@pytest.fixture(autouse=True)
def _restore_monkey_turn_service():
    """每用例后恢复 monkey_turn_service 的模块级改写（同进程防跨层污染）。

    monkey_turn_service 直接赋值替换 turn_service.get_session_factory /
    get_event_bus 且用例内不恢复——pytest 同进程先 unit 后 integration 时假体
    残留，integration 的 start_thread_scheduler 缺省装配捡到假 session 工厂，
    schedule_turn 读行恒 None 静默早退，桩轮永不终态（B2 契约测试全量回归
    15 连挂实锤）。autouse 在本模块每用例 setup 捕获原引用、teardown 复位。
    """
    original = (turn_service.get_session_factory, turn_service.get_event_bus)
    yield
    turn_service.get_session_factory, turn_service.get_event_bus = original


def monkey_turn_service(bus: EventBus, session: _FakeSession) -> None:
    """直接替换 turn_service 模块级工厂引用（asyncio 单任务内安全；恢复归 autouse 夹具）。"""
    turn_service.get_session_factory = lambda: _FakeSessionFactory(session)  # type: ignore[method-assign]
    turn_service.get_event_bus = lambda: bus  # type: ignore[method-assign]


# ---- resolve_chat_model（400001 边界）----


def test_resolve_chat_model_validates_against_options() -> None:
    """缺省用 chat.default；合法键返回 (key, alias)；非法键 → BadRequestException（400001）。"""
    assert resolve_chat_model(None, _MAPPING_PAYLOAD) == ("qwen", "qwen3.5-4b-ollama")
    assert resolve_chat_model("glm", _MAPPING_PAYLOAD) == ("glm", "glm-4.7")
    with pytest.raises(BadRequestException):
        resolve_chat_model("llama", _MAPPING_PAYLOAD)


# ---- 辅助 ----


async def _drain_all(bus: EventBus, turn_id: str) -> list[SseEvent]:
    """订阅通道取全部已缓冲事件（重放路径）；无事件时静默返回空表。"""
    gen = bus.subscribe(turn_id)
    events: list[SseEvent] = []
    while True:
        try:
            events.append(await asyncio.wait_for(gen.__anext__(), timeout=0.2))
        except (StopAsyncIteration, TimeoutError):
            break
    await gen.aclose()
    return events
