"""integration 层：迁移链（0001_m1_core → 0008_m1_turn_scheduler → 0009_m1_turn_followups
→ 0010_m2_turn_feedback → 0011_m2_thread_mgmt → 0012_m2_deep）真库验证（requires_db；
跑在 .env.test 测试库）。

链路：upgrade head → 断言核心表 + alembic_version=head(0012) + vector 扩展；downgrade
base → 断言表与 alembic_version 清除、vector 扩展保留（0001 契约：downgrade
不删 extension）；再 upgrade head → 全链可重复；末尾 `alembic check` 零漂移
（迁移产物 == ORM metadata，即 autogenerate 闭环的运行时验证）。

0008（REQ-042）专段：turns.cancel_reason 增列 + idx_turns_running 部分索引条件
重建（扩 queued）的 upgrade→downgrade→upgrade 三段逐段断言（列存在性/类型/可空/
无默认 + 索引 WHERE 条件按 pg_indexes 实文核对，PG 对 IN 有规范化改写，断言取
字面值出现性而非完整 DDL 文本）。

0009/0010（REQ-046/REQ-057，v1.9/v1.13 契约）专段：turns.followups jsonb 增列 +
turn_feedback 表（UNIQUE(turn_id) 索引、FK CASCADE）的 upgrade→downgrade→upgrade
逐段断言。

0011（v1.14 契约，REQ-058~060，B10）专段：threads 三增列（pinned NOT NULL DEFAULT
false / read_at / archived_at）+ **存量行 read_at 回填 created_at** 的
upgrade→downgrade→upgrade 逐段断言（回填必须在 0010 态的存量行上验证——先降级造行
再升级）。零新索引断言（§1.2 v1.14 取舍注）。

0012（M2 deep 底座：契约逻辑名 0002_m2_deep → 实际编号 0012——迁移链实际为
0001→0008→…→0011，与 §4 迁移表逻辑名编号错位，交付说明留映射）专段：
snapshots 表（§1.6：url_hash char(64)、唯一键 uq_snapshots_turn_url (turn_id,url_hash)、
idx_snapshots_thread、turn_id FK CASCADE、content_type 默认 'text/markdown'、
fetched_at 无服务端默认）+ artifacts 表（§1.9：thread_id FK CASCADE / turn_id 可空
FK 无动作 / idx_artifacts_thread (thread_id, created_at DESC)）+ sources.snapshot_id
增列（§1.5 M2：可空 FK→snapshots.id 无动作）的 upgrade→downgrade→upgrade 逐段断言
（列事实 information_schema / 索引 pg_indexes / FK 级联 pg_constraint.confdeltype：
'c'=CASCADE 'a'=NO ACTION）。

默认用户 seed 断言（0001 已落地——2026-11-07 评审解锁，经 app/db/seed.py 的 ORM
形态，见迁移文件 docstring）：upgrade 后 users 恰一行且 id/name == 配置值；
链路用例末段补 seed 在全新 users 表上的再建断言（downgrade→upgrade 循环幂等）。

alembic 经 conftest 的 alembic fixture 子进程运行（.env.test DB_ 变量已注入环境；
密码脱敏后进断言信息）。缺 .env.test/测试库不可达时整模块 skip（conftest 口径）。
"""

import uuid

import pytest
from psycopg2 import connect  # 断言面连接（conftest 管理面已建库）

from app.core.config import common_settings

pytestmark = pytest.mark.requires_db

#: 测试库应有的 public 表全集（M1 五表 + §1.16 turn_feedback + §1.6/§1.9 M2 deep 底座）
CORE_TABLES = (
    "users",
    "threads",
    "turns",
    "turn_events",
    "sources",
    "turn_feedback",
    "snapshots",
    "artifacts",
)

#: 链尾 revision（data-model.md §4 迁移表：0011 thread_mgmt → 0012 m2_deep）
CHAIN_HEAD = "0012"


