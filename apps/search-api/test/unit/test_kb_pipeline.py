"""unit 层：KB 入库流水线（app/rag/pipeline.py，api.md §4.1 / data-model.md
§1.7 六态状态机 / data-model.md §4 启动钩子「重启即恢复处理」真实语义面）。

覆盖点（TDD 红绿；全假体零网络零 DB）：
- **六态状态机矩阵**：pending → normalizing → chunking → indexing → ready 的
  乐观状态卫（UPDATE ... WHERE status IN expect）逐段断言——resume 语义（行已在
  chunking/indexing 状态时跳过前段、从 MinIO ir.json 续跑）；
- **ready 分支语义**（data-model §1.7 :275 / api.md §4.1 钉注）：es_enabled=false
  灰度态 pgvector 单通道确认即 ready；true 双通道——ES 确认点=B19 seam 未实装，
  本批留痕放行（不永卡 indexing）；
- **失败语义**：任一步异常 → failed + 阶段化 fail_reason（`{stage}: {detail}`，
  stage ∈ normalizing|chunking|indexing）+ 原文保留（零 purge 调用）；
- **重入队**：start() 消费 main._requeue_kb_work B16 seam 登记行（status IN
  pending/normalizing/chunking/indexing）——B17 验收断言的 unit 面（真库面归
  integration）；
- **纯函数**：parse_meta 投影映射（存储 `{}`/四键 → REST 三键/null）、URL 标题
  提取、chunk meta 七键装配、indexing 确认分支、原件扩展名归一、零向量占位。
"""

import uuid
from typing import Any

import pytest

from app.core.config import minio_settings
from app.rag import pipeline as kb_pipeline
from app.rag.chunker import Chunk
from app.rag.ir_schema import IrNode, LocalLoc, UrlLoc, serialize_ir
from app.rag.normalizers.errors import NormalizationError
from app.rag.normalizers.result import NormalizationResult
from app.rag.storage import (
    get_ir_json,
    ir_object_key,
    original_object_key,
    put_ir_json,
)

_USER_ID = "00000000-0000-4000-8000-000000000001"
_DOC_ID = "7d2f0a1e-0000-4000-8000-0000000000b1"
_BUCKET = minio_settings.bucket_kb


class _FakeRepo:
    """KbDocumentRepository 假体：记录全部状态迁移与失败落列（零 DB）。"""

    def __init__(
        self, row: dict[str, Any] | None = None, *, chunks: list[dict[str, Any]] | None = None
    ) -> None:
        self.row = row
        self.transitions: list[dict[str, Any]] = []
        self.failures: list[str] = []
        self.chunk_replacements: list[dict[str, Any]] = []
        self.transition_results: list[bool] = []  # 逐次返回值（空=恒 True）
        self.requeue_ids: list[uuid.UUID] = []
        self._chunks = chunks if chunks is not None else []
        self.listed_chunks: list[uuid.UUID] = []  # list_chunks_by_doc 调用记录（B18）

    async def load(self, doc_id: uuid.UUID) -> dict[str, Any] | None:
        return self.row

    async def transition(
        self, doc_id: uuid.UUID, *, expect: set[str], to: str, fields: dict[str, Any]
    ) -> bool:
        self.transitions.append(
            {"expect": set(expect), "to": to, "fields": dict(fields)}
        )
        if self.transition_results:
            ok = self.transition_results.pop(0)
        else:
            ok = True
        if ok and self.row is not None:
            self.row["status"] = to
        return ok

    async def mark_failed(self, doc_id: uuid.UUID, *, reason: str) -> bool:
        self.failures.append(reason)
        if self.row is not None:
            self.row["status"] = "failed"
        return True

    async def replace_chunks(
        self,
        doc_id: uuid.UUID,
        *,
        user_id: uuid.UUID,
        chunks: list[Chunk],
        content_hash: str,
        embeddings: list[list[float]] | None = None,
    ) -> bool:
        self.chunk_replacements.append(
            {
                "chunks": list(chunks),
                "content_hash": content_hash,
                "user_id": user_id,
                "embeddings": embeddings,
            }
        )
        if self.row is not None:
            self.row["status"] = "indexing"
            self.row["chunk_count"] = len(chunks)
            self.row["content_hash"] = content_hash
        return True

    async def list_chunks_by_doc(self, doc_id: uuid.UUID) -> list[dict[str, Any]]:
        """pg 事实源读回（B18 indexing 段消费——ORDER BY chunk_index 假体直返）。"""
        self.listed_chunks.append(doc_id)
        return list(self._chunks)

    async def list_requeue_ids(self) -> list[uuid.UUID]:
        return list(self.requeue_ids)


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
    """内存 MinIO 假体（test_kb_storage._FakeMinio 同款接口 + 列举/删除面）。"""

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


def _row(status: str = "pending") -> dict[str, Any]:
    return {
        "id": uuid.UUID(_DOC_ID),
        "user_id": uuid.UUID(_USER_ID),
        "status": status,
        "doc_type": "md",
        "title": "示例文档",
        "source_url": None,
        "file_object_key": None,
    }


_MD_BYTES = "# 示例\n\n正文段落。".encode("utf-8")


def _make_chunk_forest(n: int = 2, doc_hash: str = "c" * 64):
    def _fake(root: IrNode, **kwargs: Any) -> list[Any]:
        chunks = [
            Chunk(
                document_id=kwargs.get("document_id"),
                chunk_index=i,
                content=f"切片正文{i}",
                token_count=4,
                heading_path=["示例文档"],
                kind="paragraph",
                raw_type="p",
                node_id=f"loc:blk:{i}",
                loc=None,
                references=[],
                char_start=0,
                char_end=4,
                content_hash="a" * 64,
            )
            for i in range(n)
        ]
        result = type(
            "R",
            (),
            {
                "document_id": kwargs.get("document_id"),
                "chunks": chunks,
                "document_text": "切片正文",
                "document_content_hash": doc_hash,
                "skipped_units": 0,
                "oversized_units": 0,
            },
        )()
        return [result]

    return _fake


def _fake_normalize(**meta_overrides: Any):
    def _normalize(data: bytes, ext: str, **kwargs: Any) -> NormalizationResult:
        document = IrNode(
            id="loc:doc:stub",
            type="document",
            title=None,
            doc_kind="upload",
            meta={"raw_type": "normalizer.document"},
        )
        parse_meta = {"pages": None, "tables": 1, "code_blocks": 2, "parser": "markitdown"}
        parse_meta.update(meta_overrides)
        return NormalizationResult(document=document, parse_meta=parse_meta)

    return _normalize


@pytest.fixture
def fake_minio() -> _FakeMinio:
    return _FakeMinio()


