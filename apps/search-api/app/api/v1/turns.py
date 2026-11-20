"""turns 域路由（api.md §2.5/§2.6/§2.7）：轮次详情、SSE 事件流与停止。

路由层零业务逻辑：GET 详情的归属校验与 §2.5 映射在 turn_service.get_turn_detail；
cancel 的归属校验/终态判定/出队收尾在 ThreadScheduler.cancel_turn（409002）；SSE 的
归属校验在 turn_service.ensure_turn_owner（建流前 404001），本模块只做传输层装配
（帧格式/心跳/续传/终态关闭，api.md §7.1）。
"""
import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import sse_settings
from app.dependencies import get_current_user, get_db, get_event_bus
from app.schemas import (
    FeedbackRequest,
    FeedbackResponse,
    TurnCancelResult,
    TurnDetail,
    TurnExportResult,
    TurnRegenerateResult,
)
from app.services.artifact_service import ArtifactService
from app.services.event_bus import (
    FOLLOWUPS_EVENT_TYPE,
    TERMINAL_EVENT_TYPE,
    EventBus,
    SseEvent,
)
from app.services.turn_feedback_service import TurnFeedbackService
from app.services.turn_service import (
    ThreadScheduler,
    ensure_turn_owner,
    get_thread_scheduler,
    get_turn_detail,
)

router = APIRouter(prefix="/api/v1")


def get_feedback_service(
    db: Annotated[AsyncSession, Depends(get_db)]
) -> TurnFeedbackService:
    """轮反馈服务的请求级实例（api 层统一依赖缝；单测经 dependency_overrides 替换）。

    get_db 用默认 request 作用域即可（本端点无 BackgroundTask、无跨提交读——
    与 threads 的 scope="function" 特化理由不同；upsert 在请求事务内完成，
    teardown 统一提交）。
    """
    return TurnFeedbackService(db)


async def get_turn_or_404(
    turn_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user_id: Annotated[str, Depends(get_current_user)],
) -> TurnDetail:
    """GET /turns/{turn_id} 的服务缝：归属校验（404001）与 §2.5 DTO 映射在 service。

    独立成依赖便于单测整体替换，同时让路由函数退化为纯透传（零业务逻辑铁律）。
    """
    return await get_turn_detail(db, turn_id, user_id=user_id)


@router.get("/turns/{turn_id}", response_model=TurnDetail)
async def read_turn(
    turn: Annotated[TurnDetail, Depends(get_turn_or_404)],
) -> TurnDetail:
    """轮次详情（api.md §2.5）：进行中（running/queued）亦返回已持久化部分。"""
    return turn


@router.post("/turns/{turn_id}/cancel", response_model=TurnCancelResult)
async def cancel_turn(
    turn_id: str,
    user_id: Annotated[str, Depends(get_current_user)],
    scheduler: Annotated[ThreadScheduler, Depends(get_thread_scheduler)],
) -> TurnCancelResult:
    """停止轮次（api.md §2.7）：幂等协作式取消，响应 data.status 恒 "canceling"。

    queued 轮立即出队+终态、pending/running 登记标志协作式收尾，均在 scheduler 内；
    已终态 → 409002（TurnTerminalException）；终态经 SSE done(finish=canceled) 与
    §2.5 对账确认（响应契约零变更）。
    """
    result = await scheduler.cancel_turn(turn_id, user_id=user_id, reason="user")
    # status 用字面量收窄（scheduler 返回 dict[str, str]，契约恒 "canceling"）
    return TurnCancelResult(turn_id=result["turn_id"], status="canceling")