def _connect(info):
    return connect(
        host=info.host,
        port=int(info.port),
        user=info.user,
        password=info.password,
        dbname=info.name,
        connect_timeout=5,
    )


def _public_tables(cursor) -> set[str]:
    cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    return {row[0] for row in cursor.fetchall()}


def _extensions(cursor) -> set[str]:
    cursor.execute("SELECT extname FROM pg_extension")
    return {row[0] for row in cursor.fetchall()}


def _turns_cancel_reason(cursor) -> list[tuple]:
    """cancel_reason 列事实（data_type/长度/可空/默认）；information_schema 参数化查询。"""
    cursor.execute(
        "SELECT data_type, character_maximum_length, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
        ("turns", "cancel_reason"),
    )
    return cursor.fetchall()


def _turns_running_indexdef(cursor) -> str:
    """idx_turns_running 实际 DDL（pg_get_indexdef 口径；PG 规范化后的 WHERE 文本）。"""
    cursor.execute(
        "SELECT indexdef FROM pg_indexes "
        "WHERE schemaname = 'public' AND indexname = %s",
        ("idx_turns_running",),
    )
    rows = cursor.fetchall()
    assert len(rows) == 1, f"idx_turns_running 应恰一个，得到 {len(rows)}"
    return rows[0][0]


def test_chain_upgrade_downgrade_upgrade(alembic, test_db, db_truncate):
    """upgrade → downgrade base → 再 upgrade 全成功；seed/扩展/版本表逐段断言。"""
    # ── upgrade head ──
    alembic(["upgrade", "head"])
    with _connect(test_db) as conn, conn.cursor() as cursor:
        tables = _public_tables(cursor)
        assert set(CORE_TABLES) <= tables
        assert "alembic_version" in tables
        assert "vector" in _extensions(cursor)

        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchall() == [(CHAIN_HEAD,)]

    # ── downgrade base ──
    alembic(["downgrade", "base"])
    with _connect(test_db) as conn, conn.cursor() as cursor:
        tables = _public_tables(cursor)
        assert set(CORE_TABLES).isdisjoint(tables)
        # alembic 固有行为：downgrade base 清空版本行、不删 alembic_version 表
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchall() == []
        assert "vector" in _extensions(cursor)  # 扩展保留（契约：downgrade 不删 extension）

    # ── 再 upgrade head：全链可重复 ──
    alembic(["upgrade", "head"])
    with _connect(test_db) as conn, conn.cursor() as cursor:
        tables = _public_tables(cursor)
        assert set(CORE_TABLES) <= tables
        assert "alembic_version" in tables
        # seed 在全新 users 表上再建（downgrade 清表 → upgrade 重新 seed，§1.1）
        cursor.execute("SELECT id::text, name FROM users")
        assert cursor.fetchall() == [
            (common_settings.default_user_id, common_settings.default_user_name)
        ]

    # 零漂移：迁移产物 == ORM metadata（autogenerate 闭环的运行时验证）
    alembic(["check"])


def test_0001_default_user_seed(alembic, test_db, db_truncate):
    """默认用户 seed：upgrade 后 users 恰一行且 id/name == 配置值（§1.1/§4）。

    先 downgrade base 再 upgrade head：seed 仅随 0001 执行落行——测试库常驻
    head 态时 upgrade 为幂等 no-op、不触发 seed，无从断言；downgrade→upgrade
    后恰一行（不因重复执行累积多行）即幂等性的真库实证。
    """
    alembic(["downgrade", "base"])
    alembic(["upgrade", "head"])
    with _connect(test_db) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT id::text, name FROM users")
        rows = cursor.fetchall()
    assert rows == [
        (common_settings.default_user_id, common_settings.default_user_name)
    ]


