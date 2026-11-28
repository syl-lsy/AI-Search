"""b22 hitl approval gate persistence

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-27

0014_b22_hitl_gate（data-model.md §1.20 + §4 迁移表 0014 行；B22 HITL 审批门
契约先行批 v1.23，实施批次 2；REQ-069/A54）：
- **编号映射注（沿 0012/0013 先例）**：契约迁移表用逻辑名 0014_b22_hitl_gate
  （§4 v1.23 注：0014 现已被 b22_hitl_gate 占用、实施前不锁号；经迁移目录与链
  尾核实 0013_m3_kb 为链尾，2026-11-27），本迁移取实际下一号 **0014**，衔接链
  尾，未为凑号改历史链；
- 建表 **turn_approvals**（§1.20 DDL 原文逐字）：每轮至多一行的审批门记录——
  门载荷独立于 turns 热行（数十 KB 级 KB 片段原文与 rewrite 产物不进 turns 行读
  取面）、生命周期独立、线程删除经 turns 级联（FK ON DELETE CASCADE）；**web 直
  达轮亦落一行（轻量无载荷）**——「门判了零命中」可审计（R28 最坏失效的事后追
  查依据）；`verdict × choice` 天然构成误弹窗标注集（评测面免费数据）；
- **UNIQUE(turn_id)**（uq_turn_approvals_turn）一轮一行 + **超时部分索引**
  idx_turn_approvals_expiry (status, expires_at) WHERE status='pending'（扫描器
  判据面，data-model §1.20 :627）；除二者外零索引（:651 取舍注——无历史/列表查
  询需求）；
- **双列扩宽 varchar(12)→varchar(24)**（§1.3/§1.4 v1.23 列宽扩容注）：
  `awaiting_approval`（turns.status 第 8 值）与 `approval_requested` /
  `approval_resolved`（turn_events.event_type 枚举扩员）均 18 字符超旧列宽；
  varchar 扩宽为元数据级变更、锁时短；取值合法性仍由应用层 schema 校验承担
  （§4 规则不变，无 CHECK 约束）；
- downgrade 全量可逆（§4 迁移规则）：drop 表+两索引、两列回 varchar(12)——若
  存量行含 >12 字符值则 downgrade 报错属预期（awaiting 行应已收口，§1.20 :652
  downgrade 注原文）。

全部语句为 op.* 常量字面量（零 SQL 文本拼接）。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = '0014'
down_revision: Union[str, Sequence[str], None] = '0013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """turn_approvals 建表 + turns.status/turn_events.event_type 扩宽 varchar(24)。"""
    op.create_table(
        'turn_approvals',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column(
            'turn_id',
            sa.Uuid(),
            sa.ForeignKey('turns.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('status', sa.String(12), nullable=False, server_default='pending'),
        sa.Column('verdict', sa.String(12), nullable=False),
        sa.Column('hits_summary', JSONB(), nullable=False, server_default=text("'[]'")),
        sa.Column('previews', JSONB(), nullable=False, server_default=text("'[]'")),
        sa.Column('gate_payload', JSONB(), nullable=True),
        sa.Column('choice', sa.String(12), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('decided_by', sa.String(8), nullable=True),
        sa.Column(
            'requested_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('now()'),
        ),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('responded_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    # UNIQUE(turn_id)：一轮一行（§1.16 turn_feedback 先例——唯一索引而非列级约束）
    op.create_index('uq_turn_approvals_turn', 'turn_approvals', ['turn_id'], unique=True)
    # 超时扫描器判据面（§1.20 :627）：(status, expires_at) WHERE status='pending'
    op.create_index(
        'idx_turn_approvals_expiry',
        'turn_approvals',
        ['status', 'expires_at'],
        postgresql_where=text("status = 'pending'"),
    )
    # 列宽扩容注（§1.3/§1.4 v1.23）：awaiting_approval / approval_* 均 18 字符
    op.alter_column('turns', 'status', type_=sa.String(24))
    op.alter_column('turn_events', 'event_type', type_=sa.String(24))


def downgrade() -> None:
    """drop 表+两索引、两列回 varchar(12)（>12 字符存量值时报错属预期——已收口）。"""
    op.drop_index('idx_turn_approvals_expiry', table_name='turn_approvals')
    op.drop_index('uq_turn_approvals_turn', table_name='turn_approvals')
    op.drop_table('turn_approvals')
    op.alter_column('turns', 'status', type_=sa.String(12))
    op.alter_column('turn_events', 'event_type', type_=sa.String(12))
