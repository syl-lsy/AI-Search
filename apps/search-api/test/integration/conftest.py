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
    """真库 + 已升级到 head + 默认用户补种（幂等）；用后截断。

    补种默认用户：_truncate_core_tables 会连 users 一起清空，而迁移 0001 的幂等
    seed 只在首次 upgrade 执行——不补种则 threads/turns（user_id FK）全部写不进
    （B2 契约测试实锤）。以 id 锚 ON CONFLICT DO NOTHING，与 app/db/seed.py 的
    幂等 upsert 语义一致（值同源 common_settings DEFAULT_USER_ 组）。
    """
    run_alembic(test_db, ["upgrade", "head"])
    _seed_default_user(test_db)
    try:
        yield test_db
    finally:
        _truncate_core_tables(test_db)


def _seed_default_user(info: TestDbInfo) -> None:
    """幂等补种默认用户（管理面 psycopg2；INSERT 全参数绑定，值取自配置单例）。"""
    from app.core.config import common_settings  # noqa: PLC0415 —— 延迟导入：在 DB_ 注入后读

    conn = _pg_connect(info, info.name, user=info.user)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO users (id, name) VALUES (%s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (common_settings.default_user_id, common_settings.default_user_name),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def db_truncate(test_db) -> TestDbInfo:
    """用后截断（迁移链用例自管 upgrade/downgrade，本 fixture 只负责清理数据行）。"""
    yield test_db
    _truncate_core_tables(test_db)


# ---- B2 线程轮次基建（lifespan 等价启停：真库 + 桩图 + 真 EventBus，Nacos 走内置默认）----

import asyncio  # noqa: E402
import time as _time  # noqa: E402
from collections.abc import Awaitable, Callable  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from typing import Any  # noqa: E402


class _NoNacosClient:
    """NacosAssetClient 假体：start/fetch 立即失败 → AssetCenter 按降级阶梯落 L4 内置默认。

    内置默认（config_center/defaults.py）即 nacos-config.md 契约初值——spec.turn.
    default_on_conflict="reject"、model.mapping.chat.options 三键——契约测试的消费
    语义与生产 L1 快照完全一致，integration 不依赖 Nacos 存活（同 test_health.py
    「非库探针假体」口径）。
    """

    async def start(self) -> None:
        raise RuntimeError("integration 基建 fixture 不连 Nacos（落 L4 内置默认）")

    async def fetch(self, data_id: str) -> str:
        raise RuntimeError(f"integration 基建 fixture 不连 Nacos：{data_id}")

    async def close(self) -> None:
        return None


@dataclass
class B2Runtime:
    """B2 基建句柄（lifespan 等价产物）：资产中心 / EventBus / ThreadScheduler。"""

    center: Any  # AssetCenter（L4 内置默认，快照目录隔离在 tmp_path）
    bus: Any  # EventBus（进程级持有者已注册，SSE 端点经 get_event_bus 解析）
    scheduler: Any  # ThreadScheduler（TurnRunner 短事务，路由经 get_thread_scheduler 解析）


@pytest.fixture(autouse=True)
async def _recycle_engine_pool_between_tests():
    """每测试后清空引擎连接池（integration 层 loop 卫生）。

    asyncpg 连接绑定创建它的 asyncio loop，pytest-asyncio 默认每测试新 loop——
    池内跨 loop 残留连接在下测 pre_ping 处炸「Future attached to a different
    loop」（B2 契约测试实锤，setup ERROR 形态）。dispose 于当前 loop 内收口本测
    全部连接，下测 checkout 全新连接；引擎对象本身为模块级单例，dispose 只清池
    不换实例（get_session_factory 无感）。
    """
    yield
    from app.db.session import _engine  # noqa: PLC0415 —— 模块级单例的池清理仅测试设施使用

    if _engine is not None:
        await _engine.dispose()


@pytest.fixture(autouse=True)
def _stub_rewrite_llm_for_stub_flows(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """B3 节点接真后（rewrite/web_search/merge_dedupe）：走真桩图的 REST/SSE 流测试不依赖
    真 LLM 与真搜索源——fixture 假体直注（零网络、确定性、不受本地 ollama/凭证存活影响）。

    requires_llm / requires_search 标记的用例（真依赖冒烟）豁免本假体。"""
    if request.node.get_closest_marker("requires_llm") or request.node.get_closest_marker(
        "requires_search"
    ):
        return

    # 假图注入（B3 节点 5/5 synthesis 接真后）：桩流 REST/SSE 测试整体换脚本化假图——
    # 零 LLM/零真源/零网络，事件序列按 §7.4 主干（token 走 messages 流）。
    from test.integration import stub_flow

    from app.services import turn_service  # noqa: PLC0415 —— fixture 局部导入（同 b2_runtime 口径）

    real_main_graph = turn_service.main_graph  # deep 桩分流回真图（fast 才走脚本假图）
    monkeypatch.setattr(
        "app.services.turn_service.main_graph",
        stub_flow.ModeRoutingFakeGraph(real_main_graph),
    )

    # 搜索链假体：registry.resolve 恒 [tavily]、search 恒确定性候选（TurnRunner 轮启动
    # resolve 面兜底——假图下节点不执行，此处保 resolve 不抛）

    # 搜索链假体：registry.resolve 恒 [tavily]、search 恒确定性候选（桩流面零真源调用）
    from app.agent.tools import registry as registry_module
    from app.agent.tools.registry import SearchChain

    class _FakeSearchRegistry:
        def __init__(self) -> None:
            self.breakers: dict[str, Any] = {}

        def bind(self, payload: Any) -> None:
            return None

        def resolve(self, payload: Any, query_lang: str) -> SearchChain:
            return SearchChain(
                sources=["tavily"], backup=None, degraded=False, detail="tavily（假体）"
            )

        def credential_ready(self, server: str) -> bool:
            return True

        async def search(
            self, query: str, source: str, *, time_range: str | None = None, top_k: int = 5
        ) -> list[dict[str, Any]]:
            import asyncio as _asyncio

            delay = getattr(registry_module, "STUB_SEARCH_DELAY_S", 0.0)
            if delay:
                await _asyncio.sleep(delay)  # 桩流运行窗口（心跳/取消语义测试）
            return [
                {
                    "url": f"https://stub.example/{abs(hash(query)) % 1000}",
                    "title": f"桩结果：{query[:30]}",
                    "snippet": "桩搜索结果（integration 假体）",
                    "score": 0.9,
                    "origin": source,
                }
            ]

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(
        registry_module, "get_tools_registry", lambda: _FakeSearchRegistry()
    )
    monkeypatch.setattr(
        "app.services.turn_service.get_tools_registry", lambda: _FakeSearchRegistry()
    )

@pytest.fixture
async def b2_runtime(migrated_db, tmp_path):
    """lifespan 等价基建启停：资产中心 → EventBus → ThreadScheduler → seq 种入。

    启动顺序对齐 main.py lifespan（start_asset_center → start_event_bus →
    start_thread_scheduler → seed_seq_from_db）；快照目录隔离到 tmp_path（不写真实
    data/nacos_snapshot），app_env="test" 绕开 production L4 拒启动语义。用后逆序
    收口：ThreadScheduler 无常驻任务（ARCH §6.1），复位全局持有者后 bus/center
    依次 close（各自清持有者，与 lifespan finally 同序）。

    每测试新启一整套（非会话级）：EventBus 的 Condition/Event 与清扫任务均绑定
    运行 loop，pytest-asyncio 默认每测试新 loop——会话级单例会跨 loop 复用原语。
    """
    from app.config_center import start_asset_center
    from app.core.config import NacosSettings
    from app.services import turn_service
    from app.services.event_bus import start_event_bus

    center = await start_asset_center(
        settings=NacosSettings(
            snapshot_dir=str(tmp_path / "nacos-snapshot"), startup_timeout_s=1
        ),
        app_env="test",
        client=_NoNacosClient(),
    )
    try:
        bus = await start_event_bus()
        try:
            # start_thread_scheduler 幂等不复写全局——复位上测残留，保证本测的
            # scheduler 绑定本测的 bus（无 close API，复位即 lifespan 逆序收口的等价物）
            turn_service._thread_scheduler = None
            scheduler = turn_service.start_thread_scheduler(bus=bus)
            await turn_service.seed_seq_from_db()
            yield B2Runtime(center=center, bus=bus, scheduler=scheduler)
        finally:
            turn_service._thread_scheduler = None
            await bus.close()
    finally:
        await center.close()


async def await_until(
    predicate: Callable[[], Awaitable[bool] | bool],
    *,
    timeout_s: float = 30.0,
    interval_s: float = 0.05,
    description: str = "条件成立",
) -> None:
    """轮询等待谓词成立（桩图运行窗口/终态到达的通用等待；超时 AssertionError）。"""
    deadline = _time.monotonic() + timeout_s
    while True:
        result = predicate()
        if isinstance(result, Awaitable):
            result = await result
        if result:
            return
        if _time.monotonic() > deadline:
            raise AssertionError(f"等待超时（{timeout_s}s）：{description}")
        await asyncio.sleep(interval_s)


class B2Rest:
    """B2 契约测试的 REST 轮询辅助（绑定进程内 ASGI 客户端）。

    BackgroundTask（schedule_turn→图运行）在 POST 响应返回前执行（ASGITransport
    整链路驱动）——需要在运行窗口内并发交互的用例（cancel/排队/打断）以
    asyncio.create_task 提交 POST，再经本类轮询「行已提交可见」的线程/轮次：
    建轮路由的 get_db 为 scope="function"（threads.py，teardown commit 先于
    响应发送与 BackgroundTask），行提交即可从列表/详情端点拿到 thread_id/turn_id。
    """

    def __init__(self, client: Any) -> None:
        self.client = client

    async def wait_first_thread(self) -> tuple[str, str]:
        """等待首个线程/轮次行提交可见，返回 (thread_id, turn_id)（用前表已截断）。"""

        async def _visible() -> bool:
            resp = await self.client.get("/api/v1/threads")
            return resp.status_code == 200 and bool(resp.json()["data"]["items"])

        await await_until(_visible, description="首个线程行提交可见")
        resp = await self.client.get("/api/v1/threads")
        thread_id = resp.json()["data"]["items"][0]["thread_id"]
        detail = await self.client.get(f"/api/v1/threads/{thread_id}")
        return thread_id, detail.json()["data"]["turns"][0]["turn_id"]

    async def turn_payload(self, turn_id: str) -> dict[str, Any] | None:
        """GET /turns/{id} data；未提交可见（404）时返回 None（轮询容错）。"""
        resp = await self.client.get(f"/api/v1/turns/{turn_id}")
        if resp.status_code != 200:
            return None
        return resp.json()["data"]

    async def wait_turn_status(
        self, turn_id: str, statuses: set[str], *, timeout_s: float = 30.0
    ) -> dict[str, Any]:
        """轮询至轮次到达给定状态集，返回最终 §2.5 详情 data。"""

        async def _reached() -> bool:
            payload = await self.turn_payload(turn_id)
            return payload is not None and payload["status"] in statuses

        await await_until(
            _reached,
            timeout_s=timeout_s,
            description=f"轮次 {turn_id} 到达 {sorted(statuses)}",
        )
        payload = await self.turn_payload(turn_id)
        assert payload is not None
        return payload


@pytest.fixture
def b2_rest(async_client) -> B2Rest:
    """B2 REST 轮询辅助（绑定 async_client）。"""
    return B2Rest(async_client)


@pytest.fixture
def wait_for():
    """await_until 的 fixture 形态（测试模块经此轮询任意条件）。"""
    return await_until