@pytest.fixture
def stub_pipeline_seams(monkeypatch: pytest.MonkeyPatch):
    """归一化/切分双缝假体：md 假归一（parse_meta 四键）+ 固定双 chunk 产出。"""
    monkeypatch.setattr(kb_pipeline, "normalize_document", _fake_normalize())
    monkeypatch.setattr(kb_pipeline, "chunk_forest", _make_chunk_forest())
    return {"normalize": _fake_normalize(), "chunk_forest": _make_chunk_forest()}


def _seed_original(minio: _FakeMinio) -> None:
    minio.objects[(_BUCKET, original_object_key(_USER_ID, _DOC_ID, "md"))] = _MD_BYTES


def _make_pipeline(
    repo: _FakeRepo,
    minio: _FakeMinio,
    spec: dict[str, Any] | None = None,
    *,
    es_indexer: Any | None = None,
) -> kb_pipeline.KbPipeline:
    return kb_pipeline.KbPipeline(
        repo=repo,
        spec_payload_provider=lambda: spec if spec is not None else {},
        minio_client=lambda: minio,
        worker_count=0,  # unit 直驱 process_document，不 spawn worker
        es_indexer=es_indexer,
    )


# ---------------------------------------------------------------------------
# 状态机矩阵
# ---------------------------------------------------------------------------


async def test_status_machine_happy_path_es_disabled(
    stub_pipeline_seams, fake_minio
) -> None:
    _seed_original(fake_minio)  # 上传段已落原件（worker 只读不写原件）
    repo = _FakeRepo(_row("pending"))
    pipeline = _make_pipeline(repo, fake_minio, spec={"kb": {"es_enabled": False}})
    terminal = await pipeline.process_document(_DOC_ID)

    assert terminal == "ready"
    assert [(t["expect"], t["to"]) for t in repo.transitions] == [
        ({"pending"}, "normalizing"),
        ({"normalizing"}, "chunking"),
        ({"chunking"}, "indexing"),
        ({"indexing"}, "ready"),
    ]
    # parse_meta（含 parser 存储侧键）在 normalizing→chunking 迁移时落列
    chunking_fields = repo.transitions[1]["fields"]
    assert chunking_fields["parse_meta"]["parser"] == "markitdown"
    # chunking→indexing 迁移携带 chunk_count 与 content_hash（OQ-34 写入接线）
    indexing_fields = repo.transitions[2]["fields"]
    assert indexing_fields["chunk_count"] == 2
    assert indexing_fields["content_hash"] == "c" * 64
    # ready 迁移携带 ready_at
    assert repo.transitions[3]["fields"]["ready_at"] is not None
    # ir.json 已落 MinIO（键约定 §1.18）
    assert ir_object_key(_USER_ID, _DOC_ID) in [k for (_, k) in fake_minio.objects]
    # 切分产物（含 meta 七键装配原料）进 replace_chunks
    assert len(repo.chunk_replacements) == 1
    assert len(repo.chunk_replacements[0]["chunks"]) == 2
    assert repo.chunk_replacements[0]["content_hash"] == "c" * 64


async def test_document_title_enriched_into_ir(stub_pipeline_seams, fake_minio) -> None:
    """行 title 富化进 document 节点（§1.18 heading_path 构成公式 doc.title 段）。"""
    _seed_original(fake_minio)
    repo = _FakeRepo(_row("pending"))
    captured: dict[str, Any] = {}

    real_put = kb_pipeline.put_ir_json

    async def _spy_put(user_id: str, doc_id: str, root: IrNode, **kw: Any) -> str:
        captured["title"] = root.title
        return await real_put(user_id, doc_id, root, client=fake_minio)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(kb_pipeline, "put_ir_json", _spy_put)
        pipeline = _make_pipeline(repo, fake_minio, spec={})
        await pipeline.process_document(_DOC_ID)
    assert captured["title"] == "示例文档"


async def test_resume_at_chunking_skips_normalizing(fake_minio) -> None:
    repo = _FakeRepo(_row("chunking"))
    tree = IrNode(
        id="loc:doc:resume", type="document", title="续跑文档", doc_kind="upload",
        meta={"raw_type": "x"},
    )
    await put_ir_json(_USER_ID, _DOC_ID, tree, client=fake_minio)
    pipeline = _make_pipeline(repo, fake_minio, spec={})
    with pytest.MonkeyPatch.context() as mp:
        called = {"normalize": False}

        def _no_normalize(*a: Any, **k: Any) -> Any:
            called["normalize"] = True
            raise AssertionError("resume at chunking 不得重跑归一化")

        mp.setattr(kb_pipeline, "normalize_document", _no_normalize)
        mp.setattr(kb_pipeline, "chunk_forest", _make_chunk_forest(n=1))
        terminal = await pipeline.process_document(_DOC_ID)
    assert terminal == "ready"
    assert called["normalize"] is False
    assert [(t["expect"], t["to"]) for t in repo.transitions] == [
        ({"chunking"}, "indexing"),
        ({"indexing"}, "ready"),
    ]


async def test_resume_at_indexing_goes_straight_ready(fake_minio) -> None:
    repo = _FakeRepo(_row("indexing"))
    pipeline = _make_pipeline(repo, fake_minio, spec={})
    terminal = await pipeline.process_document(_DOC_ID)
    assert terminal == "ready"
    assert [(t["expect"], t["to"]) for t in repo.transitions] == [({"indexing"}, "ready")]


# ---------------------------------------------------------------------------
# ready 分支语义（es_enabled 双分支）
# ---------------------------------------------------------------------------


def test_resolve_indexing_confirmation_branches() -> None:
    gray = kb_pipeline.resolve_indexing_confirmation(False)
    assert gray.es_enabled is False
    assert gray.channel == "pgvector"
    assert gray.es_pending_seam is False

    # es_indexer 未接线（缺省 None）→ 留痕放行兜底（灰度安全——不因装配遗漏全量 failed）
    dual = kb_pipeline.resolve_indexing_confirmation(True)
    assert dual.es_enabled is True
    assert dual.channel == "dual"
    assert dual.es_pending_seam is True


def test_resolve_indexing_confirmation_seam_wired() -> None:
    """es_enabled=true + es_indexer 已注入 → es_pending_seam 反转为 False（真写入路径）。"""
    dual = kb_pipeline.resolve_indexing_confirmation(True, object())
    assert dual.es_enabled is True
    assert dual.channel == "dual"
    assert dual.es_pending_seam is False


async def test_es_enabled_true_walks_ready_with_trace(
    stub_pipeline_seams, fake_minio, log_records: list[dict]
) -> None:
    _seed_original(fake_minio)
    repo = _FakeRepo(_row("pending"))
    pipeline = _make_pipeline(repo, fake_minio, spec={"kb": {"es_enabled": True}})
    terminal = await pipeline.process_document(_DOC_ID)
    assert terminal == "ready"  # 不永卡 indexing（data-model §1.7 评审钉注）
    trace = [
        r
        for r in log_records
        if "B18" in str(r.get("message", "")) or "es_pending_seam" in str(r.get("extra", {}))
    ]
    assert trace, "es_enabled=true 分支必须留痕（B18 缝未接线兜底，灰度放行）"


