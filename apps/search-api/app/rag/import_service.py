"""KB 导入任务 REST 业务层（app/rag/import_service.py，api.md §4.7~4.9；M3/B20 B 线④）。

覆盖面：
- §4.7 建行：进行中（pending/running）判定 → 409007；落象 pending（trigger=manual）；
  connector 合法性与凭据前置在路由层（400001 / 500001）；
- §4.8 列表：`(user_id, created_at)` 顺查 + created_at DESC（新任务在前）+ 分页转发
  （§0 分页约定：limit 默认 50 上限 200 + offset 默认 0）；
- §4.9 详情：404001（不存在/越权/非法标识同码——不泄漏存在性）；
- 投影：五数一一对应 + failure_summary 由 failures 按 category 派生不落列（G4）+
  列表条目不含 failures（§4.8）。

分层纪律：DTO 进 DTO 出；service 只 flush 不 commit（事务边界在 get_db；路由层在
BackgroundTasks 入队前显式 commit——评审 P1 时序钉正同款）；语句全参数绑定。
"""

import asyncio
import uuid
from typing import Any

from sqlalchemy import func, select

from app.core.logger import BusinessLog
from app.db.models import KbImportJob
from app.exception import NotFoundException
from app.exception.kb import KbImportJobActiveException
from app.rag.import_worker import JOB_REQUEUE_STATUSES, derive_failure_summary
from app.schemas.kb import (
    KbImportJobCreated,
    KbImportJobDetail,
    KbImportJobEntry,
    KbImportFailure,
)
from app.services.base import BaseService, PageResult

#: 进行中判定状态集（api.md §4.7 409007 触发场景逐字——pending/running）

# ---------------------------------------------------------------------------
# 同用户 admission 锁（评审 P3——check-then-act 原子性）
# ---------------------------------------------------------------------------

#: per-user 建行锁（进程内——单实例部署口径）：「409007 判定 + 建行」两步在锁内
#: 串行化，并发双击不再读出双 0 建出双 pending 行（双任务先后执行同树 diff 会产生
#: 重复导入与删除/重建竞态）。多实例部署须升 partial unique index（user_id WHERE
#: status IN (pending,running)）——data-model §1.17 现钉「零索引」，升 DDL 走契约
#: 增量 diff，挂多实例部署前置项。
_USER_ADMISSION_LOCKS: dict[str, asyncio.Lock] = {}
_ADMISSION_LOCKS_GUARD = asyncio.Lock()


async def _user_admission_lock(user_id: str) -> asyncio.Lock:
    """取（惰性建）同用户锁——锁表自身的并发建键由全局 guard 串行化。"""
    async with _ADMISSION_LOCKS_GUARD:
        lock = _USER_ADMISSION_LOCKS.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            _USER_ADMISSION_LOCKS[user_id] = lock
        return lock


