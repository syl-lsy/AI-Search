"""快照与产物域 DTO（api.md §2.8 / §5.1 / §5.3——REQ-015/REQ-016，M2 批次 4）。

TurnExportResult 对齐 §2.8 data 八键（artifact_id/thread_id/artifact_type/filename/
size_bytes/content_type/created_at/download_path）；SnapshotDetail 对齐 §5.1 data 七键
（snapshot_id/url/content_type/size_bytes/fetched_at/download_url 预签名/excerpt 摘录）。
§5.2 artifacts 列表（REQ-024）归 M4——本文件不含列表 DTO。
"""
from datetime import datetime

from pydantic import BaseModel


class TurnExportResult(BaseModel):
    """POST /turns/{turn_id}/export 响应（§2.8）：导出报告登记面。"""

    artifact_id: str
    thread_id: str
    artifact_type: str  # report（code_output 随 M4 §5.2 启用）
    filename: str  # report_{YYYYMMDD_HHmm}.md（§2.8 契约格式，代码侧生成）
    size_bytes: int
    content_type: str  # text/markdown
    created_at: datetime
    download_path: str  # /api/v1/threads/{thread_id}/artifacts/{artifact_id}/download


class SnapshotDetail(BaseModel):
    """GET /sources/{source_id}/snapshot 响应（§5.1）：快照元数据 + 10 分钟预签名 +
    前 2000 字符摘录（弹窗预览；读取期口径——save_snapshot.py:15-18 钉注）。"""

    snapshot_id: str
    url: str
    content_type: str
    size_bytes: int
    fetched_at: datetime
    download_url: str  # MinIO 预签名 GET（10 分钟有效期）
    excerpt: str  # 对象正文开头 2000 字符
