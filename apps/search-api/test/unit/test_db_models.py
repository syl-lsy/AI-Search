"""unit 层：核心表 ORM metadata 对照 data-model.md §1.1~§1.9/§1.17 DDL 契约（零网络）。

models/ 未落地或列漂移时本文件先红（TDD 红绿循环的契约锚点）；真库行为
（迁移链 upgrade/downgrade/seed/扩展）由 integration 层 test_migrations.py 兜住。
断言粒度 = DDL 逐条：列/类型/可空/服务端默认/主键/FK（含 ON DELETE CASCADE）/
索引（唯一/DESC/部分索引 WHERE）/identity。

0001 范围护栏：后续迁移的增列（turns.incognito/memory_hits）在 0001 不得提前
出现（turns.cancel_reason 已随 0008、turns.followups 已随 0009 落地，移出护栏、
纳入 §1.3 逐列断言；sources.snapshot_id 已随 0012/m2_deep、sources.kb_doc_id/
kb_chunk_ids 已随 0013/m3_kb 落地，移出护栏、纳入 §1.5 逐列断言）。
"""
import uuid
from typing import get_args

import pytest
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

import app.db.models  # noqa: F401 —— 统一 import 确保五表进 Base.metadata
from app.db.base import Base
from app.db.models.kb import KbDocStatus, KbJobStatus


def _table(name: str):
    try:
        return Base.metadata.tables[name]
    except KeyError:
        pytest.fail(f"Base.metadata 缺表 {name}（db/models/ 未落地或 __init__.py 未 import）")


def _col(table, name: str):
    try:
        return table.columns[name]
    except KeyError:
        pytest.fail(f"表 {table.name} 缺列 {name}")


def _fk(table, col_name: str):
    fks = list(table.columns[col_name].foreign_keys)
    assert len(fks) == 1, f"{table.name}.{col_name} 应恰有一个 FK，得到 {len(fks)}"
    return fks[0]


def _index(table, name: str):
    for ix in table.indexes:
        if ix.name == name:
            return ix
    pytest.fail(f"表 {table.name} 缺索引 {name}")


def _assert_server_default(col, expected: str) -> None:
    assert col.server_default is not None, f"{col.table.name}.{col.name} 缺 server_default"
    assert str(col.server_default.arg) == expected, (
        f"{col.table.name}.{col.name} server_default 应为 {expected!r}，"
        f"得到 {str(col.server_default.arg)!r}"
    )


def _assert_tz_now(col) -> None:
    assert isinstance(col.type, DateTime) and col.type.timezone is True
    _assert_server_default(col, "now()")


def _assert_uuid_pk(col, *, client_default_uuid4: bool) -> None:
    assert col.primary_key and col.nullable is False
    assert col.server_default is None, "uuid 主键无 DB 端默认（DDL 契约）"
    if client_default_uuid4:
        # 全局约束「主键一律 UUID v4」：应用侧 uuid.uuid4() 生成
        # （SQLAlchemy 2.0 将 callable 包为 CallableColumnDefault，arg 需传 ctx 调用）
        assert col.default is not None and col.default.is_callable
        value = col.default.arg(None)
        assert isinstance(value, uuid.UUID) and value.version == 4
    else:
        assert col.default is None


# ---- 表清单（0001 范围 = 恰好五表）----


def test_metadata_contains_exactly_core_tables():
    """M1 五表 + M2 增量表（§1.16 turn_feedback，0010 / §1.6 snapshots、§1.9
    artifacts，0012 / §1.7 kb_documents、§1.8 kb_chunks、§1.17 kb_import_jobs，
    0013 / §1.20 turn_approvals，0014）——不得有多余/漏登表。"""
    assert set(Base.metadata.tables) == {
        "users",
        "threads",
        "turns",
        "turn_events",
        "sources",
        "turn_feedback",
        "snapshots",
        "artifacts",
        "kb_documents",
        "kb_chunks",
        "kb_import_jobs",
        "turn_approvals",
    }


# ---- §1.1 users ----


def test_users_columns():
    users = _table("users")
    assert [c.name for c in users.columns] == ["id", "name", "created_at"]

    id_col = _col(users, "id")
    # id 值 = .env DEFAULT_USER_ID（启动幂等 seed 决定），ORM 不带默认
    _assert_uuid_pk(id_col, client_default_uuid4=False)

    name_col = _col(users, "name")
    assert isinstance(name_col.type, String) and name_col.type.length == 64
    assert name_col.nullable is False and name_col.server_default is None

    _assert_tz_now(_col(users, "created_at"))


# ---- §1.2 threads ----


