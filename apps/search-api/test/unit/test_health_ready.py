"""unit 层：/health/ready 就绪探针（不真的连库，两个分支都用假引擎覆盖）。

- 数据库可达 → 200 + {"status": "ok", "database": "connected"}；
- 数据库不可达（连接抛错）→ 200 + {"status": "not ready", "database": "disconnected"}，
  不抛异常、不返回 5xx（探针语义是「答未就绪」，不是「接口出错」）。

骨架改动注：探针语句为 `conn.scalar(select(1))`（表达式层构造，编译渲染即基座口径的
探活常量，安全扫描禁原始 SQL 字符串形态）——假连接相应只实现 scalar()，记录编译后
语句文本，断言与 `str(select(1))` 动态比对（不硬编码 SQL 字面量）。
"""
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app


class _FakeConnection:
    """最小异步连接：记录探针发过的语句并支持 async with。"""

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def scalar(self, statement: object, *args: object) -> int:
        # TextClause 取 .text 属性（str() 需要方言）；Select 等表达式对象 str() 即编译文本
        self.statements.append(str(getattr(statement, "text", statement)))
        return 1

    async def __aenter__(self) -> "_FakeConnection":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


class _ConnectedEngine:
    def __init__(self) -> None:
        self.connection = _FakeConnection()

    def connect(self) -> _FakeConnection:
        return self.connection


class _BrokenEngine:
    """模拟无库/连接失败：connect() 即抛错。"""

    def connect(self) -> _FakeConnection:
        raise RuntimeError("connection refused")


def test_health_ready_reports_connected(monkeypatch):
    engine = _ConnectedEngine()
    monkeypatch.setattr("app.main.get_engine", lambda: engine)

    resp = TestClient(app).get("/health/ready")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "connected"}
    # 真的发了探活语句：select(1) 的编译文本（基座口径的探活常量，已在 scratch venv 实证）
    assert engine.connection.statements == [str(select(1))]


def test_health_ready_reports_not_ready_when_db_unreachable(monkeypatch):
    monkeypatch.setattr("app.main.get_engine", lambda: _BrokenEngine())

    resp = TestClient(app).get("/health/ready")

    assert resp.status_code == 200  # 不用 5xx：避免编排/监控把「未就绪」误判为接口故障
    assert resp.json() == {"status": "not ready", "database": "disconnected"}
