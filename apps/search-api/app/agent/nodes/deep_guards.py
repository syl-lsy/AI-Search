"""guards 节点（M2 批次 3——ARCH §4.2 guards 行 / DEV §5.4 五步闭环）。

writer 前最后防线（拓扑 guards→write——ARCH §4.2:473/512「guards: 预算·硬超时·取消·
deadline-aware」）。**双检查点语义（评审修复 1 订正——发现 2）**：ARCH §4.3/plan
批次 3 的「reflect 出边与 writer 前双检查点」= ①**回路中途 trip 归 reflect 节点判定
前预检**（`deep_reflect._pre_trip` 读同一注入面 turn_deadline/usage_collector——guards
在回路退出后才执行〔拓扑 guards→write→END 零回边〕，出边判据需 reflect 写入
degraded_reason 才活）+ ②**writer 前检查点=本节点**；`route_reflect` 的
degraded_reason 判据（_loop_continues 单一事实源）承载 trip 后的出口收敛，非独立
检查点。deep 子图内零 LLM 零 IO（纯检查点编排面——预算/超时取值经 configurable
注入，取消经 ensure_not_canceled 基元），节点纯函数零 DB/EventBus。

**三检查点（§6.2 四件套 deep 侧落地）**：
- **取消感知**：ensure_not_canceled 入口接线（R14②——pop 命中即抛 TurnCanceledSignal
  穿透重抛；取消非故障，TurnRunner 捕获走 _finish_canceled：done(finish=canceled) 零
  error、保留已产出、不进降级不产降级报告——ADR-010/ARCH §4.2:483 取消语义注；
  TurnRunner 层收尾由 test_turn_service.py:500-527 中途注入面已锁）；
- **硬超时**：turn_deadline（TurnDeadline 实例，TurnRunner 轮启动建立经 configurable
  注入——轮级锚点不可节点内重建：guards 虽单次执行，注入式锚点保证与 TurnRunner
  asyncio.timeout(:2100) 同钟同源）remaining ≤ DEGRADED_WRITER_ENVELOPE_S(30)
  + TRIP_MARGIN_S(15) → trip timeout（降级包络=min(30, max(remaining−15, 5))，
  见下方常量权威注释；裕量让 trip 提前发生，降级调用恒有全窗+调度裕量）；
- **预算**：usage_total = usage_collector.total()["total_tokens"]（TurnRunner 注入
  UsageCollector 引用——批次 1 方案 A 轮级累计 callback 的图内消费面；cancel_flags
  进程级引用注入先例 turn_service.py:2107-2109 同款）≥ spec.deep.token_budget
  （缺省 500000——nacos-config.md §3 deep 节契约初值；validator reflect_rounds≤max
  之外的图运行面第二道防线口径同 effective_rounds）→ trip budget。
  优先级 timeout > budget（轮级硬超时=最后防线——超时轮剩余时间不足以承载任何后续
  工作，语义上高于用量熔断）；trip → {"degraded_reason": ...}（api.md §7.2:803 枚举
  timeout|budget 两值；§7.2:845 500301 v1.21 注——图内熔断零 error，走
  done(finish=degraded, degraded_reason=同值) 表达，TurnRunner _finish_completed
  分流已接 turn_service.py:1434-1454）。

**幂等（D5）**：state.degraded_reason 已置不覆写（防御形态——本节点返回零
degraded_reason 键即不触碰覆盖键语义）；usage_total 检查点快照恒随 collector 在位
写入（trip 与否均写——DeepState.usage_total 声明消费面；done.usage 仍走
collector.total()=turn_service.py:1467，终值回填归批次 4 finalize——ARCH §4.2
finalize 行）。

**DeepState.degraded 收敛口径（D2 定案，2026-11-16 批次 2 派单裁决的批次 3 落定）**：
guards 不写 degraded 布尔——deep 降级单一事实源=degraded_reason（timeout|budget 两
值经 _finish_completed guard_degraded 分流 turns.status=degraded）；degraded=True→
source_failure 派生分支（turn_service.py:1450-1451 elif）deep 轮结构性不可达（deep
节点零写 degraded=True——deep_research.py:55-56/reflect/supplement 同口径）；
ARCH §4.2:503 DeepState 骨架双键声明保留（结构决策不私改），state.py 注释随本定案
收敛。

**注入面可选（guard 不抛——DEV §5.3:308 节点不依赖 TurnRunner 运行时设施的既有
口径，pop_cancel_flag 无注入面静默同款）**：无 usage_collector → 跳过预算检查且
不写 usage_total；无 turn_deadline → 跳过超时检查；双无 → 返回 {}（与批次 3 桩
行为零差异——Studio 直跑/单测形态灰度安全）。

**degraded_writer 路径（批次 4 接线落定）**：trip 置位 → `route_guards` 条件边
（本模块）分流 degraded_writer（ARCH §4.2:475-480 两出口；降级路径 DG→END 直通
不进 finalize——ARCH :480；TurnRunner 分流 done(finish=degraded) 零 error，
turn_service.py:1441-1454 批次 1 已接）。**trip 判据 = remaining ≤
DEGRADED_WRITER_ENVELOPE_S(30) + TRIP_MARGIN_S(15)**（DEGRADED_WRITER_ENVELOPE_S=
30.0，OQ-6 代码常量先例）；**降级包络 = min(30, max(remaining−15, 5))**——
degraded_writer 消费该式（非恒等 30s：裕量让 trip 提前、包络随剩余收缩并有 5s
下限兜底，权威注释见上方常量定义）。
"""

from collections.abc import Mapping
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.agent.guards import ensure_not_canceled

