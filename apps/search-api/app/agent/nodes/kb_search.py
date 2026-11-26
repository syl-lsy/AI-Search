"""kb_search 节点（plan-m3-b19 §2.2 B2 五步闭环 / §4.1——ADR-017 决策①⑦⑧）。

Send 扇出并行副本同构 web_search：每 query 副本经 configurable 注入 KbRetriever
（turn_service 生产装配 get_kb_retriever()；节点纯函数零全局单例直连），双通道
**asyncio.gather 真并发**（修订注 5 修正：各腿自持 asyncio.timeout(
kb.dual_channel_timeout_s=3) 包络——并发下整轮最坏 ≈3s（非串行 3+3），且单腿超时
仅该腿降级、另一腿结果保留）→ KB 候选条目 {chunk_id, kb_doc_id, title,
snippet=content 截断, score, origin:"kb", channel, search_query_index, channel_rank,
node_id, heading_path, content} 写 state.candidates（add reducer 与 web_search 同键）。

**B15 顺序锁第一半**：写 candidates 前 `filter_candidates`（title+snippet+content，
guards 经 Send 输入投影进节点——web_search.py 同构；drop 不进不占 ref_no）。包裹在
synthesis/_sources_block 与 deep 工具返回处（B15 判据「过滤先于转义包裹」天然满足）。

降级链三级（ARCH；修订注 9 表述）：
- KB 空/无命中 → 纯网络（既有——融合面零 KB 卡自然回退）；
- ES 不可用/超时/es_enabled=false → pgvector 单通道（运行时失败 errors 留痕
  `kb_es_unavailable`；**配置关静默短路**——修订注 9：embedding_enabled=false 的
  向量腿短路是实现语义不算第三级，config 关不留痕保持灰度态存量轮 errors[] 逐字节）；
- 双通道全关 → KB 零候选不影响网络侧（哨兵：零 ES 零 pg 调用零事件零 errors）。
**不置 degraded**（§6.2 v1.41 二分口径②——degraded_reason 枚举封闭面零触碰）。

**修订注 6 语料空判定**：index-0 副本入口语料探针（retriever.corpus_chunk_count，
优先 ES 通道）为零文档/零 chunk → 双通道零调用短路 + errors[] 留痕 `kb_corpus_empty`
（强制 hybrid 由路由降级与纠错回边承载——plan §4.4 kb_only 零候选兜底同谓词）。

stage 事件（api.md §7.2/§7.4）：每副本发 done（progress={current: idx+1, total} +
detail 通道摘要）；通道活跃时 done extra 携带可选键 `kb_funnel`
（per_query_top_k/rrf_k/kb_fuse_top_k/es_hits/vec_hits/type_weights——A35 可观测
留痕，键可省不置 null；同 key 多次出现合法——前端按 key 就地更新）。running 不发
（web_search index-0 副本主干单对持有——双 Send 形态共享同一主干对）。
"""

import asyncio
from collections.abc import Mapping
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.agent.graphs.signals import emit_stage
from app.agent.guards import ensure_not_canceled
from app.agent.nodes.fuse_rerank import SNIPPET_MAX_CHARS
from app.agent.safety.content_guard import filter_candidates
from app.agent.safety.config import load_content_guard_config
from app.rag.retrieval import KbRetriever, kb_channels_enabled

__all__ = ["kb_search"]

#: 候选标题兜底（heading_path 空——upload 文档恒有 [doc.title] 轨迹，防御面罕见路径）
_FALLBACK_TITLE = "知识库内容"

#: 语料探针独立短超时（SA-4/修订注 6：探针与双通道检索解耦——超时按「非空」处理
#: 不短路，保守语义：语料可能非空，正常检索照跑；探针仅省 IO 的优化面）
_CORPUS_PROBE_TIMEOUT_S = 1.0

#: 通道失败 errors.reason 映射（bm25 腿沿用 kb_es_unavailable——存量观测面键面不变）
_CHANNEL_FAILURE_REASONS: dict[str, str] = {
    "bm25": "kb_es_unavailable",
    "vector": "kb_vector_unavailable",
}


