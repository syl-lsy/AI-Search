"""web_search 节点（B3 节点 3/5——ARCH §4.1 web_search 行 / §6.2 搜索链）。

Send 扇出并行副本：rewrite 出边经 `search_fanout` 按 rewritten_queries 产
`Send("web_search", payload)×N`（payload 携带 copy 的 query/query_lang/time_range +
search_query_index/total——额外键经 Send 输入投影进节点、不回写 state）。语言门控在
链解析面完成（registry.resolve 轮启动冻结，TurnRunner 经 configurable 注入）——节点
按冻结链逐源并行调用（asyncio.gather），time_range 仅透传 Tavily（§4.1）。

失败语义（§4.1/§6.2，不终止）：
- 单源失败 → 其余源候选保留 + degraded=True + errors 记录（熔断由 registry 计账）；
- 冻结链为空（主位+备位全断）→ SearchChainUnavailable → TurnRunner 500201 收轮；
- 全部副本零候选且 errors 非空 → merge_dedupe 短路（A1 情形 3，synthesis 前短路）。

stage 事件（api.md §7.2/§7.4）：index 0 副本发 running（detail=链摘要——主干单对），
每副本发 done（progress={current: idx+1, total} + detail 源摘要，降级标注在 detail）；
同 key 多次出现合法（前端按 key 就地更新）。节点纯函数：registry/chain 全部经
configurable 注入，零 DB/EventBus/全局单例直连。
"""

import asyncio
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import Send

from app.agent.graphs.signals import emit_stage
from app.agent.tools.registry import SearchChainUnavailable

__all__ = ["WEB_SEARCH_TOP_K", "search_fanout", "web_search"]

#: 单源单 query 候选上限（§6.5 快速模式入口控量 top_k 5–8 文档默认值；M4 评测后调）
WEB_SEARCH_TOP_K = 5


def search_fanout(state: Any) -> list[Send] | str:
    """rewrite 出边：按 rewritten_queries 产 Send×N；空 queries 回落默认边（不悬挂）。"""
    queries = state.get("rewritten_queries") or []
    if not queries:
        return "merge_dedupe"
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
            },
        )
        for index, query in enumerate(queries)
    ]


async def web_search(state: Any, config: RunnableConfig | None = None) -> dict[str, Any]:
    """单 query 搜索副本：冻结链逐源并行 → candidates（add reducer 合并）+ 降级留痕。"""
    configurable = (config or {}).get("configurable") or {}
    chain = configurable.get("search_chain")
    registry = configurable.get("tools_registry")
    if chain is None or registry is None:
        raise SearchChainUnavailable("搜索链未注入（registry 未启动）——无法执行 web_search")
    if not chain.sources:
        raise SearchChainUnavailable(chain.detail or "搜索链不可用")

    query = str(state.get("query") or "")
    index = int(state.get("search_query_index") or 0)
    total = int(state.get("search_query_total") or 1)
    time_range = state.get("time_range")
    if index == 0:  # 主干单对：仅首副本发 running（detail=链摘要，含降级标注）
        emit_stage("search", "running", detail=chain.detail)

    def _call(source: str) -> Any:
        # time_range 仅透传 Tavily（§4.1；其余源参数面不接收时效参数）
        return registry.search(
            query, source, time_range=(time_range if source == "tavily" else None),
            top_k=WEB_SEARCH_TOP_K,
        )

    outcomes = await asyncio.gather(
        *(_call(source) for source in chain.sources), return_exceptions=True
    )
    candidates: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    ok_summary: list[str] = []
    for source, outcome in zip(chain.sources, outcomes):
        if isinstance(outcome, BaseException):
            errors.append({"stage": "search", "reason": "source_failure", "source": source})
        else:
            candidates.extend(outcome)
            ok_summary.append(f"{source}×{len(outcome)}")

    degraded = bool(errors)
    detail = " / ".join(ok_summary) or "全部源失败"
    if degraded:
        failed = ",".join(item["source"] for item in errors)
        detail += f"（降级：失败源 {failed}）"
    emit_stage(
        "search",
        "done",
        progress={"current": index + 1, "total": total},
        detail=detail,
    )
    update: dict[str, Any] = {"candidates": candidates, "degraded": degraded, "errors": errors}
    return update
