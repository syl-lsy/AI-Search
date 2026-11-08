"""unit 层示例：get_engine 懒加载单例 + SQL 事件钩子（慢查询日志）阈值分档。

`--cov=app` 会统计包内未导入的文件（0%），本测试把 db/session.py 拉满，
保证骨架开箱覆盖率过 80% 门禁。两个钩子是模块级函数（课程为闭包，此处有意改为
模块级便于单测直调）——不真连库即可覆盖正常查询 / 慢查询 / 缺配对起点三条路径。

末段覆盖会话工厂与 get_db（请求级 AsyncSession + 事务边界）：SQLAlchemy 会话
懒连接——未执行 SQL 前不发生任何 IO，commit/rollback 于干净会话是零网络操作，
故事务边界三条路径（成功提交 / 异常回滚并重抛 / 退出关会话）可全 mock 零网络验证。
"""
import time

import pytest
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import URL, event
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import db_settings, log_settings
from app.db import session as db_session
from app.db.session import (
    _after_cursor_execute,
    _before_cursor_execute,
    _query_start_times,
    get_db,
    get_engine,
    get_session_factory,
    render_main_option_url,
)


@pytest.fixture(autouse=True)
def _restore_db_singletons():
    """用后把 _engine / _session_factory 模块级单例归位为未创建态。

    本模块会向全局单例塞假值：test_get_engine_returns_singleton 先 monkeypatch
    db_settings 五个连接字段再调 get_engine()——monkeypatch 只撤销 db_settings 上的
    补丁，get_engine() 写进模块全局的 _engine 不会被恢复，假参数引擎（localhost:
    5432/test_db）会漏进后续测试。2026-11-07 全量门禁实锤：integration 层真库探针
    拿到这枚毒化单例 → db 组件 unavailable → /health/ready 503。这里在每个用例后
    强制置 None（unit 阶段无真实连接，无资源泄漏），单例由下一次 get_engine() 按
    当时真实配置重建。
    """
    yield
    db_session._engine = None
    db_session._session_factory = None


async def test_get_engine_returns_singleton(monkeypatch):
    # 补齐连接参数，避免空端口导致 URL 解析失败（create_async_engine 不建立连接）；
    # password 为 SecretStr（config 重写后密钥字段全打码），URL 侧显式 get_secret_value()
    monkeypatch.setattr(db_settings, "host", "localhost")
    monkeypatch.setattr(db_settings, "port", 5432)
    monkeypatch.setattr(db_settings, "user", "user")
    monkeypatch.setattr(db_settings, "password", SecretStr("password"))
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


# ---- 会话工厂与 get_db：请求级 AsyncSession + 事务边界在 get_db（零网络）----


def test_get_session_factory_is_singleton():
    """会话工厂懒加载单例：绑定 get_engine()，且 commit 后 ORM 对象不过期。"""
    factory = get_session_factory()
    assert factory is get_session_factory()  # 懒加载单例：重复调用返回同一对象
    assert factory.kw["bind"] is get_engine()
    assert factory.kw["expire_on_commit"] is False  # service 出 DTO 后 commit 不触发刷新 IO


async def test_get_db_yields_request_scoped_session_and_closes(monkeypatch):
    """get_db 产出请求级 AsyncSession；退出（aclose）时关闭会话、连接归还池。

    手动驱动生成器（__anext__ / aclose）而非 async-for：FastAPI 依赖拆除正是
    async contextmanager 的 __aexit__ 语义，async-for 体退出不会触发生成器收尾。
    """
    calls: list[str] = []
    gen = get_db()
    session = await gen.__anext__()
    assert isinstance(session, AsyncSession)

    real_close = session.close

    async def fake_close() -> None:
        calls.append("close")
        await real_close()

    monkeypatch.setattr(session, "close", fake_close)
    await gen.aclose()  # 正常拆除：async with __aexit__ → session.close()
    assert calls == ["close"]


async def test_get_db_commits_on_success(monkeypatch):
    """事务边界在 get_db：请求成功路径由 get_db 统一 commit（service 只 flush 不 commit）。

    成功拆除 = contextlib.__exit__(None, None, None) = 再驱动一次 __anext__
    （恢复生成器执行 → get_db 内 commit → 生成器正常耗尽，抛 StopAsyncIteration）。
    """
    calls: list[str] = []
    gen = get_db()
    session = await gen.__anext__()

    async def fake_commit() -> None:
        calls.append("commit")

    monkeypatch.setattr(session, "commit", fake_commit)
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()

    assert calls == ["commit"]


async def test_get_db_rolls_back_and_reraises_on_error(monkeypatch):
    """异常路径（FastAPI __aexit__ → athrow(exc)）：先 rollback 再原样重抛。

    全局异常处理器才能拿到原始异常按级落日志；rollback 失败不应吞掉业务异常。
    """
    calls: list[str] = []
    gen = get_db()
    session = await gen.__anext__()

    async def fake_rollback() -> None:
        calls.append("rollback")

    monkeypatch.setattr(session, "rollback", fake_rollback)
    with pytest.raises(RuntimeError, match="boom"):
        await gen.athrow(RuntimeError("boom"))

    assert calls == ["rollback"]


# ---- alembic 主配置项：连接串 ini 插值转义（评审 P2②）----


def test_render_main_option_url_escapes_percent_for_ini_roundtrip():
    """特殊字符密码：未转义串进 alembic 配置即 ValueError（含完整连接串），转义后 round-trip。

    固化缺陷形态——URL.render_as_string 把密码里的 @ / : % 百分号编码成 %40/%2F/%3A/%25，
    而 configparser 基本插值只认 %%(name)s 形态，set_main_option 时立即抛
    `ValueError: invalid interpolation syntax in '<完整连接串>'`——错误消息内嵌连接串，
    密码可逆还原。alembic 官方口径：交给 ini 前把 % 转义成 %%。

    只用假密码字面量，绝不读 .env 真值。
    """
    url = URL.create(
        "postgresql+asyncpg",
        username="user",
        password="p@ss/w:rd%1",  # 假密码字面量（测试夹具，非真实凭据）
        host="localhost",
        port=15432,
        database="test_db",
    )
    raw = url.render_as_string(hide_password=False)
    assert "%" in raw  # 密码已百分号编码（缺陷触发前提）

    unescaped = Config()  # 内存态，不落 ini 文件
    with pytest.raises(ValueError, match="interpolation"):
        unescaped.set_main_option("sqlalchemy.url", raw)

    escaped = Config()
    escaped.set_main_option("sqlalchemy.url", render_main_option_url(url))
    # configparser 读回时把 %% 还原成 %：与未转义原文逐字全等（round-trip）
    assert escaped.get_main_option("sqlalchemy.url") == raw
