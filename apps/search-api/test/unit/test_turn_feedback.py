"""unit 层：轮反馈端点（REQ-057 / api.md §2.9 v1.17；data-model.md §1.16）。

钉住的语义（冻结契约，逐条对应测试）：
- `POST /api/v1/turns/{turn_id}/feedback` body `{"helpful": bool | null}` 必填
  （v1.17：true/false=upsert 改判、null=清除反馈）——缺失/非 boolean|null
  → 400001（pydantic strict 校验经 RequestValidationError 出错误码，`"yes"`/1 仍拒）；
- 轮次不存在/不属于当前用户/非法 UUID → **404004**（api.md §0 v1.16 独立码）；
- **每轮一条 upsert 改判**（UNIQUE(turn_id) 承载：INSERT … ON CONFLICT (turn_id) DO
  UPDATE）；响应 data = `{turn_id, helpful, updated_at}`，updated_at 随改判刷新
  （data-model §1.16「随 upsert 刷新 = api.md §2.9 响应 updated_at」）；
- **null = 清除**（v1.17）：DELETE 该轮 turn_feedback 行（幂等，无行 no-op），返回
  清除形态 `{turn_id, helpful: null, updated_at: null}`；
- service 只 flush/execute **不 commit**（事务边界在 get_db）；
- §2.5 `feedback_helpful` 读回：turn_feedback 无行 → null；有行 → 现值（回看/刷新
  还原的读通道，A41）；
- 全部 DB 走最小 AsyncSession 假体（语句断言值绑定/ON CONFLICT，零网络）。
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.models import Turn, TurnFeedback
from app.dependencies import get_current_user
from app.exception import FeedbackTurnNotFoundException
from app.main import app
from app.schemas import FeedbackRequest, FeedbackResponse
from app.services.turn_feedback_service import TurnFeedbackService

_USER_ID = "00000000-0000-4000-8000-0000000000aa"
#: 他人 user_id（归属不匹配场景；与 _USER_ID 同池不同值）
_OTHER_USER_ID = "00000000-0000-4000-8000-0000000000bb"
_TURN_ID = "c31b9d74-0000-4000-8000-000000000002"


# ---- DB 假体 ----


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeSession:
    """最小 AsyncSession 假体：scalars 按实体分流（Turn 归属校验）、execute 记录语句。

    `owner_user_id` 非空时按语句绑定的 `Turn.user_id` 与行归属比较（真库 WHERE 过滤
    语义仿真）——用于表达「轮存在但归属他人」；缺省不筛（既有用例形态不变）。
    """

    def __init__(
        self,
        *,
        turn_id: uuid.UUID | None = None,
        owner_user_id: str | uuid.UUID | None = None,
        executed_row: Any = None,
    ) -> None:
        self._turn_id = turn_id
        self._owner_user_id = (
            uuid.UUID(owner_user_id) if isinstance(owner_user_id, str) else owner_user_id
        )
        self._executed_row = executed_row
        self.executed: list[Any] = []
        self.commits = 0
        self.added: list[Any] = []

    async def scalars(self, stmt: Any) -> _FakeResult:
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Turn:
            if self._turn_id is None:
                return _FakeResult([])
            if self._owner_user_id is not None:
                bound = next(
                    (
                        value
                        for key, value in stmt.compile().params.items()
                        if key.startswith("user_id")
                    ),
                    None,
                )
                assert bound is not None, "归属校验语句缺 user_id 绑定"
                if uuid.UUID(str(bound)) != self._owner_user_id:
                    return _FakeResult([])  # 归属不匹配：真库 WHERE 同样回空集
            return _FakeResult([self._turn_id])
        raise AssertionError(f"未预期的查询实体：{entity}")

    async def execute(self, stmt: Any) -> _FakeResult:
        self.executed.append(stmt)
        return _FakeResult([self._executed_row] if self._executed_row is not None else [])

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:  # 只用来断言「service 不 commit」
        self.commits += 1


def _service(session: _FakeSession) -> TurnFeedbackService:
    return TurnFeedbackService(session)  # type: ignore[arg-type]


# ---- service：归属校验（404004）----


async def test_upsert_unknown_turn_raises_404004() -> None:
    """轮不存在/不属于当前用户 → FeedbackTurnNotFoundException（404004）。"""
    session = _FakeSession(turn_id=None)
    with pytest.raises(FeedbackTurnNotFoundException):
        await _service(session).upsert_feedback(
            _TURN_ID, helpful=True, user_id=_USER_ID
        )
    assert session.executed == []  # 归属校验失败即零写（不触 upsert）


async def test_upsert_turn_owned_by_other_user_raises_404004() -> None:
    """轮存在但归属他人 user_id → 404004（归属校验 = id + user_id 双条件；越权与
    不存在同码，不泄漏存在性），归属校验失败即零写（不触 upsert）。

    安全审计 low 缺口：此前单测只覆盖「轮不存在」——归属不匹配面靠 integration 兜，
    单测假体无法表达（scalars 无条件回行）。假体按语句绑定的 `Turn.user_id` 与行归属
    比较（真库 WHERE 过滤语义仿真），归属不一致回空集。
    """
    session = _FakeSession(
        turn_id=uuid.UUID(_TURN_ID), owner_user_id=_OTHER_USER_ID
    )
    with pytest.raises(FeedbackTurnNotFoundException):
        await _service(session).upsert_feedback(
            _TURN_ID, helpful=True, user_id=_USER_ID
        )
    assert session.executed == []  # 归属不匹配即零写（不触 upsert）


async def test_upsert_illegal_turn_id_raises_404004() -> None:
    """非法 UUID 同码（不泄漏解析细节）。"""
    session = _FakeSession(turn_id=None)
    with pytest.raises(FeedbackTurnNotFoundException):
        await _service(session).upsert_feedback(
            "not-a-uuid", helpful=False, user_id=_USER_ID
        )


# ---- service：upsert 语义 ----


async def test_upsert_returns_persisted_values_and_on_conflict_sql() -> None:
    """合法轮：单条 ON CONFLICT (turn_id) DO UPDATE 语句；返回 {turn_id, helpful, updated_at}；
    service 只执行不 commit（事务边界在 get_db）。"""
    turn_uuid = uuid.UUID(_TURN_ID)
    now = datetime(2026, 11, 14, 8, 30, tzinfo=UTC)
    session = _FakeSession(
        turn_id=turn_uuid,
        executed_row=SimpleNamespace(turn_id=turn_uuid, helpful=True, updated_at=now),
    )
    result = await _service(session).upsert_feedback(
        _TURN_ID, helpful=True, user_id=_USER_ID
    )
    assert isinstance(result, FeedbackResponse)
    assert result.turn_id == _TURN_ID
    assert result.helpful is True
    assert result.updated_at == now
    assert session.commits == 0  # 不 commit

    sql = str(session.executed[0])
    assert "INSERT INTO turn_feedback" in sql
    assert "ON CONFLICT (turn_id) DO UPDATE" in sql
    assert "helpful" in sql
    # 参数绑定（无裸值拼接）：FALSE/TRUE 字面量不得出现在 SQL 文本里
    assert "TRUE" not in sql.upper().replace("UPDATED_AT", "")
    assert session.executed[0].compile().params  # 存在绑定参数（helpful / turn_id 等）


async def test_upsert_flip_false_overwrites() -> None:
    """改判为 false 走同一 upsert 路径（响应透传 DB 现值）。"""
    turn_uuid = uuid.UUID(_TURN_ID)
    now = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
    session = _FakeSession(
        turn_id=turn_uuid,
        executed_row=SimpleNamespace(turn_id=turn_uuid, helpful=False, updated_at=now),
    )
    result = await _service(session).upsert_feedback(
        _TURN_ID, helpful=False, user_id=_USER_ID
    )
    assert result.helpful is False


# ---- service：清除语义（api.md §2.9 v1.17：helpful=null 删行）----


async def test_clear_feedback_deletes_row_and_returns_null_form() -> None:
    """helpful=None → 单条参数化 DELETE（非 upsert）；返回清除形态
    {turn_id, helpful: None, updated_at: None}；service 不 commit。"""
    turn_uuid = uuid.UUID(_TURN_ID)
    session = _FakeSession(turn_id=turn_uuid)
    result = await _service(session).upsert_feedback(
        _TURN_ID, helpful=None, user_id=_USER_ID
    )
    assert isinstance(result, FeedbackResponse)
    assert result.turn_id == _TURN_ID
    assert result.helpful is None
    assert result.updated_at is None
    assert session.commits == 0  # 清除同样只 execute 不 commit
    assert len(session.executed) == 1  # 归属校验外的唯一语句 = DELETE
    sql = str(session.executed[0])
    assert "DELETE FROM turn_feedback" in sql
    assert "INSERT" not in sql.upper() and "ON CONFLICT" not in sql.upper()
    assert session.executed[0].compile().params  # 参数绑定（无裸值拼接）


async def test_clear_unknown_turn_raises_404004() -> None:
    """清除路径同样先过归属校验：轮不存在 → 404004，零写（不触 DELETE）。"""
    session = _FakeSession(turn_id=None)
    with pytest.raises(FeedbackTurnNotFoundException):
        await _service(session).upsert_feedback(
            _TURN_ID, helpful=None, user_id=_USER_ID
        )
    assert session.executed == []


# ---- schema 边界（400001 的前置条件）----


def test_feedback_request_requires_bool_or_null() -> None:
    """`helpful` 必填且为 boolean|null（v1.17）：None 合法=清除；缺失/字符串/整数
    → ValidationError（api 层 400001，strict 保留）。"""
    assert FeedbackRequest(helpful=True).helpful is True
    assert FeedbackRequest(helpful=None).helpful is None
    with pytest.raises(ValueError):
        FeedbackRequest()  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        FeedbackRequest(helpful="yes")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        FeedbackRequest(helpful=1)  # type: ignore[arg-type]


# ---- API 层 ----


class _FakeFeedbackService:
    def __init__(self, *, result: FeedbackResponse | None = None, exc: Exception | None = None) -> None:
        self._result = result
        self._exc = exc
        self.calls: list[dict[str, Any]] = []

    async def upsert_feedback(
        self, turn_id: str, *, helpful: bool | None, user_id: str
    ) -> FeedbackResponse:
        self.calls.append({"turn_id": turn_id, "helpful": helpful, "user_id": user_id})
        if self._exc is not None:
            raise self._exc
        assert self._result is not None
        return self._result


@pytest.fixture
def overrides() -> Iterator[dict[Any, Any]]:
    yield app.dependency_overrides
    app.dependency_overrides.clear()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _wire(service: Any) -> None:
    from app.api.v1.turns import get_feedback_service

    app.dependency_overrides[get_feedback_service] = lambda: service
    app.dependency_overrides[get_current_user] = lambda: _USER_ID


async def test_post_feedback_success_envelope(overrides: dict) -> None:
    """成功信封 {"code":"0","message":"ok"} + data {turn_id, helpful, updated_at}。"""
    now = datetime(2026, 11, 14, 8, 30, tzinfo=UTC)
    service = _FakeFeedbackService(
        result=FeedbackResponse(turn_id=_TURN_ID, helpful=True, updated_at=now)
    )
    _wire(service)
    async with _client() as client:
        response = await client.post(
            f"/api/v1/turns/{_TURN_ID}/feedback", json={"helpful": True}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "0" and body["message"] == "ok"
    data = body["data"]
    assert data["turn_id"] == _TURN_ID and data["helpful"] is True
    assert data["updated_at"].startswith("2026-11-14T08:30:00")
    assert service.calls == [
        {"turn_id": _TURN_ID, "helpful": True, "user_id": _USER_ID}
    ]


async def test_post_feedback_turn_missing_404004(overrides: dict) -> None:
    """轮不存在 → HTTP 404 + code 404004（api.md §0 v1.16 独立码）。"""
    service = _FakeFeedbackService(
        exc=FeedbackTurnNotFoundException("轮次不存在", detail="turn 不存在")
    )
    _wire(service)
    async with _client() as client:
        response = await client.post(
            f"/api/v1/turns/{_TURN_ID}/feedback", json={"helpful": False}
        )
    assert response.status_code == 404
    assert response.json()["code"] == "404004"
    assert response.json()["message"] == "轮次不存在"


async def test_post_feedback_invalid_body_400001(overrides: dict) -> None:
    """helpful 缺失/非 boolean|null → 400001（body 校验在边界层完成，不进 service）。"""
    service = _FakeFeedbackService(
        result=FeedbackResponse(
            turn_id=_TURN_ID, helpful=True, updated_at=datetime.now(UTC)
        )
    )
    _wire(service)
    async with _client() as client:
        missing = await client.post(f"/api/v1/turns/{_TURN_ID}/feedback", json={})
        wrong_type = await client.post(
            f"/api/v1/turns/{_TURN_ID}/feedback", json={"helpful": "yes"}
        )
        wrong_int = await client.post(
            f"/api/v1/turns/{_TURN_ID}/feedback", json={"helpful": 1}
        )
    for response in (missing, wrong_type, wrong_int):
        assert response.status_code == 400
        assert response.json()["code"] == "400001"
    assert service.calls == []  # 边界层拒收，零业务调用


async def test_post_feedback_null_clear_form(overrides: dict) -> None:
    """helpful=null（v1.17 清除）→ 透传 service；响应清除形态
    data = {turn_id, helpful: null, updated_at: null}（三键恒在、值 null）。"""
    service = _FakeFeedbackService(
        result=FeedbackResponse(turn_id=_TURN_ID, helpful=None, updated_at=None)
    )
    _wire(service)
    async with _client() as client:
        response = await client.post(
            f"/api/v1/turns/{_TURN_ID}/feedback", json={"helpful": None}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "0" and body["message"] == "ok"
    assert body["data"] == {
        "turn_id": _TURN_ID,
        "helpful": None,
        "updated_at": None,
    }
    assert service.calls == [
        {"turn_id": _TURN_ID, "helpful": None, "user_id": _USER_ID}
    ]


def test_feedback_error_code_registered() -> None:
    """404004 进 ERROR_MAP（错误码唯一事实源）且 HTTP 404。"""
    from app.exception.handler.errors import ERROR_MAP

    code, status, message = ERROR_MAP[FeedbackTurnNotFoundException]
    assert (code, status) == ("404004", 404)
    assert message == "轮次不存在"


def test_turn_feedback_orm_unique_index() -> None:
    """UNIQUE(turn_id) 承载 upsert 改判（data-model §1.16）；FK CASCADE。"""
    table = TurnFeedback.__table__
    unique_indexes = [i for i in table.indexes if i.unique]
    assert [i.name for i in unique_indexes] == ["uq_turn_feedback_turn"]
    assert [c.name for c in unique_indexes[0].columns] == ["turn_id"]
    fk = next(iter(table.c.turn_id.foreign_keys))
    assert fk.column.table.name == "turns"
    assert fk.ondelete == "CASCADE"
    for name in ("helpful", "created_at", "updated_at"):
        assert table.c[name].nullable is False