def test_threads_columns():
    threads = _table("threads")
    assert [c.name for c in threads.columns] == [
        "id",
        "user_id",
        "title",
        "mode_summary",
        "turn_count",
        "created_at",
        "updated_at",
        # v1.14 增列（0011，REQ-058~060）：线程管理三列
        "pinned",
        "read_at",
        "archived_at",
    ]

    _assert_uuid_pk(_col(threads, "id"), client_default_uuid4=True)

    user_id = _col(threads, "user_id")
    assert user_id.nullable is False
    fk = _fk(threads, "user_id")
    assert fk.target_fullname == "users.id" and fk.ondelete is None

    title = _col(threads, "title")
    assert isinstance(title.type, String) and title.type.length == 120
    assert title.nullable is False and title.server_default is None

    mode_summary = _col(threads, "mode_summary")
    assert isinstance(mode_summary.type, String) and mode_summary.type.length == 8
    assert mode_summary.nullable is False
    _assert_server_default(mode_summary, "fast")

    turn_count = _col(threads, "turn_count")
    assert isinstance(turn_count.type, Integer) and turn_count.nullable is False
    _assert_server_default(turn_count, "0")

    _assert_tz_now(_col(threads, "created_at"))
    _assert_tz_now(_col(threads, "updated_at"))

    # 0011（data-model.md §1.2 v1.14）：pinned NOT NULL DEFAULT false；
    # read_at/archived_at 可空无默认（NULL = 未读 / 活跃，事件语义同构）
    pinned = _col(threads, "pinned")
    assert isinstance(pinned.type, Boolean) and pinned.nullable is False
    _assert_server_default(pinned, "false")

    for name in ("read_at", "archived_at"):
        col = _col(threads, name)
        assert isinstance(col.type, DateTime) and col.type.timezone is True
        assert col.nullable is True and col.server_default is None


def test_threads_mgmt_columns_add_no_index():
    """§1.2 取舍注（v1.14）：三增列零新索引——唯一既有索引仍是 idx_threads_user_updated。"""
    threads = _table("threads")
    assert {ix.name for ix in threads.indexes} == {"idx_threads_user_updated"}


def test_threads_index_user_updated_desc():
    ix = _index(_table("threads"), "idx_threads_user_updated")
    assert ix.unique is False
    exprs = [str(e) for e in ix.expressions]
    assert len(exprs) == 2
    assert exprs[0].endswith("user_id")
    # DESC 语义必须保留（会话列表按最近更新排序）
    assert "updated_at" in exprs[1] and "DESC" in exprs[1].upper()


# ---- §1.3 turns（0001 范围）----


def test_turns_columns():
    turns = _table("turns")
    assert [c.name for c in turns.columns] == [
        "id",
        "thread_id",
        "user_id",
        "mode",
        "status",
        "query",
        "query_lang",
        "rewritten_queries",
        "answer_md",
        "chat_model_key",
        "model_name",
        "parent_turn_id",
        "error",
        "timing",
        "usage",
        "langfuse_trace_id",
        "degraded_reason",
        "cancel_reason",
        "followups",
        "started_at",
        "finished_at",
        "duration_ms",
        "created_at",
    ]

    _assert_uuid_pk(_col(turns, "id"), client_default_uuid4=True)

    # thread_id：FK→threads.id，无 ondelete
    assert _col(turns, "thread_id").nullable is False
    fk_thread = _fk(turns, "thread_id")
    assert fk_thread.target_fullname == "threads.id" and fk_thread.ondelete is None

    # user_id：not null 裸列（全局约束：纳入查询过滤，无独立索引——随 DDL 契约）
    user_id = _col(turns, "user_id")
    assert user_id.nullable is False and not user_id.foreign_keys

    mode = _col(turns, "mode")
    assert isinstance(mode.type, String) and mode.type.length == 8
    assert mode.nullable is False and mode.server_default is None

    status = _col(turns, "status")
    # 0014_b22_hitl_gate 扩宽 varchar(12)→varchar(24)（§1.3 v1.23 列宽扩容注：
    # awaiting_approval=18 字符超旧列宽；varchar 扩宽为元数据级变更）
    assert isinstance(status.type, String) and status.type.length == 24
    assert status.nullable is False
    _assert_server_default(status, "pending")

    query = _col(turns, "query")
    assert isinstance(query.type, Text) and query.nullable is False

    query_lang = _col(turns, "query_lang")
    assert isinstance(query_lang.type, String) and query_lang.type.length == 4
    assert query_lang.nullable is True and query_lang.server_default is None

    rewritten = _col(turns, "rewritten_queries")
    assert isinstance(rewritten.type, JSONB) and rewritten.nullable is False
    _assert_server_default(rewritten, "'[]'")

    answer_md = _col(turns, "answer_md")
    assert isinstance(answer_md.type, Text) and answer_md.nullable is True

    for name, length in [("chat_model_key", 16), ("model_name", 64)]:
        col = _col(turns, name)
        assert isinstance(col.type, String) and col.type.length == length
        assert col.nullable is False and col.server_default is None

    parent_fk = _fk(turns, "parent_turn_id")
    assert parent_fk.target_fullname == "turns.id" and parent_fk.ondelete is None
    assert _col(turns, "parent_turn_id").nullable is True

    error = _col(turns, "error")
    assert isinstance(error.type, JSONB) and error.nullable is True

    for name in ("timing", "usage"):
        col = _col(turns, name)
        assert isinstance(col.type, JSONB) and col.nullable is False
        _assert_server_default(col, "'{}'")

    langfuse_trace_id = _col(turns, "langfuse_trace_id")
    assert isinstance(langfuse_trace_id.type, String) and langfuse_trace_id.type.length == 64
    assert langfuse_trace_id.nullable is True

    degraded_reason = _col(turns, "degraded_reason")
    assert isinstance(degraded_reason.type, String) and degraded_reason.type.length == 16
    assert degraded_reason.nullable is True  # 0002 起启用写入语义，列已在 0001

    # cancel_reason：0008（REQ-042）——user|superseded|rolled_back|timeout（v1.38
    # B22 审批超时静默收口第 4 值），仅 status=canceled 时非空（§1.3 104 行）；
    # 无默认、取消不是 error（不复用 error 列语义）；varchar(16) 无 CHECK 零 DDL
    cancel_reason = _col(turns, "cancel_reason")
    assert isinstance(cancel_reason.type, String) and cancel_reason.type.length == 16
    assert cancel_reason.nullable is True and cancel_reason.server_default is None

    for name in ("started_at", "finished_at"):
        col = _col(turns, name)
        assert isinstance(col.type, DateTime) and col.type.timezone is True
        assert col.nullable is True and col.server_default is None

    duration_ms = _col(turns, "duration_ms")
    assert isinstance(duration_ms.type, Integer) and duration_ms.nullable is True

    _assert_tz_now(_col(turns, "created_at"))


