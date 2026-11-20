"""快照与产物域服务（api.md §5「快照与产物」——REQ-015 §5.1 / REQ-016 §2.8/§5.3，
M2 批次 4；§5.2 artifacts 列表归 M4）。

分层：DTO 进 DTO 出（ORM 不出本层）、**只 flush 不 commit**（事务边界在请求级
get_db——BaseService 铁律；本服务无 BackgroundTask 无需 scope 特化）。归属校验：
id + user_id 双条件（与 get_turn_detail/cancel_turn 同口径，不存在与越权同码
404001——不泄漏存在性）。

**导出门禁（409003）**：mode=deep ∧ answer_md 非空 ∧ status∈{completed,degraded}
——与 §2.5 export_available 判据同源（turn_service.py:444-446 投影先例）；
completed|degraded 均可导出（§0:43）。

**产物文件**：`threads/{thread_id}/report_{YYYYMMDD_HHmm}.md`（§2.8 契约格式——
文件名/rel_path 纯代码生成零用户输入拼接；时间戳分钟精度，同分钟重复导出同名文件
覆写（内容等价——同一 answer_md）、产物行按次登记=「重复导出生成新文件」契约面）。
**同分钟跨轮碰撞（409003，2026-11-20 终审 P2-1 收口）**：rel_path 不含 turn 维度——
同线程两个不同轮在同一分钟内先后导出会覆写物理文件，首产物行此后下载到第二轮报告
正文。写文件/登记行**之前**查同 rel_path 且不同 turn_id 的既有 artifact 行，命中即
`ExportNotAvailableException`（409003「不满足导出条件」，复用既有错误码）；同轮重复
导出（同 turn_id）维持放行（覆写+按次登记，内容等价）。残余并发面：同分钟两请求
同时查空后并发写属请求级事务竞态（DDL 无唯一约束，data-model 零变更）——分钟窗
极窄，如需硬保证须以唯一索引兜底（未来增量）。
**路径锚定（纵深防御）**：下载前 resolve 后必须位于 SANDBOX_ARTIFACTS_DIR 之下
（rel_path 纯代码生成 + DB 污染防御双保险，Mimosa 安全面）。

**下载形态（契约「302 预签名或 200 FileResponse 择一」落 200 FileResponse）**：
产物为本机沙箱目录文件（SANDBOX_ARTIFACTS_DIR）非 MinIO 对象——302 预签名面不适用；
路由层须自设 request.state.is_stream=True 跳过信封包装（core/middleware/response.py
:8-10 非 JSON 响应逃逸面——FileResponse 经 json.loads 必炸 500001）。

**快照读取（§5.1）**：source.snapshot_id（data-model §1.5 M2 增列）→ snapshots 行 →
MinIO 预签名 GET（10 分钟有效期，save_snapshot 同桶）+ 对象正文前 2000 字符摘录
（读取期口径——save_snapshot.py:15-18 钉注：DDL 无正文列）。404001 来源不存在/越权；
404002 无快照（快照行缺失的防御形态同码——行与对象一致性由回填链路保证）。
MinIO 客户端工厂可注入（单测 mock）；缺省惰性工厂（save_snapshot._default_minio_client_factory
同构——settings 单例 + importlib 解析，密钥零打印零落盘）。
"""

import asyncio
import importlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from fastapi.responses import FileResponse
from sqlalchemy import select

from app.core.config import minio_settings, sandbox_settings
from app.db.models import Artifact, Snapshot, Source, Turn
from app.exception import ExportNotAvailableException, NotFoundException, SnapshotNotFoundException
from app.schemas.artifact import SnapshotDetail, TurnExportResult
from app.services.base import BaseService

__all__ = ["ArtifactService"]

#: 摘录长度（api.md §5.1「开头 2000 字符」）
_EXCERPT_MAX_CHARS = 2000
#: 摘录读取字节窗（≥2000 字符的多字节上限——4 字节/字符 × 2000 = 8000，取 8192 对齐）
_EXCERPT_READ_BYTES = 8192
#: 预签名有效期（api.md §5.1「10 分钟」）
_PRESIGN_EXPIRES = timedelta(minutes=10)
#: 可导出终态集合（§0:43——completed|degraded）
_EXPORT_STATUSES = frozenset({"completed", "degraded"})


def _report_filename(now: datetime) -> str:
    """§2.8 文件名契约 `report_{YYYYMMDD_HHmm}.md`（分钟精度、代码侧生成）。"""
    return f"report_{now.strftime('%Y%m%d_%H%M')}.md"


def _export_gate(mode: Any, status: Any, answer_md: Any) -> bool:
    """导出门禁（409003 判据——§2.5 export_available 同源）：mode=deep ∧ answer_md
    非空 ∧ status∈{completed,degraded}。"""
    return bool(mode == "deep" and answer_md and status in _EXPORT_STATUSES)


