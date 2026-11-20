"""该来源无快照异常（api.md §0:40 404002「该来源无快照」——REQ-015 §5.1）。

触发：快速模式来源、或深度来源未抓原文（source.snapshot_id 为空——data-model
§1.5 M2 增列推导面）。资源不存在（404001 来源不存在/越权）由 NotFoundException
承担，两码分工见 api.md §0。
"""
from app.exception.base import BusinessException


class SnapshotNotFoundException(BusinessException):
    """该来源无快照（404002）：快速模式来源、或深度来源未抓原文。"""
