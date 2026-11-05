"""Agent State 定义（占位锚点，B3 实装）。

规划（ARCH §3.1 / §4）：FastState / DeepState 两个 TypedDict——fast/deep 是两张模式
子图（ADR-012），各自 state 投影需处理子图边界三风险中的「state 投影映射」（R14）。
实装时在此定义两个 TypedDict 并保持节点纯函数口径（持久化由 TurnRunner 消费事件流完成）。
"""
