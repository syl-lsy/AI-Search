"""integration 层 conftest：强注入 .env.test 的 DB_ 组，提供进程内 ASGI 客户端与真库 fixture。

真库口径（requires_db 标记的用例，B1 起启用完整版）：
- 连接信息只取 .env.test 的 DB_ 前缀变量，并在导入期**强注入 os.environ**（覆盖
  既有值——防外层 shell 残留的开发库变量把迁移/用例指错库）；密钥值绝不打印；
- 测试库不存在则创建（owner=应用角色）并预装 pgvector——vector 非 trusted 扩展，
  CREATE EXTENSION 面向超级用户（deploy 设计：postgres 超级用户引导密码复用
  DB_PASSWORD，见 deploy/docker-compose.yml postgres.environment 注释），故建库
  时预装，迁移内 `CREATE EXTENSION IF NOT EXISTS vector` 成幂等 no-op（0001
  契约的扩展步骤保留不删）；
- alembic 以子进程运行（参数列表 + shell=False，继承注入后的环境）：migrations/env.py
  从 core/config 读到测试库连接，且不与测试进程的 pytest-asyncio 事件循环冲突；
- 用后截断（TRUNCATE，db_truncate / migrated_db fixture）。

缺 .env.test / 缺 DB_ 变量 / 测试库不可达时 requires_db 用例 pytest.skip（给 reason，
不让命令红掉）；.env.test 的库名与开发库同名时 pytest.fail 停手（迁移链一律跑在
测试库，同名=可能误伤开发库，须人工裁决）。
"""
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from dotenv import dotenv_values
from httpx import ASGITransport, AsyncClient

# conftest 位于 apps/<svc>/test/integration/ 下，parents[4] 即仓库根
REPO_ROOT = Path(__file__).resolve().parents[4]
ENV_TEST_PATH = REPO_ROOT / ".env.test"
ALEMBIC_INI = "apps/search-api/alembic.ini"

# 运维受控标识符（.env.test 库名/角色名）预校验：字母/数字/下划线，防标识符逃逸
SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 只强注入 DB_ 前缀变量，不做 .env.test 全量装载：load_dotenv 会把 TAVILY_API_KEY/
# LITELLM_API_KEY 等一并泄进 os.environ——pytest 收集期 conftest 先于全部测试执行且
# 进程内全局共享，unit 层（口径为不装载 .env.test，见 unit/conftest.py docstring）
# 构造 settings 时虽传 _env_file=None 仍会被 pydantic-settings 的大小写不敏感匹配
# 读到泄漏值，「密钥默认空」断言被真密钥误杀（2026-11-07 全量门禁实锤）。
# integration 需要的非 DB_ 变量仅 LOG_FILE_DIR，由下方 setdefault 兜底（与
# .env.test 取值 tmp/logs 一致，其余 LOG_/APP_ENV 等同值项回落 .env 无行为差异）。
if ENV_TEST_PATH.exists():
    for _key, _value in dotenv_values(ENV_TEST_PATH).items():
        if _key.startswith("DB_") and _value is not None:
            os.environ[_key] = _value

# 日志文件目录：错误日志不落仓库根 logs/（tmp/ 已 gitignore）。
os.environ.setdefault("LOG_FILE_DIR", "tmp/logs")

from app.main import app  # noqa: E402 —— 必须在 DB_ 强注入之后导入，让 settings 单例取到测试库连接


