"""数据访问层接缝：get_engine() 引擎单例 + 会话工厂 + get_db（事务边界）+ 事件钩子。

- 引擎懒加载单例，连接池参数集中于此（业务代码不得另建引擎）；
  连接池参数读 DB_ 配置组（DB_POOL_SIZE/DB_MAX_OVERFLOW，§7 默认 10/5）；
  密码经 SecretStr.get_secret_value() 显式取出，URL 用 sqlalchemy.URL.create
  拼装（密码含 @ / 等特殊字符时由 URL 层正确转义，不做 f-string 裸拼）；
- get_session_factory() 会话工厂懒加载单例（expire_on_commit=False），业务代码
  不得另建 sessionmaker；get_db() 是请求级 AsyncSession 出口——**事务边界统一在
  get_db**：service 层只 flush() 不 commit()，一请求一事务；FastAPI 依赖注入
  统一从 app/dependencies.py 再导出（api/ 不直捣 db 层）。
- 建引擎时注册 SQLAlchemy 事件监听：执行前记起点、执行后算耗时——超过
  `LOG_SLOW_QUERY_THRESHOLD`（毫秒，默认 200）记 DBLog.warning（慢查询），
  否则记 DBLog.debug（SQL执行，默认 `LOG_LEVEL=INFO` 时不落盘）。
- 事件钩子是**模块级函数**（课程为闭包）——便于单测直调，不必真连库。
- render_main_option_url() 把连接串转成 ini 安全值，供 migrations/env.py 写 alembic
  主配置项（configparser 基本插值契约见函数 docstring）。

（基座口径本文件为 app/model/engine.py，按 AI Agent 模板口径改名 db/session.py。）
"""
import time
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import URL, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

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


def render_main_option_url(url: URL) -> str:
    """连接串 → alembic 主配置项的 ini 安全值（configparser 基本插值契约）。

    alembic 的 Config 建立在 configparser 之上：`set_main_option` 写入时即做**基本
    插值校验**，裸 `%` 必须成对（`%%`）或带命名（`%(name)s`），否则当场抛
    `ValueError: invalid interpolation syntax in '<整条连接串>'`——错误消息内嵌连接串。
    密码含 `@ / : %` 时 SQLAlchemy 必做百分号编码（`%40/%2F/%3A/%25`），未转义直接 set
    即触发该异常且把可逆还原的连接串带进 traceback。

    故按 alembic 官方口径把 `%` 转义为 `%%`：configparser 读回时还原，round-trip 与
    `render_as_string(hide_password=False)` 原文逐字全等（连接串语义不变）。
    """
    return url.render_as_string(hide_password=False).replace("%", "%%")


def get_engine() -> AsyncEngine:
    global _engine

    if _engine is None:
        # URL.create 由 SQLAlchemy 负责转义特殊字符（f-string 裸拼在密码含 @ / 时碎裂）；
        # SecretStr.get_secret_value() 是密钥明文的唯一出口，且只在此处（连接串拼装）出现
        url = URL.create(
            "postgresql+asyncpg",
            username=db_settings.user,
            password=db_settings.password.get_secret_value(),
            host=db_settings.host,
            port=db_settings.port,
            database=db_settings.name,
        )
        _engine = create_async_engine(
            url,
            pool_size=db_settings.pool_size,
            max_overflow=db_settings.max_overflow,
            pool_pre_ping=True,
            echo=False,
        )
        _register_db_events(_engine)

    return _engine


_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """会话工厂懒加载单例（业务代码不得另建 sessionmaker）。

    expire_on_commit=False：commit 后 ORM 对象不过期——事务边界在 get_db、
    service 层方法在 commit 前就完成 DTO 转换，过期刷新只会引入意外的请求级 IO。
    """
    global _session_factory

    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(), expire_on_commit=False
        )

    return _session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """请求级 AsyncSession（FastAPI 依赖注入出口在 app/dependencies.py 再导出）。

    **事务边界统一在 get_db**：请求处理成功由这里 commit、异常时 rollback 后
    原样重抛——service 层只 flush() 不 commit()，保证一请求一事务、异常整体回滚。
    例外（短事务模式）：TurnRunner / KB worker 在请求内做非库耗时工作（外部 SDK /
    对象存储）前显式 commit()+close() 释放连接，自行管理事务边界。

    SQLAlchemy 会话懒连接：未执行 SQL 不发生 IO，故本依赖零请求也可安全进出。
    """
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
