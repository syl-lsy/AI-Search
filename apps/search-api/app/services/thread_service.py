"""线程域业务服务：建线程+首问轮 / 追问轮 / 线程列表 / 线程详情 / 线程管理（api.md
§2.1~§2.4、§2.10/§2.11 v1.18）。

职责边界（B2 批次内分工 + B10 线程管理）：
- 本服务只做持久化与聚合维护：mode_summary/turn_count/updated_at 冗余聚合列由本服务
  写入时计算（data-model.md §1.2 口径——不在 ORM 侧触发器/事件里做，保持图节点纯函数、
  持久化归 TurnRunner 的总口径）；首问轮（create_thread_with_turn）直接初始化聚合，
  追问轮（create_turn）在其上推进；
- 并发策略裁决（on_conflict → 409001，ThreadScheduler）、TurnRunner 调用与运行态推进
  （pending→running/queued→终态）、chat_model_key/model_name 的 Nacos 解析均归 B2
  下游任务——本服务两模型字段一律由入参传入，turn.status 恒以创建瞬时态 pending 落库；
- 线程管理（v1.18，REQ-058~060，B10）：列表参数/排序/三布尔投影（§2.2）、详情置已读
  写副作用（§2.3，幂等）、部分更新 PATCH（§2.10，updated_at 不随动）、物理删除 DELETE
  （§2.11，service 显式级联 + MinIO best-effort）、追问自动取消归档（§2.4）；
- DTO 进 DTO 出（ORM 不出服务层，str↔Literal 的收窄用 cast 并在映射处集中）；只
  flush 不 commit（事务边界在 get_db，BaseService 铁律）。
"""
import uuid
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import delete, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logger import BusinessLog
from app.db.models import Artifact, Thread, Turn
from app.exception import NotFoundException, ThreadBusyException
from app.schemas.common import PageResponse, SearchMode, TurnStatus
from app.schemas.thread import (
    ThreadBrief,
    ThreadCreate,
    ThreadCreateResult,
    ThreadDeleteData,
    ThreadDetail,
    ThreadListQuery,
    ThreadPatchRequest,
    SummaryMode,
    TurnSkeleton,
)
from app.schemas.turn import TurnCreate
from app.services.base import BaseService
from app.services.object_cleanup import MinioThreadObjectPurger, ThreadObjectPurger
from app.services.turn_service import ACTIVE_STATUSES, QUEUED_STATUS

_TITLE_MAX_CHARS = 50  # REQ-006：title=首问前 50 字符
# parent_turn_id 自动挂载资格（api.md §2.4：上一深度轮 completed/degraded 时 fast 追问自动挂）
_PARENT_ELIGIBLE_STATUSES = ("completed", "degraded")
_MODE_FAST = "fast"
_MODE_DEEP = "deep"
_MODE_MIXED = "mixed"
# 删除线程的进行中轮判定（api.md §2.11 / D-10）：与 409001 同源状态集（ThreadScheduler
# ACTIVE_STATUSES=pending/running + queued——pending 瞬时态并入 running 判定）
_IN_FLIGHT_STATUSES = (*ACTIVE_STATUSES, QUEUED_STATUS)
# LIKE 通配转义字符（§2.2 q：用户输入不得放大匹配面）
_LIKE_ESCAPE = "\\"


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


def _brief(thread: Thread) -> ThreadBrief:
    """Thread ORM → §2.2 items[] 条目 DTO（v1.18 三布尔投影在此推导，D-06）：

    `is_unread = read_at IS NULL`、`archived = archived_at IS NOT NULL`。
    """
    return ThreadBrief(
        thread_id=str(thread.id),
        title=thread.title,
        mode_summary=cast(SummaryMode, thread.mode_summary),
        turn_count=thread.turn_count,
        pinned=bool(thread.pinned),
        is_unread=thread.read_at is None,
        archived=thread.archived_at is not None,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )


def _escape_like(value: str) -> str:
    """ILIKE 通配转义（§2.2 q 包含匹配）：`\\` / `%` / `_` 一律按字面量匹配。"""
    return (
        value.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", f"{_LIKE_ESCAPE}%")
        .replace("_", f"{_LIKE_ESCAPE}_")
    )


