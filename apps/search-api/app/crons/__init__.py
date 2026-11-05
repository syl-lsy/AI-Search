"""后台周期任务收口（占位注册表；ARCH §3.1 v1.18 B 项 / ADR-001·010）。

调度口径：进程内 asyncio 周期任务，由 main.py lifespan 注册（骨架阶段不注册——
B1 落地时补：任务文件放本目录一任务一文件，注册进下方 CRON_TASKS）。
与既有机制边界：启动钩子（main.py，一次性 seed/恢复/清理）与 KB worker（常驻队列
消费者）不走本注册表。

规划任务：trace_cleanup.py（B1 落地，每日清理过期 Langfuse trace）；
memory_ttl_sweeper.py（M5，记忆 TTL 衰减清扫）。
"""

# 任务注册表占位：B1 实装时收敛为 (name, interval_seconds, coroutine_factory) 结构，
# main.py lifespan 遍历注册为进程内 asyncio 周期任务
CRON_TASKS: list[str] = []
