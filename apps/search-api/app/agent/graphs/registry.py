"""GRAPH_REGISTRY：注册名 → 编译图（占位锚点，B3 实装）。

规划（ARCH §4.0 / ADR-012）：启动时一次编译三图（main/fast/deep）并注册；
`route_by_mode` 按 `state.mode` 读 Agent Spec `routing` ∩ registry 分发——
mode 行缺失或指向未注册图时回落 default_mode 并告警（B3 路由单测四条覆盖）。
"""
GRAPH_REGISTRY: dict[str, object] = {}
