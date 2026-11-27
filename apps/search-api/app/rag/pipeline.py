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
  pgvector 单通道确认即 ready（不触碰 ES——灰度分支先于 es_indexer 判定）；true
  双通道（B18）——es_indexer 已接线时 indexing 段真写入 ES，确认成功才
  indexing→ready；es_indexer=None（接线缺失）留痕放行兜底（不永卡 indexing）。
- chunking 段 embedding 接线（plan §3.3 A3）：`kb.embedding_enabled=true` → 切分后
  调 rag.embedding.embed_texts（输入 build_embedding_input——标题轨迹前置单一事实
  源），失败重试 1 次耗尽 → `chunking: embedding 写入失败: {类型名}` + failed（零
  content 回显）；false（缺省）=零向量占位逐字节现状。

边界纪律：图外 worker 组件（ADR-011）——短事务自管 session（repo 内），MinIO/归一化
等非库耗时工作不占库连接；DTO 进 DTO 出（行投影 dict 经 KbDocumentRepository 归一）。
组件缝（normalize_document / chunk_forest / put_ir_json / get_ir_json / _es_retry_sleep）
为模块级名，测试经 monkeypatch 替换（test_kb_pipeline.stub_pipeline_seams）。日志走
LogRecord 子类体系（BusinessLog），零内容回显。
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
from app.rag.embedding import build_embedding_input, embed_texts
from app.rag.indexers.es_indexer import (
    EsIndexError,
    _es_error_status,
    is_retryable_es_error,
)
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

#: ES 写入失败退避序列（plan B18 §5.3——首试后 2 次重试，秒）
_ES_RETRY_BACKOFF: tuple[float, ...] = (1.0, 3.0)

#: 退避 sleep 缝（模块级名——单测 monkeypatch 免真等 4s）
_es_retry_sleep = asyncio.sleep

#: embedding 写入尝试次数（plan §3.3 A3：失败重试恰 1 次——首试 + 1；耗尽即 failed）
_EMBEDDING_ATTEMPTS = 1 + 1

#: 模型映射 dataId（embedding 别名来源——nacos-config §5 `embedding` 键；
#: 与 scripts/kb_embedding_backfill.py 同一事实源）
_MODEL_MAPPING_DATA_ID = "ai_search.model.mapping.json"


class EmbeddingWriteError(Exception):
    """chunking 段 embedding 写入失败（plan §3.3 失败语义）。

    消息仅异常类型名/维度数/计数——**零 content 回显**（沿 B18 `_es_failure` 脱敏
    纪律）；process_document 外层转 `chunking: embedding 写入失败: {类型名}`。
    """


def collect_node_kinds(root: IrNode) -> dict[str, str]:
    """IR 树 node.id → node.type.value 全量映射（ES kind 派生——与 chunker 同一
    事实源：Chunk.kind = 首来源单元 node.type.value，pg 读回经本映射还原）。"""

    def _walk(node: IrNode, into: dict[str, str]) -> None:
        into[node.id] = node.type.value
        for child in node.children:
            _walk(child, into)

    kinds: dict[str, str] = {}
    _walk(root, kinds)
    return kinds


# ---------------------------------------------------------------------------
# 纯函数面（test_kb_pipeline 直接消费）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexingConfirmation:
    """indexing → ready 确认分支投影（§4.1 ready 分支钉注；B18 语义）。

    `es_pending_seam` 语义反转（B18 起）：仅 es_enabled=true 且 es_indexer 未接线
    时为 true（留痕放行兜底——灰度安全，不因装配遗漏全量文档 failed）；false =
    灰度单通道或真写入路径。
    """

    es_enabled: bool
    channel: str  # "pgvector"（灰度单通道）| "dual"（双通道）
    es_pending_seam: bool  # 仅 es_indexer 未接线时 true（留痕放行兜底）


def resolve_indexing_confirmation(
    es_enabled: bool, es_indexer: Any | None = None
) -> IndexingConfirmation:
    """es_enabled × es_indexer 接线态 → 确认分支。

    - false → 灰度单通道即 ready（分支先于 es_indexer 判定，接线缺失不影响灰度态）；
    - true + 缝已接线 → 双通道真写入；
    - true + es_indexer=None → 留痕放行兜底（es_pending_seam=True）。
    """
    if es_enabled:
        return IndexingConfirmation(
            es_enabled=True, channel="dual", es_pending_seam=es_indexer is None
        )
    return IndexingConfirmation(
        es_enabled=False, channel="pgvector", es_pending_seam=False
    )


