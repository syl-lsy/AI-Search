"""unit 层：done 后保活窗口（followups，api.md §7.1——fake clock 收口时序）。

钉住的语义（冻结契约，逐条对应测试）：
- **达标轮**：done 发布前登记窗口 → SSE 端点 done 后**不立即关闭**，窗口内收到
  followups 事件即下发并收口；
- **窗口超时**：静默关闭、不报错（无 error 事件、无新信号）；
- **非达标轮/开关关闭/无登记**：done 后立即收口（与 v1.7 前语义完全一致，零观测差异）；
- **重放路径**：缓冲中 done 之后存在 followups → 重放序列 `[…, done, followups]` 后关闭；
- **cursor 已持有 done**（`Last-Event-ID == done.seq`）：仍须补下发 followups（seq 更大者
  照常重放）；cursor 已覆盖 followups 时零帧关闭（既有 terminal_seq 对偶分支不变）。

clock 注入：EventBus(clock=…) 单调钟假体——窗口剩余时间与推进完全由测试控制（真实
asyncio 计时不参与判定，只用心跳节拍驱动循环重算剩余）。
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.core.config import SseSettings
from app.services.event_bus import EventBus, SseEvent
from app.api.v1.turns import _event_stream

_TURN_ID = "c31b9d74-0000-4000-8000-000000000002"
_THREAD_ID = "7d2f0a1e-0000-4000-8000-000000000001"


class _Clock:
    """单调钟假体：now() 只随测试显式推进。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _bus(clock: _Clock) -> EventBus:
    return EventBus(
        settings=SseSettings(event_ttl_minutes=30, buffer_max_events=2000), clock=clock
    )


async def _publish(bus: EventBus, event_type: str, data: dict[str, Any]) -> SseEvent:
    return await bus.publish(
        _TURN_ID, SseEvent(thread_id=_THREAD_ID, event_type=event_type, data=data)
    )


async def _collect(
    bus: EventBus, *, last_seq: int = 0, heartbeat_seconds: float = 0.02
) -> list[str]:
    """收全帧（至流自然结束）：返回 event 名序列（心跳注释行跳过）。"""
    names: list[str] = []
    stream: AsyncIterator[bytes] = _event_stream(
        bus, _TURN_ID, last_seq, heartbeat_seconds=heartbeat_seconds
    )
    async with asyncio.timeout(5):
        async for chunk in stream:
            for line in chunk.decode("utf-8").split("\n"):
                if line.startswith("event: "):
                    names.append(line.removeprefix("event: "))
    return names


# ---- 非达标轮：done 后立即收口（零行为差异）----


async def test_done_closes_immediately_without_hold_registration() -> None:
    """未登记窗口（开关关闭/非达标轮/deep 轮）→ done 后立即关闭（既有语义不变）。"""
    clock = _Clock()
    bus = _bus(clock)
    await _publish(bus, "stage", {"stage_key": "rewrite", "status": "running"})
    await _publish(bus, "done", {"finish": "completed", "duration_ms": 10})
    assert await _collect(bus) == ["stage", "done"]


# ---- 窗口内收口 ----


async def test_hold_window_delivers_followups_then_closes() -> None:
    """窗口内 followups 到达 → 下发后立即收口（done 后不提前关闭）。"""
    clock = _Clock()
    bus = _bus(clock)
    await _publish(bus, "done", {"finish": "completed", "duration_ms": 10})
    assert bus.expect_followups(_TURN_ID, window_ms=10000) is True
    assert bus.followups_hold_remaining(_TURN_ID) == pytest.approx(10.0)

    collector = asyncio.create_task(_collect(bus))
    await asyncio.sleep(0.05)  # 让订阅流走到「done 之后等待」状态
    await _publish(bus, "followups", {"items": ["A？"]})
    names = await collector
    assert names == ["done", "followups"]


async def test_hold_window_expires_silently_without_followups() -> None:
    """窗口超时（无 followups）→ 静默关闭：只有 done、无 error、无 followups。"""
    clock = _Clock()
    bus = _bus(clock)
    await _publish(bus, "done", {"finish": "completed", "duration_ms": 10})
    assert bus.expect_followups(_TURN_ID, window_ms=100) is True
    clock.advance(0.5)  # 窗口已过（deadline = 1000 + 0.1）

    names = await _collect(bus)
    assert names == ["done"]


async def test_hold_window_remaining_none_after_expiry() -> None:
    """`followups_hold_remaining` 到期返回 None（端点判定「该收口」的一致来源）。"""
    clock = _Clock()
    bus = _bus(clock)
    await _publish(bus, "done", {"finish": "completed"})
    bus.expect_followups(_TURN_ID, window_ms=100)
    assert bus.followups_hold_remaining(_TURN_ID) == pytest.approx(0.1)
    clock.advance(0.2)
    assert bus.followups_hold_remaining(_TURN_ID) is None
    assert bus.followups_hold_remaining("no-such-turn") is None


