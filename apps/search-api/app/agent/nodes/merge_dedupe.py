"""merge_dedupe 节点（B3 节点 2/5——ARCH §4.1「纯函数，不失败」；例外批次与 web_search
同批测试——DEV §5.5 闭式例外，DoD 证据独立取证）。

职责：URL 归并去重（保首见序）+ origin 合并为列表（保留 KB 标记）。短路语义：全部副本
零候选且 errors 非空 → 抛 SearchChainUnavailable → TurnRunner 短路 500201（synthesis
前短路——A1 情形 3）。

归并结果写入 `fused`（去重后的候选集）——B3 节点 4/5 fuse_rerank 读入该键按 §4.1 公式
产出最终集并发射 sources 事件（发射点=最终集定序后，ref_no 首现冻结）。纯函数：不失败
语义（畸形输入归一空集，绝不抛——短路仅由链失败语义触发）。
"""

from typing import Any

from langchain_core.runnables import RunnableConfig

from app.agent.tools.registry import SearchChainUnavailable

__all__ = ["merge_candidates", "merge_dedupe"]


def merge_candidates(candidates: Any) -> list[dict[str, Any]]:
    """URL 归并去重：保首见项字段、origin 合并为列表（保序去重，KB 标记保留）。

    不失败语义：非 dict 项 / 缺 url 项静默丢弃（畸形输入归一空集）。
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


def merge_dedupe(state: Any, config: RunnableConfig | None = None) -> dict[str, Any]:
    """归并节点：去重 + origin 合并；全源失败短路（500201 路径）。

    sources 事件不在本节点发射（发射点=fuse_rerank——最终集经 topN/rerank 定序后
    ref_no 首现冻结，§7.2）。
    """
    candidates = state.get("candidates") or []
    errors = state.get("errors") or []
    if not candidates and errors:
        # 全部副本零候选且确有源失败——synthesis 前短路（事件由 TurnRunner 出，图不发 error）
        raise SearchChainUnavailable("全部搜索源调用失败——本轮 search 无候选，synthesis 前短路")
    return {"fused": merge_candidates(candidates)}
