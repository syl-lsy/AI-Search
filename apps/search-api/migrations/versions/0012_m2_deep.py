"""m2 deep persistence base

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-16

0012_m2_deep（data-model.md §1.5/§1.6/§1.9 + §4 迁移表「0002_m2_deep」行；M2 批次 1）：
- **编号映射注**：契约迁移表用逻辑名 0002_m2_deep（0002~0007 空号为契约占位），
  实际迁移链为 0001→0008→0009→0010→0011（经迁移目录与 `make db-current` 核实
  链尾 = 0011，2026-11-16），本迁移取实际下一号 **0012**，衔接链尾；
- 建表 **snapshots**（§1.6）：网页快照行级元数据（正文存 MinIO，object_key 契约
  `snapshots/{user_id}/{thread_id}/{turn_id}/{url_hash}.md`；存档不参与检索——
  共识约束）；url_hash char(64)=sha256(url)（唯一键用，URL 可能超长）；
  content_type 默认 'text/markdown'；fetched_at 无服务端默认（写入方定值）；
  唯一索引 uq_snapshots_turn_url (turn_id, url_hash)（同轮同 URL 去重——
  save_snapshot 双执行仅一行一对象）+ idx_snapshots_thread (thread_id)；
  turn_id FK→turns.id ON DELETE CASCADE；
- 建表 **artifacts**（§1.9，M2 建表先承载 report、M4 扩 code_output）：产物按线程
  组织（REQ-024）——thread_id FK→threads.id ON DELETE CASCADE；turn_id 可空
  FK→turns.id 无级联动作（report 导出关联轮次，随 DDL 契约零私改）；
  idx_artifacts_thread (thread_id, created_at DESC)；
- **sources 增列 snapshot_id**（§1.5 M2）：uuid 可空、FK→snapshots.id 无级联动作
  （契约 SQL 原文「REFERENCES snapshots(id)」未带 ON DELETE，保持默认 NO ACTION；
  快照清理沿线程删除链：先删 turns → sources/snapshots 随 CASCADE 清空）；
- 零数据回填（纯 schema 增量：两表全新、增列可空，存量行 NULL=无快照，
  REST 投影 snapshot_available=false 语义与升级前一致——零行为变化）。

downgrade 全量可逆（§4 迁移规则）：drop sources.snapshot_id 列**先于** drop
snapshots 表（NO ACTION FK 依赖拒删，顺序敏感）；artifacts/snapshots 按
drop index → drop table 收口；全部语句为 op.* 常量字面量（零 SQL 文本拼接）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0012'
down_revision: Union[str, Sequence[str], None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # snapshots 必须先于 sources.snapshot_id 存在（FK 引用目标）
    op.create_table(
        'snapshots',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('thread_id', sa.Uuid(), nullable=False),
        sa.Column('turn_id', sa.Uuid(), nullable=False),
        sa.Column('url', sa.Text(), nullable=False),
        sa.Column('url_hash', sa.CHAR(64), nullable=False),
        sa.Column('object_key', sa.Text(), nullable=False),
        sa.Column(
            'content_type',
            sa.String(64),
            server_default=sa.text("'text/markdown'"),
            nullable=False,
        ),
        sa.Column('size_bytes', sa.BigInteger(), nullable=False),
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['turn_id'], ['turns.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'uq_snapshots_turn_url', 'snapshots', ['turn_id', 'url_hash'], unique=True
    )
    op.create_index('idx_snapshots_thread', 'snapshots', ['thread_id'], unique=False)

    op.create_table(
        'artifacts',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('thread_id', sa.Uuid(), nullable=False),
        sa.Column('turn_id', sa.Uuid(), nullable=True),
        sa.Column('artifact_type', sa.String(16), nullable=False),
        sa.Column('filename', sa.String(256), nullable=False),
        sa.Column('rel_path', sa.Text(), nullable=False),
        sa.Column('size_bytes', sa.BigInteger(), nullable=False),
        sa.Column('content_type', sa.String(64), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['thread_id'], ['threads.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['turn_id'], ['turns.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'idx_artifacts_thread',
        'artifacts',
        ['thread_id', sa.literal_column('created_at DESC')],
        unique=False,
    )

    op.add_column(
        'sources',
        sa.Column('snapshot_id', sa.Uuid(), sa.ForeignKey('snapshots.id'), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    # 顺序敏感：先删引用列（sources.snapshot_id FK NO ACTION 依赖 snapshots 行存在），
    # 再 drop 两表
    op.drop_column('sources', 'snapshot_id')
    op.drop_index('idx_artifacts_thread', table_name='artifacts')
    op.drop_table('artifacts')
    op.drop_index('idx_snapshots_thread', table_name='snapshots')
    op.drop_index('uq_snapshots_turn_url', table_name='snapshots')
    op.drop_table('snapshots')
