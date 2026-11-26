"""merge_dedupe 节点（B3 节点 2/5——ARCH §4.1「纯函数，不失败」；例外批次与 web_search
同批测试——DEV §5.5 闭式例外，DoD 证据独立取证）。

职责：URL 归并去重（保首见序）+ origin 合并为列表（保留 KB 标记）+ **候选图片投影
`images_raw`（A6，REQ-004）** + **RRF 融合收口（B19，plan §4.2——`rrf_fuse_kb` 纯
函数：跨 query×通道 RRF(k=60) 累计 + type_weights 题型权重分轴 + 同章节归并留最高分
+ kb_fuse_top_k=20 截取；ADR-017 决策②「跨 query×通道合并」要求全副本完成后的单点，
本节点=fast 子图唯一汇聚节点〔WS→MG←KS 图拓扑既有〕）**。短路语义：全部副本零候选
且 errors 含**真源失败** → 抛 SearchChainUnavailable → TurnRunner 短路 500201
（synthesis 前短路——A1 情形 3）。**KB 语义性条目豁免**（`_KB_SEMANTIC_REASONS`：
kb_corpus_empty/kb_es_unavailable/kb_vector_unavailable/kb_vector_disabled/
kb_only_fallback_web——**KB 通道失败允许 web 兜底（语义性条目豁免短路）**，路由/
通道留痕非源调用失败）：此类条目只随 errors[] 留痕（既有形态保留），零候选放行到
fuse_rerank 纠错回边（gate 判不足 → Send("rewrite") 补 web——与 kb_search.py「强制
hybrid 由路由降级与纠错回边承载」、kb_route.py「零候选兜底=纠错回边一次补 web」
同谓词）；避免 kb_only/kb_first 用户显式「只查我的文档」时拿到「全部搜索源失败」错。

归并结果写入 `fused`（去重后的候选集）——B3 节点 4/5 fuse_rerank 读入该键按 §4.1 公式
产出最终集并发射 sources 事件（发射点=最终集定序后，ref_no 首现冻结）。images_raw
填充点选本节点（扇出汇聚单点写——Send 并行副本对非 reducer 键的写入不确定，web_search
不写）；fuse_rerank 消费其选图发射 images 事件（≤3 张，§7.4 sources 与 write 之间）。
**web 侧 URL 归并逐字节不变**（负例不变式：零 KB 候选轮 merge_dedupe 输出与现状全等
——KB 段殿后于 web 段，rerank 重排归 fuse_rerank）。KB 候选（无 url）不再静默丢
（plan §4.2——URL 归并主路径旁路）。
纯函数：不失败语义（畸形输入归一空集，绝不抛——短路仅由链失败语义触发）。
"""

from collections.abc import Mapping
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.agent.guards import ensure_not_canceled
from app.agent.tools.registry import SearchChainUnavailable

__all__ = [
    "extract_images",
    "image_descriptor_from_candidate",
    "merge_candidates",
    "merge_dedupe",
    "rrf_fuse_kb",
]

#: KB 语义性 errors.reason（**KB 通道失败允许 web 兜底（语义性条目豁免短路）**——
#: 路由/通道留痕非源调用失败；零候选放行到纠错回边补 web——plan §4.4 兜底同谓词。
#: kb_only_fallback_web 为 fuse_rerank 的 kb_only 兜底留痕，随 Send("rewrite",
#: dict(state)) 带入纠错第二趟——漏豁免会误抛 500201）
_KB_SEMANTIC_REASONS: frozenset[str] = frozenset(
    {
        "kb_corpus_empty",
        "kb_es_unavailable",
        "kb_vector_unavailable",
        "kb_vector_disabled",
        "kb_only_fallback_web",
    }
)


def _has_real_source_failure(errors: Any) -> bool:
    """errors 是否含 KB 语义性条目之外的真源失败（短路判定谓词）。

    KB 语义性条目（`_KB_SEMANTIC_REASONS`）豁免；非 Mapping 条目保守按真失败计
    （不猜不吞——短路语义维持）。
    """
    for error in errors if isinstance(errors, list) else []:
        if isinstance(error, Mapping) and error.get("reason") in _KB_SEMANTIC_REASONS:
            continue
        return True
    return False