def test_turns_excludes_later_migration_columns():
    """0005/0006 的增列不得提前出现（防 autogenerate 过度生成）；cancel_reason 已随
    0008（REQ-042）、followups 已随 0009（REQ-046）落地，不再属护栏清单。"""
    turns = _table("turns")
    for name in ("incognito", "memory_hits"):
        assert name not in turns.columns, f"{name} 属后续迁移增列，不应提前包含"

    # 0009（REQ-046）：followups jsonb nullable 无默认无索引（§1.3 followups 行）
    followups = _col(turns, "followups")
    assert isinstance(followups.type, JSONB)
    assert followups.nullable is True and followups.server_default is None
    assert not any(
        "followups" in [str(c) for c in index.columns] for index in turns.indexes
    )


def test_turns_indexes():
    turns = _table("turns")

    ix_thread_created = _index(turns, "idx_turns_thread_created")
    assert ix_thread_created.unique is False
    assert [str(e).split(".")[-1] for e in ix_thread_created.expressions] == [
        "thread_id",
        "created_at",
    ]

    # 部分索引（0008 形态，REQ-042）：条件含 queued——enqueue 排队轮同入 409001
    # 裁决与孤儿轮清理口径（§1.3 146 行；drop+recreate 属 0008）
    ix_running = _index(turns, "idx_turns_running")
    assert ix_running.unique is False
    where = str(ix_running.dialect_options["postgresql"]["where"])
    assert "pending" in where and "running" in where and "queued" in where


# ---- §1.4 turn_events ----


def test_turn_events_columns():
    events = _table("turn_events")
    assert [c.name for c in events.columns] == [
        "id",
        "thread_id",
        "turn_id",
        "seq",
        "event_type",
        "payload",
        "created_at",
    ]

    # id：bigint GENERATED ALWAYS AS IDENTITY 主键（非 uuid——五表唯一例外；
    # Identity 在 SQLAlchemy 中即服务端默认，DB 端生成、应用不赋值）
    id_col = _col(events, "id")
    assert isinstance(id_col.type, BigInteger) and id_col.primary_key
    assert id_col.identity is not None and id_col.identity.always is True
    assert id_col.default is None and id_col.server_default is id_col.identity

    thread_id = _col(events, "thread_id")
    assert thread_id.nullable is False and not thread_id.foreign_keys

    fk_turn = _fk(events, "turn_id")
    assert fk_turn.target_fullname == "turns.id" and fk_turn.ondelete == "CASCADE"
    assert _col(events, "turn_id").nullable is False

    seq = _col(events, "seq")
    assert isinstance(seq.type, Integer) and seq.nullable is False

    event_type = _col(events, "event_type")
    # 0014_b22_hitl_gate 扩宽 varchar(12)→varchar(24)（§1.4 v1.23 列宽扩容注：
    # approval_requested/approval_resolved=18 字符超旧列宽）
    assert isinstance(event_type.type, String) and event_type.type.length == 24
    assert event_type.nullable is False

    payload = _col(events, "payload")
    assert isinstance(payload.type, JSONB) and payload.nullable is False

    _assert_tz_now(_col(events, "created_at"))


def test_turn_events_indexes():
    events = _table("turn_events")

    uq_thread_seq = _index(events, "uq_turn_events_thread_seq")
    assert uq_thread_seq.unique is True
    assert [str(e).split(".")[-1] for e in uq_thread_seq.expressions] == ["thread_id", "seq"]

    ix_turn = _index(events, "idx_turn_events_turn")
    assert ix_turn.unique is False
    assert [str(e).split(".")[-1] for e in ix_turn.expressions] == ["turn_id", "seq"]


# ---- §1.5 sources（M1 基础列 + 0012/m2_deep 增 snapshot_id + 0013/m3_kb 增 kb 两列）----


