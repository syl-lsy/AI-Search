"""Core Agent 层（LangGraph 编排）：state / graphs / nodes / tools / prompts / guards。

分层铁律：api/ 永不直接调用 LangGraph——图只经 services/（TurnRunner）消费；
依赖方向 api/ → services/ → agent/ → config_center/。占位阶段不引入 langgraph
第三方 import（兜底口径：AI 栈依赖解析由 uv sync 阶段闭环后 B3 实装）。
"""
