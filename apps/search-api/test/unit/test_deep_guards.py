"""unit 层：guards 节点（M2 批次 3——ARCH §4.2 guards 行 / DEV §5.4 五步闭环）。

钉住的语义（冻结契约 + 盘点定案 D2-D5，逐条对应测试）：
- **双检查点之二（writer 前）**：guards 节点本体=writer 前最后防线（拓扑 guards→write
  ——ARCH §4.2:473/512）；之一（reflect 出边）=route_reflect 读 degraded_reason
  （deep_reflect.py:176-187 判据单一事实源，本节点不重复）；
- **预算熔断注入**：usage_total（TurnRunner 注入 usage_collector 的 total()
  ["total_tokens"] 快照）≥ spec.deep.token_budget（缺省 500000——nacos-config.md §3
  deep 节契约初值，validator 之外的图运行面第二道防线）→ trip budget；
- **硬超时注入**：turn_deadline（TurnDeadline 实例，轮启动建立注入——轮级锚点不可
  节点内重建，否则回路重入会重置 300s 预算）remaining ≤ DEGRADED_WRITER_ENVELOPE_S(30)
  + TRIP_MARGIN_S(15) → trip timeout（降级包络=min(30, max(remaining−15, 5))——
  deep_guards.py 常量权威注释）；优先级 timeout > budget（轮级硬超时=最后防线语义）；
- **触发语义**：trip → {"degraded_reason": "timeout"|"budget"}（api.md §7.2:803
  枚举两值；§7.2:845 500301 v1.21 注——图内熔断零 error，走 done(finish=degraded)
  表达，TurnRunner _finish_completed 分流已接 turn_service.py:1434-1454）；
  **幂等**：state.degraded_reason 已置不覆写（D5）；usage_total 检查点快照恒随
  collector 在位写入（DeepState.usage_total 声明消费面；终值回填归批次 4 finalize）；
- **DeepState.degraded 收敛口径（D2 定案）**：guards 不写 degraded 布尔——deep 降级
  单一事实源=degraded_reason；degraded=True→source_failure 派生分支（turn_service.py:
  1450-1451 elif）deep 轮结构性不可达（deep 节点零写 degraded=True）；
- **注入面可选（guard 不抛——DEV §5.3:308 节点不依赖 TurnRunner 设施口径，
  pop_cancel_flag 同款降级）**：无 usage_collector → 跳过预算检查不写 usage_total；
  无 turn_deadline → 跳过超时检查；双无 → 返回 {}（等价桩行为零差异）；
- **取消感知**：ensure_not_canceled 入口接线（R14②——取消非故障，穿透重抛；
  TurnRunner 捕获走 _finish_canceled 零 error 收尾 ADR-010，test_turn_service.py:
  500-527 中途注入收尾面已锁）；
- 复用 agent/guards.py 既有基元（TurnDeadline/ensure_not_canceled/pop_cancel_flag
  ——不另造轮子）；update 键 ⊆ DeepState.__annotations__（R14①）。
"""

from typing import Any

import pytest

from app.agent.guards import TurnCanceledSignal
from app.agent.nodes import deep_guards
from app.agent.nodes.deep_guards import DEGRADED_WRITER_ENVELOPE_S, guards

_SPEC: dict[str, Any] = {"deep": {"token_budget": 500000, "turn_timeout_s": 300}}

_STATE: dict[str, Any] = {
    "query": "pgvector 和 Milvus 个人知识库场景怎么选？",
    "spec": _SPEC,
    "reflect_round": 2,
    "gaps": [],
    "collected_sources": [{"url": "https://a.dev/1", "ref_no": 1}],
}


class _FakeCollector:
    """UsageCollector 假体：total() 返回脚本化值（记录调用）。"""

    def __init__(self, total: dict[str, int]) -> None:
        self._total = total
        self.calls = 0

    def total(self) -> dict[str, int]:
        self.calls += 1
        return dict(self._total)


class _FakeDeadline:
    """TurnDeadline 假体：remaining_s() 返回脚本化值。"""

    def __init__(self, remaining: float) -> None:
        self._remaining = remaining

    def remaining_s(self) -> float:
        return self._remaining


def _config(
    *,
    collector: _FakeCollector | None = None,
    deadline: _FakeDeadline | None = None,
    cancel_flags: dict[str, str] | None = None,
) -> dict[str, Any]:
    configurable: dict[str, Any] = {"thread_id": "th-1", "turn_id": "t-1"}
    if collector is not None:
        configurable["usage_collector"] = collector
    if deadline is not None:
        configurable["turn_deadline"] = deadline
    if cancel_flags is not None:
        configurable["cancel_flags"] = cancel_flags
    return {"configurable": configurable}