def test_read_es_enabled_defaults_gray_safe() -> None:
    assert kb_pipeline.read_es_enabled({}) is False
    assert kb_pipeline.read_es_enabled({"kb": {}}) is False
    assert kb_pipeline.read_es_enabled({"kb": {"es_enabled": True}}) is True
    assert kb_pipeline.read_es_enabled({"kb": {"es_enabled": "yes"}}) is False  # 非严格布尔→灰度缺省


# ---------------------------------------------------------------------------
# 失败语义
# ---------------------------------------------------------------------------


async def test_normalize_failure_marks_failed_with_stage_prefix(fake_minio) -> None:
    repo = _FakeRepo(_row("pending"))
    pipeline = _make_pipeline(repo, fake_minio, spec={})
    with pytest.MonkeyPatch.context() as mp:

        # 闭包必须在 setattr 前捕获真方法：patch 后 repo.transition 即本函数自身，
        # 直接委托会自递归（RecursionError——与下方「首迁移已发生」断言矛盾）
        real_transition = repo.transition

        async def _fail_transition(doc_id: uuid.UUID, *, expect: set[str], to: str, fields: dict) -> bool:
            return await real_transition(doc_id, expect=expect, to=to, fields=fields)

        def _fail_normalize(*a: Any, **k: Any) -> NormalizationResult:
            raise NormalizationError("markitdown 转换失败（.md）")

        mp.setattr(repo, "transition", _fail_transition)
        mp.setattr(kb_pipeline, "normalize_document", _fail_normalize)
        terminal = await pipeline.process_document(_DOC_ID)
    assert terminal == "failed"
    assert repo.failures == ["normalizing: markitdown 转换失败（.md）"]
    assert repo.chunk_replacements == []  # 切分未发生
    assert [t["to"] for t in repo.transitions] == ["normalizing"]  # 首迁移已发生


async def test_chunk_stage_failure_marks_failed_and_keeps_original(
    stub_pipeline_seams, fake_minio
) -> None:
    _seed_original(fake_minio)
    repo = _FakeRepo(_row("pending"))
    pipeline = _make_pipeline(repo, fake_minio, spec={})
    with pytest.MonkeyPatch.context() as mp:

        def _fail_chunk(root: IrNode, **k: Any) -> list[Any]:
            raise ValueError("切分器内部错误")

        mp.setattr(kb_pipeline, "chunk_forest", _fail_chunk)
        terminal = await pipeline.process_document(_DOC_ID)
    assert terminal == "failed"
    assert repo.failures == ["chunking: 切分器内部错误"]
    assert repo.chunk_replacements == []
    # 原文保留可重试：零对象删除调用（失败不 purge）
    assert fake_minio.removed == []


async def test_pending_stage_failure_has_no_stage_prefix(fake_minio) -> None:
    repo = _FakeRepo(_row("pending"))
    pipeline = _make_pipeline(repo, fake_minio, spec={})
    with pytest.MonkeyPatch.context() as mp:

        async def _fail_transition(
            doc_id: uuid.UUID, *, expect: set[str], to: str, fields: dict
        ) -> bool:
            raise RuntimeError("db down")  # 首个状态迁移（pending→normalizing）即失败

        mp.setattr(repo, "transition", _fail_transition)
        terminal = await pipeline.process_document(_DOC_ID)
    assert terminal == "failed"
    # pending 期失败无阶段前缀（api.md §4.3 阶段化 fail_reason 契约）
    assert repo.failures == ["db down"]


async def test_row_gone_aborts_silently(fake_minio) -> None:
    repo = _FakeRepo(None)
    pipeline = _make_pipeline(repo, fake_minio, spec={})
    assert await pipeline.process_document(_DOC_ID) == "gone"
    assert repo.transitions == [] and repo.failures == []


async def test_ready_row_idempotent(fake_minio) -> None:
    repo = _FakeRepo(_row("ready"))
    pipeline = _make_pipeline(repo, fake_minio, spec={})
    assert await pipeline.process_document(_DOC_ID) == "ready"
    assert repo.transitions == []


async def test_failed_row_idempotent(fake_minio) -> None:
    repo = _FakeRepo(_row("failed"))
    pipeline = _make_pipeline(repo, fake_minio, spec={})
    assert await pipeline.process_document(_DOC_ID) == "failed"
    assert repo.transitions == []


async def test_transition_race_aborts_without_failed_overwrite(
    stub_pipeline_seams, fake_minio
) -> None:
    repo = _FakeRepo(_row("pending"))
    repo.transition_results = [False]  # 首迁移被并发改写/删除（rowcount 0）
    pipeline = _make_pipeline(repo, fake_minio, spec={})
    terminal = await pipeline.process_document(_DOC_ID)
    assert terminal == "aborted"
    assert repo.failures == []  # 竞态失败不写 failed（不覆盖他人状态）


async def test_mark_failed_row_gone_is_benign(fake_minio) -> None:
    """失败落列时行已被删（delete 竞态）→ 落列 rowcount 0 不炸、按 failed 收口。"""
    repo = _FakeRepo(None)
    pipeline = _make_pipeline(repo, fake_minio, spec={})
    with pytest.MonkeyPatch.context() as mp:
        calls = {"n": 0}

        async def _load(doc_id: uuid.UUID) -> dict[str, Any] | None:
            calls["n"] += 1
            return _row("pending") if calls["n"] == 1 else None

        async def _mark_failed(doc_id: uuid.UUID, *, reason: str) -> bool:
            repo.failures.append(reason)
            return False  # 行已删 → rowcount 0

        def _fail_normalize(*a: Any, **k: Any) -> NormalizationResult:
            raise NormalizationError("组件失败")

        mp.setattr(repo, "load", _load)
        mp.setattr(repo, "mark_failed", _mark_failed)
        mp.setattr(kb_pipeline, "normalize_document", _fail_normalize)
        terminal = await pipeline.process_document(_DOC_ID)
    assert terminal == "failed"
    assert repo.failures == ["normalizing: 组件失败"]


# ---------------------------------------------------------------------------
# 重入队（B17 验收：消费 B16 seam 登记行）
# ---------------------------------------------------------------------------


async def test_start_requeues_registered_rows(fake_minio) -> None:
    repo = _FakeRepo(None)
    repo.requeue_ids = [uuid.uuid4() for _ in range(3)]
    pipeline = _make_pipeline(repo, fake_minio, spec={})
    await pipeline.start()
    try:
        assert pipeline.queue.qsize() == 3
    finally:
        await pipeline.stop()


def test_requeue_status_set_matches_contract() -> None:
    # 与 main._KB_REQUEUE_STATUSES 逐字一致（data-model §4 :608 同源）
    from app.main import _KB_REQUEUE_STATUSES

    assert kb_pipeline.REQUEUE_STATUSES == _KB_REQUEUE_STATUSES


