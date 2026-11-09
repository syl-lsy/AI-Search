"""主图壳（B2 桩；ARCH §4.0 / ADR-012——B3 第 0 步替换为真壳）。

单入口编译图：按 GRAPH_REGISTRY 迭代挂子图节点（拓扑零改动口径），条件边
`route_by_mode` 按 `state.mode` 分发——亚毫秒、零 LLM·IO、**不发任何 SSE 信号**
（api.md §7 stage_key 枚举零变化）。mode 缺失或指向未注册图 → 回落
`DEFAULT_GRAPH`（"fast"）并记 AgentLog WARNING（ARCH §4.0 双层兜底同口径）。

B3 替换点：① `route_by_mode` 改读 Agent Spec `spec.routing` ∩ registry
（state.mode → 图名，本桩 mode==图名直查）；② 编译/注册收敛到服务启动时一次
完成；③ state 拆 FastState/DeepState 投影 + 子图边界处理（R14：投影映射 /
interrupt·cancel 传播 / 事件冒泡过滤）。

TurnRunner 统一 `main_graph.astream(subgraphs=True)` 消费（ARCH §6.1）；子图
custom 信号经 (namespace, mode, payload) 形状冒泡，namespace 首段=子图注册名。
"""

from collections.abc import Hashable

from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent.graphs.registry import DEFAULT_GRAPH, GRAPH_REGISTRY
from app.agent.state import MainState
from app.core.logger import AgentLog


def route_by_mode(state: MainState) -> str:
    """条件边路由：mode → GRAPH_REGISTRY 键；缺失/未注册回落 DEFAULT_GRAPH + WARNING。

    兜底不重写 turns.mode（保持用户请求原值，审计口径——ARCH §4.0）。
    """
    mode = state.get("mode")
    if not mode:
        AgentLog(
            message=f"路由回落默认子图 {DEFAULT_GRAPH}：mode 缺失",
            graph="main",
            mode="",
        ).warning()
        return DEFAULT_GRAPH
    if mode not in GRAPH_REGISTRY:
        AgentLog(
            message=f"路由回落默认子图 {DEFAULT_GRAPH}：mode={mode!r} 未注册",
            graph="main",
            mode=mode,
        ).warning()
        return DEFAULT_GRAPH
    return mode


def build_main_graph() -> CompiledStateGraph:
    """编译主图壳：按 registry 迭代挂子图节点 + START 条件边路由（无显式 END 边）。"""
    builder = StateGraph(MainState)
    for name, compiled in GRAPH_REGISTRY.items():
        builder.add_node(name, compiled)
    # 注解用 Hashable 对齐 add_conditional_edges 的 path_map 形参（dict 型变不变）
    targets: dict[Hashable, str] = {name: name for name in GRAPH_REGISTRY}
    builder.add_conditional_edges(START, route_by_mode, targets)
    return builder.compile()


graph = build_main_graph()
