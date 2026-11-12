"""merge_dedupe 节点（B3 节点 2/5——ARCH §4.1「纯函数，不失败」；例外批次与 web_search
同批测试——DEV §5.5 闭式例外，DoD 证据独立取证）。

职责：URL 归并去重（保首见序）+ origin 合并为列表（保留 KB 标记）+ **候选图片投影
`images_raw`（A6，REQ-004）**。短路语义：全部副本零候选且 errors 非空 → 抛
SearchChainUnavailable → TurnRunner 短路 500201（synthesis 前短路——A1 情形 3）。

归并结果写入 `fused`（去重后的候选集）——B3 节点 4/5 fuse_rerank 读入该键按 §4.1 公式
产出最终集并发射 sources 事件（发射点=最终集定序后，ref_no 首现冻结）。images_raw
填充点选本节点（扇出汇聚单点写——Send 并行副本对非 reducer 键的写入不确定，web_search
不写）；fuse_rerank 消费其选图发射 images 事件（≤3 张，§7.4 sources 与 write 之间）。
纯函数：不失败语义（畸形输入归一空集，绝不抛——短路仅由链失败语义触发）。
"""

from typing import Any

from langchain_core.runnables import RunnableConfig

from app.agent.guards import ensure_not_canceled
from app.agent.tools.registry import SearchChainUnavailable

__all__ = ["extract_images", "image_descriptor_from_candidate", "merge_candidates", "merge_dedupe"]


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


def merge_dedupe(state: Any, config: RunnableConfig | None = None) -> dict[str, Any]:
    """归并节点：去重 + origin 合并 + images_raw 投影；全源失败短路（500201 路径）。

    sources 事件不在本节点发射（发射点=fuse_rerank——最终集经 topN/rerank 定序后
    ref_no 首现冻结，§7.2）；images 事件同样不在本节点发射（选图沿 fused 序，
    归 fuse_rerank——§7.4 发射位在其后）。
    """
    ensure_not_canceled(config)  # 节点边界感知协作式取消（R14②：跨子图注入面）
    candidates = state.get("candidates") or []
    errors = state.get("errors") or []
    if not candidates and errors:
        # 全部副本零候选且确有源失败——synthesis 前短路（事件由 TurnRunner 出，图不发 error）
        raise SearchChainUnavailable("全部搜索源调用失败——本轮 search 无候选，synthesis 前短路")
    merged = merge_candidates(candidates)
    return {"fused": merged, "images_raw": extract_images(merged)}
