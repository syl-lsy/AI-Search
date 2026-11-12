"""e2e 层 conftest：子进程启动真服务（独立端口 18000），轮询就绪后开跑，session 结束清理。

数据库引导（docstring 预留的完整版，2026-11-11 补齐）：pytest_sessionstart 里**先重建
测试库并 `alembic upgrade head` 再启动服务**（references/testing.md 第五节口径）。
重建的必要性（首轮实证）：共享测试库经 integration 会话用后 TRUNCATE（含 users），
而迁移 0001 的幂等 seed 只在首次 upgrade 执行——种子默认用户缺失时 e2e 服务写
threads 必踩 threads_user_id_fkey（500001）；重建后 seed 随首次 upgrade 落库，
服务子进程开箱可用。DB_ 组强注入（override）同 integration/conftest.py——防外层
shell 残留的开发库变量把服务子进程/迁移指错库（服务经 Popen 继承父进程环境）。

清理注意：服务以独立进程组启动（start_new_session），收尾先 SIGTERM 整组、收尸主进程，
再探测组内残留成员补 SIGKILL，连带 uv / uvicorn reload 子进程，避免残留进程占用 18000 端口。
（POSIX 方案；Windows 改用 _process.terminate()/kill()）
"""
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psycopg2
import psycopg2.sql
import pytest
from dotenv import dotenv_values, load_dotenv

BASE_URL = "http://127.0.0.1:18000"
# conftest 位于 apps/<svc>/test/e2e/ 下，parents[4] 即仓库根
# （也是服务子进程的 cwd，决定其 .env 解析位置——所有命令统一从仓库根跑）
ROOT = Path(__file__).resolve().parents[4]

# 装载 .env.test：值进入 os.environ 后被服务子进程继承（Popen 默认继承父进程环境）
load_dotenv(ROOT / ".env.test")

# DB_ 组强注入（override）：load_dotenv 默认不覆盖既有变量，外层 shell 残留的开发库
# DB_* 会把服务子进程与迁移链指错库（integration/conftest.py 同款防护；只注入 DB_
# 前缀，其余变量维持 load_dotenv 语义不动）。
for _key, _value in dotenv_values(ROOT / ".env.test").items():
    if _key.startswith("DB_") and _value is not None:
        os.environ[_key] = _value

# 日志文件目录：错误日志不落仓库根 logs/（tmp/ 已 gitignore）。
# 在 load_dotenv 之后 setdefault：.env.test 的 LOG_FILE_DIR 优先，缺省时兜底 tmp/logs；
# 服务子进程同样继承该变量（Popen 未传 env，走父进程环境）。
os.environ.setdefault("LOG_FILE_DIR", "tmp/logs")

_process: subprocess.Popen | None = None


def _signal_group(pgid: int, sig: int) -> None:
    """对进程组发信号；组已消失（ESRCH）或只剩待收尸的僵尸时安静跳过。

    macOS 注意：killpg 对「组内已无存活、仅剩未收尸僵尸」的组返回 EPERM
    （errno 1），与 ESRCH 一样属于不可/无需再终止的状态，一并忽略。
    """
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _group_alive(pgid: int) -> bool:
    """探测进程组仍有可终止的存活成员（信号 0 只做存在性检查）。

    EPERM（macOS 对僵尸组的行为，见 _signal_group 注释）视为"无存活成员"。
    """
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _stop_service() -> None:
    """整组终止服务：SIGTERM 优雅退出，10 秒未退再 SIGKILL。

    即使主进程已退出也继续清理：uvicorn reload 的子进程可能成为孤儿仍占端口，
    而进程组 ID 始终等于启动时的主进程 PID（start_new_session=True），
    只要组内还有存活成员，killpg 依然有效。先收尸主进程再探测/补刀，
    避免其僵尸状态干扰信号 0 探测。
    """
    global _process
    if _process is None:
        return
    pgid = _process.pid

    _signal_group(pgid, signal.SIGTERM)

    try:
        _process.wait(timeout=10)  # 收尸主进程，同时给优雅退出留时间窗
    except subprocess.TimeoutExpired:
        pass

    if _group_alive(pgid):  # 组内仍有存活成员（孤儿 reload 子进程等）→ 补刀
        _signal_group(pgid, signal.SIGKILL)
        try:
            _process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    _process = None


# ---- 数据库引导：重建测试库 + alembic upgrade head（docstring 预留完整版） ----

# 运维受控标识符（.env.test 库名/角色名）预校验：字母/数字/下划线，防标识符逃逸
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

ALEMBIC_INI = "apps/search-api/alembic.ini"


