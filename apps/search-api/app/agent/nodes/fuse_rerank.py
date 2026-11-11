"""fuse_rerank 节点（B3 节点 4/5——ARCH §4.1 fuse_rerank 行 / §4.3 TTFT / §6.2 S0 落档通道）。

核心公式（§4.1）：`fused = kb 命中 ? rerank(candidates)[:topN] : candidates[:topN]`。
- **无 KB 标记（B3 常态，kb_search 未接入）→ 零 rerank 调用**直通截断——rerank 仅 KB
  命中时执行（保护 30s 轮预算；rerank 目标 ≤2s 不变）；
- **KB 命中** → rerank（LiteLLM 网关 /rerank 透传 qwen3-rerank——S0 落档：cohere 兼容
  通道直连百炼 compatible-api/v1/reranks）→ relevance 降序取 topN；
- **rerank 失败/超时** → 按 origin 得分降序排序兜底（不终止）+ degraded/errors + 复用
  stage(search, done) detail 标注（fuse_rerank 无独立 stage_key——api.md §7.2 枚举约束，
  不新增枚举值；§6.2「降级必须出现在 stage detail 与 done.degraded」）。

参数面（轮内 spec 快照，OQ-11 同源）：topN = fast.fuse_top_n（缺省 10）；rerank 超时 =
kb.rerank_timeout_s（缺省 2s）；模型别名 = 轮内映射快照 model.mapping.rerank（缺省回落
内置默认）。snippet 截断 ≤500 chars（§4.3 TTFT 手段，M1 冻结现值）。

sources 事件发射点 = 本节点（最终集经 topN 截断/rerank 重排定序后 ref_no 首现冻结——
§7.2「ref_no 首次出现即冻结」；merge_dedupe 产出的去重集是本节点输入）。节点纯函数：
rerank 客户端可 mock，零 DB/EventBus。
"""

import asyncio
import json
import uuid
from collections.abc import Mapping
from typing import Any

import httpx
from langchain_core.runnables import RunnableConfig

from app.agent.graphs.signals import emit_sources, emit_stage
from app.agent.guards import ensure_not_canceled
from app.agent.state import Candidate
from app.config_center.defaults import MODEL_MAPPING_CONFIG
from app.core.config import litellm_settings

__all__ = [
    "SNIPPET_MAX_CHARS",
    "fuse_rerank",
    "has_kb_hit",
    "rerank_via_gateway",
    "truncate_snippets",
]

#: snippet 截断上限（§4.3：fused 每条 snippet ≤500 chars——首 token ≤2s 预算手段）
SNIPPET_MAX_CHARS = 500

#: rerank 请求兜底超时（spec kb.rerank_timeout_s 缺失时的回落值，§3 初值 2s）
_DEFAULT_RERANK_TIMEOUT_S = 2.0

#: fuse_top_n 缺省（spec fast.fuse_top_n 缺失时回落，§3 初值 10）
_DEFAULT_FUSE_TOP_N = 10


def has_kb_hit(candidates: Any) -> bool:
    """KB 标记判定：任一候选 origin（列表）含 "kb" 即命中。"""
    for item in candidates if isinstance(candidates, list) else []:
        if not isinstance(item, Mapping):
            continue
        origin = item.get("origin")
        if isinstance(origin, list) and "kb" in origin:
            return True
        if origin == "kb":
            return True
    return False


def truncate_snippets(candidates: list[Candidate]) -> list[Candidate]:
    """snippet 截断 ≤SNIPPET_MAX_CHARS（§4.3；拷贝重写不改入参）。"""
    return [
        {**item, "snippet": str(item.get("snippet") or "")[:SNIPPET_MAX_CHARS]}
        for item in candidates
    ]


def _sort_by_score(candidates: list[Candidate]) -> list[Candidate]:
    """按 origin 得分降序（rerank 失败兜底序；稳定排序保同分首见序）。"""
    return sorted(candidates, key=lambda item: float(item.get("score") or 0.0), reverse=True)