def to_job_entry(row: Any) -> KbImportJobEntry:
    """kb_import_jobs 行 → §4.8 列表条目（同详情条目、不含 failures——api.md §4.8）。"""
    progress = row.progress or {}
    failures = list(row.failures or [])
    return KbImportJobEntry(
        job_id=str(row.id),
        connector=row.connector,
        trigger=row.trigger,
        status=row.status,
        total_docs=int(progress.get("total_docs") or 0),
        processed_docs=int(progress.get("processed_docs") or 0),
        imported_docs=int(progress.get("imported_docs") or 0),
        failed_docs=int(progress.get("failed_docs") or 0),
        skipped_docs=int(progress.get("skipped_docs") or 0),
        failure_summary=derive_failure_summary(failures),
        started_at=row.started_at,
        finished_at=row.finished_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def to_job_detail(row: Any) -> KbImportJobDetail:
    """kb_import_jobs 行 → §4.9 详情（failures 按处理序原样返回；failure_summary
    由 failures 派生——G4 不落存储列）。"""
    progress = row.progress or {}
    failures = [KbImportFailure(**entry) for entry in (row.failures or [])]
    return KbImportJobDetail(
        job_id=str(row.id),
        connector=row.connector,
        trigger=row.trigger,
        status=row.status,
        total_docs=int(progress.get("total_docs") or 0),
        processed_docs=int(progress.get("processed_docs") or 0),
        imported_docs=int(progress.get("imported_docs") or 0),
        failed_docs=int(progress.get("failed_docs") or 0),
        skipped_docs=int(progress.get("skipped_docs") or 0),
        failure_summary=derive_failure_summary(list(row.failures or [])),
        failures=failures,
        started_at=row.started_at,
        finished_at=row.finished_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class KbImportJobService(BaseService):
    """导入任务业务（建行 / 列表 / 详情；worker 消费面归 import_worker）。"""

    async def create_job(self, connector: str, *, user_id: str) -> KbImportJobCreated:
        """建行（trigger=manual、status=pending）：409007 判定 → flush（不 commit
        ——事务边界在 get_db；路由层显式 commit 后才入队 worker）。

        check-then-act 在同用户 admission 锁内串行化（评审 P3——并发双击原子性）。
        """
        async with await _user_admission_lock(user_id):
            active = int(
                await self.db.scalar(
                    select(func.count())
                    .select_from(KbImportJob)
                    .where(
                        KbImportJob.user_id == uuid.UUID(user_id),
                        KbImportJob.status.in_(JOB_REQUEUE_STATUSES),
                    )
                )
                or 0
            )
            if active:
                raise KbImportJobActiveException("已有进行中的知识库导入任务")
            row = KbImportJob(
                user_id=uuid.UUID(user_id),
                connector=connector,
                trigger="manual",
                status="pending",
                cursor=None,
                progress={
                    "total_docs": 0,
                    "processed_docs": 0,
                    "imported_docs": 0,
                    "failed_docs": 0,
                    "skipped_docs": 0,
                },
                failures=[],
                stats={},
            )
            self.db.add(row)
            await self.db.flush()
        BusinessLog(
            message="kb 导入任务创建（manual）",
            action="kb_import_create",
            entity="kb_import_job",
            entity_id=str(row.id),
        ).info()
        return KbImportJobCreated(job_id=str(row.id), status="pending")

    async def list_jobs(
        self, limit: int, offset: int, *, user_id: str
    ) -> PageResult[KbImportJobEntry]:
        """分页列表（§4.8）：created_at DESC 新任务在前；user 隔离。"""
        uid = uuid.UUID(user_id)
        total = int(
            await self.db.scalar(
                select(func.count())
                .select_from(KbImportJob)
                .where(KbImportJob.user_id == uid)
            )
            or 0
        )
        rows = (
            await self.db.scalars(
                select(KbImportJob)
                .where(KbImportJob.user_id == uid)
                .order_by(KbImportJob.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return PageResult(
            items=[to_job_entry(row) for row in rows],
            total=total,
            page=offset // limit + 1,
            page_size=limit,
        )

    async def get_job(self, job_id: str, *, user_id: str) -> KbImportJobDetail:
        """详情（§4.9）：404001 不存在 / 越权 / 非法标识同语义。"""
        row = await self._load_owned(job_id, user_id)
        return to_job_detail(row)

    async def _load_owned(self, job_id: str, user_id: str) -> Any:
        job_uuid = _parse_job_id(job_id)
        row = (
            await self.db.scalars(
                select(KbImportJob).where(
                    KbImportJob.id == job_uuid,
                    KbImportJob.user_id == uuid.UUID(user_id),
                )
            )
        ).first()
        if row is None:
            raise NotFoundException(
                "任务不存在",
                detail=f"job_id={job_id} 不存在或不属于当前用户",
            )
        return row


def _parse_job_id(job_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(job_id)
    except ValueError as exc:
        raise NotFoundException(
            "任务不存在", detail=f"job_id 非法：{job_id[:64]}"
        ) from exc
