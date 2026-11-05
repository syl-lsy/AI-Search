"""Graph 节点包（占位锚点）。

规划节点（ARCH §3.1）：rewrite / search / fuse / synthesis / planner / research /
reflect / supplement / code_exec / writer。开发纪律：节点一个一个闭环（写测试→实现→
接入图→Studio/Langfuse 观察→验收），禁止批量写节点；节点保持纯函数，持久化由
TurnRunner 消费事件流完成。
"""
