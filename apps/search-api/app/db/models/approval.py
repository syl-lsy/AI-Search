"""turn_approvals 表 ORM（data-model.md §1.20，0014_b22_hitl_gate；B22/REQ-069）。

每轮至多一行的审批门记录：门载荷独立于 turns 热行（数十 KB 级 KB 片段原文与
rewrite 产物不进 turns 行读取面）、生命周期独立、线程删除经 turns 级联。web 直达
轮亦落一行（轻量无载荷）——「门判了零命中」可审计（R28 最坏失效的事后追查依据）；
`verdict × choice` 天然构成误弹窗标注集（评测面免费数据，PRD 评测面条款）。

**状态投影映射注（api.md §2.5 v1.37 approval 四形态 ↔ 本表 status 六态）**：
REST 2.5 投影把本表 `approved` 与 `expired` 统一投影为 `approval.status="approved"`
（已裁决形态——`decided_by` 承载 user/timeout 区分）；`shadow`/`no_hit` 按原样投影；
`pending` 即轮 `status=awaiting_approval` 的待裁决形态。存储与投影命名分歧为契约
自身定义（§1.20 status 注 vs §2.5 decided 形态），非实现漂移。

**gate_payload.schema_version（R30 跨进程契约）**：阶段 1 代码写、阶段 2 代码读；
读不懂的版本 → 该轮 failed + 明确 error，不猜（ARCH §10 R30）。payload 形状：
`{schema_version, rewritten_queries, query_type, time_range, query_lang,
kb_candidates, planner?}`（planner 键 deep 批次 4 增量预留——api.md §7.4 挂账注）。
"""
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# ---- 行形状常量（契约值域唯一事实源：data-model.md §1.20 逐字）----

#: §1.20 status 六态：pending=待裁决（轮 status=awaiting_approval）；
#: approved=已裁决；expired=超时收口（decided_by=timeout）；canceled=用户取消/
#: 打断收尾；shadow=shadow 观测态（只观测零行为面）；no_hit=web 直达留痕（轻量、无载荷）
APPROVAL_STATUSES = (
    "pending",
    "approved",
    "expired",
    "canceled",
    "shadow",
    "no_hit",
)

#: §1.20 verdict 两值——门的判定面留痕（has_hit ∧ choice=web_only = 误弹窗统计分子）
VERDICTS = ("has_hit", "no_hit")
VERDICT_HAS_HIT = "has_hit"
VERDICT_NO_HIT = "no_hit"

#: §1.20 choice 四值（用户裁决；timeout/收口时为缺省策略值）
APPROVAL_CHOICES = ("kb_only", "web_only", "both", "custom")

#: R30 跨进程契约版本锚点：阶段 1（门节点写 gate_payload）与阶段 2（恢复路径读）
#: 的版本门槛——读不懂的版本 → 轮 failed + 明确 error，不猜（TurnRunner 恢复段消费）
GATE_PAYLOAD_SCHEMA_VERSION = 1


class TurnApproval(Base):
    """轮次审批门记录（UNIQUE(turn_id) 一轮一行；超时部分索引 WHERE status='pending'）。"""

    __tablename__ = "turn_approvals"
    __table_args__ = (
        # UNIQUE(turn_id)：一轮一行（§1.20 DDL「CREATE UNIQUE INDEX uq_turn_approvals_turn」；
        # 唯一索引而非列级约束——§1.16 turn_feedback 先例）
        Index("uq_turn_approvals_turn", "turn_id", unique=True),
        # 超时扫描器判据面（§1.20 :627）：部分索引 WHERE status='pending'
        Index(
            "idx_turn_approvals_expiry",
            "status",
            "expires_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    turn_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("turns.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(12), server_default=text("'pending'"))
    verdict: Mapped[str] = mapped_column(String(12))
    hits_summary: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'"))
    previews: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'"))
    gate_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    choice: Mapped[str | None] = mapped_column(String(12))
    note: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(String(8))
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