def test_sources_columns():
    sources = _table("sources")
    assert [c.name for c in sources.columns] == [
        "id",
        "turn_id",
        "user_id",
        "ref_no",
        "source_type",
        "title",
        "url",
        "snippet",
        "score",
        "snapshot_id",  # M2 增列（0012）
        "kb_doc_id",  # M3 增列（0013）
        "kb_chunk_ids",  # M3 增列（0013）
        "created_at",
    ]

    _assert_uuid_pk(_col(sources, "id"), client_default_uuid4=True)

    fk_turn = _fk(sources, "turn_id")
    assert fk_turn.target_fullname == "turns.id" and fk_turn.ondelete == "CASCADE"
    assert _col(sources, "turn_id").nullable is False

    user_id = _col(sources, "user_id")
    assert user_id.nullable is False and not user_id.foreign_keys

    ref_no = _col(sources, "ref_no")
    assert isinstance(ref_no.type, SmallInteger) and ref_no.nullable is False

    source_type = _col(sources, "source_type")
    assert isinstance(source_type.type, String) and source_type.type.length == 8
    assert source_type.nullable is False

    title = _col(sources, "title")
    assert isinstance(title.type, String) and title.type.length == 512
    assert title.nullable is False

    for name in ("url", "snippet"):
        col = _col(sources, name)
        assert isinstance(col.type, Text) and col.nullable is True

    score = _col(sources, "score")
    assert isinstance(score.type, Numeric) and score.type.precision == 6
    assert score.type.scale == 4 and score.nullable is True

    _assert_tz_now(_col(sources, "created_at"))


def test_sources_kb_columns():
    """kb_doc_id/kb_chunk_ids（§1.5 M3 增列，0013/m3_kb）：uuid/jsonb 均可空。

    契约 SQL 原文「ALTER TABLE sources ADD COLUMN kb_doc_id uuid, ADD COLUMN
    kb_chunk_ids jsonb」——两列**均不带 FK**（对比 snapshot_id 显式 REFERENCES，
    kb_doc_id 表格里仅注明「指向 kb_documents.id」无约束列标注；删除级联由
    kb_documents 侧 CASCADE 链与 service 层清理承担，零私改）。
    """
    sources = _table("sources")
    kb_doc_id = _col(sources, "kb_doc_id")
    assert kb_doc_id.nullable is True
    assert not kb_doc_id.foreign_keys, "kb_doc_id 无 FK（契约 SQL 原文无 REFERENCES）"
    kb_chunk_ids = _col(sources, "kb_chunk_ids")
    assert isinstance(kb_chunk_ids.type, JSONB) and kb_chunk_ids.nullable is True
    assert kb_chunk_ids.server_default is None


def test_sources_snapshot_id_column():
    """snapshot_id（§1.5 M2 增列，0012）：uuid 可空、FK→snapshots.id、无级联动作。

    契约 SQL「ALTER TABLE sources ADD COLUMN snapshot_id uuid REFERENCES
    snapshots(id)」未带 ON DELETE——保持默认 NO ACTION，零私改；行级清理沿
    线程删除链（先删 turns → sources/snapshots 随 CASCADE 清空，§1.2 v1.14 注）。
    """
    sources = _table("sources")
    col = _col(sources, "snapshot_id")
    assert col.nullable is True
    fk = _fk(sources, "snapshot_id")
    assert fk.target_fullname == "snapshots.id"
    assert fk.ondelete is None


# ---- §1.6 snapshots（M2，0012；仅深度模式，存档不参与检索）----


def test_snapshots_columns():
    """snapshots 逐列 DDL（§1.6 契约 SQL 原文）：uuid PK（应用侧 uuid4）/
    user_id·thread_id 裸 uuid 无 FK / turn_id FK CASCADE / url_hash char(64) /
    content_type 默认 'text/markdown' / fetched_at 无服务端默认（写入方定值）。"""
    snapshots = _table("snapshots")
    assert [c.name for c in snapshots.columns] == [
        "id",
        "user_id",
        "thread_id",
        "turn_id",
        "url",
        "url_hash",
        "object_key",
        "content_type",
        "size_bytes",
        "fetched_at",
        "created_at",
    ]

    _assert_uuid_pk(_col(snapshots, "id"), client_default_uuid4=True)

    # user_id/thread_id：裸 uuid（无 FK，随 DDL 契约；对象键含 user_id 前缀 REQ-011）
    for name in ("user_id", "thread_id"):
        col = _col(snapshots, name)
        assert col.nullable is False and not col.foreign_keys

    fk_turn = _fk(snapshots, "turn_id")
    assert fk_turn.target_fullname == "turns.id" and fk_turn.ondelete == "CASCADE"
    assert _col(snapshots, "turn_id").nullable is False

    assert isinstance(_col(snapshots, "url").type, Text)
    assert _col(snapshots, "url").nullable is False

    url_hash = _col(snapshots, "url_hash")
    assert isinstance(url_hash.type, CHAR) and url_hash.type.length == 64
    assert url_hash.nullable is False  # sha256(url)（唯一键用，URL 可能超长）

    assert isinstance(_col(snapshots, "object_key").type, Text)
    assert _col(snapshots, "object_key").nullable is False

    content_type = _col(snapshots, "content_type")
    assert isinstance(content_type.type, String) and content_type.type.length == 64
    assert content_type.nullable is False
    _assert_server_default(content_type, "text/markdown")

    size_bytes = _col(snapshots, "size_bytes")
    assert isinstance(size_bytes.type, BigInteger) and size_bytes.nullable is False

    fetched_at = _col(snapshots, "fetched_at")
    assert isinstance(fetched_at.type, DateTime) and fetched_at.type.timezone is True
    assert fetched_at.nullable is False and fetched_at.server_default is None

    _assert_tz_now(_col(snapshots, "created_at"))


