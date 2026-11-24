import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy import func, select
from starlette.exceptions import HTTPException

from app.agent.tools.registry import get_tools_registry, start_tools_registry
from app.api.v1 import artifacts, kb, models, sources, threads, turns
from app.api.v1.health import router as health_router
from app.config_center import start_asset_center
from app.core.config import common_settings, nacos_settings
from app.core.logger import CronLog, setup_logging
from app.core.middleware import register_middleware
from app.core.openapi import setup_openapi
from app.crons import CRON_TASKS, CronTask
from app.db.models import KbDocument, KbImportJob, Turn
from app.db.session import get_session_factory
from app.exception import BusinessException
from app.exception.handler import exception_handler, generate_error_docs
from app.rag.pipeline import get_kb_pipeline
from app.services.event_bus import start_event_bus
from app.services.turn_service import seed_seq_from_db, start_thread_scheduler


def _silence_uvicorn_loggers() -> None:
    """关闭 uvicorn 的 access/error 标准库 logger（幂等，可在多处重复调用）。

    日志单管线：请求日志由 logging 中间件统一记（每请求恰一条，`core/logger/` 配置）；
    ServerErrorMiddleware 对未知异常构造响应后 re-raise 的兜底记录，也已由全局异常
    处理器经 request.state.request_log 按 ERROR 带栈承担。保留 uvicorn 自己的输出只会
    与 loguru 管线重复（访问日志每请求多打一条）、且绕过 request_id 串联。
    """
    logging.getLogger("uvicorn.error").disabled = True
    logging.getLogger("uvicorn.access").disabled = True


# 日志系统在系统启动时完成配置（须在其余组件打印日志前）：
# development = 彩色控制台 + ERROR 额外落文件（LOG_FILE_DIR）；production = stdout 单行 JSON。
_silence_uvicorn_loggers()
setup_logging()


# ---- 启动钩子：孤儿轮清理（data-model.md §4 / api.md §7.3 第 4 条 / ARCH §3.1）----

# 孤儿判定状态集：running；v1.5 起 queued 同口径（ThreadScheduler 队列是进程内存态、
# 重启即失）。pending 为「创建→启动」毫秒级瞬时态，不进清理集（api.md §0 409001 行注）。
_ORPHAN_TURN_STATUSES: tuple[str, ...] = ("running", "queued")

# 孤儿轮 error 载荷（turns.error 契约形态 {code,message,stage_key}，data-model.md §1.3）：
# code 取错误码表兜底 500001——api.md §0 冻结表无「进程重启」专属码，不为非 HTTP 面
# 私增错误码；message 用户可读、指向重问（api.md §7.3 第 4 条）；stage_key=startup——
# 失败裁定发生在任何图节点之前，时间线上无节点可指，以启动清理上下文标注。
_ORPHAN_TURN_ERROR: dict[str, str] = {
    "code": "500001",
    "message": "服务重启导致本轮中断，请重新提问",
    "stage_key": "startup",
}


async def _cleanup_orphan_turns() -> int:
    """孤儿轮清理（启动钩子一次性）：running/queued → failed（error=进程重启）。

    进程重启时进行中轮的执行体（TurnRunner 的进程内 asyncio 任务 / ThreadScheduler
    的内存队列）已随进程消失，这些轮永远等不到终态——启动期一次性置 failed，用户可
    重新提问（api.md §7.3 第 4 条）；幂等可重入：清理后不再有 running/queued 轮，
    重复启动零副作用。返回影响行数（0 = 无孤儿）。

    事务自管（lifespan 无请求级 get_db，短事务模式）：独立 AsyncSession 一次 commit；
    turns ORM 实体级变更（select 表达式全参数绑定，session.scalars() 取实体行），
    失败由 lifespan 捕获记日志、不阻塞启动（恢复型 best-effort，就绪探针如实反映
    db 状态）。
    """
    async with get_session_factory()() as session:
        orphans = (
            await session.scalars(
                select(Turn).where(Turn.status.in_(_ORPHAN_TURN_STATUSES))
            )
        ).all()
        for turn in orphans:
            turn.status = "failed"
            turn.error = _ORPHAN_TURN_ERROR
        await session.commit()
        return len(orphans)


