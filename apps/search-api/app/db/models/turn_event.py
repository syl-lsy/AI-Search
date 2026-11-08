"""turn_events 表 ORM（data-model.md §1.4；评测回放/审计）。

持久化 stage/sources/images/done/error 五类事件（token 增量不落库，ADR-003；
thinking/context_compact 同口径；SSE followups 事件不入库——v1.9 注）。
seq 线程内单调递增：EventBus 发布时取 max(seq) per thread + 1，uq 唯一索引兜底。
"""
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TurnEvent(Base):
    """轮次事件（SSE id: 同源 = thread_id+seq；turn 删除级联清事件）。"""

    __tablename__ = "turn_events"
    __table_args__ = (
        Index("uq_turn_events_thread_seq", "thread_id", "seq", unique=True),
        Index("idx_turn_events_turn", "turn_id", "seq"),
    )

    # bigint GENERATED ALWAYS AS IDENTITY（五表唯一非 uuid 主键；DB 端生成，应用不赋值）
    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    thread_id: Mapped[uuid.UUID] = mapped_column()  # 裸 uuid（无 FK，随 DDL 契约）
    turn_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("turns.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)  # 线程内单调递增
    event_type: Mapped[str] = mapped_column(String(12))  # stage|sources|images|done|error（M6 起 +memory）
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)  # 去 envelope 冗余字段后存储
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
