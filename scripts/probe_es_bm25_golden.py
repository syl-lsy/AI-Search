"""黄金测试集 × ES BM25 / 混合检索探针（M3 B18 基线 + B19 hybrid 复测对照）。

读黄金测试集 xlsx（100 条），逐题以**固定查询形态**检索（零题面特判，
基线测量纪律——允许的自由度仅检索字段选择与 analyzer，均按契约钉版）。

`--mode bm25`（缺省，B18 基线形态——**§11 增补后 fields 版本化 bump 为三字段
content/heading_path^2/content.en（2026-11-26 mapping 变更同批）**；对 B18 旧
mapping+旧 fields 的「逐字节一致」承诺仅对 2026-11-26 前版本成立）：

    bool.must   = multi_match(query=问题, fields=["content", "heading_path^2", "content.en"],
                              type="best_fields")     # data-model §1.19「标题链权重高于正文」+ §11 双查
    bool.filter = term(user_id)                        # §1.19「user_id 恒过滤」
    size        = 30                                   # 检索口径 BM25 top30（B19 消费口径）

`--mode hybrid`（B19 复测形态，plan §8.1——**直调 rag/retrieval 钉版入口**：
`KbRetriever.bm25_search`（同一 multi_match 查询体）‖ `KbRetriever.vector_search`
（查询文本 embed → pgvector cosine top30，user_id 过滤恒携带）→ `merge_dedupe.
rrf_fuse_kb` 钉版融合（RRF(k=60) 跨 query×通道累计 + type_weights["other"]=(0.5,0.5)
分轴 + 同章节归并去叶端同前缀 + kb_fuse_top_k=20 截取）。**绕过生产检索开关**
（kb.es_enabled 运行态灰度 false——探针零经生产开关，plan §8.1 复测纪律）；
参数全部读 `defaults.SPEC_CONFIG["kb"]` 钉版值，零硬编码零调参。判定窗 = 融合后
前 kb_fuse_top_k=20 卡（top1/5/10 为其前缀切片）。node_id 经融合后按 chunk_id
批量回查 ES（judging 投影，非检索行为）。**结构口径警示（ask 钉定）**：存量
向量行为零向量占位时向量腿退化（零向量 cosine 距离=NaN、tied 排序任意）——
该状态数字只代表混合形态结构口径（并行发起/RRF 融合序/降级链），不代表
混合检索效果；效果口径前提=向量真实化回填完成（必答项②）。

双口径判定（两 mode 共用，逐字不变）：
  ① 锚点口径：命中结果的 node_id 精确等于 f"fs:blk:{锚点块ID}"
    （库内 kb_chunks.meta.node_id 键形态——fs:blk: 前缀，已知对齐率 98/100）；
  ② 摘录口径：命中结果的 content 去空白归一后，包含摘录前 40 个非空白字符
    （摘录同样去空白归一后取前 40；len<40 取全长）。

指标：top1 / top5 / top10 命中数与命中率（两口径）；miss 项附三分类初判诊断：
  A=锚点不在库（node_id term 零命中——粒度差异/未导入）
  B=锚点在库但摘录未落于该 chunk（合块/粒度/文本形态差异）
  C=目标 chunk 在库且含摘录文本、但排名 > K（排序不足——融合/rerank 治理面）

用法（仓库根执行）：

    uv run --package search-api python scripts/probe_es_bm25_golden.py \
        [--mode bm25|hybrid] \
        [--xlsx resources/大模型面试黄金测试集100条.xlsx] \
        [--out .zcode/tmp/golden_bm25_detail.json] [--user-id <uuid>] [--top-k 30]

退出码：0=完成（命中高低如实呈现，不作合格判定）；1=致命错误（缺列/缺索引/多用户）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from openpyxl import load_workbook

from app.rag.indexers.es_client import get_es_client
from app.rag.indexers.es_mapping import index_name

#: 黄金测试集缺省路径（仓库根相对）
DEFAULT_XLSX = "resources/大模型面试黄金测试集100条.xlsx"

#: 表头关键列名（xlsx 实测表头；「问题」列实际名为「输入（问题）」）
COL_ID = "ID"
COL_QUESTION = "输入（问题）"
COL_EXCERPT = "需引用段落（摘录）"
COL_ANCHOR = "锚点块ID"

#: 检索字段（契约 §1.19：content 正文 + heading_path 标题链权重高于正文；
#: analyzer 由 mapping 钉死 jieba_index/jieba_search，查询侧不再干预）；
#: §11 增补（plan-m3-b19，2026-11-26 用户裁决）：+content.en multi-field——
#: mapping 版本化 bump（§3.1 冻结面 fields 随 mapping 变更版本化），english
#: analyzer 独立分词、同份 query 天然双路（词干形态差可命中）
SEARCH_FIELDS = ["content", "heading_path^2", "content.en"]

#: 检索口径 = BM25 top30（B19 消费口径；top1/5/10 为其前缀切片）
FETCH_SIZE = 30

#: 摘录判定窗（去空白归一后前 40 个非空白字符）
EXCERPT_CHARS = 40

#: 库内 meta.node_id 键前缀（fs:blk:<飞书块 id>）
NODE_ID_PREFIX = "fs:blk:"

#: hybrid 每通道独立超时包络（镜像 kb_search 节点：dual_channel_timeout_s=3）
_HYBRID_CHANNEL_TIMEOUT_S = 3.0

_WS_RE = re.compile(r"\s+")


def ws_norm(text: str) -> str:
    """空白归一：删除全部空白字符（含全角空格/换行/制表）。"""
    return _WS_RE.sub("", text or "")


def safe_repo_path(raw: str) -> str:
    """路径护栏：规范化（解析 ..）并限制在仓库根（cwd）内，防参数路径穿越。

    本脚本为开发者 CLI 工具，路径参数属可信操作者输入；护栏为纵深防御——
    越界即报错退出，不改写不猜路径。
    """
    root = Path.cwd().resolve()
    resolved = (root / raw).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"路径越出仓库根（cwd={root}）：{raw}")
    return str(resolved)


def excerpt_key(excerpt: str) -> str:
    """摘录判定键：去空白归一后前 40 个非空白字符。"""
    return ws_norm(excerpt)[:EXCERPT_CHARS]


@dataclass
class GoldenItem:
    """单条黄金题（xlsx 行投影）。"""

    id: str
    question: str
    anchor_raw: str  # xlsx「锚点块ID」原值
    excerpt: str  # xlsx「需引用段落（摘录）」原值
    node_id: str = ""  # f"fs:blk:{anchor}"——ES 命中比对键
    anchor_hit_rank: int | None = None  # 1-based；None=前 K 未命中
    excerpt_hit_rank: int | None = None
    diag: str = ""  # miss 初判：A/B/C/""（双口径均命中则空）
    error: str = ""  # 查询异常（不中断整批）


@dataclass
class Summary:
    total: int = 0
    anchor_evaluable: int = 0
    excerpt_evaluable: int = 0
    anchor_top1: int = 0
    anchor_top5: int = 0
    anchor_top10: int = 0
    excerpt_top1: int = 0
    excerpt_top5: int = 0
    excerpt_top10: int = 0
    anchor_in_corpus: int = 0  # node_id term 可命中的锚点数（观测键对齐率分母=anchor_evaluable）
    errors: int = 0
    es_channel_errors: int = 0  # hybrid：bm25 通道超时/异常计数（结构口径观测）
    vec_channel_errors: int = 0  # hybrid：向量通道超时/异常计数（结构口径观测）
    anchor_rank_hist: dict[str, int] = field(default_factory=dict)  # 命中排名直方（≤判定窗）
    excerpt_rank_hist: dict[str, int] = field(default_factory=dict)


def load_items(xlsx_path: str) -> list[GoldenItem]:
    """xlsx → 题目列表（表头名精确匹配；数据行空「问题」跳过并计数在 total 之外）。"""
    workbook = load_workbook(xlsx_path, data_only=True)
    sheet = workbook[workbook.sheetnames[0]]
    rows = sheet.iter_rows(values_only=True)
    header = [str(cell).strip() if cell is not None else "" for cell in next(rows)]
    missing = [
        name
        for name in (COL_ID, COL_QUESTION, COL_EXCERPT, COL_ANCHOR)
        if name not in header
    ]
    if missing:
        raise ValueError(f"表头缺列 {missing}；实际表头={header}")
    idx = {name: header.index(name) for name in (COL_ID, COL_QUESTION, COL_EXCERPT, COL_ANCHOR)}

    items: list[GoldenItem] = []
    skipped = 0
    for row in rows:
        values = [row[idx[name]] if idx[name] < len(row) else None for name in
                  (COL_ID, COL_QUESTION, COL_EXCERPT, COL_ANCHOR)]
        question = str(values[1]).strip() if values[1] is not None else ""
        if not question:
            skipped += 1
            continue
        anchor_raw = str(values[3]).strip() if values[3] is not None else ""
        # 键对齐：xlsx 锚点为飞书裸块 id（doxcn...）→ 库内 node_id = fs:blk:<id>；
        # 容错：xlsx 值若已带前缀则剥掉再拼（幂等）。
        bare = anchor_raw.removeprefix(NODE_ID_PREFIX)
        items.append(
            GoldenItem(
                id=str(values[0]).strip() if values[0] is not None else "",
                question=question,
                anchor_raw=anchor_raw,
                excerpt=str(values[2]) if values[2] is not None else "",
                node_id=f"{NODE_ID_PREFIX}{bare}" if bare else "",
            )
        )
    print(f"[probe] 载入 {len(items)} 题（空问题行跳过 {skipped}）；来源 {xlsx_path}")
    return items


def _base_query(question: str, user_id: str) -> dict:
    """固定查询形态（全题统一——基线纪律：零题面特判）。"""
    return {
        "bool": {
            "must": {
                "multi_match": {
                    "query": question,
                    "fields": SEARCH_FIELDS,
                    "type": "best_fields",
                }
            },
            "filter": [{"term": {"user_id": user_id}}],
        }
    }


async def resolve_user_id(client, override: str | None) -> str:
    """user_id 取值：CLI 覆盖 > 索引聚合唯一值（多值即报错——契约恒过滤口径）。"""
    if override:
        return override
    response = await client.search(
        index=index_name(),
        size=0,
        aggs={"users": {"terms": {"field": "user_id", "size": 10}}},
    )
    buckets = response["aggregations"]["users"]["buckets"]
    keys = [b["key"] for b in buckets]
    if len(keys) != 1:
        raise ValueError(f"索引含 {len(keys)} 个 user_id {keys}——须 --user-id 显式指定（契约恒过滤）")
    return keys[0]


async def search_question(client, name: str, question: str, user_id: str) -> list[dict]:
    """单题检索：返回 [{rank, node_id, content}]（rank 1-based，≤FETCH_SIZE）。"""
    response = await client.search(
        index=name,
        query=_base_query(question, user_id),
        size=FETCH_SIZE,
        _source=["node_id", "content"],
    )
    return [
        {"rank": _rank, "node_id": hit["_source"].get("node_id"), "content": hit["_source"].get("content") or ""}
        for _rank, hit in enumerate(response["hits"]["hits"], start=1)
    ]


async def search_question_hybrid(
    retriever, client, name: str, question: str, user_id: str, kb_section: dict
) -> tuple[list[dict], list[str]]:
    """单题 hybrid 检索（plan §8.1：直调 rag/retrieval 钉版入口 + rrf_fuse_kb 钉版融合）。

    形态镜像生产 kb_search 节点（apps/search-api/app/agent/nodes/kb_search.py）：
    各通道独立 asyncio.timeout 包络（ES 慢不拖垮向量腿；通道失败互兜底留 errors）、
    每通道 channel_rank=enumerate(1-based)、候选构造后 rrf_fuse_kb 融合
    （RRF(k=60) 跨 query×通道累计 + weights=type_weights["other"] + 同章节归并
    + kb_fuse_top_k 截取）。BM25 行 node_id 缺（rag/retrieval bm25_search 行投影
    无该键）→ 融合后按 chunk_id 批量回查 ES 补投影（judging 面，非检索行为）。

    返回 ([{rank, node_id, content}] 融合序, 通道错误类型名列表)。
    """
    from app.agent.nodes.merge_dedupe import rrf_fuse_kb

    per_query_top_k = _int_or(kb_section.get("per_query_top_k"), 30)
    rrf_k = _int_or(kb_section.get("rrf_k"), 60)
    kb_fuse_top_k = _int_or(kb_section.get("kb_fuse_top_k"), 20)
    raw_weights = kb_section.get("type_weights")
    weights_map = raw_weights if isinstance(raw_weights, dict) else {}
    pair = weights_map.get("other")
    pair = tuple(pair) if isinstance(pair, (list, tuple)) and len(pair) == 2 else (0.5, 0.5)

    channel_errors: list[str] = []
    bm_rows: list[dict] = []
    vec_rows: list[dict] = []
    try:
        async with asyncio.timeout(_HYBRID_CHANNEL_TIMEOUT_S):
            bm_rows = await retriever.bm25_search(question, user_id=user_id, k=per_query_top_k)
    except Exception as exc:  # noqa: BLE001 —— 通道失败互兜底（镜像 kb_search errors 留痕）
        channel_errors.append(f"bm25:{type(exc).__name__}")
    try:
        async with asyncio.timeout(_HYBRID_CHANNEL_TIMEOUT_S):
            vec_rows = await retriever.vector_search(question, user_id=user_id, k=per_query_top_k)
    except Exception as exc:  # noqa: BLE001
        channel_errors.append(f"vector:{type(exc).__name__}")

    candidates = [
        {
            "chunk_id": row.get("chunk_id"),
            "kb_doc_id": row.get("kb_doc_id"),
            "node_id": row.get("node_id"),
            "heading_path": row.get("heading_path") or [],
            "content": row.get("content") or "",
            "channel": row.get("channel") or "bm25",
            "channel_rank": rank,
        }
        for rows in (bm_rows, vec_rows)
        for rank, row in enumerate(rows, start=1)
    ]
    fused = rrf_fuse_kb(
        candidates, rrf_k=rrf_k, kb_fuse_top_k=kb_fuse_top_k, weights=pair
    )

    # judging 投影：BM25 行 node_id 缺 → 按 chunk_id 批量回查（terms，user_id 恒过滤）
    need = [str(card["chunk_id"]) for card in fused if not card.get("node_id") and card.get("chunk_id")]
    node_map: dict[str, str] = {}
    if need:
        response = await client.search(
            index=name,
            query={
                "bool": {
                    "must": [{"terms": {"chunk_id": need}}],
                    "filter": [{"term": {"user_id": user_id}}],
                }
            },
            size=len(need),
            _source=["chunk_id", "node_id"],
        )
        node_map = {
            hit["_source"].get("chunk_id"): hit["_source"].get("node_id")
            for hit in response["hits"]["hits"]
        }

    hits = [
        {
            "rank": _rank,
            "node_id": card.get("node_id") or node_map.get(str(card.get("chunk_id") or "")),
            "content": str(card.get("content") or ""),
        }
        for _rank, card in enumerate(fused, start=1)
    ]
    return hits, channel_errors


def _int_or(value, default: int) -> int:
    """kb_search 节点同款正整数防御（严格 int 非 bool 且 >0；否则缺省）。"""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return default
    return value


async def diagnose(client, name: str, item: GoldenItem, user_id: str) -> str:
    """miss 初判：A=锚点不在库；B=锚点在库但摘录未落于该 chunk；C=锚点在库含摘录但排名>K。

    判定用**精确键**（node_id term + 归一包含），零 analyzer 依赖，分类可靠。
    """
    if not item.node_id:
        return ""  # 无锚点题不诊断
    response = await client.search(
        index=name,
        query={"bool": {"must": [{"term": {"node_id": item.node_id}}], "filter": [{"term": {"user_id": user_id}}]}},
        size=1,
        _source=["content"],
    )
    hits = response["hits"]["hits"]
    if not hits:
        return "A"
    key = excerpt_key(item.excerpt)
    if key and key in ws_norm(hits[0]["_source"].get("content") or ""):
        return "C"
    return "B"


async def run(args: argparse.Namespace) -> int:
    items = load_items(args.xlsx)
    client = get_es_client()
    name = index_name()
    summary = Summary()
    hybrid = args.mode == "hybrid"
    retriever = None
    kb_section: dict = {}
    if hybrid:
        # 直调钉版入口（plan §8.1）；参数读 defaults.SPEC_CONFIG 钉版值零硬编码
        from app.config_center.defaults import SPEC_CONFIG

        from app.rag.retrieval import KbRetriever

        kb_section = dict(SPEC_CONFIG.get("kb") or {})
        retriever = KbRetriever()
    try:
        if not await client.indices.exists(index=name):
            print(f"[probe] FATAL 索引不存在：{name}（先跑 make kb-es-backfill）")
            return 1
        user_id = await resolve_user_id(client, args.user_id)
        print(f"[probe] index={name} user_id={user_id} mode={args.mode} "
              f"fetch_size={FETCH_SIZE} "
              + (f"fields={SEARCH_FIELDS}" if not hybrid else
                 f"rrf_k={_int_or(kb_section.get('rrf_k'), 60)} "
                 f"kb_fuse_top_k={_int_or(kb_section.get('kb_fuse_top_k'), 20)} "
                 f"per_query_top_k={_int_or(kb_section.get('per_query_top_k'), 30)}"))

        wall_start = time.perf_counter()
        for item in items:
            summary.total += 1
            try:
                if hybrid:
                    hits, channel_errors = await search_question_hybrid(
                        retriever, client, name, item.question, user_id, kb_section
                    )
                    for err in channel_errors:
                        if err.startswith("bm25:"):
                            summary.es_channel_errors += 1
                        else:
                            summary.vec_channel_errors += 1
                    if channel_errors:
                        item.error = ",".join(channel_errors)
                else:
                    hits = await search_question(client, name, item.question, user_id)
            except Exception as exc:  # noqa: BLE001
                # 单题异常不中断整批，计数如实呈现
                item.error = f"{type(exc).__name__}"
                summary.errors += 1
                continue

            # 双口径判定（前缀切片即 top1/5/10 语义）
            for hit in hits:
                if item.anchor_hit_rank is None and item.node_id and hit["node_id"] == item.node_id:
                    item.anchor_hit_rank = hit["rank"]
                if item.excerpt_hit_rank is None:
                    key = excerpt_key(item.excerpt)
                    if key and key in ws_norm(hit["content"]):
                        item.excerpt_hit_rank = hit["rank"]

            # 指标累计
            if item.node_id:
                summary.anchor_evaluable += 1
            key = excerpt_key(item.excerpt)
            if key:
                summary.excerpt_evaluable += 1
            for k, rank_field in (("top1", "anchor_hit_rank"), ("top5", "anchor_hit_rank"), ("top10", "anchor_hit_rank")):
                rank = getattr(item, rank_field)
                if rank is not None and rank <= int(k.removeprefix("top")):
                    setattr(summary, f"anchor_{k}", getattr(summary, f"anchor_{k}") + 1)
            for k in ("top1", "top5", "top10"):
                rank = item.excerpt_hit_rank
                if rank is not None and rank <= int(k.removeprefix("top")):
                    setattr(summary, f"excerpt_{k}", getattr(summary, f"excerpt_{k}") + 1)
            if item.anchor_hit_rank is not None:
                summary.anchor_rank_hist[str(item.anchor_hit_rank)] = (
                    summary.anchor_rank_hist.get(str(item.anchor_hit_rank), 0) + 1
                )
            if item.excerpt_hit_rank is not None:
                summary.excerpt_rank_hist[str(item.excerpt_hit_rank)] = (
                    summary.excerpt_rank_hist.get(str(item.excerpt_hit_rank), 0) + 1
                )

            # miss 初判（任一口径 top10 未命中才查）
            if (item.anchor_hit_rank is None and item.node_id) or (
                key and item.excerpt_hit_rank is None
            ):
                item.diag = await diagnose(client, name, item, user_id)
            if item.anchor_hit_rank is None:
                pass  # in_corpus 由 diag=A 反推累计（A=不在库）
            if item.diag != "A":
                summary.anchor_in_corpus += 1 if item.node_id else 0
        wall_s = time.perf_counter() - wall_start

        # 汇总输出
        def rate(hit: int, denom: int) -> str:
            return f"{hit}/{denom}" + (f" = {hit / denom:.1%}" if denom else " = n/a")

        print(f"\n[probe] ===== {'hybrid 融合' if hybrid else 'BM25 基线'}指标（黄金测试集）=====")
        print(f"[probe] 题数 total={summary.total}  锚点可判={summary.anchor_evaluable}  "
              f"摘录可判={summary.excerpt_evaluable}  查询异常={summary.errors}"
              + (f"  bm25通道错误={summary.es_channel_errors}  "
                 f"vector通道错误={summary.vec_channel_errors}" if hybrid else ""))
        print("[probe] 锚点口径 node_id==fs:blk:<锚点块ID>：")
        print(f"[probe]   top1  {rate(summary.anchor_top1, summary.anchor_evaluable)}")
        print(f"[probe]   top5  {rate(summary.anchor_top5, summary.anchor_evaluable)}")
        print(f"[probe]   top10 {rate(summary.anchor_top10, summary.anchor_evaluable)}")
        print("[probe] 摘录口径 content⊇摘录前40非空白字符：")
        print(f"[probe]   top1  {rate(summary.excerpt_top1, summary.excerpt_evaluable)}")
        print(f"[probe]   top5  {rate(summary.excerpt_top5, summary.excerpt_evaluable)}")
        print(f"[probe]   top10 {rate(summary.excerpt_top10, summary.excerpt_evaluable)}")
        print(f"[probe] 观测键对齐（锚点 node_id 在库）= {summary.anchor_in_corpus}/{summary.anchor_evaluable}")
        print(f"[probe] 锚点命中排名直方 {dict(sorted(summary.anchor_rank_hist.items(), key=lambda kv: int(kv[0])))}")
        print(f"[probe] 摘录命中排名直方 {dict(sorted(summary.excerpt_rank_hist.items(), key=lambda kv: int(kv[0])))}")

        misses = [
            item for item in items
            if (item.anchor_hit_rank is None and item.node_id)
            or (excerpt_key(item.excerpt) and item.excerpt_hit_rank is None)
        ]
        print(f"\n[probe] miss 清单（锚点或摘录 top10 未命中，{len(misses)} 条）：")
        print("[probe] id | 初判(A不在库/B摘录未落于锚点chunk/C排序不足) | 锚点名次 | 摘录名次 | 问题")
        for item in misses:
            print(
                f"[probe] {item.id} | {item.diag or '-'} | "
                f"{item.anchor_hit_rank or '-'} | {item.excerpt_hit_rank or '-'} | "
                f"{item.question[:42]}"
            )

        if args.out:
            payload = {
                "summary": asdict(summary),
                "config": {
                    "xlsx": args.xlsx, "index": name, "user_id": user_id,
                    "mode": args.mode,
                    "fields": SEARCH_FIELDS, "fetch_size": FETCH_SIZE,
                    "excerpt_chars": EXCERPT_CHARS, "node_prefix": NODE_ID_PREFIX,
                    "rrf_k": _int_or(kb_section.get("rrf_k"), 60),
                    "kb_fuse_top_k": _int_or(kb_section.get("kb_fuse_top_k"), 20),
                    "per_query_top_k": _int_or(kb_section.get("per_query_top_k"), 30),
                    "weights_other": list(
                        (kb_section.get("type_weights") or {}).get("other") or (0.5, 0.5)
                    ),
                    "channel_timeout_s": _HYBRID_CHANNEL_TIMEOUT_S,
                    "wall_total_s": round(wall_s, 1),
                },
                "items": [asdict(item) for item in items],
            }
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            print(f"\n[probe] 明细已写 {args.out}")
        return 0
    finally:
        await client.close()


def main(argv: list[str] | None = None) -> int:
    global FETCH_SIZE
    parser = argparse.ArgumentParser(
        prog="probe_es_bm25_golden",
        description="黄金测试集 × ES BM25 基线探针（B18）。基线测量零调参——"
        "查询形态全题统一（multi_match content+heading_path^2+content.en / user_id 恒过滤 / top30）。",
    )
    parser.add_argument(
        "--mode", choices=("bm25", "hybrid"), default="bm25",
        help="bm25=B18 单通道基线形态（缺省）；hybrid=B19 双通道 RRF 融合形态（直调 rag/retrieval）",
    )
    parser.add_argument("--xlsx", default=DEFAULT_XLSX, help=f"黄金测试集路径（缺省 {DEFAULT_XLSX}）")
    parser.add_argument("--out", default=None, help="逐题明细 JSON 输出路径（缺省只打印）")
    parser.add_argument("--user-id", default=None, help="显式 user_id（缺省取索引聚合唯一值）")
    parser.add_argument("--top-k", type=int, default=FETCH_SIZE, help="检索深度（缺省 30=B19 消费口径）")
    args = parser.parse_args(argv)
    args.xlsx = safe_repo_path(args.xlsx)
    if args.out:
        args.out = safe_repo_path(args.out)
    FETCH_SIZE = args.top_k
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
