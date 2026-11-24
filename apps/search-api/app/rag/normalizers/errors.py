"""B17 主线一：归一化管线领域异常（三方 SDK 异常一律归一，不裸穿）。

形态先例：`app/rag/storage.py` IrStorageError——KB 图外组件模块内归一异常；
本包按 ask 口径升格为 BusinessException 子类（领域异常带 `original_exception` 字段 +
`raise from` 链入双保留），未注册 ERROR_MAP——若逃逸到 REST 边界按 MRO 兜底 500001
（handler/errors.py 钉定语义），常规消费方是 KB 入库 worker（归一为文档 failed 态 +
fail_reason，不经 REST 面）。

消息纪律：面向用户的 message 只含组件名/扩展名/失败类别形态词，**零文件内容回显**；
三方异常细节（常含路径/内部状态）进 detail/original_exception，只落日志侧。
"""
from app.exception.base import BusinessException


class NormalizationError(BusinessException):
    """归一化失败基类：message 面向用户（组件/类别形态）；三方原异常链入。"""

    def __init__(
        self,
        message: str,
        *,
        detail: str = "",
        original_exception: Exception | None = None,
    ) -> None:
        super().__init__(message, detail=detail, original_exception=original_exception)


class UnsupportedTypeError(NormalizationError):
    """不支持/不可路由的类型：白名单外、路由表外、spec 枚举外（显式拒绝不猜）。"""


class BadFileError(NormalizationError):
    """坏文件：魔数不符（扩展名与字节形态矛盾）等入口即可判的坏输入。"""