def _state_with(**overrides: Any) -> dict[str, Any]:
    state = {**_STATE}
    state.update(overrides)
    return state


# ---- 预算熔断注入（usage_total ≥ token_budget=500000）----


async def test_budget_tripped_over_threshold() -> None:
    """usage_total=600000 ≥ 500000 → trip budget：degraded_reason="budget" +
    usage_total 快照写入；零 degraded 布尔（D2——降级单一事实源=degraded_reason）。"""
    collector = _FakeCollector({"prompt_tokens": 100000, "total_tokens": 600000})

    update = await guards(_state_with(), _config(collector=collector))

    assert update["degraded_reason"] == "budget"
    assert update["usage_total"] == 600000
    assert collector.calls == 1
    assert "degraded" not in update  # D2：guards 不写 degraded 布尔


async def test_budget_not_tripped_under_threshold_writes_snapshot() -> None:
    """usage_total=100000 < 500000 → 不 trip：零 degraded_reason；usage_total 快照
    恒写（检查点留痕——DeepState.usage_total 声明消费面）。"""
    collector = _FakeCollector({"total_tokens": 100000})

    update = await guards(_state_with(), _config(collector=collector))

    assert "degraded_reason" not in update
    assert update["usage_total"] == 100000


async def test_budget_tripped_at_exact_threshold() -> None:
    """usage_total=500000 == token_budget → trip（≥ 语义——到达即熔断）。"""
    collector = _FakeCollector({"total_tokens": 500000})

    update = await guards(_state_with(), _config(collector=collector))

    assert update["degraded_reason"] == "budget"


async def test_budget_missing_total_tokens_counts_zero() -> None:
    """collector.total() 无 total_tokens 键（零用量轮形态）→ 按 0 计不 trip。"""
    collector = _FakeCollector({"prompt_tokens": 42})

    update = await guards(_state_with(), _config(collector=collector))

    assert "degraded_reason" not in update
    assert update["usage_total"] == 0


async def test_budget_default_500k_when_spec_missing_or_invalid() -> None:
    """spec 缺 deep 节/token_budget 非法 → 回落缺省 500000（guard 不抛口径）。"""
    collector = _FakeCollector({"total_tokens": 500001})

    update = await guards(_state_with(spec={}), _config(collector=collector))
    assert update["degraded_reason"] == "budget"

    collector2 = _FakeCollector({"total_tokens": 499999})
    update2 = await guards(
        _state_with(spec={"deep": {"token_budget": "x"}}), _config(collector=collector2)
    )
    assert "degraded_reason" not in update2  # 缺省 500k 下 499999 不 trip


# ---- 硬超时注入（TurnDeadline remaining ≤ 30+15s 包络+裕量）----


async def test_timeout_tripped_below_envelope() -> None:
    """remaining=10s ≤ 45s（30 包络+15 裕量）→ trip timeout（降级包络预留——
    ARCH §4.2:514 30s 依据 + 批次 4 评审 P1 裕量）。"""
    deadline = _FakeDeadline(10.0)

    update = await guards(_state_with(), _config(deadline=deadline))

    assert update["degraded_reason"] == "timeout"


async def test_timeout_not_tripped_above_envelope() -> None:
    """remaining=120s > 45s（30+15）→ 不 trip。"""
    deadline = _FakeDeadline(120.0)

    update = await guards(_state_with(), _config(deadline=deadline))

    assert "degraded_reason" not in update


async def test_timeout_tripped_at_exact_envelope() -> None:
    """remaining=30s ≤ 45s（30 包络+15 裕量）→ trip（≤ 语义——剩余不足以承载降级
    报告全窗+调度裕量时即让位；trip 判据边界为 30+15=45s，见 below/in_margin 用例）。"""
    deadline = _FakeDeadline(30.0)

    update = await guards(_state_with(), _config(deadline=deadline))

    assert update["degraded_reason"] == "timeout"


