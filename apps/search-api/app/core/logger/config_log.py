"""配置中心日志：config_center 换版/降级事件的结构化记录（docs/contract/nacos-config.md §6/§7）。

- 成功换版 → INFO（含 dataId/version/md5，§6「INFO 日志（含 dataId/version/md5，留痕）」）；
- 降级不静默：L2/L3 → WARNING，L4/单 dataId 校验失败 → ERROR（§7「日志逐级升 ERROR」）；
- 字段只含 dataId/version/md5/level——配置内容与任何密钥形态值绝不入日志。
"""
from dataclasses import dataclass

from app.core.logger.log_record import LogRecord


@dataclass
class ConfigLog(LogRecord):
    """配置中心级差异字段：data_id / version / md5 / source（生效层级 L1~L4）。"""

    data_id: str = ""
    version: int = 0
    md5: str = ""
    source: str = ""
