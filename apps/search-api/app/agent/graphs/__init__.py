"""图编排层出口（ARCH §3.1 / ADR-012，v1.20；B2 桩已落地）。

main = 主图壳（路由，亚毫秒/零 SSE 事件/无 LLM·IO）+ fast / deep 两张桩模式子图；
registry.py 维护 GRAPH_REGISTRY，扩展 = 新子图注册 + Agent Spec `spec.routing`
热更两步，主图拓扑零改动。langgraph.json 三入口（main/fast/deep）指向各模块的
模块级 `graph` 编译产物；图内信号 schema 见 graphs/signals.py docstring。

本包 import 时注册 Studio Langfuse configure hook（app/core/studio_observability
.register_studio_langfuse_hook——注册无条件、STUDIO_LANGFUSE env 门控激活：langgraph
dev 进程经本包 import 挂 hook，app 进程同样 import 但休眠零行为；ARCH §6.6 v1.67
边界反转，仅自托管 Langfuse）。
"""

from app.core.studio_observability import register_studio_langfuse_hook

register_studio_langfuse_hook()

