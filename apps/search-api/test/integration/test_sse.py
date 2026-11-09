"""integration：SSE 事件流契约测试（api.md §2.6/§7.1~§7.4 v1.9）——真流断言。

真库 + 桩主图 + 真 EventBus（conftest.b2_runtime）+ 进程内 ASGITransport 真流：

- ASGITransport 整流缓冲（app 跑完才交付 body）：终态轮订阅在重放完 done 后关闭，
  GET 直接返回完整流文本；运行中轮订阅以「POST 在后台执行 + 测试侧轮询发现轮次」
  驱动——订阅先行合法（§7.1 排队轮口径：纯心跳段直到首个业务事件）。
- 断言口径逐条对照 §7.1/§7.2：三行式帧（id:=seq/event:/data:=JSON）、data 信封
  三键 {seq, turn_id, ts}、心跳 ": ping" 注释行不占 seq、Last-Event-ID 续传只补
  更大 seq、turn_id 不存在建流前 404001 普通信封、done 后关闭。
"""

import asyncio
import json
import uuid
from datetime import datetime
from typing import Any

import pytest

from app.agent.graphs import fast as fast_graph
from app.core.config import sse_settings
from app.db.models import TurnEvent
from app.db.session import get_session_factory
from sqlalchemy import select

_FAST_QUERY = "LangGraph 的 Send API 和 Router 的区别？"


# ---- SSE 解析与断言辅助 ----


def parse_sse_frames(body: str) -> tuple[list[dict[str, Any]], int]:
    """SSE 文本 → (事件帧列表, 心跳行数)。

    帧形态 api.md §7.1 三行式（id:/event:/data:）；心跳为 ": ping" 注释行
    （不占 seq）；帧元素 {"id", "event", "data"}（data 为 JSON 解析结果）。
    """
    frames: list[dict[str, Any]] = []
    heartbeats = 0
    for block in body.split("\n\n"):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if all(line.startswith(":") for line in lines):
            heartbeats += 1
            continue
        frame: dict[str, Any] = {}
        for line in lines:
            if line.startswith(":"):
                continue  # 混排注释行：既非心跳帧也非帧字段
            field, _, value = line.partition(": ")
            if field == "id":
                frame["id"] = int(value)
            elif field == "event":
                frame["event"] = value
            elif field == "data":
                frame["data"] = json.loads(value)
        if frame:
            frames.append(frame)
    return frames, heartbeats


def _assert_sse_envelope(frame: dict[str, Any], turn_id: str) -> None:
    """§7.1 data 通用信封三键：seq=帧 id、turn_id、ts（ISO-8601 UTC Z 后缀）。"""
    data = frame["data"]
    assert data["seq"] == frame["id"]  # id: 与 data.seq 同源盖章
    assert data["turn_id"] == turn_id
    ts = data["ts"]
    assert ts.endswith("Z")
    datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _assert_stream_headers(resp) -> None:
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["cache-control"] == "no-cache"


async def _create_completed_turn(async_client, wait_for) -> str:
    """建一轮 fast 桩轮并等至 completed，返回 turn_id。"""
    resp = await async_client.post(
        "/api/v1/threads", json={"query": _FAST_QUERY, "mode": "fast"}
    )
    assert resp.status_code == 200
    turn_id = resp.json()["data"]["turn_id"]

    async def _completed() -> bool:
        r = await async_client.get(f"/api/v1/turns/{turn_id}")
        return r.status_code == 200 and r.json()["data"]["status"] == "completed"

    await wait_for(_completed, timeout_s=30.0, description="桩轮到达 completed")
    return turn_id


# ---- §7.1：首连全量重放 + §7.4 快速序列对账 ----


@pytest.mark.requires_db
async def test_sse_first_connect_full_replay(async_client, b2_rest, b2_runtime, wait_for):
    turn_id = await _create_completed_turn(async_client, wait_for)

    resp = await async_client.get(f"/api/v1/turns/{turn_id}/events")
    _assert_stream_headers(resp)
    frames, heartbeats = parse_sse_frames(resp.text)
    assert frames, "首连应全量重放至少一帧"
    # 桩轮完成远快于默认 15s 心跳：终态轮重放流不含心跳
    assert heartbeats == 0

    # 帧序：id 线程内单调递增；首个事件 stage、末个事件 done（done 后关闭）
    ids = [f["id"] for f in frames]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    assert frames[0]["event"] == "stage"
    assert frames[-1]["event"] == "done"
    for frame in frames:
        _assert_sse_envelope(frame, turn_id)

    # §7.2 事件类型覆盖：stage/sources/token/done（桩不发 images/memory）
    event_types = {f["event"] for f in frames}
    assert {"stage", "sources", "token", "done"} <= event_types
    assert "error" not in event_types

    # token 增量拼接 = 桩答案（与 REST answer_md 同源）
    token_concat = "".join(f["data"]["delta"] for f in frames if f["event"] == "token")
    assert token_concat == fast_graph.STUB_FAST_ANSWER_MD

    # done 帧 payload 与 REST 终态对账
    detail = (await async_client.get(f"/api/v1/turns/{turn_id}")).json()["data"]
    done_data = frames[-1]["data"]
    assert done_data["finish"] == "completed"
    assert done_data["degraded"] is False
    assert done_data["duration_ms"] == detail["duration_ms"]
    assert done_data["source_count"] == 2
    assert done_data["usage"] == {}

    # 流 ⊇ turn_events 落库子集；流多出的帧恰为只发布不落库的 token（ADR-003 第 4 条）
    async with get_session_factory()() as session:
        rows = (
            await session.scalars(
                select(TurnEvent)
                .where(TurnEvent.turn_id == uuid.UUID(turn_id))
                .order_by(TurnEvent.seq.asc())
            )
        ).all()
    persisted = {(r.seq, r.event_type) for r in rows}
    streamed = {(f["id"], f["event"]) for f in frames}
    assert persisted <= streamed
    extra = streamed - persisted
    extra_types = {t for _, t in extra}
    assert extra_types == {"token"}  # token 只发布不落库
    # token 的 seq 不在 turn_events（与 payload 逐条核对 event_type）
    for seq, event_type in sorted(persisted):
        frame = next(f for f in frames if f["id"] == seq)
        assert frame["event"] == event_type


