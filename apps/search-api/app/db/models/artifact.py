"""artifacts 表 ORM（data-model.md §1.9，0012_m2_deep；M2 建表先承载 report）。

产物按线程组织（REQ-024，thread_id FK CASCADE）；turn_id 可空 FK 无级联动作
（report 导出关联轮次，轮删不连带删产物——随 DDL 契约零私改）；
artifact_type M2 只写 `report`，`code_output` 随 M4（0004_m4_eval_sandbox）启用。
rel_path 为相对 SANDBOX_ARTIFACTS_DIR 的路径（下载用）；主键 uuid4 客户端承接。
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Artifact(Base):
    """沙箱产物登记行（M2 = 深度报告导出；文件本体在沙箱产物目录）。"""

    __tablename__ = "artifacts"
    __table_args__ = (
        # 线程产物列表：thread_id 过滤 + 新在前（DESC 语义进索引定义，
        # idx_threads_user_updated 同款 TextClause 先例）
        Index("idx_artifacts_thread", "thread_id", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column()  # 裸 uuid（无 FK，随 DDL 契约）
    thread_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("threads.id", ondelete="CASCADE")
    )
    turn_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("turns.id")
    )  # 可空、无级联动作（随 DDL 契约）
    artifact_type: Mapped[str] = mapped_column(String(16))  # report | code_output
    filename: Mapped[str] = mapped_column(String(256))  # report_{YYYYMMDD_HHmm}.md
    rel_path: Mapped[str] = mapped_column(Text)  # 相对 SANDBOX_ARTIFACTS_DIR
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
