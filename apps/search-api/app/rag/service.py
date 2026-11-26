"""KB REST 业务层（app/rag/service.py，api.md §4.1~§4.5；M3/B17）。

覆盖面（与 test_kb_service 对应）：
- §4.1 上传校验（纯函数 validate_upload：扩展名白名单 + 20MB 上限 + 音视频
  415001「暂不支持」措辞）与落象（**对象 → 记录**顺序：原件先落 MinIO，MinIO
  失败 → IrStorageError、零行——事务边界在 get_db，service 只 flush 不 commit）；
- §4.2 URL 收藏：B15 SSRF 护栏校验（guard=None kill switch 直通——与 safety.enabled
  解耦口径）→ 拒收 422001 + `url_rejected` 语义留痕（零抓取零建行）→ 同 URL
  重复返回既有条目 duplicated=true（零抓取）→ 新 URL 抓取落象（title 取 H1 /
  host 兜底，file_size_bytes 恒 null）；
- §4.3/§4.4 投影（to_kb_document：parse_meta 三键投影、source_url 可选键缺则省略）
  与过滤转发（status/doc_type/limit/offset）；
- §4.5 删除级联：行删（kb_chunks 经 FK CASCADE）+ sources 反向引用清理
  （kb_doc_id/kb_chunk_ids 置 null——零 REFERENCES 手工维护）+ MinIO 前缀清理
  best-effort（失败 WARN 不抛）。

分层纪律：DTO 进 DTO 出（KbDocument 系列出 service 层）；语句全参数绑定
（SQLAlchemy 表达式）；日志走 LogRecord 子类体系（BusinessLog），零内容回显。
"""

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import func, select, update

from app.agent.safety.config import load_content_guard_config
from app.agent.safety.content_guard import validate_url_async
from app.core.config import kb_settings, minio_settings
from app.core.logger import BusinessLog
from app.db.models import KbChunk as KbChunkRow
from app.db.models import KbDocument as KbDocumentRow
from app.db.models import Source
from app.exception import BadRequestException, NotFoundException
from app.exception.kb import (
    FileTooLargeException,
    FileUnsupportedTypeException,
    KbUrlInvalidException,
)
from app.schemas.kb import (
    KbChunkDetail,
    KbDeleteData,
    KbDocument,
    KbDocumentCollected,
    KbDocumentCreated,
    KbDocumentListQuery,
)
from app.services.base import BaseService, PageResult
from app.rag.pipeline import extract_url_title, original_ext_for_doc_type
from app.rag.storage import IrStorageError, original_object_key

__all__ = [
    "FileTooLargeException",
    "FileUnsupportedTypeException",
    "KbDocumentListQuery",
    "KbService",
    "KbUrlInvalidException",
    "MinioPrefixPurger",
    "to_kb_document",
    "validate_upload",
]

#: 音视频扩展名（api.md §4.1 :527 钉注——同 415001 码，message 必含「暂不支持」；
#: 现行白名单不含音视频，本集合只负责 message 措辞分叉，不是第二白名单）
_AUDIO_VIDEO_EXTS: frozenset[str] = frozenset(
    {"mp3", "wav", "m4a", "flac", "aac", "ogg", "opus",
     "mp4", "avi", "mov", "mkv", "webm", "wmv", "flv"}
)

#: 列宽（data-model §1.7 title varchar(512)）
_TITLE_MAX = 512

#: 预签名默认——未使用（purger 用直删；占位注：后续对象生命周期统一走 MinIO 面）
_OBJECT_PREFIX = "kb"


# ---------------------------------------------------------------------------
# 纯函数校验面
# ---------------------------------------------------------------------------


def validate_upload(
    filename: str, size: int, *, allowed_types: list[str], max_mb: int
) -> str:
    """上传入口校验：扩展名白名单 + 大小上限；返回归一化扩展名（小写、无点）。

    异常语义（api.md §4.1）：
    - 无扩展名 / 白名单外（非音视频）→ 415001（message 不含「暂不支持」）；
    - 音视频扩展名 → 415001 且 message 必含「暂不支持」（api.md §4.1 :527 钉注）；
    - 超上限 → 413001（KB_MAX_FILE_MB）。
    """
    if "." not in filename:
        raise FileUnsupportedTypeException(
            "不支持的文件类型（缺少扩展名）", detail=f"filename 缺扩展名：len={len(filename)}"
        )
    ext = filename.rsplit(".", 1)[1].lower()
    if ext in _AUDIO_VIDEO_EXTS:
        # 音视频一期不支持——显式措辞钉注（api.md §4.1 :527）
        raise FileUnsupportedTypeException(
            f"音视频文件暂不支持（.{ext}）", detail=f"ext={ext}"
        )
    if ext not in allowed_types:
        raise FileUnsupportedTypeException(
            f"不支持的文件类型（.{ext}）",
            detail=f"ext={ext} allowed={'/'.join(sorted(allowed_types))}",
        )
    if size > max_mb * 1024 * 1024:
        raise FileTooLargeException(
            f"文件超过大小上限（{max_mb}MB）", detail=f"size={size} max_mb={max_mb}"
        )
    return ext


