"""unit 层：services/event_bus.py 轮次事件总线（B1 底座）。

钉住的语义（冻结契约，逐条对应测试）：
- seq 线程级单调（data-model.md §1.4：EventBus 发布时取 max(seq) per thread + 1，
  uq(thread_id, seq) 唯一；ARCHITECTURE ADR-003 决策 2「事件带线程级单调 seq 作为
  SSE id:」）——同线程跨轮续号、换线程重起；
- 订阅先重放缓冲中 seq > last_seq 再实时跟随；首次连接 last_seq=0 全量重放
  （api.md §7.1）；
- 缓冲上限 SSE_BUFFER_MAX_EVENTS 满则淘汰最旧；淘汰缺口从现存缓冲最早事件起重放、
  静默跳过不报错（ADR-003 v1.7 增注 2，终态由 REST 对账兜底）；
- 滑动 TTL SSE_EVENT_TTL_MINUTES：每写入一个事件刷新 last_activity；仅回收
  「已终态（done）∨ 空缓冲 ∧ 无活跃订阅 ∧ 静止超时」——运行中轮缓冲永不过期，
  error 非终态（api.md §7.2：error 后必跟 done）（ADR-003 v1.7 增注 1 / api.md §7.3
  v1.5 增注）；
- 多订阅者 fan-out 各自持游标、独立重放，任一断开仅移除自身（ADR-003 v1.7 增注 3）；
- done 后事件（followups，ADR-003 v1.16）照常占号、入缓冲、刷新 last_activity；
- 订阅先行（排队轮订阅照常建立，无业务事件仅心跳——api.md §7.1 排队轮流注）；
- 进程持有者 fail-fast 与生命周期镜像 config_center（get_asset_center 同型）。
"""
import asyncio
from collections.abc import AsyncIterator

import pytest

import app.services.event_bus as event_bus_module
from app.core.config import SseSettings
from app.services.event_bus import EventBus, SseEvent


class FakeClock:
    """可手动推进的单调时钟假体：TTL 测试免真实等待。"""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _bus(
    clock: FakeClock, *, buffer_max_events: int = 20000, ttl_minutes: int = 30
) -> EventBus:
    """注入假时钟与受控 SSE settings（settings 消费路径即生产行为，仅环境值不同）。"""
    return EventBus(
        settings=SseSettings(
            event_ttl_minutes=ttl_minutes, buffer_max_events=buffer_max_events
        ),
        clock=clock,
    )


def _ev(thread_id: str = "th-1", event_type: str = "stage", **data) -> SseEvent:
    return SseEvent(thread_id=thread_id, event_type=event_type, data=data or {"k": "v"})


async def _next(gen: AsyncIterator[SseEvent], timeout: float = 2.0) -> SseEvent:
    """取下一条事件；超时抛 asyncio.TimeoutError（用于「无更多事件」断言）。"""
    return await asyncio.wait_for(gen.__anext__(), timeout)


async def _collect(
    gen: AsyncIterator[SseEvent], count: int, timeout: float = 2.0
) -> list[SseEvent]:
    return [await _next(gen, timeout) for _ in range(count)]


# ---- 发布 → 订阅 → seq 单调（线程级作用域）----


async def test_publish_assigns_monotonic_seq_and_replay_in_order():
    bus = _bus(FakeClock())
    stamped_seqs = [
        (await bus.publish("turn-a", _ev())).seq for _ in range(3)
    ]
    assert stamped_seqs == [1, 2, 3]  # 发布即分配线程内单调 seq

    gen = bus.subscribe("turn-a")  # last_seq 缺省 0 → 全量重放（api.md §7.1 首连）
    replayed = await _collect(gen, 3)
    assert [e.seq for e in replayed] == [1, 2, 3]  # 重放保序（SSE id 单调递增）
    await gen.aclose()