# ---------------------------------------------------------------------------
# 纯函数面
# ---------------------------------------------------------------------------


def test_project_parse_meta_mapping() -> None:
    assert kb_pipeline.project_parse_meta({}) is None  # 存储空对象 → REST null
    assert kb_pipeline.project_parse_meta(None) is None
    projected = kb_pipeline.project_parse_meta(
        {"pages": 3, "tables": 2, "code_blocks": 1, "parser": "markitdown", "ocr": {"x": 1}}
    )
    assert projected == {"pages": 3, "tables": 2, "code_blocks": 1}  # parser/附加键不进投影
    assert kb_pipeline.project_parse_meta({"tables": 0, "code_blocks": 0}) == {
        "pages": None,
        "tables": 0,
        "code_blocks": 0,
    }  # 非页式 pages=null（缺省键补 null/0）


def test_project_parse_meta_feishu_sub_object_not_projected() -> None:
    """data-model §1.7 parse_meta.feishu（B20 存储侧附加键，2026-11-25 确认点 C7=(a)）：
    四键子对象与 parser 同为存储侧——§4.3 投影恒三键，feishu/parser 键零泄漏。"""
    storage_meta = {
        "pages": None,
        "tables": 2,
        "code_blocks": 1,
        "parser": "feishu_blocks",
        "feishu": {
            "space_id": "7708408008821771292",
            "node_token": "WTp2dAqBposqlFxMmmhcnLsCnmf",
            "obj_type": "docx",
            "obj_edit_time": 1737100000,
            "degraded_blocks": {"bitable": 1, "block_16": 2},
        },
    }
    projected = kb_pipeline.project_parse_meta(storage_meta)
    assert projected == {"pages": None, "tables": 2, "code_blocks": 1}
    assert "feishu" not in projected and "parser" not in projected


async def test_upload_source_parse_meta_has_no_feishu_key(stub_pipeline_seams, fake_minio) -> None:
    """非飞书来源省略整个 feishu 键（§1.7 feishu 子对象「非飞书来源省略」半边）：
    上传通道 parse_meta 仅常规四键，无 feishu 子对象——storage 落列与投影两侧一致。"""
    _seed_original(fake_minio)
    repo = _FakeRepo(_row("pending"))
    pipeline = _make_pipeline(repo, fake_minio, spec={"kb": {"es_enabled": False}})
    await pipeline.process_document(_DOC_ID)
    stored = repo.transitions[1]["fields"]["parse_meta"]
    assert "feishu" not in stored  # 上传来源整个 feishu 键省略
    assert set(stored) == {"pages", "tables", "code_blocks", "parser"}


def test_chunk_row_meta_seven_keys() -> None:
    chunk = Chunk(
        document_id=_DOC_ID,
        chunk_index=0,
        content="c",
        token_count=1,
        heading_path=["h"],
        kind="code",
        raw_type=7,
        node_id="loc:blk:9",
        loc={"page": 2, "para": 3},
        references=[{"node_id": "loc:doc:1", "kind": "doc", "label": "见"}],
        char_start=0,
        char_end=1,
        content_hash="a" * 64,
    )
    meta = kb_pipeline.chunk_row_meta(chunk)
    assert set(meta) == {
        "node_id",
        "heading_path",
        "loc",
        "references",
        "raw_type",
        "char_start",
        "char_end",
    }
    assert meta["loc"] == {"page": 2, "para": 3}
    assert meta["raw_type"] == 7


def test_extract_url_title() -> None:
    assert kb_pipeline.extract_url_title("# 我的标题\n\n正文", "https://example.com/a") == "我的标题"
    assert kb_pipeline.extract_url_title("无标题正文", "https://sample.org/x") == "sample.org"
    long_text = "# " + "长" * 600
    assert len(kb_pipeline.extract_url_title(long_text, "https://example.com")) <= 512


def test_original_ext_for_doc_type() -> None:
    assert kb_pipeline.original_ext_for_doc_type("url") == "md"
    assert kb_pipeline.original_ext_for_doc_type("pdf") == "pdf"
    assert kb_pipeline.original_ext_for_doc_type("md") == "md"


def test_zero_vector_placeholder_shape() -> None:
    vec = kb_pipeline.embedding_placeholder()
    assert len(vec) == 1024  # KB_EMBEDDING_DIM（一期定死）
    assert all(v == 0 for v in vec)  # 零向量占位——B18 向量通道落地前 NOT NULL 列写入缝


# ---------------------------------------------------------------------------
# A3（plan §3.3）：chunking 段 embedding 接线（embedding_enabled 分支）
# ---------------------------------------------------------------------------


def _fake_embed(*, calls: list[list[str]], exc: Exception | None = None, dim: int = 1024):
    """embed_texts 假体（记录输入批；可编程异常/维度）。"""

    async def _embed(texts: Any, **_kwargs: Any) -> list[list[float]]:
        calls.append(list(texts))
        if exc is not None:
            raise exc
        return [[0.5] * dim for _ in texts]

    return _embed


async def test_embedding_enabled_true_embeds_chunks_with_heading_path(
    stub_pipeline_seams, fake_minio, monkeypatch: pytest.MonkeyPatch
) -> None:
    """① true + mock embed → 真向量入库：输入走 build_embedding_input（标题轨迹
    前置口径——plan §3.2 单一事实源），向量随 chunk 行同批进 replace_chunks。"""
    _seed_original(fake_minio)
    repo = _FakeRepo(_row("pending"))
    calls: list[list[str]] = []
    monkeypatch.setattr(kb_pipeline, "embed_texts", _fake_embed(calls=calls))
    pipeline = _make_pipeline(repo, fake_minio, spec={"kb": {"embedding_enabled": True}})

    terminal = await pipeline.process_document(_DOC_ID)

    assert terminal == "ready"
    assert len(calls) == 1  # 单批（2 chunks）
    assert calls[0] == [
        "标题轨迹：示例文档\n切片正文0",
        "标题轨迹：示例文档\n切片正文1",
    ]
    embeddings = repo.chunk_replacements[0]["embeddings"]
    assert isinstance(embeddings, list) and len(embeddings) == 2
    assert all(len(vector) == 1024 for vector in embeddings)
    assert all(any(value != 0 for value in vector) for vector in embeddings)


