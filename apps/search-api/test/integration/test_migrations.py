"""integration 层：0001_m1_core 迁移链真库验证（requires_db；跑在 .env.test 测试库）。

链路：upgrade head → 断言五表 + alembic_version=0001 + vector 扩展；downgrade
base → 断言五表与 alembic_version 清除、vector 扩展保留（0001 契约：downgrade
不删 extension）；再 upgrade head → 全链可重复；末尾 `alembic check` 零漂移
（迁移产物 == ORM metadata，即 autogenerate 闭环的运行时验证）。

默认用户 seed 断言（0001 已落地——2026-11-07 评审解锁，经 app/db/seed.py 的 ORM
形态，见迁移文件 docstring）：upgrade 后 users 恰一行且 id/name == 配置值；
链路用例末段补 seed 在全新 users 表上的再建断言（downgrade→upgrade 循环幂等）。

alembic 经 conftest 的 alembic fixture 子进程运行（.env.test DB_ 变量已注入环境；
密码脱敏后进断言信息）。缺 .env.test/测试库不可达时整模块 skip（conftest 口径）。
"""

import pytest
from psycopg2 import connect  # 断言面连接（conftest 管理面已建库）

from app.core.config import common_settings

pytestmark = pytest.mark.requires_db

CORE_TABLES = ("users", "threads", "turns", "turn_events", "sources")


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


def test_0001_chain_upgrade_downgrade_upgrade(alembic, test_db, db_truncate):
    """upgrade → downgrade base → 再 upgrade 全成功；seed/扩展/版本表逐段断言。"""
    # ── upgrade head ──
    alembic(["upgrade", "head"])
    with _connect(test_db) as conn, conn.cursor() as cursor:
        tables = _public_tables(cursor)
        assert set(CORE_TABLES) <= tables
        assert "alembic_version" in tables
        assert "vector" in _extensions(cursor)

        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchall() == [("0001",)]

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
