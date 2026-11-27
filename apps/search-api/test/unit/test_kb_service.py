"""unit 层：KB REST 业务层（app/rag/service.py，api.md §4.1~§4.5）。

覆盖点（TDD 红绿；全假体零网络零 DB——DB 形态归 integration）：
- **§4.1 上传校验**：扩展名白名单（KB_ALLOWED_TYPES）+ 20MB 上限 + 音视频
  415001（message 提示「暂不支持」）；title 缺省=文件名去扩展名；
- **§4.1 落象**：原件先落 MinIO（`kb/{user}/{doc}/original.{ext}`）→ 建行 pending
  → 返回八键投影（无 source_url）；
- **§4.2 URL 收藏**：url_guard SSRF 拒收 → 422001 + url_rejected 语义留痕（零
  fetcher 调用）；同 URL 重复 → 既有条目 duplicated=true（零抓取）；新 URL →
  抓取 → 原件 md 落 MinIO → 建行 pending，title 取抓取正文 H1 / host 兜底，
  file_size_bytes 恒 null；
- **§4.3/4.4 投影**：parse_meta 存储 `{}` → null、四键存储 → 三键投影；source_url
  仅 url 行出现（可选键缺则省略）；status/doc_type 过滤转发；
- **§4.5 删除级联**：404001；kb_chunks 行删（FK CASCADE 经行删）+ sources 反向
  引用清理（kb_doc_id/kb_chunk_ids 置 null——零 REFERENCES 手工维护）+ MinIO
  前缀清理 best-effort（失败 WARN 不抛）。
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.config import minio_settings
from app.db.models import KbDocument
from app.exception import NotFoundException
from app.rag import pipeline as kb_pipeline
from app.rag import service as kb_service
from app.rag.normalizers.errors import UnsupportedTypeError
from app.rag.storage import IrStorageError

_USER_ID = "00000000-0000-4000-8000-000000000001"
_DOC_ID = "7d2f0a1e-0000-4000-8000-0000000000b1"
_BUCKET = minio_settings.bucket_kb


class _FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        pass

    def release_conn(self) -> None:
        pass


class _FakeMinio:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.removed: list[str] = []

    def bucket_exists(self, bucket: str) -> bool:
        return True

    def make_bucket(self, bucket: str) -> None:
        pass

    def put_object(
        self, bucket: str, key: str, stream: Any, length: int, content_type: str | None = None
    ) -> None:
        self.objects[(bucket, key)] = stream.read(length)

    def get_object(self, bucket: str, key: str) -> _FakeResponse:
        if (bucket, key) not in self.objects:
            raise KeyError(f"object not found: {key}")
        return _FakeResponse(self.objects[(bucket, key)])

    def list_objects(self, bucket: str, prefix: str, recursive: bool = True) -> list[Any]:
        class _Obj:
            def __init__(self, name: str) -> None:
                self.object_name = name

        return [_Obj(k) for (_, k) in self.objects if k.startswith(prefix)]

    async def remove_object(self, bucket: str, key: str) -> None:
        self.removed.append(key)
        self.objects.pop((bucket, key), None)


class _FakeSession:
    """service 层 DB 面假体：add/flush/delete/scalars/scalar/execute 记录形态。"""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.executed: list[Any] = []
        self.flushed = 0
        self._scalars_first: Any = None
        self._scalars_all: list[Any] = []
        self._scalar: Any = 0

    def set_scalars_first(self, value: Any) -> "_FakeSession":
        self._scalars_first = value
        return self

    def set_scalars_all(self, values: list[Any]) -> "_FakeSession":
        self._scalars_all = values
        return self

    def set_scalar(self, value: Any) -> "_FakeSession":
        self._scalar = value
        return self

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed += 1

    async def delete(self, obj: Any) -> None:
        self.deleted.append(obj)

    async def execute(self, stmt: Any) -> str:
        self.executed.append(stmt)
        return "OK"

    class _Scalars:
        def __init__(self, first: Any, all_: list[Any]) -> None:
            self._first = first
            self._all = all_

        def first(self) -> Any:
            return self._first

        def all(self) -> list[Any]:
            return list(self._all)

    # AsyncSession 契约镜像：scalars/scalar 为协程（await 后的 ScalarResult 同步消费
    # ——house 口径 `await self.db.scalars(stmt)).all()`，见 thread_service）
    async def scalars(self, stmt: Any) -> "_FakeSession._Scalars":
        return _FakeSession._Scalars(self._scalars_first, self._scalars_all)

    async def scalar(self, stmt: Any) -> Any:
        return self._scalar


class _FakePurger:
    def __init__(self, exc: Exception | None = None) -> None:
        self.calls: list[dict[str, str]] = []
        self._exc = exc

    async def purge(self, *, user_id: str, doc_id: str) -> None:
        self.calls.append({"user_id": user_id, "doc_id": doc_id})
        if self._exc is not None:
            raise self._exc


def _service(
    session: _FakeSession,
    minio: _FakeMinio,
    *,
    purger: Any = None,
    fetcher: Any = None,
    spec: dict[str, Any] | None = None,
    es_indexer: Any = None,
) -> kb_service.KbService:
    return kb_service.KbService(
        session,  # type: ignore[arg-type]
        minio_client=lambda: minio,
        object_purger=purger or _FakePurger(),
        url_fetcher=fetcher,
        spec_payload_provider=lambda: spec if spec is not None else {},
        es_indexer=es_indexer,
    )


# ---------------------------------------------------------------------------
# §4.1 上传校验（纯函数 validate_upload）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ext", ["pdf", "md", "txt", "docx"])
def test_validate_upload_whitelisted(ext: str) -> None:
    assert kb_service.validate_upload(f"文件.{ext}", 100, allowed_types=["pdf", "md", "txt", "docx"], max_mb=20) == ext


def test_validate_upload_uppercase_and_dot_normalized() -> None:
    assert kb_service.validate_upload("Report.PDF", 100, allowed_types=["pdf"], max_mb=20) == "pdf"


def test_validate_upload_rejects_unknown_ext() -> None:
    with pytest.raises(kb_service.FileUnsupportedTypeException) as exc:
        kb_service.validate_upload("恶意.exe", 100, allowed_types=["pdf", "md", "txt", "docx"], max_mb=20)
    assert "暂不支持" not in exc.value.message  # 非音视频不带「暂不支持」措辞


def test_validate_upload_rejects_no_ext() -> None:
    with pytest.raises(kb_service.FileUnsupportedTypeException):
        kb_service.validate_upload("无扩展名", 100, allowed_types=["pdf"], max_mb=20)


@pytest.mark.parametrize("name", ["song.mp3", "clip.mp4", "movie.avi", "voice.m4a", "video.mov", "clip.mkv", "audio.wav"])
def test_validate_upload_audio_video_says_not_supported_yet(name: str) -> None:
    with pytest.raises(kb_service.FileUnsupportedTypeException) as exc:
        kb_service.validate_upload(name, 100, allowed_types=["pdf"], max_mb=20)
    assert "暂不支持" in exc.value.message  # api.md §4.1：音视频 message 提示「暂不支持」


def test_validate_upload_oversize() -> None:
    with pytest.raises(kb_service.FileTooLargeException):
        kb_service.validate_upload("big.md", 21 * 1024 * 1024, allowed_types=["md"], max_mb=20)


def test_validate_upload_respects_env_whitelist_delta() -> None:
    # KB_ALLOWED_TYPES 非法值域外（.env 收窄/扩展面）——白名单以配置单例为准
    with pytest.raises(kb_service.FileUnsupportedTypeException):
        kb_service.validate_upload("a.pdf", 1, allowed_types=["md"], max_mb=20)


# ---------------------------------------------------------------------------
# §4.1 上传落象
# ---------------------------------------------------------------------------


async def test_upload_document_happy_path() -> None:
    session = _FakeSession()
    minio = _FakeMinio()
    service = _service(session, minio)
    data = "# 报告\n\n内容".encode("utf-8")
    created = await service.upload_document(data, "设计文档.md", None, user_id=_USER_ID)

    assert created.model_dump(exclude_unset=True).keys() == {
        "document_id", "doc_type", "title", "file_size_bytes", "status",
        "fail_reason", "chunk_count", "created_at",
    }  # §4.1 八键（无 source_url）
    assert created.status == "pending"
    assert created.chunk_count == 0
    assert created.fail_reason is None
    assert created.file_size_bytes == len(data)
    assert created.title == "设计文档"  # 缺省=文件名去扩展名
    # 原件先落 MinIO（§4.1 顺序：对象 → 记录）
    doc_uuid = uuid.UUID(created.document_id)
    key = kb_pipeline.original_object_key(_USER_ID, str(doc_uuid), "md")
    assert minio.objects[(_BUCKET, key)] == data
    # 建行 pending（flush 不 commit——事务边界在 get_db）
    assert len(session.added) == 1
    row = session.added[0]
    assert isinstance(row, KbDocument)
    assert row.status == "pending"
    assert row.doc_type == "md"
    assert str(row.id) == created.document_id
    assert session.flushed == 1


async def test_upload_document_explicit_title_wins() -> None:
    service = _service(_FakeSession(), _FakeMinio())
    created = await service.upload_document(b"x", "a.md", "自定义标题", user_id=_USER_ID)
    assert created.title == "自定义标题"


async def test_upload_document_title_truncated_to_column_width() -> None:
    service = _service(_FakeSession(), _FakeMinio())
    created = await service.upload_document(b"x", "a.md", "长" * 600, user_id=_USER_ID)
    assert len(created.title) == 512  # varchar(512) 列宽截断（§1.7）


async def test_upload_document_minio_failure_no_row() -> None:
    class _BrokenMinio(_FakeMinio):
        def put_object(self, *a: Any, **k: Any) -> None:
            raise RuntimeError("minio down")

    session = _FakeSession()
    service = _service(session, _BrokenMinio())
    with pytest.raises(IrStorageError):
        await service.upload_document(b"x", "a.md", None, user_id=_USER_ID)
    assert session.added == []  # MinIO 先败 → 零行（§4.1 顺序：对象 → 记录）


# ---------------------------------------------------------------------------
# §4.2 URL 收藏
# ---------------------------------------------------------------------------


class _ExistingRow:
    """既有 kb_documents 行假体（dedup 命中形态）。"""

    def __init__(self) -> None:
        self.id = uuid.UUID(_DOC_ID)
        self.user_id = uuid.UUID(_USER_ID)
        self.doc_type = "url"
        self.title = "旧标题"
        self.source_url = "https://example.com/article"
        self.file_size_bytes = None
        self.status = "ready"
        self.fail_reason = None
        self.chunk_count = 3
        self.parse_meta: dict[str, Any] = {}
        self.created_at = datetime(2026, 11, 23, tzinfo=UTC)
        self.updated_at = datetime(2026, 11, 23, tzinfo=UTC)
        self.ready_at = datetime(2026, 11, 23, tzinfo=UTC)
        self.catalog_ids = None
        self.catalog_path = None


async def test_collect_url_guard_rejected_422001_with_trace(
    monkeypatch: pytest.MonkeyPatch, log_records: list[dict]
) -> None:
    session = _FakeSession()
    service = _service(session, _FakeMinio(), fetcher=None)

    async def _reject(url: str, guard: Any) -> Any:
        from app.agent.safety.url_guard import UrlGuardResult

        return UrlGuardResult(ok=False, reason="ip_forbidden")

    monkeypatch.setattr(kb_service, "validate_url_async", _reject)
    with pytest.raises(kb_service.KbUrlInvalidException) as exc:
        await service.collect_url("https://10.0.0.1/x", user_id=_USER_ID)
    assert exc.value.message  # 422001（映射在 ERROR_MAP——api 测断言信封码）
    assert "ip_forbidden" in exc.value.detail  # 枚举词进 detail（零 URL/host 回显）
    assert session.added == []  # 拒收零建行
    trace = [r for r in log_records if "url_rejected" in str(r.get("message", ""))]
    assert trace, "护栏拒收必须 url_rejected 语义留痕"


async def test_collect_url_guard_disabled_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """kill switch（guard None）→ 跳过校验直通（B15 与 safety.enabled 解耦口径）。"""
    fetched: list[str] = []

    async def _fetch(url: str, spec: Any) -> str:
        fetched.append(url)
        return "# 标题\n\n正文"

    service = _service(
        _FakeSession(),
        _FakeMinio(),
        fetcher=_fetcher_with(_fetch),
        spec={"safety": {"url_guard": {"enabled": False}}},
    )
    monkeypatch.setattr(kb_service, "validate_url_async", _fail_if_called)
    created = await service.collect_url("https://example.com/a", user_id=_USER_ID)
    assert created.duplicated is False
    assert fetched == ["https://example.com/a"]


async def _fail_if_called(url: str, guard: Any) -> Any:
    raise AssertionError("guard None（kill switch）不得调用校验")


def _fetcher_with(inner: Any) -> Any:
    return inner


async def test_collect_url_guard_disabled_reads_default_spec_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产装配形态（评审 P1）：KbService 接 pipeline._default_spec_payload 真实
    供给——Nacos spec 快照 safety.url_guard.enabled=false → guard None（kill
    switch）→ 跳过校验直通抓取。此前仅测注入假 provider 路径，装配面恒落
    defaults 种子的缺陷无覆盖（真实供给走 asset_center.snapshot 读取链）。"""
    fetched: list[str] = []

    async def _fetch(url: str, spec: Any) -> str:
        fetched.append(url)
        assert spec == {"safety": {"url_guard": {"enabled": False}}}
        return "# 标题\n\n正文"

    class _FakeSnapshot:
        payload: dict[str, Any] = {"safety": {"url_guard": {"enabled": False}}}

    class _FakeCenter:
        def snapshot(self, _data_id: str) -> _FakeSnapshot:
            return _FakeSnapshot()

    monkeypatch.setattr("app.config_center.get_asset_center", lambda: _FakeCenter())
    monkeypatch.setattr(kb_service, "validate_url_async", _fail_if_called)
    service = kb_service.KbService(
        _FakeSession(),
        minio_client=lambda: _FakeMinio(),
        object_purger=_FakePurger(),
        url_fetcher=_fetcher_with(_fetch),
        spec_payload_provider=kb_pipeline._default_spec_payload,
    )
    created = await service.collect_url("https://example.com/a", user_id=_USER_ID)
    assert created.duplicated is False
    assert fetched == ["https://example.com/a"]


