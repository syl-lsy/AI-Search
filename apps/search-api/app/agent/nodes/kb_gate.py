"""kb_gate 门内「单 query 检索到 Top K」管线 + 多信号确定性地板（B22 批次 2 第二段）。

契约锚点：
- **抽取位置（ARCH §4.1 :485 钉注——批次 2 第一项工作）**：`app/rag/retrieval.py`
  仅单通道原语（bm25_search/vector_search/corpus_chunk_count），「检索成功→融合→
  rerank→Top K」粒度函数不存在需新建——本模块按盘点清单复用/组装路径落地：双通道
  并行（各腿自持 `kb.dual_channel_timeout_s` 包络，kb_search 修订注 5 同型）→
  `_candidate_from_row`（kb_search 共享形态）→ `filter_candidates`（B15 顺序锁，
  drop 条目不计命中）→ `rrf_fuse_kb`（merge_dedupe 纯函数直用，rrf_k/kb_fuse_top_k/
  type_weights 按题型）→ rerank → Top K。门节点（批次 2 节点段）挂图消费；kb_search
  现路径**不动**（G3 存量逐字节）。
- **A3 硬约束（批次 0 探针 §4 实证）**：`rerank_via_gateway` 对空 documents 无前置
  守卫（网关 400 → RuntimeError）——门内 RRF 融合结果为空时**跳过 rerank 直接判空**，
  不得裸调（nacos-config.md §3 kb节×B22 增注②）。零命中判定取在 rerank 之前的
  召回层（rerank 对非空输入恒返回非空分数集，零命中语义只能在召回层成立）。
- **OQ-10**：门内只 rerank 预览所需少量条目（preview_rerank_cap=preview_count，
  减小门内 n 次 rerank 开销——nacos §3 kb节×B22 增注①）。
- **多信号确定性地板（nacos §3 hitl.noise_floor；R29 权重上调缓解）**：向量相似度
  腿主防线 + rerank 分腿**与组合**——两腿均判空才算该子问题视同空；判定零 LLM；
  缺省（enabled=false + 双阈值 0.0）= 行为等价「不设」（OQ-17 缺省态）；阈值 0.0
  的腿不激活（沿 kb.route.min_top1_score「0=门条件关闭」语义，nacos §3 v1.49）；
  A2 病态 query 防线（批次 0 探针 §4：纯标点 rerank 分反高于真实 0.499 vs 0.368）
  ——向量腿为主防线，组合可地板「高 rerank 分但低向量相似度」的病态子问题。
- **失效方向纪律（plan §4.0 五原则 / R38）**：通道全败=channel_failed（「不知道」
  ≠「没有」——调用方回落现状 hybrid，不判无）；部分通道成功照常参与判定；
  rerank 失败=异常上抛（异常与空结果在类型上分开，绝不落「空列表」分支被解释成
  零命中）；地板永不把非空候选变空除非两腿激活且均判空（缺省回落=不判无→现状口径）。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

import asyncio

from langchain_core.runnables import RunnableConfig
from langgraph.types import Send

from app.agent.graphs.signals import emit_stage
from app.agent.guards import ensure_not_canceled
from app.agent.nodes.fuse_rerank import MODEL_MAPPING_CONFIG, rerank_via_gateway
from app.agent.nodes.kb_search import _candidate_from_row
from app.agent.nodes.merge_dedupe import rrf_fuse_kb
from app.agent.safety.content_guard import ContentGuardConfig, filter_candidates
from app.agent.safety.config import load_content_guard_config
from app.core.logger import AgentLog
from app.db.models.approval import GATE_PAYLOAD_SCHEMA_VERSION
from app.rag.retrieval import KbRetriever, kb_available, kb_channels_enabled

__all__ = [
    "GATE_CONCURRENCY_LIMIT",
    "GateQueryResult",
    "approval_wait",
    "deep_hitl_gate_armed",
    "deep_kb_gate",
    "deep_route_after_gate",
    "deep_route_after_planner",
    "deep_route_by_approval",
    "evaluate_noise_floor",
    "hitl_gate_armed",
    "kb_gate",
    "retrieve_top_k_single_query",
    "route_after_gate",
    "route_by_approval",
]

#: RRF 分轴通道序（rrf_fuse_kb weights=[bm25_w, vec_w] 契约形态的兜底档）
_DEFAULT_WEIGHTS: tuple[float, float] = (0.5, 0.5)

#: 门内逐子问题并发上限（约束⑤：对齐 kb_search Send×N 量级——type_query_map 夹取
#: 上限 4，ARCH §4.1 :484「量级 ≤4」；rewrite 子问题数由夹取保证 ≤4，信号量兜底）
GATE_CONCURRENCY_LIMIT = 4

#: 门前置缺省跳过题型（hitl.skip_query_types 缺省值——spec 缺键回落，与 validator
#: 种子同值；「不进门」≠web 直达，走现状 hybrid，hitl-OQ-15）
_DEFAULT_SKIP_QUERY_TYPES: tuple[str, ...] = ("navigational", "temporal")


@dataclass(frozen=True)
class GateQueryResult:
    """单子问题门内检索结果（判定树消费面 + 预览/留痕投影源）。

    - status：`ok`（至少一路启用通道成功——含零命中）/ `channel_failed`（全部启用
      通道失败——「不知道」，调用方回落现状 hybrid 不判无）；
    - entries：RRF 融合后的 Top K 全序（≤kb_fuse_top_k；floored 时清空——地板语义
      「该子问题视同空」）；
    - preview_entries：rerank 分降序的预览子集（≤preview_rerank_cap，附 rerank_score
      内部键——SSE previews 下发时由门节点剥除，分数不下发契约 api.md §7.2）；
    - top_k_count：判定面条目数（floored → 0；hits_summary.subqueries[].top_k_count）；
    - top1_vector_sim / top1_rerank：地板两腿输入与 hits_summary.top1_score 留痕源
      （rerank 未跑/失败为 None；shadow 期记录分布供噪声地板标定，hitl-OQ-17）；
    - filter_errors：B15 注入过滤留痕（透传进门轮 errors[]，非降级）。
    """

    status: str
    entries: list[dict[str, Any]] = field(default_factory=list)
    preview_entries: list[dict[str, Any]] = field(default_factory=list)
    floored: bool = False
    top_k_count: int = 0
    top1_vector_sim: float | None = None
    top1_rerank: float | None = None
    filter_errors: list[dict[str, Any]] = field(default_factory=list)


def evaluate_noise_floor(
    *,
    floor_enabled: bool,
    vector_sim_min: float,
    rerank_top1_min: float,
    vector_channel_enabled: bool,
    vector_top1_sim: float | None,
    rerank_top1: float | None,
) -> bool:
    """多信号确定性地板判定（纯函数零 LLM——nacos §3 hitl.noise_floor 语义）。

    - 地板总闸：`floor_enabled` 非 True 恒 False（缺省回落=不判无→现状口径）；
    - 腿激活条件：向量腿 = `vector_sim_min > 0 ∧ 向量通道启用`；rerank 腿 =
      `rerank_top1_min > 0`——阈值 0.0 的腿不激活（沿 kb.route.min_top1_score
      「0=门条件关闭」语义；**与组合要求两腿均激活**，单腿配置不构成地板——防
      「enabled=true+双阈值 0.0」把全部子问题地板掉的灾难态）；
    - 腿判空：向量腿 = 向量零召回（top1 None——真实检索「什么都没有」）∨ top1 <
      阈值；rerank 腿 = top1 非 None ∧ top1 < 阈值（rerank 未产分=None 是管道缺口
      而非证据——原则 3「检索失败不是零命中」，不判空）；
    - 判定 = 两腿均激活 ∧ 均判空。
    """
    if floor_enabled is not True:
        return False
    vector_leg_active = vector_sim_min > 0 and vector_channel_enabled
    rerank_leg_active = rerank_top1_min > 0
    if not (vector_leg_active and rerank_leg_active):
        return False
    vector_leg_empty = vector_top1_sim is None or vector_top1_sim < vector_sim_min
    rerank_leg_empty = rerank_top1 is not None and rerank_top1 < rerank_top1_min
    return vector_leg_empty and rerank_leg_empty


def _floor_params(noise_floor: Mapping[str, Any] | None) -> tuple[bool, float, float]:
    """hitl.noise_floor 键族安全读取（严格布尔+阈值域守卫——validator 拒收之外的
    运行面第二道防线；畸形回落缺省=不设）。返回 (enabled, vector_sim_min,
    rerank_top1_min)。"""
    section = noise_floor if isinstance(noise_floor, Mapping) else {}
    enabled = section.get("enabled") is True  # 严格布尔读法（kb_channels_enabled 同判）

    def _threshold(key: str) -> float:
        value = section.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.0
        value = float(value)
        return value if 0.0 <= value <= 1.0 else 0.0

    return enabled, _threshold("vector_sim_min"), _threshold("rerank_top1_min")


def _weights_or_default(type_weights: Sequence[float] | None) -> tuple[float, float]:
    """题型分轴安全读取（[bm25_w, vec_w] 恰两非负数字；畸形回落兜底档）。"""
    if not isinstance(type_weights, (list, tuple)) or len(type_weights) != 2:
        return _DEFAULT_WEIGHTS
    bm25_w, vec_w = type_weights
    for weight in (bm25_w, vec_w):
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight < 0:
            return _DEFAULT_WEIGHTS
    return (float(bm25_w), float(vec_w))


async def retrieve_top_k_single_query(
    retriever: KbRetriever,
    query: str,
    *,
    user_id: Any,
    query_index: int,
    es_on: bool,
    vec_on: bool,
    per_query_top_k: int = 30,
    rrf_k: int = 60,
    kb_fuse_top_k: int = 20,
    type_weights: Sequence[float] | None = None,
    dual_channel_timeout_s: float = 3.0,
    rerank_alias: str,
    rerank_timeout_s: float = 2.0,
    preview_rerank_cap: int = 3,
    noise_floor: Mapping[str, Any] | None = None,
    guards: ContentGuardConfig | None = None,
) -> GateQueryResult:
    """单 query「检索到 Top K」可复用管线（门节点消费；kb_search 共享形态）。

    流程（ARCH §4.1 :485 组装路径）：双通道 gather 真并发（各腿自持包络）→
    `_candidate_from_row`（channel_rank 保留——RRF 分轴输入）→ filter_candidates
    （B15 顺序锁，drop 不计命中）→ rrf_fuse_kb → 【A3】RRF 空=判空零 rerank →
    非空时仅 preview 子集 rerank（OQ-10）→ 多信号地板判定（视同空则清空条目）。

    失败语义：全部启用通道失败 → `status="channel_failed"`（不判无）；rerank 失败
    → 异常上抛（R38——调用方回落现状 hybrid）；单通道失败 → 成功通道结果照常
    （部分成功≠未知）。
    """
    weights = _weights_or_default(type_weights)

    async def _run_bm25() -> list[dict[str, Any]]:
        async with asyncio.timeout(dual_channel_timeout_s):
            return await retriever.bm25_search(query, user_id=user_id, k=per_query_top_k)

    async def _run_vector() -> list[dict[str, Any]]:
        async with asyncio.timeout(dual_channel_timeout_s):
            return await retriever.vector_search(query, user_id=user_id, k=per_query_top_k)

    legs: list[tuple[str, Any]] = []
    if es_on:
        legs.append(("bm25", _run_bm25()))
    if vec_on:
        legs.append(("vector", _run_vector()))
    outcomes = await asyncio.gather(*(leg for _, leg in legs), return_exceptions=True)

    bm_rows: list[dict[str, Any]] = []
    vec_rows: list[dict[str, Any]] = []
    failed_legs = 0
    for (channel, _), outcome in zip(legs, outcomes):
        if isinstance(outcome, BaseException):
            failed_legs += 1
        elif channel == "bm25":
            bm_rows = outcome
        else:
            vec_rows = outcome
    if legs and failed_legs == len(legs):
        # 全部启用通道失败=「不知道」（plan §4.0 原则 3）——调用方回落现状 hybrid
        return GateQueryResult(status="channel_failed")

    candidates = [
        _candidate_from_row(row, query_index=query_index, rank=rank)
        for rows in (bm_rows, vec_rows)
        for rank, row in enumerate(rows, start=1)
    ]
    kept, filter_errors = filter_candidates(candidates, guards, stage="search")

    # 向量腿 top1 余弦相似度（retrieval.py vector_search score=1-余弦距离=相似度
    # 本身；取自原始通道行、先于 RRF——地板向量腿输入）
    top1_vector_sim: float | None = None
    if vec_rows:
        top1_vector_sim = max(float(row.get("score") or 0.0) for row in vec_rows)

    fused = rrf_fuse_kb(kept, rrf_k=rrf_k, kb_fuse_top_k=kb_fuse_top_k, weights=weights)

    if not fused:
        # A3 硬约束：RRF 空=判空、跳过 rerank（空 documents 裸调=网关 400）
        return GateQueryResult(
            status="ok",
            entries=[],
            top_k_count=0,
            top1_vector_sim=top1_vector_sim,
            filter_errors=filter_errors,
        )

    floor_enabled, vector_sim_min, rerank_top1_min = _floor_params(noise_floor)

    # OQ-10：只 rerank 预览所需少量条目（cap=preview_count，validator 值域 1~5）
    cap = max(1, int(preview_rerank_cap))
    subset = fused[:cap]
    documents = [str(item.get("title") or item.get("url") or "") for item in subset]
    scores = await rerank_via_gateway(
        query, documents, model_alias=rerank_alias, timeout_s=rerank_timeout_s
    )

    non_null = [float(s) for s in scores if s is not None]
    top1_rerank: float | None = max(non_null) if non_null else None
    scored_preview = [
        dict(item, rerank_score=float(s))
        for item, s in zip(subset, scores)
        if s is not None
    ]
    # rerank 分降序（None 分已剥除——预览序只用有分条目；同分稳定保 RRF 原序）
    scored_preview.sort(key=lambda item: item["rerank_score"], reverse=True)

    floored = evaluate_noise_floor(
        floor_enabled=floor_enabled,
        vector_sim_min=vector_sim_min,
        rerank_top1_min=rerank_top1_min,
        vector_channel_enabled=vec_on,
        vector_top1_sim=top1_vector_sim,
        rerank_top1=top1_rerank,
    )

    entries = [] if floored else fused
    return GateQueryResult(
        status="ok",
        entries=entries,
        preview_entries=[] if floored else scored_preview,
        floored=floored,
        top_k_count=len(entries),
        top1_vector_sim=top1_vector_sim,
        top1_rerank=top1_rerank,
        filter_errors=filter_errors,
    )


# ---- 门节点（B22 批次 2 第三段：接 fast 子图，plan §3.2 / ARCH §4.1 :484）----


def _int_or(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return default
    return value


def _hitl_section(spec: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """spec.hitl 安全读取（缺节/非 Mapping → 空表——enabled 读法恒 False=G3 缺省）。"""
    hitl = (spec or {}).get("hitl") if isinstance(spec, Mapping) else None
    return hitl if isinstance(hitl, Mapping) else {}


def hitl_gate_armed(state: Mapping[str, Any], spec: Mapping[str, Any] | None) -> bool:
    """门前置判定（plan §4.1 第一级跳过判定——**零 IO**，search_fanout 条件边消费）。

    armed = `hitl.enabled=true` ∧ 首趟〔correction_round=0，纠错第二趟不进门——
    kb_route.py:99-100 同源〕∧ `query_type ∉ hitl.skip_query_types`〔缺省
    navigational/temporal 不进门——「不进门」≠web 直达，走现状 hybrid，hitl-OQ-15〕
    ∧ `kb_available=true`〔双通道全关=「不可用」与「零命中」两件事——前者走既有
    降级不进门〕。G3：hitl 关闭（缺省）恒 False=现状路径逐字节。
    """
    hitl = _hitl_section(spec)
    if hitl.get("enabled") is not True:
        return False
    if int(state.get("correction_round") or 0) != 0:
        return False
    skip = hitl.get("skip_query_types")
    skip_types = tuple(skip) if isinstance(skip, (list, tuple)) else _DEFAULT_SKIP_QUERY_TYPES
    if str(state.get("query_type") or "other") in skip_types:
        return False
    return kb_available(spec)


def _web_sends(state: Mapping[str, Any], queries: Sequence[str]) -> list[Send]:
    """web_search Send×N（入参投影与 search_fanout web Send 逐键同构——B15 spec
    随 Send 输入投影进节点）。"""
    spec = state.get("spec")
    total = len(queries)
    return [
        Send(
            "web_search",
            {
                "query": str(query),
                "query_lang": state.get("query_lang") or "en",
                "time_range": state.get("time_range"),
                "search_query_index": index,
                "search_query_total": total,
                "spec": spec,
            },
        )
        for index, query in enumerate(queries)
    ]


def _kb_sends(state: Mapping[str, Any], queries: Sequence[str]) -> list[Send]:
    """kb_search Send×N（入参投影与 search_fanout kb Send 逐键同构）。"""
    spec = state.get("spec")
    total = len(queries)
    return [
        Send(
            "kb_search",
            {
                "query": str(query),
                "search_query_index": index,
                "search_query_total": total,
                "user_id": state.get("user_id"),
                "spec": spec,
            },
        )
        for index, query in enumerate(queries)
    ]


def route_by_approval(state: Mapping[str, Any]) -> str | list[Send]:
    """START 条件入口两阶段恢复路由（plan §3.4 / ARCH §4.1 :484 条件入口）。

    - 无 `approval_choice`（阶段 1 常规入图）→ `rewrite`（G3：现状 START→rewrite
      边的条件化改写，行为逐字节）；
    - 阶段 2（approval_choice 在场——rewritten_queries 等门载荷已随初始 state 由
      TurnRunner 注入，**不重改写**零 stage(rewrite)）：
      - `kb_only` → 直达 `merge_dedupe`（**零 kb Send**——KB 候选已随 stage-2 初始
        state 注入 `candidates`，约束④；stage(search,running,label=检索知识库) 由
        阶段 2 恢复入口发布，api.md §7.2:845——TurnRunner 段接线）；
      - `web_only` → web Send×N（KB 候选丢弃）；
      - `both`/`custom`〔custom 缺省范围=两者〕→ 现状 web+kb 双 Send（与常规形态
        同构，api.md §7.4:1077-1078——零 kb Send 仅 kb_only 形态）。
    """
    choice = state.get("approval_choice")
    if not choice:
        return "rewrite"
    queries = [str(q) for q in (state.get("rewritten_queries") or [])]
    if not queries:
        return "merge_dedupe"
    if choice == "kb_only":
        return "merge_dedupe"
    if choice == "web_only":
        return _web_sends(state, queries)
    return _web_sends(state, queries) + _kb_sends(state, queries)


def route_after_gate(state: Mapping[str, Any]) -> str | list[Send]:
    """kb_gate 出条件边三叉（plan §3.2）：

    - `await`（enforce 有命中）→ `approval_wait`（空操作终结——图自然 END，TurnRunner
      落 awaiting + 载荷后协程返回）；
    - `web_only`（enforce 全空）→ web Send×N（图一次跑完不中断；KB 候选不进
      candidates——门节点已丢弃）；
    - `hybrid`（shadow 观测 / 通道失败回落 / 门内异常回落 / 前置竞态防御）→ 现状
      web+kb 双 Send（web 段在前供给序，search_fanout hybrid 同形态——shadow 轮由此
      保持现状事件逐字节）。
    """
    action = state.get("kb_gate_action")
    if action == "await":
        return "approval_wait"
    queries = [str(q) for q in (state.get("rewritten_queries") or [])]
    if not queries:
        return "merge_dedupe"  # 防御：无子问题直达汇聚（前置已在扇出边判定）
    if action == "web_only":
        return _web_sends(state, queries)
    return _web_sends(state, queries) + _kb_sends(state, queries)


def approval_wait(state: Any, config: RunnableConfig | None = None) -> dict[str, Any]:
    """空操作终结节点（plan §3.2）：只为让图自然 END——TurnRunner 消费终态流、
    落 awaiting_approval + turn_approvals 载荷后协程返回（两阶段执行，ADR-019）。

    同步零操作（零 IO 零 state 更新）——langgraph 节点 sync/async 混用合法。"""
    return {}


def _materialize_previews(
    results: Sequence[GateQueryResult], *, preview_count: int, preview_chars: int
) -> list[dict[str, Any]]:
    """预览物化（api.md §7.2 previews 形态；OQ-02：3 条/200 字）。

    跨子问题按 chunk_id 去重、按门内 rerank 分全局降序（rerank 分跨 query 可比——
    plan §4.1 预览排序口径）、excerpt 服务端截断 ≤preview_chars（前端零二次处理）。
    输出键集恰为契约六键；分数不下发（rerank_score 不进 preview 条目）。
    """
    pool: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in results:
        for entry in result.preview_entries:
            chunk_id = str(entry.get("chunk_id"))
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            pool.append(entry)
    pool.sort(key=lambda e: float(e.get("rerank_score") or 0.0), reverse=True)
    previews: list[dict[str, Any]] = []
    for entry in pool[: max(1, preview_count)]:
        content = str(entry.get("content") or "")
        previews.append(
            {
                "kb_doc_id": entry.get("kb_doc_id"),
                "chunk_id": entry.get("chunk_id"),
                "title": entry.get("title"),
                "heading_path": list(entry.get("heading_path") or []),
                "excerpt": content[: max(1, preview_chars)],
                "query_index": entry.get("search_query_index"),
            }
        )
    return previews


def _dedupe_candidates(results: Sequence[GateQueryResult]) -> list[dict[str, Any]]:
    """阶段 2 复用候选：各子问题 RRF 融合条目并集按 chunk_id 去重（同 chunk 跨子
    问题留最高融合分——约束④「门内 TopK 并集按 chunk_id 去重注入 candidates」），
    融合分降序（KB-only 注入后 merge_dedupe 按供给序消费）。"""
    best: dict[str, dict[str, Any]] = {}
    for result in results:
        for entry in result.entries:
            chunk_id = str(entry.get("chunk_id"))
            current = best.get(chunk_id)
            if current is None or float(entry.get("score") or 0.0) > float(
                current.get("score") or 0.0
            ):
                best[chunk_id] = entry
    return sorted(best.values(), key=lambda e: float(e.get("score") or 0.0), reverse=True)


def _weights_for(
    kb_section: Mapping[str, Any], query_type: str
) -> tuple[float, float]:
    """题型分轴读取（kb.type_weights[query_type]=[bm25_w, vec_w]；缺键/畸形回落
    兜底档——kb_search type_weights 读取同源形态）。"""
    mapping = kb_section.get("type_weights")
    pair = mapping.get(query_type) if isinstance(mapping, Mapping) else None
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        return _DEFAULT_WEIGHTS
    bm25_w, vec_w = pair
    for weight in (bm25_w, vec_w):
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight < 0:
            return _DEFAULT_WEIGHTS
    return (float(bm25_w), float(vec_w))


def _gate_error(code: str, detail: dict[str, Any]) -> dict[str, Any]:
    return {"code": code, "detail": detail}


async def kb_gate(state: Any, config: RunnableConfig | None = None) -> dict[str, Any]:
    """KB 语料条件化审批门节点（api.md §7.4 带审批形态/web 直达形态的图内承载）。

    判定树（plan §4.1 / api.md §7.4:1086-1094）——判定零 LLM（ADR-019 决策②）：
    1. **前置竞态防御**：hitl 关闭（扇出边 armed 后 spec 快照漂移的纵深兜底）→
       现状 hybrid 直通零检索；
    2. **语料探针**（kb_search 修订注 6 同谓词——语料空=web 直达；探针超时按非空
       继续检索）：逐子问题 `retrieve_top_k_single_query` 并发（信号量帽
       GATE_CONCURRENCY_LIMIT=4，对齐 kb_search Send×N 量级——约束⑤）；
    3. **任一子问题全部通道失败** → 回落现状 hybrid + errors[] `hitl_gate_error`
       （「检索失败不是零命中」不判无；契约 detail={"stage":"kb_gate"}）；
    4. **门内代码异常**（含 rerank 失败——异常上抛在此捕获）→ 同 3（R38：异常与
       空结果类型上分开）；
    5. **地板后全空** → web 直达（action=web_only）：图一次跑完不中断、KB 候选不进
       candidates、payload=None（no_hit 轻量无载荷）、errors[] `hitl_no_hit`；
    6. **任一有值** → has_hit：enforce=await（弹窗，approval_wait END）；shadow=
       现状 hybrid（行为一律现状、零弹窗零直达）。

    留痕面：`stage(rewrite, done).extra.hitl` 判定帧（gate=triggered|shadow +
    verdict + subqueries + gate_ms——第二帧 done 同 key 多次出现合法 §7.2，SSE/
    turn_events 面携带判定留痕；turns.timing 无 running 开档自然不配对，2.5
    stages[].extra 自 turn_events 重建）；errors[] 诊断条随 errors 通道（均不置
    degraded）；AgentLog WARN 同步（异常面）。
    """
    ensure_not_canceled(config)  # 门在阶段 1 内有协程——节点边界取消感知（plan §9）
    spec = state.get("spec")
    spec = spec if isinstance(spec, Mapping) else {}
    hitl = _hitl_section(spec)
    if hitl.get("enabled") is not True:
        # 前置竞态防御（armed 已在扇出边判定；零检索直通=纵深兜底）
        return {"kb_gate_action": "hybrid"}

    queries = [str(q) for q in (state.get("rewritten_queries") or [])]
    if not queries:
        return {"kb_gate_action": "hybrid"}

    configurable = (config or {}).get("configurable") or {}
    retriever: KbRetriever | None = configurable.get("kb_retriever")
    kb_section = spec.get("kb") if isinstance(spec.get("kb"), Mapping) else {}
    kb_section = kb_section if isinstance(kb_section, Mapping) else {}
    es_on, vec_on = kb_channels_enabled(spec)
    shadow = hitl.get("shadow") is True
    preview_count = _int_or(hitl.get("preview_count"), 3)
    preview_chars = _int_or(hitl.get("preview_chars"), 200)
    query_type = str(state.get("query_type") or "other")
    weights = _weights_for(kb_section, query_type)
    if retriever is None:
        # 装配缺失（灰度安全同 kb_search kb_retriever_unwired 判读）→ 回落现状 hybrid
        AgentLog(
            message="kb_gate kb_retriever 未注入——回落现状 hybrid",
            graph="fast",
            node="kb_gate",
        ).warning()
        return {
            "kb_gate_action": "hybrid",
            "errors": [_gate_error("hitl_gate_error", {"stage": "kb_gate"})],
        }

    started = perf_counter()
    try:
        semaphore = asyncio.Semaphore(GATE_CONCURRENCY_LIMIT)

        async def _one(index: int, query: str) -> GateQueryResult:
            async with semaphore:
                return await retrieve_top_k_single_query(
                    retriever,
                    query,
                    user_id=state.get("user_id"),
                    query_index=index,
                    es_on=es_on,
                    vec_on=vec_on,
                    per_query_top_k=_int_or(kb_section.get("per_query_top_k"), 30),
                    rrf_k=_int_or(kb_section.get("rrf_k"), 60),
                    kb_fuse_top_k=_int_or(kb_section.get("kb_fuse_top_k"), 20),
                    type_weights=weights,
                    dual_channel_timeout_s=float(kb_section.get("dual_channel_timeout_s") or 3),
                    rerank_alias=str(
                        (configurable.get("model_mapping") or MODEL_MAPPING_CONFIG).get("rerank")
                        or MODEL_MAPPING_CONFIG["rerank"]
                    ),
                    rerank_timeout_s=float(kb_section.get("rerank_timeout_s") or 2),
                    preview_rerank_cap=preview_count,
                    noise_floor=hitl.get("noise_floor"),
                    guards=load_content_guard_config(spec),
                )

        results = list(await asyncio.gather(*(_one(i, q) for i, q in enumerate(queries))))
    except Exception as exc:  # noqa: BLE001 —— 门内代码异常回落现状 hybrid（R38 不判无）
        AgentLog(
            message=f"kb_gate 门内异常回落现状 hybrid：{type(exc).__name__}",
            graph="fast",
            node="kb_gate",
        ).warning()
        return {
            "kb_gate_action": "hybrid",
            "errors": [_gate_error("hitl_gate_error", {"stage": "kb_gate"})],
        }
    gate_ms = int((perf_counter() - started) * 1000)

    if any(result.status == "channel_failed" for result in results):
        # 任一子问题全部启用通道失败=「不知道」（plan §4.1/R28）——回落现状 hybrid
        AgentLog(
            message="kb_gate 子问题通道全败回落现状 hybrid（不判无）",
            graph="fast",
            node="kb_gate",
        ).warning()
        return {
            "kb_gate_action": "hybrid",
            "errors": [_gate_error("hitl_gate_error", {"stage": "kb_gate"})],
        }

    subqueries: list[dict[str, Any]] = []
    for index, result in enumerate(results):
        entry: dict[str, Any] = {"query_index": index, "top_k_count": result.top_k_count}
        if result.top1_rerank is not None:
            entry["top1_score"] = result.top1_rerank
        subqueries.append(entry)
    all_empty = all(result.top_k_count == 0 for result in results)
    verdict = "no_hit" if all_empty else "has_hit"

    errors: list[dict[str, Any]] = []
    if all_empty:
        action = "hybrid" if shadow else "web_only"
        if not shadow:
            # web 直达轮诊断留痕（enforce 专属——shadow 行为现状 KB 未被跳过）
            errors.append(_gate_error("hitl_no_hit", {"subqueries": subqueries}))
        payload = None  # no_hit 轻量无载荷（data-model §1.20 gate_payload null）
    else:
        action = "hybrid" if shadow else "await"
        payload = {
            "schema_version": GATE_PAYLOAD_SCHEMA_VERSION,
            "rewritten_queries": queries,
            "query_type": query_type,
            "time_range": state.get("time_range"),
            "query_lang": state.get("query_lang"),
            "kb_candidates": _dedupe_candidates(results),
        }

    # 判定面留痕帧（第二帧 stage(rewrite,done)——同 key 多次出现合法 §7.2）
    emit_stage(
        "rewrite",
        "done",
        extra={
            "hitl": {
                "gate": "shadow" if shadow else "triggered",
                "verdict": verdict,
                "subqueries": subqueries,
                "gate_ms": gate_ms,
            }
        },
    )

    update: dict[str, Any] = {
        "kb_gate_action": action,
        "kb_gate_verdict": verdict,
        "kb_gate_subqueries": subqueries,
        "kb_gate_ms": gate_ms,
    }
    if not all_empty:
        # 预览物化仅 has_hit 形态（no_hit 无预览、shadow 有值留预览供观测——
        # §2.5 approval shadow 形态 previews 不投影，本键为 TurnRunner 投影源）
        update["kb_gate_previews"] = _materialize_previews(
            results, preview_count=preview_count, preview_chars=preview_chars
        )
        update["kb_gate_payload"] = payload
    else:
        update["kb_gate_previews"] = []
        update["kb_gate_payload"] = None
    if errors:
        update["errors"] = errors
    return update


# ---- deep 门（B22 批次 4 第一段：plan §3.3 / ARCH v1.83 §4.2 分期注）----
#
# 与 fast 的三点差异（机制面）：
# 1. 独立灰度键：读 `hitl.deep_enabled`（fast 的 hitl_gate_armed/kb_gate 读
#    `hitl.enabled` 零触碰——约束①互不牵动）；无 correction_round 判定（deep 无纠错回边）；
# 2. 检索单位=planner 子课题（state.subtopics）；payload 增 `planner` 键
#    {subtopics, outline}（R30 兼容扩展——schema_version=1 不变，data-model §1.20
#    :622 planner? 预留；批次 2 fast payload 无 planner 键零回归）；
# 3. 深检索由子代理工具承载、无 Send 形态——零命中/通道失败/门内异常 → route 送
#    research 同一执行继续（web/kb 消费面桩化归第二段 R36）；has_hit → approval_wait
#    空操作终结（复用批次 2 节点）。判定器/多信号地板/预览物化/候选去重全部复用
#    本模块既有纯函数（约束⑥复用不重构）。


def deep_hitl_gate_armed(state: Mapping[str, Any], spec: Mapping[str, Any] | None) -> bool:
    """deep 门前置判定（**零 IO**——deep_route_after_planner 消费）。

    armed = `hitl.deep_enabled=true`〔独立灰度键——enabled 零消费，约束①〕∧
    `query_type ∉ hitl.skip_query_types`〔缺省 navigational/temporal 不进门；
    deep planner 产 temporal 可达、navigational 恒死键〔判导航输出 factoid，
    deep_planner.py:16〕〕∧ `kb_available=true`〔双通道全关=「不可用」与「零命中」
    两件事〕。无 correction_round 判定（deep 无纠错回边）。G3：deep_enabled 缺省
    false 恒 False=现状路径逐字节。"""
    hitl = _hitl_section(spec)
    if hitl.get("deep_enabled") is not True:
        return False
    skip = hitl.get("skip_query_types")
    skip_types = tuple(skip) if isinstance(skip, (list, tuple)) else _DEFAULT_SKIP_QUERY_TYPES
    if str(state.get("query_type") or "other") in skip_types:
        return False
    return kb_available(spec)


def deep_route_after_planner(state: Mapping[str, Any]) -> str:
    """planner 出条件边（deep_enabled 门分支——G3 缺省=false→research 现状逐字节，
    条件边与普通边执行面等价零事件差异）。"""
    spec = state.get("spec")
    spec = spec if isinstance(spec, Mapping) else {}
    if deep_hitl_gate_armed(state, spec):
        return "kb_gate"
    return "research"


def deep_route_by_approval(state: Mapping[str, Any]) -> str:
    """START 条件入口两阶段恢复（plan §3.3）：无 `approval_choice`（阶段 1）→
    planner（现状 START→planner 边的条件化改写，行为逐字节）；阶段 2（choice 在场
    ——subtopics/outline 已随初始 state 由 TurnRunner 注入，**不重规划**零
    stage(plan)）→ research。choice 四值在 deep 面同达 research（kb_only/web_only
    的执行面差异=子代理工具作用域，桩化归第二段）。"""
    if state.get("approval_choice"):
        return "research"
    return "planner"


def deep_route_after_gate(state: Mapping[str, Any]) -> str:
    """deep kb_gate 出条件边两叉（plan §3.3）：

    - `await`（enforce 有命中）→ `approval_wait`（空操作终结——图自然 END，
      TurnRunner 落 awaiting + 载荷后协程返回）；
    - 其余（web_only 零命中继续 / hybrid shadow·通道失败·门内异常回落）→
      `research` 同一执行继续（web/kb 消费面=子代理工具，桩化归第二段 R36）。"""
    if state.get("kb_gate_action") == "await":
        return "approval_wait"
    return "research"


async def deep_kb_gate(state: Any, config: RunnableConfig | None = None) -> dict[str, Any]:
    """deep KB 审批门节点（plan §3.3 / ARCH §4.2 分期注）。

    判定树与 fast kb_gate 同构（判定零 LLM，ADR-019 决策②）——检索管线复用
    retrieve_top_k_single_query（多信号地板/R38 异常语义/部分成功语义全同源）：

    1. **前置竞态防御**：deep_enabled 关（扇出边 armed 后 spec 快照漂移的纵深兜底）
       → 现状直通零检索（action=hybrid）；
    2. **逐子课题检索**（state.subtopics，信号量帽 GATE_CONCURRENCY_LIMIT=4）；
    3. **任一子课题全部通道失败** → hybrid + errors[] hitl_gate_error（不判无）；
    4. **门内代码异常** → 同 3（R38）；
    5. **地板后全空** → action=web_only：**图不终结**——route 送 research 继续
       （零命中子代理 kb_search 桩化归第二段）；payload=None、errors[] hitl_no_hit；
    6. **任一有值** → has_hit：enforce=await（approval_wait END，TurnRunner 落
       awaiting）；shadow=hybrid（research 照跑）。

    留痕面：`stage(plan, done).extra.hitl` 判定帧（gate=triggered|shadow + verdict +
    subqueries + gate_ms——同 key 多次出现合法 §7.2；deep 侧 plan 帧归属随批次 4
    契约增量钉正，api.md §7.4:1723 挂账注）；AgentLog graph="deep"。
    """
    ensure_not_canceled(config)  # 门在阶段 1 内有协程——节点边界取消感知
    spec = state.get("spec")
    spec = spec if isinstance(spec, Mapping) else {}
    hitl = _hitl_section(spec)
    if hitl.get("deep_enabled") is not True:
        # 前置竞态防御（armed 已在 planner 出边判定；零检索直通=纵深兜底）
        return {"kb_gate_action": "hybrid"}

    subtopics = [str(s) for s in (state.get("subtopics") or [])]
    if not subtopics:
        return {"kb_gate_action": "hybrid"}
    outline = [dict(o) if isinstance(o, Mapping) else {"section": str(o)} for o in (state.get("outline") or [])]

    configurable = (config or {}).get("configurable") or {}
    retriever: KbRetriever | None = configurable.get("kb_retriever")
    raw_kb = spec.get("kb")
    kb_section = raw_kb if isinstance(raw_kb, Mapping) else {}
    kb_section = kb_section if isinstance(kb_section, Mapping) else {}
    es_on, vec_on = kb_channels_enabled(spec)
    shadow = hitl.get("shadow") is True
    preview_count = _int_or(hitl.get("preview_count"), 3)
    preview_chars = _int_or(hitl.get("preview_chars"), 200)
    query_type = str(state.get("query_type") or "other")
    weights = _weights_for(kb_section, query_type)
    if retriever is None:
        AgentLog(
            message="deep_kb_gate kb_retriever 未注入——回落现状路径",
            graph="deep",
            node="kb_gate",
        ).warning()
        return {
            "kb_gate_action": "hybrid",
            "errors": [_gate_error("hitl_gate_error", {"stage": "kb_gate"})],
        }

    started = perf_counter()
    try:
        semaphore = asyncio.Semaphore(GATE_CONCURRENCY_LIMIT)

        async def _one(index: int, subtopic: str) -> GateQueryResult:
            async with semaphore:
                return await retrieve_top_k_single_query(
                    retriever,
                    subtopic,
                    user_id=state.get("user_id"),
                    query_index=index,
                    es_on=es_on,
                    vec_on=vec_on,
                    per_query_top_k=_int_or(kb_section.get("per_query_top_k"), 30),
                    rrf_k=_int_or(kb_section.get("rrf_k"), 60),
                    kb_fuse_top_k=_int_or(kb_section.get("kb_fuse_top_k"), 20),
                    type_weights=weights,
                    dual_channel_timeout_s=float(kb_section.get("dual_channel_timeout_s") or 3),
                    rerank_alias=str(
                        (configurable.get("model_mapping") or MODEL_MAPPING_CONFIG).get("rerank")
                        or MODEL_MAPPING_CONFIG["rerank"]
                    ),
                    rerank_timeout_s=float(kb_section.get("rerank_timeout_s") or 2),
                    preview_rerank_cap=preview_count,
                    noise_floor=hitl.get("noise_floor"),
                    guards=load_content_guard_config(spec),
                )

        results = list(await asyncio.gather(*(_one(i, s) for i, s in enumerate(subtopics))))
    except Exception as exc:  # noqa: BLE001 —— 门内代码异常回落现状（R38 不判无）
        AgentLog(
            message=f"deep_kb_gate 门内异常回落现状：{type(exc).__name__}",
            graph="deep",
            node="kb_gate",
        ).warning()
        return {
            "kb_gate_action": "hybrid",
            "errors": [_gate_error("hitl_gate_error", {"stage": "kb_gate"})],
        }
    gate_ms = int((perf_counter() - started) * 1000)

    if any(result.status == "channel_failed" for result in results):
        AgentLog(
            message="deep_kb_gate 子课题通道全败回落现状（不判无）",
            graph="deep",
            node="kb_gate",
        ).warning()
        return {
            "kb_gate_action": "hybrid",
            "errors": [_gate_error("hitl_gate_error", {"stage": "kb_gate"})],
        }

    subqueries: list[dict[str, Any]] = []
    for index, result in enumerate(results):
        entry: dict[str, Any] = {"query_index": index, "top_k_count": result.top_k_count}
        if result.top1_rerank is not None:
            entry["top1_score"] = result.top1_rerank
        subqueries.append(entry)
    all_empty = all(result.top_k_count == 0 for result in results)
    verdict = "no_hit" if all_empty else "has_hit"

    errors: list[dict[str, Any]] = []
    if all_empty:
        action = "hybrid" if shadow else "web_only"
        if not shadow:
            errors.append(_gate_error("hitl_no_hit", {"subqueries": subqueries}))
        payload = None  # no_hit 轻量无载荷（data-model §1.20 gate_payload null）
    else:
        action = "hybrid" if shadow else "await"
        payload = {
            "schema_version": GATE_PAYLOAD_SCHEMA_VERSION,
            "rewritten_queries": subtopics,
            "query_type": query_type,
            "time_range": state.get("time_range"),
            "query_lang": state.get("query_lang"),
            "kb_candidates": _dedupe_candidates(results),
            # R30 兼容扩展：planner 产物持久化（阶段 2 跳过 planner——ARCH §4.2
            # 分期注「planner 产物（subtopics/outline，体量小）持久化进 gate_payload」；
            # schema_version=1 不变——data-model §1.20 :622 planner? 预留键承载）
            "planner": {"subtopics": subtopics, "outline": outline},
        }

    # 判定面留痕帧（deep 侧 plan 帧——api.md §7.4:1723 挂账注批次 4 契约增量随批钉正）
    emit_stage(
        "plan",
        "done",
        extra={
            "hitl": {
                "gate": "shadow" if shadow else "triggered",
                "verdict": verdict,
                "subqueries": subqueries,
                "gate_ms": gate_ms,
            }
        },
    )

    update: dict[str, Any] = {
        "kb_gate_action": action,
        "kb_gate_verdict": verdict,
        "kb_gate_subqueries": subqueries,
        "kb_gate_ms": gate_ms,
    }
    if not all_empty:
        update["kb_gate_previews"] = _materialize_previews(
            results, preview_count=preview_count, preview_chars=preview_chars
        )
        update["kb_gate_payload"] = payload
    else:
        update["kb_gate_previews"] = []
        update["kb_gate_payload"] = None
    if errors:
        update["errors"] = errors
    return update
