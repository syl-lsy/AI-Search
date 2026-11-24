"""知识库三表 ORM（data-model.md §1.7/§1.8/§1.17，0013_m3_kb；M3 批次 1 B16）。

- kb_documents（§1.7）：文档行级元数据 + 六态状态机（pending → normalizing →
  chunking → indexing → ready，任一阶段失败 failed）；catalog_ids/catalog_path
  为 M3 增列（飞书目录链 root→leaf——本地上传/URL 恒 null，OQ-26 ⑦）；
- kb_chunks（§1.8）：切片 + pgvector 向量（vector(1024) = text-embedding-v4，
  一期维度定死）；meta jsonb 七键（node_id/heading_path/loc/references/raw_type/
  char_start/char_end——溯源 O(1) 全走本列物化字段，查询期从不遍历整树 IR，§1.18）；
- kb_import_jobs（§1.17）：飞书导入任务行（可中断可恢复，断点游标 cursor；
  worker 为图外组件 ADR-011，重启经启动钩子重入队 status IN (pending,running)）。

**状态常量唯一定义处**（data-model.md §1.7 status / §1.17 status）：
- KbDocStatus 六态——旧 parsing/embedding 作废（M3 未上线无存量语义负担），
  镜像 api.md §4.3 v1.30 与前端 api.d.ts KbDocStatus；
- KbJobStatus 四态——不设 canceled 独立终态（取消形态由 api 契约承载，防投影
  面漂移），镜像 api.md §4.7~4.9。

enum 一律 varchar 无 CHECK（§4 迁移规则：取值合法性由应用层 schema 校验承担，
状态扩容=文档性扩展零 DDL）。主键 UUID v4 由 ORM 客户端承接（§4 迁移规则）。
"""
import uuid
from datetime import datetime
from typing import Any, Literal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CHAR,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

#: kb_documents.status 六态（api.md §4.3 v1.30 镜像；旧 parsing/embedding 作废）
KbDocStatus = Literal[
    "pending", "normalizing", "chunking", "indexing", "ready", "failed"
]

#: kb_import_jobs.status 四态（api.md §4.7~4.9 镜像；不设 canceled 独立终态）
KbJobStatus = Literal["pending", "running", "completed", "failed"]


class KbDocument(Base):
    """知识库文档（Library 表格行；非 ready 文档不参与检索——ready 门不变）。"""

    __tablename__ = "kb_documents"
    __table_args__ = (
        # 文档列表：user 过滤 + 新在前（DESC 语义进索引定义，idx_threads_user_updated
        # 同款 TextClause 先例）+ Library 状态过滤轮询（§1.7 DDL 原文两索引）
        Index("idx_kb_docs_user_created", "user_id", text("created_at DESC")),
        Index("idx_kb_docs_user_status", "user_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column()  # 裸 uuid（无 FK，随 DDL 契约）
    doc_type: Mapped[str] = mapped_column(String(8))  # pdf|md|txt|docx|url
    title: Mapped[str] = mapped_column(String(512))  # 上传=文件名；URL=抓取标题
    source_url: Mapped[str | None] = mapped_column(Text)  # doc_type=url 时非空
    file_object_key: Mapped[str | None] = mapped_column(Text)  # MinIO kb 桶原件键
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    # sha256（批次 2 chunking 段写入；OQ-34 同步 diff 兜底判据前置——现行零写入点）
    content_hash: Mapped[str | None] = mapped_column(CHAR(64))
    status: Mapped[str] = mapped_column(
        String(12), server_default=text("'pending'")
    )  # KbDocStatus 六态（无 CHECK——文档性扩展零 DDL）
    fail_reason: Mapped[str | None] = mapped_column(Text)  # 阶段化 {stage}: {detail}
    chunk_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # {pages, tables, code_blocks, parser}——parser 键为存储侧留痕不进 §4.3 投影
    parse_meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # ORM onupdate=now()（评审 P3）：B17 worker 改 status 免逐处手工刷新——漏刷则
    # Library 列表 updated_at（api.md §4.3 投影）恒停在建行时刻的静默陈旧陷阱；
    # ORM 层行为，零 DDL 变更（§1.7 契约仅要求 not null default now()）
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # M3 增列（:301）：飞书目录链（root→leaf 连接器外部 token 数组）｜显示路径；
    # 本地上传/URL 来源恒 null（OQ-26 ⑦）
    catalog_ids: Mapped[list[Any] | None] = mapped_column(JSONB)
    catalog_path: Mapped[str | None] = mapped_column(Text)


class KbChunk(Base):
    """知识库切片（检索行；kb_doc_id FK CASCADE——REQ-022 删除语义即级联完成）。"""

    __tablename__ = "kb_chunks"
    __table_args__ = (
        # 文档内序号唯一（§1.8 DDL 原文：uq_kb_chunks_doc_idx）
        Index("uq_kb_chunks_doc_idx", "kb_doc_id", "chunk_index", unique=True),
        Index("idx_kb_chunks_user", "user_id"),
        # HNSW cosine（§3 决策：m=16/ef_construction=64 出厂即用无训练步；
        # 查询期 SET LOCAL hnsw.ef_search=40 由检索语句承担，非索引面）
        Index(
            "idx_kb_chunks_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": 16, "ef_construction": 64},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kb_doc_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_documents.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID] = mapped_column()  # 裸 uuid（检索过滤维度，无 FK）
    chunk_index: Mapped[int] = mapped_column(Integer)  # 文档内序号
    content: Mapped[str] = mapped_column(Text)  # 切片正文（默认 800 字符/120 重叠）
    token_count: Mapped[int] = mapped_column(Integer)  # 估算
    # vector(1024) NOT NULL——text-embedding-v4，维度=KB_EMBEDDING_DIM（一期定死）
    embedding: Mapped[Any] = mapped_column(Vector(1024))
    # {node_id, heading_path, loc, references, raw_type, char_start, char_end}
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class KbImportJob(Base):
    """飞书导入任务行（每次导入/同步一行；按文档单元逐个推进、可中断可恢复）。"""

    __tablename__ = "kb_import_jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column()  # 裸 uuid（无 FK，随 DDL 契约）
    connector: Mapped[str] = mapped_column(
        String(16), server_default=text("'feishu'")
    )  # M3 仅 feishu 合法（api.md §4.7 键位）
    trigger: Mapped[str] = mapped_column(String(16))  # manual | scheduled
    status: Mapped[str] = mapped_column(
        String(12), server_default=text("'pending'")
    )  # KbJobStatus 四态（不设 canceled）
    cursor: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # 断点游标
    # 进度五数 {total_docs, processed_docs, imported_docs, failed_docs, skipped_docs}
    # ——processed = imported + failed + skipped（api.md §4.9 一一对应）
    progress: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'")
    )
    # 失败清单条目 {title, category, message, document_id}——仅详情端点投影（§4.8）
    failures: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'"))
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # 耗时/调用量/token
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )  # null=进行中（写入方定值）
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )  # null=进行中
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # ORM onupdate=now()（评审 P3）：worker 推进 status/cursor/progress 免逐处手工
    # 刷新——同 KbDocument.updated_at 口径，零 DDL 变更（§1.17 仅 not null default）
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