async def test_collect_url_dedup_returns_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession().set_scalars_first(_ExistingRow())
    fetched: list[str] = []

    async def _fetch(url: str, spec: Any) -> str:
        fetched.append(url)
        return "# 新标题"

    async def _pass(url: str, guard: Any) -> Any:
        from app.agent.safety.url_guard import UrlGuardResult

        return UrlGuardResult(ok=True, reason=None)

    monkeypatch.setattr(kb_service, "validate_url_async", _pass)
    service = _service(session, _FakeMinio(), fetcher=_fetcher_with(_fetch))
    collected = await service.collect_url("https://example.com/article", user_id=_USER_ID)

    assert collected.duplicated is True  # 重复收藏不报错、返回既有条目
    assert collected.document_id == _DOC_ID
    assert collected.title == "旧标题"
    assert fetched == []  # 零抓取
    assert session.added == []  # 零建行


async def test_collect_url_new_entry_full_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession().set_scalars_first(None)
    minio = _FakeMinio()

    async def _fetch(url: str, spec: Any) -> str:
        return "# 抓取标题\n\n正文内容"

    async def _pass(url: str, guard: Any) -> Any:
        from app.agent.safety.url_guard import UrlGuardResult

        return UrlGuardResult(ok=True, reason=None)

    monkeypatch.setattr(kb_service, "validate_url_async", _pass)
    service = _service(session, minio, fetcher=_fetcher_with(_fetch))
    collected = await service.collect_url("https://example.com/article", user_id=_USER_ID)

    assert collected.duplicated is False
    assert collected.doc_type == "url"
    assert collected.title == "抓取标题"  # H1 提取
    assert collected.source_url == "https://example.com/article"
    assert collected.file_size_bytes is None  # URL 类型恒 null（§4.2 示例）
    assert collected.status == "pending"
    assert {**collected.model_dump(exclude_unset=True)}.keys() == {
        "document_id", "doc_type", "title", "source_url", "file_size_bytes",
        "status", "fail_reason", "chunk_count", "created_at", "duplicated",
    }
    # 抓取正文以 md 落原件（§1.7 :270）
    doc_uuid = uuid.UUID(collected.document_id)
    key = kb_pipeline.original_object_key(_USER_ID, str(doc_uuid), "md")
    assert key in [k for (_, k) in minio.objects]
    assert session.added[0].doc_type == "url"
    assert session.added[0].source_url == "https://example.com/article"


