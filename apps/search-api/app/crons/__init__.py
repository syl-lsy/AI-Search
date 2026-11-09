"""后台周期任务收口（注册表 + 一任务一文件；ARCH §3.1 v1.18 B 项 / ADR-001·010）。

调度口径：进程内 asyncio 周期任务（非 LangGraph Agent Server cron——本项目无
Agent Server，ADR-001/010）。main.py lifespan 启动时遍历 CRON_TASKS：为每项建
常驻周期循环（调用 coroutine_factory() → await → sleep(interval_seconds) 循环），
停机时对句柄 cancel() 并 await gather(..., return_exceptions=True) 优雅取消
（CancelledError 不外泄）。main.py 接线已于 B1 完成（注册 + 停机 gather 收口）；
B2 起 lifespan 启动序在此之上前置资产中心/EventBus/ThreadScheduler/seq 种入，
停机在 cron 取消之后逆序关停 EventBus 与资产中心（main.py lifespan docstring）。

CronTask = (name, interval_seconds, coroutine_factory)：
- name：任务名（日志与排查用，注册表内唯一）；
- interval_seconds：两次执行之间的间隔（秒）；
- coroutine_factory：零参工厂，每轮调度调用它取得一个新协程——协程对象只能
  await 一次，周期任务每轮必须重建（这是工厂而非协程对象的原因）。

与既有机制边界：启动钩子（main.py，一次性 seed/恢复/清理）与 KB worker
（kb_service 常驻队列消费者）不走本注册表（ARCH §3.1 crons 注）。

现有任务：
- trace_cleanup（每日）：Langfuse 过期 trace 清理，见 trace_cleanup.py；
- memory_ttl_sweeper（M5 规划）：记忆 TTL 衰减清扫，参数归 Agent Spec memory 节
  （nacos-config.md §3），落地时进本注册表。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.crons.trace_cleanup import (
    TRACE_CLEANUP_INTERVAL_SECONDS,
    cleanup_expired_traces,
)


@dataclass(frozen=True)
class CronTask:
    """单个周期任务注册项：(name, interval_seconds, coroutine_factory) 三元组。"""

    name: str
    interval_seconds: int
    coroutine_factory: Callable[[], Awaitable[None]]


async def _run_trace_cleanup() -> None:
    """注册表契约适配：批次结果已由 cleanup_expired_traces 内部落 CronLog，此处只消费返回值。"""
    await cleanup_expired_traces()


# 任务注册表：main.py lifespan 遍历注册为进程内 asyncio 周期任务（调度口径见模块 docstring）
CRON_TASKS: list[CronTask] = [
    CronTask(
        name="trace_cleanup",
        interval_seconds=TRACE_CLEANUP_INTERVAL_SECONDS,
        coroutine_factory=_run_trace_cleanup,
    ),
]
