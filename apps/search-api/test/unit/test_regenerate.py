"""unit 层：regenerate 端点服务面（B11 P2 / api.md §2.12 v1.19 / REQ-063 / A47）。

钉住的语义（冻结契约，逐条对应测试）：
- 前置三条件（全满足才推进，任一不满足 409006）：`status ∈ {degraded, failed}` ∧
  `mode=fast` ∧ sources 非空；归属校验 id+user 双条件（404001 同款）；
- 条件 UPDATE 防双击（WHERE status IN (...) AND mode=fast AND EXISTS(sources)）：
  落败方 409006；推进同时清 error/degraded_reason 并刷 started_at；
- 通道重开在请求内完成（terminal 解除——客户端 200 后立即重订阅不被短路）；
- 微图执行输入重建：fused 自 sources 行（不重检索）/ search_strategy 自
  turn_events stage(rewrite,done).extra / spec 当次新快照；
- 收尾口径（决策 14）：成功 → completed + answer_md 原地更新 + stages 追加 +
  duration_ms 不覆写 + error 清除；再失败 → degraded + error(500101)；
  取消 → canceled（stages 追加）。

零网络：EventBus 真内存实现、session/graph 假体（test.unit.turn_fakes 提供共享假体）。
"""

import uuid
from typing import Any

import pytest

from app.agent.nodes.synthesis import SynthesisError, SynthesisFallback
from app.agent.tools.registry import SearchChain
from app.config_center.cache import AssetSnapshot
from app.db.models import Source, Thread, Turn, TurnEvent, TurnFeedback
from app.exception import NotFoundException, TurnNotRegenerableException
from app.services.turn_service import ThreadScheduler
from test.unit.turn_fakes import _FakeSessionFactory, _TurnRow, _bus, _drain_all, _MessageChunk

_TURN_ID = "c31b9d74-0000-4000-8000-0000000000b1"
_THREAD_ID = "7d2f0a1e-0000-4000-8000-0000000000b2"
_USER_ID = "00000000-0000-4000-8000-0000000000b3"

_OLD_STAGE = {
    "stage_key": "write",
    "status": "done",
    "label": "综合中",
    "started_at": "2026-11-15T00:00:00+00:00",
    "ended_at": "2026-11-15T00:00:01+00:00",
    "duration_ms": 1000,
}

_SPEC_PAYLOAD: dict[str, Any] = {
    "turn": {"default_on_conflict": "reject"},
    "fast": {"turn_timeout_s": 5, "max_answer_chars": 3000},
    "followups": {
        "enabled": False,
        "count": 5,
        "generate_timeout_ms": 8000,
        "sse_hold_window_ms": 10000,
    },
}

_MAPPING_PAYLOAD: dict[str, Any] = {
    "chat": {"options": {"qwen": "chat-standard"}, "default": "qwen"},
}


def _snapshot(payload: dict[str, Any], data_id: str = "ai_search.agent.spec.json") -> AssetSnapshot:
    return AssetSnapshot(
        data_id=data_id, version=1, md5="m", updated_at="t", payload=payload
    )


class _Result:
    """execute/scalars 结果假体：rowcount 供条件 UPDATE 落败判定。"""

    def __init__(self, rows: list[Any], *, rowcount: int = 0) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return list(self._rows)


