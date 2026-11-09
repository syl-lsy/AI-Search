"""请求参数错误（400001）：API 边界校验失败的业务侧确定性拒绝。

RequestValidationError（FastAPI 校验层）之外的**运行期**参数校验失败走本异常——
如 TurnRunner 的模型解析（请求 model 键不在 Nacos chat.options 键域）。
默认消息取 api.md §0 错误码表「请求参数错误」原文。
"""

from app.exception.base import BusinessException


class BadRequestException(BusinessException):
    """请求参数错误（400001）：运行期参数校验失败的确定性拒绝。"""
