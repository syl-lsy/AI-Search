"""unit 层：main.py lifespan 收口（启动钩子孤儿轮清理 + 周期任务注册/优雅取消）。

覆盖点（data-model.md §4 启动钩子 / api.md §7.3 第 4 条 / ARCH §3.1 main.py 注）：
- 孤儿轮清理（启动钩子一次性）：running/queued → failed（error=进程重启语义），用
  turns ORM 实体级变更（select 表达式全参数绑定）；独立 AsyncSession 自管事务
  （lifespan 无请求级 get_db，短事务口径）；DB 路径走 mock session 假体——零网络零真实库；
- error 载荷契约形态：{code, message, stage_key}（data-model.md §1.3 turns.error），
  code 取错误码表兜底 500001（api.md §0 冻结表无「进程重启」专属码，不私增）；
- 失败语义：清理失败（DB 不可达）异常自钩子外抛、但 lifespan 捕获记日志不阻塞
  启动——恢复型 best-effort（幂等可重入，下次重启补做；就绪探针如实报 503）；
- 周期任务注册：CRON_TASKS 注册表 → 进程内 asyncio 周期任务，进入 lifespan 即注册
  且立即执行首轮（crons/__init__.py 调度口径：factory() → await → sleep 循环）；
- 任务异常不中断循环：记日志后进入下个周期重试（trace_cleanup 查询失败语义按此消费）；
- 停机优雅取消：yield 后逐个 cancel + gather(return_exceptions=True)——CancelledError
  送达任务、不外泄（crons/__init__.py 注册表 docstring 的收口口径）；
- uvicorn 标准库 logger 在 lifespan 内保持关闭（dictConfig 重置后的再关闭，回归守卫）。

外部依赖全 mock（DB 走 session 假体、CRON_TASKS 换假任务）——零网络。
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy.sql.selectable import Select

from app import main
from app.crons import CronTask
from app.exception import ConfigCenterException


# ---- 假体：最小 AsyncSession / 实体行（零网络，只记录调用面）----


class _FakeTurn:
    """最小 turns 实体行假体：只暴露清理会触碰的两个字段。"""

    def __init__(self, status: str) -> None:
        self.status = status
        self.error: object = None


class _FakeScalars:
    def __init__(self, turns: list[_FakeTurn]) -> None:
        self._turns = turns

    def all(self) -> list[_FakeTurn]:
        return self._turns


class _FakeSession:
    """最小异步会话假体：记录 scalars 语句与 commit/close 调用，可选注入异常。

    scalars() 直接返回实体行序列（实现走 AsyncSession.scalars() 实体级变更，
    而非 execute().scalars() 两段式）——假体同构，仅暴露实现消费的最小面。
    """

    def __init__(
        self, turns: list[_FakeTurn] | None = None, exc: Exception | None = None
    ) -> None:
        self.statements: list[object] = []
        self.commits = 0
        self.closed = False
        self._turns = turns if turns is not None else []
        self._exc = exc

    async def scalars(self, statement: object) -> _FakeScalars:
        if self._exc is not None:
            raise self._exc
        self.statements.append(statement)
        return _FakeScalars(list(self._turns))

    async def commit(self) -> None:
        self.commits += 1

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *args: object) -> bool:
        self.closed = True
        return False


def _patch_session_factory(
    monkeypatch: pytest.MonkeyPatch, session: _FakeSession
) -> None:
    """把 main.get_session_factory 换成恒返假会话的工厂（mock DB 路径的唯一缝）。"""
    monkeypatch.setattr(main, "get_session_factory", lambda: lambda: session)


async def _noop_cleanup() -> int:
    return 0


# ---- 孤儿轮清理（mock DB 路径）----


async def test_cleanup_orphan_turns_updates_running_queued_to_failed(
    monkeypatch: pytest.MonkeyPatch,
):
    """running/queued → failed：turns ORM 实体级变更（select 表达式全参数绑定）。

    查询侧 WHERE 状态集 = {running, queued}（data-model.md §4，v1.5 起 queued
    同口径）；变更侧 = status=failed + error 载荷（进程重启语义）；一次 commit、
    会话关闭、返回行数。绑定参数断言按值收集——不钉 SQLAlchemy 自动生成的参数名。
    """
    session = _FakeSession(
        turns=[_FakeTurn("running"), _FakeTurn("queued"), _FakeTurn("running")]
    )
    _patch_session_factory(monkeypatch, session)

    cleaned = await main._cleanup_orphan_turns()

    assert cleaned == 3
    assert session.commits == 1
    assert session.closed is True
    assert len(session.statements) == 1

    stmt = session.statements[0]
    assert isinstance(stmt, Select)
    assert stmt.column_descriptions[0]["entity"] is main.Turn  # turns ORM 实体

    compiled = stmt.compile()
    assert str(compiled).startswith("SELECT")  # 实体查询，非字符串拼接形态
    # WHERE 状态集：IN 列表可能渲染为单列表参数或展开多参数，拍平后按值断言
    flat_params: list[object] = []
    for value in compiled.params.values():
        if isinstance(value, (list, tuple)):
            flat_params.extend(value)
        else:
            flat_params.append(value)
    assert "running" in flat_params and "queued" in flat_params

    for turn in session._turns:  # 变更侧：实体字段就地置终态
        assert turn.status == "failed"
        assert turn.error is main._ORPHAN_TURN_ERROR


def test_orphan_turn_error_payload_matches_contract_shape():
    """error 载荷 = {code, message, stage_key}（turns.error 契约形态）。

    code 取错误码表兜底 500001（api.md §0 冻结表无「进程重启」专属码，不私增）；
    message 用户可读中文、指向重问（api.md §7.3 第 4 条「用户重问」口径）。
    """
    assert set(main._ORPHAN_TURN_ERROR) == {"code", "message", "stage_key"}
    assert main._ORPHAN_TURN_ERROR["code"] == "500001"
    assert main._ORPHAN_TURN_ERROR["message"]  # 非空用户可读消息


async def test_cleanup_orphan_turns_propagates_db_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    """DB 失败自钩子外抛（钩子保持纯语义），由 lifespan 统一捕获——不 commit。"""
    session = _FakeSession(exc=RuntimeError("db down"))
    _patch_session_factory(monkeypatch, session)

    with pytest.raises(RuntimeError, match="db down"):
        await main._cleanup_orphan_turns()
    assert session.commits == 0


# ---- lifespan 接线：B2 启动序基础设施假体（资产中心 / EventBus / seq 种入）----


def _patch_lifespan_infra(
    monkeypatch: pytest.MonkeyPatch,
    *,
    events: list[str] | None = None,
    asset_exc: Exception | None = None,
    bus_exc: Exception | None = None,
) -> list[str]:
    """patch main 命名空间的三项 B2 接线并按调用序记录（启动/关停顺序断言的数据面）。

    返回 events 记录：asset_center:start / event_bus:start / thread_scheduler:start /
    seed_seq / bus:close / center:close；关停逆序（cron 取消 → bus:close →
    center:close）由 cron 假体向同一列表追加 "cron:cancelled" 佐证。
    """
    log = events if events is not None else []

    class _FakeCenter:
        async def close(self) -> None:
            log.append("center:close")

    class _FakeBus:
        async def close(self) -> None:
            log.append("bus:close")

    async def _start_asset_center(**_kwargs: object) -> _FakeCenter:
        if asset_exc is not None:
            raise asset_exc
        log.append("asset_center:start")
        return _FakeCenter()

    async def _start_event_bus(**_kwargs: object) -> _FakeBus:
        if bus_exc is not None:
            raise bus_exc
        log.append("event_bus:start")
        return _FakeBus()

    def _start_thread_scheduler() -> object:
        log.append("thread_scheduler:start")
        return object()

    async def _seed_seq_from_db() -> None:
        log.append("seed_seq")

    monkeypatch.setattr(main, "start_asset_center", _start_asset_center)
    monkeypatch.setattr(main, "start_event_bus", _start_event_bus)
    monkeypatch.setattr(main, "start_thread_scheduler", _start_thread_scheduler)
    monkeypatch.setattr(main, "seed_seq_from_db", _seed_seq_from_db)
    return log


# ---- lifespan 接线：启动钩子 + uvicorn 再关闭 ----


async def test_lifespan_runs_orphan_cleanup_before_yield_and_silences_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
):
    """进入 lifespan：孤儿轮清理在 yield 前完成（启动即恢复一致状态）；
    uvicorn 标准库 logger 保持关闭（dictConfig 重置后的再关闭回归守卫）。"""
    events: list[str] = []

    async def _stub() -> int:
        events.append("cleanup")
        return 0

    monkeypatch.setattr(main, "_cleanup_orphan_turns", _stub)
    monkeypatch.setattr(main, "CRON_TASKS", [])
    _patch_lifespan_infra(monkeypatch)
    logging.getLogger("uvicorn.error").disabled = False  # 模拟被 uvicorn 配置重置
    logging.getLogger("uvicorn.access").disabled = False

    async with main.app.router.lifespan_context(main.app):
        assert events == ["cleanup"]
        assert logging.getLogger("uvicorn.error").disabled is True
        assert logging.getLogger("uvicorn.access").disabled is True


async def test_lifespan_survives_orphan_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    """清理失败不阻塞启动：lifespan 捕获记日志，应用照常起跑（best-effort 恢复，
    幂等可重入、下次重启补做；就绪探针如实反映 db 状态，不在此重复判定）。"""

    async def _boom() -> int:
        raise RuntimeError("db down")

    monkeypatch.setattr(main, "_cleanup_orphan_turns", _boom)
    monkeypatch.setattr(main, "CRON_TASKS", [])
    _patch_lifespan_infra(monkeypatch)

    async with asyncio.timeout(5):
        async with main.app.router.lifespan_context(main.app):
            pass  # 能进上下文即启动未被钩子失败阻断


# ---- lifespan 接线：B2 启动序（资产中心 → EventBus → 调度器 → seq 种入）与关停逆序 ----


async def test_lifespan_wires_infra_in_order_and_teardown_reverses(
    monkeypatch: pytest.MonkeyPatch,
):
    """启动序：资产中心 → EventBus → ThreadScheduler → seed_seq → 孤儿轮清理（yield 前）；
    关停逆序：EventBus.close → 资产中心.close（ThreadScheduler 无常驻任务零收口）。"""
    events: list[str] = []
    _patch_lifespan_infra(monkeypatch, events=events)
    monkeypatch.setattr(main, "_cleanup_orphan_turns", _noop_cleanup)
    monkeypatch.setattr(main, "CRON_TASKS", [])

    async with asyncio.timeout(5):
        async with main.app.router.lifespan_context(main.app):
            assert events == [
                "asset_center:start",
                "event_bus:start",
                "thread_scheduler:start",
                "seed_seq",
            ]
    assert events == [
        "asset_center:start",
        "event_bus:start",
        "thread_scheduler:start",
        "seed_seq",
        "bus:close",
        "center:close",
    ]


async def test_lifespan_shutdown_order_cron_before_bus_before_center(
    monkeypatch: pytest.MonkeyPatch,
):
    """关停逆序（ask 口径）：cron 取消 → event_bus.close() → asset_center.close()。

    先等 cron 任务起跑再退出——取消未起跑的任务体不执行（except 不触发），
    无法佐证「取消信号送达任务体」的优雅取消语义。"""
    events: list[str] = []
    _patch_lifespan_infra(monkeypatch, events=events)
    started = asyncio.Event()

    async def _sleepy() -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            events.append("cron:cancelled")  # 取消信号送达任务体（优雅取消语义）
            raise

    monkeypatch.setattr(main, "_cleanup_orphan_turns", _noop_cleanup)
    monkeypatch.setattr(
        main,
        "CRON_TASKS",
        [CronTask(name="unit_fake", interval_seconds=3600, coroutine_factory=_sleepy)],
    )

    async with asyncio.timeout(5):
        async with main.app.router.lifespan_context(main.app):
            await asyncio.wait_for(started.wait(), timeout=2)
    assert events[-3:] == ["cron:cancelled", "bus:close", "center:close"]


async def test_lifespan_asset_center_failure_propagates(monkeypatch: pytest.MonkeyPatch):
    """启动失败不静默吞掉：production L4 的 ConfigCenterException（503001）拒启动语义——
    异常从 lifespan 上抛（FastAPI 启动失败退出），不进入应用运行态。"""
    events: list[str] = []
    _patch_lifespan_infra(
        monkeypatch, events=events, asset_exc=ConfigCenterException("配置中心不可达且无缓存")
    )
    with pytest.raises(ConfigCenterException):
        async with main.app.router.lifespan_context(main.app):
            pass  # pragma: no cover —— 启动失败不应进入运行态
    assert events == []  # fail-fast：后续组件零启动


async def test_lifespan_bus_failure_still_closes_asset_center(
    monkeypatch: pytest.MonkeyPatch,
):
    """部分启动失败：已启动的资产中心仍被收口（不泄漏客户端），异常原样上抛。"""
    events: list[str] = []
    _patch_lifespan_infra(monkeypatch, events=events, bus_exc=RuntimeError("bus down"))
    with pytest.raises(RuntimeError, match="bus down"):
        async with main.app.router.lifespan_context(main.app):
            pass  # pragma: no cover —— 启动失败不应进入运行态
    assert events == ["asset_center:start", "center:close"]


# ---- lifespan 接线：周期任务注册 / 优雅取消 / 失败重试 ----


def _patch_cron_tasks(
    monkeypatch: pytest.MonkeyPatch,
    factory: Callable[[], Awaitable[None]],
    interval_seconds: int = 3600,
) -> None:
    _patch_lifespan_infra(monkeypatch)
    monkeypatch.setattr(main, "_cleanup_orphan_turns", _noop_cleanup)
    monkeypatch.setattr(
        main,
        "CRON_TASKS",
        [
            CronTask(
                name="unit_fake",
                interval_seconds=interval_seconds,
                coroutine_factory=factory,
            )
        ],
    )


async def test_lifespan_registers_cron_tasks_with_immediate_first_run(
    monkeypatch: pytest.MonkeyPatch,
):
    """进入 lifespan 即注册周期任务并立即执行首轮（factory() → await → sleep 口径，
    注册表 docstring 的调度语义；interval 3600s 不可能在测试内二次触发）。"""
    first_run = asyncio.Event()

    async def _job() -> None:
        first_run.set()

    _patch_cron_tasks(monkeypatch, _job)

    async with asyncio.timeout(5):
        async with main.app.router.lifespan_context(main.app):
            await asyncio.wait_for(first_run.wait(), timeout=2)


async def test_lifespan_cancels_cron_tasks_gracefully_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
):
    """停机优雅取消：CancelledError 送达任务体内、不外泄（gather 收口后上下文才退）。"""
    started = asyncio.Event()
    state: dict[str, bool] = {"cancelled": False}

    async def _sleepy() -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    _patch_cron_tasks(monkeypatch, _sleepy)

    async with asyncio.timeout(5):
        async with main.app.router.lifespan_context(main.app):
            await asyncio.wait_for(started.wait(), timeout=2)
    assert state["cancelled"] is True


async def test_cron_loop_survives_task_exception_and_retries_next_cycle(
    monkeypatch: pytest.MonkeyPatch,
):
    """任务异常不中断循环：记日志后进入下个周期重试（interval=0 → sleep(0) 让出
    事件循环即调度下一轮，行为与真实间隔一致、测试零等待）。"""
    calls: list[int] = []

    async def _flaky() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    _patch_cron_tasks(monkeypatch, _flaky, interval_seconds=0)

    async with asyncio.timeout(5):
        async with main.app.router.lifespan_context(main.app):
            await asyncio.sleep(0.05)
    assert len(calls) >= 2