def test_snapshots_indexes():
    """唯一键 uq_snapshots_turn_url (turn_id, url_hash) + idx_snapshots_thread。"""
    snapshots = _table("snapshots")
    uq = _index(snapshots, "uq_snapshots_turn_url")
    assert uq.unique is True
    assert [str(e).split(".")[-1] for e in uq.expressions] == ["turn_id", "url_hash"]

    idx = _index(snapshots, "idx_snapshots_thread")
    assert idx.unique is False
    assert [str(e).split(".")[-1] for e in idx.expressions] == ["thread_id"]


# ---- §1.9 artifacts（M2 建表先承载 report；M4 扩 code_output）----


def test_artifacts_columns():
    """artifacts 逐列 DDL（§1.9 契约 SQL 原文）：thread_id FK CASCADE（产物按线程
    组织 REQ-024）/ turn_id 可空 FK 无动作（report 导出关联轮次）。"""
    artifacts = _table("artifacts")
    assert [c.name for c in artifacts.columns] == [
        "id",
        "user_id",
        "thread_id",
        "turn_id",
        "artifact_type",
        "filename",
        "rel_path",
        "size_bytes",
        "content_type",
        "created_at",
    ]

    _assert_uuid_pk(_col(artifacts, "id"), client_default_uuid4=True)

    user_id = _col(artifacts, "user_id")
    assert user_id.nullable is False and not user_id.foreign_keys

    fk_thread = _fk(artifacts, "thread_id")
    assert fk_thread.target_fullname == "threads.id" and fk_thread.ondelete == "CASCADE"
    assert _col(artifacts, "thread_id").nullable is False

    fk_turn = _fk(artifacts, "turn_id")
    assert fk_turn.target_fullname == "turns.id" and fk_turn.ondelete is None
    assert _col(artifacts, "turn_id").nullable is True

    artifact_type = _col(artifacts, "artifact_type")
    assert isinstance(artifact_type.type, String) and artifact_type.type.length == 16
    assert artifact_type.nullable is False  # report | code_output（M2 先承载 report）

    filename = _col(artifacts, "filename")
    assert isinstance(filename.type, String) and filename.type.length == 256
    assert filename.nullable is False

    assert isinstance(_col(artifacts, "rel_path").type, Text)
    assert _col(artifacts, "rel_path").nullable is False

    size_bytes = _col(artifacts, "size_bytes")
    assert isinstance(size_bytes.type, BigInteger) and size_bytes.nullable is False

    content_type = _col(artifacts, "content_type")
    assert isinstance(content_type.type, String) and content_type.type.length == 64
    assert content_type.nullable is False and content_type.server_default is None

    _assert_tz_now(_col(artifacts, "created_at"))


def test_artifacts_thread_index_desc():
    """idx_artifacts_thread (thread_id, created_at DESC)——线程产物列表按新在前。

    DESC 为 TextClause 表达式（str 原样保留「created_at DESC」），与迁移侧
    sa.literal_column 渲染一致（idx_threads_user_updated 同款先例）。
    """
    idx = _index(_table("artifacts"), "idx_artifacts_thread")
    assert idx.unique is False
    assert [str(e).split(".")[-1] for e in idx.expressions] == [
        "thread_id",
        "created_at DESC",
    ]


def test_sources_unique_index_turn_ref():
    uq = _index(_table("sources"), "uq_sources_turn_ref")
    assert uq.unique is True
    assert [str(e).split(".")[-1] for e in uq.expressions] == ["turn_id", "ref_no"]


def test_uuid_pk_client_default_is_v4():
    """应用侧生成的主键确为 UUID v4（全局约束）。"""
    for table_name in ("threads", "turns", "sources"):
        _assert_uuid_pk(_table(table_name).columns["id"], client_default_uuid4=True)


def test_partial_index_uses_text_clause():
    """部分索引 WHERE 条件以 TextClause 表达（DDL 原文，autogenerate 原样渲染）。"""
    ix_running = _index(_table("turns"), "idx_turns_running")
    where = ix_running.dialect_options["postgresql"]["where"]
    assert isinstance(where, type(text("")))


# ---- §1.7 kb_documents / §1.8 kb_chunks / §1.17 kb_import_jobs（M3，0013/m3_kb）----


