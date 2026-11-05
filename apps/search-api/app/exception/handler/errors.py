"""错误码表：异常类型 → (业务错误码, HTTP 状态码, 默认消息)。

- ERROR_MAP 是错误码的唯一事实源：新增领域异常必须在此注册，否则按 MRO 回退到
  最近已注册的父类（最终兜底 Exception → 500001）；
- PROTOCOL_ERROR_MAP 覆盖框架层协议错误（路径/方法/媒体类型），code 用 HTTP 状态码字符串。
"""
from fastapi.exceptions import RequestValidationError

from app.exception.database import DatabaseException
from app.exception.not_found import NotFoundException

ERROR_MAP: dict[type, tuple[str, int, str]] = {
    RequestValidationError: ("422001", 422, "数据验证错误"),
    DatabaseException: ("422001", 422, "数据验证错误"),
    NotFoundException: ("404001", 404, "资源不存在"),
    Exception: ("500001", 500, "服务器内部错误"),
}

PROTOCOL_ERROR_MAP: dict[int, str] = {
    404: "路径不存在",
    405: "请求方法不允许",
    415: "不支持的媒体类型",
}
