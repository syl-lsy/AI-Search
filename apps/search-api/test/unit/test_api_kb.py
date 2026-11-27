"""unit 层：kb 域路由（app/api/v1/kb.py，api.md §4.1~§4.5）。

覆盖点（路由→依赖→service 请求链路；service 内部逻辑归 test_kb_service.py）：
- POST /api/v1/kb/documents（§4.1）：multipart 解析 + 201 + 八键 data；
- POST /api/v1/kb/urls（§4.2）：JSON 体 + duplicated；422001 URL 非法；
- GET /api/v1/kb/documents（§4.3）：limit/offset/status/doc_type 过滤转发（非法
  枚举 400001）+ PageResponse 信封；
- GET /api/v1/kb/documents/{id}（§4.4）：404001；
- DELETE /api/v1/kb/documents/{id}（§4.5）：200 {"deleted": true} / 404001；
- BackgroundTasks 入队时序（评审 P1 钉正：Starlette ≥1.10 事件序 handler →
  background → get_db teardown——路由在 add_task 前显式 commit，worker 才读到
  已提交行）；get_kb_service 生产装配接 _default_spec_payload；路由表注册齐全。
外部依赖全替换（dependency_overrides：假 service / 假 pipeline / 假 get_db /
假当前用户）——httpx ASGITransport 零网络。
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.v1 import kb as kb_routes
from app.dependencies import get_current_user, get_db
from app.exception import BadRequestException, NotFoundException
from app.exception.kb import (
    FileTooLargeException,
    FileUnsupportedTypeException,
    KbUrlInvalidException,
)
from app.main import app
from app.schemas.kb import (
    KbDeleteData,
    KbDocument,
    KbDocumentCollected,
    KbDocumentCreated,
)
from app.services.base import PageResult
from app.rag.pipeline import _default_spec_payload, get_kb_pipeline

_USER_ID = "00000000-0000-4000-8000-000000000001"
_NOW = datetime(2026, 11, 23, 8, 0, tzinfo=UTC)


class _FakePipeline:
    def __init__(self, timeline: list[str] | None = None) -> None:
        self.enqueued: list[str] = []
        self._timeline = timeline

    def enqueue(self, doc_id: str) -> None:
        if self._timeline is not None:
            self._timeline.append("enqueue")
        self.enqueued.append(doc_id)


class _FakeDb:
    """上传路由显式提交缝假体（commit 记录供时序断言）。"""

    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline

    async def commit(self) -> None:
        self._timeline.append("commit")


class _FakeKbService:
    def __init__(
        self,
        *,
        created: KbDocumentCreated | None = None,
        collected: KbDocumentCollected | None = None,
        page: PageResult[KbDocument] | None = None,
        detail: KbDocument | None = None,
        delete_result: KbDeleteData | None = None,
        exc: Exception | None = None,
        timeline: list[str] | None = None,
    ) -> None:
        self.upload_calls: list[dict[str, Any]] = []
        self.collect_calls: list[dict[str, Any]] = []
        self.list_calls: list[Any] = []
        self.detail_calls: list[str] = []
        self.delete_calls: list[str] = []
        self._created = created
        self._collected = collected
        self._page = page
        self._detail = detail
        self._delete = delete_result
        self._exc = exc
        self._timeline = timeline if timeline is not None else []
        self.db = _FakeDb(self._timeline)

    async def upload_document(
        self, data: bytes, filename: str, title: str | None, *, user_id: str
    ) -> KbDocumentCreated:
        self._timeline.append("upload")
        self.upload_calls.append(
            {"size": len(data), "filename": filename, "title": title, "user_id": user_id}
        )
        if self._exc:
            raise self._exc
        assert self._created is not None
        return self._created

    async def collect_url(self, url: str, *, user_id: str) -> KbDocumentCollected:
        self._timeline.append("collect")
        self.collect_calls.append({"url": url, "user_id": user_id})
        if self._exc:
            raise self._exc
        assert self._collected is not None
        return self._collected

    async def list_documents(self, query: Any, *, user_id: str) -> PageResult[KbDocument]:
        self.list_calls.append((query, user_id))
        assert self._page is not None
        return self._page

    async def get_document(self, document_id: str, *, user_id: str) -> KbDocument:
        self.detail_calls.append(document_id)
        if self._exc:
            raise self._exc
        assert self._detail is not None
        return self._detail

    async def delete_document(self, document_id: str, *, user_id: str) -> KbDeleteData:
        self.delete_calls.append(document_id)
        if self._exc:
            raise self._exc
        assert self._delete is not None
        return self._delete


def _created() -> KbDocumentCreated:
    return KbDocumentCreated(
        document_id=str(uuid.uuid4()),
        doc_type="md",
        title="示例",
        file_size_bytes=10,
        status="pending",
        fail_reason=None,
        chunk_count=0,
        created_at=_NOW,
    )


def _collected(*, duplicated: bool = False) -> KbDocumentCollected:
    return KbDocumentCollected(
        document_id=str(uuid.uuid4()),
        doc_type="url",
        title="示例文章",
        source_url="https://example.com/article",
        file_size_bytes=None,
        status="pending",
        fail_reason=None,
        chunk_count=0,
        created_at=_NOW,
        duplicated=duplicated,
    )


def _item() -> KbDocument:
    return KbDocument(
        document_id=str(uuid.uuid4()),
        doc_type="md",
        title="示例",
        file_size_bytes=10,
        status="ready",
        fail_reason=None,
        chunk_count=2,
        created_at=_NOW,
        updated_at=_NOW,
        ready_at=_NOW,
        parse_meta={"pages": None, "tables": 1, "code_blocks": 0},
        catalog_ids=None,
        catalog_path=None,
    )


@pytest.fixture
def fake_pipeline() -> _FakePipeline:
    pipeline = _FakePipeline()
    app.dependency_overrides[get_kb_pipeline] = lambda: pipeline
    yield pipeline
    app.dependency_overrides.pop(get_kb_pipeline, None)


@pytest.fixture
def fake_service():
    def _install(service: _FakeKbService) -> _FakeKbService:
        app.dependency_overrides[kb_routes.get_kb_service] = lambda: service
        return service

    yield _install
    app.dependency_overrides.pop(kb_routes.get_kb_service, None)


@pytest.fixture(autouse=True)
def _override_base_deps():
    app.dependency_overrides[get_current_user] = lambda: _USER_ID
    app.dependency_overrides[get_db] = lambda: None
    yield
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_db, None)


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# §4.1 上传
# ---------------------------------------------------------------------------


async def test_upload_created_201_envelope(fake_service, fake_pipeline) -> None:
    service = fake_service(_FakeKbService(created=_created()))
    async with _client() as client:
        resp = await client.post(
            "/api/v1/kb/documents",
            files={"file": ("示例.md", b"# hi", "text/markdown")},
            data={"title": ""},
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["code"] == "0"
    data = body["data"]
    assert set(data) == {
        "document_id", "doc_type", "title", "file_size_bytes", "status",
        "fail_reason", "chunk_count", "created_at",
    }
    assert data["status"] == "pending"
    # BackgroundTasks 入队（ASGITransport 整链驱动：background 在响应后已执行；
    # 路由已在 add_task 前显式 commit——时序回归见
    # test_upload_commits_before_background_enqueue）
    assert fake_pipeline.enqueued == [data["document_id"]]
    assert len(service.upload_calls) == 1
    assert service.upload_calls[0]["filename"] == "示例.md"


async def test_upload_commits_before_background_enqueue(fake_service) -> None:
    """入队时序回归（评审 P1）：Starlette ≥1.10 事件序 handler → background →
    get_db teardown——teardown commit 晚于 background，路由必须在 add_task 前显式
    commit，worker 新 session 才能读到已提交行（否则 process_document 判 "gone"
    静默放弃、文档永停 pending）。记录序 upload → commit → enqueue。"""
    timeline: list[str] = []
    pipeline = _FakePipeline(timeline)
    service = fake_service(_FakeKbService(created=_created(), timeline=timeline))
    app.dependency_overrides[get_kb_pipeline] = lambda: pipeline
    try:
        async with _client() as client:
            resp = await client.post(
                "/api/v1/kb/documents",
                files={"file": ("示例.md", b"# hi", "text/markdown")},
                data={"title": ""},
            )
    finally:
        app.dependency_overrides.pop(get_kb_pipeline, None)
    assert resp.status_code == 201
    assert timeline == ["upload", "commit", "enqueue"]
    assert pipeline.enqueued == [resp.json()["data"]["document_id"]]


def test_get_kb_service_wires_default_spec_provider() -> None:
    """生产装配缝（评审 P1）：get_kb_service 必须接 pipeline._default_spec_payload
    ——B15 SSRF 护栏读 Nacos spec safety.url_guard（含 enabled 单键热更回退门）
    的载体；缺省 lambda:{} 恒落 defaults 种子，护栏配置改不动。"""
    service = kb_routes.get_kb_service(db=None)
    assert service._spec_payload_provider is _default_spec_payload


def test_get_kb_service_wires_url_fetcher() -> None:
    """生产装配缝（评审 P1①）：get_kb_service 必须注入 url_fetcher——api.md §4.2
    「Firecrawl 抓正文后走 4.1 同一流水线」；None 通道使 collect_url 恒抛
    422001「通道不可用」（生产 URL 收藏端到端断裂）。"""
    service = kb_routes.get_kb_service(db=None)
    assert service._url_fetcher is not None


async def test_upload_unsupported_type_error_envelope(fake_service, fake_pipeline) -> None:
    fake_service(_FakeKbService(exc=FileUnsupportedTypeException("不支持的文件类型")))
    async with _client() as client:
        resp = await client.post(
            "/api/v1/kb/documents",
            files={"file": ("app.exe", b"MZ", "application/octet-stream")},
        )
    assert resp.status_code == 415
    body = resp.json()
    assert body["code"] == "415001"
    assert "message" in body


async def test_upload_audio_video_not_supported_yet_message(fake_service, fake_pipeline) -> None:
    fake_service(_FakeKbService(exc=FileUnsupportedTypeException("音视频暂不支持")))
    async with _client() as client:
        resp = await client.post(
            "/api/v1/kb/documents", files={"file": ("song.mp3", b"ID3", "audio/mpeg")}
        )
    assert resp.status_code == 415
    assert resp.json()["code"] == "415001"
    assert "暂不支持" in resp.json()["message"]


async def test_upload_oversize_error_envelope(fake_service, fake_pipeline) -> None:
    fake_service(_FakeKbService(exc=FileTooLargeException("文件超过大小上限")))
    async with _client() as client:
        resp = await client.post(
            "/api/v1/kb/documents", files={"file": ("big.md", b"x" * 8, "text/markdown")}
        )
    assert resp.status_code == 413
    assert resp.json()["code"] == "413001"


# ---------------------------------------------------------------------------
# §4.2 URL 收藏
# ---------------------------------------------------------------------------


async def test_collect_url_new_entry(fake_service, fake_pipeline) -> None:
    service = fake_service(_FakeKbService(collected=_collected()))
    async with _client() as client:
        resp = await client.post(
            "/api/v1/kb/urls", json={"url": "https://example.com/article"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "0"
    data = body["data"]
    assert data["duplicated"] is False
    assert data["source_url"] == "https://example.com/article"
    assert data["file_size_bytes"] is None
    assert service.collect_calls[0]["url"] == "https://example.com/article"


async def test_collect_url_duplicate(fake_service, fake_pipeline) -> None:
    fake_service(_FakeKbService(collected=_collected(duplicated=True)))
    async with _client() as client:
        resp = await client.post(
            "/api/v1/kb/urls", json={"url": "https://example.com/article"}
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["duplicated"] is True


async def test_collect_url_new_entry_commits_then_enqueues(
    fake_service, fake_pipeline
) -> None:
    """入库入队回归（评审 P1②）：新建 URL 行须走 4.1 同一流水线——显式 commit 后
    BackgroundTasks enqueue（时序同上传路由：handler → commit → enqueue；worker
    新 session 才能读到已提交行）。记录序 collect → commit → enqueue。"""
    timeline: list[str] = []
    pipeline = _FakePipeline(timeline)
    fake_service(_FakeKbService(collected=_collected(), timeline=timeline))
    app.dependency_overrides[get_kb_pipeline] = lambda: pipeline
    try:
        async with _client() as client:
            resp = await client.post(
                "/api/v1/kb/urls", json={"url": "https://example.com/article"}
            )
    finally:
        app.dependency_overrides.pop(get_kb_pipeline, None)
    assert resp.status_code == 200
    assert timeline == ["collect", "commit", "enqueue"]
    assert pipeline.enqueued == [resp.json()["data"]["document_id"]]


async def test_collect_url_duplicate_skips_enqueue(fake_service, fake_pipeline) -> None:
    """同 URL 重复收藏（duplicated=true）：返回既有条目零建行——不重复入队
    （既有行已在库/已处理；重复 enqueue 会无谓重跑流水线）。"""
    pipeline = _FakePipeline()
    fake_service(_FakeKbService(collected=_collected(duplicated=True)))
    app.dependency_overrides[get_kb_pipeline] = lambda: pipeline
    try:
        async with _client() as client:
            resp = await client.post(
                "/api/v1/kb/urls", json={"url": "https://example.com/article"}
            )
    finally:
        app.dependency_overrides.pop(get_kb_pipeline, None)
    assert resp.status_code == 200
    assert pipeline.enqueued == []


async def test_collect_url_invalid_422001(fake_service, fake_pipeline) -> None:
    fake_service(_FakeKbService(exc=KbUrlInvalidException("URL 非法", detail="ip_forbidden")))
    async with _client() as client:
        resp = await client.post("/api/v1/kb/urls", json={"url": "https://10.0.0.1/x"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "422001"


async def test_collect_url_missing_body_field_400001(fake_service, fake_pipeline) -> None:
    fake_service(_FakeKbService(collected=_collected()))
    async with _client() as client:
        resp = await client.post("/api/v1/kb/urls", json={})
    assert resp.status_code == 400
    assert resp.json()["code"] == "400001"


# ---------------------------------------------------------------------------
# §4.3 列表 / §4.4 详情
# ---------------------------------------------------------------------------


async def test_list_documents_page_envelope(fake_service, fake_pipeline) -> None:
    fake_service(_FakeKbService(page=PageResult(items=[_item()], total=1, page=1, page_size=50)))
    async with _client() as client:
        resp = await client.get("/api/v1/kb/documents?status=ready&doc_type=md&limit=10&offset=0")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "0"
    data = body["data"]
    assert data["total"] == 1
    item = data["items"][0]
    for key in ("updated_at", "ready_at", "parse_meta", "catalog_ids", "catalog_path"):
        assert key in item  # §4.3 四键恒出现
    query, _uid = _extract_query(fake_service)
    assert query.status == "ready"
    assert query.doc_type == "md"
    assert query.limit == 10
    assert query.offset == 0


def _extract_query(fake_service) -> tuple[Any, Any]:
    service = app.dependency_overrides[kb_routes.get_kb_service]()
    return service.list_calls[0]


async def test_list_documents_bad_status_400001(fake_service, fake_pipeline) -> None:
    fake_service(_FakeKbService(page=PageResult(items=[], total=0, page=1, page_size=50)))
    async with _client() as client:
        resp = await client.get("/api/v1/kb/documents?status=parsing")  # 旧值作废
    assert resp.status_code == 400
    assert resp.json()["code"] == "400001"


async def test_list_documents_empty_status_is_no_filter(fake_service, fake_pipeline) -> None:
    fake_service(_FakeKbService(page=PageResult(items=[], total=0, page=1, page_size=50)))
    async with _client() as client:
        resp = await client.get("/api/v1/kb/documents?status=&doc_type=")
    assert resp.status_code == 200
    query, _ = _extract_query(fake_service)
    assert query.status is None and query.doc_type is None  # 空串=不过滤


async def test_get_document_detail(fake_service, fake_pipeline) -> None:
    item = _item()
    fake_service(_FakeKbService(detail=item))
    async with _client() as client:
        resp = await client.get(f"/api/v1/kb/documents/{item.document_id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["document_id"] == item.document_id


async def test_get_document_404001(fake_service, fake_pipeline) -> None:
    fake_service(_FakeKbService(detail=None, exc=NotFoundException("文档不存在")))
    async with _client() as client:
        resp = await client.get(f"/api/v1/kb/documents/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["code"] == "404001"


# ---------------------------------------------------------------------------
# §4.5 删除
# ---------------------------------------------------------------------------


async def test_delete_document(fake_service, fake_pipeline) -> None:
    fake_service(_FakeKbService(delete_result=KbDeleteData(deleted=True)))
    async with _client() as client:
        resp = await client.delete(f"/api/v1/kb/documents/{uuid.uuid4()}")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"deleted": True}


async def test_delete_document_404001(fake_service, fake_pipeline) -> None:
    fake_service(_FakeKbService(exc=NotFoundException("文档不存在")))
    async with _client() as client:
        resp = await client.delete(f"/api/v1/kb/documents/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["code"] == "404001"


# ---------------------------------------------------------------------------
# 路由表注册
# ---------------------------------------------------------------------------


def test_kb_routes_registered() -> None:
    paths = {route.path for route in app.routes}
    for expected in (
        "/api/v1/kb/documents",
        "/api/v1/kb/urls",
        "/api/v1/kb/documents/{document_id}",
    ):
        assert expected in paths
    methods: dict[str, set[str]] = {}
    for route in app.routes:
        if hasattr(route, "methods") and hasattr(route, "path"):
            methods.setdefault(route.path, set()).update(route.methods)  # type: ignore[attr-defined]
    assert "POST" in methods["/api/v1/kb/documents"]
    assert "GET" in methods["/api/v1/kb/documents"]
    # DELETE 在详情路径（api.md §4.5：DELETE /api/v1/kb/documents/{document_id}——
    # 本测试上方路径注册清单同证）
    assert "DELETE" in methods["/api/v1/kb/documents/{document_id}"]
    assert "POST" in methods["/api/v1/kb/urls"]


def test_kb_router_prefix_shape() -> None:
    assert kb_routes.router.prefix == "/api/v1"


# ---------------------------------------------------------------------------
# §4.6 KB 来源溯源下钻（B19——已冻结契约纯实现批；修订注 2 fs:blk: 取舍）
# ---------------------------------------------------------------------------


class _FakeDrilldownService:
    """假 service：按 source_id 返回 DTO 或抛领域异常（路由链路面）。"""

    def __init__(self, outcomes: dict[str, Any]) -> None:
        self._outcomes = outcomes
        self.calls: list[tuple[str, str]] = []

    async def get_chunk_drilldown(self, source_id: str, *, user_id: str) -> Any:
        self.calls.append((source_id, user_id))
        outcome = self._outcomes.get(source_id)
        if outcome is None:
            raise NotFoundException("来源不存在")  # 与真 service 同语义（不存在/越权）
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _drilldown_dto(**overrides: Any) -> Any:
    from app.schemas.kb import KbChunkDetail

    payload = {
        "chunk_id": "5c2e1111-1111-4111-8111-000000000001",
        "document_id": "9a772222-2222-4222-8222-000000000001",
        "document_title": "内部设计文档-检索融合.md",
        "content": "KB 候选与网络候选合并后统一 rerank...",
        "heading_path": ["第三章 检索融合", "3.2 混合检索"],
        "page": 12,
        "paragraph": 4,
    }
    payload.update(overrides)
    # page/paragraph 恒出现（值可空）——None 也显式 set；可选键 None 不传（unset 省略）
    return KbChunkDetail.model_validate({
        k: v for k, v in payload.items() if v is not None or k in ("page", "paragraph")
    })


async def test_kb_chunk_drilldown_200_six_required_fields(monkeypatch):
    """① 200 六必选逐字段（api.md §4.6 响应示例逐字对齐）+ 本地来源两可选键省略。"""
    dto = _drilldown_dto()
    fake = _FakeDrilldownService({"src-1": dto})
    app.dependency_overrides[kb_routes.get_kb_service] = lambda: fake
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as client:
            response = await client.get(
                "/api/v1/sources/src-1/kb-chunk",
                headers={"X-User-Id": _USER_ID},
            )
    finally:
        app.dependency_overrides.pop(kb_routes.get_kb_service, None)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["chunk_id"] == "5c2e1111-1111-4111-8111-000000000001"
    assert data["document_id"] == "9a772222-2222-4222-8222-000000000001"
    assert data["document_title"] == "内部设计文档-检索融合.md"
    assert data["content"] == "KB 候选与网络候选合并后统一 rerank..."
    assert data["heading_path"] == ["第三章 检索融合", "3.2 混合检索"]
    assert data["page"] == 12
    assert data["paragraph"] == 4
    assert "node_token" not in data and "node_id" not in data
    assert fake.calls == [("src-1", _USER_ID)]


async def test_kb_chunk_drilldown_feishu_fs_node_both_keys(monkeypatch):
    """② 飞书 fs:node: 来源 → node_token + node_id 两可选键齐带（回跳外链消费面）
    + page/paragraph 键恒出现值可空。"""
    dto = _drilldown_dto(
        node_token="tok789", node_id="fs:node:tok789", page=None, paragraph=None
    )
    fake = _FakeDrilldownService({"src-2": dto})
    app.dependency_overrides[kb_routes.get_kb_service] = lambda: fake
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as client:
            response = await client.get(
                "/api/v1/sources/src-2/kb-chunk",
                headers={"X-User-Id": _USER_ID},
            )
    finally:
        app.dependency_overrides.pop(kb_routes.get_kb_service, None)
    data = response.json()["data"]
    assert data["node_token"] == "tok789"
    assert data["node_id"] == "fs:node:tok789"
    assert data["page"] is None and data["paragraph"] is None


async def test_kb_chunk_drilldown_fs_blk_keeps_node_id_only(monkeypatch):
    """② 修订注 2：fs:blk: 来源 → 保留 node_id、省略 node_token（契约字面取舍，
    收官用户裁决候选）。"""
    dto = _drilldown_dto(node_id="fs:blk:blk42")
    fake = _FakeDrilldownService({"src-3": dto})
    app.dependency_overrides[kb_routes.get_kb_service] = lambda: fake
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as client:
            response = await client.get(
                "/api/v1/sources/src-3/kb-chunk",
                headers={"X-User-Id": _USER_ID},
            )
    finally:
        app.dependency_overrides.pop(kb_routes.get_kb_service, None)
    data = response.json()["data"]
    assert data["node_id"] == "fs:blk:blk42"
    assert "node_token" not in data


async def test_kb_chunk_drilldown_non_kb_source_400001(monkeypatch):
    """③ 非 KB 来源（source_type=web）→ 400001。"""
    fake = _FakeDrilldownService(
        {"src-4": BadRequestException("非 KB 来源", detail="source_type=web")}
    )
    app.dependency_overrides[kb_routes.get_kb_service] = lambda: fake
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as client:
            response = await client.get(
                "/api/v1/sources/src-4/kb-chunk",
                headers={"X-User-Id": _USER_ID},
            )
    finally:
        app.dependency_overrides.pop(kb_routes.get_kb_service, None)
    assert response.status_code == 400
    assert response.json()["code"] == "400001"


async def test_kb_chunk_drilldown_404001_single_code(monkeypatch):
    """④ 已删除/越权/不存在 → 404001 单码（不泄漏存在性）。"""
    fake = _FakeDrilldownService(
        {
            "src-5": NotFoundException("来源不存在"),
            "src-6": NotFoundException("chunk 或文档已删除"),
        }
    )
    app.dependency_overrides[kb_routes.get_kb_service] = lambda: fake
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as client:
            for source_id in ("src-5", "src-6", "src-missing"):
                response = await client.get(
                    f"/api/v1/sources/{source_id}/kb-chunk",
                    headers={"X-User-Id": _USER_ID},
                )
                assert response.status_code == 404
                assert response.json()["code"] == "404001"
    finally:
        app.dependency_overrides.pop(kb_routes.get_kb_service, None)


async def test_kb_chunk_drilldown_route_registered():
    """⑤ 路由表注册齐全（api.md §6 端点总表 #39）。"""
    paths = {route.path for route in app.routes}
    assert "/api/v1/sources/{source_id}/kb-chunk" in paths
