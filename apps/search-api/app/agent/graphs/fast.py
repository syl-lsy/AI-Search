"""fast 模式子图（B3 五节点全部接真——ARCH §4.1）。

节点序列 rewrite → [Send 扇出] web_search×N → merge_dedupe → fuse_rerank → synthesis
（节点名 = api.md §7.2 stage_key），信号序列对齐 api.md §7.4 快速主干：

    stage(rewrite,r→d) → stage(search,r) → stage(search,d,progress)×N
    → sources(归并终态集) → images(可选，≤3 张) → stage(write,running,label=综合中)
    → token…token（messages 流） → stage(write,done)

- rewrite（1/5）出条件边三目标（v1.44 B9③）：`search_strategy=direct` → synthesis
  （免检索直答，绕过检索段；判定先于空 queries）/ 空 queries → merge_dedupe /
  其余 → Send 扇出 web_search×N；merge_dedupe（2/5）/web_search（3/5 Send 扇出）/
  fuse_rerank（4/5 KB 命中 rerank+topN+snippet≤500+sources 发射点+images 选图发射
  （A6——沿 fused 序 ≤3 张，紧邻 sources 之后）/synthesis（5/5——chat 模型流式，
  token/thinking 经 messages 流，节点只累积 answer_md）全部接真；
- token/thinking 不走 custom 流（messages 流承载——§6.1 messages 过滤：仅 fast synthesis
  节点增量映射，R14③）；
- done 不由图发射：done 归 TurnRunner 落库终态后自发布（images 由 fuse_rerank 发射——
  api.md §7.2 v1.12：轮内至多一次、≤3 张、无图不发）；
- kb_search（B5/M3）插在 web_search 与 merge_dedupe 之间（§4.1 拓扑位）；
- 最小检索分支（answerable=true）= rewrite 产出 + synthesis 提示词切分（节点内实现）；
- direct 分支（search_strategy=direct，v1.44）= 条件边直达 synthesis（零检索段）。
"""

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agent.nodes.fast_rewrite import rewrite
from app.agent.nodes.fuse_rerank import fuse_rerank
from app.agent.nodes.merge_dedupe import merge_dedupe
from app.agent.nodes.synthesis import synthesis
from app.agent.nodes.web_search import search_fanout, web_search
from app.agent.state import FastState


def build_fast_graph() -> Any:
    """编译 fast 子图（B3 五节点拓扑完备；kb_search 于 B5/M3 插入）。"""
    builder = StateGraph(FastState)
    builder.add_node("rewrite", rewrite)
    builder.add_node("web_search", web_search)
    builder.add_node("merge_dedupe", merge_dedupe)
    builder.add_node("fuse_rerank", fuse_rerank)
    builder.add_node("synthesis", synthesis)  # 节点名=节点语义（§6.1 messages 过滤判定面）；stage_key=write 在节点内发射
    builder.add_edge(START, "rewrite")
    # Send 扇出：rewrite 出边按 rewritten_queries 产 Send("web_search")×N（§4.1 拓扑）
    # v1.44 B9③：三目标条件边——direct 免检索直答轮直达 synthesis（子图内改，主图拓扑零改动）
    builder.add_conditional_edges(
        "rewrite", search_fanout, ["web_search", "merge_dedupe", "synthesis"]
    )
    builder.add_edge("web_search", "merge_dedupe")
    builder.add_edge("merge_dedupe", "fuse_rerank")
    builder.add_edge("fuse_rerank", "synthesis")
    builder.add_edge("synthesis", END)
    return builder.compile()


graph = build_fast_graph()
