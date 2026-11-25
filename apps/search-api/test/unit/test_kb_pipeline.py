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
from app.rag.ir_schema import IrNode, serialize_ir
from app.rag.normalizers.errors import NormalizationError
from app.rag.normalizers.result import NormalizationResult
from app.rag.storage import ir_object_key, original_object_key, put_ir_json

_USER_ID = "00000000-0000-4000-8000-000000000001"
_DOC_ID = "7d2f0a1e-0000-4000-8000-0000000000b1"
_BUCKET = minio_settings.bucket_kb


class _FakeRepo:
    """KbDocumentRepository 假体：记录全部状态迁移与失败落列（零 DB）。"""

    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self.row = row
        self.transitions: list[dict[str, Any]] = []
        self.failures: list[str] = []
        self.chunk_replacements: list[dict[str, Any]] = []
        self.transition_results: list[bool] = []  # 逐次返回值（空=恒 True）
        self.requeue_ids: list[uuid.UUID] = []

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
    ) -> bool:
        self.chunk_replacements.append(
            {"chunks": list(chunks), "content_hash": content_hash, "user_id": user_id}
        )
        if self.row is not None:
            self.row["status"] = "indexing"
            self.row["chunk_count"] = len(chunks)
            self.row["content_hash"] = content_hash
        return True

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
) -> kb_pipeline.KbPipeline:
    return kb_pipeline.KbPipeline(
        repo=repo,
        spec_payload_provider=lambda: spec if spec is not None else {},
        minio_client=lambda: minio,
        worker_count=0,  # unit 直驱 process_document，不 spawn worker
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

    dual = kb_pipeline.resolve_indexing_confirmation(True)
    assert dual.es_enabled is True
    assert dual.channel == "dual"
    assert dual.es_pending_seam is True  # ES 确认点=B19 seam——本批留痕放行


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
        if "B19" in str(r.get("message", "")) or "es_pending_seam" in str(r.get("extra", {}))
    ]
    assert trace, "es_enabled=true 分支必须留痕（B19 seam 未实装，灰度放行）"


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


def test_serialize_import_health() -> None:
    assert callable(serialize_ir)
