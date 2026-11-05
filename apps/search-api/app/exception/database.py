"""数据库操作异常：唯一约束、外键冲突、连接失败等基础设施错误统一转此异常。"""
from app.exception.base import BusinessException


class DatabaseException(BusinessException):
    """数据库操作异常：在 service 层捕获底层 DB 错误后转抛，原始异常挂 original_exception 供日志排查。"""
