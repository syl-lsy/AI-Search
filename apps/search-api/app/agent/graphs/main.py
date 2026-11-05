"""主图壳（占位锚点，B3 实装——B3 第 0 步：主图壳先行 + 路由单测四条先闭环）。

规划（ARCH §4.0 / ADR-012 / R14）：外层主图按 `state.mode` 经 GRAPH_REGISTRY 分发到
fast / deep 模式子图（route_by_mode，亚毫秒/零 SSE 事件/无 LLM·IO）；TurnRunner 统一
`astream(subgraphs=True)` 消费。子图边界三风险（state 投影映射 / interrupt·cancel
传播 / 事件冒泡过滤）见 R14，实装时在壳内处理。
"""