async def test_seq_scope_is_thread_not_turn():
    bus = _bus(FakeClock())
    s1 = await bus.publish("turn-1", _ev())
    s2 = await bus.publish("turn-1", _ev())
    s3 = await bus.publish("turn-2", _ev())  # 同线程新轮：续号不重起
    s4 = await bus.publish("turn-2", _ev())
    assert [s1.seq, s2.seq, s3.seq, s4.seq] == [1, 2, 3, 4]  # uq(thread_id, seq) 不撞

    other = await bus.publish("turn-9", _ev(thread_id="th-2"))
    assert other.seq == 1  # 换线程从 1 重起（seq 作用域 = 线程）


async def test_publish_returns_stamped_copy_input_untouched():
    bus = _bus(FakeClock())
    raw = _ev()
    stamped = await bus.publish("turn-a", raw)
    assert raw.seq is None and raw.ts is None  # 入参不被改写（frozen 副本盖章）
    assert stamped.seq == 1
    assert stamped.ts is not None and stamped.ts.tzinfo is not None  # UTC 盖章

    gen = bus.subscribe("turn-a")
    first = await _next(gen)
    assert first.seq == 1 and first.ts == stamped.ts  # 缓冲/重放持同一盖章副本
    await gen.aclose()


async def test_same_turn_with_different_thread_rejected():
    bus = _bus(FakeClock())
    await bus.publish("turn-a", _ev(thread_id="th-1"))
    with pytest.raises(RuntimeError):
        await bus.publish("turn-a", _ev(thread_id="th-2"))  # turn↔thread 绑定冲突属编程错误

    gen = bus.subscribe("turn-b")  # 调用不执行体（惰性）——首个 anext 才建通道
    with pytest.raises(asyncio.TimeoutError):
        await _next(gen, timeout=0.1)  # 启动：建 turn-b 通道（thread 待绑定）+ 挂跟随态
    await bus.publish("turn-b", _ev(thread_id="th-1"))  # 首 publish 绑定 thread
    with pytest.raises(RuntimeError):
        await bus.publish("turn-b", _ev(thread_id="th-2"))  # 绑定后同样拒绝换 thread


async def test_seed_thread_seq_supports_restart_reseed():
    bus = _bus(FakeClock())
    bus.seed_thread_seq("th-1", 46)  # B2：重启后按库内 max(seq) 种入，防撞持久化历史
    assert (await bus.publish("turn-new", _ev())).seq == 47
    bus.seed_thread_seq("th-1", 10)  # 种入旧值不回退计数器
    assert (await bus.publish("turn-new", _ev())).seq == 48


# ---- 按 last_seq 重放正确 ----


async def test_subscribe_replays_only_seq_greater_than_last_seq():
    bus = _bus(FakeClock())
    for _ in range(5):
        await bus.publish("turn-a", _ev())

    gen = bus.subscribe("turn-a", last_seq=3)
    fourth = await _next(gen)
    fifth = await _next(gen)
    assert [fourth.seq, fifth.seq] == [4, 5]  # 严格大于：seq=3 不重放（api.md §7.1）

    await bus.publish("turn-a", _ev())
    assert (await _next(gen)).seq == 6  # 重放尽 → 无缝转实时跟随
    await gen.aclose()


async def test_subscribe_at_latest_replays_nothing_then_follows():
    bus = _bus(FakeClock())
    for _ in range(2):
        await bus.publish("turn-a", _ev())

    # 探针用一次性订阅实例：wait_for 超时会取消并关闭生成器，不能复用
    probe = bus.subscribe("turn-a", last_seq=2)
    with pytest.raises(asyncio.TimeoutError):
        await _next(probe, timeout=0.1)  # last_seq=最新 → 重放为空，挂在跟随态

    gen = bus.subscribe("turn-a", last_seq=2)
    task = asyncio.ensure_future(_next(gen))
    await asyncio.sleep(0)
    await bus.publish("turn-a", _ev())
    assert (await task).seq == 3  # 仅实时新事件
    await gen.aclose()