def merge_candidates(candidates: Any) -> list[dict[str, Any]]:
    """URL 归并去重：保首见项字段、origin 合并为列表（保序去重，KB 标记保留）。

    不失败语义：非 dict 项 / 缺 url 项静默丢弃（畸形输入归一空集）。
    图片字段（img_src/query_images 等透传键）随首见项字段整体保留。
    """
    merged: list[dict[str, Any]] = []
    by_url: dict[str, dict[str, Any]] = {}
    for item in candidates if isinstance(candidates, list) else []:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url:
            continue
        origin = item.get("origin")
        existing = by_url.get(url)
        if existing is not None:
            if origin and origin not in existing["origin"]:
                existing["origin"].append(origin)
            continue
        entry = {**item, "origin": [origin] if origin else []}
        by_url[url] = entry
        merged.append(entry)
    return merged


def image_descriptor_from_candidate(candidate: dict[str, Any]) -> dict[str, Any] | None:
    """候选 → api.md §7.2 images 条目描述符（url 恒在；source_url/title/width/height
    键可省——v1.12 钉文字：上游缺则省略该键，不置 null、不伪造）。

    img_src 非字符串（畸形上游）→ None（该候选不产图）。
    """
    img_src = candidate.get("img_src")
    if not isinstance(img_src, str) or not img_src.strip():
        return None
    item: dict[str, Any] = {"url": img_src}
    source_url = candidate.get("url")
    if isinstance(source_url, str) and source_url:
        item["source_url"] = source_url  # 图片来源页 = 候选页（主通道逐条附带，绑定真实）
    title = candidate.get("title")
    if isinstance(title, str) and title.strip():
        item["title"] = title
    for src_key, dst_key in (("img_width", "width"), ("img_height", "height")):
        value = candidate.get(src_key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            item[dst_key] = value
    return item


def extract_images(candidates: Any) -> list[dict[str, Any]]:
    """候选图片投影（A6 images_raw 填充——ARCH §4.1「搜索附带图片」）：

    ① 候选附带 img_src（SearXNG 主通道）保归并序在前（source_url=候选页，绑定真实）；
    ② query_images 池（Tavily 顶层 images——无逐条来源页绑定）殿后，条目只带
       url/title，**不伪造 source_url**。
    不失败语义：非 dict 项/池条目静默跳过；协议过滤与 ≤3 截断归 fuse_rerank 选图面。
    """
    out: list[dict[str, Any]] = []
    for item in candidates if isinstance(candidates, list) else []:
        if not isinstance(item, dict):
            continue
        descriptor = image_descriptor_from_candidate(item)
        if descriptor is not None:
            out.append(descriptor)
    for item in candidates if isinstance(candidates, list) else []:
        if not isinstance(item, dict):
            continue
        for entry in item.get("query_images") or []:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url")
            if not isinstance(url, str) or not url:
                continue
            pool_item: dict[str, Any] = {"url": url}
            title = entry.get("title")
            if isinstance(title, str) and title.strip():
                pool_item["title"] = title
            out.append(pool_item)
    return out


def rrf_fuse_kb(
    kb_candidates: Any,
    *,
    rrf_k: int = 60,
    kb_fuse_top_k: int = 20,
    weights: tuple[float, float] | list[float] = (0.5, 0.5),
) -> list[dict[str, Any]]:
    """KB 候选 RRF 融合（plan §4.2 纯函数；ADR-017 决策②——跨 query×通道合并）。

    ① 归并键 (kb_doc_id, chunk_id)：跨 query×通道同 chunk 累计
       `score = Σ_channels Σ_queries w_channel × 1/(rrf_k + rank_i)`
       （w_channel = type_weights[query_type] 的 [bm25_w, vec_w] 分轴——调用方按
       query_type 选行传入）；
    ② 同章节归并：同 kb_doc_id 且 heading_path 去叶端同前缀的兄弟块仅留最高分
       （调研 §13.6③「同章节归并去重」——G059 同标题兄弟块收敛面）；
    ③ 截取 kb_fuse_top_k 进融合段（降序保序）。

    不失败语义：非 dict 项/缺 chunk_id 项静默跳过（畸形输入归一空集——本模块纪律）。
    """
    aggregated: dict[tuple[str, str], dict[str, Any]] = {}
    for item in kb_candidates if isinstance(kb_candidates, list) else []:
        if not isinstance(item, Mapping):
            continue
        chunk_id = item.get("chunk_id")
        doc_id = item.get("kb_doc_id")
        if not chunk_id or not doc_id:
            continue
        channel = str(item.get("channel") or "bm25")
        rank = item.get("channel_rank")
        rank = rank if isinstance(rank, int) and rank >= 1 else 1
        weight = weights[0] if channel == "bm25" else weights[1]
        contribution = float(weight) / (rrf_k + rank)
        key = (str(doc_id), str(chunk_id))
        entry = aggregated.get(key)
        if entry is None:
            entry = {**dict(item), "score": 0.0}
            aggregated[key] = entry
        entry["score"] = float(entry.get("score") or 0.0) + contribution

    cards = sorted(
        aggregated.values(),
        key=lambda card: float(card.get("score") or 0.0),
        reverse=True,
    )

    # ② 同章节归并（同 kb_doc_id + heading_path 去叶端同前缀仅留最高分——cards 已
    # 降序，首见即最高分）
    kept: list[dict[str, Any]] = []
    seen_prefixes: set[tuple[str, tuple[str, ...]]] = set()
    for card in cards:
        path = card.get("heading_path")
        prefix = tuple(str(seg) for seg in path[:-1]) if isinstance(path, list) and path else ()
        prefix_key = (str(card.get("kb_doc_id") or ""), prefix)
        if prefix and prefix_key in seen_prefixes:
            continue
        seen_prefixes.add(prefix_key)
        kept.append(card)
    return kept[:max(1, int(kb_fuse_top_k))] if kb_fuse_top_k > 0 else []


def _is_kb_candidate(item: Any) -> bool:
    """KB 候选判定（origin == "kb" 或 origin 列表含 "kb"——fuse_rerank.has_kb_hit
    同判据单一形态）。"""
    if not isinstance(item, Mapping):
        return False
    origin = item.get("origin")
    if origin == "kb":
        return True
    return isinstance(origin, list) and "kb" in origin


def merge_dedupe(state: Any, config: RunnableConfig | None = None) -> dict[str, Any]:
    """归并节点：web URL 归并（逐字节现状）+ KB RRF 融合段殿后 + images_raw 投影；
    全源失败短路（500201 路径——KB 通道语义性条目豁免，见 `_has_real_source_failure`）。

    sources 事件不在本节点发射（发射点=fuse_rerank——最终集经 topN/rerank 定序后
    ref_no 首现冻结，§7.2）；images 事件同样不在本节点发射（选图沿 fused 序，
    归 fuse_rerank——§7.4 发射位在其后）。
    """
    ensure_not_canceled(config)  # 节点边界感知协作式取消（R14②：跨子图注入面）
    candidates = state.get("candidates") or []
    errors = state.get("errors") or []
    if not candidates and _has_real_source_failure(errors):
        # 全部副本零候选且确有真源失败——synthesis 前短路（事件由 TurnRunner 出，
        # 图不发 error）；KB 通道语义性条目（语料空/通道态）豁免，放行纠错回边
        raise SearchChainUnavailable("全部搜索源调用失败——本轮 search 无候选，synthesis 前短路")
    kb_candidates = [item for item in candidates if _is_kb_candidate(item)]
    if not kb_candidates:
        # 负例不变式（plan §4.2）：零 KB 候选 → 输出与现状（纯 URL 归并）全等
        merged = merge_candidates(candidates)
        return {"fused": merged, "images_raw": extract_images(merged)}
    spec = state.get("spec")
    spec = spec if isinstance(spec, Mapping) else {}
    kb_section = spec.get("kb") if isinstance(spec, Mapping) else None
    kb_section = kb_section if isinstance(kb_section, Mapping) else {}
    rrf_value = kb_section.get("rrf_k")
    rrf_k = rrf_value if isinstance(rrf_value, int) and rrf_value > 0 else 60
    fuse_value = kb_section.get("kb_fuse_top_k")
    kb_fuse_top_k = fuse_value if isinstance(fuse_value, int) and fuse_value > 0 else 20
    weights_map = kb_section.get("type_weights")
    weights_map = weights_map if isinstance(weights_map, Mapping) else {}
    query_type = str(state.get("query_type") or "other")
    pair = weights_map.get(query_type)
    pair = pair if isinstance(pair, (list, tuple)) and len(pair) == 2 else (0.5, 0.5)
    web_candidates = [item for item in candidates if not _is_kb_candidate(item)]
    merged = merge_candidates(web_candidates) + rrf_fuse_kb(
        kb_candidates, rrf_k=rrf_k, kb_fuse_top_k=kb_fuse_top_k, weights=pair
    )
    return {"fused": merged, "images_raw": extract_images(merge_candidates(web_candidates))}