class ThreadService(BaseService):
    """线程域服务（api/ 层经依赖注入构造；事务边界在 get_db——只 flush 不 commit）。"""

    def __init__(
        self, db: AsyncSession, *, object_purger: ThreadObjectPurger | None = None
    ) -> None:
        super().__init__(db)
        # 删除线程后的对象存储清理（best-effort，失败仅 WARN——object_cleanup.py）；
        # 单测/集成注入假体，生产缺省 MinIO 前缀删除实现
        self._object_purger: ThreadObjectPurger = object_purger or MinioThreadObjectPurger()

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

        归档线程可正常追问，**建轮成功即自动取消归档**（archived_at 置 NULL——对话
        活动把线程带回活跃列表，api.md §2.4 v1.18 注 / D-13）。若后续并发策略拒绝
        （409001）随请求事务整体回滚，取消归档不留痕（「建轮成功时」语义）。

        归属读取取线程行级排他锁（`for_update=True`，评审 P2）：与 DELETE 的锁读互斥，
        杜绝「DELETE 判完 in-flight 才撞上本请求的 INSERT」→ FK IntegrityError 500001
        的竞态（用户视角应是 404001 线程已删）。本路径在 resolve_conflict_on_submit
        本就要锁同一线程行裁决（同事务重复加锁为 no-op），锁前移不引入新的争用面。
        """
        thread = await self._get_owned_thread(thread_id, user_id, for_update=True)
        if thread.archived_at is not None:
            thread.archived_at = None
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
        """线程列表（api.md §2.2 v1.18）：user 域 + mode/q/archived/unread 过滤 + 分页。

        过滤口径（D-05）：
        - `archived` 缺省 false=活跃集（archived_at IS NULL），true=仅归档；
        - `unread`=read_at IS NULL，与 archived 正交（服务端无组合特判）；
        - `q` 为标题 ILIKE 包含匹配（大小写不敏感），`%`/`_`/`\\` 通配一律转义后
          以绑定参数下发（用户输入不放大匹配面）。

        排序（D-07）：活跃/未读视图 `pinned DESC, updated_at DESC`；归档视图
        `updated_at DESC`（pinned 不参与——归档视图无置顶语义）。

        返回 PageResponse（api.md §2.2 data={total, items} 直接口径；基座 PageResult 为
        page/page_size 语义，与 limit/offset 契约不匹配，故此端点直接产出响应 DTO）。
        """
        user_uuid = _as_uuid(user_id, label="用户")
        conditions = [Thread.user_id == user_uuid]
        if query.mode is not None:
            conditions.append(Thread.mode_summary == query.mode)
        conditions.append(
            Thread.archived_at.is_not(None) if query.archived else Thread.archived_at.is_(None)
        )
        if query.unread:
            conditions.append(Thread.read_at.is_(None))
        if query.q:
            pattern = f"%{_escape_like(query.q)}%"
            conditions.append(Thread.title.ilike(pattern, escape=_LIKE_ESCAPE))

        count_stmt = select(func.count()).select_from(Thread).where(*conditions)
        total = await self.db.scalar(count_stmt)
        order_by = (
            (Thread.updated_at.desc(),)
            if query.archived
            else (Thread.pinned.desc(), Thread.updated_at.desc())
        )
        page_stmt = (
            select(Thread)
            .where(*conditions)
            .order_by(*order_by)
            .limit(query.limit)
            .offset(query.offset)
        )
        rows = (await self.db.scalars(page_stmt)).all()
        return PageResponse(total=int(total or 0), items=[_brief(row) for row in rows])

    async def get_thread_detail(self, thread_id: str, *, user_id: str) -> ThreadDetail:
        """线程详情（api.md §2.3）：骨架聚合，turns 按 created_at 升序（轮次栏时间线）。

        v1.18 写副作用（§2.3 注 / D-12）：未读线程（read_at IS NULL）打开即置已读，
        同事务 flush；幂等——已读则零写、重复 GET 无观测差异。归档线程/生成中线程
        打开同样置位（用户正在看）。`updated_at` 不因置已读变化。

        `has_artifacts`（§2.3）：artifacts 登记行存在性真实投影——EXISTS(artifacts
        WHERE thread_id=…)，走 idx_artifacts_thread 的读侧单查询（不逐轮探测）。
        """
        thread = await self._get_owned_thread(thread_id, user_id)
        if thread.read_at is None:
            thread.read_at = _utcnow()
            await self.db.flush()
        stmt = select(Turn).where(Turn.thread_id == thread.id).order_by(Turn.created_at.asc())
        turns = (await self.db.scalars(stmt)).all()
        exists_stmt = select(exists().where(Artifact.thread_id == thread.id))
        has_artifacts = bool(await self.db.scalar(exists_stmt))
        return ThreadDetail(
            thread_id=str(thread.id),
            title=thread.title,
            mode_summary=cast(SummaryMode, thread.mode_summary),
            turn_count=thread.turn_count,
            has_artifacts=has_artifacts,
            created_at=thread.created_at,
            updated_at=thread.updated_at,
            turns=[_skeleton(t) for t in turns],
        )

    async def patch_thread(
        self, thread_id: str, payload: ThreadPatchRequest, *, user_id: str
    ) -> ThreadBrief:
        """线程管理部分更新（api.md §2.10 v1.18）：置顶/标记未读/重命名/归档统一写入口。

        语义映射（D-08）：pinned 直写；is_unread=true→read_at=NULL / false→now()；
        archived=true→archived_at=now() / false→NULL；title 已由 DTO trim 与长度校验。
        **`updated_at` 不随管理动作变化**（D-09 冻结语义：仅新轮次推进 updated_at）。
        响应=更新后的线程投影（与 §2.2 items[] 同构，前端直接回填免二次拉取）。
        """
        thread = await self._get_owned_thread(thread_id, user_id)
        if payload.title is not None:
            thread.title = payload.title
        if payload.pinned is not None:
            thread.pinned = payload.pinned
        if payload.is_unread is not None:
            thread.read_at = None if payload.is_unread else _utcnow()
        if payload.archived is not None:
            thread.archived_at = _utcnow() if payload.archived else None
        await self.db.flush()
        return _brief(thread)

    async def delete_thread(self, thread_id: str, *, user_id: str) -> ThreadDeleteData:
        """删除线程及全部从属数据（api.md §2.11 v1.18，物理删除、不可恢复）。

        - 进行中轮判定（D-10/D-11）：存在 pending/running/queued 轮 → 409005 拒绝
          （状态集与 409001 同源；pending 瞬时态并入 running 判定）；
        - 级联口径（D-03）：service 层显式删——同事务**先删该线程全部 turns**（借既有
          ON DELETE CASCADE 链清 turn_events / sources / turn_feedback 及后续 snapshots /
          M4 offload_chunks），**再删 threads**（借 artifacts 链）；不改 turns.thread_id
          FK 定义（保持无级联，防误删线程留孤儿 turns 的结构性风险）；
        - MinIO 对象 best-effort 前缀清理（D-04）：失败只 WARN 不回滚（object_cleanup）。
        归档态线程可直接删除（归档不是「进行中」）。

        锁语义（评审 P2）：归属读取取线程行级排他锁（`for_update=True`）——与
        `create_turn` 的同款锁读互斥，「判 in-flight → 删行」因此不再对并发追问开
        check-then-act 窗口：删除先持锁则追问延后并读到 404001；追问先持锁则本方法
        等其提交后看到新轮 → 409005（不再是 FK IntegrityError 500001）。
        锁持至请求事务结束（get_db commit），对象清理也在锁窗口内（见下）。

        对象清理时序取舍（best-effort，接受残余方向）：purge 位于 `flush()` 之后、
        `get_db` teardown commit 之前——① 网络耗时（列举 + 批量删除）计入持锁窗口，
        本自用量级（线程级前缀、低频删除动作）可接受；② 若随后的 commit 失败（回滚），
        对象已先删、DB 行仍在 → 快照可用性缺失（孤儿方向为「行存对象亡」），由
        详情/来源读取侧的快照缺失降级承担（§2.6 404002 语义面），不新增补偿任务。
        """
        thread = await self._get_owned_thread(thread_id, user_id, for_update=True)
        in_flight = await self.db.scalar(
            select(func.count())
            .select_from(Turn)
            .where(Turn.thread_id == thread.id, Turn.status.in_(_IN_FLIGHT_STATUSES))
        )
        if in_flight:
            raise ThreadBusyException(
                "线程有进行中轮次，不可删除",
                detail=(
                    f"thread_id={thread_id} 存在 {int(in_flight)} 个 pending/running/"
                    "queued 轮——先停止（§2.7）或等待终态后重试"
                ),
            )

        user_uuid = str(thread.user_id)
        await self.db.execute(delete(Turn).where(Turn.thread_id == thread.id))
        await self.db.execute(delete(Thread).where(Thread.id == thread.id))
        await self.db.flush()
        # best-effort：清理失败只 WARN 不抛（MinIO 抖动不阻断删除主流程；DB 行已删，
        # 孤儿对象挂二期存储治理——D-04）。实现在 service 侧兜底，任何注入实现都受保护
        try:
            await self._object_purger.purge_thread_objects(
                user_id=user_uuid, thread_id=str(thread.id)
            )
        except Exception as exc:  # noqa: BLE001 —— best-effort 边界：吞异常落 WARN
            BusinessLog(
                message=f"线程对象清理失败（best-effort，孤儿对象挂二期治理）：{exc}",
                action="线程对象清理",
                entity="thread",
                entity_id=str(thread.id),
            ).warning()
        return ThreadDeleteData(deleted=True)

    async def _get_owned_thread(
        self, thread_id: str, user_id: str, *, for_update: bool = False
    ) -> Thread:
        """线程归属校验：id+user_id 双条件查询（api.md §0 404001——未找到与越权同码，
        不泄漏资源存在性）；语句全参数绑定（select 表达式）。

        `for_update=True`：取线程行级排他锁（`SELECT … FOR UPDATE`，同
        turn_service.resolve_conflict_on_submit 的串行化先例）——DELETE 与追问两条
        写路径都走锁读，把「删除线程 / 追加轮次」串成先后事务：
        - 删除先持锁 → 追问的锁读阻塞，删除提交后重新读到行已删 → 404001；
        - 追问先持锁 → 删除的锁读阻塞到追问提交，其 in-flight 计数看到新轮 → 409005。
        锁持至请求事务结束（get_db commit/rollback），期间无 check-then-act 竞态窗口。
        """
        stmt = select(Thread).where(
            Thread.id == _as_uuid(thread_id, label="线程"),
            Thread.user_id == _as_uuid(user_id, label="用户"),
        )
        if for_update:
            stmt = stmt.with_for_update()
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