async def rerank_via_gateway(
    query: str,
    documents: list[str],
    *,
    model_alias: str,
    timeout_s: float,
) -> list[float | None]:
    """LiteLLM 网关 /rerank 透传（Cohere 兼容形态）→ 按 docs index 对位的 relevance 分。

    非 2xx / 响应形态异常 → RuntimeError（节点兜底路径触发面）；凭证经网关组
    settings 单例读取，值不进日志/断言。
    """
    async with asyncio.timeout(timeout_s):
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.post(
                str(litellm_settings.base_url).rstrip("/") + "/rerank",
                headers={
                    "Authorization": "Bearer "
                    + litellm_settings.api_key.get_secret_value()
                },
                json={"model": model_alias, "query": query, "documents": documents},
            )
    if response.status_code // 100 != 2:
        raise RuntimeError(f"rerank 网关返回 {response.status_code}")
    try:
        body = response.json()
        results = body["results"]
        scores: list[float | None] = [None] * len(documents)
        for entry in results:
            index = int(entry["index"])
            scores[index] = float(entry["relevance_score"])
        return scores
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"rerank 出参形态异常：{exc}") from exc


def _rerank_order(
    scores: list[float | None], candidates: list[Candidate]
) -> list[Candidate]:
    """按 relevance 降序重排（None 分视为最低；稳定排序保同分原序）。"""
    order = sorted(
        range(len(candidates)),
        key=lambda i: (scores[i] is not None, scores[i] if scores[i] is not None else 0.0),
        reverse=True,
    )
    return [candidates[i] for i in order]


def _source_item(candidate: Candidate, ref_no: int) -> dict[str, Any]:
    """候选 → §7.2 sources batch 条目（source_id 占位——TurnRunner 以行 id 回写）。"""
    score = candidate.get("score")
    return {
        "source_id": str(uuid.uuid4()),
        "ref_no": ref_no,
        "source_type": "web",
        "title": str(candidate.get("title") or candidate.get("url") or ""),
        "url": candidate.get("url"),
        "snippet": str(candidate.get("snippet") or ""),
        "score": float(score) if isinstance(score, (int, float)) else 0.0,
        "snapshot_available": False,
        "kb_doc_id": None,
    }


async def fuse_rerank(state: Any, config: RunnableConfig | None = None) -> dict[str, Any]:
    """融合重排节点：topN 截断 + KB 命中 rerank + 兜底 + sources 事件（最终集）。"""
    ensure_not_canceled(config)  # 节点边界感知协作式取消（R14②：跨子图注入面）
    fused_input = state.get("fused") or []
    query = str(state.get("query") or "")
    spec = state.get("spec")
    spec = spec if isinstance(spec, Mapping) else {}
    fast_section = spec.get("fast") if isinstance(spec, Mapping) else None
    top_n = (
        int(fast_section.get("fuse_top_n", _DEFAULT_FUSE_TOP_N))
        if isinstance(fast_section, Mapping) and fast_section.get("fuse_top_n")
        else _DEFAULT_FUSE_TOP_N
    )
    kb_section = spec.get("kb") if isinstance(spec, Mapping) else None
    rerank_timeout = (
        float(kb_section.get("rerank_timeout_s", _DEFAULT_RERANK_TIMEOUT_S))
        if isinstance(kb_section, Mapping)
        else _DEFAULT_RERANK_TIMEOUT_S
    )

    degraded = False
    errors: list[dict[str, Any]] = []
    extra: dict[str, Any] | None = None
    if has_kb_hit(fused_input):
        configurable = (config or {}).get("configurable") or {}
        mapping = configurable.get("model_mapping")
        mapping = mapping if isinstance(mapping, Mapping) else MODEL_MAPPING_CONFIG
        alias = str(mapping.get("rerank") or MODEL_MAPPING_CONFIG["rerank"])
        documents = [
            str(item.get("title") or item.get("url") or "") for item in fused_input
        ]
        try:
            scores = await rerank_via_gateway(
                query, documents, model_alias=alias, timeout_s=rerank_timeout
            )
            fused = _rerank_order(scores, fused_input)[:top_n]
        except Exception:
            # 兜底：按 origin 得分降序（不终止）；降级留痕 stage detail + state（§6.2）
            fused = _sort_by_score(fused_input)[:top_n]
            degraded = True
            errors = [{"stage": "search", "reason": "rerank_fallback"}]
            extra = {"fallback": "rerank_fallback"}
    else:
        fused = fused_input[:top_n]  # 常态直通（B3 无 kb_search——零 rerank 调用）

    fused = truncate_snippets(fused)
    emit_sources(
        batch=[_source_item(candidate, ref_no) for ref_no, candidate in enumerate(fused, start=1)],
        total=len(fused),
    )
    if extra is not None:
        emit_stage("search", "done", detail="rerank 失败降级：按源分排序兜底", extra=extra)
    update: dict[str, Any] = {"fused": fused, "degraded": degraded}
    if errors:
        update["errors"] = errors
    return update