class _RegenSession:
    """最小 AsyncSession 假体：按实体分流 + UPDATE 行数注入 + 写入记录。"""

    def __init__(
        self,
        *,
        turn: Any = None,
        sources: list[Any] | None = None,
        events: list[Any] | None = None,
        precheck: Any = None,
        active_row: Any = None,
        update_rowcount: int = 1,
        get_failures: int = 0,
    ) -> None:
        self.turn = turn
        self.sources = sources or []
        self.events = events or []
        self.precheck = precheck
        self.active_row = active_row  # 同线程另有进行中/排队轮（单轮不变式守卫）
        self.update_rowcount = update_rowcount
        self.get_failures = get_failures  # 前 N 次 get 抛错（_load_turn 瞬断面）
        self.executed: list[Any] = []
        self.added: list[Any] = []
        self.commits = 0
        self._precheck_used = False

    async def get(self, model: Any, key: Any, *, with_for_update: bool = False) -> Any:
        if self.get_failures > 0:
            self.get_failures -= 1
            raise RuntimeError("db 瞬断（假体注入）")
        return self.turn

    async def scalars(self, stmt: Any) -> _Result:
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Turn:
            return _Result([self.turn] if self.turn is not None else [])
        if entity is Source:
            return _Result(list(self.sources))
        if entity is TurnEvent:
            return _Result(list(self.events))
        if entity is TurnFeedback:
            return _Result([])
        raise AssertionError(f"未预期的查询实体：{entity}")

    async def execute(self, stmt: Any) -> _Result:
        self.executed.append(stmt)
        if str(stmt).lstrip().upper().startswith("UPDATE"):
            return _Result([], rowcount=self.update_rowcount)
        entity = stmt.column_descriptions[0].get("entity")
        if entity is Thread:
            return _Result([])  # 线程行锁（FOR UPDATE 的取行结果不使用）
        if not self._precheck_used:
            self._precheck_used = True
            return _Result([self.precheck] if self.precheck is not None else [])
        return _Result([self.active_row] if self.active_row is not None else [])

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def __aenter__(self) -> "_RegenSession":
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


def _turn(*, status: str = "degraded", mode: str = "fast") -> _TurnRow:
    turn = _TurnRow(status=status, mode=mode, thread_id=_THREAD_ID, user_id=_USER_ID, query="问题")
    turn.id = uuid.UUID(_TURN_ID)
    turn.answer_md = "旧答案[1]。"
    turn.timing = {"stages": [dict(_OLD_STAGE)]}
    turn.duration_ms = 9000
    turn.error = {"code": "500101", "message": "综合生成失败，已保留检索来源", "stage_key": None}
    turn.degraded_reason = None
    turn.query_lang = "zh"
    return turn


def _source(ref_no: int, url: str) -> Any:
    """sources 行假体（列名对齐 Source ORM——实现按属性读取）。"""
    import types

    return types.SimpleNamespace(
        id=uuid.uuid4(),
        ref_no=ref_no,
        source_type="web",
        title=f"来源 {ref_no}",
        url=url,
        snippet="片段",
        score=None,
    )


def _event(*, seq: int, stage_key: str, status: str, extra: Any = None) -> Any:
    import types

    payload: dict[str, Any] = {"stage_key": stage_key, "status": status}
    if extra is not None:
        payload["extra"] = extra
    return types.SimpleNamespace(
        thread_id=uuid.UUID(_THREAD_ID),
        turn_id=uuid.UUID(_TURN_ID),
        seq=seq,
        event_type="stage",
        payload=payload,
    )


class _FakeRegenGraph:
    """regenerate 微图假体：stage(write) custom + token messages + 终态 updates。"""

    def __init__(self, *, answer: str = "新的答案[1]。", fail: Exception | None = None) -> None:
        self.answer = answer
        self.fail = fail
        self.payloads: list[Any] = []
        self.configs: list[Any] = []

    def astream(
        self, payload: Any, config: Any = None, stream_mode: Any = None, subgraphs: bool = False
    ) -> Any:
        self.payloads.append(payload)
        self.configs.append(config)
        fail = self.fail

        async def gen():
            yield ((), "custom", {"type": "stage", "stage_key": "write", "status": "running", "label": "综合中"})
            for delta in ("新的", "答案[1]。"):
                yield ((), "messages", (_MessageChunk(delta), {"langgraph_node": "synthesis"}))
            if fail is not None:
                raise fail
            yield ((), "custom", {"type": "stage", "stage_key": "write", "status": "done"})
            yield ((), "updates", {"synthesis": {"answer_md": self.answer, "degraded": False}})

        return gen()


def _scheduler(
    session: _RegenSession,
    *,
    bus: Any = None,
    graph: Any = None,
    spec_payload: dict[str, Any] | None = None,
) -> ThreadScheduler:
    return ThreadScheduler(
        bus=bus if bus is not None else _bus(),
        session_factory=_FakeSessionFactory(session),
        spec_snapshot=_snapshot(spec_payload or _SPEC_PAYLOAD),
        mapping_snapshot=_snapshot(_MAPPING_PAYLOAD, "ai_search.model.mapping.json"),
        tools_registry=None,
        regenerate_graph_override=graph,
    )


