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
- **错误三档分级**（B11 P1a，ARCH §6.2 v1.49）：`classify_retry_tier` 产出
  connection（连接类→`spec.<mode>.retry.connection` 长窗）/ transient（短窗=现状）/
  non_retryable（业务类不重试）；`ConnectionRetryPolicy` + `RetryWindow` 承载长窗
  参数与总窗硬闸（双闸：TurnDeadline + total_window_s，先到先放弃）。

guard 是图级基元：解析失败一律回落内置默认（不抛、不阻塞主链路）——Nacos validator
拒收非法配置是第一道防线，本模块是图运行面的第二道。
"""

import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import httpx
from langchain_core.exceptions import OutputParserException

__all__ = [
    "ConnectionRetryPolicy",
    "RetryPolicy",
    "RetryWindow",
    "TurnCanceledSignal",
    "TurnDeadline",
    "classify_retry_tier",
    "connection_retry_policy_from_spec",
    "default_retry_on",
    "ensure_not_canceled",
    "is_connection_failure",
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


@dataclass(frozen=True)
class ConnectionRetryPolicy:
    """**连接类**错误长窗重试策略（spec.<mode>.retry.connection，nacos-config.md §3 v1.24）。

    五键语义（P1a 网络瞬断韧性，synthesis 节点专属消费；rewrite 维持主节短窗）：
    - `max_attempts`：首试 + 重试总次数（默认 5 = 首试 + 4 重试）；
    - `initial_interval_s` / `backoff_factor`：退避序列 initial×factor^(n-1)（1/2/4/8）；
    - `max_interval_s`：单次退避上限（cap，默认 8.0）；
    - `total_window_s`：**重试总窗硬闸**——退避 sleeps + 重试尝试耗时 ≤ 该值，先到先
      放弃（与 `TurnDeadline` 双闸；窗闸放弃 = 既有 SynthesisError→degraded 语义）。
    """

    max_attempts: int = 5
    initial_interval_s: float = 1.0
    backoff_factor: float = 2.0
    max_interval_s: float = 8.0
    total_window_s: float = 20.0

    def delay_for(self, retry_index: int) -> float:
        """第 retry_index 次重试（1-based）的退避秒数：initial×factor^(n-1)，cap max。"""
        exponent = max(0, int(retry_index) - 1)
        delay = self.initial_interval_s * (self.backoff_factor**exponent)
        return min(delay, self.max_interval_s)


#: 连接类长窗五键（nacos-config.md §3 v1.24；缺一即整节回落主节）
_CONNECTION_POLICY_KEYS: tuple[str, ...] = (
    "max_attempts",
    "initial_interval_s",
    "backoff_factor",
    "max_interval_s",
    "total_window_s",
)


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


def connection_retry_policy_from_spec(
    spec: Mapping[str, Any] | None, mode: str
) -> ConnectionRetryPolicy | None:
    """spec.<mode>.retry.connection → ConnectionRetryPolicy；不可用返回 None。

    **灰度安全口径（nacos-config.md §3 v1.24）**：节缺失 / 非对象 / 任一键非法 →
    返回 None = 调用方回落既有 `retry` 主节短窗（=现状行为，旧配置 + 新代码零行为
    变化；整节回落而非逐键回落——「任一键非法回落主节」的读法单一，避免半新半旧
    的混合策略）。不在本层拒收配置：validator 侧同样只告警不拒收。
    """
    retry = _mode_section(spec, mode).get("retry")
    if not isinstance(retry, Mapping):
        return None
    node = retry.get("connection")
    if not isinstance(node, Mapping):
        return None
    if any(key not in node for key in _CONNECTION_POLICY_KEYS):
        return None  # 五键缺一即整节不可用（契约「任一键非法/缺失回落」单一读法）
    try:
        max_attempts = int(node["max_attempts"])
        initial_interval_s = float(node["initial_interval_s"])
        backoff_factor = float(node["backoff_factor"])
        max_interval_s = float(node["max_interval_s"])
        total_window_s = float(node["total_window_s"])
    except (TypeError, ValueError):
        return None
    if (
        max_attempts < 1
        or initial_interval_s <= 0
        or backoff_factor < 1
        or max_interval_s <= 0
        or total_window_s <= 0
    ):
        return None
    return ConnectionRetryPolicy(
        max_attempts=max_attempts,
        initial_interval_s=initial_interval_s,
        backoff_factor=backoff_factor,
        max_interval_s=max_interval_s,
        total_window_s=total_window_s,
    )


class RetryWindow:
    """重试总窗（`total_window_s` 硬闸；clock 可注入——单测确定性）。

    语义（决策 4）：退避 sleeps + 重试尝试耗时 ≤ total_window_s，「先到先放弃」——
    每次 sleep 前 `can_wait(delay)` 预检、sleep 后 `remaining_s()` 复查（真实耗时
    自然把尝试耗时计入窗口）；窗闸放弃 = 既有 SynthesisError→degraded 收束。
    """

    def __init__(
        self, window_s: float, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._clock = clock
        self._deadline = clock() + max(0.0, float(window_s))

    def remaining_s(self) -> float:
        """窗口剩余秒数（下限 0）。"""
        return max(0.0, self._deadline - self._clock())

    def can_wait(self, delay_s: float) -> bool:
        """本次退避仍落在窗内才继续（不足即放弃重试）。"""
        return self.remaining_s() >= max(0.0, float(delay_s))


# ---- 错误三档分级（决策 1：连接类长窗 / 短窗可重试 / 业务类不重试）----


@lru_cache(maxsize=1)
def _connection_exception_types() -> tuple[type[BaseException], ...]:
    """连接类异常类型集合（类型判定，零网络；openai 形态为 langchain-openai 依赖）。

    httpx.TransportError 基类覆盖契约列出的 ConnectError/ConnectTimeout/ReadError/
    WriteError/PoolTimeout/ReadTimeout（均为其子类）；`asyncio.TimeoutError` 即内置
    `TimeoutError`（单调用包络超时）、`ConnectionError` 覆盖原生 socket 断连。
    openai.APIConnectionError/APITimeoutError 为 langchain-openai 抛出形态——延迟
    导入失败时仅按名称兜底（`_CONNECTION_TYPE_NAMES`），不阻塞其余判据。
    """
    types: list[type[BaseException]] = [
        httpx.TransportError,
        # Python ≥3.11 起 asyncio.TimeoutError 即内置 TimeoutError（同一对象）——
        # 只留内置名，避免重复项
        TimeoutError,
        ConnectionError,
    ]
    try:
        import openai

        types.extend([openai.APIConnectionError, openai.APITimeoutError])
    except Exception:  # noqa: BLE001 —— 可选依赖缺失：名称兜底已覆盖
        pass
    return tuple(types)


#: 名称兜底（MCP/anyio transport 断连与 openai 包装形态；决策 1/决策 7 口径）
_CONNECTION_TYPE_NAMES: frozenset[str] = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "ClosedResourceError",
        "BrokenResourceError",
        "EndOfStream",
        "RemoteProtocolError",
    }
)


def is_connection_failure(exc: BaseException) -> bool:
    """连接类/超时类失败判定（P1a/P1b 共用；类型集合判定 + ExceptionGroup 解包）。

    ExceptionGroup（anyio/MCP 包装形态）逐叶判定：任一叶为连接类即整体视为连接类。
    """
    for leaf in _iter_exception_leaves(exc):
        if isinstance(leaf, _connection_exception_types()):
            return True
        if any(cls.__name__ in _CONNECTION_TYPE_NAMES for cls in type(leaf).__mro__):
            return True
    return False


def _iter_exception_leaves(exc: BaseException) -> Iterator[BaseException]:
    """递归解包 ExceptionGroup（anyio/MCP 包装）→ 叶异常迭代器。"""
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            yield from _iter_exception_leaves(sub)
    else:
        yield exc


def classify_retry_tier(exc: BaseException) -> str:
    """LLM 错误三档分级（决策 1；返回值 ∈ connection | transient | non_retryable）。

    - `connection`（长窗）：httpx transport 族 / 单调用包络超时 / openai 连接形态 /
      ExceptionGroup 内含上述任一；
    - `transient`（短窗，维持现状）：httpx 5xx 与既有 `default_retry_on` 放行的其余异常；
    - `non_retryable`（业务类）：ValueError/TypeError（含 pydantic ValidationError）、
      httpx 4xx、OutputParserException（结构化输出解析失败——rewrite 即时上抛先例）。
    """
    leaves = list(_iter_exception_leaves(exc))
    if any(is_connection_failure(leaf) for leaf in leaves):
        return "connection"
    for leaf in leaves:
        if isinstance(leaf, OutputParserException):
            return "non_retryable"
        if not default_retry_on(_as_leaf(leaf)):
            return "non_retryable"
    return "transient"


def _as_leaf(exc: BaseException) -> Exception:
    """`default_retry_on` 签名面收窄（叶异常一般即 Exception；非 Exception 视同不重试）。"""
    return exc if isinstance(exc, Exception) else _NonRetryableCarrier(exc)


class _NonRetryableCarrier(ValueError):
    """非 Exception 基类叶（BaseException 子类）的载体：判定即 non_retryable。"""

    def __init__(self, original: BaseException) -> None:
        super().__init__(type(original).__name__)


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
    ——节点不依赖 TurnRunner 运行时设施（DEV §5.3 边界）；跨子图透传实测批次 5 已闭
    （R14②：test_r14_cross_subgraph 锁死注入身份与节点感知）。取消收尾（canceled
    落库 + done 事件）归 TurnRunner，图内只感知不落库。
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


class TurnCanceledSignal(Exception):
    """节点边界感知到协作式取消的控制流信号（**取消非故障**，R14② 跨子图透传形态）。

    guards `pop_cancel_flag` 在节点入口命中（进程级 `_cancel_flags` 经 configurable
    引用注入，跨主图壳→子图边界可达）即上抛本信号——图内只感知、不落库，TurnRunner
    捕获后走 `_finish_canceled`（done(finish=canceled, cancel_reason=reason)，
    不产生 error 事件，api.md §7.2「打断非故障」口径）。reason ∈ user|superseded|
    rolled_back（ThreadScheduler._cancel_flags 值域）。
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def ensure_not_canceled(config: Mapping[str, Any] | None) -> None:
    """节点入口边界感知：pop 命中即抛 TurnCanceledSignal（未命中零开销直通）。

    fast 五节点入口统一接线（R14②）：无注入面（Studio 直跑/单测/无 flags）静默
    返回——节点不依赖 TurnRunner 运行时设施（DEV §5.3 边界不变）。
    """
    reason = pop_cancel_flag(config)
    if reason is not None:
        raise TurnCanceledSignal(reason)


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
