"""unit 层：后台周期任务注册表（app/crons/__init__.py，ARCH §3.1 v1.18 B 项）。

覆盖点：
- 注册表收敛为 (name, interval_seconds, coroutine_factory) 三元组结构（CronTask）；
- trace_cleanup 任务已注册（每日 86400s 间隔——ARCH §3.1「每日周期任务」口径）；
- 任务名注册表内唯一（调度器按 name 记日志/排查，重名即歧义）；
- 协程工厂每轮调用取得一个新协程对象（协程只能 await 一次，周期任务必须工厂化）。
"""

import asyncio

import pytest

from app.crons import CRON_TASKS, CronTask


def _task_by_name(name: str) -> CronTask:
    return next(task for task in CRON_TASKS if task.name == name)


def test_registry_is_list_of_cron_task():
    """注册表项全部为 CronTask 三元组结构。"""
    assert isinstance(CRON_TASKS, list)
    assert CRON_TASKS, "注册表不应为空（trace_cleanup 已落地）"
    assert all(isinstance(task, CronTask) for task in CRON_TASKS)


def test_registry_task_names_unique():
    """任务名在注册表内唯一。"""
    names = [task.name for task in CRON_TASKS]
    assert len(names) == len(set(names))


def test_trace_cleanup_registered_daily():
    """trace_cleanup 已注册且每日执行一次（86400 秒）。"""
    task = _task_by_name("trace_cleanup")
    assert task.interval_seconds == 86400


def test_coroutine_factory_returns_fresh_awaitable():
    """协程工厂每次调用取得一个新协程对象（不能是同一个可复用协程）。"""
    task = _task_by_name("trace_cleanup")
    first = task.coroutine_factory()
    second = task.coroutine_factory()
    assert asyncio.iscoroutine(first)
    assert asyncio.iscoroutine(second)
    assert first is not second
    for coro in (first, second):
        coro.close()  # 不 await（真实现会读 settings 并出网）：仅验证工厂语义后关闭


def test_cron_task_is_frozen_triple():
    """CronTask 为冻结三元组：三字段名固定，且不可变（注册表运行期不被误改）。"""
    task = _task_by_name("trace_cleanup")
    assert set(("name", "interval_seconds", "coroutine_factory")) <= set(
        task.__dataclass_fields__
    )
    with pytest.raises(AttributeError):  # FrozenInstanceError 是 AttributeError 子类
        task.name = "renamed"
