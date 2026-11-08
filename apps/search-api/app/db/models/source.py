"""sources 表 ORM（data-model.md §1.5，M1 基础列）。

snapshot_id（M2）/kb_doc_id/kb_chunk_ids（M3）随对应迁移增列，本文件不得提前
包含（护栏见 test_db_models.py）。id 即 api.md 的 source_id。
"""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, SmallInteger, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
