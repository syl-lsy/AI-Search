"""周期任务日志：crons/ 后台任务的结构化记录（ARCH §3.1 v1.18 B 项 crons 收口）。

后台任务无请求上下文（request_id 恒 "-"），字段只含任务名与计数类摘要——
trace_id 级明细留在任务返回值（TraceCleanupResult）与批次 message 里，日志不刷屏。
"""
from dataclasses import dataclass

from app.core.logger.log_record import LogRecord


@dataclass
class CronLog(LogRecord):
    """周期任务级差异字段：task（注册表 name）/ batch_index / total / ok / failed。

    计数字段为 0 时被 LogRecord._emit 的假值过滤剔除（结构化日志里无意义的
    字段不落盘）；batch_index 从 1 起计，恒存在。
    """

    task: str = ""
    batch_index: int = 0
    total: int = 0
    ok: int = 0
    failed: int = 0
