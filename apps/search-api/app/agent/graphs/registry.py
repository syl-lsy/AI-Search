"""GRAPH_REGISTRY：注册名 → 编译图（ARCH §4.0 / ADR-012）。

注册两张模式子图（键 = 图名 = 主图节点名）；`route_by_mode`（main.py）按轮内
`state.spec.routing` 快照 ∩ registry 分发（OQ-11 同源，热更仅对新轮生效），双层
兜底（ARCH §4.0 / nacos-config.md §3 routing 节）：

- 层① routing 整节缺失/读取异常/校验失败 → 内置默认表（`BUILTIN_ROUTING`）路由
  + AgentLog WARNING 一次（main.py 落）；
- 层② 节有效但 mode 行缺失或 modes[mode] 指向未注册图名 → 经
  `modes[default_mode]` 解析图名兜底 + WARNING 一次。

扩展原则（ADR-012）：新子图注册（本表加行）+ spec.routing 加映射行（热更）两步，
主图拓扑零改动；`langgraph.json` 三入口要求各图模块级 `graph` 可导入（import 即编译
一次，服务启动后进程内单例复用，不引入 lifespan 重复编译）。
"""

from langgraph.graph.state import CompiledStateGraph

from app.agent.graphs.deep import graph as deep_graph
from app.agent.graphs.fast import graph as fast_graph

#: 内置默认路由表（层①兜底，ARCH §4.0「代码内置默认表」）——default_mode 与 modes
#: 分离建模（default_mode 须为 modes 的键，nacos-config.md §3 v1.19 键域口径）。
#: 初值与契约 routing 节恒等；此时 spec.routing 不可读，default_mode 同样不可达。
BUILTIN_DEFAULT_MODE = "fast"
BUILTIN_ROUTING: dict[str, str] = {"fast": "fast", "deep": "deep"}

#: 最终兜底图名（路由表完全不可达时的最后防线；值必须是 GRAPH_REGISTRY 键）
DEFAULT_GRAPH = "fast"

GRAPH_REGISTRY: dict[str, CompiledStateGraph] = {
    "fast": fast_graph,
    "deep": deep_graph,
}
