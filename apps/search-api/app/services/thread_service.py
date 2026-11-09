"""线程域业务服务：建线程+首问轮 / 追问轮 / 线程列表 / 线程详情（api.md §2.1~§2.4）。

职责边界（B2 批次内分工）：
- 本服务只做持久化与聚合维护：mode_summary/turn_count/updated_at 冗余聚合列由本服务
  写入时计算（data-model.md §1.2 口径——不在 ORM 侧触发器/事件里做，保持图节点纯函数、
  持久化归 TurnRunner 的总口径）；首问轮（create_thread_with_turn）直接初始化聚合，
  追问轮（create_turn）在其上推进；
- 并发策略裁决（on_conflict → 409001，ThreadScheduler）、TurnRunner 调用与运行态推进
  （pending→running/queued→终态）、chat_model_key/model_name 的 Nacos 解析均归 B2
  下游任务——本服务两模型字段一律由入参传入，turn.status 恒以创建瞬时态 pending 落库；
- DTO 进 DTO 出（ORM 不出服务层，str↔Literal 的收窄用 cast 并在映射处集中）；只
  flush 不 commit（事务边界在 get_db，BaseService 铁律）。
"""
import uuid
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Thread, Turn
from app.exception import NotFoundException
from app.schemas.common import PageResponse, SearchMode, TurnStatus
from app.schemas.thread import (
    ThreadBrief,
    ThreadCreate,
    ThreadCreateResult,
    ThreadDetail,
    ThreadListQuery,
    SummaryMode,
    TurnSkeleton,
)
from app.schemas.turn import TurnCreate
from app.services.base import BaseService

_TITLE_MAX_CHARS = 50  # REQ-006：title=首问前 50 字符
# parent_turn_id 自动挂载资格（api.md §2.4：上一深度轮 completed/degraded 时 fast 追问自动挂）
_PARENT_ELIGIBLE_STATUSES = ("completed", "degraded")
_MODE_FAST = "fast"
_MODE_DEEP = "deep"
_MODE_MIXED = "mixed"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_uuid(value: str, *, label: str) -> uuid.UUID:
    """路径/上下文字符串 → UUID：非法格式按「资源不存在」处理（不泄漏解析细节）。"""
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise NotFoundException(f"{label}不存在", detail=f"非法 {label} id：{value!r}") from exc


def _summary_mode(modes: set[str]) -> SummaryMode:
    """线程模式聚合（api.md §2.2：按线程内轮次模式聚合，写入时计算）：全 fast→fast、
    全 deep→deep、混合→mixed。"""
    if modes <= {_MODE_FAST}:
        return "fast"
    if modes <= {_MODE_DEEP}:
        return "deep"
    return _MODE_MIXED


def _skeleton(turn: Turn) -> TurnSkeleton:
    """Turn ORM → §2.3 骨架 DTO（api.md v1.10 判据：has_report=mode=deep 且 answer_md
    非空——fast 轮的综合回答不计入 report；degraded=status==degraded。str→Literal
    收窄集中在此与各映射处）。"""
    return TurnSkeleton(
        turn_id=str(turn.id),
        mode=cast(SearchMode, turn.mode),
        status=cast(TurnStatus, turn.status),
        query=turn.query,
        query_lang=turn.query_lang,
        has_report=turn.mode == _MODE_DEEP and bool(turn.answer_md),
        degraded=turn.status == "degraded",
        duration_ms=turn.duration_ms,
        created_at=turn.created_at,
    )