def _int_or(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return default
    return value


def _candidate_from_row(row: Mapping[str, Any], *, query_index: int, rank: int) -> dict[str, Any]:
    """通道行 → KB 候选条目（plan §4.1 形态；title=heading_path 尾段）。"""
    content = str(row.get("content") or "")
    path = [str(seg) for seg in (row.get("heading_path") or []) if seg]
    return {
        "chunk_id": row.get("chunk_id"),
        "kb_doc_id": row.get("kb_doc_id"),
        "node_id": row.get("node_id"),
        "heading_path": path,
        "title": path[-1] if path else _FALLBACK_TITLE,
        "content": content,
        "snippet": content[:SNIPPET_MAX_CHARS],
        "score": float(row.get("score") or 0.0),
        "origin": "kb",
        "channel": str(row.get("channel") or "bm25"),
        "search_query_index": query_index,
        "channel_rank": rank,
    }


async def kb_search(state: Any, config: RunnableConfig | None = None) -> dict[str, Any]:
    """单 query KB 检索副本：双通道独立包络并行 → 过滤 → candidates + 留痕。"""
    ensure_not_canceled(config)  # 节点边界感知协作式取消（R14②：Send 扇出副本同感知）
    configurable = (config or {}).get("configurable") or {}
    retriever: KbRetriever | None = configurable.get("kb_retriever")
    spec = state.get("spec")
    spec = spec if isinstance(spec, Mapping) else {}
    kb_section = spec.get("kb") if isinstance(spec, Mapping) else None
    kb_section = kb_section if isinstance(kb_section, Mapping) else {}
    es_on, vec_on = kb_channels_enabled(spec)

    query = str(state.get("query") or "")
    index = _int_or(state.get("search_query_index"), 0)
    total = _int_or(state.get("search_query_total"), 1)
    user_id = state.get("user_id")
    timeout_s = float(kb_section.get("dual_channel_timeout_s") or 3)
    per_query_top_k = _int_or(kb_section.get("per_query_top_k"), 30)
    rrf_k = _int_or(kb_section.get("rrf_k"), 60)
    kb_fuse_top_k = _int_or(kb_section.get("kb_fuse_top_k"), 20)
    type_weights = kb_section.get("type_weights") if isinstance(kb_section.get("type_weights"), Mapping) else {}

    # 双灰度哨兵（plan §4.1）：零 ES 零 pg 零候选零事件零 errors——轮行为现状逐字节
    if not (es_on or vec_on):
        return {"candidates": [], "errors": []}
    errors: list[dict[str, Any]] = []
    if retriever is None:
        # 装配缺失但通道开启（灰度安全——不因装配遗漏炸轮；errors 留痕不置 degraded）
        return {
            "candidates": [],
            "errors": [{"stage": "search", "reason": "kb_retriever_unwired"}],
        }

    # 修订注 6：入口语料空判定（仅 index-0 副本探针——非 0 副本零额外 IO）
    corpus_total: int | None = None
    if index == 0:
        try:
            async with asyncio.timeout(_CORPUS_PROBE_TIMEOUT_S):
                corpus_total = await retriever.corpus_chunk_count(
                    prefer_es=es_on, user_id=user_id
                )
        except Exception:  # noqa: BLE001 —— 探针失败/超时不阻断（按非空处理继续检索）
            corpus_total = None
        if corpus_total == 0:
            return {
                "candidates": [],
                "errors": [{"stage": "search", "reason": "kb_corpus_empty"}],
            }

    bm_rows: list[dict[str, Any]] = []
    vec_rows: list[dict[str, Any]] = []
    # 修订注 5（修正——评审 P1 双通道串行违背契约）：两通道 asyncio.gather **真并发**，
    # 各腿自持 asyncio.timeout(timeout_s) 包络——并发下整轮最坏 ≈timeout_s（非
    # 3+3=6s 串行）；单腿超时仅该腿降级（ES 慢不拖垮向量腿，部分结果保留）
    async def _run_bm25() -> list[dict[str, Any]]:
        async with asyncio.timeout(timeout_s):
            return await retriever.bm25_search(
                query, user_id=user_id, k=per_query_top_k
            )

    async def _run_vector() -> list[dict[str, Any]]:
        async with asyncio.timeout(timeout_s):
            return await retriever.vector_search(
                query, user_id=user_id, k=per_query_top_k
            )

    legs: list[tuple[str, Any]] = []
    if es_on:
        legs.append(("bm25", _run_bm25()))
    if vec_on:
        legs.append(("vector", _run_vector()))
    outcomes = await asyncio.gather(
        *(leg for _, leg in legs), return_exceptions=True
    )
    for (channel, _), outcome in zip(legs, outcomes):
        if isinstance(outcome, BaseException):
            errors.append(
                {"stage": "search", "reason": _CHANNEL_FAILURE_REASONS[channel]}
            )
            emit_channel_warning(channel, outcome)
        elif channel == "bm25":
            bm_rows = outcome
        else:
            vec_rows = outcome

    candidates = [
        _candidate_from_row(row, query_index=index, rank=rank)
        for rows in (bm_rows, vec_rows)
        for rank, row in enumerate(rows, start=1)
    ]
    # B15 顺序锁第一半：过滤先于写 candidates（drop 不进不占 ref_no）
    guards = load_content_guard_config(spec if isinstance(spec, Mapping) else None)
    candidates, filter_errors = filter_candidates(candidates, guards, stage="search")
    errors.extend(filter_errors)

    detail_parts: list[str] = []
    if es_on:
        detail_parts.append(f"bm25×{len(bm_rows)}")
    if vec_on:
        detail_parts.append(f"vector×{len(vec_rows)}")
    emit_stage(
        "search",
        "done",
        progress={"current": index + 1, "total": total},
        detail="KB " + (" / ".join(detail_parts) or "双通道零命中"),
        extra={
            "kb_funnel": {
                "per_query_top_k": per_query_top_k,
                "rrf_k": rrf_k,
                "kb_fuse_top_k": kb_fuse_top_k,
                "es_hits": len(bm_rows),
                "vec_hits": len(vec_rows),
                "type_weights": type_weights,
            }
        },
    )
    return {"candidates": candidates, "errors": errors}


def emit_channel_warning(channel: str, exc: BaseException) -> None:
    """通道失败 AgentLog WARN（仅类型名——零 payload 零 host:port 回显）。"""
    from app.core.logger import AgentLog

    AgentLog(
        message=f"kb_search {channel} 通道失败降级（reason=kb_{channel}_unavailable）："
        f"{type(exc).__name__}",
        graph="fast",
        node="kb_search",
    ).warning()
