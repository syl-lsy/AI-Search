"""m2 thread management columns

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-15

0011_m2_thread_mgmt（data-model.md §4 迁移表 0011 行 / §1.2 v1.14，REQ-058~060；
api.md §2.2/§2.3/§2.4/§2.10/§2.11 v1.18）：
- threads 增列 `pinned boolean NOT NULL DEFAULT false`（置顶——活跃列表排序首位；
  归档视图不消费）、`read_at timestamptz NULL`（NULL=未读；REST 投影 is_unread）、
  `archived_at timestamptz NULL`（NULL=活跃；REST 投影 archived）；
- **存量回填 `read_at = created_at`**（全部视为已读——升级后不出现整屏虚假未读点；
  D-01）；pinned/archived_at 存量天然 false/NULL，零回填；
- **零新索引**（§1.2 v1.14 取舍注，D-02）：threads ≤千级——q ILIKE 顺序扫、
  pinned 排序内存排序足够；触发条件（再议 pg_trgm+GIN / 复合索引）：threads > 5 万行
  或列表/q P95 > 200ms（二期）。
- down_revision 经 `make db-current` 与迁移目录核实链尾 = 0010（2026-11-15）。

downgrade 全量可逆（§4 迁移规则）：drop 三列；read_at 未读态数据丢失属 schema
回退可接受语义（文档写明，data-model §4 0011 行）。UPDATE 语句为常量字面量（零
外部输入拼接）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0011'
down_revision: Union[str, Sequence[str], None] = '0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'threads',
        sa.Column(
            'pinned',
            sa.Boolean(),
            server_default=sa.text('false'),
            nullable=False,
        ),
    )
    op.add_column(
        'threads', sa.Column('read_at', sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        'threads', sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True)
    )
    # 存量回填（D-01）：全部视为已读——避免升级后整屏虚假未读点
    op.execute(sa.text('UPDATE threads SET read_at = created_at WHERE read_at IS NULL'))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('threads', 'archived_at')
    op.drop_column('threads', 'read_at')
    op.drop_column('threads', 'pinned')
