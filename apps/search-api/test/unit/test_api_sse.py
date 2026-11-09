"""unit 层：SSE 最小端点（app/api/v1/turns.py GET /turns/{turn_id}/events，api.md §7）。

覆盖点：
- 建流前归属校验（api.md §7.1 515 行）：不存在/越权 → 404001 普通错误信封
  （application/json，非流内错误帧、不建通道）；
- 首连全量重放：Last-Event-ID 缺省 0 → 缓冲全部事件按 seq 升序下发；
- Last-Event-ID 续传：只补 seq 更大者；游标 ≥ 终态 seq（客户端已持有 done）→
  不重发直接关闭（零帧）；
- 心跳：每 sse_settings.heartbeat_seconds（SSE_HEARTBEAT_SECONDS）一行 ": ping"，
  不占 seq（心跳后的事件 seq 连续）；
- 终态关闭：done 帧后流关闭（B2 最小版，followups 保活窗口归批次 5）；
- error 路径：close_channel 置 closed → 缓冲排空后流自然结束（无 done 帧）；
- 帧格式：三行式 id/event/data，data=JSON 信封 {"seq","turn_id","ts",...payload}；
- EventBus.terminal_seq 访问器（端点判「客户端已持有 done」的依据）语义。

总线用真实 EventBus 实例（publish/subscribe/close_channel 真语义——零网络）；
DB 走假会话；流读取全部套 asyncio.timeout 防悬挂。注：httpx ASGITransport 为
整流缓冲（app 跑完才交付 body），中途帧经 body_parts 全量保真——心跳用例以
「订阅先行 + 测试侧延迟补发 done 收流」驱动，帧序与实时到达一致。
"""

import asyncio
import json
import uuid
from collections.abc import Iterator
from contextlib import suppress
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import sse_settings
from app.db.models import Turn
from app.dependencies import get_current_user, get_db, get_event_bus
from app.main import app
from app.services.event_bus import EventBus, SseEvent

_USER_ID = "00000000-0000-0000-0000-0000000000aa"
_OTHER_USER_ID = "00000000-0000-0000-0000-0000000000bb"
_THREAD_ID = "7d2f0a1e-0000-0000-0000-000000000001"
_TURN_ID = "c31b9d74-0000-0000-0000-000000000002"
_URL = f"/api/v1/turns/{_TURN_ID}/events"


# ---- 假体：归属校验用最小会话（emulate select(Turn.id) WHERE id+user 双条件）----


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeSession:
    """owner 与 current_user 相等才返回行（同 DB 的 WHERE user_id = <当前用户> 语义）。"""

    def __init__(self, *, owner: uuid.UUID | None, current_user: uuid.UUID) -> None:
        self._owner = owner
        self._current = current_user

    async def scalars(self, stmt: Any) -> _FakeResult:
        entity = stmt.column_descriptions[0]["entity"]
        assert entity is Turn, f"未预期的查询实体：{entity}"
        if self._owner is not None and self._owner == self._current:
            return _FakeResult([self._owner])
        return _FakeResult([])


def _wire(bus: EventBus, session: _FakeSession) -> None:
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: _USER_ID
    app.dependency_overrides[get_event_bus] = lambda: bus


@pytest.fixture
def overrides() -> Iterator[dict[Any, Any]]:
    yield app.dependency_overrides
    app.dependency_overrides.clear()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _publish(
    bus: EventBus, event_type: str, data: dict[str, Any], *, turn_id: str = _TURN_ID
) -> SseEvent:
    return await bus.publish(
        turn_id,
        SseEvent(thread_id=_THREAD_ID, event_type=event_type, data=data),
    )


def _parse_frames(body: str) -> list[dict[str, str]]:
    """SSE 帧解析：空行分隔；注释行（心跳）不计入帧字段。"""
    frames: list[dict[str, str]] = []
    for block in body.split("\n\n"):
        frame: dict[str, str] = {}
        for line in block.split("\n"):
            if not line or line.startswith(":"):
                continue
            key, _, value = line.partition(": ")
            frame[key] = value
        if frame:
            frames.append(frame)
    return frames


def _owned() -> _FakeSession:
    return _FakeSession(
        owner=uuid.UUID(_USER_ID), current_user=uuid.UUID(_USER_ID)
    )


# ---- 建流前归属校验（§7.1 515 行：普通错误信封，非流内错误帧）----