# ---- regenerate_turn：前置裁决 + 条件推进 + 通道重开 ----


async def test_regenerate_turn_promotes_running_and_reopens_channel() -> None:
    """degraded 轮 → running：条件 UPDATE 带 EXISTS 守卫、通道 terminal 解除。"""
    bus = _bus()
    turn = _turn()
    # 旧流终态（degraded 收束：done 已发布）
    from app.services.event_bus import SseEvent

    await bus.publish(_TURN_ID, SseEvent(thread_id=_THREAD_ID, event_type="done", data={"finish": "degraded"}))
    assert bus.terminal_seq(_TURN_ID) is not None
    session = _RegenSession(
        turn=turn,
        sources=[_source(1, "https://a/1")],
        precheck=type("Row", (), {"id": turn.id, "thread_id": turn.thread_id, "status": "degraded", "mode": "fast"})(),
        update_rowcount=1,
    )
    scheduler = _scheduler(session, bus=bus)

    result = await scheduler.regenerate_turn(_TURN_ID, user_id=_USER_ID)

    assert result == {"turn_id": _TURN_ID, "status": "running"}
    assert bus.terminal_seq(_TURN_ID) is None  # 通道重开
    assert session.commits == 1
    update_sql = str(session.executed[-1])
    assert update_sql.strip().upper().startswith("UPDATE")
    assert "EXISTS" in update_sql.upper()  # 条件 UPDATE 含来源守卫（防双击）
    assert "error" in update_sql and "degraded_reason" in update_sql  # 清旧失败留痕


async def test_regenerate_turn_not_found_404001() -> None:
    """轮不存在/越权/非法 UUID → 404001（不泄漏存在性）。"""
    scheduler = _scheduler(_RegenSession(turn=None))
    with pytest.raises(NotFoundException):
        await scheduler.regenerate_turn(_TURN_ID, user_id=_USER_ID)
    with pytest.raises(NotFoundException):
        await scheduler.regenerate_turn("not-a-uuid", user_id=_USER_ID)


@pytest.mark.parametrize(
    ("status", "mode", "sources"),
    [
        ("completed", "fast", [1]),  # 非 {degraded, failed} 终态
        ("canceled", "fast", [1]),
        ("running", "fast", [1]),  # 进行中重复点击（预检面）
        ("degraded", "deep", [1]),  # deep 轮
        ("degraded", "fast", []),  # 零来源
        ("failed", "fast", []),  # 零源 failed（500201 等）
    ],
)
async def test_regenerate_turn_prerequisite_409006(
    status: str, mode: str, sources: list[int]
) -> None:
    """§2.12 前置三条件任一不满足 → 409006（单码覆盖语义族）。"""
    turn = _turn(status=status, mode=mode)
    session = _RegenSession(
        turn=turn,
        sources=[_source(i + 1, f"https://a/{i}") for i in sources],
        precheck=type("Row", (), {"id": turn.id, "thread_id": turn.thread_id, "status": status, "mode": mode})(),
    )
    scheduler = _scheduler(session)
    with pytest.raises(TurnNotRegenerableException):
        await scheduler.regenerate_turn(_TURN_ID, user_id=_USER_ID)
    assert session.commits == 0


async def test_regenerate_turn_failed_with_sources_allowed() -> None:
    """failed 且有来源（崩溃孤儿路径）同可重生成（决策 12）。"""
    turn = _turn(status="failed")
    session = _RegenSession(
        turn=turn,
        sources=[_source(1, "https://a/1")],
        precheck=type("Row", (), {"id": turn.id, "thread_id": turn.thread_id, "status": "failed", "mode": "fast"})(),
    )
    scheduler = _scheduler(session)
    result = await scheduler.regenerate_turn(_TURN_ID, user_id=_USER_ID)
    assert result["status"] == "running"


async def test_regenerate_turn_double_click_409006() -> None:
    """并发双击：条件 UPDATE rowcount=0 的落败方 → 409006（同码）。"""
    turn = _turn()
    session = _RegenSession(
        turn=turn,
        sources=[_source(1, "https://a/1")],
        precheck=type("Row", (), {"id": turn.id, "thread_id": turn.thread_id, "status": "degraded", "mode": "fast"})(),
        update_rowcount=0,
    )
    scheduler = _scheduler(session)
    with pytest.raises(TurnNotRegenerableException):
        await scheduler.regenerate_turn(_TURN_ID, user_id=_USER_ID)


