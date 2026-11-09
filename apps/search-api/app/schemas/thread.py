"""线程域 DTO（api.md §2.1~§2.3）：请求/响应模型，与 db/models/（ORM）严格分离。

- ThreadCreate（§2.1 首问建线程请求体）与 TurnCreate（§2.4 追问，turn.py）请求体同构；
- `model` 键不做静态 Literal——取值集合来自 Nacos chat.options 热更键（api.md §2.1
  「以 GET /models 返回为准」），静态枚举会与热更口径漂移；缺省解析与非法键的
  400001 归模型解析层（B2 下游任务）；
- 分期口径字段（用户裁决 2026-11-08）在字段注释标注真实值启用批次。
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, StrictBool

from app.schemas.common import OnConflict, PageRequest, SearchMode, TurnStatus

_QUERY_MAX_CHARS = 2000  # api.md §2.1：query 1~2000 字符（超长 → 400001）
_TITLE_MAX_CHARS = 50  # REQ-006：线程 title=首问前 50 字符（落位在 service 层）
_IMAGES_MAX_COUNT = 3  # api.md §2.1：images ≤3 张（M4 预留，REQ-025）

SummaryMode = Literal["fast", "deep", "mixed"]  # 线程模式聚合值（§2.2：写入时计算）


class ThreadCreate(BaseModel):
    """POST /threads（§2.1）与 POST /threads/{thread_id}/turns（§2.4）共用请求体。"""

    query: str = Field(
        min_length=1, max_length=_QUERY_MAX_CHARS, description="原始问题（1~2000 字符）"
    )
    mode: SearchMode = Field(description="搜索模式（默认由前端显式传入，REQ-007）")
    model: str | None = Field(
        default=None,
        description="用户选择键 qwen|glm|deepseek（以 GET /models 返回为准）；缺省用 Nacos default",
    )
    images: list[str] | None = Field(
        default=None,
        max_length=_IMAGES_MAX_COUNT,
        description="M4 预留：data URL，≤3 张；存在时路由视觉模型（REQ-025）",
    )
    incognito: StrictBool = Field(
        default=False,
        description=(
            "无痕轮（默认 false，REQ-036）。分期口径（用户裁决 2026-11-08）：请求侧接收并"
            "校验、不落库——M5（0005 迁移 turns.incognito 列）起真实值"
        ),
    )
    on_conflict: OnConflict | None = Field(
        default=None,
        description=(
            "同线程并发策略（REQ-042，§0）；缺省取 Agent Spec turn.default_on_conflict，"
            "进程内捕获为执行依据不落库（api.md §0）"
        ),
    )


class ThreadCreateResult(BaseModel):
    """POST /threads（§2.1）与追问（§2.4）响应 data：thread_id/turn_id/status。

    status 创建时刻为瞬时 pending——running/queued 由 ThreadScheduler/TurnRunner 推进后
    经响应返回（B2 下游任务接 §2.1「running 或 queued」口径）。
    """

    thread_id: str
    turn_id: str
    status: TurnStatus


class ThreadListQuery(PageRequest):
    """GET /threads 查询参数（§2.2）：分页 + mode 过滤（缺省=全部；一期无归档概念）。"""

    mode: SummaryMode | None = Field(default=None, description="按线程模式聚合值过滤")


class ThreadBrief(BaseModel):
    """线程列表项（§2.2 data.items[]）：侧栏最近/Threads 页骨架。"""

    thread_id: str
    title: str
    mode_summary: SummaryMode
    turn_count: int
    created_at: datetime
    updated_at: datetime


class TurnSkeleton(BaseModel):
    """线程详情 turns[] 骨架（§2.3）：答案页左侧轮次栏与「回看」骨架；单轮完整内容按需调 §2.5。

    has_report 为服务端推导字段（api.md v1.10 §2.3 判据）：该轮是否产出深度报告 =
    mode=deep 且 answer_md 非空（fast 轮的综合回答不计入 report）——推导口径落在
    service 层映射处；degraded 推导自 status==degraded（熔断降级但有部分内容，
    data-model.md §1.3）。
    """

    turn_id: str
    mode: SearchMode
    status: TurnStatus
    query: str
    query_lang: str | None
    has_report: bool
    degraded: bool
    duration_ms: int | None
    created_at: datetime


class ThreadDetail(BaseModel):
    """线程详情（§2.3 data）：骨架聚合。"""

    thread_id: str
    title: str
    mode_summary: SummaryMode
    turn_count: int
    has_artifacts: bool = Field(
        default=False,
        description="分期口径（用户裁决 2026-11-08）：恒 false——0002-M2（artifacts 表+迁移）起真实值",
    )
    created_at: datetime
    updated_at: datetime
    turns: list[TurnSkeleton] = Field(default_factory=list)