def _truncate_title(title: str) -> str:
    return title[:_TITLE_MAX]


def to_kb_document(row: Any) -> KbDocument:
    """kb_documents 行 → §4.3/§4.4 DTO（parse_meta 三键投影；source_url 可选键
    仅 url 行设置——构造期未设置的键经 exclude_unset 序列化「缺则省略」）。"""
    from app.rag.pipeline import project_parse_meta

    payload: dict[str, Any] = {
        "document_id": str(row.id),
        "doc_type": row.doc_type,
        "title": row.title,
        "file_size_bytes": row.file_size_bytes,
        "status": row.status,
        "fail_reason": row.fail_reason,
        "chunk_count": row.chunk_count,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "ready_at": row.ready_at,
        "parse_meta": project_parse_meta(row.parse_meta),
        "catalog_ids": row.catalog_ids,
        "catalog_path": row.catalog_path,
    }
    if row.source_url:
        payload["source_url"] = row.source_url
    return KbDocument(**payload)


# ---------------------------------------------------------------------------
# 依赖默认实现
# ---------------------------------------------------------------------------


class MinioPrefixPurger:
    """MinIO kb 前缀清理（删除级联 best-effort 段；对象直删不走预签名）。"""

    async def purge(self, *, user_id: str, doc_id: str) -> None:
        from app.rag.storage import get_kb_minio_client

        client = get_kb_minio_client()
        prefix = f"{_OBJECT_PREFIX}/{user_id}/{doc_id}/"
        bucket = minio_settings.bucket_kb

        def _purge() -> int:
            objects = list(client.list_objects(bucket, prefix=prefix, recursive=True))
            for obj in objects:
                client.remove_object(bucket, obj.object_name)
            return len(objects)

        removed = await asyncio.to_thread(_purge)
        BusinessLog(
            message=f"kb MinIO 前缀清理完成（{removed} 个对象）",
            action="kb_purge",
            entity="kb_document",
            entity_id=doc_id,
        ).info()


# ---------------------------------------------------------------------------
# 业务服务
# ---------------------------------------------------------------------------