async def test_expect_followups_unknown_channel_is_noop() -> None:
    """通道不存在（该轮从未发布事件）→ 不建通道、返回 False（编程错误面兜底）。"""
    bus = _bus(_Clock())
    assert bus.expect_followups("no-such-turn", window_ms=1000) is False
    assert bus.followups_seq("no-such-turn") is None


async def test_abandon_followups_closes_window_early() -> None:
    """生成已静默放弃（`abandon_followups`）→ 窗口提前失效、端点静默收口
    （api.md §7.1「窗口超时或生成已静默放弃 → 静默关闭」；clock 不推进也收口）。"""
    clock = _Clock()
    bus = _bus(clock)
    await _publish(bus, "done", {"finish": "completed"})
    bus.expect_followups(_TURN_ID, window_ms=10000)

    collector = asyncio.create_task(_collect(bus))
    await asyncio.sleep(0.05)  # 流已进入保活等待
    bus.abandon_followups(_TURN_ID)
    assert await collector == ["done"]  # 分段轮询在 ≤0.2s 内收口，无 error 帧


async def test_abandon_is_noop_after_followups_published() -> None:
    """已发布 followups 时 abandon 不改状态（重放路径仍需按 seq 补发）。"""
    clock = _Clock()
    bus = _bus(clock)
    await _publish(bus, "done", {"finish": "completed"})
    bus.expect_followups(_TURN_ID, window_ms=10000)
    await _publish(bus, "followups", {"items": ["A？"]})
    bus.abandon_followups(_TURN_ID)
    assert bus.followups_seq(_TURN_ID) is not None
    bus.abandon_followups("no-such-turn")  # 通道不存在 no-op


# ---- 重放路径（§7.1「终态轮重放口径」）----


async def test_replay_includes_followups_after_done() -> None:
    """缓冲中 done 后存在 followups → 重放序列 […, done, followups] 后关闭。"""
    clock = _Clock()
    bus = _bus(clock)
    await _publish(bus, "done", {"finish": "completed"})
    await _publish(bus, "followups", {"items": ["A？"]})
    assert await _collect(bus) == ["done", "followups"]


async def test_cursor_at_done_still_replays_later_followups() -> None:
    """`Last-Event-ID == done.seq`（客户端已持有 done）→ 仍补下发 followups 后关闭。"""
    clock = _Clock()
    bus = _bus(clock)
    done = await _publish(bus, "done", {"finish": "completed"})
    followups = await _publish(bus, "followups", {"items": ["A？"]})
    assert followups.seq == (done.seq or 0) + 1
    assert await _collect(bus, last_seq=done.seq or 0) == ["followups"]


async def test_cursor_past_followups_closes_with_zero_frames() -> None:
    """cursor 已覆盖 done + followups → 零帧关闭（既有 terminal_seq 对偶分支不变）。"""
    clock = _Clock()
    bus = _bus(clock)
    await _publish(bus, "done", {"finish": "completed"})
    tail = await _publish(bus, "followups", {"items": ["A？"]})
    assert await _collect(bus, last_seq=tail.seq or 0) == []


async def test_cursor_past_followups_closes_even_when_window_open() -> None:
    """窗口未耗尽 + followups 已发布 + cursor 已越过 → **零帧立即关闭**（评审 P3-3）：
    此前 `awaiting` 只看窗口剩余，客户端重连后空挂到窗口耗尽（≤10s）才收口。"""
    clock = _Clock()
    bus = _bus(clock)
    await _publish(bus, "done", {"finish": "completed"})
    bus.expect_followups(_TURN_ID, window_ms=10000)  # 窗口仍开（剩余 10s）
    tail = await _publish(bus, "followups", {"items": ["A？"]})

    async with asyncio.timeout(1.0):  # 空挂即红（窗口自然耗尽需 10s）
        assert await _collect(bus, last_seq=tail.seq or 0) == []


async def test_window_open_keeps_late_subscriber_following() -> None:
    """窗口内晚订阅（cursor 已持有 done，followups 尚未生成）→ 保持跟随至事件到达。"""
    clock = _Clock()
    bus = _bus(clock)
    done = await _publish(bus, "done", {"finish": "completed"})
    bus.expect_followups(_TURN_ID, window_ms=10000)

    collector = asyncio.create_task(_collect(bus, last_seq=done.seq or 0))
    await asyncio.sleep(0.05)
    await _publish(bus, "followups", {"items": ["晚到？"]})
    assert await collector == ["followups"]
