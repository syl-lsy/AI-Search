"""integration：S2 SSE 全链路联调闭环（批次 5——api.md §7 / ADR-003 / DEVELOPMENT §2）。

真库 + 桩主图 + 真 EventBus（b2_runtime）+ 进程内 ASGITransport 真流（同 test_sse 口径）；
LLM 走桩（stub_flow），EventBus→SSE 管道、seq、心跳、Last-Event-ID 重放全部走真管道。

六项闭环对应（rows 命名「闭环①」~「闭环⑥」）：
- ① seq 线程级单调、不重不漏（含跨多轮接续）；
- ② 15s 心跳（契约默认值）出现且不占 seq，报文形态 ": ping" 注释行；
- ③ Last-Event-ID 重放补帧无缺无重；非法游标按 0 全量重放（宽路径）；
  TTL 滑动续期/运行中缓冲永不过期语义已由 unit/test_event_bus 锁死（本文件不真等 30min）；
- ④ done ↔ GET /turns 终态对账 + turn_events 落库子集逐字段核对（不抽样）；
- ⑤ 同线程前轮未终态再提问 → reject 409001，且原轮 SSE 流不受影响；
- ⑥ ADR-003 四策略 SSE 表现：reject（原轮流照常）/ enqueue（排队轮纯心跳段→首 stage=
  rewrite→常规序列）/ interrupt·rollback（旧轮尾部 done(canceled,reason) 无 error、
  新轮独立事件流常规序列）。
"""

import asyncio
import uuid

import pytest
from sqlalchemy import select

from test.integration import stub_flow
from test.integration.test_sse import (
    _assert_sse_envelope,
    _assert_stream_headers,
    parse_sse_frames,
)
from app.core.config import sse_settings
from app.db.models import TurnEvent
from app.db.session import get_session_factory

_FAST_QUERY = "S2 联调：LangGraph Send 扇出与状态归并的关系？"


async def _db_turn_events(turn_id: str) -> list[TurnEvent]:
    async with get_session_factory()() as session:
        rows = (
            await session.scalars(
                select(TurnEvent)
                .where(TurnEvent.turn_id == uuid.UUID(turn_id))
                .order_by(TurnEvent.seq.asc())
            )
        ).all()
        return list(rows)


async def _create_completed_turn(async_client, b2_rest) -> str:
    """同步建一轮 fast 桩轮并等至 completed（无运行窗口交互需求时用）。"""
    resp = await async_client.post("/api/v1/threads", json={"query": _FAST_QUERY, "mode": "fast"})
    assert resp.status_code == 200
    turn_id = resp.json()["data"]["turn_id"]
    await b2_rest.wait_turn_status(turn_id, {"completed"})
    return turn_id


# ---- 闭环①：seq 线程级单调、不重不漏（含跨多轮接续）----


@pytest.mark.requires_db
async def test_seq_monotonic_unique_across_turns(async_client, b2_rest, b2_runtime, wait_for, monkeypatch):
    """同线程两轮先后跑完：各轮流内 id 严格递增连续无重复；跨轮接续
    （turn1 末帧 seq+1 == turn2 首帧 seq——线程级计数器跨轮推进、无重不漏）。"""
    resp = await async_client.post("/api/v1/threads", json={"query": _FAST_QUERY, "mode": "fast"})
    turn1 = resp.json()["data"]["turn_id"]
    thread_id = resp.json()["data"]["thread_id"]
    await b2_rest.wait_turn_status(turn1, {"completed"})
    resp2 = await async_client.post(
        f"/api/v1/threads/{thread_id}/turns", json={"query": "S2 第二轮追问", "mode": "fast"}
    )
    turn2 = resp2.json()["data"]["turn_id"]
    await b2_rest.wait_turn_status(turn2, {"completed"})

    stream1 = await async_client.get(f"/api/v1/turns/{turn1}/events")
    stream2 = await async_client.get(f"/api/v1/turns/{turn2}/events")
    _assert_stream_headers(stream1)
    _assert_stream_headers(stream2)
    frames1, _ = parse_sse_frames(stream1.text)
    frames2, _ = parse_sse_frames(stream2.text)
    ids1 = [f["id"] for f in frames1]
    ids2 = [f["id"] for f in frames2]
    # 不重：各自流内严格递增且唯一；不漏：从首轮起始 seq 起连续（步长 1）
    assert ids1 == sorted(set(ids1)) and ids1 == list(range(ids1[0], ids1[0] + len(ids1)))
    assert ids2 == sorted(set(ids2)) and ids2 == list(range(ids2[0], ids2[0] + len(ids2)))
    # 跨轮接续：线程级 seq 计数器推进无重不漏
    assert ids2[0] == ids1[-1] + 1
    # 流 ⊇ 各轮 turn_events 落库子集
    for turn_id, frames in ((turn1, frames1), (turn2, frames2)):
        rows = await _db_turn_events(turn_id)
        persisted = {r.seq for r in rows}
        streamed = {f["id"] for f in frames}
        assert persisted <= streamed


