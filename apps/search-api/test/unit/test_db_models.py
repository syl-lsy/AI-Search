"""unit 层：五表 ORM metadata 对照 data-model.md §1.1~§1.5 DDL 契约（零网络）。

models/ 未落地或列漂移时本文件先红（TDD 红绿循环的契约锚点）；真库行为
（迁移链 upgrade/downgrade/seed/扩展）由 integration 层 test_migrations.py 兜住。
断言粒度 = DDL 逐条：列/类型/可空/服务端默认/主键/FK（含 ON DELETE CASCADE）/
索引（唯一/DESC/部分索引 WHERE）/identity。

0001 范围护栏：后续迁移的增列（turns.incognito/memory_hits/followups、
sources.snapshot_id/kb_doc_id/kb_chunk_ids）在 0001 不得提前出现（turns.cancel_reason
已随 0008 落地，移出护栏、纳入 §1.3 逐列断言）。
"""
import uuid

import pytest
from sqlalchemy import (
    BigInteger,
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


def test_metadata_contains_exactly_five_core_tables():
    assert set(Base.metadata.tables) == {"users", "threads", "turns", "turn_events", "sources"}


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
    assert isinstance(status.type, String) and status.type.length == 12
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

    # cancel_reason：0008（REQ-042）——user|superseded|rolled_back，仅 status=canceled
    # 时非空（§1.3 104 行）；无默认、取消不是 error（不复用 error 列语义）
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
    """0005/0006/0009 的增列不得提前出现（防 autogenerate 过度生成）；
    cancel_reason 已随 0008（REQ-042）落地，不再属护栏清单。"""
    turns = _table("turns")
    for name in ("incognito", "memory_hits", "followups"):
        assert name not in turns.columns, f"{name} 属后续迁移增列，不应提前包含"


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
    assert isinstance(event_type.type, String) and event_type.type.length == 12
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


# ---- §1.5 sources（M1 基础列）----


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


def test_sources_excludes_later_migration_columns():
    """snapshot_id（M2）/kb_doc_id/kb_chunk_ids（M3）属后续迁移增列。"""
    sources = _table("sources")
    for name in ("snapshot_id", "kb_doc_id", "kb_chunk_ids"):
        assert name not in sources.columns, f"{name} 属后续迁移增列，0001 不应包含"


def test_sources_unique_index_turn_ref():
    uq = _index(_table("sources"), "uq_sources_turn_ref")
    assert uq.unique is True
    assert [str(e).split(".")[-1] for e in uq.expressions] == ["turn_id", "ref_no"]


def test_uuid_pk_client_default_is_v4():
    """应用侧生成的主键确为 UUID v4（全局约束）。"""
    for table_name in ("threads", "turns", "sources"):
        col = _table(table_name).columns["id"]
        assert col.default is not None and col.default.is_callable
        value = col.default.arg(None)
        assert isinstance(value, uuid.UUID) and value.version == 4


def test_partial_index_uses_text_clause():
    """部分索引 WHERE 条件以 TextClause 表达（DDL 原文，autogenerate 原样渲染）。"""
    ix_running = _index(_table("turns"), "idx_turns_running")
    where = ix_running.dialect_options["postgresql"]["where"]
    assert isinstance(where, type(text("")))
