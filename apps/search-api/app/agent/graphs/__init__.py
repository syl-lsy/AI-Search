"""图编排层出口（ARCH §3.1 / ADR-012，v1.20；B2 桩已落地）。

main = 主图壳（路由，亚毫秒/零 SSE 事件/无 LLM·IO）+ fast / deep 两张桩模式子图；
registry.py 维护 GRAPH_REGISTRY，扩展 = 新子图注册 + Agent Spec `spec.routing`
热更两步，主图拓扑零改动。langgraph.json 三入口（main/fast/deep）指向各模块的
模块级 `graph` 编译产物；图内信号 schema 见 graphs/signals.py docstring。
"""