def read_es_enabled(spec: dict[str, Any]) -> bool:
    """Agent Spec `kb.es_enabled` 读取（非严格布尔 → 灰度缺省 false——灰度安全）。"""
    kb = spec.get("kb") if isinstance(spec, dict) else None
    if not isinstance(kb, dict):
        return False
    return kb.get("es_enabled") is True


def read_embedding_enabled(spec: dict[str, Any]) -> bool:
    """Agent Spec `kb.embedding_enabled` 读取（严格布尔；非 True 值 → false 灰度安全）。

    false（缺省）= chunking 段零向量占位逐字节现状；true = 真嵌入写入（plan §3.3）。
    """
    kb = spec.get("kb") if isinstance(spec, dict) else None
    if not isinstance(kb, dict):
        return False
    return kb.get("embedding_enabled") is True


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
    """零向量占位（`kb.embedding_enabled=false` 灰度态 NOT NULL 列写入缝；
    维度一期定死——true 态由 chunking 段真向量覆盖，plan §3.3）。"""
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
        """行投影（流水线消费九键；行不存在 → None）。

        parse_meta：飞书附件行建行时预置 feishu 四键（同步 diff 寻址——B20 B 线），
        normalizing 段合并保留该子对象（漏保留则 node_token 丢失、同步 diff 恒判
        「新增」重复建行）。catalog_path：B18 ES doc 级目录链字段（本地/URL 来源
        恒 null——ES doc 落 null 不阻断）。
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
                "catalog_path": row.catalog_path,
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
        embeddings: list[list[float]] | None = None,
    ) -> bool:
        """切片全量替换（先删后插——重跑幂等）。

        `embeddings=None` → 零向量占位（`kb.embedding_enabled=false` 灰度态，
        逐字节现状）；否则按 chunks 同序写入真向量（plan §3.3 A3——向量与 chunk
        行同批原子写入，无零向量中间态窗口）。kb_chunks 无 user 冗余删面：按
        kb_doc_id 删（§1.8 归属键）。行写入零状态变更——kb_documents.chunk_count/
        content_hash 由调用方 transition 落列。
        """
        doc_uuid = _as_uuid(doc_id)
        if embeddings is not None and len(embeddings) != len(chunks):
            raise EmbeddingWriteError(
                f"embedding 数量不符: got={len(embeddings)} expect={len(chunks)}"
            )
        async with get_session_factory()() as session:
            await session.execute(delete(KbChunk).where(KbChunk.kb_doc_id == doc_uuid))
            for position, chunk in enumerate(chunks):
                session.add(
                    KbChunk(
                        kb_doc_id=doc_uuid,
                        user_id=_as_uuid(user_id),
                        chunk_index=chunk.chunk_index,
                        content=chunk.content,
                        token_count=chunk.token_count,
                        embedding=(
                            embeddings[position]
                            if embeddings is not None
                            else embedding_placeholder()
                        ),
                        meta=chunk_row_meta(chunk),
                    )
                )
            await session.commit()
        return True

    async def list_chunks_by_doc(self, doc_id: uuid.UUID | str) -> list[dict[str, Any]]:
        """pg 事实源读回文档 chunks（ORDER BY chunk_index——B18 indexing 段消费）。

        ES 写入不重切（chunking 段已 replace_chunks+transition；resume-from-indexing
        也走本读回路径——幂等且与 OQ-34 content_hash 落列口径自洽）。行投影为
        es_mapping.build_chunk_doc 消费面（chunk_id/kb_doc_id/user_id/chunk_index/
        content/token_count/meta 七键/created_at）。
        """
        async with get_session_factory()() as session:
            result = await session.scalars(
                select(KbChunk)
                .where(KbChunk.kb_doc_id == _as_uuid(doc_id))
                .order_by(KbChunk.chunk_index.asc())
            )
            return [
                {
                    "chunk_id": chunk.id,
                    "kb_doc_id": chunk.kb_doc_id,
                    "user_id": chunk.user_id,
                    "chunk_index": chunk.chunk_index,
                    "content": chunk.content,
                    "token_count": chunk.token_count,
                    "meta": chunk.meta or {},
                    "created_at": chunk.created_at,
                }
                for chunk in result.all()
            ]

    async def list_requeue_ids(self) -> list[uuid.UUID]:
        """启动钩子登记行 → 重入队 id 列表（status IN REQUEUE_STATUSES）。"""
        async with get_session_factory()() as session:
            result = await session.scalars(
                select(KbDocument.id).where(KbDocument.status.in_(REQUEUE_STATUSES))
            )
            return [uuid.UUID(str(value)) for value in result.all()]


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


async def build_es_doc_meta(
    row: dict[str, Any], *, minio: Any
) -> dict[str, Any]:
    """ES doc 级三键装配（B18 §5.2 步骤 2；回填脚本同源复用）。

    catalog_path 取行值；doc_kind/node_kinds 由 ir.json 一次 GET 派生
    （collect_node_kinds——kind 与 chunker 同一事实源）。IR 不可达（异常态）→
    WARN 留痕 + None 继续——辅助过滤字段不阻断写入（kind/raw_type 才是 chunk 级
    过滤字段，恒可得）。
    """
    doc_kind: str | None = None
    node_kinds: dict[str, str] = {}
    try:
        root = await get_ir_json(str(row["user_id"]), str(row["id"]), client=minio)
        doc_kind = root.doc_kind
        node_kinds = collect_node_kinds(root)
    except Exception as exc:  # noqa: BLE001 —— IR 缺失不阻断 ES 写入（B18 §4.3）
        BusinessLog(
            message=f"kb doc_kind/node_kinds IR 派生失败（{type(exc).__name__}）"
            "——doc 级辅助字段落 null 继续",
            action="kb_es_doc_kind",
            entity="kb_document",
            entity_id=str(row["id"]),
        ).warning(exc)
    return {
        "catalog_path": row.get("catalog_path"),
        "doc_kind": doc_kind,
        "node_kinds": node_kinds,
    }


# ---------------------------------------------------------------------------
# 流水线
# ---------------------------------------------------------------------------


class KbPipeline:
    """KB 入库流水线：进程内队列 + worker 池，逐文档推进六态状态机。

    依赖全注入（repo / spec 快照供给 / MinIO 工厂 / worker 数 / ES 索引器）——
    单测注入假体零网络零 DB（test_kb_pipeline）；生产形态经 `get_kb_pipeline` 装配。
    `worker_count=0` 不 spawn worker（unit 直驱 `process_document`）。
    `es_indexer=None` 时 es_enabled=true 走留痕放行兜底（灰度安全——不因装配
    遗漏全量文档 failed）；注入 KbEsIndexer 时 indexing 段真写入 ES。
    """

    def __init__(
        self,
        *,
        repo: KbDocumentRepository,
        spec_payload_provider: Callable[[], dict[str, Any]],
        minio_client: Callable[[], Any],
        worker_count: int = 1,
        es_indexer: Any | None = None,
        mapping_provider: Callable[[], dict[str, Any] | None] | None = None,
    ) -> None:
        self._repo = repo
        self._spec_payload_provider = spec_payload_provider
        self._minio_client_factory = minio_client
        self._worker_count = worker_count
        self._es_indexer = es_indexer
        self._mapping_provider = mapping_provider or _default_model_mapping
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
                read_es_enabled(self._spec_payload_provider()), self._es_indexer
            )
            if not confirmation.es_enabled:
                # 灰度单通道：pgvector 确认即 ready——分支先于 es_indexer 判定，
                # 不触碰 ES（灰度态行为与接线缺失解耦，§1.19「启用通道均确认」）
                pass
            elif confirmation.es_pending_seam:
                # es_enabled=true 但缝未接线（装配缺失/单测未注入）——留痕放行
                # 兜底（灰度安全，不永卡 indexing；B18 起 WARN 升级自 B19 info 留痕）
                BusinessLog(
                    message="B18 缝未接线：es_enabled=true 双通道确认留痕"
                    "（es_pending_seam），灰度放行 indexing→ready",
                    action="kb_indexing_confirm",
                    entity="kb_document",
                    entity_id=str(doc_id),
                ).warning()
            else:
                # 双通道真写入（B18）：pg 读回 → ES 先删后 bulk → 确认成功才 ready
                #（失败语义 §5.3：retryable 重试 2 次，耗尽 → failed 不静默 ready）
                chunks = await self._repo.list_chunks_by_doc(doc_id)
                doc_meta = await self._build_doc_meta(row)
                await self._write_es_with_retry(doc_id, row["user_id"], chunks, doc_meta)
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

    # ---- 阶段实现（indexing 确认段 / normalizing / chunking）----

    async def _build_doc_meta(self, row: dict[str, Any]) -> dict[str, Any]:
        """ES doc 级三键装配（B18 §5.2 步骤 2）——委托模块级 `build_es_doc_meta`
        （回填脚本同源复用，生产路径与脚本零分叉）。"""
        return await build_es_doc_meta(row, minio=self._minio())

    async def _write_es_with_retry(
        self,
        doc_id: str,
        user_id: Any,
        chunks: list[dict[str, Any]],
        doc_meta: dict[str, Any],
    ) -> None:
        """ES 写入（retryable 重试 2 次，退避 1s/3s——plan B18 §5.3）。

        `ensure_index` 与 `index_chunks` **同处重试 try 面内**（就绪后不重复调用——
        幂等但省一次往返）：ensure 阶段的连接拒/超时同属瞬态，必须与写入共用同一
        retryable 判定与退避节奏，否则 ES 瞬态不可达命中 ensure 即零重试终态 failed。
        retryable = 连接族 + HTTP 429/5xx（es_indexer.is_retryable_es_error）；
        耗尽/不可重试 → EsIndexError 带「类型名(状态码) + attempts」，attempts 为
        **真实已尝试次数**（不可重试首试即失败 → attempts=1，不谎报总预算）——
        process_document 外层转 `indexing: ES 写入失败: …`（零 chunk 内容回显、
        零内部 host:port 回显）。
        重试中行状态仍 indexing（乐观卫期望集不变）；行被并发改写 → ready 迁移
        rowcount 0 → aborted 收口。
        """
        es_indexer = self._es_indexer
        assert es_indexer is not None  # 调用前置：resolve 确认缝已接线
        attempts_total = 1 + len(_ES_RETRY_BACKOFF)
        index_ready = False
        for attempt in range(attempts_total):
            try:
                if not index_ready:
                    await es_indexer.ensure_index()
                    index_ready = True
                await es_indexer.index_chunks(
                    doc_id,
                    user_id=user_id,
                    chunks=chunks,
                    doc_meta=doc_meta,
                )
                return
            except Exception as exc:  # noqa: BLE001 —— retryable 判定后转 failed/重试
                is_last = attempt == attempts_total - 1
                if is_last or not is_retryable_es_error(exc):
                    raise self._es_failure(exc, attempt + 1) from exc
                await _es_retry_sleep(_ES_RETRY_BACKOFF[attempt])

    @staticmethod
    def _es_failure(exc: Exception, attempts: int) -> Exception:
        """重试耗尽/不可重试异常 → EsIndexError（detail 仅类型/状态/attempts 零内容）。

        attempts = **真实已尝试次数**（调用方传 `attempt + 1`；不可重试首试即失败
        → 1，不谎报重试总预算）。状态码提取复用 es_indexer._es_error_status
        （status_code 兼容属性 → meta.status 兜底，与 retryable 判定同一事实源）。
        EsIndexError 原样透传（indexer 侧 detail 已是计数形态——bulk 失败 N/M item，
        不二次包装制造噪音）。
        """
        if isinstance(exc, EsIndexError):
            return exc
        status = _es_error_status(exc)
        status_part = f"{status} " if isinstance(status, int) else ""
        return EsIndexError(
            f"ES 写入失败: {type(exc).__name__}({status_part}attempts={attempts})"
        )

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
        # 来源语义按行投影派生（处置线⑳ doc_kind:url 接线；data-model §1.18：
        # doc_kind 六值含 url、来源语义由 doc_kind/loc.origin 承载、ID 仍 loc:*
        # 前缀不编码来源）：URL 收藏行（doc_type=url）→ doc_kind="url" +
        # source_url 进 UrlLoc（可回跳字段）；上传/飞书附件行恒 "upload" 零变化。
        # ES doc_meta.doc_kind 由 ir.json root 单一事实源派生（build_es_doc_meta），
        # IR root 正确即 ES 面自动正确。
        doc_kind = "url" if row["doc_type"] == "url" else "upload"
        source_url = row.get("source_url") if doc_kind == "url" else None
        result = await asyncio.to_thread(
            normalize_document,
            data,
            ext,
            doc_kind=doc_kind,
            file_object_key=key,
            source_url=source_url,
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
        （OQ-34 写入接线）。`kb.embedding_enabled=true` 时切分后先调 embed（输入走
        build_embedding_input 单一事实源——标题轨迹前置），失败重试 1 次耗尽 →
        EmbeddingWriteError（外层转 `chunking: embedding 写入失败: {类型名}`）；
        false（缺省）=零向量占位逐字节现状。
        """
        user_id = str(row["user_id"])
        doc_id = str(row["id"])
        root = await get_ir_json(user_id, doc_id, client=self._minio())
        spec = self._spec_payload_provider()
        config = ChunkingConfig.from_spec(spec)
        results = await asyncio.to_thread(
            chunk_forest, root, document_id=doc_id, config=config
        )
        chunks = [chunk for result in results for chunk in result.chunks]
        content_hash = results[0].document_content_hash if results else ""
        embeddings: list[list[float]] | None = None
        if read_embedding_enabled(spec):
            embeddings = await self._embed_chunks(chunks)
        await self._repo.replace_chunks(
            doc_id,
            user_id=row["user_id"],
            chunks=chunks,
            content_hash=content_hash,
            embeddings=embeddings,
        )
        return len(chunks), content_hash

    async def _embed_chunks(self, chunks: list[Chunk]) -> list[list[float]]:
        """chunk 全量向量化（plan §3.3 A3——embed 先于 replace_chunks 短事务，
        不占库连接）。

        失败重试恰 1 次（`_EMBEDDING_ATTEMPTS`），耗尽 → EmbeddingWriteError
        （消息仅类型名/维度/计数——零 content 回显）；维度≠1024 / 数量不符 →
        同域失败（防静默脏向量）。
        """
        texts = [
            build_embedding_input(chunk.content, chunk.heading_path) for chunk in chunks
        ]
        vectors: list[list[float]] | None = None
        last_exc: Exception | None = None
        for _attempt in range(_EMBEDDING_ATTEMPTS):
            try:
                vectors = await embed_texts(
                    texts, mapping=self._mapping_provider()
                )
                break
            except Exception as exc:  # noqa: BLE001 —— 重试 1 次后转 EmbeddingWriteError
                last_exc = exc
        if vectors is None:
            assert last_exc is not None  # 循环耗尽必有异常
            raise EmbeddingWriteError(
                f"embedding 写入失败: {type(last_exc).__name__}"
            ) from last_exc
        if len(vectors) != len(chunks):
            raise EmbeddingWriteError(
                f"embedding 数量不符: got={len(vectors)} expect={len(chunks)}"
            )
        for vector in vectors:
            if len(vector) != _EMBEDDING_DIM:
                raise EmbeddingWriteError(
                    f"embedding 维度不符: got={len(vector)} expect={_EMBEDDING_DIM}"
                )
        return vectors


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