async def test_replay_skips_evicted_head_silently():
    bus = _bus(FakeClock(), buffer_max_events=3)
    for _ in range(5):
        await bus.publish("turn-a", _ev())

    gen = bus.subscribe("turn-a", last_seq=1)  # 请求 seq>1，但 seq 1/2 已被淘汰
    replayed = await _collect(gen, 3)
    assert [e.seq for e in replayed] == [3, 4, 5]  # 从现存最早事件起重放、缺口静默跳过
    await gen.aclose()


async def test_live_follower_skips_gap_when_buffer_evicted():
    clock = FakeClock()
    bus = _bus(clock, buffer_max_events=2)
    gen = bus.subscribe("turn-a")  # 先订阅（游标 0）
    task = asyncio.ensure_future(_collect(gen, 2))
    await asyncio.sleep(0)  # 让订阅者登记并进入等待

    for _ in range(4):
        await bus.publish("turn-a", _ev())
    got = await task
    assert [e.seq for e in got] == [3, 4]  # 跟随态被淘汰甩开：同样静默跳缺口
    await gen.aclose()


# ---- TTL 过期清理（滑动续期 + 运行中轮永不过期 + 活跃订阅保护）----


async def test_cleanup_expires_idle_terminal_turn_without_subscribers():
    clock = FakeClock()
    bus = _bus(clock, ttl_minutes=30)
    await bus.publish("turn-a", _ev())
    await bus.publish("turn-a", _ev(event_type="done"))  # done → 终态
    clock.advance(30 * 60 + 1)
    bus.cleanup_expired()

    gen = bus.subscribe("turn-a")
    with pytest.raises(asyncio.TimeoutError):
        await _next(gen, timeout=0.1)  # 缓冲已回收：重放为空（合成 done 路径归 B2 SSE 端点）
    await gen.aclose()


async def test_running_turn_buffer_never_expires():
    clock = FakeClock()
    bus = _bus(clock, ttl_minutes=30)
    await bus.publish("turn-a", _ev())
    await bus.publish("turn-a", _ev(event_type="error"))  # error 非终态（api.md §7.2）
    clock.advance(10 * 3600)
    bus.cleanup_expired()

    gen = bus.subscribe("turn-a")
    replayed = await _collect(gen, 2)
    assert [e.seq for e in replayed] == [1, 2]  # 未终态 ∧ 缓冲非空 → 不回收
    await gen.aclose()


async def test_every_write_refreshes_sliding_ttl():
    clock = FakeClock()
    bus = _bus(clock, ttl_minutes=30)
    await bus.publish("turn-a", _ev(event_type="done"))  # t=0 终态
    clock.advance(29 * 60)
    await bus.publish("turn-a", _ev(event_type="followups"))  # done 后照常占号 + 刷新
    clock.advance(29 * 60)  # 距 done 58min，距最后写入 29min
    bus.cleanup_expired()

    gen = bus.subscribe("turn-a")
    replayed = await _collect(gen, 2)
    assert [e.seq for e in replayed] == [1, 2]  # 滑动续期：自最后写入起算（api.md §7.3 v1.5 增注）
    await gen.aclose()

    clock.advance(60 * 60 + 1)  # 距最后写入 > TTL
    bus.cleanup_expired()
    gen2 = bus.subscribe("turn-a")
    with pytest.raises(asyncio.TimeoutError):
        await _next(gen2, timeout=0.1)
    await gen2.aclose()


async def test_active_subscriber_blocks_expiration():
    clock = FakeClock()
    bus = _bus(clock, ttl_minutes=30)
    await bus.publish("turn-a", _ev(event_type="done"))
    gen = bus.subscribe("turn-a")  # 活跃订阅者（重放后挂跟随态）
    assert (await _next(gen)).seq == 1

    clock.advance(10 * 3600)
    bus.cleanup_expired()
    gen2 = bus.subscribe("turn-a")
    assert (await _next(gen2)).seq == 1  # 有活跃订阅 → 缓冲保留（增注 1「无活跃订阅」条件）
    await gen.aclose()
    await gen2.aclose()

    bus.cleanup_expired()
    gen3 = bus.subscribe("turn-a")
    with pytest.raises(asyncio.TimeoutError):
        await _next(gen3, timeout=0.1)  # 订阅者全部离开后才按 TTL 回收
    await gen3.aclose()