class ThreadService(BaseService):
    """线程域服务（api/ 层经依赖注入构造；事务边界在 get_db——只 flush 不 commit）。"""

    def __init__(self, db: AsyncSession) -> None:
        super().__init__(db)

    async def create_thread_with_turn(
        self,
        payload: ThreadCreate,
        *,
        user_id: str,
        chat_model_key: str,
        model_name: str,
    ) -> ThreadCreateResult:
        """建线程 + 首问轮（api.md §2.1）：title=首问前 50 字符（REQ-006）；聚合列初始化即写。

        返回 ThreadCreateResult（thread_id/turn_id/status）——status 为创建瞬时态
        pending；on_conflict 裁决与图运行由 ThreadScheduler/TurnRunner 接手（下游任务）。
        """
        user_uuid = _as_uuid(user_id, label="用户")
        thread = Thread(
            id=uuid.uuid4(),
            user_id=user_uuid,
            title=payload.query[:_TITLE_MAX_CHARS],
            mode_summary=payload.mode,
            turn_count=1,
            updated_at=_utcnow(),  # 聚合写：列表按 updated_at DESC 排序（REQ-006）
        )
        turn = Turn(
            id=uuid.uuid4(),
            thread_id=thread.id,
            user_id=user_uuid,
            mode=payload.mode,
            status="pending",  # 瞬时态：调度（running/queued）归 ThreadScheduler/TurnRunner
            query=payload.query,
            chat_model_key=chat_model_key,  # 入参传入：Nacos 解析归下一个任务
            model_name=model_name,  # 同上（trace 可证 A10）
        )
        self.db.add_all([thread, turn])
        await self.db.flush()
        return ThreadCreateResult(
            thread_id=str(thread.id), turn_id=str(turn.id), status="pending"
        )

    async def create_turn(
        self,
        thread_id: str,
        payload: TurnCreate,
        *,
        user_id: str,
        chat_model_key: str,
        model_name: str,
    ) -> ThreadCreateResult:
        """追问建轮（api.md §2.4）：归属校验 → parent 自动挂载（REQ-017）→ 聚合推进。

        parent_turn_id：mode=fast 且线程内最近一深度轮 status∈{completed,degraded} 时
        自动挂载（前端无需传，api.md 180 行）；deep 追问不挂。
        """
        thread = await self._get_owned_thread(thread_id, user_id)
        turn = Turn(
            id=uuid.uuid4(),
            thread_id=thread.id,
            user_id=thread.user_id,
            mode=payload.mode,
            status="pending",
            query=payload.query,
            chat_model_key=chat_model_key,
            model_name=model_name,
        )
        if payload.mode == _MODE_FAST:
            turn.parent_turn_id = await self._deep_parent_candidate(thread.id)
        self.db.add(turn)
        await self._apply_thread_aggregates(thread, new_mode=payload.mode)
        await self.db.flush()
        return ThreadCreateResult(
            thread_id=str(thread.id), turn_id=str(turn.id), status="pending"
        )

    async def list_threads(
        self, query: ThreadListQuery, *, user_id: str
    ) -> PageResponse[ThreadBrief]:
        """线程列表（api.md §2.2）：user 域过滤 + mode 过滤 + updated_at DESC + 分页。

        返回 PageResponse（api.md §2.2 data={total, items} 直接口径；基座 PageResult 为
        page/page_size 语义，与 limit/offset 契约不匹配，故此端点直接产出响应 DTO）。
        """
        user_uuid = _as_uuid(user_id, label="用户")
        conditions = [Thread.user_id == user_uuid]
        if query.mode is not None:
            conditions.append(Thread.mode_summary == query.mode)

        count_stmt = select(func.count()).select_from(Thread).where(*conditions)
        total = await self.db.scalar(count_stmt)
        page_stmt = (
            select(Thread)
            .where(*conditions)
            .order_by(Thread.updated_at.desc())
            .limit(query.limit)
            .offset(query.offset)
        )
        rows = (await self.db.scalars(page_stmt)).all()
        return PageResponse(
            total=int(total or 0),
            items=[
                ThreadBrief(
                    thread_id=str(row.id),
                    title=row.title,
                    mode_summary=cast(SummaryMode, row.mode_summary),
                    turn_count=row.turn_count,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
                for row in rows
            ],
        )

    async def get_thread_detail(self, thread_id: str, *, user_id: str) -> ThreadDetail:
        """线程详情（api.md §2.3）：骨架聚合，turns 按 created_at 升序（轮次栏时间线）。"""
        thread = await self._get_owned_thread(thread_id, user_id)
        stmt = select(Turn).where(Turn.thread_id == thread.id).order_by(Turn.created_at.asc())
        turns = (await self.db.scalars(stmt)).all()
        return ThreadDetail(
            thread_id=str(thread.id),
            title=thread.title,
            mode_summary=cast(SummaryMode, thread.mode_summary),
            turn_count=thread.turn_count,
            # 分期口径（用户裁决 2026-11-08）：恒 false——0002-M2（artifacts 表+迁移）起真实值
            has_artifacts=False,
            created_at=thread.created_at,
            updated_at=thread.updated_at,
            turns=[_skeleton(t) for t in turns],
        )

    async def _get_owned_thread(self, thread_id: str, user_id: str) -> Thread:
        """线程归属校验：id+user_id 双条件查询（api.md §0 404001——未找到与越权同码，
        不泄漏资源存在性）；语句全参数绑定（select 表达式）。"""
        stmt = select(Thread).where(
            Thread.id == _as_uuid(thread_id, label="线程"),
            Thread.user_id == _as_uuid(user_id, label="用户"),
        )
        thread = (await self.db.scalars(stmt)).first()
        if thread is None:
            raise NotFoundException(
                "线程不存在",
                detail=f"thread_id={thread_id} 不存在或不属于当前用户",
            )
        return thread

    async def _deep_parent_candidate(self, thread_id: uuid.UUID) -> uuid.UUID | None:
        """fast 追问的 parent 候选：线程内最近一深度轮 status∈{completed,degraded} 时返回
        其 id（api.md §2.4/REQ-017）；否则 None（报告未完成/失败/取消均不挂）。"""
        stmt = (
            select(Turn)
            .where(Turn.thread_id == thread_id, Turn.mode == _MODE_DEEP)
            .order_by(Turn.created_at.desc())
            .limit(1)
        )
        deep_turn = (await self.db.scalars(stmt)).first()
        if deep_turn is not None and deep_turn.status in _PARENT_ELIGIBLE_STATUSES:
            return deep_turn.id
        return None

    async def _apply_thread_aggregates(self, thread: Thread, *, new_mode: str) -> None:
        """聚合列推进（data-model.md §1.2：写入轮次时由 service 层维护冗余聚合列）。

        mode 集合经 distinct 查询取既有值并与新轮模式取并（autoflush 下新轮已可见，
        并集幂等无重复计数）；turn_count 增量 +1（本服务是唯一写入方）。
        """
        stmt = select(Turn.mode).where(Turn.thread_id == thread.id).distinct()
        existing: set[str] = set((await self.db.scalars(stmt)).all())
        thread.turn_count += 1
        thread.mode_summary = _summary_mode(existing | {new_mode})
        thread.updated_at = _utcnow()
