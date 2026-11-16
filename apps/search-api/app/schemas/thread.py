"""线程域 DTO（api.md §2.1~§2.3、§2.10/§2.11 v1.18）：请求/响应模型，与 db/models/（ORM）
严格分离。

- ThreadCreate（§2.1 首问建线程请求体）与 TurnCreate（§2.4 追问，turn.py）请求体同构；
- `model` 键不做静态 Literal——取值集合来自 Nacos chat.options 热更键（api.md §2.1
  「以 GET /models 返回为准」），静态枚举会与热更口径漂移；缺省解析与非法键的
  400001 归模型解析层（B2 下游任务）；
- 分期口径字段（用户裁决 2026-11-08）在字段注释标注真实值启用批次；
- 线程管理（v1.18，REQ-058~060）：ThreadListQuery 增 q/archived/unread，ThreadBrief
  增 pinned/is_unread/archived 三布尔投影；ThreadPatchRequest（§2.10 部分更新）与
  ThreadDeleteData（§2.11 删除响应）。
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, StrictBool, field_validator, model_validator

from app.schemas.common import OnConflict, PageRequest, SearchMode, TurnStatus

_QUERY_MAX_CHARS = 2000  # api.md §2.1：query 1~2000 字符（超长 → 400001）
_TITLE_MAX_CHARS = 50  # REQ-006：线程 title=首问前 50 字符（落位在 service 层）
_IMAGES_MAX_COUNT = 3  # api.md §2.1：images ≤3 张（M4 预留，REQ-025）
_TITLE_PATCH_MAX_CHARS = 120  # api.md §2.10：重命名 1~120 字符（对齐 threads.title varchar(120)）
_Q_MAX_CHARS = 200  # api.md §2.2：标题搜索 1~200 字符（超长 → 400001）

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
    """GET /threads 查询参数（§2.2 v1.18）：分页 + mode + 标题搜索 + 视图过滤。

    - `q`：标题 ILIKE 包含匹配（trim 后空串=不过滤；1~200 字符，超长 400001）；
    - `archived`：缺省 false=活跃集；true=仅归档。视图三态映射（前端 radio）：
      全部/未读=活跃集、已归档=archived=true；
    - `unread`：缺省 false；true=仅未读（read_at IS NULL）；与 archived 正交（服务端
      无组合特判，D-05）——三者为 AND 叠加。
    """

    mode: SummaryMode | None = Field(default=None, description="按线程模式聚合值过滤")
    q: str | None = Field(
        default=None, description="标题模糊搜索（ILIKE 包含、大小写不敏感；1~200 字符）"
    )
    archived: bool = Field(default=False, description="true=仅归档线程（缺省 false=活跃集）")
    unread: bool = Field(default=False, description="true=仅未读（read_at IS NULL）")

    @field_validator("q")
    @classmethod
    def _normalize_q(cls, value: str | None) -> str | None:
        """trim 后空串=不过滤；超长按 400001（RequestValidationError → 400001）。"""
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        if len(stripped) > _Q_MAX_CHARS:
            raise ValueError(f"q 长度不得超过 {_Q_MAX_CHARS} 字符")
        return stripped


class ThreadBrief(BaseModel):
    """线程列表项（§2.2 data.items[]）：侧栏最近/Threads 页骨架。

    v1.18 增三布尔投影（REQ-058~060）：`pinned`=threads.pinned；`is_unread`=
    read_at IS NULL；`archived`=archived_at IS NOT NULL——推导落在 service 层映射处。
    """

    thread_id: str
    title: str
    mode_summary: SummaryMode
    turn_count: int
    pinned: bool = Field(description="置顶（活跃视图排序首位；归档视图不消费）")
    is_unread: bool = Field(description="未读投影（read_at IS NULL）")
    archived: bool = Field(description="归档投影（archived_at IS NOT NULL）")
    created_at: datetime
    updated_at: datetime


class ThreadPatchRequest(BaseModel):
    """PATCH /threads/{thread_id} 部分更新请求体（§2.10 v1.18）。

    任意字段组合、**至少一项**（空对象 400001）；字段间无互斥。语义映射：
    - title：trim 后 1~120 字符（空串/纯空白/超长 400001）；
    - pinned：直写；
    - is_unread：true→read_at=NULL / false→read_at=now()；
    - archived：true→archived_at=now() / false→archived_at=NULL。
    `updated_at` 不随本端点变化（冻结语义，data-model §1.2 v1.14 / D-09）——落位在
    service 层（本类只管形状与边界校验）。
    """

    title: str | None = Field(
        default=None, description="重命名（trim 后 1~120 字符；空串/纯空白/超长 400001）"
    )
    pinned: StrictBool | None = Field(default=None, description="true=置顶 / false=取消置顶")
    is_unread: StrictBool | None = Field(
        default=None, description="true=标记为未读（read_at 置 NULL）/ false=置已读"
    )
    archived: StrictBool | None = Field(
        default=None, description="true=归档 / false=取消归档"
    )

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("title 不得为空串或纯空白")
        if len(stripped) > _TITLE_PATCH_MAX_CHARS:
            raise ValueError(f"title 长度不得超过 {_TITLE_PATCH_MAX_CHARS} 字符")
        return stripped

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> "ThreadPatchRequest":
        """至少一项（空对象 400001）；显式 null 不构成合法更新（布尔/字符串均非 null 类型）。"""
        provided = self.model_fields_set
        if not provided:
            raise ValueError("至少提供 title/pinned/is_unread/archived 之一")
        for name in ("title", "pinned", "is_unread", "archived"):
            if name in provided and getattr(self, name) is None:
                raise ValueError(f"{name} 不得为 null")
        return self


class ThreadDeleteData(BaseModel):
    """DELETE /threads/{thread_id} 响应 data（§2.11 v1.18）：`{"deleted": true}`。"""

    deleted: bool = Field(default=True, description="删除成功恒 true（物理删除、不可恢复）")


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
