"""Core Agent 层（LangGraph 编排）：state / graphs / nodes / tools / prompts / guards。

分层铁律：api/ 永不直接调用 LangGraph——图只经 services/（TurnRunner）消费；
依赖方向 api/ → services/ → agent/ → config_center/。B2 桩主图已落地
（graphs/{main,registry,fast,deep}.py + signals.py 信号契约）；nodes/tools/prompts
仍为占位锚点，随 B3/B4 工单实装。
"""
