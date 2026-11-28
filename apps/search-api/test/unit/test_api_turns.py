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

from app.db.models import Source, Turn, TurnApproval, TurnEvent, TurnFeedback
from app.dependencies import get_current_user, get_db
from app.exception import NotFoundException, TurnNotRegenerableException, TurnTerminalException
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
    """按 select 实体类型分流：Turn 主行 / Source 来源行 / TurnEvent images 行 /
    TurnFeedback 反馈现值 / TurnApproval 审批行（get_turn_detail 的五段查询——
    反馈读回 REQ-057 起增；审批行 awaiting_ms 投影 B22 v1.37 起增，非门轮零行）。

    expect_user_id 模拟真实 SQL 的归属过滤（WHERE user_id = <当前用户>）——行为
    同语义：行 user_id 与期望不符即查无此行（越权 404001 的假体侧依据）。
    """

    def __init__(
        self,
        turn: Any = None,
        sources: list[Any] | None = None,
        *,
        images_events: list[Any] | None = None,
        feedback_rows: list[Any] | None = None,
        approval_rows: list[Any] | None = None,
        expect_user_id: uuid.UUID | None = None,
    ) -> None:
        self._turn = turn
        self._sources = sources if sources is not None else []
        self._images_events = images_events if images_events is not None else []
        self._feedback_rows = feedback_rows if feedback_rows is not None else []
        self._approval_rows = approval_rows if approval_rows is not None else []
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
        if entity is TurnFeedback:
            return _FakeResult(list(self._feedback_rows))
        if entity is TurnApproval:
            return _FakeResult(list(self._approval_rows))
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
        followups=None,  # 0009 列：未生成/非 fast 达成轮为 null（REQ-046）
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def _source_row(**overrides: Any) -> SimpleNamespace:
    """sources 行假体：列名对齐 db/models/source.py（M1 基础列 + 0012 snapshot_id +
    0013 kb 两列——B19 落库接线投影面）。"""
    row = SimpleNamespace(
        id=uuid.uuid4(),
        ref_no=1,
        source_type="web",
        title="LangGraph docs — Send API",
        url="https://docs.langchain.com/...",
        snippet="Send lets you fan out...",
        score=Decimal("0.9123"),
        snapshot_id=None,  # 0012/m2_deep 增列：None=无快照（fast 轮常态）
        kb_doc_id=None,  # 0013/m3_kb 增列：None=web 卡常态（B19 起 kb 卡带真实值）
        kb_chunk_ids=None,  # 0013/m3_kb 增列：命中切片 id 数组（kb_chunk_id 投影源）
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


class _FakeScheduler:
    """调度器假体：cancel/regenerate 记录调用面，按预设返回/抛异常。"""

    def __init__(
        self, *, result: dict[str, str] | None = None, exc: Exception | None = None
    ) -> None:
        self.cancel_calls: list[dict[str, Any]] = []
        self.regenerate_calls: list[dict[str, Any]] = []
        self.execute_regenerate_calls: list[str] = []
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

    async def regenerate_turn(self, turn_id: str, *, user_id: str) -> dict[str, str]:
        self.regenerate_calls.append({"turn_id": turn_id, "user_id": user_id})
        if self._exc is not None:
            raise self._exc
        return {"turn_id": turn_id, "status": "running"}

    async def execute_regenerate(self, turn_id: str) -> None:
        self.execute_regenerate_calls.append(turn_id)


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
            # 深度轮已读原文：snapshot_id 非空 → snapshot_available 真实推导为 true
            _source_row(snapshot_id=uuid.uuid4()),
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
    # sources：§2.5 sources[] 形状；snapshot_available=M2 起真实推导（snapshot_id
    # 非空，0012/m2_deep）；kb_doc_id 仍分期口径恒 null（M3）
    assert len(data["sources"]) == 2
    first, kb = data["sources"]
    assert first["source_type"] == "web" and first["ref_no"] == 1
    assert first["score"] == pytest.approx(0.9123)
    assert first["snapshot_available"] is True and first["kb_doc_id"] is None
    assert kb["source_type"] == "kb" and kb["url"] is None
    assert kb["snapshot_available"] is False  # snapshot_id=None → 无快照
    # stages：来自 turns.timing.stages（jsonb ISO 字符串 → datetime）
    assert data["stages"][0]["stage_key"] == "plan"
    assert data["stages"][0]["status"] == "done"
    assert data["stages"][0]["duration_ms"] == 3200
    assert data["stages"][0]["label"] == "规划 4 个子课题"
    assert data["stages"][0]["extra"] == {"subtopics": 4}  # §7.2 extra 透传（评审 P2）
    # 推导字段：degraded=status==degraded；export_available=deep 且 completed/degraded
    assert data["degraded"] is False
    assert data["export_available"] is True
    # 分期口径（用户裁决 2026-11-08）：incognito 恒 false（M5 列）/ kb_doc_id 恒
    # null（M3 增列后接真）；memory_hits 不返回
    # （api.md §2.5「M5 及之前无此字段」，评审 P2 勘误——预置字段已移除）
    assert data["incognito"] is False
    assert data["followups"] is None
    assert data["feedback_helpful"] is None  # 未反馈 = null（§2.5 v1.16，REQ-057）
    assert "memory_hits" not in data
    assert data["images"] == []  # 零 images 行→§2.5 重建空数组（turn_service._rebuild_images_briefs）
    assert data["error"] is None
    assert data["usage"]["total_tokens"] is None  # usage={}（M1 简化）
    assert data["duration_ms"] == 152300
    assert data["cancel_reason"] is None
    assert data["created_at"].startswith("2026-11-01T08:12:00")
    assert data["finished_at"].startswith("2026-11-01T08:14:32")


async def test_get_turn_reads_back_followups_and_feedback(overrides: dict) -> None:
    """§2.5 v1.16 读回双源（M2 批次 2）：followups 直读 turns.followups（异步回填值）；
    feedback_helpful 查 turn_feedback（有行=现值，无行=null）。"""
    session = _FakeSession(
        turn=_turn_row(
            mode="fast", followups=["追问一？", "追问二？"], answer_md="答案正文"
        ),
        feedback_rows=[False],
    )
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: _USER_ID
    async with _client() as client:
        response = await client.get(f"/api/v1/turns/{_TURN_ID}")
    data = response.json()["data"]
    assert data["followups"] == ["追问一？", "追问二？"]
    assert data["feedback_helpful"] is False

    # 无反馈行 → null（未反馈，非 false）；无 followups 列值 → null
    session_none = _FakeSession(turn=_turn_row(mode="fast"))
    app.dependency_overrides[get_db] = lambda: session_none
    async with _client() as client:
        payload = (await client.get(f"/api/v1/turns/{_TURN_ID}")).json()["data"]
    assert payload["feedback_helpful"] is None
    assert payload["followups"] is None


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


# ---- POST /api/v1/turns/{turn_id}/regenerate（§2.12 v1.19 / REQ-063）----


async def test_regenerate_success_returns_running_and_schedules_task(
    overrides: dict,
) -> None:
    """200 + data={turn_id,status:"running"}；前置裁决与微图执行分属 service/BackgroundTask。"""
    scheduler = _FakeScheduler()
    app.dependency_overrides[get_current_user] = lambda: _USER_ID
    app.dependency_overrides[get_thread_scheduler] = lambda: scheduler
    async with _client() as client:
        response = await client.post(f"/api/v1/turns/{_TURN_ID}/regenerate")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "0" and body["message"] == "ok"
    assert body["data"] == {"turn_id": _TURN_ID, "status": "running"}
    assert scheduler.regenerate_calls == [{"turn_id": _TURN_ID, "user_id": _USER_ID}]
    assert scheduler.execute_regenerate_calls == [_TURN_ID]  # BackgroundTask 已排程


async def test_regenerate_not_found_404001(overrides: dict) -> None:
    scheduler = _FakeScheduler(exc=NotFoundException("轮次不存在"))
    app.dependency_overrides[get_current_user] = lambda: _USER_ID
    app.dependency_overrides[get_thread_scheduler] = lambda: scheduler
    async with _client() as client:
        response = await client.post(f"/api/v1/turns/{_TURN_ID}/regenerate")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "404001" and body["message"] == "轮次不存在"
    assert scheduler.execute_regenerate_calls == []  # 前置失败不排程


async def test_regenerate_not_regenerable_409006(overrides: dict) -> None:
    scheduler = _FakeScheduler(exc=TurnNotRegenerableException("轮次不支持重新生成"))
    app.dependency_overrides[get_current_user] = lambda: _USER_ID
    app.dependency_overrides[get_thread_scheduler] = lambda: scheduler
    async with _client() as client:
        response = await client.post(f"/api/v1/turns/{_TURN_ID}/regenerate")
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "409006" and body["message"] == "轮次不支持重新生成"


# ---- §2.5 v1.37 approval 投影四形态（B22；expired→approved+decided_by=timeout 映射）----


def _approval_row(**overrides: Any) -> SimpleNamespace:
    base = SimpleNamespace(
        status="expired",
        verdict="has_hit",
        choice="both",
        note=None,
        decided_by="timeout",
        requested_at=datetime(2026, 11, 27, 8, 12, 6, tzinfo=UTC),
        expires_at=datetime(2026, 11, 27, 8, 14, 6, tzinfo=UTC),
        responded_at=datetime(2026, 11, 27, 8, 14, 6, tzinfo=UTC),
        previews=[],
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


async def test_get_turn_approval_expired_maps_to_approved_timeout(
    overrides: dict,
) -> None:
    """expired 审批行 → 2.5 approval.status='approved' + decided_by='timeout'
    （存储/投影命名分歧为契约自身定义：data-model §1.20 status 注 vs api.md §2.5
    decided 形态）；note None 键省略；awaiting_ms 同帧投影。"""
    approval = _approval_row()
    session = _FakeSession(turn=_turn_row(status="completed"), approval_rows=[approval])
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: _USER_ID
    async with _client() as client:
        response = await client.get(f"/api/v1/turns/{_TURN_ID}")
    assert response.status_code == 200
    approval_out = response.json()["data"]["approval"]
    assert approval_out == {
        "status": "approved",
        "choice": "both",
        "decided_by": "timeout",
    }  # note None 键省略（键可省口径）
    assert response.json()["data"]["awaiting_ms"] == 120000


async def test_get_turn_approval_pending_form_with_previews(
    overrides: dict,
) -> None:
    """pending 形态（轮 status=awaiting_approval）：verdict/expires_at/previews 全量
    在 payload（刷新恢复以此为准源重新打开弹层——frontend-api 生命周期 15）。"""
    previews = [
        {
            "kb_doc_id": "9a77",
            "chunk_id": "5c2e",
            "title": "检索融合",
            "heading_path": ["KB", "RRF"],
            "excerpt": "RRF 融合……",
            "query_index": 0,
        }
    ]
    approval = _approval_row(
        status="pending",
        expires_at=datetime(2026, 11, 27, 8, 14, 6, tzinfo=UTC),
        previews=previews,
    )
    session = _FakeSession(
        turn=_turn_row(status="awaiting_approval"), approval_rows=[approval]
    )
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: _USER_ID
    async with _client() as client:
        response = await client.get(f"/api/v1/turns/{_TURN_ID}")
    approval_out = response.json()["data"]["approval"]
    assert approval_out["status"] == "pending"
    assert approval_out["verdict"] == "has_hit"
    assert approval_out["expires_at"] == "2026-11-27T08:14:06+00:00"
    assert approval_out["previews"] == previews


async def test_get_turn_approval_omitted_for_non_gate_turn(
    overrides: dict,
) -> None:
    """非门轮（无审批行）approval/awaiting_ms 键整体省略不置 null（api.md §2.5 键
    可省口径 serializer 弹键；canceled 审批行契约四形态未列——同按省略承载，
    anomalies 上报）。"""
    session = _FakeSession(turn=_turn_row())
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: _USER_ID
    async with _client() as client:
        response = await client.get(f"/api/v1/turns/{_TURN_ID}")
    data = response.json()["data"]
    assert "approval" not in data
    assert "awaiting_ms" not in data
