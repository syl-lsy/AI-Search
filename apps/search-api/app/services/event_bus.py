"""轮次事件总线（B1 底座）：publish / subscribe 内存缓冲与多订阅者扇出（ADR-003）。

契约锚点（实现语义逐条对齐，勿凭直觉改）：
- **seq 线程级单调**：data-model.md §1.4「seq 生成：EventBus 发布时取
  max(seq) per thread + 1（每线程串行发布，天然无竞争）」+ uq(thread_id, seq)
  唯一索引——计数器按 thread_id 作用域维护，缓冲按 turn_id 键控（ARCHITECTURE
  §6.1「EventBus 维护 turn_id → {buffer, subscribers: set}」）；
- **订阅语义**（api.md §7.1/§7.3）：先重放缓冲中 seq > last_seq 的事件再实时跟随；
  last_seq=0（首连无 Last-Event-ID）全量重放；订阅先行（排队轮无业务事件）合法；
- **缓冲上限**：SSE_BUFFER_MAX_EVENTS（deque maxlen，满则淘汰最旧）；淘汰缺口从
  现存缓冲最早事件起重放、静默跳过不报错——终态一致性由 done → GET /turns/{id}
  的 REST 对账兜底（ADR-003 v1.7 增注 2）；
- **滑动 TTL**：SSE_EVENT_TTL_MINUTES，每写入一个事件刷新 last_activity；仅回收
  「已终态（done 已过）∨ 空缓冲 ∧ 无活跃订阅 ∧ 静止超时」——运行中轮（running/
  queued）缓冲永不过期，error 非终态（api.md §7.2：error 后必跟 done）
  （ADR-003 v1.7 增注 1 / api.md §7.3 v1.5 增注）；
- **done 后事件**（followups，ADR-003 v1.16）：照常占号、入缓冲、刷新 last_activity
  ——publish 不因终态拒绝写入。

生命周期：main.py lifespan（B2 接线）调 start_event_bus()；api/ 层经
app/dependencies.py 的 get_event_bus 消费（未启动即取 fail-fast，与
config_center.get_asset_center 同型持有者）。TurnRunner（B2）进程重启后须对既有
线程调 seed_thread_seq(thread_id, 库内 max(seq))——内存计数器不跨进程存活，
不种入则新轮 seq 与 turn_events 持久化历史撞 uq(thread_id, seq)。

单进程内存实现（ADR-003 后果：多实例部署需外置总线，与 ADR-001 同一扩展触发点）。
全异步接口；订阅者迭代协议：async for 消费，断开/停止即 aclose（框架对
StreamingResponse 断连自动关闭生成器）——finally 注销订阅者，仅移除自身。
"""
import asyncio
from collections import deque
from collections.abc import AsyncGenerator, Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from itertools import islice
from time import monotonic
from typing import Any

from app.core.config import SseSettings, sse_settings

__all__ = [
    "TERMINAL_EVENT_TYPE",
    "EventBus",
    "SseEvent",
    "get_event_bus",
    "start_event_bus",
]

# 轮终态标记：done 事件到达即置通道终态（清扫资格判据）。
# api.md §7.2：error 非终态（error 后必跟 done）；done 后仍可发布（followups）。
TERMINAL_EVENT_TYPE = "done"

# 后台清扫周期（秒）：环境无关节奏值，代码常量判据；TTL 判定粒度 ≤ 本周期。
SWEEP_INTERVAL_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class SseEvent:
    """SSE 事件（api.md §7.1 data 通用信封的进程内形态）。

    thread_id/event_type/data 为发布方必填；seq/ts 由 EventBus.publish 盖章
    （frozen 副本，入参不被改写）。data 只承载类型字段——信封字段 seq/turn_id/ts
    由 SSE 端点（B2）装配，turn_id 以订阅流为准不在事件内冗余。
    payload dict 视为只读：事件跨订阅者共享同一实例。
    """

    thread_id: str
    event_type: str
    data: dict[str, Any] = field(default_factory=dict)
    seq: int | None = None  # publish 时分配（SSE id: 同源，线程内单调递增）
    ts: datetime | None = None  # publish 时盖章（UTC；重放不刷新——ts 为发布时刻）


