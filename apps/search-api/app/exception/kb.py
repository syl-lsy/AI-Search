"""KB 域异常（api.md §0 错误码表 M3 三码：413001 / 415001 / 422001）。

契约锚点（api.md :45-47）：
- 413001 | 413 | 文件超过大小上限 | 上传 > 20MB（KB_MAX_FILE_MB）；
- 415001 | 415 | 不支持的文件类型 | 扩展名不在 pdf/md/txt/docx；音视频/图片同此码；
- 422001 | 422 | URL 非法 | URL 收藏：非 http/https、解析失败。

message 纪律：面向用户、零文件内容回显；音视频扩展名的 415001 message 必含
「暂不支持」（api.md §4.1 :527 钉注）。
"""
from app.exception.base import BusinessException


class FileTooLargeException(BusinessException):
    """文件超过大小上限（413001：KB_MAX_FILE_MB）。"""


class FileUnsupportedTypeException(BusinessException):
    """不支持的文件类型（415001）：白名单外扩展名；音视频/图片同此码。"""


class KbUrlInvalidException(BusinessException):
    """URL 非法（422001）：URL 收藏的 scheme/解析/SSRF 拒收确定性拒绝。"""
