"""unit 层：threads 域路由（app/api/v1/threads.py，api.md §2.1~§2.4）。

覆盖点（路由→依赖→service 的请求链路；service 内部逻辑归 test_thread_service.py）：
- POST /threads（§2.1）：建线程+首问轮走完整提交链——模型解析（缺省取 Nacos
  chat.default / 非法键 400001，先于建轮零写库）→ thread_service 建轮(flush) →
  resolve_conflict_on_submit → BackgroundTasks(schedule_turn)（依赖 teardown commit
  后执行，无竞态）；响应 status：PROCEED→running、ENQUEUE/INTERRUPT→queued
  （api.md §2.1 注「running 或 queued」）；
- GET /threads（§2.2）：分页 + mode 过滤参数解析（默认 limit 50/offset 0；limit
  上限 200——超限/非法枚举 400001）；
- GET /threads/{thread_id}（§2.3）：404001（不存在/越权由 service 判定，路由透传）；
- POST /threads/{thread_id}/turns（§2.4）：追问链路同 §2.1；404001/409001；
- 成功信封 {"code":"0","message":"ok"}（/api/ 前缀统一包装）与错误体 {"code","message"}。

外部依赖全替换（dependency_overrides：假 service/假 scheduler/假 mapping/假当前
用户/假 get_db）——httpx ASGITransport 零网络。
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.v1 import threads
from app.dependencies import get_current_user, get_db, get_model_mapping
from app.exception import ConflictException, NotFoundException
from app.main import app
from app.schemas import (
    PageResponse,
    ThreadBrief,
    ThreadCreateResult,
    ThreadDetail,
    ThreadListQuery,
    TurnSkeleton,
)
from app.services.turn_service import SubmitDecision, get_thread_scheduler

_USER_ID = "00000000-0000-0000-0000-0000000000aa"
_THREAD_ID = "7d2f0a1e-0000-0000-0000-000000000001"
_TURN_ID = "c31b9d74-0000-0000-0000-000000000002"

# model.mapping 快照假体（结构同 defaults.py §5 内置默认；别名值对本层不可见——
# 只透传给 service，此处仅断言「键→别名」解析结果进 service 调用面）
_MAPPING: dict[str, Any] = {
    "chat": {
        "options": {
            "qwen": "chat-standard",
            "glm": "chat-standard-glm",
            "deepseek": "chat-standard-deepseek",
        },
        "default": "qwen",
    }
}


class _FakeThreadService:
    """线程域服务假体：记录调用面，按预设返回 DTO 或抛领域异常（零网络零 DB）。"""

    def __init__(
        self,
        *,
        create_exc: Exception | None = None,
        detail_exc: Exception | None = None,
        list_result: PageResponse[ThreadBrief] | None = None,
        detail_result: ThreadDetail | None = None,
    ) -> None:
        self.create_thread_calls: list[dict[str, Any]] = []
        self.create_turn_calls: list[dict[str, Any]] = []
        self.list_calls: list[ThreadListQuery] = []
        self.detail_calls: list[str] = []
        self._create_exc = create_exc
        self._detail_exc = detail_exc
        self._list_result = list_result
        self._detail_result = detail_result

    async def create_thread_with_turn(
        self,
        payload: Any,
        *,
        user_id: str,
        chat_model_key: str,
        model_name: str,
    ) -> ThreadCreateResult:
        self.create_thread_calls.append(
            {
                "query": payload.query,
                "mode": payload.mode,
                "model": payload.model,
                "incognito": payload.incognito,
                "on_conflict": payload.on_conflict,
                "user_id": user_id,
                "chat_model_key": chat_model_key,
                "model_name": model_name,
            }
        )
        if self._create_exc is not None:
            raise self._create_exc
        return ThreadCreateResult(
            thread_id=_THREAD_ID, turn_id=_TURN_ID, status="pending"
        )

    async def create_turn(
        self,
        thread_id: str,
        payload: Any,
        *,
        user_id: str,
        chat_model_key: str,
        model_name: str,
    ) -> ThreadCreateResult:
        self.create_turn_calls.append(
            {
                "thread_id": thread_id,
                "query": payload.query,
                "mode": payload.mode,
                "user_id": user_id,
                "chat_model_key": chat_model_key,
                "model_name": model_name,
            }
        )
        if self._create_exc is not None:
            raise self._create_exc
        return ThreadCreateResult(
            thread_id=_THREAD_ID, turn_id=_TURN_ID, status="pending"
        )

    async def list_threads(
        self, query: ThreadListQuery, *, user_id: str
    ) -> PageResponse[ThreadBrief]:
        self.list_calls.append(query)
        if self._list_result is not None:
            return self._list_result
        return PageResponse(total=0, items=[])

    async def get_thread_detail(self, thread_id: str, *, user_id: str) -> ThreadDetail:
        self.detail_calls.append(thread_id)
        if self._detail_exc is not None:
            raise self._detail_exc
        if self._detail_result is not None:
            return self._detail_result
        raise NotFoundException("线程不存在")


class _FakeScheduler:
    """调度器假体：resolve_conflict_on_submit 按预设返回决策/抛 409001；
    schedule_turn 只记录调用（真实排程语义归 test_turn_service.py）。"""

    def __init__(
        self,
        *,
        decision: SubmitDecision = SubmitDecision.PROCEED,
        resolve_exc: Exception | None = None,
    ) -> None:
        self.resolve_calls: list[dict[str, Any]] = []
        self.schedule_calls: list[str] = []
        self._decision = decision
        self._resolve_exc = resolve_exc

    async def resolve_conflict_on_submit(
        self, session: Any, *, thread_id: str, turn_id: str, on_conflict: str | None
    ) -> SubmitDecision:
        self.resolve_calls.append(
            {"thread_id": thread_id, "turn_id": turn_id, "on_conflict": on_conflict}
        )
        if self._resolve_exc is not None:
            raise self._resolve_exc
        return self._decision

    async def schedule_turn(self, turn_id: str) -> None:
        self.schedule_calls.append(turn_id)


@pytest.fixture
def overrides() -> Iterator[dict[Any, Any]]:
    """dependency_overrides 生命周期：用例自装，结束统一清空（不污染其他用例）。"""
    yield app.dependency_overrides
    app.dependency_overrides.clear()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _wire(
    service: _FakeThreadService,
    scheduler: _FakeScheduler,
    *,
    mapping: Any = _MAPPING,
) -> None:
    """装四类依赖假体：get_db / get_current_user / get_model_mapping / scheduler / service。"""
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: _USER_ID
    app.dependency_overrides[get_model_mapping] = lambda: mapping
    app.dependency_overrides[get_thread_scheduler] = lambda: scheduler
    app.dependency_overrides[threads.get_thread_service] = lambda: service


def _brief() -> ThreadBrief:
    moment = datetime(2026, 11, 1, 8, 12, tzinfo=UTC)
    return ThreadBrief(
        thread_id=_THREAD_ID,
        title="LangGraph 的 Send API 和 Router 的区别？",
        mode_summary="mixed",
        turn_count=3,
        created_at=moment,
        updated_at=moment,
    )


# ---- POST /api/v1/threads（§2.1 建线程+首问轮）----


async def test_post_threads_success_proceed_returns_running(overrides: dict) -> None:
    service = _FakeThreadService()
    scheduler = _FakeScheduler(decision=SubmitDecision.PROCEED)
    _wire(service, scheduler)
    async with _client() as client:
        response = await client.post(
            "/api/v1/threads",
            json={"query": "LangGraph 的 Send API 和 Router 的区别？", "mode": "fast"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "0" and body["message"] == "ok"
    # data.status：PROCEED=立即执行 running（api.md §2.1 响应示例）
    assert body["data"] == {
        "thread_id": _THREAD_ID,
        "turn_id": _TURN_ID,
        "status": "running",
    }
    # 模型解析缺省取 chat.default，键与别名一并进 service（A10 落 turns.model_name）
    call = service.create_thread_calls[0]
    assert call["chat_model_key"] == "qwen"
    assert call["model_name"] == "chat-standard"
    assert call["user_id"] == _USER_ID
    # 策略链照走（新线程无并发冲突）：on_conflict 缺省 None（快照值在 scheduler 内取）
    assert scheduler.resolve_calls == [
        {
            "thread_id": _THREAD_ID,
            "turn_id": _TURN_ID,
            "on_conflict": None,
        }
    ]
    # BackgroundTask 排程在响应后执行（ASGITransport 走完整生命周期）
    assert scheduler.schedule_calls == [_TURN_ID]


async def test_post_threads_explicit_model_and_enqueue_returns_queued(
    overrides: dict,
) -> None:
    service = _FakeThreadService()
    scheduler = _FakeScheduler(decision=SubmitDecision.ENQUEUE)
    _wire(service, scheduler)
    async with _client() as client:
        response = await client.post(
            "/api/v1/threads",
            json={
                "query": "深度调研多智能体框架",
                "mode": "deep",
                "model": "glm",
                "on_conflict": "enqueue",
            },
        )
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "queued"
    call = service.create_thread_calls[0]
    assert call["model"] == "glm" and call["chat_model_key"] == "glm"
    assert call["model_name"] == "chat-standard-glm"
    assert scheduler.resolve_calls[0]["on_conflict"] == "enqueue"


async def test_post_threads_interrupt_decision_returns_queued(overrides: dict) -> None:
    service = _FakeThreadService()
    scheduler = _FakeScheduler(decision=SubmitDecision.INTERRUPT)
    _wire(service, scheduler)
    async with _client() as client:
        response = await client.post(
            "/api/v1/threads",
            json={"query": "打断当前轮", "mode": "fast", "on_conflict": "interrupt"},
        )
    assert response.status_code == 200
    # interrupt：新轮先置 queued 等旧轮 canceled 后启动（响应 status=queued）
    assert response.json()["data"]["status"] == "queued"


async def test_post_threads_invalid_model_key_400001(overrides: dict) -> None:
    service = _FakeThreadService()
    scheduler = _FakeScheduler()
    _wire(service, scheduler)
    async with _client() as client:
        response = await client.post(
            "/api/v1/threads",
            json={"query": "非法模型键", "mode": "fast", "model": "bad-key"},
        )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "400001"
    # 400001 先于建轮（模型解析在 REST 建轮前调用）——零写库零排程
    assert service.create_thread_calls == []
    assert scheduler.schedule_calls == []


async def test_post_threads_body_validation_400001(overrides: dict) -> None:
    service = _FakeThreadService()
    scheduler = _FakeScheduler()
    _wire(service, scheduler)
    async with _client() as client:
        empty_query = await client.post(
            "/api/v1/threads", json={"query": "", "mode": "fast"}
        )
        missing_mode = await client.post(
            "/api/v1/threads", json={"query": "缺 mode"}
        )
        bad_on_conflict = await client.post(
            "/api/v1/threads",
            json={"query": "非法策略枚举", "mode": "fast", "on_conflict": "fallback"},
        )
    for response in (empty_query, missing_mode, bad_on_conflict):
        assert response.status_code == 400
        assert response.json()["code"] == "400001"
    assert service.create_thread_calls == []


async def test_post_threads_images_over_limit_400001(overrides: dict) -> None:
    service = _FakeThreadService()
    scheduler = _FakeScheduler()
    _wire(service, scheduler)
    async with _client() as client:
        response = await client.post(
            "/api/v1/threads",
            json={
                "query": "图片超限",
                "mode": "fast",
                "images": ["data:image/png;base64,x"] * 4,
            },
        )
    assert response.status_code == 400
    assert response.json()["code"] == "400001"


async def test_post_threads_incognito_accepted_not_dropped(overrides: dict) -> None:
    """incognito 分期口径（用户裁决 2026-11-08）：请求侧接收并校验、不落库——
    路由把完整 payload 交给 service（落库裁剪在 service/迁移分期）。"""
    service = _FakeThreadService()
    scheduler = _FakeScheduler()
    _wire(service, scheduler)
    async with _client() as client:
        response = await client.post(
            "/api/v1/threads",
            json={"query": "无痕轮", "mode": "fast", "incognito": True},
        )
    assert response.status_code == 200
    assert service.create_thread_calls[0]["incognito"] is True


async def test_post_threads_reject_409001(overrides: dict) -> None:
    service = _FakeThreadService()
    scheduler = _FakeScheduler(resolve_exc=ConflictException("线程已有进行中轮次"))
    _wire(service, scheduler)
    async with _client() as client:
        response = await client.post(
            "/api/v1/threads", json={"query": "并发拒绝", "mode": "fast"}
        )
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "409001" and body["message"] == "线程已有进行中轮次"
    # 409001 在建轮之后抛出（请求事务由 get_db 回滚，pending 轮零残留——真实事务
    # 语义归 integration；此处断言排程未发生）
    assert scheduler.schedule_calls == []


# ---- GET /api/v1/threads（§2.2 线程列表）----


async def test_get_threads_list_success_with_filters(overrides: dict) -> None:
    service = _FakeThreadService(list_result=PageResponse(total=1, items=[_brief()]))
    scheduler = _FakeScheduler()
    _wire(service, scheduler)
    async with _client() as client:
        response = await client.get(
            "/api/v1/threads", params={"limit": 2, "offset": 4, "mode": "fast"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "0" and body["message"] == "ok"
    assert body["data"]["total"] == 1
    assert body["data"]["items"][0]["thread_id"] == _THREAD_ID
    assert body["data"]["items"][0]["mode_summary"] == "mixed"
    query = service.list_calls[0]
    assert (query.limit, query.offset, query.mode) == (2, 4, "fast")


async def test_get_threads_list_default_pagination(overrides: dict) -> None:
    service = _FakeThreadService()
    scheduler = _FakeScheduler()
    _wire(service, scheduler)
    async with _client() as client:
        await client.get("/api/v1/threads")
    query = service.list_calls[0]
    assert (query.limit, query.offset, query.mode) == (50, 0, None)


async def test_get_threads_list_limit_upper_bound_200(overrides: dict) -> None:
    """分页边界（api.md §0：limit 上限 200）——limit=200 合法、limit=201 → 400001。"""
    service = _FakeThreadService()
    scheduler = _FakeScheduler()
    _wire(service, scheduler)
    async with _client() as client:
        ok = await client.get("/api/v1/threads", params={"limit": 200})
        over = await client.get("/api/v1/threads", params={"limit": 201})
    assert ok.status_code == 200
    assert service.list_calls[0].limit == 200
    assert over.status_code == 400
    assert over.json()["code"] == "400001"


async def test_get_threads_list_invalid_mode_400001(overrides: dict) -> None:
    service = _FakeThreadService()
    scheduler = _FakeScheduler()
    _wire(service, scheduler)
    async with _client() as client:
        response = await client.get("/api/v1/threads", params={"mode": "archived"})
    assert response.status_code == 400
    assert response.json()["code"] == "400001"


# ---- GET /api/v1/threads/{thread_id}（§2.3 线程详情）----


def _detail_result() -> ThreadDetail:
    moment = datetime(2026, 11, 1, 8, 12, tzinfo=UTC)
    return ThreadDetail(
        thread_id=_THREAD_ID,
        title="对比调研",
        mode_summary="deep",
        turn_count=1,
        created_at=moment,
        updated_at=moment,
        turns=[
            TurnSkeleton(
                turn_id=_TURN_ID,
                mode="deep",
                status="completed",
                query="对比 LangGraph 与 CrewOS",
                query_lang="zh",
                has_report=True,
                degraded=False,
                duration_ms=152300,
                created_at=moment,
            )
        ],
    )


async def test_get_thread_detail_success(overrides: dict) -> None:
    service = _FakeThreadService(detail_result=_detail_result())
    scheduler = _FakeScheduler()
    _wire(service, scheduler)
    async with _client() as client:
        response = await client.get(f"/api/v1/threads/{_THREAD_ID}")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "0" and body["message"] == "ok"
    data = body["data"]
    assert data["thread_id"] == _THREAD_ID
    assert data["has_artifacts"] is False  # 分期口径：0002-M2 起真实值
    assert data["turns"][0]["has_report"] is True
    assert data["turns"][0]["degraded"] is False
    assert service.detail_calls == [_THREAD_ID]


async def test_get_thread_detail_not_found_404001(overrides: dict) -> None:
    service = _FakeThreadService(detail_exc=NotFoundException("线程不存在"))
    scheduler = _FakeScheduler()
    _wire(service, scheduler)
    async with _client() as client:
        response = await client.get(f"/api/v1/threads/{uuid.uuid4()}")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "404001" and body["message"] == "线程不存在"


# ---- POST /api/v1/threads/{thread_id}/turns（§2.4 追问）----


async def test_post_turns_success_proceed(overrides: dict) -> None:
    service = _FakeThreadService()
    scheduler = _FakeScheduler(decision=SubmitDecision.PROCEED)
    _wire(service, scheduler)
    async with _client() as client:
        response = await client.post(
            f"/api/v1/threads/{_THREAD_ID}/turns",
            json={"query": "追问：Send 的适用边界？", "mode": "fast"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "0" and body["message"] == "ok"
    assert body["data"] == {
        "thread_id": _THREAD_ID,
        "turn_id": _TURN_ID,
        "status": "running",
    }
    call = service.create_turn_calls[0]
    assert call["thread_id"] == _THREAD_ID
    assert call["chat_model_key"] == "qwen"  # 缺省取 Nacos chat.default
    assert scheduler.schedule_calls == [_TURN_ID]


async def test_post_turns_thread_not_found_404001(overrides: dict) -> None:
    service = _FakeThreadService(create_exc=NotFoundException("线程不存在"))
    scheduler = _FakeScheduler()
    _wire(service, scheduler)
    async with _client() as client:
        response = await client.post(
            f"/api/v1/threads/{uuid.uuid4()}/turns",
            json={"query": "线程没了", "mode": "fast"},
        )
    assert response.status_code == 404
    assert response.json()["code"] == "404001"
    assert scheduler.schedule_calls == []


async def test_post_turns_conflict_409001(overrides: dict) -> None:
    service = _FakeThreadService()
    scheduler = _FakeScheduler(resolve_exc=ConflictException("线程已有进行中轮次"))
    _wire(service, scheduler)
    async with _client() as client:
        response = await client.post(
            f"/api/v1/threads/{_THREAD_ID}/turns",
            json={"query": "double-texting", "mode": "fast"},
        )
    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "409001" and body["message"] == "线程已有进行中轮次"
    assert scheduler.schedule_calls == []
