"""threads 域路由（api.md §2.1~§2.4）：建线程+首问轮 / 线程列表 / 线程详情 / 追问。

路由层零业务逻辑：只做解析 + 参数校验 + 依赖注入 + 调 service——建轮与聚合在
ThreadService（thread_service.py）、并发策略裁决与排程在 ThreadScheduler
（turn_service.py）、模型键解析在 turn_service.resolve_chat_model（非法键 400001）。

提交链路调用时序（B2 裁决）：thread_service 建轮(flush) → resolve_conflict_on_submit
（请求事务内裁决，REJECT 抛 409001——pending 轮随 get_db 回滚零残留）→
BackgroundTasks(schedule_turn, turn_id)——本路由链的 get_db 以 scope="function"
解析，teardown commit 在端点返回后、响应发送与 BackgroundTask 之前执行
（FastAPI 0.136 默认 request 作用域 teardown 与 BackgroundTask 同任务严格串行、
晚于后者——调度器将读不到未提交轮行，integration 实锤后改定，机制详见
get_thread_service docstring），schedule_turn 读到的是已提交的轮行，无竞态。
"""
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db, get_model_mapping
from app.schemas import (
    PageResponse,
    ThreadBrief,
    ThreadCreate,
    ThreadCreateResult,
    ThreadDetail,
    ThreadListQuery,
    TurnCreate,
    TurnStatus,
)
from app.services.thread_service import ThreadService
from app.services.turn_service import (
    SubmitDecision,
    ThreadScheduler,
    get_thread_scheduler,
    resolve_chat_model,
)

router = APIRouter(prefix="/api/v1")


def get_thread_service(
    db: Annotated[AsyncSession, Depends(get_db, scope="function")]
) -> ThreadService:
    """线程域服务请求级实例（api 层统一依赖缝；单测经 dependency_overrides 整体替换）。

    get_db 以 scope="function" 解析：teardown（commit/rollback）在端点函数返回后、
    响应序列化与 BackgroundTask 之前执行——BackgroundTask（schedule_turn）因此
    读到已提交的轮行。FastAPI ≥0.106 的 request 作用域 teardown 在响应发送后才
    跑，且与 BackgroundTask 在同一内层任务里**严格晚于** background 返回（实测
    0.136：BackgroundTask 先执行、随后才 teardown commit）——默认作用域下调度器
    永远读不到未提交的轮行（结构性死锁，integration 实锤）。路由与
    get_thread_service 两侧必须同 scope：cache_key 含 computed_scope，不同 scope
    会解析出两个互不可见会话。
    """
    return ThreadService(db)


# 裁决 → 响应 status（api.md §2.1「running 或 queued」）：PROCEED=立即执行 running；
# ENQUEUE/INTERRUPT/ROLLBACK=新轮先置 queued（interrupt/rollback 的新轮为抢占轮：
# 等被打断旧轮 canceled 后**插队**启动，既有 queued 顺延——裁决 A，2026-11-08）；
# REJECT 不产出响应（409001 已抛出）。
_DECISION_STATUS: dict[SubmitDecision, TurnStatus] = {
    SubmitDecision.PROCEED: "running",
    SubmitDecision.ENQUEUE: "queued",
    SubmitDecision.INTERRUPT: "queued",
    SubmitDecision.ROLLBACK: "queued",
}


async def _resolve_and_schedule(
    *,
    scheduler: ThreadScheduler,
    db: AsyncSession,
    background_tasks: BackgroundTasks,
    result: ThreadCreateResult,
    on_conflict: str | None,
) -> ThreadCreateResult:
    """建轮后的公共收尾：并发策略裁决（409001 由此出）+ BackgroundTask 排程。

    响应 status 由裁决映射（_DECISION_STATUS）——thread_service 落库的瞬时 pending
    不外露（api.md §2.1 响应只有 running/queued 两态）。
    """
    decision = await scheduler.resolve_conflict_on_submit(
        db, thread_id=result.thread_id, turn_id=result.turn_id, on_conflict=on_conflict
    )
    background_tasks.add_task(scheduler.schedule_turn, result.turn_id)
    return ThreadCreateResult(
        thread_id=result.thread_id,
        turn_id=result.turn_id,
        status=_DECISION_STATUS[decision],
    )