async def test_sse_unknown_turn_404001_before_stream(overrides: dict) -> None:
    bus = EventBus()
    _wire(bus, _FakeSession(owner=None, current_user=uuid.UUID(_USER_ID)))
    async with _client() as client:
        response = await client.get(_URL)
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "404001" and body["message"] == "轮次不存在"
    # 建流前拒绝：普通 JSON 信封而非 event-stream
    assert response.headers["content-type"].startswith("application/json")


async def test_sse_cross_user_404001_before_stream(overrides: dict) -> None:
    bus = EventBus()
    _wire(
        bus,
        _FakeSession(owner=uuid.UUID(_OTHER_USER_ID), current_user=uuid.UUID(_USER_ID)),
    )
    async with _client() as client:
        response = await client.get(_URL)
    assert response.status_code == 404
    assert response.json()["code"] == "404001"


async def test_sse_illegal_turn_id_404001(overrides: dict) -> None:
    bus = EventBus()
    _wire(bus, _FakeSession(owner=None, current_user=uuid.UUID(_USER_ID)))
    async with _client() as client:
        response = await client.get("/api/v1/turns/not-a-uuid/events")
    assert response.status_code == 404
    assert response.json()["code"] == "404001"


# ---- 重放与续传（§7.1 512-513 行）----


async def test_sse_first_connect_replays_full_buffer(overrides: dict) -> None:
    bus = EventBus()
    await _publish(bus, "stage", {"stage_key": "plan", "status": "running", "label": "规划"})
    await _publish(bus, "sources", {"batch": [{"ref_no": 1}], "total": 1})
    await _publish(bus, "done", {"finish": "completed", "duration_ms": 1000})
    _wire(bus, _owned())
    async with _client() as client:
        async with asyncio.timeout(10):
            async with client.stream("GET", _URL) as response:
                assert response.status_code == 200
                body = (await response.aread()).decode("utf-8")
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = _parse_frames(body)
    assert [f.get("id") for f in frames] == ["1", "2", "3"]
    assert [f.get("event") for f in frames] == ["stage", "sources", "done"]
    # data 信封：{"seq","turn_id","ts",...payload}——payload 字段平铺在顶层
    first = json.loads(frames[0]["data"])
    assert first["seq"] == 1 and first["turn_id"] == _TURN_ID
    assert first["stage_key"] == "plan" and first["status"] == "running"
    assert first["ts"].endswith("Z") and "T" in first["ts"]
    last = json.loads(frames[2]["data"])
    assert last["finish"] == "completed" and last["duration_ms"] == 1000


async def test_sse_last_event_id_replays_only_greater_seq(overrides: dict) -> None:
    bus = EventBus()
    await _publish(bus, "stage", {"stage_key": "plan", "status": "running"})
    await _publish(bus, "token", {"delta": "答案增量"})
    await _publish(bus, "done", {"finish": "completed"})
    _wire(bus, _owned())
    async with _client() as client:
        async with asyncio.timeout(10):
            async with client.stream("GET", _URL, headers={"Last-Event-ID": "1"}) as response:
                body = (await response.aread()).decode("utf-8")
    frames = _parse_frames(body)
    assert [f.get("id") for f in frames] == ["2", "3"]  # 只补 seq 更大者
    assert "event: done" in body  # 终态帧照常下发后关闭


async def test_sse_cursor_at_done_closes_without_resend(overrides: dict) -> None:
    """Last-Event-ID ≥ 终态 seq：客户端已持有 done——不重发，直接关闭（零帧）。"""
    bus = EventBus()
    await _publish(bus, "stage", {"stage_key": "plan", "status": "running"})
    done = await _publish(bus, "done", {"finish": "completed"})
    assert done.seq == 2
    _wire(bus, _owned())
    async with _client() as client:
        async with asyncio.timeout(10):
            async with client.stream(
                "GET", _URL, headers={"Last-Event-ID": str(done.seq)}
            ) as response:
                body = (await response.aread()).decode("utf-8")
    assert body == ""  # 零帧（无 done 重复、无心跳悬挂）


# ---- 心跳（§7.1 512 行：不占 seq）与终态关闭----