def test_kb_documents_columns():
    """kb_documents 逐列 DDL（§1.7 契约 SQL 原文 :282-298 + :301 M3 增列两列）。

    status varchar(12) 六态无 CHECK（文档性扩展零 DDL，api.md §4.3 v1.30 镜像）；
    content_hash char(64) 可空（批次 2 chunking 段写入 sha256）；parse_meta
    not null default '{}'（存储空对象 {} ↔ REST 投影 null 的映射在 service 层——
    api.md v1.31 口径）；catalog_ids/catalog_path 可空无默认（本地上传/URL 恒
    null，OQ-26 ⑦；飞书目录链 root→leaf）。updated_at 另带 ORM onupdate=now()
    （非 DDL 面——评审 P3：B17 worker 改 status 免手工刷新，防 Library 投影
    updated_at 恒停在建行时刻的静默陈旧）。
    """
    docs = _table("kb_documents")
    assert [c.name for c in docs.columns] == [
        "id",
        "user_id",
        "doc_type",
        "title",
        "source_url",
        "file_object_key",
        "file_size_bytes",
        "content_hash",
        "status",
        "fail_reason",
        "chunk_count",
        "parse_meta",
        "created_at",
        "updated_at",
        "ready_at",
        "catalog_ids",
        "catalog_path",
    ]

    _assert_uuid_pk(_col(docs, "id"), client_default_uuid4=True)

    user_id = _col(docs, "user_id")
    assert user_id.nullable is False and not user_id.foreign_keys

    doc_type = _col(docs, "doc_type")
    assert isinstance(doc_type.type, String) and doc_type.type.length == 8
    assert doc_type.nullable is False  # pdf|md|txt|docx|url

    title = _col(docs, "title")
    assert isinstance(title.type, String) and title.type.length == 512
    assert title.nullable is False

    for name in ("source_url", "file_object_key"):
        col = _col(docs, name)
        assert isinstance(col.type, Text) and col.nullable is True

    size = _col(docs, "file_size_bytes")
    assert isinstance(size.type, BigInteger) and size.nullable is True

    content_hash = _col(docs, "content_hash")
    assert isinstance(content_hash.type, CHAR) and content_hash.type.length == 64
    assert content_hash.nullable is True

    status = _col(docs, "status")
    assert isinstance(status.type, String) and status.type.length == 12
    assert status.nullable is False
    _assert_server_default(status, "'pending'")

    fail_reason = _col(docs, "fail_reason")
    assert isinstance(fail_reason.type, Text) and fail_reason.nullable is True

    chunk_count = _col(docs, "chunk_count")
    assert isinstance(chunk_count.type, Integer) and chunk_count.nullable is False
    _assert_server_default(chunk_count, "0")

    parse_meta = _col(docs, "parse_meta")
    assert isinstance(parse_meta.type, JSONB) and parse_meta.nullable is False
    _assert_server_default(parse_meta, "'{}'")

    _assert_tz_now(_col(docs, "created_at"))
    docs_updated_at = _col(docs, "updated_at")
    _assert_tz_now(docs_updated_at)
    assert docs_updated_at.onupdate is not None  # ORM onupdate=now()（评审 P3——status 变更免手工刷新，防投影面静默陈旧；零 DDL）

    ready_at = _col(docs, "ready_at")
    assert isinstance(ready_at.type, DateTime) and ready_at.type.timezone is True
    assert ready_at.nullable is True and ready_at.server_default is None

    catalog_ids = _col(docs, "catalog_ids")
    assert isinstance(catalog_ids.type, JSONB) and catalog_ids.nullable is True
    assert catalog_ids.server_default is None

    catalog_path = _col(docs, "catalog_path")
    assert isinstance(catalog_path.type, Text) and catalog_path.nullable is True
    assert catalog_path.server_default is None


def test_kb_documents_indexes_and_no_fk():
    """两索引（§1.7 DDL 原文）：user 过滤 + 新在前（DESC）+ 状态过滤；全表零 FK。

    零 FK：契约 DDL 原文无 REFERENCES（user_id 裸 uuid）；级联删除走 service
    层编排（REQ-022 三存储级联），不经 db FK 链。
    """
    docs = _table("kb_documents")
    assert all(not c.foreign_keys for c in docs.columns)

    idx_created = _index(docs, "idx_kb_docs_user_created")
    assert idx_created.unique is False
    assert [str(e).split(".")[-1] for e in idx_created.expressions] == [
        "user_id",
        "created_at DESC",
    ]

    idx_status = _index(docs, "idx_kb_docs_user_status")
    assert idx_status.unique is False
    assert [str(e).split(".")[-1] for e in idx_status.expressions] == [
        "user_id",
        "status",
    ]


def test_kb_chunks_columns():
    """kb_chunks 逐列 DDL（§1.8 契约 SQL 原文 :321-330）。

    embedding vector(1024) NOT NULL（text-embedding-v4 维度=KB_EMBEDDING_DIM，
    一期 1024 定死——§4 回滚注）；meta jsonb not null default '{}'（七键
    {node_id, heading_path, loc, references, raw_type, char_start, char_end}
    为 jsonb 形状约定、零 DDL——v1.19 REQ-053 扩容）。
    """
    chunks = _table("kb_chunks")
    assert [c.name for c in chunks.columns] == [
        "id",
        "kb_doc_id",
        "user_id",
        "chunk_index",
        "content",
        "token_count",
        "embedding",
        "meta",
        "created_at",
    ]

    _assert_uuid_pk(_col(chunks, "id"), client_default_uuid4=True)

    fk_doc = _fk(chunks, "kb_doc_id")
    assert fk_doc.target_fullname == "kb_documents.id" and fk_doc.ondelete == "CASCADE"
    assert _col(chunks, "kb_doc_id").nullable is False

    user_id = _col(chunks, "user_id")
    assert user_id.nullable is False and not user_id.foreign_keys  # 检索过滤维度

    chunk_index = _col(chunks, "chunk_index")
    assert isinstance(chunk_index.type, Integer) and chunk_index.nullable is False

    content = _col(chunks, "content")
    assert isinstance(content.type, Text) and content.nullable is False

    token_count = _col(chunks, "token_count")
    assert isinstance(token_count.type, Integer) and token_count.nullable is False

    embedding = _col(chunks, "embedding")
    assert isinstance(embedding.type, Vector) and embedding.type.dim == 1024
    assert embedding.nullable is False

    meta = _col(chunks, "meta")
    assert isinstance(meta.type, JSONB) and meta.nullable is False
    _assert_server_default(meta, "'{}'")

    _assert_tz_now(_col(chunks, "created_at"))


