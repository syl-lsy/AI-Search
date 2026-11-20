"""不满足导出条件异常（api.md §0:43 409003「不满足导出条件」——REQ-016 §2.8）。

触发：非深度轮、或轮次未到 completed/degraded（completed|degraded 均可导出）；
门禁判据与 §2.5 export_available 同源（mode=deep ∧ answer_md 非空 ∧ status∈
{completed,degraded}——turn_service.py:444-446 投影先例）。
"""
from app.exception.base import BusinessException


class ExportNotAvailableException(BusinessException):
    """不满足导出条件（409003）：非深度轮、或轮次未到 completed/degraded。"""
