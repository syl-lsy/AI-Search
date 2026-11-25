"""integration：KB 导入重导/删除级联（B20 B 线——§4.5 同款三存储级联真库形态）。

覆盖面（真 pg + 真 MinIO；ES 腿=B18 seam 未实装——es_indexer 注入假体记录调用，
真 ES 腿挂 B18）：
- 差集删除级联（C5=(a)）：远端已不存在的本地行三面级联删除（pg 行删 + chunks FK
  CASCADE + sources 反向清理 + MinIO 前缀清理）+ stats.deleted_docs 计数入 stats
  不进五数；
- 重导替换级联（api.md §4.7「重导=整文档单元替换」）：obj_edit_time 变大 → 先删后建
  （三面同删 → 重建新行 + 流水线入队）；
- worker 短事务纪律：行对 worker 新 session 可见（commit 后读得到）。
MinIO 缺凭据/不可达 → pytest.skip（沿 test_kb_ir_storage 先例）；pg 缺 .env.test →
requires_db skip。
"""

import asyncio
import uuid
from typing import Any

import pytest

from app.core.config import feishu_settings
from app.rag.feishu_client import NodeInfo
from app.rag.import_worker import (
    FeishuImportDocRepository,
    FeishuImportWorker,
    KbImportJobRepository,
    LocalDoc,
    TokenBucket,
)

pytestmark = pytest.mark.requires_db

_USER = uuid.UUID("00000000-0000-4000-8000-000000000001")  # migrated_db 种子默认用户
_USER_STR = str(_USER)


# ---------------------------------------------------------------------------
# MinIO 活性探测（缺凭据/不可达 skip——沿 test_kb_ir_storage 先例）
# ---------------------------------------------------------------------------


def _minio_client() -> Any:
    from app.rag.storage import get_kb_minio_client

    return get_kb_minio_client()


@pytest.fixture(scope="module")
def minio_client() -> Any:
    try:
        from app.core.config import minio_settings

        client = _minio_client()
        # 探活=kb 桶存在性（级联测试的真实前置；该部署策略对不存在桶的 HEAD
        # 返回 AccessDenied，不能用任意名探测）
        client.bucket_exists(minio_settings.bucket_kb)
    except Exception as exc:  # noqa: BLE001 —— 缺凭据/不可达 → skip
        pytest.skip(f"MinIO 不可达或缺凭据（{type(exc).__name__}）——级联 MinIO 腿 skip")
    return client


# ---------------------------------------------------------------------------
# 假体（飞书面 / ES seam / 流水线入队）
# ---------------------------------------------------------------------------


class _FakeEsIndexer:
    """ES seam 假体：记录删除调用（真 ES 腿挂 B18——app/rag/indexers 未实装）。"""

    def __init__(self) -> None:
        self.deleted: list[tuple[str, str]] = []

    async def delete_document(self, *, kb_doc_id: str, user_id: str) -> None:
        self.deleted.append((kb_doc_id, user_id))


class _FakePipeline:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    def enqueue(self, doc_id: str) -> None:
        self.enqueued.append(doc_id)


