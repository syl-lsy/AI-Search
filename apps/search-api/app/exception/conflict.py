"""并发冲突类领域异常（409 族）：同线程并发策略、轮次取消与线程删除的确定性拒绝。

REQ-042 同线程并发提问四策略（api.md §0 错误码表 / ARCHITECTURE ADR-010）：
- ConflictException：reject 策略拒绝新轮 / 排队位已满（409001「线程已有进行中轮次」）；
- TurnTerminalException：对 completed/degraded/failed/canceled 轮调用 cancel
  （409002「轮次已处于终态，不可取消」）；
- ThreadBusyException：删除线程时该线程仍有进行中轮（409005「线程有进行中轮次，
  不可删除」，api.md §2.11 v1.18 / D-11）——独立码不复用 409001（避免「删除被拒」
  混入并发提问埋点面，同 404004 先例）；判定状态集与 409001 同源：
  pending/running/queued。
- TurnNotRegenerableException：POST /turns/{id}/regenerate 前置不满足（409006
  「轮次不支持重新生成」，api.md §2.12 v1.19 / REQ-063）——单码覆盖非
  {degraded,failed} 终态 / 零来源 / deep 轮 / 进行中重复点击（条件推进落败）。
默认消息取 api.md §0「含义」列原文；业务代码可按场景传更具体的 message
（如带排队深度上限说明），响应体 message 一律用异常自身的 message。
"""
from app.exception.base import BusinessException


class ConflictException(BusinessException):
    """线程已有进行中轮次（409001）：reject 拒绝或 enqueue 排队位已满（per-thread 深度 1）。"""


class TurnTerminalException(BusinessException):
    """轮次已处于终态，不可取消（409002）：终态轮调用 §2.7 cancel 时的确定性拒绝。"""


class ThreadBusyException(BusinessException):
    """线程仍有进行中轮次，不可删除（409005）：DELETE /threads/{id} 的确定性拒绝。"""


class TurnNotRegenerableException(BusinessException):
    """轮次不支持重新生成（409006）：§2.12 regenerate 前置不满足 / 并发双击落败。"""