@dataclass(slots=True, eq=False)  # eq=False：按对象恒等入 set（订阅者集合语义）
class _Subscriber:
    """订阅句柄：cursor = 下一条待取事件在通道累计写入计数中的位置（与淘汰解耦）。"""

    cursor: int


@dataclass(slots=True)
class _TurnChannel:
    """单轮通道：缓冲 + 订阅者集合 + 终态/活动标记（ARCHITECTURE §6.1 订阅者集合语义）。"""

    thread_id: str | None  # 首个 publish 绑定；订阅先行创建时为 None
    buffer: deque[SseEvent]  # maxlen = SSE_BUFFER_MAX_EVENTS（append 满自动淘汰最旧）
    cond: asyncio.Condition
    subscribers: set[_Subscriber]
    appended: int = 0  # 累计写入数（订阅游标基准——淘汰使 deque 下标漂移，不能按下标跟随）
    terminal: bool = False  # done 事件后置位（ADR-003 v1.7 增注 1 清扫判据）
    closed: bool = False  # close_channel 显式关闭置位（订阅流结束信号；done 不置它）
    last_activity: float = 0.0  # 滑动 TTL 基准（clock() 每写入刷新）


class EventBus:
    """轮次事件总线：发布盖章入缓冲 + 扇出；订阅重放 + 跟随；TTL 清扫。

    settings/clock 经构造注入（单测注入假体；生产缺省读 SSE_ 组单例与单调钟）。
    线程串行发布（ThreadScheduler 每线程同时一轮）+ 单事件循环——计数器与缓冲
    变更均无跨任务竞争，Condition 仅用于订阅者等待/唤醒。
    线程 seq 计数器随进程存活不回收（int 级开销，M1 单用户规模可忽略）。
    """

    def __init__(
        self,
        *,
        settings: SseSettings | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._settings = settings or sse_settings
        self._clock = clock
        self._channels: dict[str, _TurnChannel] = {}
        self._thread_last_seq: dict[str, int] = {}  # thread_id → 已分配最大 seq
        self._sweeper: asyncio.Task[None] | None = None

    # ---- 发布 ----

    async def publish(self, turn_id: str, event: SseEvent) -> SseEvent:
        """发布事件：线程级 seq 盖章（max+1）→ 写缓冲 → 扇出。返回盖章副本。

        - seq 按线程作用域分配（data-model.md §1.4），缓冲按 turn_id 键控；
        - 每写入刷新 last_activity（滑动 TTL）；done 置通道终态；
        - 缓冲满由 deque(maxlen) 淘汰最旧（SSE_BUFFER_MAX_EVENTS）；
        - 同 turn 重复绑定不同 thread 属编程错误，fail-fast（RuntimeError）。
        """
        channel = self._channel_for_publish(turn_id, event.thread_id)
        seq = self._thread_last_seq.get(event.thread_id, 0) + 1
        self._thread_last_seq[event.thread_id] = seq
        stamped = replace(event, seq=seq, ts=datetime.now(UTC))

        channel.appended += 1
        channel.buffer.append(stamped)
        channel.last_activity = self._clock()
        if event.event_type == TERMINAL_EVENT_TYPE:
            channel.terminal = True

        async with channel.cond:  # 持锁唤醒（notify 要求持锁）；缓冲变更本身无竞争
            channel.cond.notify_all()
        return stamped

    def _channel_for_publish(self, turn_id: str, thread_id: str) -> _TurnChannel:
        channel = self._channels.get(turn_id)
        if channel is None:
            channel = _TurnChannel(
                thread_id=thread_id,
                buffer=deque(maxlen=self._settings.buffer_max_events),
                cond=asyncio.Condition(),
                subscribers=set(),
                last_activity=self._clock(),
            )
            self._channels[turn_id] = channel
        elif channel.thread_id is not None and channel.thread_id != thread_id:
            raise RuntimeError(
                f"turn {turn_id} 已绑定 thread {channel.thread_id}，"
                f"拒绝以 thread {thread_id} 重复发布（turn↔thread 绑定冲突）"
            )
        if channel.thread_id is None:  # 订阅先行建的通道：首个 publish 绑定 thread
            channel.thread_id = thread_id
        return channel

    # ---- 订阅 ----

    async def subscribe(
        self, turn_id: str, last_seq: int = 0
    ) -> AsyncGenerator[SseEvent, None]:
        """订阅轮事件流：重放缓冲中 seq > last_seq 者 → 实时跟随，直至消费方停止。

        - 惰性启动：首次 __anext__ 才登记订阅者并取重放快照（登记与快照间无 await，
          无缝隙）；订阅先行（轮无事件）合法，挂在跟随态等待；
        - last_seq 早于现存缓冲最早事件时缺口静默跳过（ADR-003 v1.7 增注 2）；
        - 各订阅者独立游标互不干扰；断开/停止迭代即从订阅者集合移除自身；
        - 不因 done 自动结束——done 后保活窗口（followups，ADR-003 v1.16）与
          流关闭时机归 SSE 端点（B2）裁决，消费方自行 break/aclose（async 生成器，
          注解即 AsyncGenerator——aclose 是消费方关闭协议的一部分）。
        """
        channel = self._channels.get(turn_id)
        if channel is None:  # 订阅先行：先建空通道（thread 待首个 publish 绑定）
            channel = _TurnChannel(
                thread_id=None,
                buffer=deque(maxlen=self._settings.buffer_max_events),
                cond=asyncio.Condition(),
                subscribers=set(),
                last_activity=self._clock(),
            )
            self._channels[turn_id] = channel
        sub = _Subscriber(cursor=channel.appended)
        channel.subscribers.add(sub)
        try:
            # 重放快照（同步取，免淘汰竞态）：登记游标之前已缓冲且 seq 更大者
            replay = [
                ev
                for ev in tuple(channel.buffer)
                if ev.seq is not None and ev.seq > last_seq
            ]
            for event in replay:
                yield event
            while True:
                async with channel.cond:
                    while sub.cursor >= channel.appended and not channel.closed:
                        await channel.cond.wait()
                    if channel.closed and sub.cursor >= channel.appended:
                        break  # 通道显式关闭（close_channel）→ 正常结束（finally 注销）
                    batch = self._drain(channel, sub)  # 出锁前取段（同步无 await）
                for event in batch:
                    yield event
        finally:
            channel.subscribers.discard(sub)  # 断开仅移除自身（ADR-003 v1.7 增注 3）

    @staticmethod
    def _drain(channel: _TurnChannel, sub: _Subscriber) -> list[SseEvent]:
        """取订阅者游标之后的新事件并推进游标；淘汰缺口钳到现存最早事件（静默跳过）。"""
        start = channel.appended - len(channel.buffer)  # 现存缓冲最早事件的位置
        read_from = max(sub.cursor, start)
        # deque 无随机访问，islice 惰性走段（免整表拷贝；缓冲上限 20000）
        batch = list(islice(channel.buffer, read_from - start, len(channel.buffer)))
        sub.cursor = channel.appended
        return batch

    # ---- 缓冲治理（TTL 清扫）----

    def cleanup_expired(self) -> int:
        """回收过期缓冲：终态 ∨ 空缓冲 ∧ 无活跃订阅 ∧ 静止超时。返回回收轮数。

        运行中轮（running/queued）与非空未终态缓冲永不过期（ADR-003 v1.7 增注 1）；
        空缓冲无可保护内容，一并回收防泄漏（订阅先行轮断开后的残壳）。
        后台周期任务（start）与本入口共用；单测/运维可直接调用。
        """
        ttl_seconds = self._settings.event_ttl_minutes * 60
        now = self._clock()
        expired = [
            turn_id
            for turn_id, channel in self._channels.items()
            if not channel.subscribers
            and now - channel.last_activity > ttl_seconds
            and (channel.terminal or not channel.buffer)
        ]
        for turn_id in expired:
            del self._channels[turn_id]
        return len(expired)

    def seed_thread_seq(self, thread_id: str, last_seq: int) -> None:
        """种入线程 seq 基线（B2：进程重启后按 turn_events 库内 max(seq) 调用）。

        计数器只前进不回退（max 语义），重复种入旧值无副作用。
        """

        current = self._thread_last_seq.get(thread_id, 0)
        self._thread_last_seq[thread_id] = max(current, last_seq)

    def terminal_seq(self, turn_id: str) -> int | None:
        """该轮终态事件（done）的 seq；通道不存在或未终态返回 None。

        SSE 端点（B2 最小版）的「客户端已持有 done」判定依据：重连 Last-Event-ID
        ≥ 本值时不重发 done、直接关闭（api.md §7.1 512-514 行的重放对偶分支——
        done 在缓冲中只发一次）。close_channel 关闭的通道（error 路径）缓冲无
        done 事件 → 返回 None（订阅流随 closed 自然结束，不依赖本判定）。
        """
        channel = self._channels.get(turn_id)
        if channel is None or not channel.terminal:
            return None
        for event in reversed(channel.buffer):
            if event.event_type == TERMINAL_EVENT_TYPE and event.seq is not None:
                return event.seq
        return None

    async def close_channel(self, turn_id: str) -> bool:
        """显式关闭轮通道并唤醒订阅者结束（B2：TurnRunner 失败路径调用）。

        - **不发事件、不落库**——api.md §7.2 error 条目「error 后……或直接关闭
          （无内容时）」的进程内实现；订阅者消费完既有缓冲后收到结束信号，
          async for 正常退出（不悬挂）；
        - 同时置 terminal：failed 轮无 done 事件、terminal 永不置位 → 缓冲永不清收
          的泄漏由此修复（cleanup_expired 判据恢复可回收）；
        - **done 路径不走本方法**：done 后保活窗口（followups，ADR-003 v1.16）依赖
          「terminal 不结束订阅流」契约——关闭时机归 SSE 端点裁决（172 行 docstring）；
        - 幂等：重复关闭无副作用；通道不存在（该轮从未发布/订阅）no-op 返回 False。
        """
        channel = self._channels.get(turn_id)
        if channel is None:
            return False
        channel.closed = True
        channel.terminal = True
        async with channel.cond:  # 持锁唤醒挂起的订阅者
            channel.cond.notify_all()
        return True

    # ---- 生命周期（后台周期清扫）----

    async def start(self) -> "EventBus":
        """启动后台清扫任务（幂等；main.py lifespan / start_event_bus 调用）。"""
        if self._sweeper is None:
            self._sweeper = asyncio.create_task(
                self._sweep_loop(), name="event-bus-sweeper"
            )
        return self

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            self.cleanup_expired()

    async def close(self) -> None:
        """停清扫任务；若进程持有者即本实例则一并清除（镜像 AssetCenter.close）。"""
        if self._sweeper is not None:
            self._sweeper.cancel()
            with suppress(asyncio.CancelledError):
                await self._sweeper
            self._sweeper = None
        if _event_bus is self:
            _set_event_bus(None)


# ---- 进程级持有者（main.py lifespan 启动时注册；dependencies 经此取）----

_event_bus: EventBus | None = None


def _set_event_bus(bus: EventBus | None) -> None:
    global _event_bus
    _event_bus = bus


def get_event_bus() -> EventBus:
    """取进程级事件总线（未启动时 fail-fast——组装层尚未 start 即消费属编程错误）。"""
    if _event_bus is None:
        raise RuntimeError(
            "EventBus 尚未启动：请在 lifespan 启动钩子调用 start_event_bus()"
        )
    return _event_bus


async def start_event_bus(*, settings: SseSettings | None = None) -> EventBus:
    """启动事件总线并注册进程级持有者（main.py lifespan 调用；B2 接线）。

    settings 仅供单测注入；生产调用方一律不传（缓冲上限/TTL 走 SSE_ 组单例）。
    """
    bus = EventBus(settings=settings)
    await bus.start()
    _set_event_bus(bus)
    return bus
