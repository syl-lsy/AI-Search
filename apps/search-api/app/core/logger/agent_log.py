"""Agent 图域日志：LangGraph 图/节点/路由事件的结构化记录。

- 路由回落等图域 WARNING 不走裸 logging / print，统一经本记录类落
  （业务代码新增日志一律走 LogRecord 子类体系，不散落自建出口）；
- 图内高频信号（stage/token/sources）不落日志——它们是 SSE 事件载荷，
  观测走 Langfuse trace（ARCH §6.6），日志只记异常与降级路径。
"""

from dataclasses import dataclass

from app.core.logger.log_record import LogRecord


@dataclass
class AgentLog(LogRecord):
    """Agent 图域差异字段：graph（图名）/ mode（请求模式）/ node（节点名）。"""

    graph: str = ""
    mode: str = ""
    node: str = ""