async def test_regenerate_turn_thread_busy_409006() -> None:
    """线程级单轮不变式：同线程另有进行中/排队轮 → 409006（不把本轮再置 running）。"""
    turn = _turn()
    session = _RegenSession(
        turn=turn,
        sources=[_source(1, "https://a/1")],
        precheck=type("Row", (), {"id": turn.id, "thread_id": turn.thread_id, "status": "degraded", "mode": "fast"})(),
        active_row=(uuid.uuid4(),),
    )
    scheduler = _scheduler(session)
    with pytest.raises(TurnNotRegenerableException):
        await scheduler.regenerate_turn(_TURN_ID, user_id=_USER_ID)
    assert session.commits == 0


# ---- execute_regenerate：微图执行与收尾口径 ----


async def test_execute_regenerate_success_merges_stages_and_replaces_answer() -> None:
    """成功 → completed：answer_md 原地更新、stages 追加 write 段、duration 不覆写、
    旧 error 清除；事件面 = stage(write,running) + token… + stage(done) + done(completed)。"""
    bus = _bus()
    turn = _turn(status="running")  # 条件推进后由 BackgroundTask 执行
    session = _RegenSession(
        turn=turn,
        sources=[_source(1, "https://a/1"), _source(2, "https://b/2")],
        events=[
            _event(seq=1, stage_key="rewrite", status="done", extra={"strategy": "verify"}),
            _event(seq=2, stage_key="search", status="done"),
        ],
    )
    graph = _FakeRegenGraph(answer="新的答案[1][2]。")
    scheduler = _scheduler(session, bus=bus, graph=graph)

    await scheduler.execute_regenerate(_TURN_ID)

    assert graph.payloads[0]["fused"][0]["url"] == "https://a/1"  # 不重检索：sources 重建
    assert graph.payloads[0]["search_strategy"] == "verify"  # turn_events 重建
    assert graph.payloads[0]["answerable_from_knowledge"] is False  # 保守缺省
    assert turn.status == "completed"
    assert turn.answer_md == "新的答案[1][2]。"
    assert turn.duration_ms == 9000  # 不覆写（提交→首次 done 口径）
    assert turn.error is None and turn.degraded_reason is None
    stages = turn.timing["stages"]
    assert stages[0] == _OLD_STAGE  # 原时间线保留
    assert stages[-1]["stage_key"] == "write" and stages[-1]["status"] == "done"  # 追加段
    events = await _drain_all(bus, _TURN_ID)
    kinds = [e.event_type for e in events]
    assert kinds[0] == "stage" and events[0].data["status"] == "running"
    assert "token" in kinds
    assert [e.data["finish"] for e in events if e.event_type == "done"] == ["completed"]
    done = next(e for e in events if e.event_type == "done")
    assert done.data["source_count"] == 2  # 沿该轮累计不变
    assert done.data["duration_ms"] == 9000


async def test_execute_regenerate_failure_degrades_with_500101() -> None:
    """**防御分支**（节点契约外形态）：SynthesisError 带 sources 时仍 error(500101)+
    done(degraded)。v1.59 起真实再失败走 SynthesisFallback 兜底（上一用例），本路径
    仅防节点契约被破坏时轮永卡 running 的兜底收尾（500101 实际不可达）。"""
    bus = _bus()
    turn = _turn(status="running")
    session = _RegenSession(turn=turn, sources=[_source(1, "https://a/1")], events=[])
    scheduler = _scheduler(session, bus=bus, graph=_FakeRegenGraph(fail=SynthesisError("重试耗尽")))

    await scheduler.execute_regenerate(_TURN_ID)

    assert turn.status == "degraded"
    assert turn.error["code"] == "500101"
    assert turn.duration_ms == 9000
    events = await _drain_all(bus, _TURN_ID)
    kinds = [e.event_type for e in events]
    error = next(e for e in events if e.event_type == "error")
    assert error.data["code"] == "500101"
    done = next(e for e in events if e.event_type == "done")
    assert done.data["finish"] == "degraded" and done.data["source_count"] == 1
    assert kinds[-1] == "done"  # 降级收束：error 后跟 done


