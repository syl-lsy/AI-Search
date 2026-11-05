"""数据访问层接缝：get_engine() 引擎单例 + SQL 执行事件钩子（慢查询日志）。

- 引擎懒加载单例，连接池参数集中于此（业务代码不得另建引擎）；
  连接池参数读 DB_ 配置组（DB_POOL_SIZE/DB_MAX_OVERFLOW，§7 默认 10/5）；
- 建引擎时注册 SQLAlchemy 事件监听：执行前记起点、执行后算耗时——超过
  `LOG_SLOW_QUERY_THRESHOLD`（毫秒，默认 200）记 DBLog.warning（慢查询），
  否则记 DBLog.debug（SQL执行，默认 `LOG_LEVEL=INFO` 时不落盘）。
- 两个钩子是**模块级函数**（课程为闭包）——便于单测直调，不必真连库。

（基座口径本文件为 app/model/engine.py，按 AI Agent 模板口径改名 db/session.py。）
"""
import time
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import db_settings, log_settings
from app.core.logger.db_log import DBLog

_engine: AsyncEngine | None = None

# 执行中的 SQL 起点：key = SQLAlchemy 执行上下文 id（一次执行一进一出，配对着删除）
_query_start_times: dict[int, float] = {}


def _before_cursor_execute(
    conn: Any,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> None:
    """执行前：记下这条 SQL 的起点。"""
    _query_start_times[id(context)] = time.perf_counter()


def _after_cursor_execute(
    conn: Any,
    cursor: Any,
    statement: str,
    parameters: Any,
    context: Any,
    executemany: bool,
) -> None:
    """执行后：算耗时并按阈值分档——≥ 阈值记慢查询 warning，其余记 debug。"""
    start = _query_start_times.pop(id(context), None)
    if start is None:  # 没有配对的执行前事件（如外部直接触发）：静默跳过
        return
    duration = (time.perf_counter() - start) * 1000
    if duration >= log_settings.slow_query_threshold:
        DBLog(
            message="慢查询",
            sql=statement,
            params=str(parameters),
            duration_ms=round(duration, 2),
        ).warning()
    else:
        DBLog(
            message="SQL执行",
            sql=statement,
            params=str(parameters),
            duration_ms=round(duration, 2),
        ).debug()


def _register_db_events(engine: AsyncEngine) -> None:
    """把 SQL 执行钩子挂到引擎上（异步引擎统一经 sync_engine 触发事件）。"""
    event.listen(engine.sync_engine, "before_cursor_execute", _before_cursor_execute)
    event.listen(engine.sync_engine, "after_cursor_execute", _after_cursor_execute)


def get_engine() -> AsyncEngine:
    global _engine

    if _engine is None:
        url = f"postgresql+asyncpg://{db_settings.user}:{db_settings.password}@{db_settings.host}:{db_settings.port}/{db_settings.name}"
        _engine = create_async_engine(
            url,
            pool_size=db_settings.pool_size,
            max_overflow=db_settings.max_overflow,
            pool_pre_ping=True,
            echo=False,
        )
        _register_db_events(_engine)

    return _engine
