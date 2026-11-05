"""unit 层示例：get_engine 懒加载单例 + SQL 事件钩子（慢查询日志）阈值分档。

`--cov=app` 会统计包内未导入的文件（0%），本测试把 db/session.py 拉满，
保证骨架开箱覆盖率过 80% 门禁。两个钩子是模块级函数（课程为闭包，此处有意改为
模块级便于单测直调）——不真连库即可覆盖正常查询 / 慢查询 / 缺配对起点三条路径。
"""
import time

from sqlalchemy import event

from app.core.config import db_settings, log_settings
from app.db.session import (
    _after_cursor_execute,
    _before_cursor_execute,
    _query_start_times,
    get_engine,
)


async def test_get_engine_returns_singleton(monkeypatch):
    # 补齐连接参数，避免空端口导致 URL 解析失败（create_async_engine 不建立连接）
    monkeypatch.setattr(db_settings, "host", "localhost")
    monkeypatch.setattr(db_settings, "port", "5432")
    monkeypatch.setattr(db_settings, "user", "user")
    monkeypatch.setattr(db_settings, "password", "password")
    monkeypatch.setattr(db_settings, "name", "test_db")

    engine = get_engine()
    assert engine is get_engine()  # 懒加载单例：重复调用返回同一对象
    assert engine.url.database == "test_db"
    # 慢查询钩子已真挂在 sync_engine 上（事件名拼错/挂错对象会静默失效，这里按「事件名 + 钩子函数」显式守卫）
    assert event.contains(
        engine.sync_engine, "before_cursor_execute", _before_cursor_execute
    )
    assert event.contains(
        engine.sync_engine, "after_cursor_execute", _after_cursor_execute
    )

    await engine.dispose()  # 释放连接池资源（尚未连接，无副作用）


# ---- SQL 事件钩子：慢查询阈值分档（直调模块级函数，不连库）----

def test_db_hooks_log_debug_for_normal_query(log_records):
    context = object()  # 执行上下文：钩子按 id(context) 配对起点
    _before_cursor_execute(None, None, "SELECT 1", (), context, False)
    _after_cursor_execute(None, None, "SELECT 1", (), context, False)

    record = log_records[-1]
    assert record["level"].name == "DEBUG"
    assert record["message"] == "SQL执行"
    assert record["extra"]["sql"] == "SELECT 1"
    assert record["extra"]["duration_ms"] >= 0
    assert id(context) not in _query_start_times  # 起点已配对删除，映射表不泄漏


def test_db_hooks_log_warning_for_slow_query(log_records):
    """起点改早 1 秒：耗时必定超过默认阈值 LOG_SLOW_QUERY_THRESHOLD=200ms。"""
    context = object()
    _query_start_times[id(context)] = time.perf_counter() - 1

    _after_cursor_execute(None, None, "SELECT pg_sleep(1)", "()", context, False)

    record = log_records[-1]
    assert record["level"].name == "WARNING"
    assert record["message"] == "慢查询"
    assert record["extra"]["duration_ms"] >= log_settings.slow_query_threshold
    assert record["extra"]["params"] == "()"
    assert id(context) not in _query_start_times


def test_db_hooks_ignore_unpaired_execution(log_records):
    """没有配对的执行前事件（如外部直接触发）：静默跳过，不误报耗时。"""
    _after_cursor_execute(None, None, "SELECT 1", (), object(), False)

    assert log_records == []