def test_kb_chunks_indexes_hnsw():
    """唯一键 (kb_doc_id, chunk_index) + user 索引 + HNSW cosine（§1.8 DDL 原文）。

    HNSW 参数 m=16/ef_construction=64（§3 决策：出厂即用无训练步；cosine 与
    rerank 语义一致；查询期 ef_search=40 由检索语句 SET LOCAL 承担，非索引面）。
    """
    chunks = _table("kb_chunks")

    uq = _index(chunks, "uq_kb_chunks_doc_idx")
    assert uq.unique is True
    assert [str(e).split(".")[-1] for e in uq.expressions] == [
        "kb_doc_id",
        "chunk_index",
    ]

    idx_user = _index(chunks, "idx_kb_chunks_user")
    assert idx_user.unique is False
    assert [str(e).split(".")[-1] for e in idx_user.expressions] == ["user_id"]

    hnsw = _index(chunks, "idx_kb_chunks_hnsw")
    assert hnsw.unique is False
    pg_opts = hnsw.dialect_options["postgresql"]
    assert pg_opts["using"] == "hnsw"
    assert pg_opts["ops"] == {"embedding": "vector_cosine_ops"}
    assert pg_opts["with"] == {"m": 16, "ef_construction": 64}
    assert [str(e).split(".")[-1] for e in hnsw.expressions] == ["embedding"]


def test_kb_import_jobs_columns():
    """kb_import_jobs 逐列 DDL（§1.17 基线形态 :529-537，0013 建表）。

    status 四态 default 'pending'（不设 canceled 独立终态——api.md §4.7~4.9
    镜像）；progress 五数 {total, processed, imported, failed, skipped} 为
    jsonb 形状约定；failures default '[]'（仅详情端点投影，§4.8 口径）；
    started_at/finished_at 可空无默认（null=进行中）。updated_at 另带 ORM
    onupdate=now()（非 DDL 面——评审 P3：worker 推进 status/cursor/progress
    免手工刷新，同 kb_documents.updated_at 口径）。
    """
    jobs = _table("kb_import_jobs")
    assert [c.name for c in jobs.columns] == [
        "id",
        "user_id",
        "connector",
        "trigger",
        "status",
        "cursor",
        "progress",
        "failures",
        "stats",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    ]

    _assert_uuid_pk(_col(jobs, "id"), client_default_uuid4=True)

    user_id = _col(jobs, "user_id")
    assert user_id.nullable is False and not user_id.foreign_keys

    connector = _col(jobs, "connector")
    assert isinstance(connector.type, String) and connector.type.length == 16
    assert connector.nullable is False
    _assert_server_default(connector, "'feishu'")

    trigger = _col(jobs, "trigger")
    assert isinstance(trigger.type, String) and trigger.type.length == 16
    assert trigger.nullable is False and trigger.server_default is None  # manual|scheduled

    status = _col(jobs, "status")
    assert isinstance(status.type, String) and status.type.length == 12
    assert status.nullable is False
    _assert_server_default(status, "'pending'")

    cursor = _col(jobs, "cursor")
    assert isinstance(cursor.type, JSONB) and cursor.nullable is True
    assert cursor.server_default is None  # 断点游标，未推进=null

    progress = _col(jobs, "progress")
    assert isinstance(progress.type, JSONB) and progress.nullable is False
    _assert_server_default(progress, "'{}'")

    failures = _col(jobs, "failures")
    assert isinstance(failures.type, JSONB) and failures.nullable is False
    _assert_server_default(failures, "'[]'")

    stats = _col(jobs, "stats")
    assert isinstance(stats.type, JSONB) and stats.nullable is True

    for name in ("started_at", "finished_at"):
        col = _col(jobs, name)
        assert isinstance(col.type, DateTime) and col.type.timezone is True
        assert col.nullable is True and col.server_default is None

    _assert_tz_now(_col(jobs, "created_at"))
    jobs_updated_at = _col(jobs, "updated_at")
    _assert_tz_now(jobs_updated_at)
    assert jobs_updated_at.onupdate is not None  # ORM onupdate=now()（评审 P3——worker 推进状态免手工刷新；零 DDL）


def test_kb_import_jobs_has_no_indexes():
    """§1.17 索引取舍：自用单人、串行 worker、量级 ≤ 百级——零索引（唯一键外
    需求也无；顺查 (user_id, created_at) 足够，批次 5 实装复核注保留）。"""
    assert _table("kb_import_jobs").indexes == set()


def test_kb_tables_have_no_check_constraints():
    """enum 一律 varchar 无 CHECK（§4 迁移规则；六态/四态扩容=文档性扩展零 DDL）。"""
    for name in ("kb_documents", "kb_chunks", "kb_import_jobs"):
        checks = [
            c
            for c in _table(name).constraints
            if isinstance(c, CheckConstraint)
        ]
        assert checks == [], f"{name} 不应有 CHECK 约束（enum 一律 varchar）"


