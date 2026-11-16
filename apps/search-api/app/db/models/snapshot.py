"""snapshots 表 ORM（data-model.md §1.6，0012_m2_deep；M2）。

仅深度模式：子代理已读原文存档（Firecrawl markdown），正文存 MinIO 对象
（object_key 路径契约 `snapshots/{user_id}/{thread_id}/{turn_id}/{url_hash}.md`），
行级元数据落本表；存档不参与检索（共识约束）。大文件口径（ARCH §10 Q2）：
>2MB 截断入库、对象全量存 MinIO。
主键 uuid4 由 ORM 客户端承接（全局口径，DDL 无 db 端 DEFAULT）；
user_id/thread_id 裸 uuid 无 FK（随 DDL 契约，同 source.py 先例）；
fetched_at 无服务端默认——写入方（save_snapshot，批次 4）定值。
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Snapshot(Base):
    """网页快照（url_hash=sha256(url)；唯一键 (turn_id, url_hash) 一轮一 URL 一行）。"""

    __tablename__ = "snapshots"
    __table_args__ = (
        # 唯一索引而非列级 UNIQUE（uq_* 命名口径，同 uq_sources_turn_ref 先例）：
        # 同轮同 URL 去重（save_snapshot 双执行仅一行一对象，M2 计划批次 1）
        Index("uq_snapshots_turn_url", "turn_id", "url_hash", unique=True),
        # 快照入口按线程回看（§1.6 DDL）
        Index("idx_snapshots_thread", "thread_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column()  # 裸 uuid（无 FK，随 DDL 契约）
    thread_id: Mapped[uuid.UUID] = mapped_column()  # 裸 uuid（无 FK，随 DDL 契约）
    turn_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("turns.id", ondelete="CASCADE")
    )
    url: Mapped[str] = mapped_column(Text)  # 原始 URL
    url_hash: Mapped[str] = mapped_column(CHAR(64))  # sha256(url)（唯一键用）
    object_key: Mapped[str] = mapped_column(Text)  # MinIO 对象键
    content_type: Mapped[str] = mapped_column(
        String(64), server_default="text/markdown"
    )
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