# ---- §7.1/§7.3：Last-Event-ID 续传只补更大 seq ----


@pytest.mark.requires_db
async def test_sse_last_event_id_incremental(async_client, b2_rest, b2_runtime, wait_for):
    turn_id = await _create_completed_turn(async_client, wait_for)
    full, _ = parse_sse_frames(
        (await async_client.get(f"/api/v1/turns/{turn_id}/events")).text
    )
    assert full
    # 断点取首个 sources 帧（断线发生在来源卡片后）
    mid_seq = next(f["id"] for f in full if f["event"] == "sources")

    resumed = await async_client.get(
        f"/api/v1/turns/{turn_id}/events", headers={"Last-Event-ID": str(mid_seq)}
    )
    _assert_stream_headers(resumed)
    frames, _ = parse_sse_frames(resumed.text)
    assert frames, "断点之后应有补发帧"
    assert all(f["id"] > mid_seq for f in frames)  # 只补更大 seq
    assert frames[0]["id"] == mid_seq + 1  # seq 连续无缺口
    assert frames[-1]["event"] == "done"  # 重放全部后发送 done 并关闭
    # 补发集合 = 全量流中 seq 更大者的精确子集
    full_after = [f for f in full if f["id"] > mid_seq]
    assert [(f["id"], f["event"]) for f in frames] == [
        (f["id"], f["event"]) for f in full_after
    ]


# ---- §7.1：客户端已持有 done → 零帧关闭（terminal_seq 对偶分支）----


@pytest.mark.requires_db
async def test_sse_last_event_id_at_terminal_closes_silently(
    async_client, b2_rest, b2_runtime, wait_for
):
    turn_id = await _create_completed_turn(async_client, wait_for)
    full, _ = parse_sse_frames(
        (await async_client.get(f"/api/v1/turns/{turn_id}/events")).text
    )
    done_seq = full[-1]["id"]

    resp = await async_client.get(
        f"/api/v1/turns/{turn_id}/events", headers={"Last-Event-ID": str(done_seq)}
    )
    _assert_stream_headers(resp)
    frames, heartbeats = parse_sse_frames(resp.text)
    assert frames == [] and heartbeats == 0  # 零帧直接关闭（done 不重发）


# ---- §7.1：心跳 ": ping"（不占 seq；订阅先行合法）----


@pytest.mark.requires_db
async def test_sse_heartbeat_during_running_turn(
    async_client, b2_rest, b2_runtime, wait_for, monkeypatch
):
    """运行中轮订阅：心跳织入业务事件之间（心跳间隔配置调小至 1s，桩图 delay 制造窗口）。"""
    monkeypatch.setattr(sse_settings, "heartbeat_seconds", 1)
    monkeypatch.setattr(fast_graph, "SEARCH_DELAY_S", 2.5)

    post_task = asyncio.create_task(
        async_client.post("/api/v1/threads", json={"query": _FAST_QUERY, "mode": "fast"})
    )
    try:
        _, turn_id = await b2_rest.wait_first_thread()
        # 订阅先行（轮运行中）：重放已发布事件后挂在跟随态等新事件
        stream_task = asyncio.create_task(
            async_client.get(f"/api/v1/turns/{turn_id}/events")
        )
        try:
            post_resp = await asyncio.wait_for(post_task, timeout=30)
            assert post_resp.status_code == 200
            stream = await asyncio.wait_for(stream_task, timeout=30)
        finally:
            stream_task.cancel()
            await asyncio.gather(stream_task, return_exceptions=True)
    finally:
        await asyncio.gather(post_task, return_exceptions=True)

    _assert_stream_headers(stream)
    frames, heartbeats = parse_sse_frames(stream.text)
    assert heartbeats >= 1, "运行窗口内应至少出现一次 ': ping' 心跳"
    # 心跳不占 seq：帧 id 序列仍连续单调（心跳只以注释行出现）
    ids = [f["id"] for f in frames]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    # 业务事件照常推进至终态：done 收尾（图跑完后订阅流随之关闭）
    assert frames, "心跳之外应有业务事件帧"
    assert frames[-1]["event"] == "done"
    for frame in frames:
        _assert_sse_envelope(frame, turn_id)


# ---- §7.1：turn_id 不存在 → 建流前 404001（普通错误信封，非流内错误帧）----


@pytest.mark.requires_db
async def test_sse_unknown_turn_404_before_stream(async_client, b2_rest, b2_runtime):
    resp = await async_client.get(f"/api/v1/turns/{uuid.uuid4()}/events")
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "404001"
    assert body["message"] == "轮次不存在"
    assert "data" not in body
    # 非法 UUID 同码（不泄漏解析细节）
    resp = await async_client.get("/api/v1/turns/not-a-uuid/events")
    assert resp.status_code == 404
    assert resp.json()["code"] == "404001"