def test_kb_status_literals_match_contract():
    """六态/四态常量逐字（唯一定义处 = app/db/models/kb.py）。

    KbDocStatus 六态镜像 api.md §4.3 v1.30（旧 parsing/embedding 作废；pending +
    normalizing/chunking/indexing 为 data-model §4 启动钩子 IN 列表的取值子集）；
    KbJobStatus 四态镜像 api.md §4.7~4.9（不设 canceled 独立终态）。
    """
    assert get_args(KbDocStatus) == (
        "pending",
        "normalizing",
        "chunking",
        "indexing",
        "ready",
        "failed",
    )
    assert get_args(KbJobStatus) == (
        "pending",
        "running",
        "completed",
        "failed",
    )


def test_uuid_pk_client_default_is_v4_kb_tables():
    """kb 三表主键亦为应用侧 uuid4（§1.17 注「ORM 客户端 uuid4 承接，全局口径」）。"""
    for table_name in ("kb_documents", "kb_chunks", "kb_import_jobs"):
        _assert_uuid_pk(_table(table_name).columns["id"], client_default_uuid4=True)


# ---- §1.20 turn_approvals（B22 HITL 审批门，v1.23 增，0014）----


def test_turn_approvals_columns():
    """§1.20 字段级 DDL 对齐：13 列序/类型/可空/server_default 逐列断言。"""
    approvals = _table("turn_approvals")
    assert [c.name for c in approvals.columns] == [
        "id",
        "turn_id",
        "status",
        "verdict",
        "hits_summary",
        "previews",
        "gate_payload",
        "choice",
        "note",
        "decided_by",
        "requested_at",
        "expires_at",
        "responded_at",
    ]

    _assert_uuid_pk(_col(approvals, "id"), client_default_uuid4=True)

    # turn_id：FK→turns.id ON DELETE CASCADE（线程删除级联 REQ-022 面随轮走）、
    # not null；UNIQUE(turn_id) 一轮一行由唯一索引承载（非列级约束，§1.16 先例）
    fk_turn = _fk(approvals, "turn_id")
    assert fk_turn.target_fullname == "turns.id" and fk_turn.ondelete == "CASCADE"
    assert _col(approvals, "turn_id").nullable is False

    status = _col(approvals, "status")
    assert isinstance(status.type, String) and status.type.length == 12
    assert status.nullable is False
    _assert_server_default(status, "'pending'")

    verdict = _col(approvals, "verdict")
    assert isinstance(verdict.type, String) and verdict.type.length == 12
    assert verdict.nullable is False and verdict.server_default is None

    for jsonb_col in ("hits_summary", "previews"):
        col = _col(approvals, jsonb_col)
        assert isinstance(col.type, JSONB) and col.nullable is False
        _assert_server_default(col, "'[]'")

    gate_payload = _col(approvals, "gate_payload")
    assert isinstance(gate_payload.type, JSONB) and gate_payload.nullable is True
    assert gate_payload.server_default is None  # no_hit 轮=null（载荷事实源）

    choice = _col(approvals, "choice")
    assert isinstance(choice.type, String) and choice.type.length == 12
    assert choice.nullable is True and choice.server_default is None

    note = _col(approvals, "note")
    assert isinstance(note.type, Text) and note.nullable is True

    decided_by = _col(approvals, "decided_by")
    assert isinstance(decided_by.type, String) and decided_by.type.length == 8
    assert decided_by.nullable is True and decided_by.server_default is None

    requested_at = _col(approvals, "requested_at")
    assert isinstance(requested_at.type, DateTime) and requested_at.type.timezone is True
    assert requested_at.nullable is False
    _assert_server_default(requested_at, "now()")

    for plain_tz_col in ("expires_at", "responded_at"):
        col = _col(approvals, plain_tz_col)
        assert isinstance(col.type, DateTime) and col.type.timezone is True
        assert col.server_default is None
    assert _col(approvals, "expires_at").nullable is False
    assert _col(approvals, "responded_at").nullable is True


def test_turn_approvals_indexes():
    """§1.20 索引面：UNIQUE(turn_id) + (status, expires_at) 部分索引 WHERE pending；
    除二者外零索引（§1.20 取舍注——无历史/列表查询需求）。"""
    approvals = _table("turn_approvals")

    uq_turn = _index(approvals, "uq_turn_approvals_turn")
    assert uq_turn.unique is True
    assert [str(e).split(".")[-1] for e in uq_turn.expressions] == ["turn_id"]

    ix_expiry = _index(approvals, "idx_turn_approvals_expiry")
    assert ix_expiry.unique is False
    assert [str(e).split(".")[-1] for e in ix_expiry.expressions] == [
        "status",
        "expires_at",
    ]
    where_sql = str(ix_expiry.dialect_options["postgresql"]["where"])
    assert "pending" in where_sql, "部分索引 WHERE 条件应限定 status='pending'"

    # 除 UNIQUE 与超时索引外零索引（§1.20 取舍注）
    assert len(approvals.indexes) == 2, (
        f"turn_approvals 应恰两索引（uq+超时部分索引），得到 {len(approvals.indexes)}"
    )
