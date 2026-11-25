"""KB 入库流水线（M3/B17 主线三）：六态状态机 worker——归一化 → 逐级切分 → 索引确认。

契约锚点：
- api.md §4.1（上传即 pending；入库流水线推进六态）/ §4.3（fail_reason 阶段化
  `{stage}: {detail}`，stage ∈ normalizing|chunking|indexing；pending 期失败无前缀）；
- data-model.md §1.7 六态状态机（pending → normalizing → chunking → indexing →
  ready，任一阶段失败 failed；乐观状态卫 `UPDATE ... WHERE status IN expect`——
  rowcount 0 = 行已被并发改写/删除 → **abort 不覆盖他人状态**）；§1.18（ir.json
  键约定与装载钉注——经 storage.get_ir_json 内置 rebuild_siblings）；
- §4 启动钩子（:608 重入队语义）：status IN (pending,normalizing,chunking,indexing)
  的登记行由 worker 消费（REQUEUE_STATUSES 与 main._KB_REQUEUE_STATUSES 逐字一致，
  test_kb_pipeline::test_requeue_status_set_matches_contract 锁死）；
- ready 分支（data-model §1.7 :275 / api.md §4.1 钉注）：es_enabled=false 灰度态
  pgvector 单通道确认即 ready；true 双通道——ES 确认点=B19 seam 未实装，本批留痕
  放行（不永卡 indexing）。

边界纪律：图外 worker 组件（ADR-011）——短事务自管 session（repo 内），MinIO/归一化
等非库耗时工作不占库连接；DTO 进 DTO 出（行投影 dict 经 KbDocumentRepository 归一）。
组件缝（normalize_document / chunk_forest / put_ir_json / get_ir_json）为模块级名，
测试经 monkeypatch 替换（test_kb_pipeline.stub_pipeline_seams）。日志走 LogRecord
子类体系（BusinessLog），零内容回显。
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

from sqlalchemy import delete, select, update

from app.core.config import kb_settings, minio_settings
from app.core.logger import BusinessLog
from app.db.models import KbChunk, KbDocument
from app.db.session import get_session_factory
from app.rag.chunker import Chunk, ChunkingConfig, chunk_forest
from app.rag.ir_schema import IrNode
from app.rag.normalizers import normalize_document
from app.rag.normalizers.registry import SPEC_DATA_ID
from app.rag.storage import (
    original_object_key,
    put_ir_json,
    get_ir_json,
    get_kb_minio_client,
)

#: 重入队状态集（data-model.md §4 :608 逐字；与 main._KB_REQUEUE_STATUSES 同源，
#: 常量逐字断言单测锁死——漏中间态则文档重启静默卡死）
REQUEUE_STATUSES: tuple[str, ...] = (
    "pending",
    "normalizing",
    "chunking",
    "indexing",
)

#: 六态终态（重入队不入列；process_document 幂等直返）
_TERMINAL_STATUSES: frozenset[str] = frozenset({"ready", "failed"})

#: 一期定死（kb_settings.embedding_dim 缺省 1024 = text-embedding-v4；§1.8）
_EMBEDDING_DIM = kb_settings.embedding_dim


# ---------------------------------------------------------------------------
# 纯函数面（test_kb_pipeline 直接消费）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexingConfirmation:
    """indexing → ready 确认分支投影（§4.1 ready 分支钉注）。"""

    es_enabled: bool
    channel: str  # "pgvector"（灰度单通道）| "dual"（双通道）
    es_pending_seam: bool  # ES 确认点=B19 seam 未实装——true 时留痕放行


def resolve_indexing_confirmation(es_enabled: bool) -> IndexingConfirmation:
    """es_enabled → 确认分支：false 灰度单通道即 ready；true 双通道留痕放行（B19）。"""
    if es_enabled:
        return IndexingConfirmation(es_enabled=True, channel="dual", es_pending_seam=True)
    return IndexingConfirmation(
        es_enabled=False, channel="pgvector", es_pending_seam=False
    )


def read_es_enabled(spec: dict[str, Any]) -> bool:
    """Agent Spec `kb.es_enabled` 读取（非严格布尔 → 灰度缺省 false——灰度安全）。"""
    kb = spec.get("kb") if isinstance(spec, dict) else None
    if not isinstance(kb, dict):
        return False
    return kb.get("es_enabled") is True


def project_parse_meta(meta: dict[str, Any] | None) -> dict[str, Any] | None:
    """存储 parse_meta（四键+附加键）→ REST §4.3 三键投影。

    存储 `{}` / None → null；缺省键补 null/0（pages 非页式来源恒 null；
    parser 为存储侧键不进投影）。
    """
    if not meta:
        return None
    return {
        "pages": meta.get("pages"),
        "tables": meta.get("tables", 0),
        "code_blocks": meta.get("code_blocks", 0),
    }


def chunk_row_meta(chunk: Chunk) -> dict[str, Any]:
    """Chunk → kb_chunks.meta 七键（data-model §1.8——溯源 O(1) 消费面）。"""
    return {
        "node_id": chunk.node_id,
        "heading_path": list(chunk.heading_path),
        "loc": chunk.loc,
        "references": list(chunk.references),
        "raw_type": chunk.raw_type,
        "char_start": chunk.char_start,
        "char_end": chunk.char_end,
    }


def extract_url_title(text: str, url: str) -> str:
    """URL 收藏标题提取：正文首个标题行 → host 兜底；列宽 512 截断（§1.7）。"""
    title: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            candidate = stripped.lstrip("#").strip()
            if candidate:
                title = candidate
                break
    if title is None:
        from urllib.parse import urlsplit

        title = urlsplit(url).hostname or url
    return title[:512]


def original_ext_for_doc_type(doc_type: str) -> str:
    """doc_type → 原件扩展名（URL 行存抓取正文 md，§1.7 :270）。"""
    return "md" if doc_type == "url" else doc_type


def embedding_placeholder() -> list[float]:
    """零向量占位（B18 向量通道落地前 NOT NULL 列写入缝；维度一期定死）。"""
    return [0.0] * _EMBEDDING_DIM


# ---------------------------------------------------------------------------
# 仓储（六态乐观状态卫——全部 UPDATE 带状态 WHERE，rowcount 即判定）
# ---------------------------------------------------------------------------


class KbDocumentRepository:
    """kb_documents/kb_chunks 数据访问（短事务自管 session——图外 worker 例外口径）。

    `transition` 为状态机唯一写入口：`UPDATE kb_documents SET ... WHERE id=:id AND
    status IN :expect`（乐观状态卫）——rowcount 0 = 并发改写/行删，调用方按 abort
    收口，绝不覆盖他人状态。全部语句参数绑定（SQLAlchemy 表达式），零拼接。
    """

    async def load(self, doc_id: uuid.UUID | str) -> dict[str, Any] | None:
        """行投影（流水线消费八键；行不存在 → None）。

        parse_meta：飞书附件行建行时预置 feishu 四键（同步 diff 寻址——B20 B 线），
        normalizing 段合并保留该子对象（漏保留则 node_token 丢失、同步 diff 恒判
        「新增」重复建行）。
        """
        async with get_session_factory()() as session:
            row = await session.get(KbDocument, _as_uuid(doc_id))
            if row is None:
                return None
            return {
                "id": row.id,
                "user_id": row.user_id,
                "status": row.status,
                "doc_type": row.doc_type,
                "title": row.title,
                "source_url": row.source_url,
                "file_object_key": row.file_object_key,
                "parse_meta": row.parse_meta,
            }

    async def transition(
        self,
        doc_id: uuid.UUID | str,
        *,
        expect: set[str],
        to: str,
        fields: dict[str, Any],
    ) -> bool:
        """乐观状态迁移（expect 状态集卫）；返回 rowcount > 0。"""
        stmt = (
            update(KbDocument)
            .where(KbDocument.id == _as_uuid(doc_id), KbDocument.status.in_(expect))
            .values(**fields, status=to)
        )
        async with get_session_factory()() as session:
            result = await session.execute(stmt)
            await session.commit()
            rowcount = getattr(result, "rowcount", 0) or 0
            return rowcount > 0

    async def mark_failed(self, doc_id: uuid.UUID | str, *, reason: str) -> bool:
        """失败落列（阶段化 fail_reason；仅中间态/pending 可落——终态不覆盖）。

        行已被删（delete 竞态）→ rowcount 0 返回 False，调用方按 failed 收口不炸。
        """
        stmt = (
            update(KbDocument)
            .where(
                KbDocument.id == _as_uuid(doc_id),
                KbDocument.status.in_(REQUEUE_STATUSES),
            )
            .values(status="failed", fail_reason=reason)
        )
        async with get_session_factory()() as session:
            result = await session.execute(stmt)
            await session.commit()
            rowcount = getattr(result, "rowcount", 0) or 0
            return rowcount > 0

    async def replace_chunks(
        self,
        doc_id: uuid.UUID | str,
        *,
        user_id: uuid.UUID | str,
        chunks: list[Chunk],
        content_hash: str,
    ) -> bool:
        """切片全量替换（先删后插——重跑幂等；embedding 走零向量占位缝）。

        kb_chunks 无 user 冗余删面：按 kb_doc_id 删（§1.8 归属键）。行写入零状态
        变更——kb_documents.chunk_count/content_hash 由调用方 transition 落列。
        """
        doc_uuid = _as_uuid(doc_id)
        async with get_session_factory()() as session:
            await session.execute(delete(KbChunk).where(KbChunk.kb_doc_id == doc_uuid))
            for chunk in chunks:
                session.add(
                    KbChunk(
                        kb_doc_id=doc_uuid,
                        user_id=_as_uuid(user_id),
                        chunk_index=chunk.chunk_index,
                        content=chunk.content,
                        token_count=chunk.token_count,
                        embedding=embedding_placeholder(),
                        meta=chunk_row_meta(chunk),
                    )
                )
            await session.commit()
        return True

    async def list_requeue_ids(self) -> list[uuid.UUID]:
        """启动钩子登记行 → 重入队 id 列表（status IN REQUEUE_STATUSES）。"""
        async with get_session_factory()() as session:
            result = await session.scalars(
                select(KbDocument.id).where(KbDocument.status.in_(REQUEUE_STATUSES))
            )
            return [uuid.UUID(str(value)) for value in result.all()]


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


# ---------------------------------------------------------------------------
# 流水线
# ---------------------------------------------------------------------------


class KbPipeline:
    """KB 入库流水线：进程内队列 + worker 池，逐文档推进六态状态机。

    依赖全注入（repo / spec 快照供给 / MinIO 工厂 / worker 数）——单测注入假体
    零网络零 DB（test_kb_pipeline）；生产形态经 `get_kb_pipeline` 装配。
    `worker_count=0` 不 spawn worker（unit 直驱 `process_document`）。
    """

    def __init__(
        self,
        *,
        repo: KbDocumentRepository,
        spec_payload_provider: Callable[[], dict[str, Any]],
        minio_client: Callable[[], Any],
        worker_count: int = 1,
    ) -> None:
        self._repo = repo
        self._spec_payload_provider = spec_payload_provider
        self._minio_client_factory = minio_client
        self._worker_count = worker_count
        self._workers: list[asyncio.Task[None]] = []
        self._queue: asyncio.Queue[str] | None = None

    # ---- 队列面 ----

    @property
    def queue(self) -> asyncio.Queue[str]:
        """入库队列（惰性构造——绑定首个使用方所在 loop）。"""
        if self._queue is None:
            self._queue = asyncio.Queue()
        return self._queue

    def enqueue(self, doc_id: str) -> None:
        """登记文档（REST 上传 BackgroundTasks 调用点；启动钩子重入队同缝）。"""
        self.queue.put_nowait(str(doc_id))

    async def start(self) -> None:
        """启动：消费 B16 启动钩子登记行（status IN REQUEUE_STATUSES）+ spawn worker。"""
        for doc_id in await self._repo.list_requeue_ids():
            self.enqueue(str(doc_id))
        if self._worker_count > 0 and not self._workers:
            self._workers = [
                asyncio.create_task(self._worker_loop(index))
                for index in range(self._worker_count)
            ]

    async def stop(self) -> None:
        """停机：取消 worker 池（队列内残留由下次启动重入队承接——队列无持久化）。"""
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []

    async def _worker_loop(self, index: int) -> None:
        while True:
            doc_id = await self.queue.get()
            try:
                await self.process_document(doc_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— worker 面兜底：单文档失败不拖垮池
                BusinessLog(
                    message=f"kb worker 处理异常（{type(exc).__name__}，单文档失败已隔离）",
                    action="kb_worker",
                    entity="kb_document",
                    entity_id=doc_id,
                ).error(exc)
            finally:
                self.queue.task_done()

    # ---- 状态机（api.md §4.1 / data-model §1.7）----

    async def process_document(self, doc_id: str) -> str:
        """推进单文档至终态；返回终态语义：ready|failed|gone|aborted。

        - 行不存在 → "gone"（静默，上传后即删的竞态）；终态行幂等直返；
        - 乐观卫 rowcount 0 → "aborted"（不写 failed——不覆盖并发方状态）；
        - 任一步异常 → failed + 阶段化 fail_reason（`{stage}: {detail}`；pending
          期无前缀），原文零 purge（可重试）。
        """
        try:
            row = await self._repo.load(doc_id)
        except Exception as exc:
            await self._fail(doc_id, str(exc))
            return "failed"
        if row is None:
            return "gone"
        status: str = row["status"]
        if status in _TERMINAL_STATUSES:
            return status

        stage: str | None = None  # None=pending 期（fail_reason 无阶段前缀）
        try:
            if status == "pending":
                if not await self._transition(doc_id, {"pending"}, "normalizing", {}):
                    return "aborted"
                status = "normalizing"
            if status == "normalizing":
                stage = "normalizing"
                parse_meta = await self._run_normalizing(row)
                if not await self._transition(
                    doc_id,
                    {"normalizing"},
                    "chunking",
                    {"parse_meta": parse_meta},
                ):
                    return "aborted"
                status = "chunking"
            if status == "chunking":
                stage = "chunking"
                chunk_count, content_hash = await self._run_chunking(row)
                stage = "indexing"
                if not await self._transition(
                    doc_id,
                    {"chunking"},
                    "indexing",
                    {"chunk_count": chunk_count, "content_hash": content_hash},
                ):
                    return "aborted"
            # status == "indexing"（含 resume：indexing 直入确认段）
            stage = "indexing"
            confirmation = resolve_indexing_confirmation(
                read_es_enabled(self._spec_payload_provider())
            )
            if confirmation.es_pending_seam:
                # ES 确认点=B19 seam 未实装——留痕放行，不永卡 indexing（§1.7 :275）
                BusinessLog(
                    message="B19 seam 未实装：es_enabled=true 双通道确认留痕"
                    "（es_pending_seam），灰度放行 indexing→ready",
                    action="kb_indexing_confirm",
                    entity="kb_document",
                    entity_id=str(doc_id),
                ).info()
            ready_at = datetime.now(UTC)
            if not await self._transition(
                doc_id, {"indexing"}, "ready", {"ready_at": ready_at}
            ):
                return "aborted"
            return "ready"
        except Exception as exc:
            reason = f"{stage}: {exc}" if stage else str(exc)
            await self._fail(doc_id, reason)
            return "failed"

    async def _transition(
        self, doc_id: str, expect: set[str], to: str, fields: dict[str, Any]
    ) -> bool:
        return await self._repo.transition(doc_id, expect=expect, to=to, fields=fields)

    async def _fail(self, doc_id: str, reason: str) -> None:
        """失败落列（best-effort——落列失败只留痕，终态语义由调用方收口）。"""
        try:
            await self._repo.mark_failed(doc_id, reason=reason[:2048])
        except Exception as exc:  # noqa: BLE001
            BusinessLog(
                message=f"kb 失败落列异常（{type(exc).__name__}，best-effort 留痕）",
                action="kb_mark_failed",
                entity="kb_document",
                entity_id=str(doc_id),
            ).error(exc)

    # ---- 阶段实现（normalizing / chunking）----

    def _minio(self) -> Any:
        return self._minio_client_factory()

    async def _run_normalizing(self, row: dict[str, Any]) -> dict[str, Any]:
        """原件下载 → 归一化（to_thread）→ 标题富化 → ir.json 落桶；返回 parse_meta。

        原件读取失败不预占失败语义：空字节交归一化组件显式拒绝（坏文件语义域——
        组件消息即 fail_reason），读取异常 WARN 留痕（零内容回显）。
        """
        user_id = str(row["user_id"])
        doc_id = str(row["id"])
        ext = original_ext_for_doc_type(row["doc_type"])
        key = original_object_key(user_id, doc_id, ext)
        client = self._minio()
        try:
            response = await asyncio.to_thread(
                client.get_object, minio_settings.bucket_kb, key
            )
            try:
                data = await asyncio.to_thread(response.read)
            finally:
                for teardown in ("close", "release_conn"):
                    handle = getattr(response, teardown, None)
                    if callable(handle):
                        await asyncio.to_thread(handle)
        except Exception as exc:
            BusinessLog(
                message=f"kb 原件读取失败（{type(exc).__name__}），空字节交归一化组件显式拒绝",
                action="kb_read_original",
                entity="kb_document",
                entity_id=doc_id,
            ).warning(exc)
            data = b""
        # 归一化（同步 CPU 面下放线程池；缝名模块级——测试经 monkeypatch 替换）
        result = await asyncio.to_thread(
            normalize_document, data, ext, doc_kind="upload", file_object_key=key
        )
        root: IrNode = result.document
        root.title = row["title"]  # 标题富化（§1.18 heading_path 构成公式 doc.title 段）
        await put_ir_json(user_id, doc_id, root, client=client)
        parse_meta = dict(result.parse_meta)
        # 飞书附件行 feishu 四键合并保留（B20 B 线——同步 diff 寻址）：建行时预置的
        # parse_meta.feishu 不被归一化产物覆写丢失，否则 node_token 断链、同步 diff
        # 恒判「新增」重复建行（api.md §4.7 判定序①的本地行寻址依赖此键）
        existing = row.get("parse_meta") or {}
        if isinstance(existing, dict) and isinstance(existing.get("feishu"), dict):
            parse_meta["feishu"] = existing["feishu"]
        return parse_meta

    async def _run_chunking(self, row: dict[str, Any]) -> tuple[int, str]:
        """ir.json 装载（rebuild_siblings 内置）→ 逐级切分 → kb_chunks 全量替换。

        返回 (chunk_count, document_content_hash)——chunking→indexing 迁移落列原料
        （OQ-34 写入接线）。
        """
        user_id = str(row["user_id"])
        doc_id = str(row["id"])
        root = await get_ir_json(user_id, doc_id, client=self._minio())
        config = ChunkingConfig.from_spec(self._spec_payload_provider())
        results = await asyncio.to_thread(
            chunk_forest, root, document_id=doc_id, config=config
        )
        chunks = [chunk for result in results for chunk in result.chunks]
        content_hash = results[0].document_content_hash if results else ""
        await self._repo.replace_chunks(
            doc_id,
            user_id=row["user_id"],
            chunks=chunks,
            content_hash=content_hash,
        )
        return len(chunks), content_hash


# ---------------------------------------------------------------------------
# 生产装配（api 依赖缝——单测经 dependency_overrides 替换）
# ---------------------------------------------------------------------------

_kb_pipeline_instance: KbPipeline | None = None


def _default_spec_payload() -> dict[str, Any]:
    """Agent Spec 快照 payload（config_center 唯一出口；未装载/缺失回落空 dict——
    pipeline 各读键全部自带灰度缺省，不炸）。"""
    from app.config_center import get_asset_center

    try:
        snapshot = get_asset_center().snapshot(SPEC_DATA_ID)
    except RuntimeError:
        return {}
    return dict(snapshot.payload) if snapshot is not None else {}


def get_kb_pipeline() -> KbPipeline:
    """进程级单例（REST BackgroundTasks 入队缝；单测经 dependency_overrides 替换）。

    首次调用惰性装配（真实 repo + MinIO 惰性工厂 + spec 快照供给）。start()/stop()
    由宿主 lifespan 承担（未 start 时仅入队不消费——登记行经启动钩子重入队承接）。
    """
    global _kb_pipeline_instance
    if _kb_pipeline_instance is None:
        _kb_pipeline_instance = KbPipeline(
            repo=KbDocumentRepository(),
            spec_payload_provider=_default_spec_payload,
            minio_client=get_kb_minio_client,
            worker_count=1,
        )
    return _kb_pipeline_instance
