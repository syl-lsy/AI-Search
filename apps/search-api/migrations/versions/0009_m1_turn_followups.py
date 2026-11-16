"""m1 turn followups

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-14

0009_m1_turn_followups（data-model.md §4 迁移表 0009 行 / §1.3 followups 行，REQ-046）：
- turns 增列 `followups jsonb`（nullable、无索引、无默认）——后续提问建议 string[]
  （长度 1~5，语言跟随 query_lang）；由 TurnRunner **图外钩子**在 done 落库后异步
  回填（ARCHITECTURE §6.1/ADR-011），deep 轮与失败/超时放弃轮恒 null。
- down_revision 经 `make db-current` 核实链尾 = 0008（0002~0007 尚未创建，M2/M3/M4/
  M5 里程碑未开工），同 0008 惯例按届时链尾衔接。
- REST §2.5 `followups` 字段为该列直读（事实源；SSE followups 事件仅为通知通道）；
  不入 turn_events（§1.4 落库子集不变）。

downgrade 全量可逆（§4 迁移规则）：drop column；零 SQL 文本拼接。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0009'
down_revision: Union[str, Sequence[str], None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # nullable jsonb（§1.3：string[] ≤5，无索引、无默认——未生成即 null）
    op.add_column(
        'turns',
        sa.Column('followups', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('turns', 'followups')
