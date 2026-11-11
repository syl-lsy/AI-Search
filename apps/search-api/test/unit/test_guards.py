"""unit 层：guards 图级可靠性基元（B3 第 0 步 API 与参数解析——ARCH §3.1/§6.2）。

钉住的语义（冻结契约，逐条对应测试）：
- RetryPolicy 参数从轮内 spec 快照 `spec.<mode>.retry` 读（nacos-config.md §3 v1.3
  退避写法：initial_interval_s × backoff_factor + jitter）；节缺失/字段非法逐项回落
  内置默认（guard 不阻塞主链路）；图级 RetryPolicy 是唯一重试所有者（网关侧
  num_retries=0 去重，§6.2 P0）——分类重试判据代码层固化：ValueError/TypeError
  等确定性失败不重试、httpx 仅 5xx 触发重试；
- 分级超时：LLM 单调用 / 工具两级（spec.<mode>.llm_timeout_s / tool_timeout_s；
  契约缺省 fast 30/10、deep 60/30，§6.2 容错清单）；
- 取消感知：与 TurnRunner _cancel_flags 同语义（dict[turn_id, reason]），节点边界
  pop 即感知+清理；无注入面（Studio 直跑）静默 None——节点不依赖 TurnRunner 设施；
- deadline-aware 预算（§6.2 P0）：轮级 turn_timeout_s 包络计剩余预算，重试尝试前
  不足以覆盖预计单次成本即放弃、提前走降级路径。

跨子图透传实测归批次 5（R14②），本批只留单测证据。
"""

import operator
from typing import Any

import httpx

from app.agent.guards import (
    RetryPolicy,
    TurnDeadline,
    default_retry_on,
    llm_timeout_s,
    pop_cancel_flag,
    retry_policy_from_spec,
    tool_timeout_s,
)


def test_retry_policy_from_spec_reads_mode_section() -> None:
    """spec.<mode>.retry → RetryPolicy：读对应模式节、忽略其他模式。"""
    spec: dict[str, Any] = {
        "fast": {
            "retry": {
                "max_attempts": 2,
                "initial_interval_s": 0.5,
                "backoff_factor": 3.0,
                "jitter": False,
            }
        },
        "deep": {"retry": {"max_attempts": 5, "initial_interval_s": 2.0}},
    }
    policy = retry_policy_from_spec(spec, "fast")
    assert policy == RetryPolicy(
        max_attempts=2, initial_interval_s=0.5, backoff_factor=3.0, jitter=False
    )
    deep = retry_policy_from_spec(spec, "deep")
    assert deep.max_attempts == 5 and deep.initial_interval_s == 2.0
    assert deep.backoff_factor == 2.0 and deep.jitter is True  # 未给字段回落默认


def test_retry_policy_falls_back_on_missing_or_invalid() -> None:
    """节缺失/结构非法/数值越界（max_attempts<1、initial≤0、factor<1，§3 校验口径）
    → 内置默认（guard 不阻塞主链路；Nacos validator 拒收之外的路由面防线）。"""
    default = RetryPolicy()
    assert retry_policy_from_spec({}, "fast") == default
    assert retry_policy_from_spec({"fast": {}}, "fast") == default
    assert retry_policy_from_spec({"fast": {"retry": "oops"}}, "fast") == default
    assert (
        retry_policy_from_spec({"fast": {"retry": {"max_attempts": 0}}}, "fast")
        == default
    )
    assert (
        retry_policy_from_spec(
            {"fast": {"retry": {"initial_interval_s": 0, "backoff_factor": 0.5}}}, "fast"
        )
        == default
    )


def test_default_retry_on_classification() -> None:
    """分类重试（§6.2 代码层固化）：确定性失败不重试；httpx 仅 5xx；其余默认重试。"""
    assert default_retry_on(ValueError("bad value")) is False
    assert default_retry_on(TypeError("bad type")) is False
    request = httpx.Request("GET", "https://example.com")
    response_500 = httpx.Response(500, request=request)
    error_500 = httpx.HTTPStatusError("boom", request=request, response=response_500)
    assert default_retry_on(error_500) is True
    response_404 = httpx.Response(404, request=request)
    error_404 = httpx.HTTPStatusError("missing", request=request, response=response_404)
    assert default_retry_on(error_404) is False
    assert default_retry_on(httpx.ConnectError("refused")) is True
    assert default_retry_on(httpx.ReadTimeout("slow")) is True
    assert default_retry_on(RuntimeError("transient")) is True


def test_llm_and_tool_timeouts_from_spec_with_contract_defaults() -> None:
    """分级超时（§6.2）：读 spec.<mode>，缺省回落契约值——fast 30/10、deep 60/30。"""
    spec: dict[str, Any] = {
        "fast": {"llm_timeout_s": 15, "tool_timeout_s": 5},
        "deep": {"llm_timeout_s": 90},
    }
    assert llm_timeout_s(spec, "fast") == 15.0
    assert tool_timeout_s(spec, "fast") == 5.0
    assert llm_timeout_s(spec, "deep") == 90.0
    assert tool_timeout_s(spec, "deep") == 30.0  # deep 未给 tool_timeout_s → 契约缺省
    assert llm_timeout_s({}, "fast") == 30.0  # 整节缺失 → 契约缺省
    assert tool_timeout_s({}, "deep") == 30.0
    assert llm_timeout_s({"fast": {"llm_timeout_s": "oops"}}, "fast") == 30.0  # 非法回落


def test_pop_cancel_flag_same_semantics_as_turn_runner() -> None:
    """取消感知与 TurnRunner _cancel_flags 同语义：pop 即感知+清理；无注入面静默 None。"""
    flags: dict[str, str] = {"turn-1": "superseded"}
    config = {"configurable": {"turn_id": "turn-1", "cancel_flags": flags}}

    assert pop_cancel_flag(config) == "superseded"
    assert flags == {}  # pop 即清理（与 TurnRunner 事件边界 pop 口径一致）
    assert pop_cancel_flag(config) is None  # 第二次感知不到（已消费）
    # 非本人轮 / 无 flags 注入 / 无 config（Studio 直跑）：一律 None，节点不被阻塞
    assert pop_cancel_flag(
        {"configurable": {"turn_id": "turn-2", "cancel_flags": {"turn-9": "user"}}}
    ) is None
    assert pop_cancel_flag({"configurable": {"turn_id": "turn-1"}}) is None
    assert pop_cancel_flag(None) is None


def test_turn_deadline_budget_gates_retry_attempts() -> None:
    """deadline-aware 预算（§6.2 P0）：剩余预算 ≥ 预计单次成本才允许再试；耗尽后下限 0。"""
    now = 100.0

    def clock() -> float:
        return now

    deadline = TurnDeadline(60.0, clock=clock)
    assert deadline.remaining_s() == 60.0

    now = 150.0  # 已耗 50s
    assert deadline.remaining_s() == 10.0
    assert deadline.can_attempt(9.0) is True  # 预算足：允许再试
    assert deadline.can_attempt(11.0) is False  # 不足：放弃重试、提前走降级路径

    now = 200.0  # 超时后：剩余下限 0
    assert deadline.remaining_s() == 0.0
    assert deadline.can_attempt(0.0) is True  # 零成本尝试不受阻（降级收尾路径可用）