class _FakeClient:
    """飞书面假体：远端节点集可参数化（重导替换 / 差集删除两场景）。"""

    def __init__(self, *, empty: bool = False, keep_node: bool = False) -> None:
        self.calls = 0
        self._empty = empty
        self._keep_node = keep_node  # P1 门正臂：远端含 keep（与本地一致不变更）

    async def list_spaces(self) -> list[Any]:
        return [type("S", (), {"space_id": "sp-test", "name": "测试空间"})()]

    async def walk_space_tree(self, space_id: str, **kw: Any) -> Any:
        nodes: list[NodeInfo] = []
        if not self._empty:
            nodes.append(
                NodeInfo(
                    node_token="tok-x",
                    obj_token="obj-x",
                    obj_type="docx",
                    parent_node_token="",
                    node_type="",
                    has_child=False,
                    title="替换文档",
                    obj_edit_time=200,
                )
            )
        if self._keep_node:
            nodes.append(
                NodeInfo(
                    node_token="tok-keep",
                    obj_token="obj-keep",
                    obj_type="docx",
                    parent_node_token="",
                    node_type="",
                    has_child=False,
                    title="保留文档",
                    obj_edit_time=100,
                )
            )
        return type(
            "W",
            (),
            {
                "space_id": space_id,
                "nodes": nodes,
                "errors": [],
                "shortcut_count": 0,
                "truncated_nodes": 0,
                "truncated_depth": 0,
            },
        )()

    async def list_document_blocks(self, document_id: str) -> Any:
        self.calls += 1
        # 最小 page(1)+text(2) 块集（map_blocks 可消费的最简形态）
        run = type("R", (), {"content": "替换正文。", "text_element_style": None})()
        elem = type("El", (), {"text_run": run, "mention_doc": None})()
        payload = type("T", (), {"elements": [elem], "style": None})()
        page = type(
            "B",
            (),
            {"block_id": "page1", "parent_id": "", "children": ["b1"], "block_type": 1},
        )()
        text = type(
            "B",
            (),
            {
                "block_id": "b1",
                "parent_id": "page1",
                "children": [],
                "block_type": 2,
                "text": payload,
            },
        )()
        return type("BP", (), {"items": [page, text], "truncated": False})()


# ---------------------------------------------------------------------------
# 真库辅助（kb_documents/kb_chunks/kb_import_jobs 自管清理——不在 conftest 截断清单）
# ---------------------------------------------------------------------------


async def _cleanup_kb_tables() -> None:
    from sqlalchemy import delete

    from app.db.models import KbChunk, KbDocument, KbImportJob
    from app.db.session import get_session_factory

    async with get_session_factory()() as session:
        await session.execute(delete(KbChunk))
        await session.execute(delete(KbDocument))
        await session.execute(delete(KbImportJob))
        await session.commit()


async def _seed_local_doc(
    *,
    node_token: str,
    edit: int,
    chunks: int = 1,
    title: str = "旧文档",
    catalog_path: str = "旧文档",
) -> str:
    """种入本地飞书来源文档行（parse_meta.feishu.space_id=sp-test——P1 空间过滤对齐）
    + chunks + MinIO 原件/ir 两对象（级联三面的原料）。"""
    from app.db.models import KbChunk, KbDocument
    from app.db.session import get_session_factory
    from app.rag.pipeline import embedding_placeholder
    from app.rag.storage import original_object_key, put_kb_object

    doc_id = uuid.uuid4()
    async with get_session_factory()() as session:
        session.add(
            KbDocument(
                id=doc_id,
                user_id=_USER,
                doc_type="docx",
                title=title,
                source_url=None,
                file_object_key=None,
                file_size_bytes=None,
                status="ready",
                fail_reason=None,
                chunk_count=chunks,
                parse_meta={"feishu": {
                    "space_id": "sp-test",
                    "node_token": node_token,
                    "obj_type": "docx",
                    "obj_edit_time": edit,
                }},
                catalog_ids=[],
                catalog_path=catalog_path,
            )
        )
        await session.flush()  # 文档行先落（chunks FK 前置）
        for i in range(chunks):
            session.add(
                KbChunk(
                    kb_doc_id=doc_id,
                    user_id=_USER,
                    chunk_index=i,
                    content=f"旧切片{i}",
                    token_count=4,
                    embedding=embedding_placeholder(),
                    meta={"heading_path": ["旧文档"], "char_start": 0, "char_end": 4},
                )
            )
        await session.commit()
    # MinIO 两对象（原件 + ir 形态的最简对象——前缀清理面）
    key = original_object_key(_USER_STR, str(doc_id), "md")
    await put_kb_object(key, b"old-bytes", client=_minio_client())
    ir_key = f"kb/{_USER_STR}/{doc_id}/ir.json"
    await put_kb_object(ir_key, b"{}", client=_minio_client())
    return str(doc_id)