async def test_timeout_tripped_in_margin_band() -> None:
    """批次 4 评审 P1——trip 判据加裕量：remaining=40s ∈ (30, 30+15] 裕量带 →
    即刻 trip（旧判据 40 不 trip 的形态下，降级节点实际剩余=40s−调度开销 < 30s
    包络+裕量，慢 LLM 撞满即被外层轮级超时截杀→error(500301) 违背 api.md §7.2:845
    图内熔断零 error 形态——裕量带提前让位，降级调用恒有全窗+调度裕量）。"""
    deadline = _FakeDeadline(40.0)

    update = await guards(_state_with(), _config(deadline=deadline))

    assert update["degraded_reason"] == "timeout"


async def test_timeout_not_tripped_beyond_margin_band() -> None:
    """remaining=46s > 30+15 裕量带上界 → 不 trip（正常轮继续研究）。"""
    deadline = _FakeDeadline(46.0)

    update = await guards(_state_with(), _config(deadline=deadline))

    assert "degraded_reason" not in update


async def test_timeout_priority_over_budget_when_both_tripped() -> None:
    """双条件同时满足 → timeout（轮级硬超时=最后防线语义，优先级显式钉死）。"""
    collector = _FakeCollector({"total_tokens": 600000})
    deadline = _FakeDeadline(5.0)

    update = await guards(_state_with(), _config(collector=collector, deadline=deadline))

    assert update["degraded_reason"] == "timeout"


async def test_degraded_reason_idempotent_not_overwritten() -> None:
    """state.degraded_reason 已置（防御形态）→ 不覆写：保持原值，零新 trip 键。"""
    collector = _FakeCollector({"total_tokens": 600000})
    deadline = _FakeDeadline(5.0)

    update = await guards(
        _state_with(degraded_reason="budget"),
        _config(collector=collector, deadline=deadline),
    )

    assert "degraded_reason" not in update  # 不触碰（幂等——已置语义保留）
    assert update["usage_total"] == 600000  # 快照仍写


# ---- 注入面可选（guard 不抛）----


async def test_no_injection_faces_returns_empty() -> None:
    """双注入面缺失（Studio 直跑/单测形态）→ 返回 {}（等价桩行为零差异）。"""
    update = await guards(_state_with(), _config())

    assert update == {}


async def test_collector_only_skips_timeout_check() -> None:
    """仅 collector 注入 → 预算面运行、超时面跳过。"""
    collector = _FakeCollector({"total_tokens": 100})

    update = await guards(_state_with(), _config(collector=collector))

    assert update == {"usage_total": 100}


async def test_deadline_only_skips_budget_check() -> None:
    """仅 deadline 注入 → 超时面运行、预算面跳过（零 usage_total 写入）。"""
    deadline = _FakeDeadline(120.0)

    update = await guards(_state_with(), _config(deadline=deadline))

    assert update == {}


# ---- 取消感知（穿透重抛——取消非故障 ADR-010）----


async def test_canceled_at_entry_raises_signal() -> None:
    """入口 pop 命中取消标志 → TurnCanceledSignal 穿透重抛（取消非故障；
    TurnRunner 捕获走 _finish_canceled 零 error 收尾）；零检查零写入。"""
    collector = _FakeCollector({"total_tokens": 600000})
    flags = {"t-1": "user"}

    with pytest.raises(TurnCanceledSignal) as exc_info:
        await guards(_state_with(), _config(collector=collector, cancel_flags=flags))

    assert exc_info.value.reason == "user"
    assert flags == {}  # pop 即感知+清理（与 TurnRunner _cancel_flags 同语义）
    assert collector.calls == 0  # 取消优先于一切检查


# ---- 契约面：常量 / state 键纪律 / 基元复用 ----


def test_envelope_constant_is_30s() -> None:
    """DEGRADED_WRITER_ENVELOPE_S=30.0（ARCH §4.2:514 degraded_writer 30s 包络——
    trip 阈值依据；代码常量 OQ-6 先例）。"""
    assert DEGRADED_WRITER_ENVELOPE_S == 30.0


async def test_writes_only_deepstate_declared_keys() -> None:
    """update 键 ⊆ DeepState.__annotations__（R14①）。"""
    from app.agent.state import DeepState

    collector = _FakeCollector({"total_tokens": 600000})

    update = await guards(_state_with(), _config(collector=collector))

    assert set(update) <= set(DeepState.__annotations__)
    assert set(update) <= {"usage_total", "degraded_reason"}


def test_reuses_guards_primitives_single_source_of_truth() -> None:
    """复用 agent/guards.py 既有基元（不另造轮子）：ensure_not_canceled 即
    guards.ensure_not_canceled 同一对象。"""
    from app.agent import guards as guards_module

    assert deep_guards.ensure_not_canceled is guards_module.ensure_not_canceled
