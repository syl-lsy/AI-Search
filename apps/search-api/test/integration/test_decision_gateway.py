"""integration：decision 薄客户端真网关活体冒烟（requires_llm——decision-model 别名经
LiteLLM 网关 typesafe provider 到百炼 Token Plan systemone 端点，R19 探针 PASS 形态）。

口径（Decision Model 工单批次 2——ADR-016 / nacos-config.md §1 v1.33）：
- `run_decision` 活体 200 → answers 四约定题键解析正确（query_type/search_strategy/
  answerable/safety_shadow），latency_ms/input_tokens 进 metadata（R19 探针实证：
  模型侧 P50 ≈40.9ms、5 连发逐字节一致——输出确定性）；
- kill switch 形态（decision=""）→ `resolve_decision_params` None（零调用零网络）；
- 失败分类冒烟：非法方言题面（box）→ 上游 400 → run_decision None（错误信封校验类
  ——不抛穿，rewrite 侧 light 兜底）。

执行纪律：网关/decision-model 别名不可达时 skip（记录到 skipped，不算失败）——
沿 test_rewrite_node.py 现行探活模式；判定质量属抽样实验（R19 §五），正式命中率
结论归 M4 评测线，本用例只做「端到端能不能解析」的活体判定。
"""

from dataclasses import replace
from typing import Any

import httpx
import pytest
from loguru import logger

pytestmark = pytest.mark.requires_llm

_DECISION_ALIAS = "decision-model"

#: 连接类失败桶（decision_client._classify_failure：connection/transient/timeout——
#: transport 瞬态；解析失败/400 = non_retryable 永不重试）
_CONNECTION_CLASSES: frozenset[str] = frozenset({"connection", "transient", "timeout"})


def _connection_class_failure(records: list[Any]) -> bool:
    """attempt 1 是否留下连接类失败留痕（decision_client._log_failure 格式：
    `decision 调用失败（{category}，单次尝试零重试，...）`）。"""
    for record in records:
        message = str(record.get("message", ""))
        for category in _CONNECTION_CLASSES:
            if f"decision 调用失败（{category}" in message:
                return True
    return False


def _decision_alias_ready() -> str | None:
    """网关/decision-model 别名探活；不可达原因字符串（None=就绪）。不打印任何凭据。"""
    from app.core.config import litellm_settings

    try:
        resp = httpx.get(
            str(litellm_settings.base_url).rstrip("/") + "/v1/models",
            headers={
                "Authorization": "Bearer " + litellm_settings.api_key.get_secret_value()
            },
            timeout=5,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 —— 探活失败统一 skip
        return f"LiteLLM 网关不可达（{type(exc).__name__}）"
    ids = {m.get("id") for m in resp.json().get("data", [])}
    if _DECISION_ALIAS not in ids:
        return "decision 别名未装载（decision-model 不在网关模型清单——批次 3 灰度窗就位）"
    return None


class _Budget:
    """TurnDeadline 假体（活体调用恒放行）。"""

    def can_attempt(self, estimated_cost_s: float) -> bool:
        return True


def _config() -> dict[str, Any]:
    return {"configurable": {"thread_id": "th-int", "turn_id": "int-decision-smoke"}}


async def test_run_decision_live_gateway_parses_answers() -> None:
    """真网关活体：decision-model 别名 200 → 四约定题键解析正确 + metadata 齐。"""
    skip_reason = _decision_alias_ready()
    if skip_reason:
        pytest.skip(skip_reason)

    from app.agent import decision_client
    from app.config_center.defaults import MODEL_MAPPING_CONFIG

    params = decision_client.resolve_decision_params(
        {**MODEL_MAPPING_CONFIG, "decision": _DECISION_ALIAS}
    )
    assert params is not None
    assert params.questions  # 内置题面四题键齐备

    # 活体调用：连接类失败单次重试（套件负载下端点瞬态 ReadTimeout 实证——重试次
    # 放宽单次超时；两次皆败仍判失败，不 skip、解析失败/400 类不重试——回归真信号
    # 不被藏掉。门禁裁决 2026-11-23：授权测试侧稳健化，与生产 P1a 连接类退避同哲学）
    records: list[Any] = []
    sink_id = logger.add(lambda message: records.append(message.record), level=0)
    try:
        outcome = await decision_client.run_decision(
            query="什么是向量数据库？为什么要用？主流方案有哪些？",
            history=[],
            params=params,
            budget=_Budget(),
            config=_config(),
        )
    finally:
        logger.remove(sink_id)
    if outcome is None and _connection_class_failure(records):
        from app.agent.decision_client import _TIMEOUT_MS_MAX

        outcome = await decision_client.run_decision(
            query="什么是向量数据库？为什么要用？主流方案有哪些？",
            history=[],
            params=replace(
                params,
                timeout_ms=min(params.timeout_ms * 4, _TIMEOUT_MS_MAX),
            ),
            budget=_Budget(),
            config=_config(),
        )
    assert outcome is not None, "活体 200 期望 DecisionOutcome（None=调用失败，检查网关日志）"
    # 四约定题键解析正确（判定值随模型/题面版本漂移——只断结构不断文案）
    assert outcome.query_type in (
        "factoid", "list", "comparison", "multi_facet",
        "temporal", "how_to", "navigational", "other",
    )
    assert outcome.search_strategy in ("direct", "recall", "verify", "web")
    assert isinstance(outcome.answerable, bool)
    assert outcome.safety_shadow in ("none", "danger", "profanity", "privacy", "politics", "other", None)
    # metadata 观测面（usage/latency——R19 探针实证 latency_ms 响应自带）
    assert outcome.metadata.get("latency_ms") is not None
    assert "answers" in outcome.metadata
    # 观测载荷（stage extra 源）逐字段齐备
    assert outcome.extra_observation["decision_source_fields"]["query_type"] in (
        "decision", "conservative",
    )


async def test_run_decision_box_dialect_rejected_returns_none() -> None:
    """方言硬校验的反向活体（R19 探针 §七）：box 题型上游 400 → run_decision None
    （失败语义不抛穿——rewrite 侧 light 兜底）。"""
    skip_reason = _decision_alias_ready()
    if skip_reason:
        pytest.skip(skip_reason)

    from app.agent import decision_client
    from app.agent.decision_client import DecisionParams
    from app.config_center.defaults import MODEL_MAPPING_CONFIG

    params = DecisionParams(
        alias=_DECISION_ALIAS,
        questions={"g": {"type": "box", "criteria": {}}},  # litellm box 方言——上游 400 拒收
        confidence_floor=0.5,
        timeout_ms=2000,
        shadow=False,
    )
    assert params is not None
    resolved = decision_client.resolve_decision_params(
        {**MODEL_MAPPING_CONFIG, "decision": _DECISION_ALIAS}
    )
    assert resolved is not None  # kill switch 开启形态先自证
    outcome = await decision_client.run_decision(
        query="q",
        history=[],
        params=params,
        budget=_Budget(),
        config=_config(),
    )
    assert outcome is None


def test_kill_switch_offline_no_params() -> None:
    """kill switch 关闭态（零网络可跑）：decision="" → params None。"""
    from app.agent import decision_client

    assert decision_client.resolve_decision_params({"decision": ""}) is None
    assert decision_client.resolve_decision_params({"light": "qwen3.5-4b-ollama"}) is None
