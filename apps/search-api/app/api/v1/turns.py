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

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import sse_settings
from app.dependencies import get_current_user, get_db, get_event_bus
from app.schemas import TurnCancelResult, TurnDetail
from app.services.event_bus import TERMINAL_EVENT_TYPE, EventBus, SseEvent
from app.services.turn_service import (
    ThreadScheduler,
    ensure_turn_owner,
    get_thread_scheduler,
    get_turn_detail,
)

router = APIRouter(prefix="/api/v1")


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


# ---- SSE 事件流（api.md §2.6/§7.1——B2 最小版：重放 + 心跳 + 终态关闭）----


class _QueueSignal:
    """订阅队列哨兵（与 SseEvent 事件对象区分；不携带数据）。"""


_HEARTBEAT_SIGNAL = _QueueSignal()  # 心跳帧占位（": ping"，不占 seq）
_STREAM_END_SIGNAL = _QueueSignal()  # 订阅流自然结束（closed 排空 / 泵完成）


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

    终态关闭（B2 最小版，followups 保活窗口归批次 5）：
    - done 事件发出后即关闭——重放路径的 done 来自缓冲本身，不重复发（§7.1 514 行
      「重放全部后发送 done 并关闭」；error 路径无 done，随 close_channel 的 closed
      在缓冲排空后自然结束）；
    - Last-Event-ID ≥ 终态 seq（客户端已持有 done）→ 不订阅直接关闭（terminal_seq
      对偶分支——否则订阅流挂起在跟随态等不到新事件）。

    泵/心跳双任务 + 单队列：心跳经队列织入而非 wait_for(anext)——后者超时会向
    subscribe 生成器注入 CancelledError 致订阅被拆（心跳必须与订阅共存）；
    finally 逐个 cancel + aclose（断开注销订阅者，event_bus.py finally 口径）。
    """
    terminal = bus.terminal_seq(turn_id)
    if terminal is not None and last_seq >= terminal:
        return  # 客户端已持有 done：零帧关闭

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
    try:
        while True:
            item = await queue.get()
            if item is _STREAM_END_SIGNAL:
                return
            if isinstance(item, BaseException):
                raise item
            if item is _HEARTBEAT_SIGNAL:
                yield b": ping\n\n"  # 注释行不占 seq（api.md §7.1 512 行）
                continue
            yield _format_sse(item, turn_id=turn_id)
            if item.event_type == TERMINAL_EVENT_TYPE:
                return  # done 即关（B2 最小版）
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