# ---- 闭环②：心跳契约默认值 + 报文形态 ": ping"（不占 seq）----


@pytest.mark.requires_db
async def test_heartbeat_contract_default_and_wire_format(
    async_client, b2_rest, b2_runtime, monkeypatch
):
    """契约默认心跳 15s（config 缺省值）；运行窗口内心跳以 ": ping" 注释行出现、
    不占 seq（业务帧 id 不因心跳中断连续性）。"""
    assert sse_settings.heartbeat_seconds == 15  # api.md §7.1「每 15s」契约缺省
    assert sse_settings.event_ttl_minutes == 30  # §7.3 缓冲 TTL 30 分钟契约缺省
    monkeypatch.setattr(sse_settings, "heartbeat_seconds", 1)  # 测试窗口压缩
    monkeypatch.setattr(stub_flow, "FAKE_DELAY_S", 2.5)

    post_task = asyncio.create_task(
        async_client.post("/api/v1/threads", json={"query": _FAST_QUERY, "mode": "fast"})
    )
    try:
        _, turn_id = await b2_rest.wait_first_thread()
        stream_task = asyncio.create_task(
            async_client.get(f"/api/v1/turns/{turn_id}/events")
        )
        try:
            await asyncio.wait_for(post_task, timeout=30)
            stream = await asyncio.wait_for(stream_task, timeout=30)
        finally:
            stream_task.cancel()
            await asyncio.gather(stream_task, return_exceptions=True)
    finally:
        await asyncio.gather(post_task, return_exceptions=True)

    body = stream.text
    assert body.count(": ping") >= 1  # 心跳以 ": ping" 注释行出现（§7.1 报文形态）
    frames, heartbeats = parse_sse_frames(body)
    assert heartbeats >= 1
    ids = [f["id"] for f in frames]
    assert ids == sorted(set(ids)) and ids == list(range(ids[0], ids[0] + len(ids)))
    # 心跳行不携带 id/event/data（纯注释）——逐块核对形态
    for block in body.split("\n\n"):
        lines = [line for line in block.splitlines() if line.strip()]
        if lines and all(line.startswith(":") for line in lines):
            assert lines == [": ping"]  # 心跳块恰一行、精确文本


# ---- 闭环③：非法 Last-Event-ID 按 0 全量重放（宽路径）----


@pytest.mark.requires_db
async def test_invalid_last_event_id_replays_full(async_client, b2_rest, b2_runtime):
    """非法 Last-Event-ID（非数字）按 0 处理 → 全量重放 == 首连流（§7.1 513 行宽路径）。"""
    turn_id = await _create_completed_turn(async_client, b2_rest)
    baseline = await async_client.get(f"/api/v1/turns/{turn_id}/events")
    full = await async_client.get(
        f"/api/v1/turns/{turn_id}/events", headers={"Last-Event-ID": "not-a-number"}
    )
    assert full.text == baseline.text  # 逐字节一致：全量重放


# ---- 闭环④：done ↔ GET 终态对账 + turn_events 逐字段核对（不抽样）----


@pytest.mark.requires_db
async def test_done_reconciliation_and_turn_events_field_by_field(
    async_client, b2_rest, b2_runtime
):
    """每个落库事件：SSE data 去信封三键后与 turn_events.payload **逐字段相等**
    （不抽样）；done 帧 ↔ GET /turns §2.5 终态字段逐项对账。"""
    turn_id = await _create_completed_turn(async_client, b2_rest)
    stream = await async_client.get(f"/api/v1/turns/{turn_id}/events")
    frames, _ = parse_sse_frames(stream.text)
    rows = await _db_turn_events(turn_id)

    # 流 ⊇ 落库子集；逐行逐字段比对（payload=去信封冗余后的业务字段，§7.2 落库口径）
    by_seq = {f["id"]: f for f in frames}
    for row in rows:
        frame = by_seq[row.seq]
        assert frame["event"] == row.event_type
        envelope_keys = {"seq", "turn_id", "ts"}
        assert {k: v for k, v in frame["data"].items() if k not in envelope_keys} == row.payload

    # done ↔ GET /turns 对账（§7.1「事件到达时 2.5 已可对账」不变式）
    detail = (await async_client.get(f"/api/v1/turns/{turn_id}")).json()["data"]
    done = by_seq[rows[-1].seq]
    assert done["event"] == "done"
    assert done["data"]["finish"] == "completed" and detail["status"] == "completed"
    assert done["data"]["degraded"] == detail["degraded"]
    assert done["data"]["duration_ms"] == detail["duration_ms"]
    assert done["data"]["source_count"] == len(detail["sources"])
    # usage 语义对账：§2.5 DTO 全 None 键（缺省形态）与 done 帧 {} 同义（桩轮无用量）
    detail_usage = {k: v for k, v in detail["usage"].items() if v is not None}
    assert done["data"]["usage"] == detail_usage