@router.post("/threads", response_model=ThreadCreateResult)
async def create_thread(
    payload: ThreadCreate,
    background_tasks: BackgroundTasks,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    user_id: Annotated[str, Depends(get_current_user)],
    service: Annotated[ThreadService, Depends(get_thread_service)],
    scheduler: Annotated[ThreadScheduler, Depends(get_thread_scheduler)],
    mapping: Annotated[Mapping[str, Any], Depends(get_model_mapping)],
) -> ThreadCreateResult:
    """首问建线程并启动轮次（api.md §2.1）：建线程 + 第一轮 + 启动图运行，立即返回。

    模型解析先于建轮（resolve_chat_model 非法键 400001——零写库）；新线程无并发冲突
    但策略链照走（四策略入口同一处，不因首问旁路）。get_db scope="function" 的
    理由见 get_thread_service docstring（teardown commit 先于 BackgroundTask）。
    """
    chat_model_key, model_name = resolve_chat_model(payload.model, mapping)
    result = await service.create_thread_with_turn(
        payload, user_id=user_id, chat_model_key=chat_model_key, model_name=model_name
    )
    return await _resolve_and_schedule(
        scheduler=scheduler,
        db=db,
        background_tasks=background_tasks,
        result=result,
        on_conflict=payload.on_conflict,
    )


@router.get("/threads", response_model=PageResponse[ThreadBrief])
async def list_threads(
    query: Annotated[ThreadListQuery, Query()],
    user_id: Annotated[str, Depends(get_current_user)],
    service: Annotated[ThreadService, Depends(get_thread_service)],
) -> PageResponse[ThreadBrief]:
    """线程列表（api.md §2.2）：分页 + mode 过滤（缺省=全部；一期无归档概念）。"""
    return await service.list_threads(query, user_id=user_id)


@router.get("/threads/{thread_id}", response_model=ThreadDetail)
async def get_thread_detail(
    thread_id: str,
    user_id: Annotated[str, Depends(get_current_user)],
    service: Annotated[ThreadService, Depends(get_thread_service)],
) -> ThreadDetail:
    """线程详情（api.md §2.3）：答案页骨架聚合；不存在或不属当前用户 → 404001。"""
    return await service.get_thread_detail(thread_id, user_id=user_id)


@router.post("/threads/{thread_id}/turns", response_model=ThreadCreateResult)
async def create_turn(
    thread_id: str,
    payload: TurnCreate,
    background_tasks: BackgroundTasks,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    user_id: Annotated[str, Depends(get_current_user)],
    service: Annotated[ThreadService, Depends(get_thread_service)],
    scheduler: Annotated[ThreadScheduler, Depends(get_thread_scheduler)],
    mapping: Annotated[Mapping[str, Any], Depends(get_model_mapping)],
) -> ThreadCreateResult:
    """追问建轮（api.md §2.4）：请求体同 §2.1；404001 线程不存在 / 409001 并发拒绝。

    模型解析同样先于建轮（与 §2.1 同口径）；parent_turn_id 自动挂载（REQ-017）与
    聚合推进在 service 内完成。
    """
    chat_model_key, model_name = resolve_chat_model(payload.model, mapping)
    result = await service.create_turn(
        thread_id,
        payload,
        user_id=user_id,
        chat_model_key=chat_model_key,
        model_name=model_name,
    )
    return await _resolve_and_schedule(
        scheduler=scheduler,
        db=db,
        background_tasks=background_tasks,
        result=result,
        on_conflict=payload.on_conflict,
    )
