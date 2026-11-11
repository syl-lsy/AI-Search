"""guards：图级可靠性基元（ARCH §3.1/§6.2）——预算·硬超时·取消三件套 + deadline-aware。

B3 第 0 步落 API 与参数解析（单测锁死 test/unit/test_guards.py）；节点接线随 B3 各
节点逐个闭环（rewrite 起）；跨子图透传实测归批次 5（R14②——guards 取消标志为进程
级 dict 引用、理论上不受子图边界影响，届时实测确认）。

四件职责（§6.2 容错清单）：
- **取消感知**：TurnRunner 把进程级 `_cancel_flags`（dict[turn_id, reason]）引用经
  RunnableConfig `configurable` 注入图运行，节点边界调 `pop_cancel_flag` 感知——与
  TurnRunner 事件边界同语义、同「pop 即感知+清理」口径；打断/取消非故障，canceled
  收尾归 TurnRunner（图内只感知、不落库）。
- **RetryPolicy**：参数从轮内 spec 快照 `spec.<mode>.retry` 读（OQ-11：随 state 进图，
  热更仅对后续轮生效；nacos-config.md §3 v1.3 退避写法）。图级 RetryPolicy 是唯一
  重试所有者（网关侧 num_retries=0 去重，§6.2 P0）；分类重试判据 `default_retry_on`
  代码层固化：ValueError/TypeError 等确定性失败不重试、httpx 仅 5xx 触发重试。
- **分级超时**：LLM 单调用 / 工具两级（`spec.<mode>.llm_timeout_s` /
  `tool_timeout_s`；契约缺省 fast 30s/10s、deep 60s/30s——§6.2；firecrawl 30s 归
  deep.tool_timeout_s 契约值）。
- **deadline-aware 预算**（§6.2 P0）：`TurnDeadline` 以轮级 turn_timeout_s 包络计
  剩余预算，重试尝试前 `can_attempt(预计单次成本)` 不足即放弃重试、提前走降级路径
  （动机：rewrite 3 次 × 30s ≈ 95s 超 fast 60s 兜底）。

guard 是图级基元：解析失败一律回落内置默认（不抛、不阻塞主链路）——Nacos validator
拒收非法配置是第一道防线，本模块是图运行面的第二道。
"""

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

__all__ = [
    "RetryPolicy",
    "TurnDeadline",
    "default_retry_on",
    "llm_timeout_s",
    "pop_cancel_flag",
    "retry_policy_from_spec",
    "tool_timeout_s",
]


@dataclass(frozen=True)
class RetryPolicy:
    """图级重试策略（§6.2：图级 RetryPolicy 是唯一重试所有者）。

    默认值 = nacos-config.md §3 retry 节契约初值（max_attempts=3、
    initial_interval_s=1、backoff_factor=2.0、jitter=True——退避序列等效 1s/2s/4s）。
    """

    max_attempts: int = 3
    initial_interval_s: float = 1.0
    backoff_factor: float = 2.0
    jitter: bool = True


_DEFAULT_RETRY = RetryPolicy()

#: 分级超时契约缺省（§6.2 容错清单；spec 缺值时的回落口径）
_DEFAULT_LLM_TIMEOUT_S: dict[str, float] = {"fast": 30.0, "deep": 60.0}
_DEFAULT_TOOL_TIMEOUT_S: dict[str, float] = {"fast": 10.0, "deep": 30.0}


def _mode_section(spec: Mapping[str, Any] | None, mode: str) -> Mapping[str, Any]:
    """取 spec.<mode> 节（结构非法视同缺失——guard 不抛）。"""
    if not isinstance(spec, Mapping):
        return {}
    section = spec.get(mode)
    return section if isinstance(section, Mapping) else {}


