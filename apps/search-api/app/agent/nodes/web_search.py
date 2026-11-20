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

B15 内容侧护栏（写 candidates 处逐候选扫 title+snippet+content——内容进上下文的
最早公共点）：flag=保留+errors 留痕（injection_blocked）、drop=丢弃（来源卡/[n]
自然一致）；护栏动作**不置 degraded**（degraded_reason 枚举封闭，errors[] 非封闭
诊断通道）；spec 经 Send 输入投影进节点（不回写 state），缺失回落 defaults 种子
（部署即启用）。

stage 事件（api.md §7.2/§7.4）：index 0 副本发 running（detail=链摘要——主干单对），
每副本发 done（progress={current: idx+1, total} + detail 源摘要，降级标注在 detail；
B11 P1b：发生同源延迟重试时 done 的 `extra.source_retries` 携带 `{源名: 次数}`——
无重试零键，前端零渲染义务）；同 key 多次出现合法（前端按 key 就地更新）。节点纯
函数：registry/chain 全部经 configurable 注入，零 DB/EventBus/全局单例直连。

**多副本留痕口径（双评审 P2-2 裁决口径 B）**：stage(search,done) 本就**按副本发**
（progress current/total 语义），retry_sink 为副本内局部变量——不同副本各自留痕、
互不聚合（SSE 与 §2.5 时间线可见多个 search done 条目，消费侧按 stage_key 就地更新，
取最后一条；§2.5 为全时间线数组）。聚合到统一计数需改 stage 发射语义与 state 契约
（新增 reducer 键 / 主图投影），改动面大于收益，故钉正「各自留痕」描述而非聚合实现。
"""

import asyncio
from collections.abc import Mapping
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import Send

from app.agent.graphs.signals import emit_stage
from app.agent.guards import ensure_not_canceled
from app.agent.safety.content_guard import filter_candidates
from app.agent.safety.config import load_content_guard_config
from app.agent.tools.registry import SearchChainUnavailable

__all__ = ["WEB_SEARCH_TOP_K", "search_fanout", "web_search"]

#: 单源单 query 候选上限（§6.5 快速模式入口控量 top_k 5–8 文档默认值；M4 评测后调）
WEB_SEARCH_TOP_K = 5


def search_fanout(state: Any) -> list[Send] | str:
    """rewrite 出边四目标路由（v1.44 B9③ direct / v1.51 recall / api.md §7.4 两形态）。

    判定顺序（契约钉死）：① `search_strategy ∈ {direct, recall}` → `"synthesis"`
    （免检索直达——direct 零事实面轮 / recall 回顾整理轮；**先于空 queries 判定**，
    两轮 queries 内容不参与路由）；② 空 queries → `"merge_dedupe"`（不悬挂，既有逻辑）；
    ③ 其余 → `Send("web_search")×N`。缺省/非法 strategy（旧提示词形态、state 侧未声明值）
    一律走 ③——灰度安全。

    **目标复用**：recall 与 direct 共用 `"synthesis"` 目标（fast 子图条件边目标列表
    `["web_search", "merge_dedupe", "synthesis"]` 零改动——子图内扩展、主图拓扑零改动，
    ADR-012 子图内扩展第二例）。
    """
    if state.get("search_strategy") in ("direct", "recall"):
        return "synthesis"
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
                # B15 内容侧护栏：spec 随 Send 输入投影进节点（不回写 state）——
                # web_search 写 candidates 处逐候选注入扫描的配置源（fast 子图 state
                # 的 spec 键不进 Send 输入，节点侧只能自携）
                "spec": state.get("spec"),
            },
        )
        for index, query in enumerate(queries)
    ]


async def web_search(state: Any, config: RunnableConfig | None = None) -> dict[str, Any]:
    """单 query 搜索副本：冻结链逐源并行 → candidates（add reducer 合并）+ 降级留痕。"""
    ensure_not_canceled(config)  # 节点边界感知协作式取消（R14②：Send 扇出副本同感知）
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

    retry_sink: dict[str, int] = {}  # 源名→同源延迟重试次数（P1b 留痕，无重试零键）

    def _call(source: str) -> Any:
        # time_range 仅透传 Tavily（§4.1；其余源参数面不接收时效参数）
        # retry_sink：同源延迟重试次数留痕（B11 P1b / api.md §7.2 v1.19 source_retries）
        return registry.search(
            query, source, time_range=(time_range if source == "tavily" else None),
            top_k=WEB_SEARCH_TOP_K, retry_sink=retry_sink,
        )

    outcomes = await asyncio.gather(
        *(_call(source) for source in chain.sources), return_exceptions=True
    )
    candidates: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    ok_summary: list[str] = []
    # B15 内容侧护栏：逐候选注入扫描（title+snippet+content）——flag=保留+留痕、
    # drop=丢弃（来源卡/[n] 自然一致）；护栏动作不置 degraded（degraded_reason 枚举
    # 封闭 timeout|budget|source_failure，护栏留痕走 errors[] injection_blocked）；
    # spec 缺失/形态漂移回落 defaults 种子（部署即启用，load_content_guard_config）
    spec = state.get("spec")
    guards = load_content_guard_config(spec if isinstance(spec, Mapping) else None)
    for source, outcome in zip(chain.sources, outcomes):
        if isinstance(outcome, BaseException):
            errors.append({"stage": "search", "reason": "source_failure", "source": source})
        else:
            filtered, filter_errors = filter_candidates(outcome, guards, stage="search")
            candidates.extend(filtered)
            errors.extend(filter_errors)
            ok_summary.append(f"{source}×{len(filtered)}")

    degraded = bool(
        [e for e in errors if e.get("reason") == "source_failure"]
    )  # B15：degraded 只由 source_failure 派生（护栏留痕 injection_blocked 非降级语义）
    detail = " / ".join(ok_summary) or "全部源失败"
    if degraded:
        failed = ",".join(
            item["source"] for item in errors if item.get("reason") == "source_failure"
        )
        detail += f"（降级：失败源 {failed}）"
    emit_stage(
        "search",
        "done",
        progress={"current": index + 1, "total": total},
        detail=detail,
        # B11 P1b / api.md §7.2 v1.19：可选键 source_retries（无重试零键——同 images 缺键省略）
        extra={"source_retries": dict(retry_sink)} if retry_sink else None,
    )
    update: dict[str, Any] = {"candidates": candidates, "degraded": degraded, "errors": errors}
    return update
