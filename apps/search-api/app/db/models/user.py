"""users 表 ORM（data-model.md §1.1；MVP 仅默认用户一行）。

id 值 = `.env DEFAULT_USER_ID`（迁移 0001 幂等 seed），ORM 不带默认——二期 auth
时 id 来源换成登录态，不动表。
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class User(Base):
    """用户（MVP 仅默认用户一行；全局约束：一切用户数据表携带 user_id）。"""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)  # 迁移 0001 幂等 seed 指定（app/db/seed.py）
    name: Mapped[str] = mapped_column(String(64))  # 展示名（侧栏底部）
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