async def _doc_row(doc_id: str) -> Any | None:
    from app.db.models import KbDocument
    from app.db.session import get_session_factory

    async with get_session_factory()() as session:
        return await session.get(KbDocument, uuid.UUID(doc_id))


async def _chunk_count(doc_id: str) -> int:
    from sqlalchemy import func, select

    from app.db.models import KbChunk
    from app.db.session import get_session_factory

    async with get_session_factory()() as session:
        return int(
            await session.scalar(
                select(func.count()).select_from(KbChunk)
                .where(KbChunk.kb_doc_id == uuid.UUID(doc_id))
            )
            or 0
        )


def _list_minio_prefix(user_id: str, doc_id: str) -> list[str]:
    client = _minio_client()
    from app.core.config import minio_settings

    bucket = minio_settings.bucket_kb
    return [
        obj.object_name
        for obj in client.list_objects(
            bucket, prefix=f"kb/{user_id}/{doc_id}/", recursive=True
        )
    ]


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


async def test_remote_deletion_cascades_three_storages(
    migrated_db, minio_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """差集删除级联（C5=(a) + P1 完成性门正臂）：远端 walk 完整且非空（keep 与本地
    一致不变更、gone 缺席=已删除）→ gone 三存储级联删除 + stats.deleted_docs 计数入
    stats 不进五数（pg 行删/chunks FK CASCADE/MinIO 前缀清理 + ES seam 记录 +
    kb_import_cascade_delete 逐文档留痕）。"""
    monkeypatch.setattr(feishu_settings, "space_id", "sp-test", raising=False)
    keep_id = await _seed_local_doc(
        node_token="tok-keep", edit=100, chunks=1,
        title="保留文档", catalog_path="保留文档",
    )
    gone_id = await _seed_local_doc(node_token="tok-gone", edit=1, chunks=2)
    es_indexer = _FakeEsIndexer()
    pipeline = _FakePipeline()
    jobs = KbImportJobRepository()
    job_id = await jobs.create_job(
        user_id=_USER_STR,
        trigger="scheduled",
        progress={"total_docs": 0, "processed_docs": 0, "imported_docs": 0,
                  "failed_docs": 0, "skipped_docs": 0},
        stats={},
    )

    worker = FeishuImportWorker(
        jobs=jobs,
        docs=FeishuImportDocRepository(),
        client_provider=lambda: _FakeClient(empty=True, keep_node=True),  # 远端仅 keep
        pipeline=pipeline,  # type: ignore[arg-type]
        spec_payload_provider=lambda: {},
        minio_client=_minio_client,
        purger=None,  # 缺省 MinioPrefixPurger（真 MinIO 前缀清理）
        es_indexer=es_indexer,
        bucket=TokenBucket(min_interval_s=0.0),
    )

    try:
        outcome = await worker.process_job(job_id)

        assert outcome == "completed"
        row = await jobs.load(job_id)
        assert row is not None
        assert row["stats"]["deleted_docs"] == 1  # 计数入 stats 不进五数（仅 gone）
        assert row["progress"]["total_docs"] == 0  # keep unchanged 不计 total_docs
        assert row["progress"]["processed_docs"] == 0

        assert await _doc_row(gone_id) is None  # pg 行删（gone）
        assert await _chunk_count(gone_id) == 0  # chunks FK CASCADE
        assert await _doc_row(keep_id) is not None  # keep 不受影响
        assert es_indexer.deleted == [(gone_id, _USER_STR)]  # ES seam（B18 前假体）
        assert _list_minio_prefix(_USER_STR, gone_id) == []  # MinIO 前缀清理
    finally:
        await _cleanup_kb_tables()


async def test_remote_deletion_skipped_when_remote_empty_but_local_present(
    migrated_db, minio_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P1 完成性门（保守臂真库形态）：远端 walk 完整但零文档单元而本地非空 →
    「空响应」不判「已清空」——零级联零 deleted_docs，任务仍 completed。"""
    monkeypatch.setattr(feishu_settings, "space_id", "sp-test", raising=False)
    doc_id = await _seed_local_doc(node_token="tok-gone", edit=1, chunks=1)
    es_indexer = _FakeEsIndexer()
    pipeline = _FakePipeline()
    jobs = KbImportJobRepository()
    job_id = await jobs.create_job(
        user_id=_USER_STR,
        trigger="scheduled",
        progress={"total_docs": 0, "processed_docs": 0, "imported_docs": 0,
                  "failed_docs": 0, "skipped_docs": 0},
        stats={},
    )

    worker = FeishuImportWorker(
        jobs=jobs,
        docs=FeishuImportDocRepository(),
        client_provider=lambda: _FakeClient(empty=True),
        pipeline=pipeline,  # type: ignore[arg-type]
        spec_payload_provider=lambda: {},
        minio_client=_minio_client,
        purger=None,
        es_indexer=es_indexer,
        bucket=TokenBucket(min_interval_s=0.0),
    )

    try:
        outcome = await worker.process_job(job_id)

        assert outcome == "completed"
        row = await jobs.load(job_id)
        assert row is not None
        assert "deleted_docs" not in (row["stats"] or {})
        assert await _doc_row(doc_id) is not None  # 本地行保全（P1 门）
        assert es_indexer.deleted == []
    finally:
        await _cleanup_kb_tables()


async def test_reimport_replacement_cascades_then_rebuilds(
    migrated_db, minio_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重导替换级联（api.md §4.7）：obj_edit_time 变大 → 先删（三面）后建（新行 +
    流水线入队）；替换删除不计入 stats.deleted_docs（净效果是替换非删除）。"""
    monkeypatch.setattr(feishu_settings, "space_id", "sp-test", raising=False)
    old_doc_id = await _seed_local_doc(node_token="tok-x", edit=100, chunks=1)
    es_indexer = _FakeEsIndexer()
    pipeline = _FakePipeline()
    jobs = KbImportJobRepository()
    job_id = await jobs.create_job(
        user_id=_USER_STR,
        trigger="manual",
        progress={"total_docs": 0, "processed_docs": 0, "imported_docs": 0,
                  "failed_docs": 0, "skipped_docs": 0},
        stats={},
    )

    worker = FeishuImportWorker(
        jobs=jobs,
        docs=FeishuImportDocRepository(),
        client_provider=lambda: _FakeClient(),
        pipeline=pipeline,  # type: ignore[arg-type]
        spec_payload_provider=lambda: {},
        minio_client=_minio_client,
        purger=None,  # 缺省 MinioPrefixPurger
        es_indexer=es_indexer,
        bucket=TokenBucket(min_interval_s=0.0),
    )

    try:
        outcome = await worker.process_job(job_id)

        assert outcome == "completed"
        row = await jobs.load(job_id)
        assert row is not None
        progress = row["progress"]
        assert progress["imported_docs"] == 1
        assert "deleted_docs" not in (row["stats"] or {})  # 替换删除不进 deleted_docs

        assert await _doc_row(old_doc_id) is None  # 旧行三面级联删除
        assert await _chunk_count(old_doc_id) == 0
        assert es_indexer.deleted == [(old_doc_id, _USER_STR)]
        assert _list_minio_prefix(_USER_STR, old_doc_id) == []

        assert len(pipeline.enqueued) == 1  # 新行建成进流水线（G10 计数时点）
        new_doc_id = pipeline.enqueued[0]
        new_row = await _doc_row(new_doc_id)
        assert new_row is not None
        assert new_row.doc_type == "docx"
        assert new_row.status == "chunking"
        assert new_row.parse_meta["parser"] == "feishu_blocks"
        assert new_row.parse_meta["feishu"]["node_token"] == "tok-x"
        assert new_row.catalog_path == "替换文档"  # C9=(a) 顶层=自身标题
    finally:
        await _cleanup_kb_tables()