def retry_policy_from_spec(spec: Mapping[str, Any] | None, mode: str) -> RetryPolicy:
    """spec.<mode>.retry → RetryPolicy；节缺失/字段非法/数值越界回落内置默认。

    越界口径同 nacos-config.md §3 校验（max_attempts ≥ 1、initial_interval_s > 0、
    backoff_factor ≥ 1）——validator 拒收之外的路由面第二道防线。
    """
    retry = _mode_section(spec, mode).get("retry")
    if not isinstance(retry, Mapping):
        return _DEFAULT_RETRY
    try:
        max_attempts = int(retry.get("max_attempts", _DEFAULT_RETRY.max_attempts))
        initial_interval_s = float(
            retry.get("initial_interval_s", _DEFAULT_RETRY.initial_interval_s)
        )
        backoff_factor = float(
            retry.get("backoff_factor", _DEFAULT_RETRY.backoff_factor)
        )
        jitter = bool(retry.get("jitter", _DEFAULT_RETRY.jitter))
    except (TypeError, ValueError):
        return _DEFAULT_RETRY
    if max_attempts < 1 or initial_interval_s <= 0 or backoff_factor < 1:
        return _DEFAULT_RETRY
    return RetryPolicy(
        max_attempts=max_attempts,
        initial_interval_s=initial_interval_s,
        backoff_factor=backoff_factor,
        jitter=jitter,
    )


def llm_timeout_s(spec: Mapping[str, Any] | None, mode: str) -> float:
    """LLM 单调用超时（秒）：spec.<mode>.llm_timeout_s；缺省按模式回落契约值。"""
    raw = _mode_section(spec, mode).get("llm_timeout_s")
    try:
        value = float(raw)  # type: ignore[arg-type] —— raw 为 None/非数时走 except
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    return _DEFAULT_LLM_TIMEOUT_S.get(mode, _DEFAULT_LLM_TIMEOUT_S["fast"])


def tool_timeout_s(spec: Mapping[str, Any] | None, mode: str) -> float:
    """工具调用超时（秒）：spec.<mode>.tool_timeout_s；缺省按模式回落契约值。"""
    raw = _mode_section(spec, mode).get("tool_timeout_s")
    try:
        value = float(raw)  # type: ignore[arg-type]
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    return _DEFAULT_TOOL_TIMEOUT_S.get(mode, _DEFAULT_TOOL_TIMEOUT_S["fast"])


def default_retry_on(exc: Exception) -> bool:
    """分类重试判据（§6.2 v1.3 代码层固化，LangGraph RetryPolicy retry_on 用）。

    - ValueError/TypeError：确定性失败（校验/编程错误）——重试同样失败，不重试；
    - httpx.HTTPStatusError：仅 5xx 触发重试（4xx 重试无意义）；
    - 其余 httpx 网络故障（连接/超时）与其余异常：默认重试（由 max_attempts 封顶）。
    """
    if isinstance(exc, (ValueError, TypeError)):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return True


def pop_cancel_flag(config: Mapping[str, Any] | None) -> str | None:
    """节点边界感知协作式取消（与 TurnRunner `_cancel_flags` 同语义）：pop 即感知+清理。

    注入面：TurnRunner astream 的 `configurable["cancel_flags"]`（进程级 dict 引用）+
    `configurable["turn_id"]`。无注入面（Studio 直跑/单测/无 flags）时静默返回 None
    ——节点不依赖 TurnRunner 运行时设施（DEV §5.3 边界）；跨子图透传实测归批次 5
    （R14②）。取消收尾（canceled 落库 + done 事件）归 TurnRunner，图内只感知不落库。
    """
    if not isinstance(config, Mapping):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, Mapping):
        return None
    flags = configurable.get("cancel_flags")
    turn_id = configurable.get("turn_id")
    if not isinstance(flags, dict) or not turn_id:
        return None
    return flags.pop(str(turn_id), None)


class TurnDeadline:
    """轮级 deadline（turn_timeout_s 包络）——deadline-aware 重试预算（§6.2 P0）。

    每次重试**尝试前**查剩余预算：`can_attempt(预计单次成本)` 不足即放弃重试、
    提前走降级路径（动机：rewrite 3 次 × 30s ≈ 95s 超 fast 60s 兜底）。clock 可注入
    （单测确定性）；剩余预算下限 0（超时后 zero-cost 尝试仍放行，供降级收尾路径）。
    """

    def __init__(
        self, timeout_s: float, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._clock = clock
        self._deadline = clock() + timeout_s

    def remaining_s(self) -> float:
        """剩余轮级预算（秒，下限 0）。"""
        return max(0.0, self._deadline - self._clock())

    def can_attempt(self, estimated_cost_s: float) -> bool:
        """剩余预算 ≥ 预计单次成本才允许再试（不足即放弃重试、提前走降级路径）。"""
        return self.remaining_s() >= estimated_cost_s
