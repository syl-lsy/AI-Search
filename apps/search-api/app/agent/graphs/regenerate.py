"""regenerate 微图（B11 P2 / api.md §2.12 v1.19 / _increment-resilience §2.3 决策 10）。

「仅重新生成」的执行形态：**进程内单例单节点图**——只挂 synthesis 节点，复用其
纯函数与 custom/messages 流信号面（stage(write) + token/thinking 增量可经既有事件
翻译器映射；节点名 = "synthesis" 使 `_is_fast_synthesis_messages` 的
`langgraph_node` 过滤天然成立）。

- **非 GRAPH_REGISTRY**：那张表是 mode 路由面（fast/deep 用户模式），regenerate 不是
  用户 mode——注册进去会污染 mode 语义、破坏「前端点模式仅为本地状态」的契约
  （决策 10 备选否决）。
- 不重新检索：输入 state（fused/query/search_strategy/spec/mapping）由 TurnRunner
  自既有 turns/sources/turn_events 行重建（§2.12），sources/[n] 映射稳定。
- 编译时机：import 一次 = 编译一次（三图编译同口径），服务进程内单例复用。

StateGraph 的 reducer/字段沿用 FastState（synthesis 只写 answer_md/degraded——
FastState 子集，投影零差异）。
"""

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent.nodes.synthesis import synthesis
from app.agent.state import FastState


def build_regenerate_graph() -> CompiledStateGraph:
    """编译单节点微图（START → synthesis → END）。"""
    builder = StateGraph(FastState)
    builder.add_node("synthesis", synthesis)
    builder.add_edge(START, "synthesis")
    builder.add_edge("synthesis", END)
    return builder.compile()


graph: Any = build_regenerate_graph()
