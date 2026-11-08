"""turns 表 ORM（data-model.md §1.3，0001 范围）。

0005/0006/0008/0009 的增列（incognito/memory_hits/cancel_reason/followups）随对应
迁移落地，本文件不得提前包含（护栏见 test_db_models.py）。
status/枚举列一律 varchar 无 CHECK（§4 规则：避开 PG enum 变更痛苦；取值校验在
schema 层与 service 层）。
"""
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
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


class Turn(Base):
    """轮次（一次提问→回答的完整生命周期；时间线/成本/错误均为 jsonb 列）。"""

    __tablename__ = "turns"
    __table_args__ = (
        Index("idx_turns_thread_created", "thread_id", "created_at"),
        # 部分索引（0001 形态）：进行中轮裁决（409001 检查/孤儿轮清理）；
        # 扩 queued 的 drop+recreate 属 0008（REQ-042）
        Index(
            "idx_turns_running",
            "thread_id",
            postgresql_where=text("status IN ('pending','running')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    thread_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("threads.id"))
    user_id: Mapped[uuid.UUID] = mapped_column()  # 裸 uuid（无 FK，随 DDL 契约）
    mode: Mapped[str] = mapped_column(String(8))  # fast|deep
    status: Mapped[str] = mapped_column(String(12), server_default="pending")
    query: Mapped[str] = mapped_column(Text)  # 原始问题（≤2000 字符，api 层校验）
    query_lang: Mapped[str | None] = mapped_column(String(4))  # zh|en（TurnRunner 注入）
    rewritten_queries: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'"))
    answer_md: Mapped[str | None] = mapped_column(Text)  # 流式增量 UPDATE，终态全量写一次
    chat_model_key: Mapped[str] = mapped_column(String(16))  # qwen|glm|deepseek（M4 视觉轮 vision）
    model_name: Mapped[str] = mapped_column(String(64))  # Nacos 映射解析结果（trace 可证 A10）
    parent_turn_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("turns.id")
    )  # REQ-017：深度报告后的快速追问
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # {code,message,stage_key}
    timing: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'"))
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'"))
    langfuse_trace_id: Mapped[str | None] = mapped_column(String(64))  # session=thread_id
    degraded_reason: Mapped[str | None] = mapped_column(String(16))  # M2 起写入
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)  # 提交→done
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