async def test_embedding_disabled_zero_embed_calls_placeholder_unchanged(
    stub_pipeline_seams, fake_minio, monkeypatch: pytest.MonkeyPatch
) -> None:
    """② false（缺省）→ 字节级哨兵：零 embed 调用 + replace_chunks 走占位分支
    （embeddings=None——生产路径逐字节写零向量，与 B19 前现状等价）。"""

    async def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("embedding_enabled=false 不得调用 embed（零向量现状哨兵）")

    monkeypatch.setattr(kb_pipeline, "embed_texts", _boom)
    _seed_original(fake_minio)
    repo = _FakeRepo(_row("pending"))
    pipeline = _make_pipeline(repo, fake_minio, spec={"kb": {"embedding_enabled": False}})

    terminal = await pipeline.process_document(_DOC_ID)

    assert terminal == "ready"
    assert repo.chunk_replacements[0]["embeddings"] is None  # 占位分支（零向量）
    placeholder = kb_pipeline.embedding_placeholder()
    assert len(placeholder) == 1024 and all(v == 0 for v in placeholder)


async def test_embedding_failure_retries_once_then_failed(
    stub_pipeline_seams, fake_minio, monkeypatch: pytest.MonkeyPatch
) -> None:
    """③ embed 失败重试 1 次后 failed：fail_reason 前缀精确全等（阶段前缀 +
    脱敏类型名——零原文回显，沿 B18 `_es_failure` 语义）。"""
    _seed_original(fake_minio)
    repo = _FakeRepo(_row("pending"))
    calls: list[list[str]] = []
    monkeypatch.setattr(
        kb_pipeline,
        "embed_texts",
        _fake_embed(calls=calls, exc=RuntimeError("上游原文含敏感内容")),
    )
    pipeline = _make_pipeline(repo, fake_minio, spec={"kb": {"embedding_enabled": True}})

    terminal = await pipeline.process_document(_DOC_ID)

    assert terminal == "failed"
    assert len(calls) == 2  # 重试恰 1 次（首试 + 1）
    assert repo.failures == ["chunking: embedding 写入失败: RuntimeError"]
    assert "上游原文含敏感内容" not in repo.failures[0]
    assert repo.chunk_replacements == []  # embed 失败不写 chunk 行


