"""通用 DTO：分页请求/响应 + thread/turn 两域共享的枚举口径。

分页统一 limit（默认 50，上限 200）+ offset（默认 0），列表响应含 total（api.md §0）。
枚举口径为 api.md §0/§2.3/§2.5 与 data-model.md §1.3 的冻结值：
- SearchMode：搜索模式 fast|deep（REQ-007：前端点模式仅为本地状态）；
- OnConflict：同线程并发策略四枚举（REQ-042，对齐 LangGraph Agent Server
  multitask_strategy；官方枚举无 fallback——口语 fallback 即 rollback）；
- TurnStatus：轮次状态七枚举（data-model.md §1.3；pending 为「创建→启动」毫秒级
  瞬时态，queued 为 enqueue 策略稳定等待态）。
枚举只约束 API 边界（DTO 层），ORM 列按 data-model.md §4 口径保持 varchar 无 CHECK。
"""
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")

SearchMode = Literal["fast", "deep"]
OnConflict = Literal["reject", "enqueue", "interrupt", "rollback"]
TurnStatus = Literal[
    "pending", "running", "queued", "completed", "degraded", "failed", "canceled"
]


class PageRequest(BaseModel):
    """分页查询参数（列表端点 query 参数基座；api.md §0：limit 默认 50 上限 200）。"""

    limit: int = Field(default=50, ge=1, le=200, description="每页条数（默认 50，上限 200）")
    offset: int = Field(default=0, ge=0, description="偏移量（默认 0）")


class PageResponse(BaseModel, Generic[T]):
    """分页响应载荷（/api/ 信封 data 部分）：total + items（api.md §0「列表响应含 total」）。"""

    total: int = Field(description="过滤条件下的总条数")
    items: list[T] = Field(default_factory=list, description="当前页数据")