async def test_execute_regenerate_fallback_degrades_without_error() -> None:
    """v1.59 增量 A：再失败走兜底产出收束（零 error）——status=degraded、
    answer_md=兜底全文、清旧 error、duration 不覆写、stages 追加。"""
    bus = _bus()
    turn = _turn(status="running")
    turn.error = {"code": "500101", "message": "综合生成失败，请稍后重试", "stage_key": None}
    session = _RegenSession(turn=turn, sources=[_source(1, "https://a/1")], events=[])
    fallback = SynthesisFallback(
        answer_md="部分正文。", kind="synthesis_partial",
        failure="timeout", attempts=2, source_count=1,
    )
    scheduler = _scheduler(session, bus=bus, graph=_FakeRegenGraph(fail=fallback))

    await scheduler.execute_regenerate(_TURN_ID)

    assert turn.status == "degraded"
    assert turn.answer_md == "部分正文。"  # 部分正文持久化（刷新不丢）
    assert turn.error is None  # 兜底轮无错误卡（清旧 error）
    assert turn.degraded_reason is None
    assert turn.duration_ms == 9000  # regenerate 不覆写
    events = await _drain_all(bus, _TURN_ID)
    kinds = [e.event_type for e in events]
    assert "error" not in kinds  # 零 error 事件
    done = next(e for e in events if e.event_type == "done")
    assert done.data["finish"] == "degraded"
    assert done.data["degraded"] is True
    assert done.data["degraded_reason"] is None
    assert done.data["source_count"] == 1


async def test_execute_regenerate_canceled_before_start() -> None:
    """推进后、开跑前取消（标志已在）→ canceled 收尾（merge stages、无 error 事件）。"""
    bus = _bus()
    turn = _turn(status="running")
    session = _RegenSession(turn=turn, sources=[_source(1, "https://a/1")], events=[])
    scheduler = _scheduler(session, bus=bus, graph=_FakeRegenGraph())
    scheduler._cancel_flags[_TURN_ID] = "user"

    await scheduler.execute_regenerate(_TURN_ID)

    assert turn.status == "canceled" and turn.cancel_reason == "user"
    events = await _drain_all(bus, _TURN_ID)
    assert [e.event_type for e in events] == ["done"]
    assert events[0].data["cancel_reason"] == "user"


async def test_execute_regenerate_skips_when_not_running() -> None:
    """竞态：推进后已被取消/删除（status≠running）→ 零事件退出。"""
    bus = _bus()
    turn = _turn(status="canceled")
    graph = _FakeRegenGraph()
    scheduler = _scheduler(_RegenSession(turn=turn, sources=[]), bus=bus, graph=graph)
    await scheduler.execute_regenerate(_TURN_ID)
    assert graph.payloads == []
    assert await _drain_all(bus, _TURN_ID) == []


async def test_build_regenerate_context_from_sources_and_events() -> None:
    """输入重建：fused 按 sources 行序（SQL ref_no 升序）、search_strategy 缺省 web。"""
    turn = _turn()
    session = _RegenSession(
        turn=turn,
        sources=[_source(1, "https://a/1"), _source(2, "https://b/2")],
        events=[],
    )
    scheduler = _scheduler(session)

    ctx = await scheduler._build_regenerate_context(turn)

    assert [item["url"] for item in ctx.fused] == ["https://a/1", "https://b/2"]
    assert ctx.sources_count == 2
    assert ctx.search_strategy == "web"
    assert ctx.regenerate is True
    assert ctx.spec_payload["fast"]["turn_timeout_s"] == 5  # 当次快照


async def test_execute_regenerate_timeout_degrades_with_500301() -> None:
    """轮级超时（turn_timeout_s 包络）→ 500301 降级收束（stages/duration 口径同 regenerate）。"""
    import asyncio

    class _SlowGraph:
        def astream(self, payload: Any, config: Any = None, stream_mode: Any = None, subgraphs: bool = False) -> Any:
            async def gen():
                await asyncio.sleep(0.2)
                yield ((), "custom", {"type": "stage", "stage_key": "write", "status": "running"})

            return gen()

    bus = _bus()
    turn = _turn(status="running")
    session = _RegenSession(turn=turn, sources=[_source(1, "https://a/1")], events=[])
    spec = {**_SPEC_PAYLOAD, "fast": {**_SPEC_PAYLOAD["fast"], "turn_timeout_s": 0.02}}
    scheduler = _scheduler(session, bus=bus, graph=_SlowGraph(), spec_payload=spec)

    await scheduler.execute_regenerate(_TURN_ID)

    assert turn.status == "degraded"
    assert turn.error["code"] == "500301"
    assert turn.degraded_reason == "timeout"
    assert turn.duration_ms == 9000
    events = await _drain_all(bus, _TURN_ID)
    assert [e.event_type for e in events] == ["error", "done"]
    assert events[0].data["code"] == "500301"


