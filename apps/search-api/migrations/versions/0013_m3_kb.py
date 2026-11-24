"""m3 kb persistence base

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-23

0013_m3_kb（data-model.md §1.5/§1.7/§1.8/§1.17 + §4 迁移表「0003_m3_kb」行；
M3 批次 1 B16）：
- **编号映射注（沿 0012_m2_deep 行先例）**：契约迁移表用逻辑名 0003_m3_kb
  （0002~0007 空号为契约占位；§4 v1.19 注钉定本迁移编号预留），实际迁移链为
  0001→0008→0009→0010→0011→0012（经迁移目录与链尾核实 0012，2026-11-23），
  本迁移取实际下一号 **0013**，衔接链尾，未为凑号改历史链；
- 建表 **kb_documents**（§1.7）：知识库文档行级元数据——基础 15 列（§1.7 DDL
  原文）+ **M3 增列 catalog_ids jsonb / catalog_path text**（:301 已批准
  2026-11-22 确认点②；建表迁移一并落地，本地上传/URL 恒 null）；**status
  varchar(12) 六态（pending|normalizing|chunking|indexing|ready|failed，旧
  parsing/embedding 作废——api.md §4.3 v1.30 镜像）无 CHECK**（enum 一律
  varchar，状态扩容=文档性扩展零 DDL）；content_hash char(64) 可空（批次 2
  chunking 段写入）；parse_meta jsonb not null default '{}'（存储空对象 ↔ REST
  投影 null 映射在 service 层）；索引 idx_kb_docs_user_created (user_id,
  created_at DESC) + idx_kb_docs_user_status (user_id, status)；全表零 FK；
- 建表 **kb_chunks**（§1.8）：切片 + pgvector 向量——kb_doc_id FK→kb_documents.id
  ON DELETE CASCADE（REQ-022 删除语义即级联完成）；embedding **vector(1024)
  NOT NULL**（text-embedding-v4，一期 1024 定死——§4 回滚注：维度变更属破坏性
  迁移二期治理）；meta jsonb not null default '{}'（七键形状约定零 DDL）；
  索引 uq_kb_chunks_doc_idx UNIQUE (kb_doc_id, chunk_index) + idx_kb_chunks_user
  (user_id) + **idx_kb_chunks_hnsw USING hnsw (embedding vector_cosine_ops)
  WITH (m=16, ef_construction=64)**（§3 决策：HNSW+cosine 出厂即用，建于空表
  随写入增量构建）；
- 建表 **kb_import_jobs**（§1.17 基线形态，v1.19 增；REQ-067/A53）：飞书导入
  任务行——status **四态**（pending|running|completed|failed）default 'pending'
  不设 canceled 独立终态（api.md §4.7~4.9 镜像防投影面漂移）；connector
  varchar(16) default 'feishu'；trigger varchar(16)（manual|scheduled）；
  cursor jsonb 可空（断点游标）；progress 五数 / failures 清单 / stats 观测
  为 jsonb；started_at/finished_at 可空（null=进行中）；**零索引**（§1.17
  取舍：自用单人、串行 worker、量级 ≤ 百级）；worker 为图外组件（ADR-011），
  进程重启经启动钩子重入队 status IN (pending,running)；
- **sources 增列 kb_doc_id / kb_chunk_ids**（§1.5 M3，:224 契约 SQL 原文）：
  两列均可空、**无 FK**（对比 snapshot_id 显式 REFERENCES snapshots(id)——
  kb_doc_id 契约原文无 REFERENCES，零私改；kb_chunk_ids jsonb 命中切片 id
  数组，引用下钻预留 REQ-053）；零数据回填（纯 schema 增量，存量行 NULL=非
  KB 来源，行为与升级前一致）；
- **配套动作（同批契约钉死，data-model.md §4 :608 契约注）**：启动钩子 KB
  重入队 IN 列表同步扩容为 (pending,normalizing,chunking,indexing) /
  (pending,running)——旧 parsing 作废；漏改钩子列表则中间态文档重启静默卡死
  （单测锁死：test_main.py 常量逐字断言）。

downgrade 全量可逆（§4 迁移规则）：drop sources 两列；kb_chunks **先于**
kb_documents drop（FK CASCADE 依赖顺序）；三表按 drop index → drop table
收口（kb_import_jobs 无索引直 drop）；全部语句为 op.* 常量字面量（零 SQL
文本拼接）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = '0013'
down_revision: Union[str, Sequence[str], None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # kb_documents 必须先于 kb_chunks 存在（FK 引用目标）
    op.create_table(
        'kb_documents',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('doc_type', sa.String(8), nullable=False),
        sa.Column('title', sa.String(512), nullable=False),
        sa.Column('source_url', sa.Text(), nullable=True),
        sa.Column('file_object_key', sa.Text(), nullable=True),
        sa.Column('file_size_bytes', sa.BigInteger(), nullable=True),
        sa.Column('content_hash', sa.CHAR(64), nullable=True),
        sa.Column(
            'status', sa.String(12), server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column('fail_reason', sa.Text(), nullable=True),
        sa.Column(
            'chunk_count', sa.Integer(), server_default=sa.text('0'), nullable=False
        ),
        sa.Column(
            'parse_meta', JSONB(), server_default=sa.text("'{}'"), nullable=False
        ),
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
        sa.Column('ready_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('catalog_ids', JSONB(), nullable=True),
        sa.Column('catalog_path', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'idx_kb_docs_user_created',
        'kb_documents',
        ['user_id', sa.literal_column('created_at DESC')],
        unique=False,
    )
    op.create_index(
        'idx_kb_docs_user_status', 'kb_documents', ['user_id', 'status'], unique=False
    )

    op.create_table(
        'kb_chunks',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('kb_doc_id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('chunk_index', sa.Integer(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('token_count', sa.Integer(), nullable=False),
        sa.Column('embedding', Vector(1024), nullable=False),
        sa.Column('meta', JSONB(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        # 表级 FK 唯一定义（Column 级不重复挂——否则 PG 建出两个同名 FK 约束，
        # 0012 snapshots 先例同款形态）
        sa.ForeignKeyConstraint(['kb_doc_id'], ['kb_documents.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'uq_kb_chunks_doc_idx',
        'kb_chunks',
        ['kb_doc_id', 'chunk_index'],
        unique=True,
    )
    op.create_index('idx_kb_chunks_user', 'kb_chunks', ['user_id'], unique=False)
    op.create_index(
        'idx_kb_chunks_hnsw',
        'kb_chunks',
        ['embedding'],
        unique=False,
        postgresql_using='hnsw',
        postgresql_ops={'embedding': 'vector_cosine_ops'},
        postgresql_with={'m': 16, 'ef_construction': 64},
    )

    op.create_table(
        'kb_import_jobs',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column(
            'connector', sa.String(16), server_default=sa.text("'feishu'"),
            nullable=False,
        ),
        sa.Column('trigger', sa.String(16), nullable=False),
        sa.Column(
            'status', sa.String(12), server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column('cursor', JSONB(), nullable=True),
        sa.Column(
            'progress', JSONB(), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column(
            'failures', JSONB(), server_default=sa.text("'[]'"), nullable=False
        ),
        sa.Column('stats', JSONB(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint('id'),
    )
    # 零索引（§1.17 取舍注：自用单人、串行 worker，顺查足够）

    # sources 增列（§1.5 M3，:224 契约 SQL 原文——两列均可空、无 FK）
    op.add_column('sources', sa.Column('kb_doc_id', sa.Uuid(), nullable=True))
    op.add_column('sources', sa.Column('kb_chunk_ids', JSONB(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    # sources 两列无 FK 无依赖，先收（沿「先删引用列再 drop 表」惯例）
    op.drop_column('sources', 'kb_chunk_ids')
    op.drop_column('sources', 'kb_doc_id')
    # kb_import_jobs 无索引直 drop；kb_chunks 的 FK CASCADE 依赖 kb_documents——
    # drop kb_chunks 必须先于 kb_documents（顺序敏感）
    op.drop_table('kb_import_jobs')
    op.drop_index('idx_kb_chunks_hnsw', table_name='kb_chunks')
    op.drop_index('idx_kb_chunks_user', table_name='kb_chunks')
    op.drop_index('uq_kb_chunks_doc_idx', table_name='kb_chunks')
    op.drop_table('kb_chunks')
    op.drop_index('idx_kb_docs_user_status', table_name='kb_documents')
    op.drop_index('idx_kb_docs_user_created', table_name='kb_documents')
    op.drop_table('kb_documents')
