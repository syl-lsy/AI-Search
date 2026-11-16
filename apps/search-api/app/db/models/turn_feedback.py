"""turn_feedback 表 ORM（data-model.md §1.16，0010_m2_turn_feedback；REQ-057）。

一轮一行（`UNIQUE(turn_id)` 承载 api.md §2.9 的 upsert 改判语义：重复提交覆盖既有行、
无撤销态）；主键 uuid4 由 ORM 客户端承接（全局口径，DDL 无 db 端 DEFAULT）；无
user_id 冗余（经 turn_id→turns 间接归属，同 turn_events 先例）。
helpful 列名与 SQLAlchemy `column.helpful` 类名无冲突（非保留字）。
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TurnFeedback(Base):
    """轮次有帮助反馈（api.md §2.9：true=有帮助 / false=没有帮助；改判即覆盖更新）。"""

    __tablename__ = "turn_feedback"
    __table_args__ = (
        # UNIQUE(turn_id)：一轮一行（§1.16 DDL「CREATE UNIQUE INDEX uq_turn_feedback_turn」；
        # 唯一索引而非列级 UNIQUE 约束——ON CONFLICT (turn_id) 按索引推定同样生效）
        Index("uq_turn_feedback_turn", "turn_id", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    turn_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("turns.id", ondelete="CASCADE")
    )
    helpful: Mapped[bool] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