@router.post("/turns/{turn_id}/regenerate", response_model=TurnRegenerateResult)
async def regenerate_turn(
    turn_id: str,
    background_tasks: BackgroundTasks,
    user_id: Annotated[str, Depends(get_current_user)],
    scheduler: Annotated[ThreadScheduler, Depends(get_thread_scheduler)],
) -> TurnRegenerateResult:
    """degraded/failed-with-sources 快速轮「仅重新生成」（api.md §2.12 v1.19，REQ-063）。

    路由层零业务逻辑：归属校验（404001）/ 前置裁决与条件推进（409006）/ 通道重开
    均在 `ThreadScheduler.regenerate_turn`；微图执行（单节点 synthesis，非
    GRAPH_REGISTRY）为 BackgroundTask——条件推进已在请求内 commit（scheduler 短事务），
    任务读到 running 行即可执行。SSE 同通道 seq 续推，前端重订阅后按
    `stage(write,running)` 清空重渲（frontend-api §3 生命周期 14）。
    """
    result = await scheduler.regenerate_turn(turn_id, user_id=user_id)
    background_tasks.add_task(scheduler.execute_regenerate, turn_id)
    return TurnRegenerateResult(turn_id=result["turn_id"], status="running")


@router.post("/turns/{turn_id}/feedback", response_model=FeedbackResponse)
async def submit_feedback(
    turn_id: str,
    payload: FeedbackRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    service: Annotated[TurnFeedbackService, Depends(get_feedback_service)],
) -> FeedbackResponse:
    """轮次有帮助反馈（api.md §2.9 v1.17，REQ-057）：每轮一条 upsert 改判；null=清除。

    边界校验在 pydantic（helpful 缺失/非 boolean|null → 400001，零业务调用）；
    归属校验与 upsert/清除在 service（轮不存在 → 404004；helpful=null → 删行并返回
    清除形态 {turn_id, helpful: null, updated_at: null}）；读回经 §2.5
    `feedback_helpful`（无独立 GET 端点）。
    """
    return await service.upsert_feedback(
        turn_id, helpful=payload.helpful, user_id=user_id
    )


def get_artifact_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ArtifactService:
    """快照与产物域服务（api.md §5）：请求级会话注入（get_db——service 只 flush 不
    commit，事务边界统一在请求级）；单测经 dependency_overrides 替换。"""
    return ArtifactService(db)


@router.post("/turns/{turn_id}/export", response_model=TurnExportResult)
async def export_turn(
    turn_id: str,
    user_id: Annotated[str, Depends(get_current_user)],
    service: Annotated[ArtifactService, Depends(get_artifact_service)],
) -> TurnExportResult:
    """导出报告 Markdown（api.md §2.8 v1.27 面，REQ-016）：深度轮报告写线程产物目录
    并登记 artifacts 行。

    路由层零业务逻辑：归属校验（404001）/ 导出门禁（409003——非深度轮或未到
    completed|degraded）/ 同分钟跨轮碰撞检查（409003——同线程不同轮同分钟已导出，
    防物理文件覆写污染首产物行）/ 文件写入与登记均在 ArtifactService.export_turn_report；
    completed|degraded 均可导出（§0:43），同轮重复导出按次登记新产物行。前端仅消费
    download_path 原样请求（§5.3）。
    """
    return await service.export_turn_report(turn_id, user_id=user_id)


# ---- SSE 事件流（api.md §2.6/§7.1——重放 + 心跳 + 保活窗口收口）----


class _QueueSignal:
    """订阅队列哨兵（与 SseEvent 事件对象区分；不携带数据）。"""


_HEARTBEAT_SIGNAL = _QueueSignal()  # 心跳帧占位（": ping"，不占 seq）
_STREAM_END_SIGNAL = _QueueSignal()  # 订阅流自然结束（closed 排空 / 泵完成）

#: done 后保活窗口的轮询粒度（秒）：窗口剩余时间/放弃信号的检查周期。
#: 为什么不直接等满窗口：生成可能**提前静默放弃**（TurnRunner 调 bus.abandon_followups）
#: ——此时窗口失效但队列无新事件，分段轮询保证 ≤ 该粒度的收口时延（api.md §7.1
#: 「窗口超时或生成已静默放弃 → 静默关闭」）。数值为环境无关的节奏常量。
FOLLOWUPS_HOLD_POLL_SECONDS = 0.2