def test_0008_turn_scheduler_upgrade_downgrade_upgrade(alembic, test_db, db_truncate):
    """0008（REQ-042）三段真库验证：upgrade→downgrade→upgrade 逐段断言。

    cancel_reason 增列（varchar(16) nullable 无默认）与 idx_turns_running 部分索引
    条件重建（扩 queued）在每个段位的真库事实：information_schema / pg_indexes
    参数化查询；PG 对 WHERE IN 有规范化改写（= ANY(ARRAY[...]) 等），索引断言取
    字面值出现性而非完整 DDL 文本（'queued' 出现/不出现 + 'pending'/'running' 恒在）。
    """
    # 起点锚到 0001（head 幂等升至链尾后回退，起点与执行顺序解耦）
    alembic(["upgrade", "head"])
    alembic(["downgrade", "0001"])

    # ── 段 1：upgrade 0001→0008（cancel_reason + 索引扩 queued 落地）──
    alembic(["upgrade", "0008"])
    with _connect(test_db) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchall() == [("0008",)]
        assert _turns_cancel_reason(cursor) == [("character varying", 16, "YES", None)]
        indexdef = _turns_running_indexdef(cursor)
        assert "'queued'" in indexdef and "'pending'" in indexdef and "'running'" in indexdef

    # ── 段 2：downgrade 0008→0001（列删除、索引还原 pending/running——全量可逆）──
    alembic(["downgrade", "0001"])
    with _connect(test_db) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchall() == [("0001",)]
        assert _turns_cancel_reason(cursor) == []
        indexdef = _turns_running_indexdef(cursor)
        assert "'queued'" not in indexdef
        assert "'pending'" in indexdef and "'running'" in indexdef

    # ── 段 3：再 upgrade 0001→0008（可重复）+ 零漂移 ──
    alembic(["upgrade", "0008"])
    with _connect(test_db) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchall() == [("0008",)]
        assert _turns_cancel_reason(cursor) == [("character varying", 16, "YES", None)]
        assert "'queued'" in _turns_running_indexdef(cursor)

    # 迁移产物 == ORM metadata（含 0008 列与索引形态）：0008 非链尾，先升至 head
    # （0009/0010 为纯增量，不触本段断言面）
    alembic(["upgrade", "head"])
    alembic(["check"])


def _turns_followups(cursor) -> list[tuple]:
    """followups 列事实（data_type/可空/默认）；information_schema 参数化查询。"""
    cursor.execute(
        "SELECT data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
        ("turns", "followups"),
    )
    return cursor.fetchall()


def _turn_feedback_indexdef(cursor) -> str:
    """uq_turn_feedback_turn 实际 DDL（唯一索引 + 列 turn_id）。"""
    cursor.execute(
        "SELECT indexdef FROM pg_indexes "
        "WHERE schemaname = 'public' AND indexname = %s",
        ("uq_turn_feedback_turn",),
    )
    rows = cursor.fetchall()
    assert len(rows) == 1, f"uq_turn_feedback_turn 应恰一个，得到 {len(rows)}"
    return rows[0][0]


def test_0009_0010_followups_and_feedback_upgrade_downgrade_upgrade(
    alembic, test_db, db_truncate
) -> None:
    """0009（turns.followups）+ 0010（turn_feedback）三段真库验证（REQ-046/REQ-057）。

    段 1：0008→0010 增列 + 建表/唯一索引落地；段 2：downgrade 到 0008 全量可逆
    （列与表/索引一并消失）；段 3：再 upgrade head 可重复 + `alembic check` 零漂移。
    """
    alembic(["upgrade", "head"])
    alembic(["downgrade", "0008"])

    # ── 段 1：upgrade 0008→0010 ──
    alembic(["upgrade", "head"])
    with _connect(test_db) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchall() == [(CHAIN_HEAD,)]
        # 0009：nullable jsonb、无默认
        assert _turns_followups(cursor) == [("jsonb", "YES", None)]
        # 0010：表 + UNIQUE(turn_id) 索引
        assert "turn_feedback" in _public_tables(cursor)
        indexdef = _turn_feedback_indexdef(cursor)
        assert "UNIQUE INDEX" in indexdef.upper() and "(turn_id)" in indexdef

    # ── 段 2：downgrade 0010→0008（列/表/索引全量可逆）──
    alembic(["downgrade", "0008"])
    with _connect(test_db) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchall() == [("0008",)]
        assert _turns_followups(cursor) == []
        assert "turn_feedback" not in _public_tables(cursor)

    # ── 段 3：再 upgrade head（可重复）+ 零漂移 ──
    alembic(["upgrade", "head"])
    with _connect(test_db) as conn, conn.cursor() as cursor:
        assert _turns_followups(cursor) == [("jsonb", "YES", None)]
        assert "turn_feedback" in _public_tables(cursor)
    alembic(["check"])