# ---- 启动钩子：KB 队列重入队（data-model.md §4 :608；M3/B16 seam）----

# 重入队状态集（data-model.md §4 :608 逐字）：kb_documents 六态中四个中间态 +
# pending（ready/failed 终态不入列）；kb_import_jobs 四态中 pending/running
# （pending=已建未启动的任务行也须恢复——§1.17）。与状态机扩容同批钉死
# （api.md §4.1 v1.30「pending + normalizing/chunking/indexing」同源，旧 parsing
# 作废）——漏改本列表则中间态文档重启静默卡死（契约注；常量逐字断言单测锁死，
# test_main.py::test_kb_requeue_status_constants_match_contract）。
_KB_REQUEUE_STATUSES: tuple[str, ...] = (
    "pending",
    "normalizing",
    "chunking",
    "indexing",
)
_KB_JOB_REQUEUE_STATUSES: tuple[str, ...] = ("pending", "running")


async def _requeue_kb_work() -> tuple[int, int]:
    """KB 队列重入队启动钩子（M3/B16 seam）：中间态文档与未完任务行计数登记。

    进程内流水线队列（KB 入库）与飞书导入任务队列无持久化、重启即失——启动时
    按 data-model.md §4 查询应重入队行数。真队列消费：KB 入库 worker 已于本
    lifespan 下行接线（B17，get_kb_pipeline().start() 内部经 repo.list_requeue_ids
    消费文档登记行）；飞书导入任务行归 B20。本钩子做「查询计数 + 日志登记」：
    两个只读计数查询、零状态变更、零 commit（幂等可重入）。
    独立 AsyncSession 自管（同 _cleanup_orphan_turns 短事务口径）；失败由
    lifespan 捕获记日志、不阻塞启动（恢复型 best-effort）。
    """
    async with get_session_factory()() as session:
        doc_count = await session.scalar(
            select(func.count())
            .select_from(KbDocument)
            .where(KbDocument.status.in_(_KB_REQUEUE_STATUSES))
        )
        job_count = await session.scalar(
            select(func.count())
            .select_from(KbImportJob)
            .where(KbImportJob.status.in_(_KB_JOB_REQUEUE_STATUSES))
        )
        return int(doc_count or 0), int(job_count or 0)


# ---- 周期任务注册（app/crons/ 注册表 → 进程内 asyncio 周期任务，ADR-001/010）----


async def _cron_loop(task: CronTask) -> None:
    """单任务常驻循环（crons/__init__.py 调度口径）：立即执行首轮 → sleep(interval) 循环。

    任务异常不中断循环——记 CronLog.error 后进入下个周期重试（trace_cleanup 的
    「查询失败本周期放弃、下周期重试」语义即按此消费）；CancelledError 是
    BaseException、不落入 except，原样上抛交由停机优雅取消收口。
    """
    while True:
        try:
            await task.coroutine_factory()
        except Exception as exc:
            CronLog(
                message=f"周期任务 {task.name} 执行失败——本周期放弃，下个周期重试",
                task=task.name,
            ).error(exc)
        await asyncio.sleep(task.interval_seconds)


