"""m1 turn scheduler

Revision ID: 0008
Revises: 0001
Create Date: 2026-11-08

0008_m1_turn_scheduler（data-model.md §4 迁移表 0008 行，REQ-042；down_revision="0001"
经 `make db-current` 核实链尾 0001 后衔接——0005~0007 未创建，B2 开工时链尾即 0001）：
- turns 增列 `cancel_reason varchar(16)`（§1.3 104 行：user|superseded|rolled_back，
  仅 status=canceled 时非空；nullable 无默认——取消不是 error，不复用 error 列语义）；
- idx_turns_running 部分索引条件重建（§1.3 141-142 行：PG 不支持 ALTER INDEX 改
  WHERE，drop+recreate；条件扩 queued——enqueue 排队轮同入 409001 裁决与孤儿轮
  清理口径）。
- turns.status 扩 queued 为文档性扩展（列本为 varchar 无 CHECK，零 DDL，§1.3 140 行）。

downgrade 全量可逆（§4 迁移规则）：索引还原 ('pending','running') + drop column；
一律 op/SQLAlchemy 表达式，零 SQL 文本拼接。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0008'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# idx_turns_running 两个段位的 WHERE 条件（契约 §1.3 137/141 行原文；sa.text
# TextClause——部分索引 WHERE 以 DDL 原文表达，autogenerate/check 同口径渲染）
_INDEX_WHERE_BASE = "status IN ('pending','running')"
_INDEX_WHERE_QUEUED = "status IN ('pending','running','queued')"


def upgrade() -> None:
    """Upgrade schema."""
    # cancel_reason：一等 varchar 列（与 status/degraded_reason 同款风格，§1.3 104
    # 行落位取舍）；op.add_column 物理上追加表尾，ORM/契约字段序以 §1.3 表序为准
    op.add_column(
        'turns', sa.Column('cancel_reason', sa.String(length=16), nullable=True)
    )
    # 部分索引重建：drop 0001 形态 → 按 0008 口径 recreate（PG 无 ALTER INDEX 改
    # WHERE；重建为常规操作锁时短，§1.3 142 行）
    op.drop_index(
        'idx_turns_running',
        table_name='turns',
        postgresql_where=sa.text(_INDEX_WHERE_BASE),
    )
    op.create_index(
        'idx_turns_running',
        'turns',
        ['thread_id'],
        unique=False,
        postgresql_where=sa.text(_INDEX_WHERE_QUEUED),
    )


def downgrade() -> None:
    """Downgrade schema."""
    # 逆序全量可逆：索引还原 0001 条件 → drop column（§4 规则「索引/增列 drop 即可」）
    op.drop_index(
        'idx_turns_running',
        table_name='turns',
        postgresql_where=sa.text(_INDEX_WHERE_QUEUED),
    )
    op.create_index(
        'idx_turns_running',
        'turns',
        ['thread_id'],
        unique=False,
        postgresql_where=sa.text(_INDEX_WHERE_BASE),
    )
    op.drop_column('turns', 'cancel_reason')