def test_turn_feedback_fk_cascade(alembic, test_db, db_truncate) -> None:
    """FK 定义含 ON DELETE CASCADE（data-model §1.16：线程删除经 turns 级联）。"""
    alembic(["upgrade", "head"])
    with _connect(test_db) as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT confdeltype FROM pg_constraint "
            "WHERE conrelid = 'turn_feedback'::regclass AND contype = 'f'"
        )
        assert cursor.fetchall() == [("c",)]  # 'c' = CASCADE


# ---- 0011（v1.14，REQ-058~060/B10）：threads 三增列 + read_at 存量回填 ----


def _threads_mgmt_columns(cursor) -> list[tuple]:
    """threads 三增列事实（列名/类型/可空/默认），按 DDL 顺序返回。"""
    cursor.execute(
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'threads' "
        "AND column_name IN ('pinned', 'read_at', 'archived_at') "
        "ORDER BY ordinal_position"
    )
    return cursor.fetchall()


def _insert_legacy_thread(info, *, title: str) -> uuid.UUID:
    """在 0010 态（无三列）造存量线程行：用于验证 0011 的 read_at 回填。"""
    thread_id = uuid.uuid4()
    conn = _connect(info)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO users (id, name) VALUES (%s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (
                    common_settings.default_user_id,
                    common_settings.default_user_name,
                ),
            )
            cursor.execute(
                "INSERT INTO threads (id, user_id, title, created_at, updated_at) "
                "VALUES (%s, %s, %s, now() - interval '2 days', now())",
                (str(thread_id), common_settings.default_user_id, title),
            )
        conn.commit()
    finally:
        conn.close()
    return thread_id


def test_0011_thread_mgmt_upgrade_downgrade_upgrade(alembic, test_db, db_truncate) -> None:
    """0011 三段真库验证（B-1）：升级增列 + 存量回填 → 降级三列移除 → 再升级可重复。

    回填断言必须在 0010 态的存量行上做（全新表无行 → 回填无从验证），故段 1 前先
    降级到 0010 并插入一行，再升级 head。
    """
    alembic(["upgrade", "head"])
    alembic(["downgrade", "0010"])

    with _connect(test_db) as conn, conn.cursor() as cursor:
        assert _threads_mgmt_columns(cursor) == []  # 0010 态无三列
    legacy_id = _insert_legacy_thread(test_db, title="存量线程（升级前创建）")

    # ── 段 1：upgrade 0010→0011（三增列 + 存量回填）──
    alembic(["upgrade", "head"])
    with _connect(test_db) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchall() == [(CHAIN_HEAD,)]
        assert _threads_mgmt_columns(cursor) == [
            ("pinned", "boolean", "NO", "false"),  # NOT NULL DEFAULT false（§1.2）
            ("read_at", "timestamp with time zone", "YES", None),
            ("archived_at", "timestamp with time zone", "YES", None),
        ]
        cursor.execute(
            "SELECT pinned, read_at = created_at, archived_at "
            "FROM threads WHERE id = %s",
            (str(legacy_id),),
        )
        assert cursor.fetchall() == [(False, True, None)]  # 存量全部视为已读
        # 零新索引（§1.2 v1.14 取舍注）
        cursor.execute(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'public' AND tablename = 'threads' "
            "AND indexname <> 'threads_pkey'"
        )
        assert {row[0] for row in cursor.fetchall()} == {"idx_threads_user_updated"}

    # ── 段 2：downgrade 0011→0010（drop 三列；read_at 数据丢失属回退语义）──
    alembic(["downgrade", "0010"])
    with _connect(test_db) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchall() == [("0010",)]
        assert _threads_mgmt_columns(cursor) == []
        cursor.execute("SELECT count(*) FROM threads WHERE id = %s", (str(legacy_id),))
        assert cursor.fetchone() == (1,)  # 降级只 drop 列，不动行
        # 回 0010 后存量行仍在（read_at 状态随列一起消失——schema 回退可接受语义）
        cursor.execute("SELECT title FROM threads WHERE id = %s", (str(legacy_id),))
        assert cursor.fetchone() == ("存量线程（升级前创建）",)

    # ── 段 3：再 upgrade head（可重复）+ 零漂移（ORM metadata 对齐）──
    alembic(["upgrade", "head"])
    with _connect(test_db) as conn, conn.cursor() as cursor:
        assert len(_threads_mgmt_columns(cursor)) == 3
        # 再升级时该行已存在 → 回填按 NULL 条件跳过（幂等）且 pinned 恢复 default
        cursor.execute(
            "SELECT pinned, read_at = created_at FROM threads WHERE id = %s",
            (str(legacy_id),),
        )
        assert cursor.fetchall() == [(False, True)]
    alembic(["check"])


