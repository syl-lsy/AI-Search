"""轮反馈 DTO（api.md §2.9，REQ-057；v1.17 反馈取消语义）。

`FeedbackRequest`：请求体 `{"helpful": bool | null}`（必填——true/false=upsert 改判，
null=清除反馈即删除该轮 turn_feedback 行；缺失/非 boolean|null 由 pydantic 边界校验
出 400001，service 层信任已校验数据）；
`FeedbackResponse`：响应 data `{turn_id, helpful, updated_at}`——upsert 形态 updated_at
为最近改判时间（随 upsert 刷新，data-model.md §1.16）；清除形态 helpful=null 且
updated_at=null（行已删除，api.md §2.9 v1.17 清除形态示例，三键恒在值置 null）。
读回通道是 §2.5 `feedback_helpful`（无独立 GET 端点，api.md §2.9「读回」行）。
"""
from datetime import datetime

from pydantic import BaseModel, Field


class FeedbackRequest(BaseModel):
    """POST /api/v1/turns/{turn_id}/feedback 请求体（§2.9 v1.17：helpful boolean | null 必填）。

    `strict=True`：**不做宽松强制转换**——`"yes"` / `1` / `"true"` 一律 400001
    （api.md §2.9 错误行「helpful 缺失/非 boolean|null 一律拒，strict 保留」；
    pydantic 默认 lax 模式会把「yes」/1 收成 True，与契约不符）。
    `None` 合法 = 清除反馈（v1.17——再次点击已选中按钮发 null 即取消，删除该轮
    turn_feedback 行，Perplexity 同钮再点即取消实机行为）。
    """

    helpful: bool | None = Field(strict=True)


class FeedbackResponse(BaseModel):
    """POST feedback 响应 data（§2.9）：一轮一条、改判即覆盖；null 形态=清除（v1.17）。"""

    turn_id: str
    helpful: bool | None
    updated_at: datetime | None