async def test_sse_heartbeat_visible_and_done_closes_stream(
    overrides: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """订阅先行（无预置事件）：静默窗口内 ": ping" 可见且不占 seq；done 补发后流关闭。"""
    monkeypatch.setattr(sse_settings, "heartbeat_seconds", 1)
    bus = EventBus()
    _wire(bus, _owned())

    async def _late_done() -> None:
        await asyncio.sleep(2.5)  # 静默 ≥2 个心跳周期后收流
        await _publish(bus, "done", {"finish": "completed"})

    async with _client() as client:
        task = asyncio.create_task(_late_done())
        try:
            async with asyncio.timeout(10):
                async with client.stream("GET", _URL) as response:
                    body = (await response.aread()).decode("utf-8")
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    assert body.count(": ping") >= 1  # 静默窗口内至少一个心跳行
    frames = [f for f in _parse_frames(body) if f.get("event")]
    assert [f.get("event") for f in frames] == ["done"]
    assert frames[0].get("id") == "1"  # 心跳不占 seq：首个事件仍从 1 起
    assert not body.rstrip().endswith("ping")  # done 后流关闭（ping 不是最后一帧）


async def test_sse_error_channel_close_ends_stream_without_done(overrides: dict) -> None:
    """error 路径：close_channel 置 closed——缓冲排空后流自然结束，无 done 帧。"""
    bus = EventBus()
    await _publish(bus, "stage", {"stage_key": "write", "status": "running"})
    await _publish(bus, "error", {"code": "500001", "message": "内部错误", "stage_key": None})
    assert await bus.close_channel(_TURN_ID) is True
    _wire(bus, _owned())
    async with _client() as client:
        async with asyncio.timeout(10):
            async with client.stream("GET", _URL) as response:
                body = (await response.aread()).decode("utf-8")
    frames = _parse_frames(body)
    assert [f.get("event") for f in frames] == ["stage", "error"]
    assert "event: done" not in body  # failed 轮不发 done（任务 1/2 失败路径口径）


# ---- EventBus.terminal_seq 访问器（端点终态判定依据）----


async def test_event_bus_terminal_seq_accessor() -> None:
    bus = EventBus()
    assert bus.terminal_seq(_TURN_ID) is None  # 通道不存在
    await _publish(bus, "stage", {"stage_key": "plan", "status": "running"})
    assert bus.terminal_seq(_TURN_ID) is None  # 未终态
    done = await _publish(bus, "done", {"finish": "completed"})
    assert bus.terminal_seq(_TURN_ID) == done.seq
    assert bus.terminal_seq("no-such-turn") is None

    # close_channel 路径：terminal 置位但缓冲无 done → None（流随 closed 自然结束）
    other = "c31b9d74-0000-0000-0000-000000000003"
    await _publish(bus, "error", {"code": "500001"}, turn_id=other)
    await bus.close_channel(other)
    assert bus.terminal_seq(other) is None


# ---- Last-Event-ID 头解析鲁棒性（非数字游标按首连全量重放）----


async def test_sse_illegal_last_event_id_falls_back_to_full_replay(
    overrides: dict,
) -> None:
    bus = EventBus()
    await _publish(bus, "stage", {"stage_key": "plan", "status": "running"})
    await _publish(bus, "done", {"finish": "completed"})
    _wire(bus, _owned())
    async with _client() as client:
        async with asyncio.timeout(10):
            async with client.stream(
                "GET", _URL, headers={"Last-Event-ID": "not-a-number"}
            ) as response:
                body = (await response.aread()).decode("utf-8")
    frames = _parse_frames(body)
    assert [f.get("id") for f in frames] == ["1", "2"]  # 游标无效 → 全量重放


# ---- db 依赖作用域守卫（评审 P2：连接归还先于流式响应消费）----


def test_sse_route_db_dependency_is_function_scoped() -> None:
    """SSE 端点 db 依赖 scope="function"：归属校验一条 SELECT 后即归还池连接。

    request 作用域（默认）下 teardown 在响应（整条流）发完才执行——池连接以
    idle-in-transaction 占满 SSE 流生命周期，多订阅并发耗尽 pool（10+5）；
    StreamingResponse 装配不依赖请求级 session，function 作用域零语义损失。
    """
    import typing

    from app.api.v1.turns import stream_turn_events

    hints = typing.get_type_hints(stream_turn_events, include_extras=True)
    depends = hints["db"].__metadata__[0]
    assert depends.dependency is get_db  # 同一依赖 callable（overrides 可整体替换）
    assert getattr(depends, "scope", None) == "function"
