"""并发冲突类领域异常（409 族）：同线程并发策略与轮次取消的确定性拒绝。

REQ-042 同线程并发提问四策略（api.md §0 错误码表 / ARCHITECTURE ADR-010）：
- ConflictException：reject 策略拒绝新轮 / 排队位已满（409001「线程已有进行中轮次」）；
- TurnTerminalException：对 completed/degraded/failed/canceled 轮调用 cancel
  （409002「轮次已处于终态，不可取消」）。
默认消息取 api.md §0「含义」列原文；业务代码可按场景传更具体的 message
（如带排队深度上限说明），响应体 message 一律用异常自身的 message。
"""
from app.exception.base import BusinessException


class ConflictException(BusinessException):
    """线程已有进行中轮次（409001）：reject 拒绝或 enqueue 排队位已满（per-thread 深度 1）。"""


class TurnTerminalException(BusinessException):
    """轮次已处于终态，不可取消（409002）：终态轮调用 §2.7 cancel 时的确定性拒绝。"""