async def test_execute_regenerate_canceled_mid_stream() -> None:
    """运行中取消（事件边界感知）→ aclose 收口 + canceled（answer_md 不被覆写）。"""
    scheduler_holder: dict[str, Any] = {}

    class _CancelMidStreamGraph:
        def astream(self, payload: Any, config: Any = None, stream_mode: Any = None, subgraphs: bool = False) -> Any:
            async def gen():
                yield ((), "custom", {"type": "stage", "stage_key": "write", "status": "running"})
                scheduler_holder["scheduler"]._cancel_flags[_TURN_ID] = "user"  # 取消在运行中到达
                yield ((), "custom", {"type": "stage", "stage_key": "write", "status": "done"})
                yield ((), "updates", {"synthesis": {"answer_md": "不应落库", "degraded": False}})

            return gen()

    bus = _bus()
    turn = _turn(status="running")
    session = _RegenSession(turn=turn, sources=[_source(1, "https://a/1")], events=[])
    scheduler = _scheduler(session, bus=bus, graph=_CancelMidStreamGraph())
    scheduler_holder["scheduler"] = scheduler

    await scheduler.execute_regenerate(_TURN_ID)

    assert turn.status == "canceled" and turn.cancel_reason == "user"
    assert turn.answer_md == "旧答案[1]。"  # 取消不落新答案
    assert turn.duration_ms == 9000
    events = await _drain_all(bus, _TURN_ID)
    assert [e.event_type for e in events] == ["stage", "done"]
    assert events[0].data["status"] == "running"  # 取消前的 stage 已发布
    assert events[-1].data["cancel_reason"] == "user"


# ---- 双评审修复：线程行锁 / 加载段收口 ----


async def test_regenerate_turn_locks_thread_row_before_busy_check_and_clears_finished_at() -> None:
    """评审 P3-1：锁序 Thread(FOR UPDATE) → 活跃轮查询 → 条件 UPDATE；推进清 finished_at。"""
    turn = _turn()
    session = _RegenSession(
        turn=turn,
        sources=[_source(1, "https://a/1")],
        precheck=type(
            "Row",
            (),
            {"id": turn.id, "thread_id": turn.thread_id, "status": "degraded", "mode": "fast"},
        )(),
    )
    scheduler = _scheduler(session)

    await scheduler.regenerate_turn(_TURN_ID, user_id=_USER_ID)

    # 语句序：① 归属+状态预检 ② Thread 行锁 ③ 活跃轮查询 ④ 条件 UPDATE
    assert "FOR UPDATE" in str(session.executed[1]).upper()
    assert "threads" in str(session.executed[1]).lower()
    assert "turns" in str(session.executed[2]).lower()
    update_sql = str(session.executed[3])
    assert update_sql.strip().upper().startswith("UPDATE")
    assert "finished_at" in update_sql  # 重新 running：旧终态时刻作废


async def test_execute_regenerate_load_failure_degrades_not_stuck() -> None:
    """评审 P3-2：_load_turn 瞬断（try 内）→ 重载一次 → 500001 降级收束，不永卡 running。"""
    bus = _bus()
    turn = _turn(status="running")
    session = _RegenSession(
        turn=turn, sources=[_source(1, "https://a/1")], events=[], get_failures=1
    )
    scheduler = _scheduler(session, bus=bus, graph=_FakeRegenGraph())

    await scheduler.execute_regenerate(_TURN_ID)

    assert turn.status == "degraded"
    assert turn.error["code"] == "500001"
    assert turn.answer_md == "旧答案[1]。"  # 未生成新答案
    events = await _drain_all(bus, _TURN_ID)
    assert [e.event_type for e in events] == ["error", "done"]
