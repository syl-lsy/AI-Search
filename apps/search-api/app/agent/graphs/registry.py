"""GRAPH_REGISTRY：注册名 → 编译图（B2 桩期；ARCH §4.0 / ADR-012）。

注册两张桩模式子图（键 = 图名 = 主图节点名）；`route_by_mode` 按 `state.mode`
分发——mode 缺失或指向未注册图时回落 `DEFAULT_GRAPH` 并告警（AgentLog WARNING，
main.py 落；路由单测四条见 test/unit/test_stub_graphs.py）。

- 桩期 mode==图名直查；B3 改 `route_by_mode` 读 Agent Spec `spec.routing` ∩
  registry（主图拓扑零改动，扩展 = 新子图注册 + spec.routing 热更两步）；
- 桩期模块 import 时即编译（langgraph.json 三入口要求模块级 `graph` 可导入）；
  B3 收敛为服务启动时一次编译三图注册（main.py lifespan 组装）。
"""

from langgraph.graph.state import CompiledStateGraph

from app.agent.graphs.deep import graph as deep_graph
from app.agent.graphs.fast import graph as fast_graph

#: 桩期默认子图（B3 改由内置默认表/Agent Spec default_mode 解析）
DEFAULT_GRAPH = "fast"

GRAPH_REGISTRY: dict[str, CompiledStateGraph] = {
    "fast": fast_graph,
    "deep": deep_graph,
}
