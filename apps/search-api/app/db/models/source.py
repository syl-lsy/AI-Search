"""sources 表 ORM（data-model.md §1.5，M1 基础列 + 0012 增 snapshot_id + 0013 增 kb 两列）。

snapshot_id（M2，0012）：深度已读原文指向 snapshots.id——可空、无级联动作
（随 DDL 契约零私改；REST 投影 snapshot_available=本列非空，turn_service 推导）。
kb_doc_id/kb_chunk_ids（M3，0013/m3_kb）：知识库来源回链——kb_doc_id 可空裸 uuid
无 FK（契约 SQL 原文无 REFERENCES；「指向 kb_documents.id」仅为文档性注记）、
kb_chunk_ids jsonb 可空数组（命中切片 id 数组——引用下钻预留，REQ-053）。
id 即 api.md 的 source_id。
"""
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, SmallInteger, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base


class Source(Base):
    """轮次来源（引用编号 ref_no 与正文 [n] 一一对应；KB 上传文件无 URL → null）。"""

    __tablename__ = "sources"
    __table_args__ = (
        Index("uq_sources_turn_ref", "turn_id", "ref_no", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    turn_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("turns.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column()  # 裸 uuid（无 FK，随 DDL 契约）
    ref_no: Mapped[int] = mapped_column(SmallInteger)  # 轮内引用编号
    source_type: Mapped[str] = mapped_column(String(8))  # web|kb（【网络】/【知识库】徽标）
    title: Mapped[str] = mapped_column(String(512))
    url: Mapped[str | None] = mapped_column(Text)
    snippet: Mapped[str | None] = mapped_column(Text)  # 摘要/片段（卡片副文本）
    score: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))  # 检索/rerank 得分
    # 0012/m2_deep 增列（§1.5 M2）：深度已读原文 → snapshots.id（可空、无级联动作）
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("snapshots.id")
    )
    # 0013/m3_kb 增列（§1.5 M3）：知识库来源回链（两列均可空、无 FK——契约 SQL
    # 原文「ADD COLUMN kb_doc_id uuid, ADD COLUMN kb_chunk_ids jsonb」零 REFERENCES）
    kb_doc_id: Mapped[uuid.UUID | None] = mapped_column()  # → kb_documents.id（文档性注记）
    kb_chunk_ids: Mapped[list[Any] | None] = mapped_column(JSONB)  # 命中切片 id 数组
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