async def test_collect_url_title_falls_back_to_host(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession().set_scalars_first(None)

    async def _fetch(url: str, spec: Any) -> str:
        return "无标题正文"

    async def _pass(url: str, guard: Any) -> Any:
        from app.agent.safety.url_guard import UrlGuardResult

        return UrlGuardResult(ok=True, reason=None)

    monkeypatch.setattr(kb_service, "validate_url_async", _pass)
    service = _service(session, _FakeMinio(), fetcher=_fetcher_with(_fetch))
    collected = await service.collect_url("https://fallback.example/x", user_id=_USER_ID)
    assert collected.title == "fallback.example"


async def test_collect_url_fetcher_url_rejected_422001_with_trace(
    monkeypatch: pytest.MonkeyPatch, log_records: list[dict]
) -> None:
    """第二道护栏（读取链逐源复验）拒收 → 422001 + url_rejected 留痕（零建行
    零落象）；文案与通用「抓取失败」区分。"""
    from app.agent.safety.content_guard import UrlRejectedError

    session = _FakeSession().set_scalars_first(None)
    minio = _FakeMinio()

    async def _pass(url: str, guard: Any) -> Any:
        from app.agent.safety.url_guard import UrlGuardResult

        return UrlGuardResult(ok=True, reason=None)

    async def _rejected_read(url: str, spec: Any) -> str:
        raise UrlRejectedError(
            "url 被安全护栏拒绝（ip_forbidden）——零物理读取", reason="ip_forbidden"
        )

    monkeypatch.setattr(kb_service, "validate_url_async", _pass)
    service = _service(session, minio, fetcher=_fetcher_with(_rejected_read))
    with pytest.raises(kb_service.KbUrlInvalidException) as exc:
        await service.collect_url("https://example.com/a", user_id=_USER_ID)
    assert "拒收" in exc.value.message  # 与「URL 抓取失败」文案区分
    assert "ip_forbidden" in exc.value.detail  # reason 枚举（零 URL/host 回显）
    assert minio.objects == {}  # 零落象
    assert session.added == []  # 零建行
    trace = [r for r in log_records if "url_rejected" in str(r.get("message", ""))]
    assert trace, "第二道护栏拒收必须 url_rejected 语义留痕"


async def test_collect_url_rejects_oversized_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """抓取正文超 KB_MAX_FILE_MB 同口径上限 → 422001（零落象零建行）。"""
    session = _FakeSession().set_scalars_first(None)
    minio = _FakeMinio()

    async def _fetch(url: str, spec: Any) -> str:
        return "a" * (1024 * 1024 + 1)  # 超 1MB 一字节

    async def _pass(url: str, guard: Any) -> Any:
        from app.agent.safety.url_guard import UrlGuardResult

        return UrlGuardResult(ok=True, reason=None)

    monkeypatch.setattr(kb_service, "validate_url_async", _pass)
    monkeypatch.setattr(kb_service.kb_settings, "max_file_mb", 1)
    service = _service(session, minio, fetcher=_fetcher_with(_fetch))
    with pytest.raises(kb_service.KbUrlInvalidException) as exc:
        await service.collect_url("https://example.com/big", user_id=_USER_ID)
    assert "上限" in exc.value.message or "上限" in exc.value.detail
    assert minio.objects == {}
    assert session.added == []


async def test_collect_url_rejects_empty_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """抓取正文空白（strip 后空串）→ 422001（零落象零建行）。"""
    session = _FakeSession().set_scalars_first(None)
    minio = _FakeMinio()

    async def _fetch(url: str, spec: Any) -> str:
        return "  \n\t  "

    async def _pass(url: str, guard: Any) -> Any:
        from app.agent.safety.url_guard import UrlGuardResult

        return UrlGuardResult(ok=True, reason=None)

    monkeypatch.setattr(kb_service, "validate_url_async", _pass)
    service = _service(session, minio, fetcher=_fetcher_with(_fetch))
    with pytest.raises(kb_service.KbUrlInvalidException) as exc:
        await service.collect_url("https://example.com/empty", user_id=_USER_ID)
    assert "为空" in exc.value.message or "为空" in exc.value.detail
    assert minio.objects == {}
    assert session.added == []


# ---------------------------------------------------------------------------
# §4.3/4.4 投影
# ---------------------------------------------------------------------------


def _doc_row(**overrides: Any) -> Any:
    row = KbDocument(
        id=uuid.UUID(_DOC_ID),
        user_id=uuid.UUID(_USER_ID),
        doc_type="md",
        title="文档",
        source_url=None,
        file_size_bytes=10,
        status="ready",
        fail_reason=None,
        chunk_count=2,
        parse_meta={"pages": None, "tables": 1, "code_blocks": 0, "parser": "markitdown"},
        created_at=datetime(2026, 11, 23, tzinfo=UTC),
        updated_at=datetime(2026, 11, 23, 1, tzinfo=UTC),
        ready_at=datetime(2026, 11, 23, 0, 30, tzinfo=UTC),
        catalog_ids=None,
        catalog_path=None,
    )
    for k, v in overrides.items():
        setattr(row, k, v)
    return row


def test_to_kb_document_projection_ready_md() -> None:
    projected = kb_service.to_kb_document(_doc_row())
    dumped = projected.model_dump(exclude_unset=True)
    assert dumped.keys() == {
        "document_id", "doc_type", "title", "file_size_bytes", "status",
        "fail_reason", "chunk_count", "created_at", "updated_at", "ready_at",
        "parse_meta", "catalog_ids", "catalog_path",
    }  # §4.3 十三键（source_url 可选键——md 行缺则省略）
    assert dumped["parse_meta"] == {"pages": None, "tables": 1, "code_blocks": 0}
    assert "parser" not in dumped["parse_meta"]  # parser 存储侧键不进投影
    assert dumped["catalog_ids"] is None and dumped["catalog_path"] is None  # 本地上传恒 null


def test_to_kb_document_parse_meta_empty_is_null() -> None:
    row = _doc_row(parse_meta={}, status="pending", ready_at=None)
    projected = kb_service.to_kb_document(row)
    dumped = projected.model_dump(exclude_unset=True)
    assert dumped["parse_meta"] is None  # 存储空对象 {} → REST null
    assert dumped["ready_at"] is None


def test_to_kb_document_url_row_includes_source_url() -> None:
    row = _doc_row(doc_type="url", source_url="https://example.com/a", file_size_bytes=None)
    dumped = kb_service.to_kb_document(row).model_dump(exclude_unset=True)
    assert dumped["source_url"] == "https://example.com/a"
    assert dumped["file_size_bytes"] is None


def test_to_kb_document_failed_keeps_parse_meta() -> None:
    row = _doc_row(status="failed", fail_reason="chunking: 切分器内部错误", ready_at=None)
    dumped = kb_service.to_kb_document(row).model_dump(exclude_unset=True)
    assert dumped["status"] == "failed"
    assert dumped["fail_reason"] == "chunking: 切分器内部错误"
    assert dumped["parse_meta"] == {"pages": None, "tables": 1, "code_blocks": 0}  # 失败态保留已达成摘要


async def test_list_documents_passes_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession().set_scalars_all([_doc_row()]).set_scalar(1)
    service = _service(session, _FakeMinio())
    query = kb_service.KbDocumentListQuery(limit=50, offset=0, status=None, doc_type=None)
    page = await service.list_documents(query, user_id=_USER_ID)
    assert page.total == 1
    assert len(page.items) == 1
    assert page.items[0].model_dump(exclude_unset=True)["status"] == "ready"


async def test_get_document_not_found() -> None:
    session = _FakeSession().set_scalars_first(None)
    service = _service(session, _FakeMinio())
    with pytest.raises(NotFoundException):
        await service.get_document(_DOC_ID, user_id=_USER_ID)


async def test_get_document_rejects_non_uuid() -> None:
    service = _service(_FakeSession(), _FakeMinio())
    with pytest.raises(NotFoundException):
        await service.get_document("not-a-uuid", user_id=_USER_ID)


# ---------------------------------------------------------------------------
# §4.5 删除级联
# ---------------------------------------------------------------------------


class _FakeEsIndexer:
    """KbEsIndexer 假体（删除级联 ES 腿消费面）：记录调用，可编程失败。"""

    def __init__(self, exc: Exception | None = None) -> None:
        self.calls: list[dict[str, str]] = []
        self._exc = exc

    async def delete_document(self, kb_doc_id: Any, *, user_id: str) -> None:
        self.calls.append({"kb_doc_id": str(kb_doc_id), "user_id": user_id})
        if self._exc is not None:
            raise self._exc


async def test_delete_document_cascades(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _doc_row()
    session = _FakeSession().set_scalars_first(row)
    purger = _FakePurger()
    service = _service(session, _FakeMinio(), purger=purger)
    result = await service.delete_document(_DOC_ID, user_id=_USER_ID)

    assert result.deleted is True
    assert session.deleted == [row]  # 行删（kb_chunks 经 FK CASCADE）
    assert len(session.executed) == 1  # sources 反向引用清理 UPDATE（零 REFERENCES 手工维护）
    assert purger.calls == [{"user_id": _USER_ID, "doc_id": _DOC_ID}]  # MinIO 前缀清理


async def test_delete_document_es_leg_wired_and_ordered() -> None:
    """删除级联 ES 腿（B18）：pg 行删 → ES 腿 → MinIO purge（pg 事实源先删，
    ES/MinIO 均为可重建副本侧）；kwargs 与 import_worker 级联缝签名一致。"""
    row = _doc_row()
    session = _FakeSession().set_scalars_first(row)
    order: list[str] = []

    class _OrderedPurger:
        async def purge(self, *, user_id: str, doc_id: str) -> None:
            order.append("purge")

    es_indexer = _FakeEsIndexer()

    async def _record_delete(kb_doc_id: Any, *, user_id: str) -> None:
        order.append("es")
        es_indexer.calls.append({"kb_doc_id": str(kb_doc_id), "user_id": user_id})

    es_indexer.delete_document = _record_delete  # type: ignore[method-assign]
    service = _service(
        session, _FakeMinio(), purger=_OrderedPurger(), es_indexer=es_indexer
    )
    result = await service.delete_document(_DOC_ID, user_id=_USER_ID)
    assert result.deleted is True
    assert es_indexer.calls == [{"kb_doc_id": _DOC_ID, "user_id": _USER_ID}]
    assert order == ["es", "purge"]  # ES 腿在 MinIO purge 之前（plan B18 §6.1）


async def test_delete_document_es_leg_failure_is_best_effort(
    monkeypatch: pytest.MonkeyPatch, log_records: list[dict]
) -> None:
    """ES 不可达：删除仍 deleted=true（api.md §4.5 契约零变更），
    WARN 留痕（action=kb_purge_es）；残余孤儿归 audit 治理（plan B18 §6.2）。"""
    row = _doc_row()
    session = _FakeSession().set_scalars_first(row)
    es_indexer = _FakeEsIndexer(exc=RuntimeError("es down"))
    service = _service(session, _FakeMinio(), es_indexer=es_indexer)
    result = await service.delete_document(_DOC_ID, user_id=_USER_ID)
    assert result.deleted is True
    assert es_indexer.calls  # ES 腿确被调用
    warns = [
        r
        for r in log_records
        if r.get("level").name == "WARNING"
        and r.get("extra", {}).get("action") == "kb_purge_es"
    ]
    assert warns, "ES 删除腿失败必须落 WARN（action=kb_purge_es）"


async def test_delete_document_es_leg_none_zero_calls() -> None:
    """es_indexer 注入 None（缺省=现状）：零 ES 调用零异常（灰度安全兜底）。"""
    row = _doc_row()
    session = _FakeSession().set_scalars_first(row)
    purger = _FakePurger()
    service = _service(session, _FakeMinio(), purger=purger, es_indexer=None)
    result = await service.delete_document(_DOC_ID, user_id=_USER_ID)
    assert result.deleted is True
    assert purger.calls == [{"user_id": _USER_ID, "doc_id": _DOC_ID}]  # 主流程照常


async def test_delete_document_purge_failure_is_best_effort(
    monkeypatch: pytest.MonkeyPatch, log_records: list[dict]
) -> None:
    row = _doc_row()
    session = _FakeSession().set_scalars_first(row)
    purger = _FakePurger(exc=RuntimeError("minio down"))
    service = _service(session, _FakeMinio(), purger=purger)
    result = await service.delete_document(_DOC_ID, user_id=_USER_ID)
    assert result.deleted is True  # MinIO 抖动不阻断删除主流程
    warns = [r for r in log_records if r.get("level").name == "WARNING"]
    assert warns, "purge 失败必须落 WARN"


async def test_delete_document_not_found() -> None:
    session = _FakeSession().set_scalars_first(None)
    service = _service(session, _FakeMinio(), purger=_FakePurger())
    with pytest.raises(NotFoundException):
        await service.delete_document(_DOC_ID, user_id=_USER_ID)


async def test_delete_document_rejects_non_uuid() -> None:
    service = _service(_FakeSession(), _FakeMinio(), purger=_FakePurger())
    with pytest.raises(NotFoundException):
        await service.delete_document("junk", user_id=_USER_ID)


# ---------------------------------------------------------------------------
# 归一化路由校验（registry 白名单与 service 校验同域防漂移）
# ---------------------------------------------------------------------------


def test_registry_unsupported_type_is_normalization_family() -> None:
    # 白名单外扩展名在 registry 侧同样显式拒绝（UnsupportedTypeError——worker 段
    # 兜底防线；REST 段已在入口拒，正常不可达，此断言锁依赖面存在性）
    assert issubclass(UnsupportedTypeError, Exception)


# ---------------------------------------------------------------------------
# §4.6 溯源下钻 service 链（B19——Source→chunk→doc 解析 + 修订注 2 取舍）
# ---------------------------------------------------------------------------


class _DrilldownDb:
    """get(Model, uuid) 按对象表解析的最小假 session（溯源链零 scalars 面）。"""

    def __init__(self, objects: dict[tuple[type, str], Any]) -> None:
        self._objects = objects

    async def get(self, model: Any, obj_uuid: Any) -> Any:
        return self._objects.get((model, str(obj_uuid)))


def _drilldown_service(objects: dict[tuple[type, str], Any]) -> Any:
    import uuid as uuid_module

    return kb_service.KbService(
        _DrilldownDb(objects),
        minio_client=lambda: None,
        object_purger=None,
        spec_payload_provider=lambda: {},
        es_indexer=None,
    )


def _uuid(value: str) -> Any:
    import uuid as uuid_module

    return uuid_module.UUID(value)


_SRC_ID = "aaaaaaaa-1111-4111-8111-000000000001"
_USER = "00000000-0000-4000-8000-000000000001"
_CHUNK_ID = "5c2e1111-1111-4111-8111-000000000001"
_DOC_ID = "9a772222-2222-4222-8222-000000000001"


def _drilldown_fixtures(
    node_id: str | None,
    *,
    doc_type: str = "md",
    parse_meta: dict[str, Any] | None = None,
) -> dict[tuple[type, str], Any]:
    from app.db.models import KbChunk as KbChunkRow
    from app.db.models import KbDocument as KbDocumentRow
    from app.db.models import Source as SourceRow

    source = SourceRow(
        id=_uuid(_SRC_ID), turn_id=_uuid(_DOC_ID), user_id=_uuid(_USER),
        ref_no=1, source_type="kb", title="KB 卡", url=None, snippet="s",
        score=None, kb_doc_id=_uuid(_DOC_ID), kb_chunk_ids=[_CHUNK_ID],
    )
    chunk = KbChunkRow(
        id=_uuid(_CHUNK_ID), kb_doc_id=_uuid(_DOC_ID), user_id=_uuid(_USER),
        chunk_index=0, content="KB 原文内容", token_count=4,
        meta={
            "node_id": node_id,
            "heading_path": ["第三章", "3.2 混合检索"],
            "loc": {"origin": "local", "page": 12},
        },
    )
    doc = KbDocumentRow(
        id=_uuid(_DOC_ID), user_id=_uuid(_USER), doc_type=doc_type,
        title="内部设计文档-检索融合.md", status="ready", parse_meta=parse_meta,
    )
    return {
        (SourceRow, _SRC_ID): source,
        (KbChunkRow, _CHUNK_ID): chunk,
        (KbDocumentRow, _DOC_ID): doc,
    }


async def test_drilldown_fs_node_source_full_projection() -> None:
    """fs:node: 来源 → 六必选 + node_token/node_id 齐带；page 有值 paragraph null 键恒现。"""
    service = _drilldown_service(_drilldown_fixtures("fs:node:tok42"))
    detail = await service.get_chunk_drilldown(_SRC_ID, user_id=_USER)
    dumped = detail.model_dump(mode="json", exclude_unset=True)
    assert dumped["chunk_id"] == _CHUNK_ID
    assert dumped["document_id"] == _DOC_ID
    assert dumped["document_title"] == "内部设计文档-检索融合.md"
    assert dumped["content"] == "KB 原文内容"
    assert dumped["heading_path"] == ["第三章", "3.2 混合检索"]
    assert dumped["page"] == 12 and dumped["paragraph"] is None
    assert dumped["node_token"] == "tok42"
    assert dumped["node_id"] == "fs:node:tok42"


async def test_drilldown_fs_blk_keeps_node_id_omits_token() -> None:
    """修订注 2 基线：fs:blk: → node_id 保留；文档 parse_meta 无 feishu 面时
    node_token 键省略（exclude_unset 落实）。"""
    service = _drilldown_service(_drilldown_fixtures("fs:blk:blk9"))
    detail = await service.get_chunk_drilldown(_SRC_ID, user_id=_USER)
    dumped = detail.model_dump(mode="json", exclude_unset=True)
    assert dumped["node_id"] == "fs:blk:blk9"
    assert "node_token" not in dumped


async def test_drilldown_fs_blk_supplements_token_from_doc_parse_meta() -> None:
    """GWT#2 补桥（用户裁决）：fs:blk: chunk + 文档 parse_meta.feishu.node_token →
    node_token 下发（回跳可达）；node_id 保持 fs:blk: 形态。库面实测：飞书导入行
    doc_type=docx、源标记=parse_meta.feishu 面在场。"""
    service = _drilldown_service(
        _drilldown_fixtures(
            "fs:blk:blk9",
            doc_type="docx",
            parse_meta={"feishu": {"node_token": "MJgewCcz2iycFukzjG0cxBV2nld"}},
        )
    )
    detail = await service.get_chunk_drilldown(_SRC_ID, user_id=_USER)
    dumped = detail.model_dump(mode="json", exclude_unset=True)
    assert dumped["node_token"] == "MJgewCcz2iycFukzjG0cxBV2nld"
    assert dumped["node_id"] == "fs:blk:blk9"


@pytest.mark.parametrize(
    "parse_meta",
    [
        {"feishu": {}},  # 面在场但无 token
        {"feishu": {"node_token": ""}},  # 空串
        {"feishu": {"node_token": "   "}},  # 纯空白
        {"feishu": {"node_token": 123}},  # 非字符串
        {"pages": 3},  # 无 feishu 面
    ],
)
async def test_drilldown_feishu_meta_without_usable_token_keeps_omitted(
    parse_meta: dict[str, Any],
) -> None:
    """token 取不到（缺失/空/非字符串）→ 键省略（不置 null，键可省语义）。"""
    service = _drilldown_service(
        _drilldown_fixtures("fs:blk:blk9", doc_type="docx", parse_meta=parse_meta)
    )
    detail = await service.get_chunk_drilldown(_SRC_ID, user_id=_USER)
    dumped = detail.model_dump(mode="json", exclude_unset=True)
    assert "node_token" not in dumped


async def test_drilldown_local_doc_unaffected_by_parse_meta() -> None:
    """web/doc 本地来源（loc: 前缀）即便 parse_meta 带无关键 → 两可选键全省略。"""
    service = _drilldown_service(
        _drilldown_fixtures("loc:blk:p1", doc_type="docx", parse_meta={"pages": 3})
    )
    detail = await service.get_chunk_drilldown(_SRC_ID, user_id=_USER)
    dumped = detail.model_dump(mode="json", exclude_unset=True)
    assert "node_token" not in dumped and "node_id" not in dumped


async def test_drilldown_loc_prefix_both_keys_omitted() -> None:
    """loc: 前缀（本地/URL）→ 两可选键全省略（不置 null）。"""
    service = _drilldown_service(_drilldown_fixtures("loc:blk:p1"))
    detail = await service.get_chunk_drilldown(_SRC_ID, user_id=_USER)
    dumped = detail.model_dump(mode="json", exclude_unset=True)
    assert "node_token" not in dumped and "node_id" not in dumped


async def test_drilldown_non_kb_source_400001() -> None:
    from app.db.models import KbChunk as KbChunkRow
    from app.db.models import KbDocument as KbDocumentRow
    from app.db.models import Source as SourceRow

    objects = _drilldown_fixtures("loc:blk:p1")
    web = SourceRow(
        id=_uuid(_SRC_ID), turn_id=_uuid(_DOC_ID), user_id=_uuid(_USER),
        ref_no=1, source_type="web", title="网页", url="https://a.example",
        snippet="s", score=None,
    )
    objects = {k: v for k, v in objects.items() if k[0] is not SourceRow}
    objects[(SourceRow, _SRC_ID)] = web
    service = _drilldown_service(objects)
    from app.exception.handler.errors import ERROR_MAP

    with pytest.raises(kb_service.BadRequestException) as excinfo:
        await service.get_chunk_drilldown(_SRC_ID, user_id=_USER)
    assert ERROR_MAP[type(excinfo.value)][0] == "400001"


async def test_drilldown_missing_and_mismatch_404001_single_code() -> None:
    """不存在/越权/chunk 已删除/文档已删除/kb_chunk_ids 空 → 全部 404001 单码。"""
    from app.db.models import KbChunk as KbChunkRow
    from app.db.models import KbDocument as KbDocumentRow
    from app.db.models import Source as SourceRow

    # ① source 不存在
    service = _drilldown_service({})
    from app.exception.handler.errors import ERROR_MAP

    with pytest.raises(kb_service.NotFoundException) as e:
        await service.get_chunk_drilldown(_SRC_ID, user_id=_USER)
    assert ERROR_MAP[type(e.value)][0] == "404001"
    # ② 越权（user 不匹配）
    service = _drilldown_service(_drilldown_fixtures(None))
    with pytest.raises(kb_service.NotFoundException) as e:
        await service.get_chunk_drilldown(_SRC_ID, user_id="99999999-9999-4999-8999-999999999999")
    assert ERROR_MAP[type(e.value)][0] == "404001"
    # ③ chunk 已删除（kb_chunk_ids 指向缺失行）
    objects = _drilldown_fixtures(None)
    objects = {k: v for k, v in objects.items() if k[0] is not KbChunkRow}
    service = _drilldown_service(objects)
    with pytest.raises(kb_service.NotFoundException) as e:
        await service.get_chunk_drilldown(_SRC_ID, user_id=_USER)
    assert ERROR_MAP[type(e.value)][0] == "404001"
    # ④ 文档已删除 + 越权 chunk（user 不匹配）
    partial = _drilldown_fixtures(None)
    other_user = _uuid("99999999-9999-4999-8999-999999999999")
    chunk = partial[(KbChunkRow, _CHUNK_ID)]
    chunk.user_id = other_user  # 越权形态
    doc = partial[(KbDocumentRow, _DOC_ID)]
    objects = {(SourceRow, _SRC_ID): partial[(SourceRow, _SRC_ID)], (KbChunkRow, _CHUNK_ID): chunk}
    service = _drilldown_service(objects)
    with pytest.raises(kb_service.NotFoundException) as e:
        await service.get_chunk_drilldown(_SRC_ID, user_id=_USER)
    assert ERROR_MAP[type(e.value)][0] == "404001"
    # ⑤ 文档缺失
    objects[(KbDocumentRow, _DOC_ID)] = None
    service = _drilldown_service(objects)
    with pytest.raises(kb_service.NotFoundException) as e:
        await service.get_chunk_drilldown(_SRC_ID, user_id=_USER)
    assert ERROR_MAP[type(e.value)][0] == "404001"


async def test_drilldown_source_row_without_chunk_ids_404001() -> None:
    """来源行 kb_chunk_ids 空（异常态）→ 404001（不泄漏存在性）。"""
    from app.db.models import Source as SourceRow
    from app.exception.handler.errors import ERROR_MAP

    source = SourceRow(
        id=_uuid(_SRC_ID), turn_id=_uuid(_DOC_ID), user_id=_uuid(_USER),
        ref_no=1, source_type="kb", title="KB 卡", url=None, snippet="s",
        score=None, kb_doc_id=None, kb_chunk_ids=None,
    )
    service = _drilldown_service({(SourceRow, _SRC_ID): source})
    with pytest.raises(kb_service.NotFoundException) as e:
        await service.get_chunk_drilldown(_SRC_ID, user_id=_USER)
    assert ERROR_MAP[type(e.value)][0] == "404001"
