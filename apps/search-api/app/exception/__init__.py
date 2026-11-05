"""领域异常统一出口：业务代码从这里 import（`from app.exception import NotFoundException`）。

新增领域异常 = 本目录新增一个文件定义 BusinessException 子类 → 加进下面的 __all__ →
在 app/exception/handler/errors.py 的 ERROR_MAP 注册（业务错误码, HTTP 状态码, 默认消息）；
错误码文档（/docs 的「错误码说明」）会随之自动更新。
"""
from app.exception.base import BusinessException
from app.exception.database import DatabaseException
from app.exception.not_found import NotFoundException

__all__ = ["BusinessException", "DatabaseException", "NotFoundException"]
