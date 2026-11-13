"""unit 层：turns 域路由（app/api/v1/turns.py，api.md §2.5/§2.7）。

覆盖点：
- GET /turns/{turn_id}（§2.5）：路由 + service 映射（turn_service.get_turn_detail）
  一体验证——进行中（running）亦返回已持久化部分；归属校验（不存在/越权/非法
  UUID）一律 404001「轮次不存在」；推导字段 degraded/export_available；§2.5 全
  字段形状（sources/stages/usage/error/分期口径字段恒值）；
- POST /turns/{turn_id}/cancel（§2.7）：响应 data.status="canceling"（仅为响应信号，
  非 turns.status 枚举值）；404001 / 409002（已终态）；
- 成功信封 {"code":"0","message":"ok"} 与错误体 {"code","message"}。

DB 走最小 AsyncSession 假体（按 select 实体类型分流 Turn/Source/TurnEvent——零网络）；
cancel 的调度器走 dependency_overrides 假体（排程/出队语义归 test_turn_service.py）。
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.models import Source, Turn, TurnEvent
from app.dependencies import get_current_user, get_db
from app.exception import NotFoundException, TurnTerminalException
from app.main import app
from app.services.turn_service import get_thread_scheduler

_USER_ID = "00000000-0000-0000-0000-0000000000aa"
_OTHER_USER_ID = "00000000-0000-0000-0000-0000000000bb"
_TURN_ID = "c31b9d74-0000-0000-0000-000000000002"


# ---- 假体：最小 AsyncSession / turns・sources 行（零网络，只暴露实现消费的面）----


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeSession:
    """按 select 实体类型分流：Turn 主行 / Source 来源行 / TurnEvent images 行
    （get_turn_detail 的三段查询——A6 P1 评审修复起含 §2.5 images[] 重建查询）。

    expect_user_id 模拟真实 SQL 的归属过滤（WHERE user_id = <当前用户>）——行为
    同语义：行 user_id 与期望不符即查无此行（越权 404001 的假体侧依据）。
    """

    def __init__(
        self,
        turn: Any = None,
        sources: list[Any] | None = None,
        *,
        images_events: list[Any] | None = None,
        expect_user_id: uuid.UUID | None = None,
    ) -> None:
        self._turn = turn
        self._sources = sources if sources is not None else []
        self._images_events = images_events if images_events is not None else []
        self._expect_user_id = expect_user_id

    async def scalars(self, stmt: Any) -> _FakeResult:
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Turn:
            rows = [self._turn] if self._turn is not None else []
            if self._expect_user_id is not None:
                rows = [
                    row
                    for row in rows
                    if getattr(row, "user_id", None) == self._expect_user_id
                ]
            return _FakeResult(rows)
        if entity is Source:
            return _FakeResult(list(self._sources))
        if entity is TurnEvent:
            return _FakeResult(list(self._images_events))
        raise AssertionError(f"未预期的查询实体：{entity}")


def _turn_row(**overrides: Any) -> SimpleNamespace:
    """turns 行假体：列名对齐 db/models/turn.py（值即 §2.5 契约示例口径）。"""
    row = SimpleNamespace(
        id=uuid.UUID(_TURN_ID),
        thread_id=uuid.UUID("7d2f0a1e-0000-0000-0000-000000000001"),
        user_id=uuid.UUID(_USER_ID),
        mode="deep",
        status="completed",
        query="对比 LangGraph 与 CrewOS 做调研 Agent 的取舍",
        query_lang="zh",
        rewritten_queries=["LangGraph 调研 agent 架构"],
        answer_md="# 调研报告：...\n\n## 摘要",
        parent_turn_id=None,
        error=None,
        timing={
            "stages": [
                {
                    "stage_key": "plan",
                    "status": "done",
                    "started_at": "2026-11-01T08:12:01Z",
                    "ended_at": "2026-11-01T08:12:04Z",
                    "duration_ms": 3200,
                    "label": "规划 4 个子课题",
                    "extra": {"subtopics": 4},
                }
            ]
        },
        usage={},
        degraded_reason=None,
        cancel_reason=None,
        duration_ms=152300,
        created_at=datetime(2026, 11, 1, 8, 12, tzinfo=UTC),
        finished_at=datetime(2026, 11, 1, 8, 14, 32, tzinfo=UTC),
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def _source_row(**overrides: Any) -> SimpleNamespace:
    """sources 行假体：列名对齐 db/models/source.py（M1 基础列）。"""
    row = SimpleNamespace(
        id=uuid.uuid4(),
        ref_no=1,
        source_type="web",
        title="LangGraph docs — Send API",
        url="https://docs.langchain.com/...",
        snippet="Send lets you fan out...",
        score=Decimal("0.9123"),
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


class _FakeScheduler:
    """调度器假体：cancel_turn 记录调用面，按预设返回/抛异常。"""

    def __init__(
        self, *, result: dict[str, str] | None = None, exc: Exception | None = None
    ) -> None:
        self.cancel_calls: list[dict[str, Any]] = []
        self._result = result
        self._exc = exc

    async def cancel_turn(
        self, turn_id: str, *, user_id: str, reason: str = "user"
    ) -> dict[str, str]:
        self.cancel_calls.append(
            {"turn_id": turn_id, "user_id": user_id, "reason": reason}
        )
        if self._exc is not None:
            raise self._exc
        return self._result or {"turn_id": turn_id, "status": "canceling"}


@pytest.fixture
def overrides() -> Iterator[dict[Any, Any]]:
    yield app.dependency_overrides
    app.dependency_overrides.clear()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---- GET /api/v1/turns/{turn_id}（§2.5：路由 + service 映射一体）----


async def test_get_turn_completed_deep_full_shape(overrides: dict) -> None:
    session = _FakeSession(
        turn=_turn_row(),
        sources=[
            _source_row(),
            _source_row(
                ref_no=2,
                source_type="kb",
                title="内部设计文档-检索融合.md",
                url=None,
                snippet="KB 候选与网络候选合并后统一 rerank...",
                score=Decimal("0.8871"),
            ),
        ],
    )
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: _USER_ID
    async with _client() as client:
        response = await client.get(f"/api/v1/turns/{_TURN_ID}")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "0" and body["message"] == "ok"
    data = body["data"]
    assert data["turn_id"] == _TURN_ID
    assert data["thread_id"].startswith("7d2f0a1e")
    assert (data["mode"], data["status"]) == ("deep", "completed")
    assert data["query_lang"] == "zh"
    assert data["rewritten_queries"] == ["LangGraph 调研 agent 架构"]
    assert data["answer_md"].startswith("# 调研报告")
    assert data["parent_turn_id"] is None
    # sources：§2.5 sources[] 形状；snapshot_available/kb_doc_id 分期口径恒值
    assert len(data["sources"]) == 2
    first, kb = data["sources"]
    assert first["source_type"] == "web" and first["ref_no"] == 1
    assert first["score"] == pytest.approx(0.9123)
    assert first["snapshot_available"] is False and first["kb_doc_id"] is None
    assert kb["source_type"] == "kb" and kb["url"] is None
    # stages：来自 turns.timing.stages（jsonb ISO 字符串 → datetime）
    assert data["stages"][0]["stage_key"] == "plan"
    assert data["stages"][0]["status"] == "done"
    assert data["stages"][0]["duration_ms"] == 3200
    assert data["stages"][0]["label"] == "规划 4 个子课题"
    assert data["stages"][0]["extra"] == {"subtopics": 4}  # §7.2 extra 透传（评审 P2）
    # 推导字段：degraded=status==degraded；export_available=deep 且 completed/degraded
    assert data["degraded"] is False
    assert data["export_available"] is True
    # 分期口径（用户裁决 2026-11-08）：恒 false / null；memory_hits 不返回
    # （api.md §2.5「M5 及之前无此字段」，评审 P2 勘误——预置字段已移除）
    assert data["incognito"] is False
    assert data["followups"] is None
    assert "memory_hits" not in data
    assert data["images"] == []  # 零 images 行→§2.5 重建空数组（turn_service._rebuild_images_briefs）
    assert data["error"] is None
    assert data["usage"]["total_tokens"] is None  # usage={}（M1 简化）
    assert data["duration_ms"] == 152300
    assert data["cancel_reason"] is None
    assert data["created_at"].startswith("2026-11-01T08:12:00")
    assert data["finished_at"].startswith("2026-11-01T08:14:32")


async def test_get_turn_running_returns_persisted_partial(overrides: dict) -> None:
    """进行中调用合法：返回已持久化部分（api.md §2.5 253 行）。"""
    session = _FakeSession(
        turn=_turn_row(
            status="running", answer_md=None, timing={}, finished_at=None
        )
    )
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: _USER_ID
    async with _client() as client:
        response = await client.get(f"/api/v1/turns/{_TURN_ID}")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "running"
    assert data["answer_md"] is None
    assert data["sources"] == [] and data["stages"] == []
    assert data["finished_at"] is None
    assert data["export_available"] is False


async def test_get_turn_degraded_deep_derivations(overrides: dict) -> None:
    """degraded 推导：degraded=True；deep 降级轮 export_available 仍 True（§2.5 248 行）。"""
    session = _FakeSession(
        turn=_turn_row(status="degraded", degraded_reason="timeout")
    )
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: _USER_ID
    async with _client() as client:
        response = await client.get(f"/api/v1/turns/{_TURN_ID}")
    data = response.json()["data"]
    assert data["degraded"] is True
    assert data["degraded_reason"] == "timeout"
    assert data["export_available"] is True


async def test_get_turn_fast_completed_export_unavailable(overrides: dict) -> None:
    session = _FakeSession(turn=_turn_row(mode="fast"))
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: _USER_ID
    async with _client() as client:
        response = await client.get(f"/api/v1/turns/{_TURN_ID}")
    data = response.json()["data"]
    assert data["export_available"] is False  # 仅 deep 且 completed/degraded


async def test_get_turn_deep_completed_empty_answer_export_unavailable(
    overrides: dict,
) -> None:
    """api.md v1.10 §2.5：export_available 补 answer_md 非空条件（与 §2.3 has_report
    同源）——deep+completed 但无产出时 false。"""
    session = _FakeSession(turn=_turn_row(status="completed", answer_md=None))
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: _USER_ID
    async with _client() as client:
        response = await client.get(f"/api/v1/turns/{_TURN_ID}")
    data = response.json()["data"]
    assert (data["mode"], data["status"]) == ("deep", "completed")
    assert data["answer_md"] is None
    assert data["export_available"] is False  # deep 但无产出


async def test_get_turn_failed_error_payload(overrides: dict) -> None:
    error = {"code": "500001", "message": "内部错误", "stage_key": None}
    session = _FakeSession(
        turn=_turn_row(status="failed", error=error, answer_md=None)
    )
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: _USER_ID
    async with _client() as client:
        response = await client.get(f"/api/v1/turns/{_TURN_ID}")
    data = response.json()["data"]
    assert data["status"] == "failed"
    assert data["error"] == error


async def test_get_turn_canceled_cancel_reason(overrides: dict) -> None:
    session = _FakeSession(
        turn=_turn_row(status="canceled", cancel_reason="superseded")
    )
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: _USER_ID
    async with _client() as client:
        response = await client.get(f"/api/v1/turns/{_TURN_ID}")
    data = response.json()["data"]
    assert data["status"] == "canceled"
    assert data["cancel_reason"] == "superseded"


async def test_get_turn_not_found_404001(overrides: dict) -> None:
    session = _FakeSession(turn=None)
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: _USER_ID
    async with _client() as client:
        response = await client.get(f"/api/v1/turns/{_TURN_ID}")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "404001" and body["message"] == "轮次不存在"


async def test_get_turn_cross_user_404001(overrides: dict) -> None:
    """越权同码 404001（api.md §0：不泄漏资源存在性）——假体按 user_id 过滤同语义。"""
    session = _FakeSession(
        turn=_turn_row(user_id=uuid.UUID(_OTHER_USER_ID)),
        expect_user_id=uuid.UUID(_USER_ID),
    )
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: _USER_ID
    async with _client() as client:
        response = await client.get(f"/api/v1/turns/{_TURN_ID}")
    assert response.status_code == 404
    assert response.json()["code"] == "404001"


async def test_get_turn_illegal_uuid_404001(overrides: dict) -> None:
    session = _FakeSession()
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: _USER_ID
    async with _client() as client:
        response = await client.get("/api/v1/turns/not-a-uuid")
    assert response.status_code == 404
    assert response.json()["code"] == "404001"


# ---- POST /api/v1/turns/{turn_id}/cancel（§2.7 停止）----


async def test_cancel_success_returns_canceling(overrides: dict) -> None:
    scheduler = _FakeScheduler()
    app.dependency_overrides[get_current_user] = lambda: _USER_ID
    app.dependency_overrides[get_thread_scheduler] = lambda: scheduler
    async with _client() as client:
        response = await client.post(f"/api/v1/turns/{_TURN_ID}/cancel")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "0" and body["message"] == "ok"
    assert body["data"] == {"turn_id": _TURN_ID, "status": "canceling"}
    assert scheduler.cancel_calls == [
        {"turn_id": _TURN_ID, "user_id": _USER_ID, "reason": "user"}
    ]


async def test_cancel_not_found_404001(overrides: dict) -> None:
    scheduler = _FakeScheduler(exc=NotFoundException("轮次不存在"))
    app.dependency_overrides[get_current_user] = lambda: _USER_ID
    app.dependency_overrides[get_thread_scheduler] = lambda: scheduler
    async with _client() as client:
        response = await client.post(f"/api/v1/turns/{_TURN_ID}/cancel")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "404001" and body["message"] == "轮次不存在"


async def test_cancel_terminal_409002(overrides: dict) -> None:
    scheduler = _FakeScheduler(exc=TurnTerminalException("轮次已处于终态，不可取消"))
    app.dependency_overrides[get_current_user] = lambda: _USER_ID
    app.dependency_overrides[get_thread_scheduler] = lambda: scheduler
    async with _client() as client:
        response = await client.post(f"/api/v1/turns/{_TURN_ID}/cancel")
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "409002" and body["message"] == "轮次已处于终态，不可取消"


# ---- §2.5 images[] 自 turn_events 重建（api.md v1.12，A6 P1 评审修复）----


async def test_get_turn_rebuilds_images_from_turn_events(overrides: dict) -> None:
    """images 行在场 → §2.5 images[] 重建：条目键可省形态透传——全键条目带
    source_url/title/width/height；仅 url 条目序列化**不含 null 键**（不置 null，
    api.md §7.2 v1.12 同口径）；零 images 行 → 空数组（其余用例假体默认即零行）。"""
    images_event = SimpleNamespace(
        seq=8,
        event_type="images",
        payload={
            "images": [
                {
                    "url": "https://example.com/a.png",
                    "source_url": "https://example.com/page",
                    "title": "架构图",
                    "width": 1200,
                    "height": 750,
                },
                {"url": "https://example.com/bare.png"},
            ]
        },
    )
    session = _FakeSession(turn=_turn_row(), images_events=[images_event])
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: _USER_ID
    async with _client() as client:
        response = await client.get(f"/api/v1/turns/{_TURN_ID}")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["images"] == [
        {
            "url": "https://example.com/a.png",
            "source_url": "https://example.com/page",
            "title": "架构图",
            "width": 1200,
            "height": 750,
        },
        {"url": "https://example.com/bare.png"},
    ]
    assert "source_url" not in data["images"][1]
    assert "title" not in data["images"][1]
    assert "width" not in data["images"][1]
    assert "height" not in data["images"][1]


async def test_get_turn_skips_malformed_image_entries(overrides: dict) -> None:
    """历史行畸形条目（非 dict/缺 url/字段非法）静默跳过——重建不拖垮回看面。"""
    images_event = SimpleNamespace(
        seq=8,
        event_type="images",
        payload={
            "images": [
                "junk",  # 非 dict
                {"title": "缺 url"},  # 缺 url（url 恒在口径）
                {"url": "https://example.com/ok.png", "width": "wide"},  # 字段非法
                {"url": "https://example.com/good.png"},  # 合法
            ]
        },
    )
    session = _FakeSession(turn=_turn_row(), images_events=[images_event])
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: _USER_ID
    async with _client() as client:
        response = await client.get(f"/api/v1/turns/{_TURN_ID}")
    assert response.status_code == 200
    assert response.json()["data"]["images"] == [{"url": "https://example.com/good.png"}]
