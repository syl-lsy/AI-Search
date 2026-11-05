"""资源不存在异常：查询类接口找不到目标资源时抛出。"""
from app.exception.base import BusinessException


class NotFoundException(BusinessException):
    """资源不存在：映射 HTTP 404 + 业务错误码 404001（注册处见 handler/errors.py 的 ERROR_MAP）。"""