def _format_sse(event: SseEvent, *, turn_id: str) -> bytes:
    """单事件三行式帧（api.md §7.1 510-511 行）：id:=seq、event:=类型、data:=JSON。

    data 信封 = {"seq","turn_id","ts",...payload}——turn_id/ts 由端点装配（SseEvent
    只承载线程/类型/业务字段，event_bus.py SseEvent docstring 口径）；信封三键后置
    展开（payload 同名键不穿透，信封字段权威）；ts 以 UTC "Z" 后缀对齐契约示例。
    """
    ts = (event.ts or datetime.now(UTC)).astimezone(UTC)
    envelope = {
        **event.data,
        "seq": int(event.seq or 0),
        "turn_id": turn_id,
        "ts": ts.isoformat().replace("+00:00", "Z"),
    }
    data = json.dumps(envelope, ensure_ascii=False)
    return f"id: {envelope['seq']}\nevent: {event.event_type}\ndata: {data}\n\n".encode(
        "utf-8"
    )


async def _event_stream(
    bus: EventBus, turn_id: str, last_seq: int, *, heartbeat_seconds: int
) -> AsyncIterator[bytes]:
    """SSE 帧流：EventBus.subscribe（重放 + 跟随）泵入队列，消费侧织入心跳。

    终态收口（api.md §7.1）：
    - **保活窗口**（v1.7 增，v1.16 起默认开启）：达标轮（fast+completed+答案非空+
      followups.enabled）由 TurnRunner 在 done 发布前登记窗口——done 后不立即关闭，
      窗口内 followups 事件到达即下发后收口；窗口耗尽（或无事件）静默关闭（不报错）。
      未登记（开关关闭/非达标轮/deep 轮）→ done 后立即关闭（与既有语义零观测差异）；
    - **重放路径**：缓冲中 done 之后存在 followups 时重放序列为 `[…, done, followups]`
      后关闭；`Last-Event-ID ≥ done.seq` 仍须补下发 seq 更大的 followups（terminal_seq
      对偶分支扩展到「还有未取事件」）；cursor 已覆盖全部事件 → 零帧关闭；
    - error 路径无 done，随 close_channel 的 closed 在缓冲排空后自然结束。

    泵/心跳双任务 + 单队列：心跳经队列织入而非 wait_for(anext)——后者超时会向
    subscribe 生成器注入 CancelledError 致订阅被拆（心跳必须与订阅共存）；保活等待
    用 `wait_for(queue.get())`（只取消取队操作，不触订阅生成器）。
    finally 逐个 cancel + aclose（断开注销订阅者，event_bus.py finally 口径）。
    """
    terminal = bus.terminal_seq(turn_id)
    if terminal is not None and last_seq >= terminal:
        followups_seq = bus.followups_seq(turn_id)
        # 等待条件收紧（评审 P3-3）：followups 已发布时不再「等窗口」——只剩
        # 「还有未取事件」（seq > cursor）一种继续订阅的理由；否则窗口期内重连
        # 会空挂到窗口耗尽（≤10s）才收口
        awaiting = (
            followups_seq is None and bus.followups_hold_remaining(turn_id) is not None
        )
        unseen = followups_seq is not None and followups_seq > last_seq
        if not awaiting and not unseen:
            return  # 客户端已持有 done 且无未取事件：零帧关闭

    queue: asyncio.Queue[Any] = asyncio.Queue()
    iterator = bus.subscribe(turn_id, last_seq)

    async def _pump() -> None:
        try:
            async for event in iterator:
                queue.put_nowait(event)
            queue.put_nowait(_STREAM_END_SIGNAL)
        except Exception as exc:  # 泵异常交消费方上抛收尾，不静默死亡悬挂队列
            queue.put_nowait(exc)

    async def _heartbeat() -> None:
        while True:
            await asyncio.sleep(heartbeat_seconds)
            queue.put_nowait(_HEARTBEAT_SIGNAL)

    pump_task = asyncio.create_task(_pump(), name=f"sse-pump:{turn_id}")
    heartbeat_task = asyncio.create_task(
        _heartbeat(), name=f"sse-heartbeat:{turn_id}"
    )
    # 晚订阅（cursor 已过 done）时窗口状态在进入即生效；运行中轮此处恒 False，
    # done 帧到达时再裁决（TurnRunner 在 done 发布前登记窗口——此刻已可见）
    hold_active = bus.followups_hold_remaining(turn_id) is not None
    try:
        while True:
            if hold_active:
                remaining = bus.followups_hold_remaining(turn_id)
                if remaining is None:
                    return  # 窗口耗尽/生成已放弃：静默关闭（不报错、无新信号）
                try:
                    item = await asyncio.wait_for(
                        queue.get(), timeout=min(remaining, FOLLOWUPS_HOLD_POLL_SECONDS)
                    )
                except TimeoutError:
                    continue  # 分段轮询：下一轮重算窗口（含 abandon 提前收口）
            else:
                item = await queue.get()
            if item is _STREAM_END_SIGNAL:
                return
            if isinstance(item, BaseException):
                raise item
            if item is _HEARTBEAT_SIGNAL:
                yield b": ping\n\n"  # 注释行不占 seq（api.md §7.1 512 行）
                continue
            yield _format_sse(item, turn_id=turn_id)
            if item.event_type == FOLLOWUPS_EVENT_TYPE:
                return  # 事件下发后收口（§7.1「事件到达 → 下发后关闭」）
            if item.event_type == TERMINAL_EVENT_TYPE:
                # **done 终态判定按最新一次 done**（api.md §7.1 v1.19 regenerate 注）：
                # 重放/跟随路径中遇到的旧 done（regenerate 前历史，item.seq ≠ 通道
                # 最新终态 seq；通道重开期 latest=None）只是轨迹留痕——不关流、继续
                # 跟随；仅当本帧即最新终态时才走既有收口逻辑（保活窗口/直接关闭）。
                if bus.terminal_seq(turn_id) != item.seq:
                    continue
                if bus.followups_hold_remaining(turn_id) is not None:
                    hold_active = True  # 达标轮：done 后进入保活窗口
                    continue
                if bus.followups_seq(turn_id) is not None:
                    continue  # followups 已在缓冲（seq > done）：随泵下发后收口
                return  # 非达标轮：done 即关（既有语义）
    finally:
        heartbeat_task.cancel()
        pump_task.cancel()
        await asyncio.gather(heartbeat_task, pump_task, return_exceptions=True)
        with suppress(Exception):  # 泵已取完/已死时 aclose 幂等；断开注销订阅者
            await iterator.aclose()


