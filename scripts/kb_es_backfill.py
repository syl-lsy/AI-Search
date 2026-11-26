"""KB ES 存量回填/重建/审计 + content_hash 基线重算脚本（M3 批次 4 / B18 + 批次 5 / B19 索引面）。

OQ-28「pg 事实源、ES 可整体重建，重建脚本随工单交付」的兑现 + B19 必答项①
「存量 content_hash 基线重算」的兑现。四模式：

- `--mode backfill`（缺省）：遍历 kb_documents WHERE status='ready' 逐文档——
  ES count == pg chunk_count → skip（二次执行近零成本）；否则 pg 读回 chunks +
  doc_meta（build_es_doc_meta 同源）→ ensure_index → 先删后 bulk（确定性 _id，
  重跑零重复）；
- `--mode rebuild`：删整索引 → ensure_index（重建 mapping）→ 全量 backfill →
  显式 _refresh（pg 事实源零风险——契约钉字）；
- `--mode audit`：只读核对——ES 总数 vs pg 总数、逐文档 count diff 清单、孤儿清单
  （ES kb_doc_id ∉ pg kb_documents，任意状态）计数；`--purge-orphans` 显式 flag
  一键清孤儿（写操作缺省关闭）；
- `--mode hash-realign`（B19 必答项①建议形态）：存量 content_hash 基线重算——
  遍历 ready 文档 → ir.json（MinIO）→ `chunk_forest` **同一代码路径**重算
  content_hash 方案 A（document_text + 标题结构清单，2026-11-26 用户裁决）新口径
  hash → UPDATE kb_documents.content_hash；**零重导零重嵌**（纯 MinIO GET+hash）；
  行值已一致 → skip 零写（幂等）。不迁移的后果=飞书定时 diff 判定序④对存量
  恒判变更 → 首轮全量误重导 + 重嵌。实际执行归数据工程师阶段（本批只交脚本）。

装配纪律（plan §7.3）：DB 走 get_session_factory、MinIO 走 get_kb_minio_client、
ES 走 get_es_client——与生产路径同一工厂零分叉；bulk 尺寸窗切器在
KbEsIndexer.index_chunks 内（≤500 actions / ≤8MB）；文档间串行零 sleep；
零 chunk 内容回显（仅计数/uuid/title 截断）。

单文档隔离（P3#5 教训，B19 P3#4 收口）：逐文档循环捕获 `_DOC_ISOLATED_ERRORS`
（EsIndexError + 连接类 + DBAPI 连接面）——单文档失败记账续跑，**不中断整批**。

用法（全部在仓库根执行）：

    # 1) 存量回填（幂等，可重复执行）
    uv run --package search-api python scripts/kb_es_backfill.py

    # 2) 整索引重建（mapping 变更后）
    uv run --package search-api python scripts/kb_es_backfill.py --mode rebuild

    # 3) 只读核对（ES vs pg 一致性 + 孤儿清单）
    uv run --package search-api python scripts/kb_es_backfill.py --mode audit

    # 4) 清孤儿（ES 残留「pg 已删」的文档）
    uv run --package search-api python scripts/kb_es_backfill.py --mode audit --purge-orphans

    # 5) 存量 content_hash 基线重算（B19 方案 A——幂等可重复；执行归数据工程师阶段）
    uv run --package search-api python scripts/kb_es_backfill.py --mode hash-realign

    # Makefile 封装（等价）：make kb-es-backfill ARGS="--mode rebuild"

退出码：0=成功（audit 有 diff/孤儿不算失败——数字如实呈现）；1=致命错误。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from typing import Any

from elastic_transport import ConnectionError as EsConnectionError
from elastic_transport import ConnectionTimeout as EsConnectionTimeout
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from app.db.models import KbChunk, KbDocument
from app.db.session import get_session_factory
from app.rag.chunker import chunk_forest
from app.rag.indexers.es_client import get_es_client
from app.rag.indexers.es_indexer import EsIndexError, KbEsIndexer
from app.rag.indexers.es_mapping import index_name
from app.rag.pipeline import KbDocumentRepository, build_es_doc_meta
from app.rag.storage import get_ir_json, get_kb_minio_client

#: 单文档隔离的异常族（P3#5 教训 / B19 P3#4 收口）：ES 业务失败 + 连接类 +
#: DB 连接面——逐文档循环捕获后记账续跑，不中断整批
_DOC_ISOLATED_ERRORS: tuple[type[BaseException], ...] = (
    EsIndexError,
    EsConnectionError,
    EsConnectionTimeout,
    DBAPIError,
)


async def _load_ready_docs() -> list[dict[str, Any]]:
    """ready 文档行投影（id/user_id/chunk_count/catalog_path/title/content_hash
    ——标题仅展示截断；content_hash 供 hash-realign 幂等判定）。"""
    async with get_session_factory()() as session:
        result = await session.execute(
            select(
                KbDocument.id,
                KbDocument.user_id,
                KbDocument.chunk_count,
                KbDocument.catalog_path,
                KbDocument.title,
                KbDocument.status,
                KbDocument.content_hash,
            ).where(KbDocument.status == "ready")
        )
        return [
            {
                "id": row.id,
                "user_id": row.user_id,
                "chunk_count": row.chunk_count,
                "catalog_path": row.catalog_path,
                "title": (row.title or "")[:80],
                "status": row.status,
                "content_hash": row.content_hash,
            }
            for row in result.all()
        ]


async def _load_all_doc_ids() -> set[str]:
    """pg 全部 kb_documents id（任意状态——孤儿判定基准：ES 侧 id ∉ 本集合）。"""
    async with get_session_factory()() as session:
        result = await session.scalars(select(KbDocument.id))
        return {str(value) for value in result.all()}


async def _pg_chunk_total() -> int:
    async with get_session_factory()() as session:
        return int((await session.scalar(select(func.count()).select_from(KbChunk))) or 0)


async def backfill_doc(
    indexer: KbEsIndexer, repo: KbDocumentRepository, doc: dict[str, Any]
) -> str:
    """单文档回填：skip（计数一致）| ok（写入）| diff 标记（audit 模式只读不写）。

    幂等语义：count_by_doc == chunk_count → skip；否则先删后 bulk（确定性 _id
    覆盖，重跑零重复）。返回动作标记供计数汇总。
    """
    es_count = await indexer.count_by_doc(doc["id"])
    if es_count == doc["chunk_count"]:
        return "skip"
    rows = await repo.list_chunks_by_doc(doc["id"])
    doc_meta = await build_es_doc_meta(
        {
            "id": doc["id"],
            "user_id": doc["user_id"],
            "catalog_path": doc["catalog_path"],
        },
        minio=get_kb_minio_client(),
    )
    await indexer.ensure_index()
    await indexer.index_chunks(
        doc["id"], user_id=doc["user_id"], chunks=rows, doc_meta=doc_meta
    )
    return "ok"


async def cmd_backfill(*, rebuild: bool) -> int:
    """backfill/rebuild 主流程：串行逐文档；尾部显式 _refresh。"""
    indexer = KbEsIndexer(client=get_es_client)
    repo = KbDocumentRepository()
    client = get_es_client()
    name = index_name()
    try:
        if rebuild:
            # rebuild：删整索引重建 mapping（pg 事实源零风险——契约钉字 OQ-28）
            if await client.indices.exists(index=name):
                await client.indices.delete(index=name)
                print(f"[rebuild] 索引已删除：{name}")
        await indexer.ensure_index()

        docs = await _load_ready_docs()
        print(f"[backfill] ready 文档数：{len(docs)}（index={name}）")
        ok = skipped = failed = 0
        for index, doc in enumerate(docs, start=1):
            try:
                action = await backfill_doc(indexer, repo, doc)
            except _DOC_ISOLATED_ERRORS as exc:
                # P3#5 教训（B19 P3#4 收口）：连接类异常单文档隔离——不中断整批
                failed += 1
                print(
                    f"[backfill] FAIL doc={doc['id']} title={doc['title']}"
                    f" reason={type(exc).__name__}"
                )
                continue
            if action == "skip":
                skipped += 1
            else:
                ok += 1
            if index % 100 == 0:
                print(f"[backfill] 进度 {index}/{len(docs)}（ok={ok} skip={skipped}）")
        await client.indices.refresh(index=name)
        es_total = int((await client.count(index=name))["count"])
        print(
            f"[backfill] 完成：写入 {ok} / skip {skipped} / 失败 {failed}；"
            f"ES 总数 {es_total}"
        )
        return 0 if failed == 0 else 1
    finally:
        await client.close()


async def cmd_audit(*, purge_orphans: bool) -> int:
    """只读核对：ES 总数 vs pg 总数、逐文档 diff 清单、孤儿清单；--purge-orphans
    显式写操作（清「pg 已删」的 ES 残留——删除腿 best-effort 的孤儿治理，plan §6.2）。"""
    indexer = KbEsIndexer(client=get_es_client)
    client = get_es_client()
    name = index_name()
    try:
        pg_total = await _pg_chunk_total()
        docs = await _load_ready_docs()
        if not await client.indices.exists(index=name):
            print(f"[audit] 索引不存在：{name}（零回填或已重建中途）")
            print(f"[audit] pg chunk 总数 {pg_total} / ES 总数 0")
            return 0
        es_total = int((await client.count(index=name))["count"])
        print(f"[audit] pg chunk 总数 {pg_total} / ES 总数 {es_total}")

        diff: list[str] = []
        count_query_failures = 0
        for doc in docs:
            try:
                es_count = await indexer.count_by_doc(doc["id"])
            except _DOC_ISOLATED_ERRORS as exc:
                # 单文档隔离（P3#4 同款）：单文档 count 查询失败记账续跑
                count_query_failures += 1
                diff.append(
                    f"doc={doc['id']} pg={doc['chunk_count']}"
                    f" es=count 查询失败({type(exc).__name__})"
                )
                continue
            if es_count != doc["chunk_count"]:
                diff.append(f"doc={doc['id']} pg={doc['chunk_count']} es={es_count}")
        if diff:
            print(f"[audit] 逐文档 count diff（{len(diff)} 条 ready 文档不一致）：")
            for line in diff:
                print(f"  - {line}")
        else:
            print("[audit] 逐文档 count：ready 文档全部一致")
        if count_query_failures:
            print(
                f"[audit] count 查询失败 {count_query_failures} 条"
                "（连接类——已单文档隔离计入 diff）"
            )

        # 孤儿清单：ES kb_doc_id ∉ pg kb_documents（任意状态）
        response = await client.search(
            index=name,
            query={"match_all": {}},
            aggs={"docs": {"terms": {"field": "kb_doc_id", "size": 65536}}},
            size=0,
        )
        es_doc_ids = {
            bucket["key"] for bucket in response["aggregations"]["docs"]["buckets"]
        }
        pg_ids = await _load_all_doc_ids()
        orphans = sorted(es_doc_ids - pg_ids)
        if orphans:
            print(f"[audit] 孤儿文档（ES 有 pg 无）：{len(orphans)} 个")
            for orphan in orphans[:20]:
                print(f"  - {orphan}")
            if purge_orphans:
                for orphan in orphans:
                    await indexer.delete_document(kb_doc_id=orphan, user_id="")
                await client.indices.refresh(index=name)
                print(f"[audit] --purge-orphans：已清除 {len(orphans)} 个孤儿文档")
        else:
            print("[audit] 孤儿文档：0")
        return 0
    finally:
        await client.close()


async def realign_doc(
    doc: dict[str, Any], *, session_factory: Any, minio_client: Any
) -> tuple[str, str]:
    """单文档 content_hash 重算（hash-realign 核心；返回 (动作, 新口径 hash)）。

    链路：ir.json（MinIO）→ `chunk_forest` **同一代码路径**（零算法复制——沿
    backfill 复用 build_es_doc_meta 先例）→ 方案 A 新口径 hash（document_text +
    标题结构清单）→ 行值已一致 skip 零写（幂等）→ UPDATE 走 status='ready'
    乐观卫（rowcount 0 = 行不在 ready 态 → gone 不覆盖并发方状态）。

    动作：ok=已更新 / skip=行值已一致 / gone=行不在 ready 态 / ir_missing=ir.json
    不可达（记账续跑——异常态文档不阻断整批）。DB 连接类异常**上抛**由调用方
    循环按 `_DOC_ISOLATED_ERRORS` 隔离（与 backfill 同款纪律）。
    """
    doc_id = str(doc["id"])
    try:
        root = await get_ir_json(str(doc["user_id"]), doc_id, client=minio_client)
    except Exception:  # noqa: BLE001 —— IR 不可达=记账续跑（异常态文档）
        return ("ir_missing", "")
    results = await asyncio.to_thread(chunk_forest, root, document_id=doc_id)
    new_hash = results[0].document_content_hash if results else ""
    if new_hash == doc.get("content_hash"):
        return ("skip", new_hash)
    stmt = (
        update(KbDocument)
        .where(KbDocument.id == uuid.UUID(doc_id), KbDocument.status == "ready")
        .values(content_hash=new_hash)
    )
    async with session_factory() as session:
        result = await session.execute(stmt)
        await session.commit()
    updated = (getattr(result, "rowcount", 0) or 0) > 0
    return ("ok", new_hash) if updated else ("gone", new_hash)


async def cmd_hash_realign() -> int:
    """hash-realign 主流程（B19 必答项①建议形态）：存量 content_hash 基线重算。

    零重导零重嵌（纯 MinIO GET + hash + UPDATE 单列）；单文档失败/ir 缺失记账
    续跑不中断整批。实际执行归数据工程师阶段（小批量试跑 → 全量）。
    """
    session_factory = get_session_factory()
    minio_client = get_kb_minio_client()
    docs = await _load_ready_docs()
    print(f"[hash-realign] ready 文档数：{len(docs)}")
    ok = skipped = missing = gone = failed = 0
    for index, doc in enumerate(docs, start=1):
        try:
            action, _hash = await realign_doc(
                doc, session_factory=session_factory, minio_client=minio_client
            )
        except _DOC_ISOLATED_ERRORS as exc:
            failed += 1
            print(
                f"[hash-realign] FAIL doc={doc['id']} title={doc['title']}"
                f" reason={type(exc).__name__}"
            )
            continue
        if action == "ok":
            ok += 1
        elif action == "skip":
            skipped += 1
        elif action == "ir_missing":
            missing += 1
        else:
            gone += 1
        if index % 100 == 0:
            print(
                f"[hash-realign] 进度 {index}/{len(docs)}"
                f"（更新={ok} 一致跳过={skipped} ir缺失={missing}）"
            )
    print(
        f"[hash-realign] 完成：更新 {ok} / 一致跳过 {skipped} / ir 缺失 {missing} /"
        f" 不在 ready {gone} / 失败 {failed}"
    )
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kb_es_backfill",
        description=(
            "KB ES BM25 存量回填/重建/审计（B18）+ content_hash 基线重算（B19）。"
            "pg 为事实源：backfill 幂等（确定性 _id + 先删后 bulk + count skip 判定），"
            "rebuild 删整索引重建，audit 只读核对（--purge-orphans 显式清孤儿），"
            "hash-realign 存量 content_hash 方案 A 基线重算（零重导零重嵌）。"
        ),
    )
    parser.add_argument(
        "--mode",
        choices=("backfill", "rebuild", "audit", "hash-realign"),
        default="backfill",
        help=(
            "backfill=存量回填（缺省）；rebuild=删索引全量重建；audit=只读核对；"
            "hash-realign=存量 content_hash 基线重算（B19 必答项①）"
        ),
    )
    parser.add_argument(
        "--purge-orphans",
        action="store_true",
        help="仅 audit 模式：清除「ES 有 pg 无」的孤儿文档（写操作，显式开启）",
    )
    args = parser.parse_args(argv)

    if args.mode == "audit":
        return asyncio.run(cmd_audit(purge_orphans=args.purge_orphans))
    if args.purge_orphans:
        parser.error("--purge-orphans 仅与 --mode audit 组合")
    if args.mode == "hash-realign":
        return asyncio.run(cmd_hash_realign())
    return asyncio.run(cmd_backfill(rebuild=args.mode == "rebuild"))


if __name__ == "__main__":
    sys.exit(main())