# ---- 0012（M2 deep 底座，契约逻辑名 0002_m2_deep → 实际 0012）：
#      snapshots / artifacts 两表 + sources.snapshot_id 增列 ----


def _table_columns(cursor, table: str) -> list[tuple]:
    """整表列事实（列名/类型/长度/可空/默认），按 DDL 顺序返回。"""
    cursor.execute(
        "SELECT column_name, data_type, character_maximum_length, is_nullable, "
        "column_default FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s "
        "ORDER BY ordinal_position",
        (table,),
    )
    return cursor.fetchall()


def _sources_snapshot_id(cursor) -> list[tuple]:
    cursor.execute(
        "SELECT data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'sources' "
        "AND column_name = 'snapshot_id'"
    )
    return cursor.fetchall()


def _indexdef(cursor, indexname: str) -> str:
    cursor.execute(
        "SELECT indexdef FROM pg_indexes "
        "WHERE schemaname = 'public' AND indexname = %s",
        (indexname,),
    )
    rows = cursor.fetchall()
    assert len(rows) == 1, f"{indexname} 应恰一个，得到 {len(rows)}"
    return rows[0][0]


def _fk_delete_types(cursor, table: str) -> list[tuple]:
    """表上全部 FK 的 (被引用表, confdeltype)：'c'=CASCADE、'a'=NO ACTION。"""
    cursor.execute(
        "SELECT confrelid::regclass::text, confdeltype FROM pg_constraint "
        "WHERE conrelid = %s::regclass AND contype = 'f' ORDER BY 1",
        (table,),
    )
    return cursor.fetchall()


def _snapshot_id_fk_delete_type(cursor) -> str:
    """sources.snapshot_id → snapshots FK 的 confdeltype（唯一 FK 指向 snapshots）。"""
    cursor.execute(
        "SELECT confdeltype FROM pg_constraint "
        "WHERE conrelid = 'sources'::regclass AND contype = 'f' "
        "AND confrelid = 'snapshots'::regclass"
    )
    rows = cursor.fetchall()
    assert len(rows) == 1, f"sources→snapshots FK 应恰一个，得到 {len(rows)}"
    return rows[0][0]