class KbService(BaseService):
    """KB 文档业务（上传 / URL 收藏 / 列表 / 详情 / 删除）。

    依赖注入面（单测假体零网络零 DB；生产经 api 层 `get_kb_service` 装配）：
    - `minio_client`：MinIO 客户端工厂（storage.get_kb_minio_client 惰性单例）；
    - `object_purger`：删除级联的 MinIO 前缀清理（缺省 MinioPrefixPurger）；
    - `url_fetcher`：URL 抓取缝 `async (url, spec) -> markdown 文本`（None=通道
      未配置——collect_url 显式拒绝，不静默）；
    - `spec_payload_provider`：Agent Spec 快照供给（护栏配置 / 后续 kb 节键）；
    - `es_indexer`：删除级联 ES 腿（B18——KbEsIndexer 协议 delete_document；
      None=零操作兜底，生产经 get_kb_service 注入 get_kb_es_indexer()）。
    """

    def __init__(
        self,
        db: Any,
        *,
        minio_client: Callable[[], Any] | None = None,
        object_purger: Any = None,
        url_fetcher: Callable[[str, dict[str, Any]], Any] | None = None,
        spec_payload_provider: Callable[[], dict[str, Any]] | None = None,
        es_indexer: Any | None = None,
    ) -> None:
        super().__init__(db)
        from app.rag.storage import get_kb_minio_client

        self._minio_factory = minio_client or get_kb_minio_client
        self._purger = object_purger if object_purger is not None else MinioPrefixPurger()
        self._url_fetcher = url_fetcher
        self._spec_payload_provider = spec_payload_provider or (lambda: {})
        self._es_indexer = es_indexer

    # ---- §4.1 上传 ----

    async def upload_document(
        self, data: bytes, filename: str, title: str | None, *, user_id: str
    ) -> KbDocumentCreated:
        """上传落象：校验 → 原件落 MinIO（对象 → 记录顺序）→ 建行 pending。"""
        ext = validate_upload(
            filename,
            len(data),
            allowed_types=kb_settings.allowed_types,
            max_mb=kb_settings.max_file_mb,
        )
        final_title = _truncate_title(title if title else Path(filename).stem)
        doc_id = uuid.uuid4()
        key = original_object_key(user_id, str(doc_id), ext)
        await self._put_original(key, data)
        created_at = datetime.now(UTC)
        row = KbDocumentRow(
            id=doc_id,
            user_id=uuid.UUID(user_id),
            doc_type=ext,
            title=final_title,
            source_url=None,
            file_object_key=key,
            file_size_bytes=len(data),
            status="pending",
            fail_reason=None,
            chunk_count=0,
            parse_meta={},
            created_at=created_at,
            updated_at=created_at,
        )
        self.db.add(row)
        await self.db.flush()  # 事务边界在 get_db（service 只 flush 不 commit）
        return KbDocumentCreated(
            document_id=str(doc_id),
            doc_type=ext,
            title=final_title,
            file_size_bytes=len(data),
            status="pending",
            fail_reason=None,
            chunk_count=0,
            created_at=created_at,
        )

    async def _put_original(self, key: str, data: bytes) -> None:
        """原件写入（对象先于记录；IO 失败归一 IrStorageError——上传段零行）。"""
        client = self._minio_factory()
        bucket = minio_settings.bucket_kb

        def _put() -> None:
            if not client.bucket_exists(bucket):
                client.make_bucket(bucket)
            import io

            client.put_object(bucket, key, io.BytesIO(data), length=len(data))

        try:
            await asyncio.to_thread(_put)
        except Exception as exc:
            raise IrStorageError(
                f"原件写入失败：{type(exc).__name__}（key={key}）"
            ) from exc

    # ---- §4.2 URL 收藏 ----

    async def collect_url(self, url: str, *, user_id: str) -> KbDocumentCollected:
        """URL 收藏：护栏 → 去重 → 抓取 → 落象（对象 → 记录）。"""
        spec = self._spec_payload_provider()
        guard = load_content_guard_config(spec).url_guard
        if guard is not None:  # guard None = kill switch（B15 与 safety.enabled 解耦）
            verdict = await validate_url_async(url, guard)
            if not verdict.ok:
                # url_rejected 语义留痕（零 URL/host 回显——reason 为固定枚举）
                BusinessLog(
                    message="url_rejected：URL 收藏被 SSRF 护栏拒收（零抓取零建行）",
                    action="collect_url",
                    entity="kb_url",
                ).warning()
                raise KbUrlInvalidException(
                    "URL 非法或不可访问", detail=f"reason={verdict.reason}"
                )

        existing = (
            await self.db.scalars(
                select(KbDocumentRow).where(
                    KbDocumentRow.user_id == uuid.UUID(user_id),
                    KbDocumentRow.doc_type == "url",
                    KbDocumentRow.source_url == url,
                )
            )
        ).first()
        if existing is not None:
            # 同 URL 重复收藏：返回既有条目（duplicated=true）——零抓取零建行
            return KbDocumentCollected(
                document_id=str(existing.id),
                doc_type=existing.doc_type,
                title=existing.title,
                # 命中本行即 source_url == url（去重 WHERE 同源）——运行时非空，
                # `or url` 仅为类型面收敛（列契约 doc_type=url 恒非 null）
                source_url=existing.source_url or url,
                file_size_bytes=existing.file_size_bytes,
                status=existing.status,
                fail_reason=existing.fail_reason,
                chunk_count=existing.chunk_count,
                created_at=existing.created_at,
                duplicated=True,
            )

        if self._url_fetcher is None:
            raise KbUrlInvalidException(
                "URL 收藏通道不可用", detail="url_fetcher 未配置（生产装配缺位）"
            )
        try:
            fetched = await self._url_fetcher(url, spec)
        except Exception as exc:
            raise KbUrlInvalidException(
                "URL 抓取失败", detail=f"fetcher={type(self._url_fetcher).__name__}",
                original_exception=exc,
            ) from exc
        title = _truncate_title(extract_url_title(fetched, url))
        doc_id = uuid.uuid4()
        key = original_object_key(user_id, str(doc_id), original_ext_for_doc_type("url"))
        await self._put_original(key, fetched.encode("utf-8"))
        created_at = datetime.now(UTC)
        row = KbDocumentRow(
            id=doc_id,
            user_id=uuid.UUID(user_id),
            doc_type="url",
            title=title,
            source_url=url,
            file_object_key=key,
            file_size_bytes=None,  # URL 类型恒 null（§4.2）
            status="pending",
            fail_reason=None,
            chunk_count=0,
            parse_meta={},
            created_at=created_at,
            updated_at=created_at,
        )
        self.db.add(row)
        await self.db.flush()
        return KbDocumentCollected(
            document_id=str(doc_id),
            doc_type="url",
            title=title,
            source_url=url,
            file_size_bytes=None,
            status="pending",
            fail_reason=None,
            chunk_count=0,
            created_at=created_at,
            duplicated=False,
        )

    # ---- §4.3 列表 / §4.4 详情 ----

    async def list_documents(
        self, query: KbDocumentListQuery, *, user_id: str
    ) -> PageResult[KbDocument]:
        """分页列表（新在前；status/doc_type 过滤转发——空值=不过滤）。"""
        filters = [KbDocumentRow.user_id == uuid.UUID(user_id)]
        if query.status:
            filters.append(KbDocumentRow.status == query.status)
        if query.doc_type:
            filters.append(KbDocumentRow.doc_type == query.doc_type)
        total = int(
            await self.db.scalar(
                select(func.count())
                .select_from(KbDocumentRow)
                .where(*filters)  # type: ignore[arg-type]
            )
            or 0
        )
        rows = (
            await self.db.scalars(
                select(KbDocumentRow)
                .where(*filters)  # type: ignore[arg-type]
                .order_by(KbDocumentRow.created_at.desc())
                .limit(query.limit)
                .offset(query.offset)
            )
        ).all()
        return PageResult(
            items=[to_kb_document(row) for row in rows],
            total=total,
            page=query.offset // query.limit + 1,
            page_size=query.limit,
        )

    async def get_document(self, document_id: str, *, user_id: str) -> KbDocument:
        """详情（404001：不存在 / 越权 / 非法标识同语义）。"""
        row = await self._load_owned(document_id, user_id)
        return to_kb_document(row)

    async def _load_owned(self, document_id: str, user_id: str) -> Any:
        doc_uuid = _parse_document_id(document_id)
        row = (
            await self.db.scalars(
                select(KbDocumentRow).where(
                    KbDocumentRow.id == doc_uuid,
                    KbDocumentRow.user_id == uuid.UUID(user_id),
                )
            )
        ).first()
        if row is None:
            raise NotFoundException(
                "文档不存在",
                detail=f"document_id={document_id} 不存在或不属于当前用户",
            )
        return row

    # ---- §4.6 溯源下钻（B19——api.md §4.6 已冻结契约纯实现批）----

    async def get_chunk_drilldown(self, source_id: str, *, user_id: str) -> KbChunkDetail:
        """KB 来源溯源下钻（REQ-053/A36）：sources 行（id+user 双过滤）→
        source_type≠"kb" → 400001；kb_chunk_id（kb_chunk_ids[0]）→ kb_chunks 行
        （user_id 双过滤）→ kb_documents 行——任一缺失=已删除/越权/不存在 →
        **404001 单码**（不泄漏存在性，契约字面）。

        投影：六必选 + 两可选——meta.heading_path jsonb list 直投；meta.loc
        {page?, para?} 缺则 null 键恒出现；meta.node_id 形态 `fs:node:{token}` 解析
        token（两可选键齐带）、`fs:blk:` 前缀保留 node_id 省略 node_token（修订注 2
        ——收官用户裁决候选）、其余前缀两键省略。**不拉 ir.json**（溯源 O(1) 契约
        走 meta 物化字段）；content 仅进 UI 不进 LLM（转义归前端 DOMPurify）。
        """
        try:
            source_uuid = uuid.UUID(source_id)
            user_uuid = uuid.UUID(user_id)
        except ValueError as exc:
            raise NotFoundException(
                "来源不存在", detail=f"非法 source/user id：{source_id!r}"
            ) from exc
        source = await self.db.get(Source, source_uuid)
        if source is None or source.user_id != user_uuid:
            raise NotFoundException(
                "来源不存在", detail=f"source_id={source_id} 不存在或不属于当前用户"
            )
        if source.source_type != "kb":
            raise BadRequestException(
                "非 KB 来源", detail=f"source_type={source.source_type}（溯源仅 KB 卡）"
            )
        chunk_ids = source.kb_chunk_ids if isinstance(source.kb_chunk_ids, list) else []
        if not chunk_ids:
            raise NotFoundException("来源不存在", detail="来源行无 kb_chunk_id 解析键")
        try:
            chunk_uuid = uuid.UUID(str(chunk_ids[0]))
        except ValueError as exc:
            raise NotFoundException("来源不存在", detail="kb_chunk_id 形态非法") from exc
        chunk = await self.db.get(KbChunkRow, chunk_uuid)
        if chunk is None or chunk.user_id != user_uuid:
            raise NotFoundException(
                "来源不存在", detail="对应 chunk 不存在/已删除或越权"
            )
        doc = await self.db.get(KbDocumentRow, chunk.kb_doc_id)
        if doc is None or doc.user_id != user_uuid:
            raise NotFoundException(
                "来源不存在", detail="对应文档不存在/已删除或越权"
            )
        meta = chunk.meta if isinstance(chunk.meta, dict) else {}
        heading_path = [str(seg) for seg in (meta.get("heading_path") or [])]
        raw_loc = meta.get("loc")
        loc = raw_loc if isinstance(raw_loc, dict) else {}
        payload: dict[str, Any] = {
            "chunk_id": str(chunk.id),
            "document_id": str(doc.id),
            "document_title": str(doc.title or ""),
            "content": str(chunk.content or ""),
            "heading_path": heading_path,
            "page": loc.get("page"),
            "paragraph": loc.get("para"),
        }
        node_id = meta.get("node_id")
        if isinstance(node_id, str) and node_id.startswith("fs:node:"):
            payload["node_token"] = node_id.removeprefix("fs:node:")
            payload["node_id"] = node_id
        elif isinstance(node_id, str) and node_id.startswith("fs:blk:"):
            # 修订注 2：fs:blk: 保留 node_id、省略 node_token（无 wiki 外跳 token）
            payload["node_id"] = node_id
        return KbChunkDetail.model_validate(payload)

    # ---- §4.5 删除级联 ----

    async def delete_document(self, document_id: str, *, user_id: str) -> KbDeleteData:
        """删除级联：行删（kb_chunks 经 FK CASCADE）→ sources 反向引用清理（置
        null，零 REFERENCES 手工维护）→ ES 腿（B18，best-effort——失败 WARN 不
        阻断，残余孤儿归 audit 治理）→ MinIO 前缀清理 best-effort（失败 WARN）。
        响应恒 deleted=true（api.md §4.5 契约零变更、零新增错误码）。"""
        row = await self._load_owned(document_id, user_id)
        doc_uuid = _parse_document_id(document_id)
        await self.db.delete(row)
        await self.db.execute(
            update(Source)
            .where(Source.kb_doc_id == doc_uuid)
            .values(kb_doc_id=None, kb_chunk_ids=None)
        )
        if self._es_indexer is not None:
            try:
                await self._es_indexer.delete_document(
                    kb_doc_id=document_id, user_id=user_id
                )
            except Exception as exc:  # noqa: BLE001 —— ES 抖动不阻断删除主流程（best-effort，
                # plan B18 §6.2：pg 事实源先删，ES 残留孤儿由 audit --purge-orphans 兜底）
                BusinessLog(
                    message=f"kb ES 删除腿失败（{type(exc).__name__}）——best-effort 继续",
                    action="kb_purge_es",
                    entity="kb_document",
                    entity_id=document_id,
                ).warning(exc)
        try:
            await self._purger.purge(user_id=user_id, doc_id=document_id)
        except Exception as exc:  # noqa: BLE001 —— MinIO 抖动不阻断删除主流程（best-effort）
            BusinessLog(
                message=f"kb MinIO 前缀清理失败（{type(exc).__name__}）——best-effort 继续",
                action="kb_purge",
                entity="kb_document",
                entity_id=document_id,
            ).warning(exc)
        return KbDeleteData(deleted=True)


def _parse_document_id(document_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(document_id)
    except ValueError as exc:
        raise NotFoundException(
            "文档不存在", detail=f"document_id 非法：{document_id[:64]}"
        ) from exc
