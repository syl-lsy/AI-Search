"""fast 模式子图（B3 五节点全部接真 + B19 kb_search 接真——ARCH §4.1）。

节点序列 rewrite → [Send 扇出] web_search×N + kb_search×N → merge_dedupe →
fuse_rerank → [纠错条件边] synthesis｜rewrite(纠错回边)（节点名 = api.md §7.2
stage_key），信号序列对齐 api.md §7.4 快速主干：

    stage(rewrite,r→d) → stage(search,r) → stage(search,d,progress)×N
    → sources(归并终态集) → images(可选，≤3 张) → stage(write,running,label=综合中)
    → token…token（messages 流） → stage(write,done)

- rewrite（1/5）出条件边多目标（v1.44 B9③ direct / v1.51 recall / B19 kb_route 三档
  hybrid 双 Send｜kb_first/kb_only 单 kb Send——判定序与降级归 kb_route.resolve_）：
  `search_strategy ∈ {direct, recall}` → synthesis（免检索直达，绕过检索段；判定先于
  空 queries）/ 空 queries → merge_dedupe / 其余 → Send 扇出（hybrid 双通道 Send）；
  merge_dedupe（2/5 RRF 融合收口）/ web_search（3/5 Send 扇出）/ kb_search（B19 五步
  闭环——双通道独立 3s 包络 + filter_candidates 顺序锁 + 语料空短路）/
  fuse_rerank（4/5 KB 命中 rerank+基数上界 30 截取+topN+snippet≤500+sources 发射点+
  images 选图发射）；
- **B19 纠错回边**（plan §4.4）：fuse_rerank 出条件边 `route_after_fuse`——确定性
  gate 判不足（kb_first/kb_only ∧ correction_enabled ∧ 轮帽未达 ∧ 零命中∨top1<阈值∨
  来源数不足）→ `Send("rewrite", {**state, correction_round: round+1})` 纠错第二趟
  （强制 hybrid=web+kb 并行补查）；否则 synthesis（correction_enabled=false 灰度缺省
  =现状路径直达）；synthesis（5/5——chat 模型流式，token/thinking 经 messages 流）；
- token/thinking 不走 custom 流（messages 流承载——§6.1 messages 过滤：仅 fast synthesis
  节点增量映射，R14③）；
- done 不由图发射：done 归 TurnRunner 落库终态后自发布（images 由 fuse_rerank 发射——
  api.md §7.2 v1.12：轮内至多一次、≤3 张、无图不发）；
- 最小检索分支（answerable=true）= rewrite 产出 + synthesis 提示词切分（节点内实现）；
- direct 分支（search_strategy=direct，v1.44）= 条件边直达 synthesis（零检索段）。
"""

from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.agent.nodes.fast_rewrite import rewrite
from app.agent.nodes.fuse_rerank import fuse_rerank
from app.agent.nodes.kb_route import correction_round_of, kb_supply_verdict
from app.agent.nodes.kb_search import kb_search
from app.agent.nodes.merge_dedupe import merge_dedupe
from app.agent.nodes.synthesis import synthesis
from app.agent.nodes.web_search import search_fanout, web_search
from app.agent.state import FastState

#: rewrite 条件边目标表（目标侧——recall 与 direct 共用 synthesis；B19 +`"kb_search"`
#: 第五目标〔plan §2.2 B6 钉定：search_fanout_targets 加 kb_search〕——子图内扩展、
#: 主图拓扑零改动；单测锁结构 `search_fanout_targets`）
search_fanout_targets: list[str] = ["web_search", "merge_dedupe", "synthesis", "kb_search"]

#: fuse_rerank 条件边目标表（B19 纠错回边——synthesis 直达｜rewrite 纠错第二趟）
_FUSE_TARGETS: list[str] = ["synthesis", "rewrite"]


def route_after_fuse(state: Any, config: RunnableConfig | None = None) -> Send | str:
    """fuse_rerank 出边（B19 纠错回边——plan §4.4「轮帽 1 次，子图内条件边」）。

    确定性信号 gate 判不足（`kb_supply_verdict`——零 LLM 纯函数；判定谓词含
    correction_enabled 灰度开关与轮帽）→ `Send("rewrite", {**state, correction_round:
    round+1})` 纠错第二趟（rewrite 入口自持 running 帧 + 强制 hybrid + 反馈注入；
    轮帽持久化由 rewrite update 落 state）；判足/灰度关闭/轮帽命中 → `"synthesis"`
    （现状路径）。拓扑零新增节点——Send 回边为 ADR-012 子图内扩展。
    """
    if kb_supply_verdict(state):
        return "synthesis"
    payload = dict(state)
    payload["correction_round"] = correction_round_of(state) + 1
    return Send("rewrite", payload)


def build_fast_graph() -> Any:
    """编译 fast 子图（五节点拓扑 + B19 kb_search/纠错条件边；主图拓扑零改动）。"""
    builder = StateGraph(FastState)
    builder.add_node("rewrite", rewrite)
    builder.add_node("web_search", web_search)
    builder.add_node("kb_search", kb_search)
    builder.add_node("merge_dedupe", merge_dedupe)
    builder.add_node("fuse_rerank", fuse_rerank)
    builder.add_node("synthesis", synthesis)  # 节点名=节点语义（§6.1 messages 过滤判定面）；stage_key=write 在节点内发射
    builder.add_edge(START, "rewrite")
    # Send 扇出：rewrite 出边按 rewritten_queries 产 Send（hybrid=web+kb 双 Send、
    # kb_first/kb_only=单 kb Send；direct/recall 直达 synthesis——目标复用，子图内改）
    builder.add_conditional_edges("rewrite", search_fanout, search_fanout_targets)
    builder.add_edge("web_search", "merge_dedupe")
    builder.add_edge("kb_search", "merge_dedupe")  # B19：merge_dedupe=唯一汇聚节点（WS→MG←KS）
    builder.add_edge("merge_dedupe", "fuse_rerank")
    # B19 纠错回边：fuse_rerank 出条件边（synthesis｜rewrite 第二趟）——零新节点
    builder.add_conditional_edges("fuse_rerank", route_after_fuse, _FUSE_TARGETS)
    builder.add_edge("synthesis", END)
    return builder.compile()


graph = build_fast_graph()