async def test_embedding_dimension_mismatch_failed(
    stub_pipeline_seams, fake_minio, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⑤ 返回维度≠1024 → failed（防静默脏向量；阶段前缀 chunking）。"""
    _seed_original(fake_minio)
    repo = _FakeRepo(_row("pending"))
    calls: list[list[str]] = []
    monkeypatch.setattr(kb_pipeline, "embed_texts", _fake_embed(calls=calls, dim=3))
    pipeline = _make_pipeline(repo, fake_minio, spec={"kb": {"embedding_enabled": True}})

    terminal = await pipeline.process_document(_DOC_ID)

    assert terminal == "failed"
    assert len(repo.failures) == 1
    assert repo.failures[0].startswith("chunking: ")
    assert "维度" in repo.failures[0]
    assert "0.5" not in repo.failures[0]  # 零向量值回显
    assert repo.chunk_replacements == []


async def test_resume_at_chunking_embeds_once_and_stays_idempotent(
    fake_minio, monkeypatch: pytest.MonkeyPatch
) -> None:
    """④ resume-from-chunking：重入 chunking 段对全量 chunks 恰 embed 一次（单批），
    终态 ready 后再次 process_document 幂等直返零 embed（终态不重复）。"""
    repo = _FakeRepo(_row("chunking"))
    tree = IrNode(
        id="loc:doc:resume-embed",
        type="document",
        title="续跑文档",
        doc_kind="upload",
        meta={"raw_type": "x"},
    )
    await put_ir_json(_USER_ID, _DOC_ID, tree, client=fake_minio)
    calls: list[list[str]] = []
    monkeypatch.setattr(kb_pipeline, "embed_texts", _fake_embed(calls=calls))
    monkeypatch.setattr(kb_pipeline, "chunk_forest", _make_chunk_forest(n=2))
    pipeline = _make_pipeline(repo, fake_minio, spec={"kb": {"embedding_enabled": True}})

    assert await pipeline.process_document(_DOC_ID) == "ready"
    assert len(calls) == 1 and len(calls[0]) == 2

    assert await pipeline.process_document(_DOC_ID) == "ready"
    assert len(calls) == 1  # 终态幂等——再次调用零额外 embed


async def test_normalizing_preserves_feishu_parse_meta_for_attachment_rows(
    stub_pipeline_seams, fake_minio
) -> None:
    """飞书附件行 parse_meta.feishu 键保留（B20 B 线——附件同步 diff 寻址面）：
    normalizing 产物覆写 parse_meta 时合并保留既有 feishu 四键子对象——漏保留则
    附件行 node_token 丢失、同步 diff 恒判「新增」重复建行。"""
    feishu_meta = {
        "space_id": "sp1",
        "node_token": "tok-att",
        "obj_type": "file",
        "obj_edit_time": 1732166400,
    }
    row = _row("pending")
    row["parse_meta"] = {"feishu": feishu_meta}  # worker 建行时预置（附件 pending 行）
    repo = _FakeRepo(row)
    _seed_original(fake_minio)
    pipeline = _make_pipeline(repo, fake_minio)

    outcome = await pipeline.process_document(str(row["id"]))

    assert outcome == "ready"
    chunking_transition = next(
        t for t in repo.transitions if t["to"] == "chunking"
    )
    parse_meta = chunking_transition["fields"]["parse_meta"]
    assert parse_meta["feishu"] == feishu_meta  # 四键原样保留
    assert "parser" in parse_meta  # 归一化产物键照常合入


# ---------------------------------------------------------------------------
# 处置线⑳ doc_kind:url 接线（M3 退出批——URL 收藏通道来源语义落 IR/ES）
# ---------------------------------------------------------------------------


def _source_aware_normalize(calls: list[dict[str, Any]]):
    """kwargs 驱动归一化假体（与 markdown_mapper.make_loc 同构）：收到
    doc_kind="url" → UrlLoc（source_url 投影），其余 → LocalLoc；calls 捕获
    normalize_document 缝收到的 kwargs（红→绿断言面）。"""

    def _normalize(data: bytes, ext: str, **kwargs: Any) -> NormalizationResult:
        calls.append(dict(kwargs))
        doc_kind = kwargs.get("doc_kind", "upload")
        source_url = kwargs.get("source_url")
        if doc_kind == "url":
            loc: LocalLoc | UrlLoc = UrlLoc(origin="url", source_url=source_url or "")
        else:
            loc = LocalLoc(origin="local", file_object_key=kwargs.get("file_object_key"))
        document = IrNode(
            id="loc:doc:stub",
            type="document",
            title=None,
            doc_kind=doc_kind,
            loc=loc,
            meta={"raw_type": "normalizer.document"},
        )
        parse_meta = {"pages": None, "tables": 0, "code_blocks": 0, "parser": "markitdown"}
        return NormalizationResult(document=document, parse_meta=parse_meta)

    return _normalize


async def _run_url_or_upload_row(row: dict[str, Any], fake_minio, monkeypatch):
    """共驱：kwargs 驱动归一化缝 + es_enabled=true 双通道全链至 ready；
    返回 (calls, fake_es, ir_root)。"""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(kb_pipeline, "normalize_document", _source_aware_normalize(calls))
    monkeypatch.setattr(kb_pipeline, "chunk_forest", _make_chunk_forest())
    fake_es = _FakeEsIndexer()
    repo = _FakeRepo(row)
    _seed_original(fake_minio)
    pipeline = _make_pipeline(
        repo, fake_minio, spec={"kb": {"es_enabled": True}}, es_indexer=fake_es
    )
    outcome = await pipeline.process_document(str(row["id"]))
    assert outcome == "ready"
    root = await get_ir_json(_USER_ID, str(row["id"]), client=fake_minio)
    return calls, fake_es, root


async def test_url_row_marks_doc_kind_url_and_projects_source_url(
    fake_minio, monkeypatch: pytest.MonkeyPatch
) -> None:
    """处置线⑳ URL 收藏通道（doc_type=url 行）：归一化缝按来源收
    doc_kind="url" + source_url；IR root.doc_kind=url 且 root.loc=UrlLoc
    （source_url 进可回跳字段、不混入 loc:* ID 前缀）；ES doc_meta.doc_kind
    派生 url（IR root 单一事实源）。"""
    row = _row("pending")
    row["doc_type"] = "url"
    row["source_url"] = "https://example.com/article"
    calls, fake_es, root = await _run_url_or_upload_row(row, fake_minio, monkeypatch)

    # ① 归一化缝收到来源语义参数
    assert calls[0]["doc_kind"] == "url"
    assert calls[0]["source_url"] == "https://example.com/article"
    # ② IR root：doc_kind=url + UrlLoc（source_url 可回跳字段；id 仍 loc:doc: 前缀）
    assert root.doc_kind == "url"
    assert root.id.startswith("loc:doc:")
    assert isinstance(root.loc, UrlLoc)
    assert root.loc.source_url == "https://example.com/article"
    # ③ ES doc_meta 派生（IR root 单一事实源）
    assert fake_es.index_kwargs[0]["doc_meta"]["doc_kind"] == "url"


async def test_upload_row_keeps_doc_kind_upload_without_source_url(
    fake_minio, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上传通道零变化（回归保护）：doc_type=md 行恒 doc_kind="upload"、
    source_url 不投影（None）；root.loc 仍 LocalLoc。"""
    row = _row("pending")  # doc_type="md"、source_url=None
    calls, fake_es, root = await _run_url_or_upload_row(row, fake_minio, monkeypatch)

    assert calls[0]["doc_kind"] == "upload"
    assert calls[0]["source_url"] is None
    assert root.doc_kind == "upload"
    assert isinstance(root.loc, LocalLoc)
    assert root.loc.file_object_key == calls[0]["file_object_key"]
    assert fake_es.index_kwargs[0]["doc_meta"]["doc_kind"] == "upload"


def test_serialize_import_health() -> None:
    assert callable(serialize_ir)


# ---------------------------------------------------------------------------
# B18 ES BM25 写入通道（indexing 段 seam 替换——plan B18 §5）
# ---------------------------------------------------------------------------


class _FakeEsIndexer:
    """KbEsIndexer 假体：记录调用序列；可编程失败（fail_times/ensure_fail_times 次后成功）。"""

    def __init__(
        self,
        *,
        fail_exc: Exception | None = None,
        fail_times: int = 0,
        ensure_fail_exc: Exception | None = None,
        ensure_fail_times: int = 0,
    ) -> None:
        self.calls: list[tuple[str, str]] = []  # (方法名, doc_id) 顺序留痕
        self.index_kwargs: list[dict[str, Any]] = []
        self._fail_exc = fail_exc
        self._fail_left = fail_times
        self._ensure_fail_exc = ensure_fail_exc
        self._ensure_fail_left = ensure_fail_times

    async def ensure_index(self) -> None:
        self.calls.append(("ensure_index", ""))
        if self._ensure_fail_left > 0:
            self._ensure_fail_left -= 1
            assert self._ensure_fail_exc is not None
            raise self._ensure_fail_exc

    async def index_chunks(
        self,
        kb_doc_id: Any,
        *,
        user_id: Any,
        chunks: list[dict[str, Any]],
        doc_meta: dict[str, Any],
    ) -> None:
        self.calls.append(("index_chunks", str(kb_doc_id)))
        self.index_kwargs.append(
            {
                "kb_doc_id": kb_doc_id,
                "user_id": user_id,
                "chunks": list(chunks),
                "doc_meta": dict(doc_meta),
            }
        )
        if self._fail_left > 0:
            self._fail_left -= 1
            assert self._fail_exc is not None
            raise self._fail_exc

    async def delete_document(self, kb_doc_id: Any, *, user_id: Any) -> None:
        self.calls.append(("delete_document", str(kb_doc_id)))


class _RaisingEsIndexer:
    """哨兵假体：任何方法被调即 AssertionError（es_enabled=false 零 ES 调用锁死）。"""

    async def ensure_index(self) -> None:
        raise AssertionError("es_enabled=false 分支不得触 ES（ensure_index）")

    async def index_chunks(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("es_enabled=false 分支不得触 ES（index_chunks）")

    async def delete_document(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("es_enabled=false 分支不得触 ES（delete_document）")

    async def count_by_doc(self, *args: Any, **kwargs: Any) -> int:
        raise AssertionError("es_enabled=false 分支不得触 ES（count_by_candidate）")


def _pg_chunk_row(index: int) -> dict[str, Any]:
    """kb_chunks 行投影假体（list_chunks_by_doc 返回形态）。"""
    return {
        "chunk_id": uuid.UUID(f"22222222-2222-4222-8222-22222222220{index}"),
        "kb_doc_id": uuid.UUID(_DOC_ID),
        "user_id": uuid.UUID(_USER_ID),
        "chunk_index": index,
        "content": f"切片正文{index}",
        "token_count": 4,
        "meta": {
            "node_id": f"loc:blk:{index}",
            "heading_path": ["示例文档"],
            "loc": {"page": index + 1},
            "references": [],
            "raw_type": "p",
            "char_start": 0,
            "char_end": 4,
        },
        "created_at": None,
    }


async def test_es_enabled_false_zero_es_calls_and_transition_unchanged(
    stub_pipeline_seams, fake_minio
) -> None:
    """哨兵测试（字节级不变自证）：es_enabled=false → es_indexer 任何方法零调用 +
    transition 参数与现状逐字段相同（ready 迁移仅 ready_at 一个字段）。"""
    _seed_original(fake_minio)
    repo = _FakeRepo(_row("pending"))
    pipeline = _make_pipeline(
        repo, fake_minio, spec={"kb": {"es_enabled": False}}, es_indexer=_RaisingEsIndexer()
    )
    terminal = await pipeline.process_document(_DOC_ID)
    assert terminal == "ready"
    assert [(t["expect"], t["to"]) for t in repo.transitions] == [
        ({"pending"}, "normalizing"),
        ({"normalizing"}, "chunking"),
        ({"chunking"}, "indexing"),
        ({"indexing"}, "ready"),
    ]
    ready_fields = repo.transitions[-1]["fields"]
    assert set(ready_fields.keys()) == {"ready_at"}
    assert ready_fields["ready_at"] is not None


async def test_es_enabled_true_real_write_then_ready(
    stub_pipeline_seams, fake_minio
) -> None:
    """缝已接线 + es_enabled=true：ensure_index → index_chunks 恰一次 → ready；
    chunks 走 pg 事实源读回（list_chunks_by_doc），doc_meta 携带 IR 派生三键。"""
    _seed_original(fake_minio)
    repo = _FakeRepo(_row("pending"), chunks=[_pg_chunk_row(0), _pg_chunk_row(1)])
    repo.row["catalog_path"] = "研发 > 手册"
    fake = _FakeEsIndexer()
    pipeline = _make_pipeline(
        repo, fake_minio, spec={"kb": {"es_enabled": True}}, es_indexer=fake
    )
    terminal = await pipeline.process_document(_DOC_ID)
    assert terminal == "ready"
    # ensure_index 先于 index_chunks；写入恰一次（无重试）
    assert fake.calls == [("ensure_index", ""), ("index_chunks", _DOC_ID)]
    kwargs = fake.index_kwargs[0]
    assert kwargs["kb_doc_id"] == _DOC_ID  # str 形态（ES _id 确定性键同域）
    assert kwargs["user_id"] == uuid.UUID(_USER_ID)  # 行投影原值透传
    assert [c["chunk_index"] for c in kwargs["chunks"]] == [0, 1]
    # doc_meta：catalog_path 取行值；doc_kind/node_kinds 由 ir.json 派生（假归一化
    # 产物单根树 loc:doc:stub/type=document——kind 派生与 chunker 同一事实源）
    assert kwargs["doc_meta"]["catalog_path"] == "研发 > 手册"
    assert kwargs["doc_meta"]["doc_kind"] == "upload"
    assert kwargs["doc_meta"]["node_kinds"] == {"loc:doc:stub": "document"}
    # ready 前 ES 写入已确认（先写入后迁移）
    assert repo.transitions[-1]["to"] == "ready"
    assert repo.listed_chunks == [_DOC_ID]  # str 形态（process_document 入参同域）


async def test_es_write_retryable_failure_exhausts_and_fails(
    stub_pipeline_seams, fake_minio, monkeypatch: pytest.MonkeyPatch
) -> None:
    """retryable（HTTP 503）三连失败：首试 + 2 重试后 failed，
    fail_reason 阶段化前缀 `indexing:`、零 chunk 内容回显；退避 1s/3s。"""
    from elastic_transport import ApiResponseMeta
    from elasticsearch.exceptions import ApiError as EsApiError

    def _api_error(status: int) -> EsApiError:
        meta = ApiResponseMeta(
            status=status, http_version="1.1", headers={}, duration=0, node=None
        )
        return EsApiError("boom", meta, {})

    _seed_original(fake_minio)
    repo = _FakeRepo(_row("pending"), chunks=[_pg_chunk_row(0)])
    fake = _FakeEsIndexer(fail_exc=_api_error(503), fail_times=3)
    pipeline = _make_pipeline(
        repo, fake_minio, spec={"kb": {"es_enabled": True}}, es_indexer=fake
    )
    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(kb_pipeline, "_es_retry_sleep", _fake_sleep)
    terminal = await pipeline.process_document(_DOC_ID)
    assert terminal == "failed"
    assert fake.calls.count(("index_chunks", _DOC_ID)) == 3  # 首次 + 2 重试
    assert sleeps == [1.0, 3.0]
    # 正向全等（脱敏锁死）：状态码 + 真实尝试数 3，原始 message/正文零回显
    assert repo.failures == ["indexing: ES 写入失败: ApiError(503 attempts=3)"]


async def test_es_write_transient_failure_recovers(
    stub_pipeline_seams, fake_minio, monkeypatch: pytest.MonkeyPatch
) -> None:
    """retryable 单次失败 + 次试成功 → ready（瞬态不误杀）。"""
    from elastic_transport import ApiResponseMeta
    from elasticsearch.exceptions import ApiError as EsApiError

    meta = ApiResponseMeta(
        status=429, http_version="1.1", headers={}, duration=0, node=None
    )
    _seed_original(fake_minio)
    repo = _FakeRepo(_row("pending"), chunks=[_pg_chunk_row(0)])
    fake = _FakeEsIndexer(fail_exc=EsApiError("too many requests", meta, {}), fail_times=1)
    pipeline = _make_pipeline(
        repo, fake_minio, spec={"kb": {"es_enabled": True}}, es_indexer=fake
    )
    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(kb_pipeline, "_es_retry_sleep", _fake_sleep)
    terminal = await pipeline.process_document(_DOC_ID)
    assert terminal == "ready"
    assert fake.calls.count(("index_chunks", _DOC_ID)) == 2  # 首试失败 + 重试成功
    assert sleeps == [1.0]
    assert repo.failures == []


async def test_es_write_non_retryable_failure_fails_immediately(
    stub_pipeline_seams, fake_minio
) -> None:
    """bulk item 失败（EsIndexError 非 retryable）→ 不重试直接 failed。"""
    from app.rag.indexers.es_indexer import EsIndexError

    _seed_original(fake_minio)
    repo = _FakeRepo(_row("pending"), chunks=[_pg_chunk_row(0), _pg_chunk_row(1)])
    fake = _FakeEsIndexer(
        fail_exc=EsIndexError("ES 写入失败: bulk 失败 1/2 item"), fail_times=1
    )
    pipeline = _make_pipeline(repo, fake_minio, spec={"kb": {"es_enabled": True}}, es_indexer=fake)
    terminal = await pipeline.process_document(_DOC_ID)
    assert terminal == "failed"
    assert fake.calls.count(("index_chunks", _DOC_ID)) == 1  # 零重试
    assert repo.failures == ["indexing: ES 写入失败: bulk 失败 1/2 item"]


async def test_es_ensure_index_retryable_failure_recovers(
    stub_pipeline_seams, fake_minio, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ensure_index 首抛 retryable（连接拒）→ 重试面内按同判据重试 → 二次成功 → ready。

    回归锁（评审 P2 #2/#12 同根）：ensure_index 曾在重试 try 面之外，ES 瞬态不可达
    命中它即零重试终态 failed。现判据=ensure_index 恰 2 次 + 退避 [1.0] + 零失败。
    """
    from elastic_transport import ConnectionError as EsConnectionError

    _seed_original(fake_minio)
    repo = _FakeRepo(_row("pending"), chunks=[_pg_chunk_row(0)])
    fake = _FakeEsIndexer(
        ensure_fail_exc=EsConnectionError("connection refused"),
        ensure_fail_times=1,
    )
    pipeline = _make_pipeline(
        repo, fake_minio, spec={"kb": {"es_enabled": True}}, es_indexer=fake
    )
    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(kb_pipeline, "_es_retry_sleep", _fake_sleep)
    terminal = await pipeline.process_document(_DOC_ID)
    assert terminal == "ready"
    assert fake.calls.count(("ensure_index", "")) == 2  # 首试失败 + 重试成功
    assert fake.calls.count(("index_chunks", _DOC_ID)) == 1  # 仅在索引就绪后写一次
    assert sleeps == [1.0]
    assert repo.failures == []


async def test_es_ensure_index_retryable_exhausts_and_fails_desensitized(
    stub_pipeline_seams, fake_minio, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ensure_index 恒 retryable → 重试耗尽 → failed；fail_reason 阶段化前缀 +
    脱敏（原始异常串含内部 host:port 不得回显，仅类型名/attempts 计数）。"""
    from elastic_transport import ConnectionError as EsConnectionError

    raw = "connect to 127.0.0.1:19200 refused (connection aborted)"
    _seed_original(fake_minio)
    repo = _FakeRepo(_row("pending"), chunks=[_pg_chunk_row(0)])
    fake = _FakeEsIndexer(ensure_fail_exc=EsConnectionError(raw), ensure_fail_times=99)
    pipeline = _make_pipeline(
        repo, fake_minio, spec={"kb": {"es_enabled": True}}, es_indexer=fake
    )
    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(kb_pipeline, "_es_retry_sleep", _fake_sleep)
    terminal = await pipeline.process_document(_DOC_ID)
    assert terminal == "failed"
    assert fake.calls.count(("ensure_index", "")) == 3  # 首次 + 2 重试
    assert fake.calls.count(("index_chunks", _DOC_ID)) == 0  # 索引未就绪不写
    assert sleeps == [1.0, 3.0]
    # 正向全等（脱敏锁死）：仅 类型名 + attempts 计数，原始 message/host/port 零回显
    assert repo.failures == ["indexing: ES 写入失败: ConnectionError(attempts=3)"]


async def test_es_ensure_index_non_retryable_fails_immediately_with_real_attempts(
    stub_pipeline_seams, fake_minio
) -> None:
    """ensure_index 抛不可重试异常（HTTP 400）→ 首试即 failed，零重试；
    fail_reason 的 attempts 必须是**真实尝试数 1**（曾误传总预算 3 → 谎报 attempts=3）。"""
    from elastic_transport import ApiResponseMeta
    from elasticsearch.exceptions import ApiError as EsApiError

    meta = ApiResponseMeta(
        status=400, http_version="1.1", headers={}, duration=0, node=None
    )
    _seed_original(fake_minio)
    repo = _FakeRepo(_row("pending"), chunks=[_pg_chunk_row(0)])
    fake = _FakeEsIndexer(
        ensure_fail_exc=EsApiError("illegal argument", meta, {}),
        ensure_fail_times=99,
    )
    pipeline = _make_pipeline(
        repo, fake_minio, spec={"kb": {"es_enabled": True}}, es_indexer=fake
    )
    terminal = await pipeline.process_document(_DOC_ID)
    assert terminal == "failed"
    assert fake.calls.count(("ensure_index", "")) == 1  # 不可重试 → 零重试
    assert fake.calls.count(("index_chunks", _DOC_ID)) == 0
    assert repo.failures == ["indexing: ES 写入失败: ApiError(400 attempts=1)"]


async def test_resume_at_indexing_es_true_reads_pg_chunks_without_rechunk(
    fake_minio,
) -> None:
    """resume-at-indexing（es_enabled=true）：直入写入段——pg 读回 + 先删后 bulk，
    不重切（chunk_forest 零调用）；ir.json 缺失（异常态）→ doc_kind=None 继续。"""
    repo = _FakeRepo(_row("indexing"), chunks=[_pg_chunk_row(0)])
    fake = _FakeEsIndexer()
    pipeline = _make_pipeline(
        repo, fake_minio, spec={"kb": {"es_enabled": True}}, es_indexer=fake
    )

    def _no_chunk_forest(*a: Any, **k: Any) -> Any:
        raise AssertionError("resume at indexing 不得重切")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(kb_pipeline, "chunk_forest", _no_chunk_forest)
        terminal = await pipeline.process_document(_DOC_ID)
    assert terminal == "ready"
    assert [(t["expect"], t["to"]) for t in repo.transitions] == [({"indexing"}, "ready")]
    assert fake.calls == [("ensure_index", ""), ("index_chunks", _DOC_ID)]
    kwargs = fake.index_kwargs[0]
    assert kwargs["doc_meta"]["doc_kind"] is None  # ir.json 不可达 → None 不阻断
    assert kwargs["doc_meta"]["node_kinds"] == {}


async def test_doc_kind_ir_failure_warns_and_continues(
    fake_minio, log_records: list[dict]
) -> None:
    """doc_kind IR 获取异常 → WARN 留痕 + 存 None 继续（doc_kind 辅助字段不阻断写入）。"""
    repo = _FakeRepo(_row("indexing"), chunks=[_pg_chunk_row(0)])
    fake = _FakeEsIndexer()
    pipeline = _make_pipeline(
        repo, fake_minio, spec={"kb": {"es_enabled": True}}, es_indexer=fake
    )
    terminal = await pipeline.process_document(_DOC_ID)
    assert terminal == "ready"
    warns = [r for r in log_records if r.get("level").name == "WARNING"]
    assert warns, "IR 不可达必须 WARN 留痕"


def test_collect_node_kinds_walks_ir_tree() -> None:
    """IR 树 node.id → node.type.value 全量映射（kind 派生与 chunker 同一事实源）。"""
    from app.rag.ir_schema import IrNode, IrNodeType

    root = IrNode(
        id="loc:doc:1",
        type=IrNodeType.DOCUMENT,
        title="文档",
        meta={"raw_type": "normalizer.document"},
        children=[
            IrNode(
                id="loc:blk:0",
                type=IrNodeType.HEADING,
                title="章",
                level=2,
                meta={"raw_type": "h2"},
                children=[
                    IrNode(
                        id="loc:blk:1",
                        type=IrNodeType.CODE,
                        text="print(1)",
                        meta={"raw_type": "code_block"},
                    )
                ],
            ),
            IrNode(
                id="loc:blk:2",
                type=IrNodeType.PARAGRAPH,
                text="正文",
                meta={"raw_type": "p"},
            ),
        ],
    )
    kinds = kb_pipeline.collect_node_kinds(root)
    assert kinds == {
        "loc:doc:1": "document",
        "loc:blk:0": "heading",
        "loc:blk:1": "code",
        "loc:blk:2": "paragraph",
    }