def _excerpt_of(content: str, limit: int = _EXCERPT_MAX_CHARS) -> str:
    """§5.1 摘录＝对象正文开头 2000 字符（读取期口径）。"""
    return content[:limit]


def _resolve_artifact_path(base: Path | str, rel_path: str) -> Path:
    """产物路径锚定（纵深防御）：resolve 后必须位于 base 之下——穿越/绝对路径
    形态拒绝（ValueError，零文件系统副作用）。"""
    base_resolved = Path(base).resolve()
    target = (base_resolved / rel_path).resolve()
    if not target.is_relative_to(base_resolved):
        raise ValueError(f"产物路径逃逸锚定目录：{rel_path!r}")
    return target


def _default_minio_client_factory() -> Any:
    """MinIO 客户端惰性构造（save_snapshot._default_minio_client_factory 同构：
    SDK 运行期 importlib 解析——导入期不阻塞应用启动）。"""
    minio = importlib.import_module("minio")
    return minio.Minio(
        minio_settings.endpoint,
        access_key=minio_settings.access_key.get_secret_value(),
        secret_key=minio_settings.secret_key.get_secret_value(),
        secure=minio_settings.secure,
    )


class ArtifactService(BaseService):
    """快照与产物域服务（§5）：会话由 api 层依赖注入（get_db 请求级事务）；
    MinIO 客户端工厂可注入（单测 mock——缺省惰性工厂）。"""

    def __init__(self, db: Any, *, minio_client_factory: Callable[[], Any] | None = None) -> None:
        super().__init__(db)
        self._minio_client_factory = minio_client_factory or _default_minio_client_factory

    # ---- POST /turns/{turn_id}/export（§2.8，REQ-016）----

    async def export_turn_report(self, turn_id: str, *, user_id: str) -> TurnExportResult:
        """导出深度报告：门禁（404001/409003）→ 同分钟跨轮碰撞检查（409003）→
        产物文件写入 + artifacts 行登记（flush 事务边界在 get_db）→ §2.8 八键结果。
        重复导出按次登记新产物行（同轮不属碰撞）。"""
        try:
            turn_uuid = uuid.UUID(turn_id)
            user_uuid = uuid.UUID(user_id)
        except ValueError as exc:
            raise NotFoundException(
                "轮次不存在", detail=f"非法 turn/user id：{turn_id!r}"
            ) from exc
        turn = (
            await self.db.scalars(
                select(Turn).where(Turn.id == turn_uuid, Turn.user_id == user_uuid)
            )
        ).first()
        if turn is None:
            raise NotFoundException(
                "轮次不存在", detail=f"turn_id={turn_id} 不存在或不属于当前用户"
            )
        if not _export_gate(turn.mode, turn.status, turn.answer_md):
            raise ExportNotAvailableException(
                "不满足导出条件",
                detail=f"mode={turn.mode} status={turn.status} answer_md={'非空' if turn.answer_md else '空'}",
            )

        assert turn.answer_md is not None  # 门禁后 narrowing（mypy/pyright 面）
        now = datetime.now(UTC)
        filename = _report_filename(now)
        rel_path = f"threads/{turn.thread_id}/{filename}"
        # 同分钟跨轮碰撞（P2-1）：rel_path 分钟精度不含 turn 维度——同线程不同轮
        # 同分钟先后导出会覆写物理文件（首产物行此后下载到第二轮正文）。写文件前
        # 拒：同 rel_path 且不同 turn_id 的既有产物行 ⇒ 409003（复用既有错误码）；
        # 同轮重复导出（同 turn_id）不属碰撞，维持覆写+按次登记。
        existing_turn_ids = (
            await self.db.scalars(
                select(Artifact.turn_id).where(Artifact.rel_path == rel_path)
            )
        ).all()
        if any(tid is not None and tid != turn.id for tid in existing_turn_ids):
            raise ExportNotAvailableException(
                "不满足导出条件",
                detail=(
                    f"同分钟跨轮碰撞：rel_path={rel_path!r} 已有其他轮产物行"
                    f"（turn_id={turn_id}）"
                ),
            )
        base = Path(sandbox_settings.artifacts_dir)
        target = _resolve_artifact_path(base, rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = turn.answer_md.encode("utf-8")
        target.write_bytes(data)

        artifact = Artifact(
            id=uuid.uuid4(),
            user_id=turn.user_id,
            thread_id=turn.thread_id,
            turn_id=turn.id,
            artifact_type="report",
            filename=filename,
            rel_path=rel_path,
            size_bytes=len(data),
            content_type="text/markdown",
            created_at=now,  # 显式赋值：行与 DTO 同值（§2.8 created_at 形态一致）
        )
        self.db.add(artifact)
        await self.db.flush()  # 只 flush 不 commit（铁律 10——事务边界在 get_db）

        return TurnExportResult(
            artifact_id=str(artifact.id),
            thread_id=str(turn.thread_id),
            artifact_type="report",
            filename=filename,
            size_bytes=len(data),
            content_type="text/markdown",
            created_at=now,
            download_path=(
                f"/api/v1/threads/{turn.thread_id}/artifacts/{artifact.id}/download"
            ),
        )

    # ---- GET /threads/{thread_id}/artifacts/{artifact_id}/download（§5.3）----

    async def open_download(
        self, thread_id: str, artifact_id: str, *, user_id: str
    ) -> FileResponse:
        """产物下载（200 FileResponse——契约择一面落本形态）：归属+路径段一致性校验
        （404001 不泄漏存在性）→ 锚定校验 → FileResponse（路由层自设 is_stream 逃逸
        信封）。产物行在而文件缺失（孤儿行防御形态）→ 404001 同码。"""
        try:
            artifact_uuid = uuid.UUID(artifact_id)
            user_uuid = uuid.UUID(user_id)
        except ValueError as exc:
            raise NotFoundException(
                "产物不存在", detail=f"非法 artifact/user id：{artifact_id!r}"
            ) from exc
        artifact = (
            await self.db.scalars(
                select(Artifact).where(
                    Artifact.id == artifact_uuid, Artifact.user_id == user_uuid
                )
            )
        ).first()
        if artifact is None or str(artifact.thread_id) != thread_id:
            # 越权/不存在/路径段不符同码——不泄漏存在性（get_turn_detail 同口径）
            raise NotFoundException(
                "产物不存在",
                detail=f"artifact_id={artifact_id} 不存在、不属于当前用户或与线程不符",
            )
        target = _resolve_artifact_path(sandbox_settings.artifacts_dir, artifact.rel_path)
        if not target.is_file():
            raise NotFoundException(
                "产物文件缺失", detail=f"rel_path={artifact.rel_path!r} 无对应文件"
            )
        return FileResponse(
            target,
            media_type=artifact.content_type,
            filename=artifact.filename,
        )

    # ---- GET /sources/{source_id}/snapshot（§5.1，REQ-015）----

    async def get_source_snapshot(self, source_id: str, *, user_id: str) -> SnapshotDetail:
        """快照查看：来源归属校验（404001）→ snapshot_id 判空（404002）→ MinIO
        预签名（10 分钟）+ 前 2000 字符摘录 → §5.1 七键结果。"""
        try:
            source_uuid = uuid.UUID(source_id)
            user_uuid = uuid.UUID(user_id)
        except ValueError as exc:
            raise NotFoundException(
                "来源不存在", detail=f"非法 source/user id：{source_id!r}"
            ) from exc
        source = (
            await self.db.scalars(
                select(Source).where(Source.id == source_uuid, Source.user_id == user_uuid)
            )
        ).first()
        if source is None:
            raise NotFoundException(
                "来源不存在", detail=f"source_id={source_id} 不存在或不属于当前用户"
            )
        if source.snapshot_id is None:
            raise SnapshotNotFoundException(
                "该来源无快照", detail=f"source_id={source_id} 未抓取原文"
            )
        snapshot = (
            await self.db.scalars(
                select(Snapshot).where(Snapshot.id == source.snapshot_id)
            )
        ).first()
        if snapshot is None:
            # 防御形态：source.snapshot_id 指向的行缺失（回填链路一致性破绽）——
            # 同 404002 收口（用户语义等价：该来源无可用快照）
            raise SnapshotNotFoundException(
                "该来源无快照", detail=f"snapshot_id={source.snapshot_id} 行缺失"
            )

        client = await asyncio.to_thread(self._minio_client_factory)
        download_url = await asyncio.to_thread(
            client.presigned_get_object,
            minio_settings.bucket_snapshot,
            snapshot.object_key,
            expires=_PRESIGN_EXPIRES,
        )
        excerpt = await asyncio.to_thread(
            self._read_excerpt,
            client,
            minio_settings.bucket_snapshot,
            snapshot.object_key,
        )
        return SnapshotDetail(
            snapshot_id=str(snapshot.id),
            url=snapshot.url,
            content_type=snapshot.content_type,
            size_bytes=int(snapshot.size_bytes),
            fetched_at=snapshot.fetched_at,
            download_url=download_url,
            excerpt=excerpt,
        )

    @staticmethod
    def _read_excerpt(client: Any, bucket: str, object_key: str) -> str:
        """对象正文摘录（同步 SDK 面——asyncio.to_thread 下放；首窗 8192 字节 ≥
        2000 字符的 4 字节/字符上限；UTF-8 解码容错+截断）。"""
        response = client.get_object(bucket, object_key)
        try:
            data = response.read(_EXCERPT_READ_BYTES)
        finally:
            response.close()
            response.release_conn()
        return _excerpt_of(data.decode("utf-8", errors="ignore"))