def _register_cron_tasks() -> list[asyncio.Task[None]]:
    """遍历 CRON_TASKS 注册表，为每项建常驻周期任务（Task name 供运行期排查）。"""
    return [
        asyncio.create_task(_cron_loop(task), name=f"cron:{task.name}")
        for task in CRON_TASKS
    ]


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """启动钩子：再关一次 uvicorn 标准库 logger（幂等，理由见下）。

    uvicorn 每次启动都会按自己的 log 配置重建标准库 logger（dictConfig 会把
    disabled 重置为 False），而 `fastapi dev` 在配置日志之前就先导入了本模块——
    模块顶部那次关闭会被随后的配置覆盖。启动钩子在服务真正起跑前再关一次，
    此后含访问日志在内的 uvicorn 输出都走 loguru 单管线（只剩 uvicorn 起跑前
    的 Started server process / Waiting for application startup 两行由 CLI 打出）。

    启动序（B2 接线，一次性，先于周期任务——应用先具备依赖与一致状态再开始后台工作）：
    - 资产中心（start_asset_center，NACOS_ 组参 + common_settings.app_env）——
      production L4（不可达且无缓存）抛 ConfigCenterException 503001 拒启动；
    - EventBus（start_event_bus，SSE_ 组单例）→ ThreadScheduler（start_thread_
      scheduler，四策略调度 + TurnRunner；无常驻任务、停机零收口）；
    - seq 种入（seed_seq_from_db）：按 turn_events 库内 max(seq) per thread 种入
      EventBus 计数器——不种入则新轮 seq 与持久化历史撞 uq(thread_id, seq)；
    - 孤儿轮清理：running/queued 置 failed（error=进程重启），见 _cleanup_orphan_turns；
      失败记日志不阻塞启动（恢复型 best-effort，幂等可重入、下次重启补做）；
    - KB 队列重入队 + 入库 worker 接线（M3/B16 seam + B17 消费端，data-model.md
      §4 :608）：kb_documents status IN (pending,normalizing,chunking,indexing) 与
      kb_import_jobs status IN (pending,running) 只读计数登记（进程内队列无持久化，
      重启即恢复处理；旧 parsing 值随六态扩容作废，IN 列表与状态机同批钉死、
      常量逐字断言单测锁死）；随后 get_kb_pipeline().start() 消费登记行
      （repo.list_requeue_ids → 队列）并 spawn worker 池——api.md §4.1「入队 →
      ready」主链路的生产消费端。两步均失败记日志不阻塞启动（恢复型 best-effort，
      登记行下次重启重入队承接）；
    - 默认用户 seed：不在钩子做——迁移 0001 幂等 seed 承担（app/db/seed.py，
      data-model.md §1.1/§4；钩子不重复承担 seed 职责）。

    基础设施启动失败（资产中心/EventBus/调度器/seq 种入）不静默吞掉：原样上抛
    令应用启动失败退出（503001 拒启动语义保持；部分启动的组件由嵌套 finally
    逆序收口，不泄漏客户端）。周期任务注册：CRON_TASKS → 进程内 asyncio 周期
    任务（非 LangGraph Agent Server cron，本项目无 Agent Server，ADR-001/010）。
    停机逆序：cron 逐个 cancel + gather 收口 → KB 入库 worker.stop（队列内残留由
    下次启动重入队承接）→ EventBus.close → 资产中心.close。
    """
    _silence_uvicorn_loggers()

    center = await start_asset_center(
        settings=nacos_settings, app_env=common_settings.app_env
    )
    try:
        bus = await start_event_bus()
        try:
            start_tools_registry()  # 进程级工具注册表（熔断状态/连接缓存，B3 web_search）
            start_thread_scheduler()
            await seed_seq_from_db()

            try:
                cleaned = await _cleanup_orphan_turns()
            except Exception as exc:
                # task 字段复用为启动钩子标识：与 crons 同为无请求上下文的后台任务，同口径落 CronLog
                CronLog(
                    message="启动钩子孤儿轮清理失败——本次跳过，下次重启补做",
                    task="startup_hook",
                ).error(exc)
            else:
                if cleaned:
                    CronLog(
                        message=f"启动钩子孤儿轮清理完成：{cleaned} 条进行中轮置 failed",
                        task="startup_hook",
                        total=cleaned,
                    ).info()

            # KB 队列重入队（M3/B16 seam）：中间态文档与未完任务行计数登记，
            # 文档登记行由下行 KB 入库 worker（B17）start() 消费、任务行归 B20
            # ——失败不阻塞启动（best-effort 同上口径）
            try:
                kb_docs, kb_jobs = await _requeue_kb_work()
            except Exception as exc:
                CronLog(
                    message="启动钩子 KB 队列重入队失败——本次跳过，下次重启补做",
                    task="startup_hook",
                ).error(exc)
            else:
                CronLog(
                    message=(
                        "启动钩子 KB 队列重入队登记："
                        f"{kb_docs} 条中间态文档 / {kb_jobs} 条未完任务行待 worker 消费"
                    ),
                    task="startup_hook",
                    total=kb_docs + kb_jobs,
                ).info()

            # KB 入库 worker 接线（M3/B17）：消费上行登记行（start() 内部
            # list_requeue_ids → 队列）并 spawn worker 池；失败不阻塞启动
            # （best-effort 同上两钩子口径，登记行下次重启重入队承接）
            try:
                await get_kb_pipeline().start()
            except Exception as exc:
                CronLog(
                    message="KB 入库 worker 启动失败——登记行下次重启重入队承接",
                    task="startup_hook",
                ).error(exc)

            cron_handles = _register_cron_tasks()
            try:
                yield
            finally:
                for handle in cron_handles:
                    handle.cancel()
                await asyncio.gather(*cron_handles, return_exceptions=True)
                await get_kb_pipeline().stop()
        finally:
            await bus.close()
    finally:
        try:
            await get_tools_registry().aclose()
        except RuntimeError:
            pass  # 注册表未及启动（资产中心失败早退）——无可收口
        await center.close()


