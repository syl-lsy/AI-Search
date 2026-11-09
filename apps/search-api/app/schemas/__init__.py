"""Pydantic DTO 层（请求/响应模型）——与 db/models/（ORM）严格分离，互不混用。

基座口径本目录为 app/schema/，按 AI Agent 模板口径改名 app/schemas/。
规划模型族（ARCH §3.1，B2 起按 contract/api.md 落地）：Thread* / Turn* / Source* /
Kb* / Artifact* / Sse*；Suggestion*（v1.17）。B2 已落地 Thread* / Turn*（api.md
§2.1~§2.5），在此统一重导出（api/ 层 `from app.schemas import ...` 单一出口；
枚举口径随 common 一并出）。
"""
from app.schemas.common import (
    OnConflict,
    PageRequest,
    PageResponse,
    SearchMode,
    TurnStatus,
)
from app.schemas.model import ModelListResponse, ModelOption
from app.schemas.thread import (
    ThreadBrief,
    ThreadCreate,
    ThreadCreateResult,
    ThreadDetail,
    ThreadListQuery,
    TurnSkeleton,
)
from app.schemas.turn import (
    ImageBrief,
    MemoryHits,
    SourceBrief,
    StageBrief,
    TurnCancelResult,
    TurnCreate,
    TurnDetail,
    TurnError,
    UsageBrief,
)

__all__ = [
    "ImageBrief",
    "MemoryHits",
    "ModelListResponse",
    "ModelOption",
    "OnConflict",
    "PageRequest",
    "PageResponse",
    "SearchMode",
    "SourceBrief",
    "StageBrief",
    "ThreadBrief",
    "ThreadCreate",
    "ThreadCreateResult",
    "ThreadDetail",
    "ThreadListQuery",
    "TurnCancelResult",
    "TurnCreate",
    "TurnDetail",
    "TurnError",
    "TurnSkeleton",
    "TurnStatus",
    "UsageBrief",
]
