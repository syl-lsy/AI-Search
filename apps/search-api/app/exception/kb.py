"""KB 域异常（api.md §0 错误码表 M3 三码：413001 / 415001 / 422001 + B20 两面）。

契约锚点（api.md :45-47 / §4.7）：
- 413001 | 413 | 文件超过大小上限 | 上传 > 20MB（KB_MAX_FILE_MB）；
- 415001 | 415 | 不支持的文件类型 | 扩展名不在 pdf/md/txt/docx；音视频/图片同此码；
- 422001 | 422 | URL 非法 | URL 收藏：非 http/https、解析失败；
- 409007 | 409 | 已有进行中的知识库导入任务 | pending/running 导入任务存在时重复
  触发 POST /api/v1/kb/import/jobs（沿 409004 记忆抽取同族语义）【v1.30 增】；
- 500001（兜底面）| FEISHU_APP_ID/SECRET 未配置——凭据只进 .env 禁入 Nacos
  （裁决 #8）；业务异常出口走 BusinessException 子类（防裸 Exception 的 ERROR
  traceback 噪音，铁律 8），ERROR_MAP 注册同码兜底。

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


class KbImportJobActiveException(BusinessException):
    """已有进行中的知识库导入任务（409007）：pending/running 存在时重复触发导入。"""


class FeishuNotConfiguredException(BusinessException):
    """飞书凭据未配置（500001 兜底语义面）：触发导入前置校验确定性拒绝。"""
