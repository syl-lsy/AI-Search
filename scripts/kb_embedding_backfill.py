"""KB 存量向量真实化回填脚本（M3 批次 5 / B19——plan §2.1 A5 / §3.4② 必答项②建议形态）。

背景（ask 门禁级前置事实，2026-11-26 会话 SQL 实测）：kb_chunks 25979/25979 行
embedding 全零向量占位——pgvector 检索腿为退化通道；本脚本把存量 chunk 行向量
真实化（外部 API 计费面——与 ES backfill 语义不同，独立脚本交付）。

三钉定（plan §3.4②）：
- **行级判据**：零向量/缺失/空 = 待回填（`is_zero_vector`）；非零跳过 = 幂等
  （二次执行近零成本）；
- **回填输入=新口径**：`build_embedding_input(content, meta.heading_path)`——
  标题轨迹前置轨迹与 3.2 前置后的新块向量输入**同一单一事实源**（混用两口径
  向量会撕裂向量空间——plan 必答项②钉字）；
- **行级 UPDATE**：逐行 UPDATE kb_chunks.embedding（参数绑定）；数量不符显式
  EmbeddingError（零静默部分写）；连接/网关异常 **per-文档隔离记账续跑**
  （P3#5 同款教训——不中断整批）。

模型别名：`ai_search.model.mapping.json` 快照 `embedding` 键优先，缺省回落内置
text-embedding-v4（nacos-config §5 :379）；维度 = kb_settings.embedding_dim
（1024——embed_texts 内置维度护栏，响应维度不符即 EmbeddingError 不静默）。

时序纪律（plan 必答项②）：回填完成 → 用户收官窗口与 es_enabled 同窗翻
`kb.embedding_enabled=true`；回填完成前向量腿短路（检索面 B1 职责，非本脚本）。
`--mode audit` 提供零向量计数对账出口（plan §10 Risk 5）。

装配纪律：DB 走 get_session_factory（与生产路径同一工厂零分叉）；embed 走
app.rag.embedding.embed_texts（pipeline 接线同一事实源）；零 chunk 内容回显
（仅计数/uuid/title 截断；凭据只经 litellm_settings 单例，零打印）。

实际执行归数据工程师阶段（小批量 10 文档估时先行 → 推全量报告 → 用户确认 →
全量回填）。用法（仓库根执行）：

    # 1) 零向量行回填（幂等可重复；建议先小批量试跑）
    uv run --package search-api python scripts/kb_embedding_backfill.py

    # 2) 零向量统计对账（只读）
    uv run --package search-api python scripts/kb_embedding_backfill.py --mode audit

退出码：0=成功；1=有失败文档（如实呈现，回填未完整——翻键前置条件未满足）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections import Counter
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from app.core.config import kb_settings
from app.db.models import KbChunk, KbDocument
from app.db.session import get_session_factory
from app.rag.embedding import EmbeddingError, build_embedding_input, embed_texts

#: 单文档隔离的异常族（P3#5 同款纪律）：网关失败归一（embed_texts 重试耗尽后
#: EmbeddingError）+ DB 连接面——逐文档循环捕获后记账续跑，不中断整批
_DOC_ISOLATED_ERRORS: tuple[type[BaseException], ...] = (
    EmbeddingError,
    DBAPIError,
    OSError,
)

#: 模型映射 dataId（defaults.DATA_IDS 第 5 项——字面双投影沿脚本侧先例）
_MAPPING_DATA_ID = "ai_search.model.mapping.json"


def is_zero_vector(vector: Any) -> bool:
    """行级判据（必答项②）：零向量/缺失/空 = 待回填；非零（含部分零）= 已回填。

    兼容 pgvector 驱动返回形态（list / numpy 数组——鸭子计数与逐元素判零）。
    """
    if vector is None:
        return True
    try:
        count = len(vector)
    except TypeError:
        return True
    if count == 0:
        return True
    return all(v == 0 for v in vector)


def _spec_mapping() -> dict[str, Any] | None:
    """轮内映射快照（`model.mapping`）——未装载/缺失回落 None（embed_texts
    内置别名 text-embedding-v4 兜底）。pipeline `_default_spec_payload` 同款缝。"""
    from app.config_center import get_asset_center

    try:
        snapshot = get_asset_center().snapshot(_MAPPING_DATA_ID)
    except RuntimeError:
        return None
    payload = snapshot.payload if snapshot is not None else None
    return dict(payload) if isinstance(payload, dict) else None


async def _load_ready_docs() -> list[dict[str, Any]]:
    """ready 文档行投影（id/user_id/title——标题仅展示截断，零内容回显）。"""
    async with get_session_factory()() as session:
        result = await session.execute(
            select(KbDocument.id, KbDocument.user_id, KbDocument.title).where(
                KbDocument.status == "ready"
            )
        )
        return [
            {
                "id": row.id,
                "user_id": row.user_id,
                "title": (row.title or "")[:80],
            }
            for row in result.all()
        ]


async def _load_doc_chunk_rows(
    session: Any, doc_id: uuid.UUID | str
) -> list[dict[str, Any]]:
    """pg 读回文档 chunk 行（ORDER BY chunk_index；含 embedding 供行级判定）。"""
    doc_uuid = doc_id if isinstance(doc_id, uuid.UUID) else uuid.UUID(str(doc_id))
    result = await session.execute(
        select(
            KbChunk.id,
            KbChunk.content,
            KbChunk.meta,
            KbChunk.embedding,
        )
        .where(KbChunk.kb_doc_id == doc_uuid)
        .order_by(KbChunk.chunk_index.asc())
    )
    return [
        {
            "id": row.id,
            "content": row.content,
            "meta": row.meta,
            "embedding": row.embedding,
        }
        for row in result.all()
    ]


async def backfill_doc(
    doc: dict[str, Any],
    *,
    session_factory: Any,
    embed_fn: Any | None = None,
) -> dict[str, int]:
    """单文档回填：行级零向量判定 → 新口径输入分批 embed → 行级 UPDATE。

    幂等 = 非零跳过（全非零文档零 embed 调用零 UPDATE）；embed 返回数量与待回填
    行不符 → EmbeddingError（零静默部分写）。`embed_fn` 为测试缝（缺省=生产
    embed_texts）。
    """
    embed = embed_fn if embed_fn is not None else embed_texts
    async with session_factory() as session:
        rows = await _load_doc_chunk_rows(session, doc["id"])
    pending = [row for row in rows if is_zero_vector(row["embedding"])]
    if not pending:
        return {"embedded": 0, "skipped": len(rows), "total": len(rows)}
    inputs = [
        build_embedding_input(
            row["content"],
            (
                (row["meta"] or {}).get("heading_path")
                if isinstance(row["meta"], dict)
                else []
            )
            or [],
        )
        for row in pending
    ]
    vectors = await embed(inputs, mapping=_spec_mapping())
    if len(vectors) != len(pending):
        raise EmbeddingError(
            f"embedding 返回数量不符: got={len(vectors)} expect={len(pending)}"
        )
    async with session_factory() as session:
        for row, vector in zip(pending, vectors):
            await session.execute(
                update(KbChunk)
                .where(KbChunk.id == row["id"])
                .values(embedding=vector)
            )
        await session.commit()
    return {"embedded": len(pending), "skipped": len(rows) - len(pending), "total": len(rows)}


async def cmd_backfill() -> int:
    """backfill 主流程：串行逐文档，连接/网关异常单文档隔离记账续跑。"""
    session_factory = get_session_factory()
    docs = await _load_ready_docs()
    print(
        f"[embedding-backfill] ready 文档数：{len(docs)}"
        f"（维度={kb_settings.embedding_dim}）"
    )
    embedded_total = skipped_total = failed = 0
    for index, doc in enumerate(docs, start=1):
        try:
            counts = await backfill_doc(doc, session_factory=session_factory)
        except _DOC_ISOLATED_ERRORS as exc:
            failed += 1
            print(
                f"[embedding-backfill] FAIL doc={doc['id']} title={doc['title']}"
                f" reason={type(exc).__name__}"
            )
            continue
        embedded_total += counts["embedded"]
        skipped_total += counts["skipped"]
        if index % 20 == 0:
            print(
                f"[embedding-backfill] 进度 {index}/{len(docs)}"
                f"（已嵌 {embedded_total} / 非零跳过 {skipped_total} / 失败 {failed}）"
            )
    print(
        f"[embedding-backfill] 完成：真实化 {embedded_total} / 非零跳过 {skipped_total}"
        f" / 失败文档 {failed}"
    )
    if failed == 0:
        print(
            "[embedding-backfill] 回填完整：与 es_enabled 同窗翻 kb.embedding_enabled=true"
            "（用户收官动作）"
        )
    return 0 if failed == 0 else 1


async def cmd_audit() -> int:
    """audit（只读）：零向量统计对账——总数 / 零向量数 / 逐文档分布（前 20 条）。"""
    zero_vector = [0.0] * kb_settings.embedding_dim
    async with get_session_factory()() as session:
        total = int(
            await session.scalar(select(func.count()).select_from(KbChunk)) or 0
        )
        result = await session.execute(
            select(KbChunk.kb_doc_id).where(KbChunk.embedding == zero_vector)
        )
        zero_doc_ids = [row[0] for row in result.all()]
    zero = len(zero_doc_ids)
    print(
        f"[audit] chunk 总数 {total} / 零向量 {zero}"
        f"（{zero / total:.1%}）" if total else "[audit] chunk 总数 0"
    )
    if zero_doc_ids:
        per_doc = Counter(str(doc_id) for doc_id in zero_doc_ids)
        print(f"[audit] 逐文档零向量分布（前 {min(20, len(per_doc))} 条）：")
        for doc_id, count in per_doc.most_common(20):
            print(f"  - doc={doc_id} zero_rows={count}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kb_embedding_backfill",
        description=(
            "KB 存量向量真实化回填（B19 必答项②）。行级判据=零向量，幂等=非零跳过；"
            "输入=新口径（heading_path 前置，build_embedding_input 单一事实源）；"
            "异常 per-文档隔离续跑；audit 只读对账。实际执行归数据工程师阶段。"
        ),
    )
    parser.add_argument(
        "--mode",
        choices=("backfill", "audit"),
        default="backfill",
        help="backfill=零向量行回填（缺省）；audit=零向量统计对账（只读）",
    )
    args = parser.parse_args(argv)
    if args.mode == "audit":
        return asyncio.run(cmd_audit())
    return asyncio.run(cmd_backfill())


if __name__ == "__main__":
    sys.exit(main())