# ---- 缓冲上限淘汰 ----


async def test_buffer_cap_evicts_oldest():
    clock = FakeClock()
    bus = _bus(clock, buffer_max_events=3)
    for _ in range(5):
        await bus.publish("turn-a", _ev())

    gen = bus.subscribe("turn-a")  # last_seq=0 全量重放——缓冲只余最新 3 条
    replayed = await _collect(gen, 3)
    assert [e.seq for e in replayed] == [3, 4, 5]  # SSE_BUFFER_MAX_EVENTS 满则淘汰最旧

    await bus.publish("turn-a", _ev())
    assert (await _next(gen)).seq == 6  # 淘汰不影响实时跟随
    await gen.aclose()


# ---- 多订阅者 fan-out 互不干扰 ----


async def test_multiple_subscribers_each_receive_full_stream():
    clock = FakeClock()
    bus = _bus(clock)
    await bus.publish("turn-a", _ev())
    await bus.publish("turn-a", _ev())

    sub_a = bus.subscribe("turn-a")
    sub_b = bus.subscribe("turn-a")  # 晚到订阅者：独立重放同一份缓冲
    task_a = asyncio.ensure_future(_collect(sub_a, 4))
    task_b = asyncio.ensure_future(_collect(sub_b, 4))
    await asyncio.sleep(0)

    await bus.publish("turn-a", _ev())
    await bus.publish("turn-a", _ev())
    got_a, got_b = await asyncio.gather(task_a, task_b)
    assert [e.seq for e in got_a] == [1, 2, 3, 4]  # 各自收到全量事件流
    assert [e.seq for e in got_b] == [1, 2, 3, 4]
    await sub_a.aclose()
    await sub_b.aclose()


async def test_subscriber_disconnect_does_not_disturb_others():
    clock = FakeClock()
    bus = _bus(clock)
    await bus.publish("turn-a", _ev())
    await bus.publish("turn-a", _ev())
    sub_a = bus.subscribe("turn-a")
    sub_b = bus.subscribe("turn-a")
    assert [e.seq for e in await _collect(sub_a, 2)] == [1, 2]
    assert [e.seq for e in await _collect(sub_b, 2)] == [1, 2]

    await bus.publish("turn-a", _ev())
    assert (await _next(sub_a)).seq == 3
    await sub_a.aclose()  # A 断开：仅自身移除（ADR-003 v1.7 增注 3）

    await bus.publish("turn-a", _ev())
    # B 不受 A 断开影响：停止迭代期间的积压（seq 3）+ 新事件（seq 4）完整连续送达
    assert [e.seq for e in await _collect(sub_b, 2)] == [3, 4]
    await sub_b.aclose()


async def test_subscribe_before_first_publish_follows_live():
    clock = FakeClock()
    bus = _bus(clock)
    gen = bus.subscribe("turn-a")  # 订阅先行（排队轮：无业务事件仅心跳——api.md §7.1）
    task = asyncio.ensure_future(_next(gen))
    await asyncio.sleep(0)

    await bus.publish("turn-a", _ev())
    assert (await task).seq == 1
    await gen.aclose()


# ---- 进程持有者与生命周期（镜像 config_center 持有者模式）----


async def test_holder_fail_fast_then_start_and_close():
    original = event_bus_module._event_bus
    event_bus_module._event_bus = None
    try:
        with pytest.raises(RuntimeError):
            event_bus_module.get_event_bus()  # 未启动即取属编程错误（fail-fast）

        bus = await event_bus_module.start_event_bus()
        assert event_bus_module.get_event_bus() is bus
        await bus.close()
        with pytest.raises(RuntimeError):
            event_bus_module.get_event_bus()  # close 清持有者（与 AssetCenter.close 同型）
    finally:
        event_bus_module._event_bus = original
