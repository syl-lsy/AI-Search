"""轮反馈端点专属异常：目标轮次不存在（api.md §0 错误码 404004）。

独立于 404001 的理由（api.md §0 v1.16）：语义同 404001 的 turn 维度，但独立码便于
反馈面埋点与客户端区分（反馈按钮点击后轮被删/失效的单独处置）。HTTP 状态同为 404，
注册处见 handler/errors.py 的 ERROR_MAP。
"""
from app.exception.base import BusinessException


class FeedbackTurnNotFoundException(BusinessException):
    """反馈目标轮次不存在或不属于当前用户（映射 404004 / HTTP 404）。"""
