"""m2 turn feedback

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-14

0010_m2_turn_feedback（data-model.md §4 迁移表 0010 行 / §1.16，REQ-057；api.md §2.9）：
- 建表 turn_feedback（id uuid PK / turn_id uuid NOT NULL FK→turns.id ON DELETE CASCADE /
  helpful boolean NOT NULL / created_at、updated_at timestamptz NOT NULL DEFAULT now()）；
- 唯一索引 uq_turn_feedback_turn (turn_id)——一轮一行，承载 api.md §2.9 的 upsert 改判
  （INSERT … ON CONFLICT (turn_id) DO UPDATE，无撤销态）；
- 无 user_id 冗余（经 turn_id→turns 间接归属，同 turn_events 先例）、无其他索引
  （自用无报表面，§1.16 取舍）。
- down_revision 经 `make db-current` 核实链尾 = 0008 → 本批同时创建 0009
  （turns.followups，REQ-046 实现缺口补口），故本迁移衔接 0009。

downgrade 全量可逆（§4 迁移规则）：drop index + drop table；零 SQL 文本拼接。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0010'
down_revision: Union[str, Sequence[str], None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'turn_feedback',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('turn_id', sa.Uuid(), nullable=False),
        sa.Column('helpful', sa.Boolean(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['turn_id'], ['turns.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'uq_turn_feedback_turn', 'turn_feedback', ['turn_id'], unique=True
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_turn_feedback_turn', table_name='turn_feedback')
    op.drop_table('turn_feedback')