# 生产环境隐藏文档入口（/docs、/redoc、/openapi.json 全部关闭）
_hide_docs = common_settings.app_env == "production"

app = FastAPI(
    lifespan=lifespan,
    # 应用名是纯代码常量（配置三分法：ARCH §7 通用组无 APP_NAME 变量），不入 .env
    title="AI Search API",
    # /docs 的「错误码说明」由 ERROR_MAP 自动生成
    description=generate_error_docs(),
    docs_url=None if _hide_docs else "/docs",
    redoc_url=None if _hide_docs else "/redoc",
    openapi_url=None if _hide_docs else "/openapi.json",
)

# 中间件统一在 app/core/middleware/__init__.py 的注册表声明（当前含 X-Process-Time、
# 请求日志与 /api/ 统一成功信封），须在异常处理器注册之前、FastAPI 实例化之后挂载。
register_middleware(app)

# OpenAPI 同步：/api/ 路径的 200/201 响应 schema 包装为统一信封，与响应中间件保持一致。
# 在文档首次生成前调用即可——openapi 生成是惰性的（monkey-patch app.openapi，
# 真正生成时才读取当时已注册的全部路由，故不必等 include_router 之后）。
setup_openapi(app)

# 全局异常处理四处注册：校验失败 / 协议错误 / 领域异常 / 未知异常兜底。
# BusinessException 必须显式注册——starlette 把 Exception 注册路由给最外层
# ServerErrorMiddleware，它构造响应后仍会 re-raise（供服务器记日志）：业务异常
# 若不显式注册就会在 ExceptionMiddleware 层冒泡走这条路，每个业务 404/422 都
# 产生 ERROR traceback 噪音，且下游默认 TestClient(app)
# （raise_server_exceptions=True）拿到的是异常而不是统一 JSON。
app.add_exception_handler(RequestValidationError, exception_handler)
app.add_exception_handler(HTTPException, exception_handler)
app.add_exception_handler(BusinessException, exception_handler)
app.add_exception_handler(Exception, exception_handler)

# 双健康探针（api.md §1，不走信封故挂根路径而非 /api/ 前缀）：/health 存活 +
# /health/ready 就绪（四组件并行探活，实现在 app/api/v1/health.py）。
app.include_router(health_router)

# REST 路由（api.md §2/§3/§4/§5，B2 工单 + M2 批次 4 §5 增量 + M3 B17 §4 增量）：
# threads / turns / models / artifacts / sources / kb——prefix=/api/v1 在各 router
# 模块内声明（/api/ 前缀触发成功信封包装，core/middleware/response.py；artifacts
# 下载自设 is_stream 逃逸）。
app.include_router(threads.router)
app.include_router(turns.router)
app.include_router(models.router)
app.include_router(artifacts.router)
app.include_router(sources.router)
app.include_router(kb.router)