@router.get("/turns/{turn_id}/events")
async def stream_turn_events(
    turn_id: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db, scope="function")],
    user_id: Annotated[str, Depends(get_current_user)],
    bus: Annotated[EventBus, Depends(get_event_bus)],
) -> StreamingResponse:
    """SSE 事件流（api.md §2.6/§7.1）：重放 + 心跳 + 终态关闭（B2 最小版）。

    建流前归属校验（404001 普通信封，api.md §7.1 515 行——异常在
    StreamingResponse 之前抛出，经全局业务异常处理器出 application/json）；
    Last-Event-ID 缺省/非数字按 0（首连全量重放）；订阅先行（排队轮）合法——
    纯心跳段直到首个 stage 事件（api.md §7.1 517 行）。is_stream 标志必须自设
    （统一信封中间件据此跳过流式响应，core/middleware/response.py）。

    db 依赖以 scope="function" 解析（评审 P2）：teardown（归还连接）在端点返回
    后、流式响应消费前执行——request 作用域会把池连接以 idle-in-transaction 占满
    整条流生命周期，多订阅并发下耗尽 pool（10+5）。StreamingResponse 装配不依赖
    请求级 session（事件流走 EventBus，归属校验一条 SELECT 即完成）。
    """
    await ensure_turn_owner(db, turn_id, user_id=user_id)
    try:
        last_seq = int(request.headers.get("last-event-id") or 0)
    except ValueError:
        last_seq = 0  # 非数字游标按首连全量重放（§7.1 513 行宽路径）
    request.state.is_stream = True
    return StreamingResponse(
        _event_stream(
            bus, turn_id, last_seq, heartbeat_seconds=sse_settings.heartbeat_seconds
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )
