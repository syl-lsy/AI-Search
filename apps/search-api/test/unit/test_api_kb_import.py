"""unit 层：kb 导入任务路由 + 业务层（api.md §4.7~4.9，B20 B 线④）。

覆盖点（路由→依赖→service 请求链路；worker 内部逻辑归 test_kb_import_worker.py）：
- POST /api/v1/kb/import/jobs（§4.7）：connector 非法 400001 / 缺体 400001 /
  进行中 409007 / 凭据缺失 500001（§0 兜底语义——裁决 #8 凭据只进 .env）/ 成功
  200 {job_id, status:"pending"} + 显式 commit 先于入队（评审 P1 时序钉正同款）；
- GET /api/v1/kb/import/jobs（§4.8）：?limit=50&offset=0 默认与转发（§0 分页约定）+
  created_at DESC + items 同详情条目**不含 failures** + total；
- GET /api/v1/kb/import/jobs/{job_id}（§4.9）：全键（trigger/status 四态/五数/
  failure_summary/failures 四键条目/时间戳）+ 404001（不存在与越权同码）；
- 投影面：failure_summary 由 failures 派生不落列（G4）；五数恒等式
  processed=imported+failed+skipped；
- 错误码注册：409007 进 ERROR_MAP（§0 冻结表——实现批注册 errors.py）。
外部依赖全替换（dependency_overrides：假 service / 假 worker / 假 get_db / 假当前
用户）——httpx ASGITransport 零网络。
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.v1 import kb as kb_routes
from app.dependencies import get_current_user, get_db
from app.exception import NotFoundException
from app.exception.handler.errors import ERROR_MAP
from app.exception.kb import (
    FeishuNotConfiguredException,
    KbImportJobActiveException,
)
from app.main import app
from app.rag.import_service import KbImportJobService, to_job_detail, to_job_entry
from app.rag.import_worker import get_import_worker
from app.schemas.kb import (
    KbImportJobCreated,
    KbImportJobDetail,
    KbImportJobEntry,
    KbImportFailure,
)
from app.services.base import PageResult

_USER_ID = "00000000-0000-4000-8000-000000000001"
_NOW = datetime(2026, 11, 23, 8, 0, tzinfo=UTC)
_JOB_ID = "3d81c0de-0000-4000-8000-00000000d0a1"


# ---------------------------------------------------------------------------
# 假体（service / worker / db）
# ---------------------------------------------------------------------------


class _FakeDb:
    """建行路由显式提交缝假体（commit 记录供时序断言）。"""

    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline

    async def commit(self) -> None:
        self._timeline.append("commit")


class _FakeImportService:
    def __init__(
        self,
        *,
        created: KbImportJobCreated | None = None,
        page: PageResult[KbImportJobEntry] | None = None,
        detail: KbImportJobDetail | None = None,
        exc: Exception | None = None,
        timeline: list[str] | None = None,
    ) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []
        self.detail_calls: list[str] = []
        self._created = created or KbImportJobCreated(job_id=_JOB_ID, status="pending")
        self._page = page
        self._detail = detail
        self._exc = exc
        self._timeline = timeline if timeline is not None else []
        self.db = _FakeDb(self._timeline)

    async def create_job(
        self, connector: str, *, user_id: str
    ) -> KbImportJobCreated:
        self._timeline.append("create")
        self.create_calls.append({"connector": connector, "user_id": user_id})
        if self._exc:
            raise self._exc
        return self._created

    async def list_jobs(
        self, limit: int, offset: int, *, user_id: str
    ) -> PageResult[KbImportJobEntry]:
        self.list_calls.append({"limit": limit, "offset": offset, "user_id": user_id})
        assert self._page is not None
        return self._page

    async def get_job(self, job_id: str, *, user_id: str) -> KbImportJobDetail:
        self.detail_calls.append(job_id)
        if self._exc:
            raise self._exc
        assert self._detail is not None
        return self._detail


class _FakeWorker:
    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline
        self.enqueued: list[str] = []

    def enqueue(self, job_id: str) -> None:
        self._timeline.append("enqueue")
        self.enqueued.append(job_id)


def _override(
    monkeypatch: pytest.MonkeyPatch,
    *,
    service: _FakeImportService,
    worker: _FakeWorker,
) -> None:
    app.dependency_overrides[get_current_user] = lambda: _USER_ID
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[kb_routes.get_kb_import_service] = lambda: service
    app.dependency_overrides[get_import_worker] = lambda: worker
    monkeypatch.setattr(
        kb_routes, "_require_feishu_credentials", lambda: None
    )  # 凭据面单独用例打开


def _clear_overrides() -> None:
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(kb_routes.get_kb_import_service, None)
    app.dependency_overrides.pop(get_import_worker, None)


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# POST /api/v1/kb/import/jobs（§4.7）
# ---------------------------------------------------------------------------


async def test_post_creates_job_and_enqueues_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功触发：200 {job_id, status:"pending"}；入队时序=显式 commit 先于 enqueue
    （Starlette ≥1.10 事件序 handler → background → teardown——worker 新 session
    须读到已提交行，评审 P1 钉正同款）。"""
    timeline: list[str] = []
    service = _FakeImportService(timeline=timeline)
    worker = _FakeWorker(timeline)
    _override(monkeypatch, service=service, worker=worker)
    try:
        async with _client() as client:
            resp = await client.post(
                "/api/v1/kb/import/jobs", json={"connector": "feishu"}
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == "0"
        assert body["data"] == {"job_id": _JOB_ID, "status": "pending"}
        assert service.create_calls == [{"connector": "feishu", "user_id": _USER_ID}]
        assert timeline == ["create", "commit", "enqueue"]  # commit 先于 enqueue
        assert worker.enqueued == [_JOB_ID]
    finally:
        _clear_overrides()


async def test_post_rejects_non_feishu_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connector 非法 → 400001（M3 仅 feishu 合法——连接器扩展沿此键位）。"""
    service = _FakeImportService()
    _override(monkeypatch, service=service, worker=_FakeWorker([]))
    try:
        async with _client() as client:
            resp = await client.post(
                "/api/v1/kb/import/jobs", json={"connector": "notion"}
            )
        assert resp.status_code == 400
        assert resp.json()["code"] == "400001"
        assert service.create_calls == []
    finally:
        _clear_overrides()


async def test_post_missing_body_maps_to_400001(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """请求体缺失 → RequestValidationError → 400001（信封语义）。"""
    service = _FakeImportService()
    _override(monkeypatch, service=service, worker=_FakeWorker([]))
    try:
        async with _client() as client:
            resp = await client.post("/api/v1/kb/import/jobs")
        assert resp.status_code == 400
        assert resp.json()["code"] == "400001"
    finally:
        _clear_overrides()


async def test_post_conflict_when_active_job_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已有进行中任务 → 409007（§0 冻结表；沿 409004 同族语义）。"""
    service = _FakeImportService(exc=KbImportJobActiveException("已有进行中的知识库导入任务"))
    _override(monkeypatch, service=service, worker=_FakeWorker([]))
    try:
        async with _client() as client:
            resp = await client.post(
                "/api/v1/kb/import/jobs", json={"connector": "feishu"}
            )
        assert resp.status_code == 409
        assert resp.json()["code"] == "409007"
        assert resp.json()["message"] == "已有进行中的知识库导入任务"
    finally:
        _clear_overrides()


async def test_post_missing_credentials_maps_to_500001(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """凭据缺失 → 500001 兜底（api.md §4.7——凭据只进 .env 禁入 Nacos，裁决 #8）；
    业务异常出口（非裸 Exception——避免 ERROR traceback 噪音，铁律 8）。"""
    app.dependency_overrides[get_current_user] = lambda: _USER_ID
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[kb_routes.get_kb_import_service] = (
        lambda: _FakeImportService()
    )
    app.dependency_overrides[get_import_worker] = lambda: _FakeWorker([])
    # 凭据缺失注入（本机 .env 或已配置真实凭据——守卫面用空值直判，不依赖环境）
    monkeypatch.setattr(
        kb_routes,
        "_require_feishu_credentials",
        lambda: kb_routes._require_feishu_credentials_with("", ""),
    )
    try:
        async with _client() as client:
            resp = await client.post(
                "/api/v1/kb/import/jobs", json={"connector": "feishu"}
            )
        assert resp.status_code == 500
        assert resp.json()["code"] == "500001"
    finally:
        _clear_overrides()


def test_error_map_registers_409007_and_500001_faces() -> None:
    """§0 冻结错误码实现批注册：409007（导入任务进行中）+ 500001（凭据缺失兜底面）。"""
    assert ERROR_MAP[KbImportJobActiveException] == (
        "409007",
        409,
        "已有进行中的知识库导入任务",
    )
    assert ERROR_MAP[FeishuNotConfiguredException][:2] == ("500001", 500)


# ---------------------------------------------------------------------------
# GET 列表 / 详情（§4.8 / §4.9）
# ---------------------------------------------------------------------------


def _entry(*, with_failures: bool = False) -> KbImportJobEntry:
    failures = [
        KbImportFailure(
            title="调研 mindnote",
            category="unsupported_type",
            message="mindnote 无读取 API，已跳过",
            document_id=None,
        )
    ]
    kwargs: dict[str, Any] = dict(
        job_id=_JOB_ID,
        connector="feishu",
        trigger="manual",
        status="running",
        total_docs=931,
        processed_docs=312,
        imported_docs=306,
        failed_docs=4,
        skipped_docs=2,
        failure_summary=[{"category": "block_fetch_failed", "count": 3}],
        started_at=_NOW,
        finished_at=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    if with_failures:
        return KbImportJobDetail(**kwargs, failures=failures)
    return KbImportJobEntry(**kwargs)


def _page_payload() -> PageResult[KbImportJobEntry]:
    return PageResult(items=[_entry()], total=1, page=1, page_size=50)


async def test_list_jobs_forwards_pagination_and_omits_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4.8：默认 ?limit=50&offset=0 转发（§0 分页约定）+ created_at DESC 由 service
    承担 + items 不含 failures（失败清单随详情返回）+ total 在位。"""
    service = _FakeImportService(page=_page_payload())
    _override(monkeypatch, service=service, worker=_FakeWorker([]))
    try:
        async with _client() as client:
            resp = await client.get("/api/v1/kb/import/jobs")
        assert resp.status_code == 200
        body = resp.json()["data"]
        assert body["total"] == 1
        (item,) = body["items"]
        assert "failures" not in item  # 列表不含失败清单
        assert item["job_id"] == _JOB_ID
        assert service.list_calls == [{"limit": 50, "offset": 0, "user_id": _USER_ID}]
    finally:
        _clear_overrides()


async def test_list_jobs_forwards_explicit_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _FakeImportService(page=_page_payload())
    _override(monkeypatch, service=service, worker=_FakeWorker([]))
    try:
        async with _client() as client:
            resp = await client.get("/api/v1/kb/import/jobs?limit=10&offset=20")
        assert service.list_calls[0]["limit"] == 10
        assert service.list_calls[0]["offset"] == 20
    finally:
        _clear_overrides()


async def test_detail_returns_full_entry_with_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4.9 详情：failures 四键条目在位（仅详情端点返回）；键面同契约示例。"""
    service = _FakeImportService(detail=_entry(with_failures=True))
    _override(monkeypatch, service=service, worker=_FakeWorker([]))
    try:
        async with _client() as client:
            resp = await client.get(f"/api/v1/kb/import/jobs/{_JOB_ID}")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["job_id"] == _JOB_ID
        assert data["failures"][0]["category"] == "unsupported_type"
        assert data["failures"][0]["document_id"] is None
        assert service.detail_calls == [_JOB_ID]
    finally:
        _clear_overrides()


async def test_detail_404001_for_missing_or_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """404001：任务不存在或不属于当前用户同码（越权不泄漏存在性——§4.9 错误行）。"""
    service = _FakeImportService(exc=NotFoundException("任务不存在"))
    _override(monkeypatch, service=service, worker=_FakeWorker([]))
    try:
        async with _client() as client:
            resp = await client.get(f"/api/v1/kb/import/jobs/{uuid.uuid4()}")
        assert resp.status_code == 404
        assert resp.json()["code"] == "404001"
    finally:
        _clear_overrides()


def test_routes_registered() -> None:
    """路由表：三个导入端点已注册（api.md §4.7~4.9）。"""
    paths = {route.path for route in app.routes}
    assert "/api/v1/kb/import/jobs" in paths
    assert "/api/v1/kb/import/jobs/{job_id}" in paths


# ---------------------------------------------------------------------------
# 投影面（service 纯函数——行形态假体零 DB）
# ---------------------------------------------------------------------------


def _job_row(**over: Any) -> Any:
    base: dict[str, Any] = {
        "id": uuid.UUID(_JOB_ID),
        "connector": "feishu",
        "trigger": "scheduled",
        "status": "completed",
        "progress": {
            "total_docs": 10,
            "processed_docs": 10,
            "imported_docs": 7,
            "failed_docs": 1,
            "skipped_docs": 2,
        },
        "failures": [
            {"title": "a", "category": "unsupported_type", "message": "m1", "document_id": None},
            {"title": "b", "category": "block_fetch_failed", "message": "m2", "document_id": None},
            {"title": "c", "category": "unsupported_type", "message": "m3", "document_id": None},
        ],
        "stats": {"deleted_docs": 3},
        "started_at": _NOW,
        "finished_at": _NOW,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(over)
    return type("Row", (), base)()


def test_to_job_detail_projects_five_counts_and_identity() -> None:
    """五数投影一一对应（data-model §1.17 progress 键位镜像）+ 恒等式
    processed = imported + failed + skipped（api.md §4.9）。"""
    detail = to_job_detail(_job_row())
    assert detail.total_docs == 10
    assert detail.processed_docs == 10
    assert detail.processed_docs == detail.imported_docs + detail.failed_docs + detail.skipped_docs
    assert detail.trigger == "scheduled"
    assert detail.status == "completed"


def test_failure_summary_derived_not_stored() -> None:
    """failure_summary 由 failures 按 category 派生（G4 不落列）——skip 类条目
    同样计入，首见序。"""
    detail = to_job_detail(_job_row())
    assert detail.failure_summary == [
        {"category": "unsupported_type", "count": 2},
        {"category": "block_fetch_failed", "count": 1},
    ]


def test_to_job_entry_excludes_failures_but_keeps_summary() -> None:
    """列表条目 = 详情条目不含 failures（api.md §4.8「同 4.9 详情条目、不含 failures」）。"""
    entry = to_job_entry(_job_row())
    assert not hasattr(entry, "failures")
    assert entry.failure_summary == [
        {"category": "unsupported_type", "count": 2},
        {"category": "block_fetch_failed", "count": 1},
    ]


def test_service_create_job_raises_conflict_on_active() -> None:
    """service 建行：进行中（pending/running）存在 → 409007（user 隔离判定）。"""
    seen: dict[str, Any] = {}

    class _Db:
        async def scalar(self, stmt: Any) -> int:
            seen["stmt"] = stmt
            return 1  # 已有进行中

    import asyncio

    service = KbImportJobService(_Db())  # type: ignore[arg-type]
    with pytest.raises(KbImportJobActiveException):
        asyncio.run(service.create_job("feishu", user_id=_USER_ID))


def test_service_missing_credentials_guard() -> None:
    """凭据缺失守卫：app_id/secret 均空 → FeishuNotConfiguredException（500001 面）。"""
    with pytest.raises(FeishuNotConfiguredException):
        kb_routes._require_feishu_credentials_with(
            app_id="", app_secret=""
        )


async def test_list_jobs_limit_out_of_range_maps_400001(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P3：limit/offset 越界走 FastAPI Query 校验通道 → 400001（handler 体内手动
    实例化 KbDocumentListQuery 会触发未注册 ERROR_MAP 的 ValidationError → 兜底
    500001 而非 400001）。"""
    service = _FakeImportService(page=_page_payload())
    _override(monkeypatch, service=service, worker=_FakeWorker([]))
    try:
        async with _client() as client:
            resp = await client.get("/api/v1/kb/import/jobs?limit=500")
            assert resp.status_code == 400
            assert resp.json()["code"] == "400001"
            resp = await client.get("/api/v1/kb/import/jobs?limit=0")
            assert resp.status_code == 400
            assert resp.json()["code"] == "400001"
            resp = await client.get("/api/v1/kb/import/jobs?offset=-1")
            assert resp.status_code == 400
            assert resp.json()["code"] == "400001"
    finally:
        _clear_overrides()


def test_create_job_admission_lock_serializes_check_then_act() -> None:
    """P3 原子性：同用户并发双击 → per-user admission 锁串行化 check-then-act——
    第二请求在首请求建行后读 active=1 → 409007（无锁则两请求都读到 0 → 双 pending
    行，串行 worker 下产生重复导入与删除/重建竞态）。"""
    import asyncio

    state = {"created": 0}

    class _Db:
        async def scalar(self, stmt: Any) -> int:
            return state["created"]  # 建行后 active=1（模拟已提交可见）

        def add(self, row: Any) -> None:
            pass

        async def flush(self) -> None:
            await asyncio.sleep(0)  # 让出事件循环——并发交错面
            state["created"] += 1

    async def _create() -> Any:
        service = KbImportJobService(_Db())  # type: ignore[arg-type]
        return await service.create_job("feishu", user_id=_USER_ID)

    async def _drive() -> tuple[int, int]:
        outcomes = await asyncio.gather(_create(), _create(), return_exceptions=True)
        ok = sum(1 for r in outcomes if not isinstance(r, Exception))
        conflicts = sum(
            1 for r in outcomes if isinstance(r, KbImportJobActiveException)
        )
        return ok, conflicts

    ok, conflicts = asyncio.run(_drive())
    assert (ok, conflicts) == (1, 1)  # 恰一建行、一冲突
    assert state["created"] == 1