@pytest.fixture
async def async_client():
    """进程内 ASGI 客户端（不开真实端口）；接入数据库后补 dependency_overrides[get_db]。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ---- 真库 fixture（requires_db 用例专用；管理面操作用同步 psycopg2，避免事件循环耦合）----

import psycopg2  # noqa: E402 —— dev 依赖（ARCH 锚点项），建库/断言用；运行时引擎仍为 asyncpg
import psycopg2.sql  # noqa: E402 —— 标识符组合引用（sql.Identifier），须显式导入子模块


@dataclass(frozen=True)
class TestDbInfo:
    """测试库连接信息（值全部来自 .env.test DB_ 组；密码只在内存流转，绝不打印）。"""

    host: str
    port: str
    user: str
    password: str
    name: str


def _pg_connect(info: TestDbInfo, dbname: str, *, user: str):
    return psycopg2.connect(
        host=info.host,
        port=int(info.port),
        user=user,
        password=info.password,
        dbname=dbname,
        connect_timeout=5,
    )


def _database_exists(cursor, dbname: str) -> bool:
    """库存在性探测：参数化查询（%s 占位符 + 元组绑定，无字符串拼接）。"""
    cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,))
    return cursor.fetchone() is not None


def _ensure_test_database(info: TestDbInfo) -> None:
    """测试库不存在则创建（owner=应用角色）+ 预装 pgvector（口径见模块 docstring）。

    库名/角色名是运维受控值（.env.test），仍做两层防护：正则预校验 +
    psycopg2.sql.Identifier 组合引用（libpq 层标识符转义，无字符串拼接）。
    """
    if not (SAFE_IDENTIFIER_RE.fullmatch(info.name) and SAFE_IDENTIFIER_RE.fullmatch(info.user)):
        pytest.fail("测试库库名/角色名含非法标识符字符——请检查 .env.test（仅允许字母数字下划线）")

    # 应用角色连维护库 postgres（PUBLIC CONNECT 默认放行）探测库是否存在
    conn = _pg_connect(info, "postgres", user=info.user)
    conn.autocommit = True
    try:
        with conn.cursor() as cursor:
            if _database_exists(cursor, info.name):
                return
    finally:
        conn.close()

    # 超级用户建库（postgres 引导密码复用 DB_PASSWORD，deploy/docker-compose.yml 设计；
    # 应用角色无 CREATEDB——deploy/postgres/init.sh 仅授 LOGIN）。标识符经 libpq
    # 层组合引用（psycopg2.sql.Identifier，官方防注入 API），无任何字符串拼接。
    conn = _pg_connect(info, "postgres", user="postgres")
    conn.autocommit = True
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                psycopg2.sql.SQL("CREATE DATABASE ")
                + psycopg2.sql.Identifier(info.name)
                + psycopg2.sql.SQL(" OWNER ")
                + psycopg2.sql.Identifier(info.user)
            )
    finally:
        conn.close()

    # 预装 pgvector（超级用户；应用角色后续迁移内的 IF NOT EXISTS 成为幂等 no-op）
    conn = _pg_connect(info, info.name, user="postgres")
    conn.autocommit = True
    try:
        with conn.cursor() as cursor:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        conn.close()


@pytest.fixture(scope="session")
def test_db() -> TestDbInfo:
    """真库会话 fixture：装载校验 → 同名防呆 → 建库兜底；前置不满足时 skip/fail（见 docstring）。"""
    if not ENV_TEST_PATH.exists():
        pytest.skip(
            "缺 .env.test（cp .env.test.example .env.test 并填 DB_ 凭证）——requires_db 用例跳过"
        )
    values = dotenv_values(ENV_TEST_PATH)
    missing = [
        key
        for key in ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME")
        if not values.get(key)
    ]
    if missing:
        pytest.skip(f".env.test 缺 {', '.join(missing)}——requires_db 用例跳过")

    info = TestDbInfo(
        host=values["DB_HOST"],
        port=values["DB_PORT"],
        user=values["DB_USER"],
        password=values["DB_PASSWORD"],
        name=values["DB_NAME"],
    )

    # 防呆：与开发库同名 → 停手（迁移链误伤开发库不可接受，人工裁决后再跑）
    dev_db_name = dotenv_values(REPO_ROOT / ".env").get("DB_NAME")
    if dev_db_name and info.name == dev_db_name:
        pytest.fail(
            f".env.test 的 DB_NAME 与开发库同名（{info.name}）——迁移链测试停手，"
            "请改为独立测试库名并人工确认"
        )

    try:
        _ensure_test_database(info)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"测试库不可达（{type(exc).__name__}）——requires_db 用例跳过")
    return info


def run_alembic(info: TestDbInfo, args: list[str]) -> None:
    """子进程跑 alembic（固定参数列表 + shell=False，继承注入后的环境 → env.py 读到测试库）。"""
    command: list[str] = [sys.executable, "-m", "alembic", "-c", ALEMBIC_INI]
    command.extend(args)
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=os.environ,
        capture_output=True,
        text=True,
        timeout=120,
        shell=False,
    )
    if result.returncode != 0:
        sanitized = f"{result.stdout}\n{result.stderr}".replace(info.password, "***")
        raise AssertionError(
            f"alembic {args} 失败（exit {result.returncode}）：\n{sanitized}"
        )


@pytest.fixture
def alembic(test_db):
    """alembic 子进程执行器（绑定测试库信息）：alembic(["upgrade", "head"])。"""

    def _run(args: list[str]) -> None:
        run_alembic(test_db, args)

    return _run


def _truncate_core_tables(info: TestDbInfo) -> None:
    """TRUNCATE 五张核心业务表（RESTART IDENTITY 清 identity 序列；CASCADE 处理 FK）。

    不动 alembic_version——迁移链状态在用例间保留（重复 upgrade 为幂等 no-op）。
    """
    # SQL 为固定常量（表清单写死、无任何插值）；套 try/except 容忍 downgrade 中间态
    conn = _pg_connect(info, info.name, user=info.user)
    conn.autocommit = True
    try:
        with conn.cursor() as cursor:
            try:
                cursor.execute(
                    "TRUNCATE TABLE users, threads, turns, turn_events, sources "
                    "RESTART IDENTITY CASCADE"
                )
            except psycopg2.errors.UndefinedTable:
                pass  # 迁移链失败停留 downgrade base 中间态：表不存在即无需清理
    finally:
        conn.close()


@pytest.fixture
def migrated_db(test_db) -> TestDbInfo:
    """真库 + 已升级到 head（幂等）；用后截断。供需要已迁移 schema 的 integration 用例使用。"""
    run_alembic(test_db, ["upgrade", "head"])
    try:
        yield test_db
    finally:
        _truncate_core_tables(test_db)


@pytest.fixture
def db_truncate(test_db) -> TestDbInfo:
    """用后截断（迁移链用例自管 upgrade/downgrade，本 fixture 只负责清理数据行）。"""
    yield test_db
    _truncate_core_tables(test_db)
