"""threads 表 ORM（data-model.md §1.2）。

mode_summary/turn_count 为冗余聚合列：写入轮次时由 service 层维护（不在 ORM 侧
触发器/事件里做，保持图节点纯函数、持久化归 TurnRunner 的总口径）。
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Thread(Base):
    """线程（REQ-006：title=首问前 50 字符；列表按 updated_at DESC 排序）。"""

    __tablename__ = "threads"
    __table_args__ = (
        # 会话列表主查询：user_id 过滤 + 最近更新在前（DESC 语义进索引定义）
        Index("idx_threads_user_updated", "user_id", text("updated_at DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))  # 全局约束 user_id 过滤
    title: Mapped[str] = mapped_column(String(120))
    mode_summary: Mapped[str] = mapped_column(String(8), server_default="fast")  # fast|deep|mixed
    turn_count: Mapped[int] = mapped_column(Integer, server_default="0")  # 冗余计数
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
