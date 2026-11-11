"""轮次运行时（B2）：ThreadScheduler 四策略调度 + TurnRunner 事件翻译管线。

单文件承载（ARCH §3.1 turn_service 行）：同线程并发提问四策略（REQ-042/ADR-010）、
开图（桩主图单例）、消费图事件流 → SSE 事件 + 落库、取消/失败/超时收口。

契约锚点（实现语义逐条对齐，勿凭直觉改）：
- **决策矩阵**（api.md §0 / ADR-010）：reject=存在未终态轮（running/queued；pending
  瞬时态并入 running 判定）即 409001；enqueue=有 running 无 queued → status=queued
  入队（per-thread 深度上限 1=代码常量 QUEUE_DEPTH_LIMIT，排队位已满 409001）；
  interrupt/rollback=有 running 则打断（cancel_reason=superseded/rolled_back，新轮
  先置 queued 等旧轮 canceled 后启动，「立即」=不经排队），无 running 退化 enqueue
  语义（队满 409001）；缺省 policy 取 AssetSnapshot 的 turn.default_on_conflict
  （进程内捕获为执行依据、不落库，热更仅对新提交生效）；
- **TurnRunner 短事务例外**（ARCH §6.1 事务边界条）：自持 session factory，每次
  持久化开短事务（写入即 commit 即归还连接）；图运行是脱离请求生命周期的独立任务；
- **事件翻译**（ARCH §6.1 + agent/graphs/signals.py 约定）：
  main_graph.astream(stream_mode=["messages","updates","custom"], subgraphs=True)；
  custom 通道 payload type ∈ stage/sources/token/images 一对一翻译；token 承载演进：
  B2 桩无真模型 token 亦走 custom，B4 真模型后 messages 流增量同翻译器归一；
  stage/sources/images/done/error 以**发布盖章的同一 seq** 落 turn_events
  （payload=去 envelope 冗余字段后的业务字段）；token/thinking 只发布不落库
  （ADR-003 第 4 条）；done 由 TurnRunner 终态落库后自发布（图不发 done）；
- **终态不变式**：先落 turns（status/answer_md/timing/usage/finished_at/duration_ms/
  error/cancel_reason）再 publish done（「事件到达时 2.5 已可对账」，§7.2）；
  duration 口径=「提交→done」（finished_at − created_at，data-model.md §1.3）；
- **失败路径**：error 事件（500001 兜底 / 500301 硬超时熔断）+ turns.status=failed +
  EventBus.close_channel（api.md §7.2 error 条目「直接关闭」口径 + 修 failed 轮
  缓冲永不清收的泄漏）；打断/取消不是故障——绝不产生 error 事件；
- **协作式取消**：cancel 置进程内标志，图循环在每个事件边界感知（B2 桩图节点间
  即边界；B3 guards 节点边界感知实装后同语义）；queued 轮取消=立即出队+终态
  canceled（cancel_reason=user，不经节点边界）。

生命周期：main.py lifespan（B2 接线）调 start_thread_scheduler()；路由层经
get_thread_scheduler() 消费（未启动 fail-fast，与 event_bus/config_center 同型）。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, cast

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graphs.main import graph as main_graph
from app.agent.nodes.synthesis import SynthesisError
from app.agent.tools.registry import SearchChainUnavailable, ToolRegistry, get_tools_registry
from app.config_center import AssetSnapshot, get_asset_center
from app.core.logger import AgentLog
from app.core.observability import build_turn_callbacks
from app.db.models import Source, Thread, Turn, TurnEvent
from app.db.session import get_session_factory
from app.exception import BadRequestException, ConflictException, NotFoundException, TurnTerminalException
from app.schemas.common import SearchMode, TurnStatus
from app.schemas.turn import (
    CancelReason,
    DegradedReason,
    SourceBrief,
    StageBrief,
    StageKey,
    StageStatus,
    SourceType,
    TurnDetail,
    TurnError,
    UsageBrief,
)
from app.services.event_bus import EventBus, SseEvent, get_event_bus

__all__ = [
    "ACTIVE_STATUSES",
    "FINAL_STATUSES",
    "HISTORY_WINDOW_TURNS",
    "QUEUE_DEPTH_LIMIT",
    "SubmitDecision",
    "ThreadScheduler",
    "decide_on_submit",
    "detect_query_lang",
    "ensure_turn_owner",
    "get_thread_scheduler",
    "get_turn_detail",
    "history_statement",
    "resolve_chat_model",
    "seed_seq_from_db",
    "start_thread_scheduler",
]

# 轮次状态口径（data-model.md §1.3 status 枚举）
ACTIVE_STATUSES = ("pending", "running")  # 运行占用判定：pending 瞬时态并入 running（api.md §0）
QUEUED_STATUS = "queued"
FINAL_STATUSES = ("completed", "degraded", "failed", "canceled")

# per-thread 排队深度上限（PRD OQ-20，代码常量）：queued 存在即「排队位已满」→ 409001
QUEUE_DEPTH_LIMIT = 1

# Nacos dataId（nacos-config.md：五 dataId 键名；TurnRunner 轮快照捕获）
_SPEC_DATA_ID = "ai_search.agent.spec.json"
_MAPPING_DATA_ID = "ai_search.model.mapping.json"
_MCP_DATA_ID = "ai_search.agent.mcp.json"

# spec 缺省兜底（TurnSpec 节缺失时组装层回落 reject 的同款内置口径）
_DEFAULT_POLICY = "reject"
_DEFAULT_TURN_TIMEOUT_S = 60.0

# 历史装配窗口（ARCH §4.1/§6.5：最近 6 轮 {q, a_head≤200 chars}）。nacos spec `history`
# 节为弃用迁移口径（memory.short_term 于 M5 接管，nacos-config.md §3），B3 以实现常量
# 执行，M5 记忆重构时随 MB2 工单替换。
HISTORY_WINDOW_TURNS = 6
HISTORY_ANSWER_HEAD_CHARS = 200

# 历史纳入状态口径：completed/degraded 正常纳入；canceled 纳入（打断轮已产出内容
# 进历史装配，ARCH §6.1）；rolled_back 整体排除（内容回退语义，ARCH §6.1——由
# cancel_reason 过滤承担）；failed/无答案轮不产 a_head，由 answer_md IS NOT NULL 排除。
_HISTORY_STATUSES = ("completed", "degraded", "canceled")

# 中文判定阈值（PRD OQ-2：CJK 字符占比 ≥30% 判 zh——实现层常量，不进 Nacos）
CJK_RATIO_THRESHOLD = 0.30


class SubmitDecision(str, Enum):
    """同线程并发提交决策（api.md §0 四策略 + 空闲放行）。

    REJECT 值取 "reject_409001"：与策略枚举 reject（用户选择）区分——决策是矩阵
    产出（reject 策略遇未终态轮 / 任一策略队满），策略是请求入参。
    """

    PROCEED = "proceed"
    REJECT = "reject_409001"
    ENQUEUE = "enqueue"
    INTERRUPT = "interrupt"
    ROLLBACK = "rollback"


def decide_on_submit(
    policy: str, *, has_running: bool, has_queued: bool
) -> SubmitDecision:
    """四策略决策矩阵（纯函数，16 分支；单测穷举锁定）。

    入参：policy（请求侧 on_conflict 或快照缺省值）+ 该线程未终态轮的存在性
    （running 轮按 api.md §0 口径把 pending 并入 running 判定；queued 轮存在即
    排队位已满——QUEUE_DEPTH_LIMIT=1）。产出决策由 resolve 阶段执行（状态推进/
    打断登记/409001 抛出）。
    """
    if policy == "reject":
        return SubmitDecision.REJECT if (has_running or has_queued) else SubmitDecision.PROCEED
    if policy == "enqueue":
        if not has_running:
            # 无 running：空闲放行；仅剩 queued（队满）→ 409001（api.md「排队位已满」）
            return SubmitDecision.REJECT if has_queued else SubmitDecision.PROCEED
        return SubmitDecision.ENQUEUE if not has_queued else SubmitDecision.REJECT
    # interrupt / rollback：有 running → 打断当前轮；无 running 退化 enqueue 语义
    if has_running:
        return SubmitDecision.INTERRUPT if policy == "interrupt" else SubmitDecision.ROLLBACK
    return SubmitDecision.REJECT if has_queued else SubmitDecision.PROCEED


def resolve_chat_model(
    model_key: str | None, mapping: Mapping[str, Any]
) -> tuple[str, str]:
    """解析 (chat_model_key, model_name)：缺省用 chat.default；非法键 400001。

    - model 键域 = Nacos model.mapping.chat.options 键（api.md §2.1「以 GET /models
      返回为准」——静态 Literal 会与热更漂移，故运行期校验）；
    - model_name = options[key]（LiteLLM 别名，落 turns.model_name 可证 A10）；
    - 非法键 → BadRequestException（api.md §0 400001「请求参数错误」）。
    """
    chat = mapping.get("chat") or {}
    options: dict[str, str] = dict(chat.get("options") or {})
    key = model_key if model_key else chat.get("default")
    if not key or key not in options:
        raise BadRequestException(
            "请求参数错误",
            detail=f"model 键 {key!r} 不在 model.mapping.chat.options 键域",
        )
    return str(key), str(options[key])


def _is_cjk_char(ch: str) -> bool:
    """CJK 表意文字区段（统一表意 + 扩展 A/B——OQ-2「CJK 字符」的代码口径）。"""
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF  # CJK 统一表意文字
        or 0x3400 <= code <= 0x4DBF  # 扩展 A
        or 0x20000 <= code <= 0x2A6DF  # 扩展 B
    )


def detect_query_lang(query: str) -> str:
    """图启动前纯代码语言判定（ARCH §4.1 前置口径；PRD OQ-2）：CJK 字符占比 ≥30% → zh。

    分母为非空白字符（空白不稀释占比）；空串/零有效字符回落 en。改写不改判——
    rewrite 节点不再判语言，直接消费注入值。
    """
    chars = [c for c in query if not c.isspace()]
    if not chars:
        return "en"
    cjk_count = sum(1 for c in chars if _is_cjk_char(c))
    return "zh" if cjk_count / len(chars) >= CJK_RATIO_THRESHOLD else "en"


def _is_fast_synthesis_messages(namespace: Any, chunk_pair: Any) -> bool:
    """R14③ messages 过滤（§6.1）：仅 synthesis 节点增量放行（token/thinking 映射面）。

    实测运行形态（2026-11-10 探针）：subgraphs=True 下 messages 帧 namespace 为空、
    节点标识在 chunk metadata `langgraph_node`——按 metadata 判定。fast/deep 侧跨子图
    区分（namespace 路径面）实测归批次 5（R14③——deep writer 不产 messages，deepagents
    实装时收敛）。
    """
    meta = chunk_pair[1] if isinstance(chunk_pair, tuple) and len(chunk_pair) > 1 else None
    return isinstance(meta, Mapping) and meta.get("langgraph_node") == "synthesis"


def history_statement(thread_id: str, turn_id: str) -> Any:
    """历史装配查询（ARCH §6.5）：最近 N 轮 {q, answer_md}，created_at 倒序取窗。

    纳入/排除口径见 _HISTORY_STATUSES 注；全部条件参数绑定（无字符串拼接）。
    返回按 created_at 倒序的行集（调用方反转为时间正序装配）。
    """
    return (
        select(Turn.query, Turn.answer_md)
        .where(
            Turn.thread_id == uuid.UUID(thread_id),
            Turn.id != uuid.UUID(turn_id),
            Turn.status.in_(_HISTORY_STATUSES),
            Turn.answer_md.is_not(None),
            or_(Turn.cancel_reason.is_(None), Turn.cancel_reason != "rolled_back"),
        )
        .order_by(Turn.created_at.desc(), Turn.id.desc())
        .limit(HISTORY_WINDOW_TURNS)
    )


async def get_turn_detail(
    session: AsyncSession, turn_id: str, *, user_id: str
) -> TurnDetail:
    """轮次详情（api.md §2.5——回看/终态渲染唯一数据源）：归属校验 + ORM→DTO 映射。

    - 读请求级 session（get_db），只读不写，事务边界不变；进行中（running/queued）
      调用同样合法：返回已持久化部分（§2.5「进行中调用同样合法」）；
    - 归属校验：id+user 双条件（与 cancel_turn 同口径），不存在/越权/非法 UUID 一律
      404001「轮次不存在」（api.md §0 未找到与越权同码，不泄漏存在性）；
    - 推导字段（TurnDetail docstring 口径）：degraded=status==degraded；
      export_available=mode=deep 且 answer_md 非空 且 status∈{completed,degraded}
      （api.md v1.10 §2.5——判据与 §2.3 has_report 同源化）；
    - 分期口径（用户裁决 2026-11-08）：incognito 恒 false（M5 列）、followups 恒 null
      （0009+B7）、images 恒空（B2 桩不发 images 事件，M4 视觉轮起定回放口径）、
      snapshot_available 恒 false / kb_doc_id 恒 null（M2/M3 增列后接真实值）；
      memory_hits 不返回（api.md §2.5「M5 及之前无此字段」，M6 随迁移再增）。
    """
    try:
        turn_uuid = uuid.UUID(turn_id)
        user_uuid = uuid.UUID(user_id)
    except ValueError as exc:
        raise NotFoundException(
            "轮次不存在", detail=f"非法 turn/user id：{turn_id!r}"
        ) from exc
    stmt = select(Turn).where(Turn.id == turn_uuid, Turn.user_id == user_uuid)
    turn = (await session.scalars(stmt)).first()
    if turn is None:
        raise NotFoundException(
            "轮次不存在",
            detail=f"turn_id={turn_id} 不存在或不属于当前用户",
        )
    source_rows = (
        await session.scalars(
            select(Source).where(Source.turn_id == turn_uuid).order_by(Source.ref_no.asc())
        )
    ).all()
    status = cast(TurnStatus, turn.status)
    mode = cast(SearchMode, turn.mode)
    timing = turn.timing if isinstance(turn.timing, dict) else {}
    stages_raw = timing.get("stages") or []
    usage = turn.usage if isinstance(turn.usage, dict) else {}
    return TurnDetail(
        turn_id=str(turn.id),
        thread_id=str(turn.thread_id),
        mode=mode,
        status=status,
        query=turn.query,
        query_lang=turn.query_lang,
        rewritten_queries=[str(q) for q in (turn.rewritten_queries or [])],
        parent_turn_id=str(turn.parent_turn_id) if turn.parent_turn_id else None,
        answer_md=turn.answer_md,
        sources=[
            SourceBrief(
                source_id=str(row.id),
                ref_no=int(row.ref_no),
                source_type=cast(SourceType, row.source_type),
                title=row.title,
                url=row.url,
                # 契约 snippet 必填；来源行为空（历史/异常行）按空串呈现
                snippet=row.snippet or "",
                # 契约 score 必填；来源缺分（异常行）按 0 分呈现
                score=float(row.score) if row.score is not None else 0.0,
                snapshot_available=False,  # 分期口径：M2（snapshot_id 列）起真实值
                kb_doc_id=None,  # 分期口径：M3（kb_doc_id 列）起真实值
            )
            for row in source_rows
        ],
        images=[],  # B2 桩不发 images 事件（翻译面预留）；M4 视觉轮起定回放口径
        stages=[
            StageBrief(
                stage_key=cast(StageKey, entry.get("stage_key", "")),
                status=cast(StageStatus, entry.get("status", "")),
                label=str(entry.get("label") or ""),
                started_at=entry.get("started_at"),
                ended_at=entry.get("ended_at"),
                duration_ms=entry.get("duration_ms"),
                extra=entry.get("extra"),  # §7.2 extra 透传（§2.5 stages[].extra）
            )
            for entry in stages_raw
            if isinstance(entry, dict)
        ],
        degraded=status == "degraded",
        degraded_reason=cast(DegradedReason | None, turn.degraded_reason),
        error=TurnError(**turn.error) if isinstance(turn.error, dict) else None,
        usage=UsageBrief(**usage),
        duration_ms=turn.duration_ms,
        export_available=mode == "deep"
        and bool(turn.answer_md)
        and status in ("completed", "degraded"),
        cancel_reason=cast(CancelReason | None, turn.cancel_reason),
        created_at=turn.created_at,
        finished_at=turn.finished_at,
    )


async def ensure_turn_owner(
    session: AsyncSession, turn_id: str, *, user_id: str
) -> None:
    """建流前归属校验（api.md §7.1 515 行「turn_id 不存在 → 建流前 404001」）。

    与 get_turn_detail/cancel_turn 同口径：不存在/越权/非法 UUID 一律 404001
    「轮次不存在」（api.md §0 未找到与越权同码，不泄漏存在性）；SSE 端点在
    StreamingResponse 之前调用——错误经全局业务异常处理器出普通错误信封
    （application/json），而非流内错误帧。轻量存在性查询（select(Turn.id)，
    不加载整行与 sources）。
    """
    try:
        turn_uuid = uuid.UUID(turn_id)
        user_uuid = uuid.UUID(user_id)
    except ValueError as exc:
        raise NotFoundException(
            "轮次不存在", detail=f"非法 turn/user id：{turn_id!r}"
        ) from exc
    stmt = select(Turn.id).where(Turn.id == turn_uuid, Turn.user_id == user_uuid)
    if (await session.scalars(stmt)).first() is None:
        raise NotFoundException(
            "轮次不存在",
            detail=f"turn_id={turn_id} 不存在或不属于当前用户",
        )


async def seed_seq_from_db() -> None:
    """进程重启后按 turn_events 库内 max(seq) 逐线程种入 EventBus 计数器。

    不种入则新轮 seq 与持久化历史撞 uq(thread_id, seq)（event_bus.py docstring
    「生命周期」条）；lifespan 接线归接口任务（本函数零参可直接 await）。
    """
    stmt = select(TurnEvent.thread_id, func.max(TurnEvent.seq)).group_by(
        TurnEvent.thread_id
    )
    async with get_session_factory()() as session:
        rows = (await session.execute(stmt)).all()
    bus = get_event_bus()
    for thread_id, max_seq in rows:
        if max_seq is None:  # 无事件的线程：计数器保持零基线，跳过
            continue
        bus.seed_thread_seq(str(thread_id), int(max_seq))


# ---- 进程级持有者（main.py lifespan 启动时注册；路由层经此取）----

_thread_scheduler: ThreadScheduler | None = None


def start_thread_scheduler(*, bus: EventBus | None = None) -> ThreadScheduler:
    """启动 ThreadScheduler 并注册进程级持有者（main.py lifespan 调用；B2 接线）。

    bus 缺省取 get_event_bus()（EventBus 先启——lifespan 顺序：EventBus →
    ThreadScheduler）；重复启动幂等返回既有实例。
    """
    global _thread_scheduler
    if _thread_scheduler is None:
        _thread_scheduler = ThreadScheduler(
            bus=bus if bus is not None else get_event_bus()
        )
    return _thread_scheduler


def get_thread_scheduler() -> ThreadScheduler:
    """取进程级调度器（未启动时 fail-fast——组装层尚未 start 即消费属编程错误）。"""
    if _thread_scheduler is None:
        raise RuntimeError(
            "ThreadScheduler 尚未启动：请在 lifespan 启动钩子调用 start_thread_scheduler()"
        )
    return _thread_scheduler


@dataclass
class _RunContext:
    """单轮运行的内存聚合：stage 时间线配对、sources 计数（进程内，随轮消亡）。

    `spec_payload` / `mapping_payload`：运行开始时一次性捕获的资产快照 payload
    （OQ-11——轮内全部读取走该快照，热更仅对后续轮生效；ARCH §4.0/§6.1）。
    """

    turn_id: str
    thread_id: str
    user_id: Any = None
    spec_payload: Mapping[str, Any] = field(default_factory=dict)
    mapping_payload: Mapping[str, Any] = field(default_factory=dict)
    mcp_payload: Mapping[str, Any] = field(default_factory=dict)
    stages: list[dict[str, Any]] = field(default_factory=list)
    stage_started: dict[str, datetime] = field(default_factory=dict)
    stage_label: dict[str, str] = field(default_factory=dict)
    sources_count: int = 0


class ThreadScheduler:
    """同线程并发四策略调度 + TurnRunner 图执行管线（进程内单例，短事务自持）。

    依赖全部构造注入（bus/session_factory/graph/快照）——单测零网络假体直构；
    生产由 start_thread_scheduler() 缺省装配（EventBus/db session factory/桩主图
    单例/Nacos 快照消费）。线程串行发布 + 单事件循环——进程内状态（取消标志/
    唤醒事件）无跨任务竞争。
    """

    def __init__(
        self,
        *,
        bus: EventBus,
        session_factory: Any = None,
        graph: Any = None,
        spec_snapshot: AssetSnapshot | None = None,
        mapping_snapshot: AssetSnapshot | None = None,
        tools_registry: ToolRegistry | None = None,
    ) -> None:
        self._bus = bus
        self._session_factory = (
            session_factory if session_factory is not None else get_session_factory()
        )
        self._graph: Any = graph if graph is not None else main_graph
        self._spec_snapshot = spec_snapshot
        self._mapping_snapshot = mapping_snapshot
        self._tools_registry = tools_registry
        # 进程内取消标志：turn_id → cancel_reason（user|superseded|rolled_back）；
        # 图循环在每个事件边界 pop 感知（B2 桩图节点间即边界；B3 guards 同语义实装）
        self._cancel_flags: dict[str, str] = {}
        # 线程级变更信号：run_turn 终态/取消/出队时 set，等待者即时唤醒
        # （DB 是启动条件的事实源，event 仅延迟优化——clear 竞态无害，有 0.2s 兜底轮询）
        self._thread_events: dict[str, asyncio.Event] = {}
        # 线程级抢占标记：thread_id → 抢占轮 turn_id（interrupt/rollback 新轮）。
        # 裁决 A（2026-11-08，api.md §0 打断时序「立即=不经排队」的唯一自洽读法）：
        # 「有 running 且有 queued」时打断 running 后执行槽归新轮（插队、不经排队），
        # 既有 queued 轮顺延到新轮终态之后（保持 queued，自动启动触发点后移）。
        # 清除点三处（任何路径都不留无终态清除点的永久标记，评审 P2）：
        # ① _persist_terminal 抢占轮终态；② schedule_turn 读不到行/行已终态；
        # ③ _can_start 消费时自愈（被引用轮已消失或已终态 → 失配即清）。
        # 登记在 resolve 内（请求事务未提交），事务回滚时行消失但进程内标记已在
        # ——②③ 即该场景的兜底；进程重启后标记随进程消亡（BackgroundTask 不复跑、
        # 孤儿清理兜底）。
        self._preempt_turns: dict[str, str] = {}

    # ---- 抢占标记维护（裁决 A：清除点见 _preempt_turns 注释）----

    def _forget_preempt(self, thread_id: str, turn_id: str) -> None:
        """清除指向 turn_id 的线程级抢占标记（幂等；失配不动——已被新登记覆盖）。

        终态收尾（_persist_terminal）与消费时自愈（_can_start）共用：只要标记
        指向的轮不再是「本线程的未终态抢占轮」，标记即无继续生效的理由。
        """
        if self._preempt_turns.get(thread_id) == turn_id:
            self._preempt_turns.pop(thread_id, None)

    def _forget_preempt_turn(self, turn_id: str) -> None:
        """按轮 id 清除抢占标记（行已不存在时拿不到 thread_id——表极小，线性扫描）。"""
        for thread_id in [
            tid for tid, preemptor in self._preempt_turns.items() if preemptor == turn_id
        ]:
            self._preempt_turns.pop(thread_id, None)

    @staticmethod
    def _preemptor_alive(preemptor: str, rows: Sequence[Any]) -> bool:
        """抢占标记有效性：被引用轮仍在场且未终态（pending/running/queued）。

        被引用轮不存在（登记后请求事务回滚）/已终态 → 失配，标记应自愈清除；
        status 判据而非「出现在查询结果里」（终态行不构成启动阻塞）。
        """
        return any(
            str(other.id) == preemptor
            and other.status in (*ACTIVE_STATUSES, QUEUED_STATUS)
            for other in rows
        )

    # ---- 轮快照（OQ-11：运行周期开始时一次性捕获 AssetSnapshot，轮内全部读取走该快照）----

    def _capture_spec_payload(self) -> Mapping[str, Any]:
        """一次性捕获 Agent Spec 快照 payload（OQ-11）——调用方每运行周期至多一次。"""
        snap = self._spec_snapshot
        if snap is None:
            snap = get_asset_center().snapshot(_SPEC_DATA_ID)
        if snap is None:
            raise RuntimeError(f"Agent Spec 快照不可用（{_SPEC_DATA_ID} 未装载）")
        return snap.payload

    def _capture_mapping_payload(self) -> Mapping[str, Any]:
        """一次性捕获模型映射快照 payload（OQ-11）——调用方每运行周期至多一次。"""
        snap = self._mapping_snapshot
        if snap is None:
            snap = get_asset_center().snapshot(_MAPPING_DATA_ID)
        if snap is None:
            raise RuntimeError(f"模型映射快照不可用（{_MAPPING_DATA_ID} 未装载）")
        return snap.payload

    def _capture_mcp_payload(self) -> Mapping[str, Any]:
        """一次性捕获 MCP 清单快照 payload（OQ-11 同源——搜索链解析的配置面）。

        资产中心未启动（单测/Studio 直跑）→ 空表（链解析回落全不可用——假体注入面）。
        """
        try:
            snap = get_asset_center().snapshot(_MCP_DATA_ID)
        except Exception as exc:  # noqa: BLE001 —— 回落属预期运行形态（组装层未启动）
            AgentLog(
                message=f"mcp 快照捕获异常（回落空表）：{type(exc).__name__}: {exc}",
                graph="turn_runner",
            ).warning()
            return {}
        if snap is None:
            AgentLog(
                message="mcp 快照缺失（store 无该 dataId）",
                graph="turn_runner",
            ).warning()
            return {}
        if not snap.payload:
            AgentLog(
                message=(
                    f"mcp 快照为空 payload（version={snap.version} md5={snap.md5} "
                    f"data_id={snap.data_id} updated_at={snap.updated_at}）"
                ),
                graph="turn_runner",
            ).warning()
        return snap.payload

    def _default_policy(self, spec_payload: Mapping[str, Any]) -> str:
        """缺省并发策略（入参=已捕获快照，不再实时读）。"""
        turn = spec_payload.get("turn") or {}
        return str(turn.get("default_on_conflict", _DEFAULT_POLICY))

    def _turn_timeout_s(self, spec_payload: Mapping[str, Any], mode: str) -> float:
        """轮级硬超时（入参=已捕获快照，不再实时读；500301 熔断依据）。"""
        spec = spec_payload.get(mode) or {}
        return float(spec.get("turn_timeout_s", _DEFAULT_TURN_TIMEOUT_S))

    # ---- 提交裁决（请求事务内；路由层在建轮后、响应前调用）----

    async def resolve_conflict_on_submit(
        self,
        session: Any,
        *,
        thread_id: str,
        turn_id: str,
        on_conflict: str | None,
    ) -> SubmitDecision:
        """并发策略裁决 + 状态推进 + 打断登记（请求级 session，事务边界归 get_db）。

        - 决策矩阵产出 proceed/enqueue/interrupt/rollback；REJECT 抛 409001
          （ConflictException——请求级 get_db 整体回滚，thread_service 刚建的
          pending turn 行随之消除，零残留）；
        - 状态推进：proceed → running（POST 响应 status=running，api.md §2.1）、
          enqueue/interrupt/rollback → queued（响应 status=queued）；
        - interrupt/rollback 对每个 ACTIVE 轮登记协作式取消标志（superseded/
          rolled_back）——被打断轮由图循环节点边界感知后收尾；新轮登记为线程级
          抢占轮（裁决 A：被打断轮终态后插队启动，既有 queued 顺延）。
          抢占登记在本请求事务内（进程内存，先于 commit）——若事务其后回滚
          （新轮行消失），标记由消费侧自愈清除（_can_start 校验失配 /
          schedule_turn 读不到行），不会永久阻塞该线程既有 queued 轮（评审 P2）。
        """
        # OQ-11：请求周期一次性捕获 spec 快照为执行依据（缺省策略读快照，不实时反复读；
        # 热更仅对新提交生效——api.md §0 / nacos-config.md §3 turn 节生效边界）
        spec_payload = self._capture_spec_payload()
        policy = on_conflict if on_conflict is not None else self._default_policy(spec_payload)
        # 裁决串行化（评审 P2）：先 FOR UPDATE 锁线程行再查活动轮——READ COMMITTED
        # 下两个并发 POST 互相看不到对方未提交轮，双双 PROCEED 会击穿「每线程同时
        # 一轮」；线程行锁把两笔裁决串成先后事务，后者读到前者已提交的 running。
        await session.execute(
            select(Thread).where(Thread.id == uuid.UUID(thread_id)).with_for_update()
        )
        stmt = select(Turn).where(
            Turn.thread_id == uuid.UUID(thread_id),
            Turn.id != uuid.UUID(turn_id),
            Turn.status.in_([*ACTIVE_STATUSES, QUEUED_STATUS]),
        )
        rows = (await session.scalars(stmt)).all()
        has_running = any(row.status in ACTIVE_STATUSES for row in rows)
        has_queued = any(row.status == QUEUED_STATUS for row in rows)
        decision = decide_on_submit(policy, has_running=has_running, has_queued=has_queued)
        if decision is SubmitDecision.REJECT:
            raise ConflictException(
                "线程已有进行中轮次",
                detail=(
                    f"thread {thread_id} policy={policy} "
                    f"has_running={has_running} has_queued={has_queued}"
                ),
            )
        me = await session.get(Turn, uuid.UUID(turn_id))
        if me is not None:
            if decision is SubmitDecision.PROCEED:
                me.status = "running"
                # started_at=图运行开始（data-model.md §1.3）：PROCEED 不经
                # _promote_running，同事务盖章——否则即时轮 started_at 落到
                # 终态防御分支≈finished_at（评审 P2）
                me.started_at = datetime.now(UTC)
            else:
                me.status = QUEUED_STATUS
        if decision in (SubmitDecision.INTERRUPT, SubmitDecision.ROLLBACK):
            reason = "superseded" if decision is SubmitDecision.INTERRUPT else "rolled_back"
            for row in rows:
                if row.status in ACTIVE_STATUSES:
                    self._cancel_flags[str(row.id)] = reason
            # 抢占登记（裁决 A）：打断后新轮插队——被打断轮 canceled 后执行槽归
            # 新轮，既有 queued 顺延到新轮终态之后（_can_start 消费本标记）
            self._preempt_turns[thread_id] = turn_id
        return decision

    # ---- 协作式取消（api.md §2.7）----

    async def cancel_turn(
        self, turn_id: str, *, user_id: str, reason: str = "user"
    ) -> dict[str, str]:
        """停止轮次：running/pending=登记标志协作式收尾；queued=立即出队+终态。

        - 归属校验（id+user 双条件）→ 404001；终态轮 → 409002（TurnTerminalException）；
        - 响应契约恒 {"turn_id", "status": "canceling"}（canceling 仅为响应信号，
          非 turns.status 枚举值——api.md §2.7 v1.4 注）。
        """
        try:
            turn_uuid = uuid.UUID(turn_id)
        except ValueError as exc:
            raise NotFoundException(
                "轮次不存在", detail=f"非法 turn id：{turn_id!r}"
            ) from exc
        async with self._session_factory() as session:
            row = await session.get(Turn, turn_uuid)
            if row is None or str(row.user_id) != user_id:
                raise NotFoundException(
                    "轮次不存在",
                    detail=f"turn_id={turn_id} 不存在或不属于当前用户",
                )
            status = row.status
            thread_id = str(row.thread_id)
        if status in FINAL_STATUSES:
            raise TurnTerminalException(
                "轮次已处于终态，不可取消",
                detail=f"turn {turn_id} status={status}",
            )
        if status == QUEUED_STATUS:
            # 第二道感知（评审 P1）：读 queued→出队收尾与调度 promote 存在交错窗口
            # ——登记标志后若调度已 promote 并进入执行，入口 pop 走 _finish_canceled，
            # 其条件落库与本方互斥（行锁串行），恒单终态；收口后清理防标志泄漏。
            self._cancel_flags[turn_id] = reason
            await self._finish_canceled(
                str(turn_uuid), thread_id, reason="user"
            )
            self._cancel_flags.pop(turn_id, None)
            self._nudge_thread(thread_id)
        else:
            self._cancel_flags[turn_id] = reason  # pending/running：节点边界感知
        return {"turn_id": turn_id, "status": "canceling"}

    # ---- 短事务持久化（TurnRunner 例外：写入即 commit 即归还连接）----

    async def _persist_terminal(
        self,
        turn_id: str,
        *,
        status: str,
        answer_md: str | None = None,
        error: dict[str, Any] | None = None,
        cancel_reason: str | None = None,
        stages: list[dict[str, Any]] | None = None,
        degraded_reason: str | None = None,
        rewritten_queries: list[str] | None = None,
    ) -> tuple[datetime, int] | None:
        """终态落 turns（条件收口：行锁 + 终态重查，终态绝不被覆写）。

        duration 口径=「提交→done」：finished_at − created_at（data-model.md §1.3）。
        返回 (finished_at, duration_ms) 供 done payload 复用（先落库后发事件）；
        返回 None = 行已终态（cancel 与图收尾的交错竞态本方落败，评审 P1）——
        调用方必须跳过 done/error 发布，杜绝双终态事件（winner 已发布其一）。
        行锁（FOR UPDATE）+ 锁内终态重查与「UPDATE ... WHERE status NOT IN final
        0 行命中跳过」语义等价：锁持有期内无并发写者，重查即裁决。
        """
        async with self._session_factory() as session:
            row = await session.get(Turn, uuid.UUID(turn_id), with_for_update=True)
            if row is None:
                # 编程错误兜底（轮被并发删除）；不阻塞事件面收尾
                now = datetime.now(UTC)
                return now, 0
            # 抢占轮到达终态（胜/败两分支都算终态可见）：清线程级抢占标记，
            # 既有 queued 轮恢复常规 FIFO 启动（裁决 A）
            self._forget_preempt(str(row.thread_id), turn_id)
            if row.status in FINAL_STATUSES:
                return None  # 终态竞态落败：winner 已发布终态事件，本方零事件退出
            now = datetime.now(UTC)
            row.status = status
            if answer_md is not None:
                row.answer_md = answer_md
            row.timing = {"stages": stages or []}
            row.usage = {}
            row.finished_at = now
            if row.started_at is None:  # 失败/取消路径可能未经 promote（防御）
                row.started_at = now
            row.duration_ms = max(
                0, int((now - row.created_at).total_seconds() * 1000)
            )
            if error is not None:
                row.error = error
            if cancel_reason is not None:
                row.cancel_reason = cancel_reason
            if degraded_reason is not None:
                row.degraded_reason = degraded_reason
            if rewritten_queries is not None:
                row.rewritten_queries = rewritten_queries
            await session.commit()
            return now, int(row.duration_ms or 0)

    async def _publish_and_persist(
        self,
        ctx: _RunContext,
        event_type: str,
        business: dict[str, Any],
        *,
        sources_batch: list[dict[str, Any]] | None = None,
    ) -> SseEvent:
        """发布（EventBus 盖章线程级 seq）→ 同 seq 落 turn_events（同一短事务）。

        sources 事件在**同一短事务**内写 sources 表（ref_no 唯一约束语义；
        score/Decimal 化——jsonb 里的 float 经 str() 精确转换）。
        source_id 单一事实源（data-model.md §1.5「id 即 api.md 的 source_id」）：
        先建行定 id、以行 id 回写事件 batch 后再发布——§7.2 sources 事件 /
        turn_events payload / §2.5 sources[] 三面同 id 可对账（评审 P2）；
        回写为拷贝重写，不改图侧入参 dict。
        """
        source_rows: list[Source] = []
        if sources_batch:
            for item in sources_batch:
                score = item.get("score")
                source_rows.append(
                    Source(
                        id=uuid.uuid4(),
                        turn_id=uuid.UUID(ctx.turn_id),
                        user_id=uuid.UUID(str(ctx.user_id)),
                        ref_no=int(item["ref_no"]),
                        source_type=str(item["source_type"]),
                        title=str(item.get("title", "")),
                        url=item.get("url"),
                        snippet=item.get("snippet"),
                        score=Decimal(str(score)) if score is not None else None,
                    )
                )
            business["batch"] = [
                {**item, "source_id": str(row.id)}
                for item, row in zip(sources_batch, source_rows)
            ]
        stamped = await self._bus.publish(
            ctx.turn_id,
            SseEvent(thread_id=ctx.thread_id, event_type=event_type, data=business),
        )
        async with self._session_factory() as session:
            session.add(
                TurnEvent(
                    thread_id=uuid.UUID(ctx.thread_id),
                    turn_id=uuid.UUID(ctx.turn_id),
                    seq=stamped.seq if stamped.seq is not None else 0,
                    event_type=event_type,
                    payload=business,
                )
            )
            for row in source_rows:
                session.add(row)
            await session.commit()
        return stamped

    async def _publish_only(self, ctx: _RunContext, event_type: str, data: dict[str, Any]) -> None:
        """只发布不落库（token/thinking——ADR-003 第 4 条不入 turn_events）。"""
        await self._bus.publish(
            ctx.turn_id,
            SseEvent(thread_id=ctx.thread_id, event_type=event_type, data=data),
        )

    # ---- 终态收尾（不变式：先落 turns 再 publish done）----

    async def _finish_completed(self, ctx: _RunContext, final_state: dict[str, Any]) -> None:
        answer_md = final_state.get("answer_md")
        # 图内降级留痕接线（§6.2）：search 单源失败/备位装载 → FastState.degraded=True
        # → done.degraded=True + degraded_reason=source_failure（turns.degraded_reason 列同步）
        degraded = bool(final_state.get("degraded"))
        raw_queries = final_state.get("rewritten_queries")
        rewritten = (
            [str(q) for q in raw_queries]
            if isinstance(raw_queries, list)
            else None
        )
        outcome = await self._persist_terminal(
            ctx.turn_id,
            status="completed",
            answer_md=answer_md if isinstance(answer_md, str) else None,
            stages=ctx.stages,
            degraded_reason="source_failure" if degraded else None,
            rewritten_queries=rewritten,
        )
        if outcome is None:
            return  # 终态竞态落败（并发取消已收口）：跳过 done 发布，防双终态
        _, duration_ms = outcome
        await self._publish_and_persist(
            ctx,
            "done",
            {
                "finish": "completed",
                "degraded": degraded,
                "degraded_reason": "source_failure" if degraded else None,
                "duration_ms": duration_ms,
                "source_count": ctx.sources_count,
                "usage": {},
            },
        )
        self._nudge_thread(ctx.thread_id)

    async def _finish_canceled(
        self,
        turn_id: str,
        thread_id: str,
        *,
        reason: str,
        source_count: int = 0,
        stages: list[dict[str, Any]] | None = None,
    ) -> None:
        """canceled 收尾：queued 轮立即出队（user）与运行中轮节点边界收尾（superseded/
        rolled_back/user）共用；保留已产出（sources 已随事件即时落库）。"""
        ctx_like = _RunContext(turn_id=turn_id, thread_id=thread_id)
        outcome = await self._persist_terminal(
            turn_id, status="canceled", cancel_reason=reason, stages=stages or []
        )
        if outcome is None:
            return  # 终态竞态落败（图收尾已完成）：跳过 done 发布，防双终态
        _, duration_ms = outcome
        await self._publish_and_persist(
            ctx_like,
            "done",
            {
                "finish": "canceled",
                "degraded": False,
                "degraded_reason": None,
                "duration_ms": duration_ms,
                "source_count": source_count,
                "usage": {},
                "cancel_reason": reason,
            },
        )

    async def _finish_degraded_with_error(
        self,
        ctx: _RunContext,
        *,
        code: str,
        message: str,
        exc: Exception | None = None,
        degraded_reason: str | None = None,
    ) -> None:
        """部分内容降级收束：error 事件 + turns.status=degraded + done(finish=degraded)。

        api.md §7.2 error 收束括注（v1.10 ⑥）：有部分内容（已收集 sources/部分答案）时
        error 后跟 done(finish=degraded)；终态条件落库先行（竞态落败方零事件退出）。
        """
        AgentLog(message=f"轮次降级收束：{message}", graph="turn_runner").error(exc)
        error_payload: dict[str, Any] = {
            "code": code,
            "message": message,
            "stage_key": None,
        }
        outcome = await self._persist_terminal(
            ctx.turn_id,
            status="degraded",
            error=error_payload,
            stages=ctx.stages,
            degraded_reason=degraded_reason,
        )
        if outcome is None:
            return
        _, duration_ms = outcome
        await self._publish_and_persist(ctx, "error", error_payload)
        await self._publish_and_persist(
            ctx,
            "done",
            {
                "finish": "degraded",
                "degraded": True,
                "degraded_reason": degraded_reason,
                "duration_ms": duration_ms,
                "source_count": ctx.sources_count,
                "usage": {},
            },
        )
        self._nudge_thread(ctx.thread_id)

    # ---- 调度循环（BackgroundTask 入口：commit 后触发；DB 为事实源 + event 加速）----

    async def schedule_turn(self, turn_id: str) -> None:
        """调度单轮：queued/pending 轮等待启动条件 → 推进 running → 执行图。

        - 启动条件（每线程同时一轮）：无其他 ACTIVE 轮（pending/running）且次序
          约束满足——常规为 queued FIFO（更早提交者先行）；interrupt/rollback 的
          新轮为抢占轮（裁决 A，2026-11-08）：被打断轮 canceled 后插队启动
          （「不经排队」），既有 queued 轮顺延到抢占轮终态之后（_can_start/
          _persist_terminal 消费与清除 _preempt_turns 标记）；
        - 终态/cancel 掉的轮 → 直接返回（进程重启后 BackgroundTask 不复跑，
          孤儿轮清理钩子兜底——queued/running 已置 failed）；
        - 抢占标记兜底清除（评审 P2）：行读不到 = 登记后请求事务回滚（行永不出现，
          终态收尾点 _persist_terminal 永不到达）或行已终态（收尾点已过期）——
          早退前清掉指向本轮的标记，避免同线程既有 queued 轮被永久阻塞。
        """
        while True:
            row = await self._load_turn(turn_id)
            if row is None:
                self._forget_preempt_turn(turn_id)
                return
            if row.status in FINAL_STATUSES:
                self._forget_preempt_turn(turn_id)
                return
            if row.status == "running":
                await self._execute_turn(row)
                return
            if await self._can_start(row):
                if not await self._promote_running(turn_id):
                    return  # 推进落败（已终态——如 queued 取消竞态收口）：不执行
                row.status = "running"
                await self._execute_turn(row)
                return
            await self._wait_thread_change(str(row.thread_id))

    async def _load_turn(self, turn_id: str) -> Any:
        async with self._session_factory() as session:
            return await session.get(Turn, uuid.UUID(turn_id))

    async def _persist_query_lang(self, turn_id: str, query_lang: str) -> None:
        """query_lang 落 turns 列（图启动前短事务；§2.5 事实源——列注释「TurnRunner 注入」）。"""
        async with self._session_factory() as session:
            row = await session.get(Turn, uuid.UUID(turn_id))
            if row is not None and row.query_lang != query_lang:
                row.query_lang = query_lang
                await session.commit()

    async def _load_history(self, thread_id: str, turn_id: str) -> list[dict[str, str]]:
        """最近 N 轮历史装配（ARCH §6.5：{q, a_head≤HISTORY_ANSWER_HEAD_CHARS}）。

        短事务只读，图启动前完成；纳入/排除口径见 history_statement。返回时间正序
        （旧→新，提示词装配自然读序）。
        """
        async with self._session_factory() as session:
            rows = (await session.execute(history_statement(thread_id, turn_id))).all()
        return [
            {
                "q": str(query),
                "a_head": str(answer_md or "")[:HISTORY_ANSWER_HEAD_CHARS],
            }
            for query, answer_md in reversed(rows)
        ]

    async def _can_start(self, row: Any) -> bool:
        """启动条件（每线程同时一轮）：无其他 ACTIVE 轮 + 抢占/FIFO 次序约束。

        - 常规：queued FIFO（更早提交者先行）；
        - 抢占（裁决 A，2026-11-08）：线程存在未终态抢占轮（interrupt/rollback
          新轮）时——抢占轮 bypass FIFO 直接启动，既有 queued 轮顺延等待
          （「打断后立即执行新轮、不经排队；已有 queued 不受影响保持排队」）；
          抢占轮终态后标记清除（_persist_terminal），恢复常规 FIFO；
        - 陈旧标记自愈（评审 P2）：登记发生在请求事务内（resolve），事务回滚时
          新轮行消失、唯终态清除点永不到达。消费时校验被引用轮仍在场且未终态，
          失配即清标记并按常规 FIFO 放行——否则本线程 queued 轮被永久阻塞
          （0.2s 兜底轮询空转至进程重启）。已知取舍：登记先于 commit，被引用轮
          在提交前对该查询不可见 → 理论上存在毫秒级窗口使本方法提前自愈（抢占
          次序退化为常规 FIFO，仍会执行、无阻塞无数据损失）；换取的是任何回滚
          路径都不会留下永久标记（无时间阈值/常量，判据全来自 DB 事实源）。
        """
        thread_id = str(row.thread_id)
        preemptor = self._preempt_turns.get(thread_id)
        stmt = select(Turn).where(
            Turn.thread_id == row.thread_id,
            Turn.id != row.id,
            Turn.status.in_([*ACTIVE_STATUSES, QUEUED_STATUS]),
        )
        async with self._session_factory() as session:
            rows = (await session.scalars(stmt)).all()
        if preemptor is not None and preemptor != str(row.id):
            if not self._preemptor_alive(preemptor, rows):
                self._forget_preempt(thread_id, preemptor)
                preemptor = None
        for other in rows:
            if other.status in ACTIVE_STATUSES:
                return False
            if other.status == QUEUED_STATUS and other.created_at < row.created_at:
                if preemptor != str(row.id):
                    return False  # queued FIFO：更早提交者先行（抢占轮 bypass）
        if preemptor is not None and preemptor != str(row.id):
            return False  # 未终态抢占轮在场：既有 queued 顺延（裁决 A）
        return True

    async def _promote_running(self, turn_id: str) -> bool:
        """pending/queued → running（短事务；started_at=图运行开始——排队时长可观测）。

        条件推进（评审 P1）：仅 pending/queued 可推 running——queued 取消与
        promote 交错时行已终态（canceled），本方落败返回 False、调用方不执行，
        杜绝「canceled 被改回 running 再覆写 completed」的双终态。
        """
        async with self._session_factory() as session:
            row = await session.get(Turn, uuid.UUID(turn_id), with_for_update=True)
            if row is None or row.status not in ("pending", QUEUED_STATUS):
                return False
            row.status = "running"
            row.started_at = datetime.now(UTC)
            await session.commit()
            return True

    async def _wait_thread_change(self, thread_id: str) -> None:
        """等待线程变更信号（0.2s 兜底轮询——DB 是事实源，event 仅延迟优化）。"""
        event = self._thread_events.setdefault(thread_id, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout=0.2)
        except TimeoutError:
            pass
        event.clear()

    def _nudge_thread(self, thread_id: str) -> None:
        event = self._thread_events.get(thread_id)
        if event is not None:
            event.set()

    # ---- TurnRunner：图执行 + 事件翻译（ARCH §6.1）----

    async def _execute_turn(self, turn: Any) -> None:
        """执行单轮：astream 三流消费 → 翻译 → publish/落库 → 终态收尾。

        - OQ-11：运行开始一次性捕获 spec/mapping 快照存入 _RunContext——轮内全部
          读取（timeout/注入 state 的 spec/chat 模型解析）一律走该快照，热更不串轮；
        - 初始 state 注入 FastState 全量输入（ARCH §4.1）：历史装配（DB 短事务）+
          query_lang 前置纯代码判定（OQ-2）+ chat_model_key 复用 resolve_chat_model；
          images 恒空（M4 视觉轮预留）；
        - 取消感知在每个事件边界（含启动前）——pop 标志即收尾 canceled；
          timeout 守卫 = asyncio.timeout(spec.<mode>.turn_timeout_s) 包整个 astream。
        """
        turn_id = str(turn.id)
        thread_id = str(turn.thread_id)
        ctx = _RunContext(
            turn_id=turn_id,
            thread_id=thread_id,
            user_id=turn.user_id,
            spec_payload=self._capture_spec_payload(),
            mapping_payload=self._capture_mapping_payload(),
            mcp_payload=self._capture_mcp_payload(),
        )
        timeout_s = self._turn_timeout_s(ctx.spec_payload, str(turn.mode))
        cancel_reason = self._cancel_flags.pop(turn_id, None)
        if cancel_reason is not None:  # 启动前已被取消/打断：零业务事件直接收尾
            await self._finish_canceled(turn_id, thread_id, reason=cancel_reason)
            self._nudge_thread(thread_id)
            return
        query_lang = detect_query_lang(str(turn.query))
        await self._persist_query_lang(turn_id, query_lang)
        # 搜索链轮启动冻结（nacos-config §2：装载决策读熔断实时态一次，轮内中途熔断不切换）
        tools_registry = (
            self._tools_registry
            if self._tools_registry is not None
            else get_tools_registry()
        )
        tools_registry.bind(ctx.mcp_payload)  # 连接条目+熔断参数按本轮快照刷新（指纹缓存复用）
        search_chain = tools_registry.resolve(ctx.mcp_payload, query_lang)
        if search_chain.degraded or not search_chain.sources:
            servers = ctx.mcp_payload.get("servers") if isinstance(ctx.mcp_payload, Mapping) else None
            server_summary = (
                {name: entry.get("enabled") for name, entry in servers.items()}
                if isinstance(servers, Mapping)
                else f"payload-type={type(ctx.mcp_payload).__name__} keys={list(ctx.mcp_payload)[:5] if isinstance(ctx.mcp_payload, Mapping) else 'N/A'}"
            )
            creds = {name: tools_registry.credential_ready(name) for name in ("tavily", "bocha", "searxng", "anysearch")}
            AgentLog(
                message=f"搜索链降级/不可用：{search_chain.detail}（sources={search_chain.sources}，servers-enabled={server_summary}，creds={creds}）",
                graph="turn_runner",
            ).warning()
        chat_model_key, _ = resolve_chat_model(
            str(turn.chat_model_key), ctx.mapping_payload
        )
        initial_state: dict[str, Any] = {
            "turn_id": turn_id,
            "thread_id": thread_id,
            "user_id": str(turn.user_id),
            "mode": str(turn.mode),
            "query": str(turn.query),
            "chat_model_key": chat_model_key,
            "history": await self._load_history(thread_id, turn_id),
            "query_lang": query_lang,
            "spec": dict(ctx.spec_payload),  # MappingProxyType → 可序列化 dict 随 state 进图
            "images": [],  # M4 视觉轮预留（REQ 视觉面），本批恒空
        }
        # 首 stage 图前同步发布（ARCH §4.3「首个 stage ≤1s」的结构性保证：不等 LLM/
        # 图首帧——rewrite 节点内不再发 running，避免重复 seq）
        first_stage: dict[str, Any] = {"stage_key": "rewrite", "status": "running"}
        await self._publish_and_persist(ctx, "stage", first_stage)
        self._absorb_stage(ctx, first_stage)  # timing.stages 配对开档（done 帧到达时合档）
        final_state: dict[str, Any] = {}
        try:
            async with asyncio.timeout(timeout_s):
                async for namespace, stream_mode, chunk in self._graph.astream(
                    initial_state,
                    config={
                        "configurable": {
                            "thread_id": thread_id,
                            "turn_id": turn_id,
                            "model_mapping": dict(ctx.mapping_payload),
                            "search_chain": search_chain,
                            "tools_registry": tools_registry,
                        },
                        # Langfuse trace root=轮（ARCH §6.6）：trace_id=turn_id 对账、
                        # session=thread_id 经 langfuse_* metadata 键承载（v3 契约键）
                        "callbacks": build_turn_callbacks(
                            thread_id=thread_id,
                            turn_id=turn_id,
                            user_id=str(turn.user_id),
                            mode=str(turn.mode),
                        ),
                        "metadata": {
                            "langfuse_session_id": thread_id,
                            "langfuse_user_id": str(turn.user_id),
                            "langfuse_tags": [f"mode:{turn.mode}"],
                            "turn_id": turn_id,
                            "mode": str(turn.mode),
                        },
                    },
                    stream_mode=["messages", "updates", "custom"],
                    subgraphs=True,
                ):
                    cancel_reason = self._cancel_flags.pop(turn_id, None)
                    if cancel_reason is not None:  # 节点边界感知（打断非故障）
                        break
                    if stream_mode == "custom":
                        await self._translate_custom(ctx, chunk)
                    elif stream_mode == "messages" and _is_fast_synthesis_messages(
                        namespace, chunk
                    ):
                        # R14③ messages 过滤（§6.1）：仅 fast 子图 synthesis 节点增量映射
                        # token/thinking——rewrite 等其余节点与 deep 侧（批次 5 实测）不放行
                        await self._translate_message_chunk(ctx, chunk)
                    elif stream_mode == "updates" and not namespace:
                        # 顶层 updates 合并最终 state（answer_md/sources——子图投影回填）
                        for update in chunk.values():
                            final_state.update(update)
        except TimeoutError as exc:
            # 500301：零内容 failed+close；有部分内容（sources 已发布）→ 降级收束（timeout）
            await self._fail_turn(
                ctx, code="500301", message="轮次执行超时，请稍后重试", exc=exc
            )
            return
        except SearchChainUnavailable as exc:
            # 全源失败短路（ARCH §4.1 A1 情形 3）：error 事件由 TurnRunner 出（500201）
            await self._fail_turn(
                ctx, code="500201", message="全部搜索源不可用，请稍后重试", exc=exc
            )
            return
        except SynthesisError as exc:
            # LLM 调用重试耗尽（§4.1 synthesis 行 / api.md §7.2 500101）：已收集 sources
            # 保留——error 后跟 done(finish=degraded)（v1.10 error 收束括注「有部分内容」形态）
            await self._finish_degraded_with_error(
                ctx, code="500101", message="综合生成失败，已保留检索来源", exc=exc,
                degraded_reason=None,  # 枚举（timeout/budget/source_failure）外不上报
            )
            return
        except Exception as exc:
            await self._fail_turn(ctx, code="500001", message="内部错误", exc=exc)
            return
        self._cancel_flags.pop(turn_id, None)  # 防御：清理运行中登记的残留标志
        if cancel_reason is not None:
            await self._finish_canceled(
                turn_id,
                thread_id,
                reason=cancel_reason,
                source_count=ctx.sources_count,
                stages=ctx.stages,
            )
            self._nudge_thread(thread_id)
            return
        await self._finish_completed(ctx, final_state)

    async def _translate_custom(self, ctx: _RunContext, payload: dict[str, Any]) -> None:
        """custom 信号 → SSE 事件（一对一翻译；未知类型告警忽略）。

        token/thinking 不在 custom 域（§6.1：仅 messages 流映射——B3 节点 5/5 接真后
        custom 流仅承载 stage/sources/images；桩期 custom-token 路径随桩清理移除）。
        """
        ptype = payload.get("type")
        business = {k: v for k, v in payload.items() if k != "type"}
        if ptype == "stage":
            self._absorb_stage(ctx, business)
            await self._publish_and_persist(ctx, "stage", business)
        elif ptype == "sources":
            batch = business.get("batch") or []
            await self._publish_and_persist(
                ctx, "sources", business, sources_batch=batch
            )
            ctx.sources_count += len(batch)
        elif ptype == "images":
            await self._publish_and_persist(ctx, "images", business)  # B2 桩不发；翻译面预留
        else:
            AgentLog(
                message=f"忽略未知图内信号类型：{ptype!r}", graph="turn_runner"
            ).warning()

    async def _translate_message_chunk(self, ctx: _RunContext, chunk_pair: Any) -> None:
        """messages 帧翻译（调用面已经 _is_fast_synthesis_messages 过滤）。"""

        """messages 帧 → token/thinking（按帧读 .content——R8：禁止聚合后再过 blocks）。"""
        message = chunk_pair[0] if isinstance(chunk_pair, tuple) else chunk_pair
        for kind, delta in self._extract_deltas(message):
            await self._publish_only(ctx, kind, {"delta": delta})

    @staticmethod
    def _extract_deltas(message: Any) -> list[tuple[str, str]]:
        """单帧增量化：reasoning_content → thinking；str content / text 块 → token；
        thinking 内容块 → thinking。空 delta 跳过。"""
        out: list[tuple[str, str]] = []
        kwargs = getattr(message, "additional_kwargs", None) or {}
        reasoning = kwargs.get("reasoning_content")
        if reasoning:
            out.append(("thinking", str(reasoning)))
        content = getattr(message, "content", None)
        if isinstance(content, str):
            if content:
                out.append(("token", content))
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text" and block.get("text"):
                    out.append(("token", str(block["text"])))
                elif btype == "thinking" and block.get("thinking"):
                    out.append(("thinking", str(block["thinking"])))
        return out

    def _absorb_stage(self, ctx: _RunContext, business: dict[str, Any]) -> None:
        """stage 信号 → turns.timing.stages 配对聚合（running 开档 / done·failed 合档）。

        §7.2 可选业务字段 progress/detail/extra 随合档写入条目（评审 P2——
        此前被丢弃，B3 图一发即静默丢失）：extra 经 get_turn_detail 透传给
        §2.5 stages[].extra；progress/detail 留档 timing 供 B3 消费/审计
        （§2.5 面未冻结其字段，扩面时再映射）。
        """
        key = str(business.get("stage_key", ""))
        status = str(business.get("status", ""))
        now = datetime.now(UTC)
        if status == "running":
            ctx.stage_started[key] = now
            if business.get("label"):
                ctx.stage_label[key] = str(business["label"])
            return
        started = ctx.stage_started.pop(key, None)
        if started is None:
            return
        entry: dict[str, Any] = {
            "stage_key": key,
            "status": status,
            "started_at": started.isoformat(),
            "ended_at": now.isoformat(),
            "duration_ms": int((now - started).total_seconds() * 1000),
        }
        label = ctx.stage_label.pop(key, None)
        if label:
            entry["label"] = label
        for name in ("progress", "detail", "extra"):
            if name in business:
                entry[name] = business[name]
        ctx.stages.append(entry)

    async def _fail_turn(
        self,
        ctx: _RunContext,
        *,
        code: str,
        message: str,
        exc: Exception | None = None,
    ) -> None:
        """失败路径（api.md §7.2 v1.10 error 收束两句）：

        - **有部分内容**（sources 已发布）→ error + done(finish=degraded) +
          turns.status=degraded（§7.1 例外把 failed 终态限定为「失败且无部分内容」）；
        - **零内容** → error + turns.status=failed + close_channel（「直接关闭」口径）。

        条件落库先行：终态竞态落败（并发取消已收口）→ 不发 error、不 close——
        winner 的 done 已把流收口，失败方零事件退出。
        """
        if ctx.sources_count > 0:
            # timeout/未知异常落在 sources 之后=有部分内容——降级收束（同 500101 形态）
            degraded_reason = "timeout" if code == "500301" else None  # 枚举内才上报
            await self._finish_degraded_with_error(
                ctx, code=code, message=message, exc=exc, degraded_reason=degraded_reason
            )
            return
        AgentLog(message=f"轮次执行失败：{message}", graph="turn_runner").error(exc)
        error_payload: dict[str, Any] = {
            "code": code,
            "message": message,
            "stage_key": None,
        }
        outcome = await self._persist_terminal(
            ctx.turn_id, status="failed", error=error_payload, stages=ctx.stages
        )
        if outcome is None:
            return
        await self._publish_and_persist(ctx, "error", error_payload)
        await self._bus.close_channel(ctx.turn_id)
        self._nudge_thread(ctx.thread_id)