__all__ = [
    "DEGRADED_WRITER_ENVELOPE_S",
    "TRIP_MARGIN_S",
    "DEFAULT_TOKEN_BUDGET",
    "guards",
    "route_guards",
]

#: degraded_writer 降级报告包络（秒）——guards trip timeout 的阈值依据（剩余轮级
#: 预算 ≤ 该值即让位）：ARCH §4.2:514「超时包络 30s」（批次 4 degraded_writer 实装
#: 消费同值）；代码常量（OQ-6 IMAGES_MAX_COUNT 先例——与环境无关的纯逻辑值）
DEGRADED_WRITER_ENVELOPE_S: float = 30.0

#: trip 裕量（秒，批次 4 评审 P1 修复）——trip 判据=remaining ≤ 包络+裕量：裸判据
#: （≤30s）下降级节点实际剩余=remaining−调度开销 < 自身 30s 包络，慢 LLM 撞满即被
#: 外层轮级 asyncio.timeout 截杀 → error(500301) 违背 api.md §7.2:845「图内熔断
#: 零 error」钉死形态（复跑实证：真调用撞轮级包络 → 500301 error 事件）。裕量让
#: trip 提前发生，降级调用恒有 ENVELOPE 全窗+MARGIN 调度裕量。guards 与
#: reflect._pre_trip 同式同源（双检查点单一事实源），degraded_writer 包络消费
#: min(ENVELOPE, remaining−MARGIN)（防御下限 _MIN_ENVELOPE_S）。
TRIP_MARGIN_S: float = 15.0

#: 包络防御下限（秒）——remaining−MARGIN 极端形态（异常注入/时钟漂移）兜底：
#: 包络恒 ≥ 5s，机械速览兜底窗保底。
_MIN_ENVELOPE_S: float = 5.0

#: spec.deep.token_budget 缺失/非法回落（nacos-config.md §3 deep 节契约初值 500k）
DEFAULT_TOKEN_BUDGET: float = 500000.0


def _deep_section(spec: Any) -> dict[str, Any]:
    """spec.deep 节（结构非法视同缺失——guard 不抛）。"""
    if isinstance(spec, dict):
        section = spec.get("deep")
        if isinstance(section, dict):
            return section
    return {}


def _token_budget(state: Any) -> float:
    """spec.deep.token_budget 解析（非法/非正回落缺省 500000——guard 不抛口径）。"""
    raw = _deep_section(state.get("spec") if isinstance(state, dict) else None).get("token_budget")
    try:
        value = float(raw)  # type: ignore[arg-type]
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    return DEFAULT_TOKEN_BUDGET


async def guards(state: Any, config: RunnableConfig | None = None) -> dict[str, Any]:
    """guards 节点（deep 子图）：取消感知 → 硬超时/预算双检查 → trip 置
    degraded_reason；无注入面零行为直通（等价桩）。"""
    ensure_not_canceled(config)  # 取消非故障——穿透重抛（TurnRunner _finish_canceled 收尾）

    configurable = (config or {}).get("configurable") or {}
    update: dict[str, Any] = {}

    # 预算检查（注入面可选）：usage_total 快照恒随 collector 在位写入（检查点留痕）
    collector = configurable.get("usage_collector")
    usage_total: int | None = None
    if collector is not None:
        try:
            total = collector.total()
        except Exception:  # noqa: BLE001 —— collector 异常视同零用量（观测面不阻塞主链路）
            total = {}
        usage_total = int(total.get("total_tokens") or 0)
        update["usage_total"] = usage_total

    # 硬超时检查（注入面可选）：剩余 ≤ 包络+裕量 → 让位降级报告（裕量=降级调用
    # 的调度开销预留——批次 4 评审 P1，包络与轮级包络零裕量竞态消除）
    deadline = configurable.get("turn_deadline")
    timeout_tripped = False
    if deadline is not None:
        try:
            remaining = float(deadline.remaining_s())
        except Exception:  # noqa: BLE001 —— deadline 异常视同预算充足（观测面不阻塞）
            remaining = float("inf")
        timeout_tripped = remaining <= DEGRADED_WRITER_ENVELOPE_S + TRIP_MARGIN_S

    # 预算判定（timeout 优先——轮级硬超时=最后防线语义）
    budget_tripped = usage_total is not None and usage_total >= _token_budget(state)

    # trip 置值（幂等——state.degraded_reason 已置不覆写：本节点零 degraded_reason
    # 键即不触碰覆盖键语义）；D2：guards 不写 degraded 布尔（降级单一事实源=
    # degraded_reason，_finish_completed guard_degraded 分流承载）
    already_tripped = isinstance(state, dict) and state.get("degraded_reason") is not None
    if not already_tripped and (timeout_tripped or budget_tripped):
        update["degraded_reason"] = "timeout" if timeout_tripped else "budget"
    return update


def route_guards(state: Any) -> str:
    """guards 条件边路由（M2 批次 4 接线——ARCH §4.2:475-480 两出口；纯 state 函数，
    langgraph 路由无 config 参数面）：
    - "degraded_writer"：degraded_reason 已置（deep 图内结构性仅 timeout|budget 两值
      ——guards 置位 + reflect._pre_trip 同源；§6.2 双检查点收敛出口）；
    - "writer"：未 trip（正常轮结构性不可达 degraded_writer）。
    取消不经本路由：guards 入口 ensure_not_canceled 先于路由抛 TurnCanceledSignal
    （ADR-010 取消不进降级——done(finish=canceled) 零 error 保留已产出）。"""
    reason = state.get("degraded_reason") if isinstance(state, Mapping) else None
    return "degraded_writer" if reason is not None else "writer"
