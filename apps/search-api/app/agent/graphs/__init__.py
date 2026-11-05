"""图编排层出口：导出编译图（ARCH §3.1 / ADR-012，v1.20）。

规划（B3 实装）：main = 主图壳（路由，亚毫秒/零 SSE 事件/无 LLM·IO）+ fast / deep
两张模式子图；启动时由 registry.py 一次编译三图注册 GRAPH_REGISTRY；扩展 =
新子图注册 + Agent Spec `spec.routing` 热更两步，主图拓扑零改动。

占位阶段不导出任何符号（B3 定稿主图入口后，langgraph.json 的 graphs 指向本包
main/fast/deep 暴露的编译图）。本包占位文件不 import langgraph——保证 AI 栈依赖
就位前骨架模块引用一致。
"""