# ---- 闭环⑤/⑥-reject：409001 且原轮 SSE 流不受影响 ----


@pytest.mark.requires_db
async def test_reject_409001_keeps_running_turn_stream_healthy(
    async_client, b2_rest, b2_runtime, monkeypatch
):
    """同线程前轮未终态再提问 → 409001（缺省 reject）；原轮 SSE 流照常推进至
    done(completed)（零 error 帧、终态对账一致）——SSE 视角无副作用。"""
    monkeypatch.setattr(stub_flow, "FAKE_DELAY_S", 2.0)
    post_task = asyncio.create_task(
        async_client.post("/api/v1/threads", json={"query": "S2 reject 占位轮", "mode": "fast"})
    )
    try:
        thread_id, turn1 = await b2_rest.wait_first_thread()
        await b2_rest.wait_turn_status(turn1, {"running"})
        body = await async_client.post(
            f"/api/v1/threads/{thread_id}/turns",
            json={"query": "S2 并发提问应被拒", "mode": "fast"},
        )
        assert body.status_code == 409
        assert body.json()["code"] == "409001"
        assert body.json()["message"] == "线程已有进行中轮次"
    finally:
        await asyncio.wait_for(post_task, timeout=30)
    detail = await b2_rest.wait_turn_status(turn1, {"completed"})
    assert detail["status"] == "completed"

    stream = await async_client.get(f"/api/v1/turns/{turn1}/events")
    frames, _ = parse_sse_frames(stream.text)
    events = [f["event"] for f in frames]
    assert "error" not in events  # 拒绝不影响原轮（409001 是请求层错误、不进流）
    assert frames[-1]["event"] == "done" and frames[-1]["data"]["finish"] == "completed"
    assert frames[-1]["data"]["duration_ms"] == detail["duration_ms"]


# ---- 闭环⑥-enqueue：排队轮纯心跳段 → 首 stage=rewrite → 常规序列 ----


@pytest.mark.requires_db
async def test_enqueue_queued_turn_stream_silent_until_first_stage(
    async_client, b2_rest, b2_runtime, wait_for, monkeypatch
):
    """§7.1 排队轮口径：订阅先行期间无业务事件（纯心跳段）；当前轮 done 后收到
    本轮首个 stage（fast=rewrite），此后与常规序列一致。"""
    monkeypatch.setattr(sse_settings, "heartbeat_seconds", 1)
    monkeypatch.setattr(stub_flow, "FAKE_DELAY_S", 2.5)
    post1 = asyncio.create_task(
        async_client.post("/api/v1/threads", json={"query": "S2 enqueue 占位轮", "mode": "fast"})
    )
    try:
        thread_id, turn1 = await b2_rest.wait_first_thread()
        await b2_rest.wait_turn_status(turn1, {"running"})
        post2 = asyncio.create_task(
            async_client.post(
                f"/api/v1/threads/{thread_id}/turns",
                json={"query": "S2 排队轮", "mode": "fast", "on_conflict": "enqueue"},
            )
        )
        try:
            turn2: str | None = None

            async def _second_visible() -> bool:
                nonlocal turn2
                detail = await async_client.get(f"/api/v1/threads/{thread_id}")
                if detail.status_code != 200:
                    return False
                turns = detail.json()["data"]["turns"]
                if len(turns) < 2:
                    return False
                turn2 = turns[1]["turn_id"]
                return True

            await wait_for(_second_visible, description="排队轮行可见")
            await b2_rest.wait_turn_status(turn2, {"queued"})
            # 订阅必须先于 post2 await（ASGI 下 post2 响应含 BackgroundTask 全程——
            # 排队轮整个执行完才返回）：先订阅才存在「排队纯心跳段」窗口
            stream_task = asyncio.create_task(
                async_client.get(f"/api/v1/turns/{turn2}/events")
            )
        finally:
            await asyncio.wait_for(post2, timeout=30)

        try:
            await b2_rest.wait_turn_status(turn2, {"completed"}, timeout_s=45.0)
            stream = await asyncio.wait_for(stream_task, timeout=45)
        finally:
            stream_task.cancel()
            await asyncio.gather(stream_task, return_exceptions=True)
    finally:
        await asyncio.gather(post1, return_exceptions=True)

    _assert_stream_headers(stream)
    frames, heartbeats = parse_sse_frames(stream.text)
    assert heartbeats >= 1  # 排队/运行窗口内出现 ": ping" 心跳（订阅先行于执行）
    assert frames, "排队轮执行后应有业务帧"
    first = frames[0]
    assert first["event"] == "stage"  # 首个业务事件即 stage（执行开始信号）
    assert first["data"]["stage_key"] == "rewrite" and first["data"]["status"] == "running"
    assert frames[-1]["event"] == "done" and frames[-1]["data"]["finish"] == "completed"
    for frame in frames:
        _assert_sse_envelope(frame, turn2)