def test_0012_snapshots_artifacts_upgrade_downgrade_upgrade(
    alembic, test_db, db_truncate
) -> None:
    """0012 三段真库验证：升级建两表+增列 → 降级全量可逆 → 再升级可重复 + 零漂移。

    降级顺序敏感：sources.snapshot_id FK 引用 snapshots——drop 列必须先于 drop 表
    （否则 NO ACTION 依赖拒删）；本断言即该顺序的真库实证。
    """
    alembic(["upgrade", "head"])
    alembic(["downgrade", "0011"])

    # ── 段 1：upgrade 0011→0012（两表 + 索引 + sources.snapshot_id）──
    alembic(["upgrade", "head"])
    with _connect(test_db) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchall() == [(CHAIN_HEAD,)]

        # snapshots（§1.6 契约 SQL 原文逐列）
        assert _table_columns(cursor, "snapshots") == [
            ("id", "uuid", None, "NO", None),
            ("user_id", "uuid", None, "NO", None),
            ("thread_id", "uuid", None, "NO", None),
            ("turn_id", "uuid", None, "NO", None),
            ("url", "text", None, "NO", None),
            ("url_hash", "character", 64, "NO", None),
            ("object_key", "text", None, "NO", None),
            # PG information_schema 对 varchar 默认值带 ::character varying 规范化后缀
            ("content_type", "character varying", 64, "NO", "'text/markdown'::character varying"),
            ("size_bytes", "bigint", None, "NO", None),
            ("fetched_at", "timestamp with time zone", None, "NO", None),
            ("created_at", "timestamp with time zone", None, "NO", "now()"),
        ]
        # 唯一键 (turn_id, url_hash) + 线程索引
        uq = _indexdef(cursor, "uq_snapshots_turn_url")
        assert "UNIQUE INDEX" in uq.upper() and "(turn_id, url_hash)" in uq
        idx_thread = _indexdef(cursor, "idx_snapshots_thread")
        assert "(thread_id)" in idx_thread
        assert _fk_delete_types(cursor, "snapshots") == [("turns", "c")]

        # artifacts（§1.9 契约 SQL 原文逐列）
        assert _table_columns(cursor, "artifacts") == [
            ("id", "uuid", None, "NO", None),
            ("user_id", "uuid", None, "NO", None),
            ("thread_id", "uuid", None, "NO", None),
            ("turn_id", "uuid", None, "YES", None),
            ("artifact_type", "character varying", 16, "NO", None),
            ("filename", "character varying", 256, "NO", None),
            ("rel_path", "text", None, "NO", None),
            ("size_bytes", "bigint", None, "NO", None),
            ("content_type", "character varying", 64, "NO", None),
            ("created_at", "timestamp with time zone", None, "NO", "now()"),
        ]
        idx_art = _indexdef(cursor, "idx_artifacts_thread")
        assert "(thread_id, created_at DESC)" in idx_art
        # thread_id CASCADE（线程删除链 REQ-024）；turn_id 无动作（NO ACTION）
        assert _fk_delete_types(cursor, "artifacts") == [
            ("threads", "c"),
            ("turns", "a"),
        ]

        # sources.snapshot_id（§1.5 M2 增列）：uuid 可空、无默认、FK 无动作
        assert _sources_snapshot_id(cursor) == [("uuid", "YES", None)]
        assert _snapshot_id_fk_delete_type(cursor) == "a"

    # ── 段 2：downgrade 0012→0011（drop 列先于 drop 表——全量可逆）──
    alembic(["downgrade", "0011"])
    with _connect(test_db) as conn, conn.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchall() == [("0011",)]
        tables = _public_tables(cursor)
        assert "snapshots" not in tables and "artifacts" not in tables
        assert _sources_snapshot_id(cursor) == []

    # ── 段 3：再 upgrade head（可重复）+ 零漂移（ORM metadata 对齐）──
    alembic(["upgrade", "head"])
    with _connect(test_db) as conn, conn.cursor() as cursor:
        assert _sources_snapshot_id(cursor) == [("uuid", "YES", None)]
        assert _table_columns(cursor, "snapshots")[0] == ("id", "uuid", None, "NO", None)
        assert _table_columns(cursor, "artifacts")[0] == ("id", "uuid", None, "NO", None)
    alembic(["check"])