def _default_model_mapping() -> dict[str, Any] | None:
    """模型映射快照 payload（embedding 别名来源；未装载/缺失回落 None——
    embed_texts 内置 text-embedding-v4 兜底。回填脚本 `_spec_mapping` 同款缝）。"""
    from app.config_center import get_asset_center

    try:
        snapshot = get_asset_center().snapshot(_MODEL_MAPPING_DATA_ID)
    except RuntimeError:
        return None
    return dict(snapshot.payload) if snapshot is not None else None


def get_kb_pipeline() -> KbPipeline:
    """进程级单例（REST BackgroundTasks 入队缝；单测经 dependency_overrides 替换）。

    首次调用惰性装配（真实 repo + MinIO 惰性工厂 + spec 快照供给 + ES 索引器——
    B18 双通道写入；灰度态 es_enabled=false 分支不触碰 ES，接线在位零影响）。
    start()/stop() 由宿主 lifespan 承担（未 start 时仅入队不消费——登记行经启动
    钩子重入队承接）。
    """
    global _kb_pipeline_instance
    if _kb_pipeline_instance is None:
        from app.rag.indexers.es_indexer import get_kb_es_indexer

        _kb_pipeline_instance = KbPipeline(
            repo=KbDocumentRepository(),
            spec_payload_provider=_default_spec_payload,
            minio_client=get_kb_minio_client,
            worker_count=1,
            es_indexer=get_kb_es_indexer(),
        )
    return _kb_pipeline_instance
