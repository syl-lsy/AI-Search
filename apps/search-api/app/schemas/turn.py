"""轮次域 DTO（api.md §2.4/§2.5）。

TurnCreate 与 ThreadCreate 请求体同构（§2.4「请求体同 2.1」）——继承复用校验边界；
TurnDetail 为轮次终态详情（回看/终态渲染唯一数据源，§2.5 全字段）；
SourceBrief/StageBrief 对齐 §2.5 sources[]/stages[] 形状。

分期口径字段（用户裁决 2026-11-08，逐字段注释标注真实值启用批次）：
- incognito：请求侧（ThreadCreate）接收并校验、不落库；TurnDetail 恒 false——M5 起；
- followups：恒 null——0009 迁移 + B7 工单起；
- memory_hits：**不在 TurnDetail 返回**（api.md §2.5「M5 及之前无此字段」——评审 P2
  勘误：此前预置恒空命中字段属提前出面；M6（记忆 P1）随迁移与 §7.2 memory 事件一并增）；
- has_artifacts 属 ThreadDetail（thread.py），同口径 0002-M2 起真实值。
"""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import SearchMode, TurnStatus
from app.schemas.thread import ThreadCreate

StageKey = Literal["rewrite", "search", "read", "reflect", "plan", "write", "code_exec"]
StageStatus = Literal["running", "done", "failed"]
SourceType = Literal["web", "kb"]
CancelReason = Literal["user", "superseded", "rolled_back"]
DegradedReason = Literal["timeout", "budget", "source_failure"]


class TurnCreate(ThreadCreate):
    """POST /threads/{thread_id}/turns 请求体（§2.4）：请求体同 §2.1，继承复用校验边界。

    追问轮同样支持 incognito（无痕态下继续追问即继续无痕，前端逐轮携带）与
    on_conflict（并发提问天然发生在此端点）。
    """


class SourceBrief(BaseModel):
    """来源条目（§2.5 sources[]）：ref_no 与答案 [n] 一一对应；source_id 用于 §5.1 快照查看。"""

    source_id: str
    ref_no: int
    source_type: SourceType
    title: str
    url: str | None  # kb 来源无 url
    snippet: str
    score: float
    snapshot_available: bool
    kb_doc_id: str | None


class ImageBrief(BaseModel):
    """答案图片引用（§2.5 images[]）。"""

    url: str
    source_url: str | None = None
    title: str | None = None
    width: int | None = None
    height: int | None = None


class StageBrief(BaseModel):
    """阶段条目（§2.5 stages[]）：stage_key/status 枚举见 §2.5 字段冻结说明。"""

    stage_key: StageKey
    status: StageStatus
    label: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_ms: int | None = None
    extra: dict[str, Any] | None = None


class UsageBrief(BaseModel):
    """成本用量（§2.5 usage）：Langfuse 累计回填；进行中轮可缺省（返回已持久化部分）。"""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None


class TurnError(BaseModel):
    """终态错误载荷（data-model.md §1.3 turns.error：{code, message, stage_key}）。"""

    code: str
    message: str
    stage_key: str | None = None


class MemoryHits(BaseModel):
    """记忆注入命中（api.md §2.5：结构与 §7.2 memory 事件一致，memory-api.md §5.2）。

    M6（记忆 P1）随 turns.memory_hits 增列（0005 后续迁移）一并启用并挂到
    TurnDetail——M5 及之前 TurnDetail 无此字段（api.md §2.5 冻结口径），本模型
    暂无消费方（保留类型定义供 M6 接线，评审 P2 勘误：此前提前挂 TurnDetail）。
    """

    profile_used: bool = False
    items: list[dict[str, Any]] = Field(default_factory=list)


class TurnCancelResult(BaseModel):
    """POST /turns/{turn_id}/cancel 响应 data（§2.7）：status 恒 "canceling"。

    canceling 仅为响应信号、非 turns.status 枚举值（api.md §2.7 v1.4 注——queued 轮
    取消在毫秒级出终态，canceling 仅存在于响应体）；终态经 SSE done(finish=canceled)
    与 §2.5 对账确认。
    """

    turn_id: str
    status: Literal["canceling"]


class TurnDetail(BaseModel):
    """轮次终态详情（§2.5）：进行中（running/queued）调用同样合法，返回已持久化部分。

    degraded/export_available 为服务端推导字段，推导口径落在 service 层映射处：
    degraded=status==degraded；export_available=mode=deep 且 answer_md 非空 且
    status∈{completed,degraded}（api.md v1.10 §2.5，判据与 §2.3 has_report 同源化）。
    """

    turn_id: str
    thread_id: str
    mode: SearchMode
    status: TurnStatus
    query: str
    query_lang: str | None = None
    rewritten_queries: list[str] = Field(default_factory=list)
    parent_turn_id: str | None = None  # REQ-017：深度报告后的快速追问指向该深度轮
    answer_md: str | None = None  # 快速=综合回答 Markdown（≤3000 字上限）；深度=完整报告
    sources: list[SourceBrief] = Field(default_factory=list)
    images: list[ImageBrief] = Field(default_factory=list)
    stages: list[StageBrief] = Field(default_factory=list)
    degraded: bool = False
    degraded_reason: DegradedReason | None = None  # M2 起写入（data-model.md §1.3）
    error: TurnError | None = None
    usage: UsageBrief | None = None
    duration_ms: int | None = None
    export_available: bool = False
    incognito: bool = Field(
        default=False,
        description="分期口径（用户裁决 2026-11-08）：恒 false——M5（0005 迁移）起真实值",
    )
    followups: list[str] | None = Field(
        default=None,
        description="分期口径（用户裁决 2026-11-08）：恒 null——0009 迁移 + B7 工单起真实值",
    )
    cancel_reason: CancelReason | None = None  # 仅 status=canceled 时非空（§2.5 v1.4）
    created_at: datetime
    finished_at: datetime | None = None