# ---- 闭环⑥-interrupt/rollback：旧轮尾部 done(canceled,reason) 无 error、新轮独立流 ----


@pytest.mark.requires_db
@pytest.mark.parametrize(
    ("on_conflict", "expected_reason"),
    [("interrupt", "superseded"), ("rollback", "rolled_back")],
)
async def test_break_strategy_old_turn_stream_tail_and_new_turn_stream(
    async_client, b2_rest, b2_runtime, wait_for, monkeypatch, on_conflict, expected_reason
):
    """§7.4 v1.4 注②：打断的旧轮尾部 [任意进行中事件] → done(canceled, reason)——
    无 error 事件；新轮为独立 turn 独立事件流、按常规序列至 done(completed)；
    旧轮 done ↔ GET 对账（status=canceled / cancel_reason 同源）。"""
    monkeypatch.setattr(stub_flow, "FAKE_DELAY_S", 3.0)
    post1 = asyncio.create_task(
        async_client.post("/api/v1/threads", json={"query": f"S2 {on_conflict} 旧轮", "mode": "fast"})
    )
    try:
        thread_id, turn1 = await b2_rest.wait_first_thread()
        await b2_rest.wait_turn_status(turn1, {"running"})
        resp2 = await async_client.post(
            f"/api/v1/threads/{thread_id}/turns",
            json={"query": f"S2 {on_conflict} 新轮", "mode": "fast", "on_conflict": on_conflict},
        )
        assert resp2.status_code == 200
        turn2 = resp2.json()["data"]["turn_id"]
        detail1 = await b2_rest.wait_turn_status(turn1, {"canceled"})
        assert detail1["cancel_reason"] == expected_reason
        detail2 = await b2_rest.wait_turn_status(turn2, {"completed"}, timeout_s=45.0)
    finally:
        await asyncio.gather(post1, return_exceptions=True)

    # 旧轮流：无 error 帧；末帧 done(canceled, reason)；done ↔ GET 对账
    stream1 = await async_client.get(f"/api/v1/turns/{turn1}/events")
    frames1, _ = parse_sse_frames(stream1.text)
    events1 = [f["event"] for f in frames1]
    assert "error" not in events1  # 打断非故障（§7.2/§0）
    assert frames1[0]["event"] == "stage"  # 已产事件保留（stage 在前）
    assert frames1[-1]["event"] == "done"
    assert frames1[-1]["data"]["finish"] == "canceled"
    assert frames1[-1]["data"]["cancel_reason"] == expected_reason
    assert frames1[-1]["data"]["duration_ms"] == detail1["duration_ms"]
    # 旧轮流 ids 仍单调连续（打断不破坏 seq 语义）
    ids1 = [f["id"] for f in frames1]
    assert ids1 == list(range(ids1[0], ids1[0] + len(ids1)))

    # 新轮流：独立事件流、常规序列至 done(completed)；与旧轮无 seq 交集
    stream2 = await async_client.get(f"/api/v1/turns/{turn2}/events")
    frames2, _ = parse_sse_frames(stream2.text)
    assert frames2[0]["event"] == "stage"
    assert frames2[-1]["event"] == "done" and frames2[-1]["data"]["finish"] == "completed"
    ids2 = [f["id"] for f in frames2]
    assert ids2 == list(range(ids2[0], ids2[0] + len(ids2)))
    assert set(ids1).isdisjoint(ids2)  # seq 线程级作用域：不同轮各自占号、无重复
    # 跨轮线程级单调：新轮首事件 seq 晚于旧轮 done seq
    done1_seq = next(f["id"] for f in frames1 if f["event"] == "done")
    assert ids2[0] > done1_seq
    assert detail2["status"] == "completed"