def _admin_connect(db_values: dict[str, str], dbname: str, *, user: str):
    """管理面连接（同步 psycopg2）：超级用户引导密码复用 DB_PASSWORD
    （deploy/docker-compose.yml postgres.environment 设计，integration 同款）。"""
    return psycopg2.connect(
        host=db_values["DB_HOST"],
        port=int(db_values["DB_PORT"]),
        user=user,
        password=db_values["DB_PASSWORD"],
        dbname=dbname,
        connect_timeout=5,
    )


def _bootstrap_test_db() -> None:
    """DROP（WITH FORCE）→ CREATE（owner=应用角色）→ pgvector 预装 → alembic head。

    密钥值绝不打印（alembic 失败输出先做密码消毒再入错误信息）；库名/角色名经
    正则预校验 + psycopg2.sql.Identifier 组合引用（libpq 层转义，零字符串拼接）；
    与开发库同名时停手（迁移链误伤开发库不可接受——integration/conftest.py 同款防呆）。
    """
    from dotenv import dotenv_values  # noqa: PLC0415 —— 与上方注入共用来源，延迟导入就近可读

    db_values = {
        key: value
        for key, value in dotenv_values(ROOT / ".env.test").items()
        if key in {"DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME"}
        and value is not None
    }
    missing = {"DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME"} - set(db_values)
    if missing:
        raise RuntimeError(f".env.test 缺 {', '.join(sorted(missing))}——e2e 数据库引导停手")

    db_name, db_user = db_values["DB_NAME"], db_values["DB_USER"]
    if not (_SAFE_IDENTIFIER_RE.fullmatch(db_name) and _SAFE_IDENTIFIER_RE.fullmatch(db_user)):
        raise RuntimeError("测试库库名/角色名含非法标识符字符——请检查 .env.test（仅字母数字下划线）")
    dev_db_name = dotenv_values(ROOT / ".env").get("DB_NAME")
    if dev_db_name and db_name == dev_db_name:
        raise RuntimeError(f".env.test 的 DB_NAME 与开发库同名（{db_name}）——重建停手，人工裁决")

    # DROP（PG13+ WITH (FORCE) 终止残留连接）+ CREATE + pgvector 预装（超级用户）
    conn = _admin_connect(db_values, "postgres", user="postgres")
    conn.autocommit = True  # DROP/CREATE DATABASE 不得处于事务块内
    try:
        with conn.cursor() as cursor:
            # DDL 标识符无法走 %s 绑定参数——用 psycopg2 官方 Composable 组合式
            # （libpq 层转义 + 正则预校验双层防护；integration/conftest.py 同款先例）
            cursor.execute(
                psycopg2.sql.SQL("DROP DATABASE IF EXISTS ")
                + psycopg2.sql.Identifier(db_name)
                + psycopg2.sql.SQL(" WITH (FORCE)")
            )
            cursor.execute(
                psycopg2.sql.SQL("CREATE DATABASE ")
                + psycopg2.sql.Identifier(db_name)
                + psycopg2.sql.SQL(" OWNER ")
                + psycopg2.sql.Identifier(db_user)
            )
        conn.close()
        conn = _admin_connect(db_values, db_name, user="postgres")
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")  # vector 非 trusted 扩展
    finally:
        conn.close()

    # alembic upgrade head（子进程固定参数列表 + shell=False；迁移 0001 幂等 seed 随首次
    # upgrade 落默认用户——integration「不补种则 threads/turns 全部写不进」实锤的根修）
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", ALEMBIC_INI, "upgrade", "head"],
        cwd=ROOT,
        env=os.environ,
        capture_output=True,
        text=True,
        timeout=180,
        shell=False,
        check=False,
    )
    if result.returncode != 0:
        sanitized = f"{result.stdout}\n{result.stderr}".replace(
            db_values["DB_PASSWORD"], "***"
        )
        raise RuntimeError(f"alembic upgrade head 失败（exit {result.returncode}）：\n{sanitized}")


def pytest_sessionstart(session):
    """重建测试库并升级到 head，再启动真服务子进程、等待 /health 就绪（60s 超时）。"""
    global _process
    _bootstrap_test_db()
    _process = subprocess.Popen(
        [
            "uv", "run", "--package", "search-api",
            "fastapi", "dev", "apps/search-api/app/main.py",
            "--port", "18000",
            "--no-reload",  # e2e 不需要热重载：无 reloader 中间层，SIGTERM 收尾确定、少一层子进程
        ],
        cwd=ROOT,
        start_new_session=True,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{BASE_URL}/health", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)  # 统一退避：连接失败与非 200（如 5xx）一律等待，避免全速空转
    _stop_service()
    raise RuntimeError("e2e 服务启动超时：/health 未在 60s 内就绪")


def pytest_sessionfinish(session, exitstatus):
    _stop_service()


@pytest.fixture
def base_url() -> str:
    """真服务基地址（e2e 测试统一经它发请求）。"""
    return BASE_URL
