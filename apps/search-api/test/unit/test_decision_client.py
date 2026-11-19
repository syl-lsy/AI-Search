"""unit 层：decision_client 薄客户端（Decision Model 工单批次 2——ADR-016 /
nacos-config.md §1 v1.33 `fast.decision.*` / §5 `model.mapping.decision`）。

钉住的语义（冻结契约，逐条对应测试）：
- **kill switch**：`model.mapping.decision` 空/缺失 → `resolve_decision_params` 返回
  None（decision 整体跳过，= 现状行为，存量轮零变化沿 search_strategy v1.20 先例）；
- **参数解析**：questions/confidence_floor/timeout_ms/shadow 从 prompts 快照标量键读，
  缺失/非法回落代码内置默认（0.5 / 1000 / False，= 契约建议初值）并告警；
- **questions_json 载入**：快照 JSON 解析 → 快照值非法回落内置默认（built-in 文件）；
- **输入装配**：state 文本 = query 全文 + recall 判定语境的近轮历史摘编（ARCH §6.5
  v1.62 决策步骤输入构成注——装配变量集单测锁死）；
- **响应解析**：answers 按四约定题键取值——choice 题 `choice` 值 + `confidence`
  （独立校准量，≠max(probabilities)，R19 探针实证）；noul 题 `noul` 概率即答案
  （≥0.5 → True）；safety_shadow 仅观测；
- **阈值闸（ask 口径：该字段回落保守缺省）**：choice confidence <
  `fast.decision.confidence_floor` → query_type 回落 other / search_strategy 回落 web
  （R17 第三重压缩「拿不准一律 web」数值化）；枚举外值同保守回落；
- **失败语义（对齐 guards 分类，单次尝试零重试）**：超时/连接类/HTTP 4xx/解析失败 →
  None（rewrite 侧走 light 全职责兜底，不置 degraded）；deadline-aware：轮级剩余预算
  < decision 超时 → 放弃调用直接 None（沿 rewrite 现口径）；
- **Langfuse 独立 generation**：未配置（单测缺省）零开销 no-op；配置后 generation
  model=decision 别名、usage/latency/confidence/probabilities 落 metadata（SDK 异常
  告警不阻塞主链路——观测不阻塞口径）。

单测全 mock 零网络：httpx 调用经 `_post_systemone` 注入面打桩。
"""

from typing import Any

import httpx
import pytest

from app.agent import decision_client
from app.agent.decision_client import (
    DEFAULT_CONFIDENCE_FLOOR,
    DEFAULT_TIMEOUT_MS,
    DecisionOutcome,
    build_state_text,
    decision_alias,
    load_questions,
    parse_answers,
    resolve_decision_params,
)

# ---- kill switch（model.mapping.decision 空/缺失 = 关闭）----


def test_kill_switch_empty_string_disables() -> None:
    """decision=""（缺省 kill switch）→ None（decision 不启用）。"""
    assert decision_alias({"decision": ""}) == ""
    assert resolve_decision_params({"decision": ""}) is None


def test_kill_switch_missing_key_disables() -> None:
    """旧映射 payload（无 decision 键）+ 新代码 → None（存量轮零行为变化回归锁）。"""
    legacy = {"light": "qwen3.5-4b-ollama", "memory": "chat-speed"}
    assert decision_alias(legacy) == ""
    assert resolve_decision_params(legacy) is None


def test_kill_switch_none_mapping_disables() -> None:
    """mapping 非 Mapping（Studio 直跑形态）→ None。"""
    assert decision_alias(None) == ""
    assert resolve_decision_params(None) is None


def test_enabled_alias_resolves_params() -> None:
    """非空别名 → DecisionParams（questions/confidence_floor/timeout_ms/shadow 齐）。"""
    params = resolve_decision_params({"decision": "decision-model"})
    assert params is not None
    assert params.alias == "decision-model"
    assert params.questions  # 内置 questions_json 可解析（四题键）
    assert params.confidence_floor == pytest.approx(DEFAULT_CONFIDENCE_FLOOR)
    assert params.timeout_ms == DEFAULT_TIMEOUT_MS
    assert params.shadow is False


# ---- questions_json 载入（快照 → 内置回落）----


def _questions_snapshot(monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    """prompts 快照打桩：prompts 节可含 fast.decision.questions_json（JSON 字符串）。"""
    prompts: dict[str, Any] = {}
    if payload is not None:
        prompts["fast.decision.questions_json"] = payload

    class _FakeSnapshot:
        payload = {"prompts": prompts}

    class _FakeCenter:
        def snapshot(self, data_id: str) -> _FakeSnapshot:
            return _FakeSnapshot()

    monkeypatch.setattr(decision_client, "get_asset_center", lambda: _FakeCenter())


def test_load_questions_from_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """快照值合法 JSON → 解析为 questions dict（Nacos 热更题面生效）。"""
    import json

    _questions_snapshot(monkeypatch, json.dumps({"q1": {"type": "noul"}}))
    assert load_questions() == {"q1": {"type": "noul"}}


def test_load_questions_falls_back_to_builtin_on_bad_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """快照值非法 JSON → 回落内置默认（契约灰度安全：键缺失/非法回落代码内置默认）。"""
    _questions_snapshot(monkeypatch, "not-json{{{")
    questions = load_questions()
    assert questions == load_questions_without_snapshot()
    assert "query_type" in questions and "search_strategy" in questions


def load_questions_without_snapshot() -> dict[str, Any]:
    """资产中心未启动形态的内置题面（与 load_questions 回落源同一份）。"""
    return decision_client._builtin_questions()


def test_load_questions_falls_back_when_center_unavailable() -> None:
    """资产中心未启动（单测/Studio）→ 内置默认（defaults.py questions_json）。"""
    assert "query_type" in load_questions()
    assert "search_strategy" in load_questions()
    assert "answerable" in load_questions()
    assert "safety_shadow" in load_questions()


def test_builtin_questions_carry_dialect_contract() -> None:
    """内置题面方言契约（nacos-config §1 v1.33 / R19 探针硬约束）：
    四约定题键齐备；type ∈ {noul, choice, score}；choice.criteria=对象；
    other 兜底仅 query_type（v1.33 原文形态——判据回滚 v0 后 strategy/safety_shadow
    选项=契约原值，策略拿不准回落 web 在代码侧）；
    noul 无 criteria 平铺 instructions。"""
    questions = load_questions()
    assert set(questions) >= {"query_type", "search_strategy", "answerable", "safety_shadow"}
    for key in ("query_type", "search_strategy", "safety_shadow"):
        question = questions[key]
        assert question["type"] == "choice"
        assert isinstance(question["criteria"], dict)  # choice criteria 必须是对象
    assert "other" in questions["query_type"]["options"]  # other 兜底仅 query_type（百炼官方建议）
    assert set(questions["search_strategy"]["options"]) == {"direct", "recall", "verify", "web"}
    assert set(questions["safety_shadow"]["options"]) == {
        "none", "danger", "profanity", "privacy", "politics",
    }
    assert questions["answerable"]["type"] == "noul"
    assert isinstance(questions["answerable"]["instructions"], str)


# ---- 输入装配（变量集单测锁死，ARCH §6.5 v1.62）----


def test_state_text_query_only_when_no_history() -> None:
    """空历史 → 仅 query 全文（回顾类护栏在 decision 判据与代码两侧共同兜）。"""
    text = build_state_text("你好，能做什么？", [])
    assert "你好，能做什么？" in text
    assert "历史" not in text


def test_state_text_appends_recent_history_block() -> None:
    """非空历史 → query 全文 + 近轮 Q/A 摘编块（recall 判定语境）。"""
    history = [
        {"q": "什么是向量数据库", "a_head": "向量数据库是…"},
        {"q": "pgvector 是什么", "a_head": "pgvector 是…"},
    ]
    text = build_state_text("鸡心倒吊是什么", history)
    assert "鸡心倒吊是什么" in text
    assert "Q: 什么是向量数据库" in text
    assert "A: 向量数据库是…" in text
    assert "Q: pgvector 是什么" in text


def test_state_text_ignores_malformed_history() -> None:
    """history 结构非法（非 list / 条目非 Mapping）→ 零贡献不抛（装配防御）。"""
    text = build_state_text("q", "not-a-list")  # type: ignore[arg-type]
    assert "q" in text


# ---- 响应解析（answers → 四字段 + 阈值闸）----


def _choice(value: str, confidence: float, probabilities: dict[str, float]) -> dict[str, Any]:
    return {"type": "choice", "choice": value, "confidence": confidence, "probabilities": probabilities}


def test_parse_answers_full_effective() -> None:
    """全题有效且 confidence ≥ floor → decision 判定生效（source=decision）。"""
    answers = {
        "query_type": _choice("comparison", 0.9, {"comparison": 0.9}),
        "search_strategy": _choice("verify", 0.8, {"verify": 0.8}),
        "answerable": {"type": "noul", "noul": 0.95},
        "safety_shadow": _choice("none", 0.97, {"none": 0.97}),
    }
    outcome = parse_answers(answers, confidence_floor=0.5)
    assert isinstance(outcome, DecisionOutcome)
    assert outcome.query_type == "comparison"
    assert outcome.search_strategy == "verify"
    assert outcome.answerable is True
    assert outcome.answerable_source == "decision"  # verify 判定与模型答案一致——非护栏介入
    assert outcome.safety_shadow == "none"
    assert outcome.extra_observation["decision_source_fields"] == {
        "query_type": "decision",
        "search_strategy": "decision",
        "answerable": "decision",
    }


def test_parse_answers_low_confidence_search_strategy_falls_back_web() -> None:
    """confidence < floor → search_strategy 回落 web（R17 数值化——该字段保守缺省）。"""
    answers = {
        "query_type": _choice("factoid", 0.9, {"factoid": 0.9}),
        "search_strategy": _choice("direct", 0.34, {"direct": 0.5}),
        "answerable": {"type": "noul", "noul": 0.9},
    }
    outcome = parse_answers(answers, confidence_floor=0.5)
    assert outcome.search_strategy == "web"
    assert outcome.extra_observation["decision_source_fields"]["search_strategy"] == "conservative"
    assert outcome.query_type == "factoid"  # 其字段不受影响（逐字段闸）


def test_parse_answers_low_confidence_query_type_falls_back_other() -> None:
    """query_type confidence < floor → 回落保守缺省 other（该字段保守缺省）。"""
    answers = {
        "query_type": _choice("factoid", 0.2, {"factoid": 0.2}),
        "search_strategy": _choice("web", 0.9, {"web": 0.9}),
        "answerable": {"type": "noul", "noul": 0.1},
    }
    outcome = parse_answers(answers, confidence_floor=0.5)
    assert outcome.query_type == "other"
    assert outcome.search_strategy == "web"
    assert outcome.answerable is False  # noul 0.1 < 0.5 → False（保守）


def test_parse_answers_illegal_enum_value_falls_back_conservative() -> None:
    """choice 枚举外值 → 该字段回落保守缺省（query_type→other / strategy→web）。"""
    answers = {
        "query_type": _choice("opinion", 0.99, {"opinion": 0.99}),
        "search_strategy": _choice("offline", 0.99, {"offline": 0.99}),
    }
    outcome = parse_answers(answers, confidence_floor=0.5)
    assert outcome.query_type == "other"
    assert outcome.search_strategy == "web"


def test_parse_answers_missing_question_uses_conservative() -> None:
    """约定题键缺失/形态非法 → 该字段保守缺省（answerable=False / safety_shadow=None）。"""
    outcome = parse_answers({"query_type": "not-a-dict"}, confidence_floor=0.5)
    assert outcome.query_type == "other"
    assert outcome.search_strategy == "web"
    assert outcome.answerable is False
    assert outcome.safety_shadow is None


def test_parse_answers_noul_probability_is_the_answer() -> None:
    """noul 概率即答案（无独立 confidence，R19 探针）——P(yes) ≥ 0.5 → True。"""
    answers = {"answerable": {"type": "noul", "noul": 0.51}}
    assert parse_answers(answers, confidence_floor=0.5).answerable is True
    answers = {"answerable": {"type": "noul", "noul": 0.49}}
    assert parse_answers(answers, confidence_floor=0.5).answerable is False


def test_parse_answers_safety_shadow_is_observation_only() -> None:
    """safety_shadow 仅观测记录（不参与拦截——契约保守口径），值原样进 observation。"""
    answers = {
        "safety_shadow": _choice("politics", 0.88, {"politics": 0.88}),
    }
    outcome = parse_answers(answers, confidence_floor=0.5)
    assert outcome.safety_shadow == "politics"
    assert outcome.extra_observation["safety_shadow"] == "politics"


# ---- 混合规则护栏（nacos-config.md §1 v1.20 两字段一致纪律——decision 输出回填处强制）----


def test_parse_answers_verify_guard_forces_answerable_true() -> None:
    """护栏①：strategy=verify + 模型答 no（noul 低概率）→ 回填处强制 True，
    answerable_source=guard（以 strategy 为准修正模型误答——「verify → answerable=True」）。"""
    answers = {
        "search_strategy": _choice("verify", 0.9, {"verify": 0.9}),
        "answerable": {"type": "noul", "noul": 0.2},  # 模型答 no
    }
    outcome = parse_answers(answers, confidence_floor=0.5)
    assert outcome.search_strategy == "verify"
    assert outcome.answerable is True  # 护栏强制——模型答 no 亦修正
    assert outcome.answerable_source == "guard"
    assert outcome.extra_observation["decision_source_fields"]["answerable"] == "guard"


def test_parse_answers_verify_guard_covers_missing_answerable_question() -> None:
    """护栏①边界：strategy=verify + answerable 题缺失 → 仍强制 True（护栏覆盖保守
    缺省形态——strategy 已高置信生效，不回退 conservative 缺省 False）。"""
    answers = {"search_strategy": _choice("verify", 0.9, {"verify": 0.9})}
    outcome = parse_answers(answers, confidence_floor=0.5)
    assert outcome.search_strategy == "verify"
    assert outcome.answerable is True
    assert outcome.answerable_source == "guard"


def test_parse_answers_direct_recall_guard_forces_answerable_false() -> None:
    """护栏②：direct/recall + 模型答 yes → 回填处强制 False（历史 ≠ 知识纪律），
    answerable_source=guard 维持。"""
    for strategy in ("direct", "recall"):
        answers = {
            "search_strategy": _choice(strategy, 0.9, {strategy: 0.9}),
            "answerable": {"type": "noul", "noul": 0.95},  # 模型答 yes
        }
        outcome = parse_answers(answers, confidence_floor=0.5)
        assert outcome.search_strategy == strategy
        assert outcome.answerable is False  # 强制 false 维持
        assert outcome.answerable_source == "guard"


def test_parse_answers_web_keeps_model_answerable_raw_value() -> None:
    """web → 保留模型原值（回滚态 answerable 驱动的 synthesis 变体零变化）：
    答案题有效 = source=decision（含低于阈值的 no——值来自模型答案）；答案题缺失 =
    保守缺省 source=conservative。"""
    # web + 模型答 yes → 保留 True
    outcome = parse_answers({
        "search_strategy": _choice("web", 0.9, {"web": 0.9}),
        "answerable": {"type": "noul", "noul": 0.8},
    }, confidence_floor=0.5)
    assert outcome.answerable is True
    assert outcome.answerable_source == "decision"
    # web + 模型答 no（有效答案低于阈值）→ 保留 False，source=decision（值来自模型）
    outcome = parse_answers({
        "search_strategy": _choice("web", 0.9, {"web": 0.9}),
        "answerable": {"type": "noul", "noul": 0.3},
    }, confidence_floor=0.5)
    assert outcome.answerable is False
    assert outcome.answerable_source == "decision"
    # web + 答案题缺失 → 保守缺省 False
    outcome = parse_answers({
        "search_strategy": _choice("web", 0.9, {"web": 0.9}),
    }, confidence_floor=0.5)
    assert outcome.answerable is False
    assert outcome.answerable_source == "conservative"


# ---- run_decision（httpx 调用面：超时/失败分类/deadline-aware）----


def _systemone_response(answers: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": "decision-model-preview",
        "request_id": "req-1",
        "answers": answers,
        "usage": {"input_tokens": 108},
        "latency_ms": 40.9,
    }


class _Budget:
    """TurnDeadline 假体（can_attempt 可编排）。"""

    def __init__(self, can_attempt: bool) -> None:
        self._can_attempt = can_attempt

    def can_attempt(self, estimated_cost_s: float) -> bool:
        return self._can_attempt


def _params(**overrides: Any) -> Any:
    base = resolve_decision_params({"decision": "decision-model"})
    assert base is not None
    return base


@pytest.fixture()
def turn_config() -> dict[str, Any]:
    return {"configurable": {"thread_id": "th-1", "turn_id": "11111111-1111-1111-1111-111111111111"}}


async def test_run_decision_success_parses_answers(
    monkeypatch: pytest.MonkeyPatch, turn_config: dict[str, Any]
) -> None:
    """200 + 合法 answers → DecisionOutcome（usage/latency 进 metadata）。"""
    captured: dict[str, Any] = {}

    async def _fake_post(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return _systemone_response({
            "query_type": _choice("other", 0.9, {"other": 0.9}),
            "search_strategy": _choice("direct", 0.8, {"direct": 0.8}),
            "answerable": {"type": "noul", "noul": 0.1},
        })

    monkeypatch.setattr(decision_client, "_post_systemone", _fake_post)
    params = _params()
    outcome = await decision_client.run_decision(
        query="你好，能做什么？",
        history=[],
        params=params,
        budget=_Budget(True),
        config=turn_config,
    )
    assert outcome is not None
    assert outcome.search_strategy == "direct"
    assert outcome.answerable is False
    # 请求面：端点 = 网关 /v1/systemone（base_url 复用 litellm_settings 现行值）
    assert captured["url"].endswith("/v1/systemone")
    assert captured["payload"]["model"] == "decision-model"
    assert "你好，能做什么？" in captured["payload"]["state"]
    assert captured["payload"]["questions"] == params.questions
    # metadata：usage/latency/confidence/probabilities 落 Langfuse generation metadata 源
    assert outcome.metadata["latency_ms"] == 40.9
    assert outcome.metadata["input_tokens"] == 108


async def test_run_decision_timeout_returns_none(
    monkeypatch: pytest.MonkeyPatch, turn_config: dict[str, Any]
) -> None:
    """超时（TimeoutError）→ None（rewrite 侧 light 全职责兜底，单次尝试零重试）。"""

    async def _timeout_post(**kwargs: Any) -> dict[str, Any]:
        raise TimeoutError("decision timeout")

    monkeypatch.setattr(decision_client, "_post_systemone", _timeout_post)
    outcome = await decision_client.run_decision(
        query="q", history=[], params=_params(), budget=_Budget(True), config=turn_config
    )
    assert outcome is None


async def test_run_decision_connection_failure_returns_none(
    monkeypatch: pytest.MonkeyPatch, turn_config: dict[str, Any]
) -> None:
    """连接类失败（httpx.ConnectError，guards connection 桶）→ None。"""

    async def _conn_post(**kwargs: Any) -> dict[str, Any]:
        raise httpx.ConnectError("refused", request=httpx.Request("POST", "http://gw/v1/systemone"))

    monkeypatch.setattr(decision_client, "_post_systemone", _conn_post)
    outcome = await decision_client.run_decision(
        query="q", history=[], params=_params(), budget=_Budget(True), config=turn_config
    )
    assert outcome is None


async def test_run_decision_http_4xx_returns_none(
    monkeypatch: pytest.MonkeyPatch, turn_config: dict[str, Any]
) -> None:
    """HTTP 4xx（鉴权/方言拒收）→ None（错误信封两套并存不抛穿）。"""
    request = httpx.Request("POST", "http://gw/v1/systemone")
    response = httpx.Response(401, request=request, json={"error": {"message": "invalid_api_key"}})

    async def _auth_post(**kwargs: Any) -> dict[str, Any]:
        raise httpx.HTTPStatusError("401", request=request, response=response)

    monkeypatch.setattr(decision_client, "_post_systemone", _auth_post)
    outcome = await decision_client.run_decision(
        query="q", history=[], params=_params(), budget=_Budget(True), config=turn_config
    )
    assert outcome is None


async def test_run_decision_malformed_body_returns_none(
    monkeypatch: pytest.MonkeyPatch, turn_config: dict[str, Any]
) -> None:
    """200 但 answers 缺失/非对象（解析失败）→ None。"""

    async def _bad_post(**kwargs: Any) -> dict[str, Any]:
        return {"code": "InvalidParameter", "message": "..."}

    monkeypatch.setattr(decision_client, "_post_systemone", _bad_post)
    outcome = await decision_client.run_decision(
        query="q", history=[], params=_params(), budget=_Budget(True), config=turn_config
    )
    assert outcome is None


async def test_run_decision_deadline_exhausted_skips_call(
    monkeypatch: pytest.MonkeyPatch, turn_config: dict[str, Any]
) -> None:
    """deadline-aware（沿 rewrite 现口径）：剩余预算 < decision 超时 → 零调用直接 None。"""
    calls: list[Any] = []

    async def _spy_post(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return _systemone_response({})

    monkeypatch.setattr(decision_client, "_post_systemone", _spy_post)
    outcome = await decision_client.run_decision(
        query="q", history=[], params=_params(), budget=_Budget(False), config=turn_config
    )
    assert outcome is None
    assert calls == []  # 预算不足 → 放弃调用（不烧网关请求）


async def test_run_decision_observe_failure_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, turn_config: dict[str, Any]
) -> None:
    """Langfuse 观测异常 → 告警不阻塞（观测不阻塞主链路口径）。"""

    async def _ok_post(**kwargs: Any) -> dict[str, Any]:
        return _systemone_response({
            "search_strategy": _choice("web", 0.9, {"web": 0.9}),
        })

    def _boom(**kwargs: Any) -> None:
        raise RuntimeError("langfuse down")

    monkeypatch.setattr(decision_client, "_post_systemone", _ok_post)
    monkeypatch.setattr(decision_client, "observe_decision", _boom)
    outcome = await decision_client.run_decision(
        query="q", history=[], params=_params(), budget=_Budget(True), config=turn_config
    )
    assert outcome is not None


# ---- Langfuse generation（独立 generation：model=decision-model、metadata 落观测）----


def test_observe_decision_noop_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """LANGFUSE_ 未配置（单测缺省形态）→ 零开销 no-op（不构造客户端）。"""
    constructed: list[Any] = []

    def _boom() -> None:
        constructed.append(True)
        raise AssertionError("未配置不应构造 Langfuse 客户端")

    monkeypatch.setattr(decision_client, "_langfuse_configured", lambda: False)
    monkeypatch.setattr(decision_client, "_langfuse_client", _boom)
    decision_client.observe_decision(
        turn_id="11111111-1111-1111-1111-111111111111",
        alias="decision-model",
        state_text="q",
        raw={"answers": {}},
        outcome=None,
        latency_ms=None,
        error="timeout",
    )
    assert constructed == []


def test_observe_decision_generation_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置齐全 → start_observation(generation)：model=别名、metadata 带
    confidence/probabilities/latency/usage、trace_id=turn_id hex 化。"""
    recorded: dict[str, Any] = {}

    class _FakeSpan:
        def __init__(self, **kwargs: Any) -> None:
            recorded.update(kwargs)

        def end(self) -> None:
            recorded["ended"] = True

    class _FakeClient:
        def start_observation(self, **kwargs: Any) -> _FakeSpan:
            recorded["started"] = True
            return _FakeSpan(**kwargs)

    monkeypatch.setattr(
        decision_client, "_langfuse_client", lambda: _FakeClient()
    )
    monkeypatch.setattr(
        decision_client, "_langfuse_configured", lambda: True
    )
    raw = _systemone_response({
        "search_strategy": _choice("web", 0.71, {"web": 0.79, "verify": 0.19}),
    })
    decision_client.observe_decision(
        turn_id="2b5e1f0a-0000-0000-0000-000000000001",
        alias="decision-model",
        state_text="鸡心倒吊是什么",
        raw=raw,
        outcome=None,
        latency_ms=41.0,
        error=None,
    )
    assert recorded.get("started") and recorded.get("ended")
    assert recorded["as_type"] == "generation"
    assert recorded["name"] == decision_client.GENERATION_NAME
    assert recorded["model"] == "decision-model"
    trace_context = recorded["trace_context"]
    assert trace_context["trace_id"] == "2b5e1f0a000000000000000000000001"  # hex 化（32-hex）
    metadata = recorded["metadata"]
    assert metadata["search_strategy_confidence"] == pytest.approx(0.71)
    assert metadata["latency_ms"] == pytest.approx(41.0)
    assert metadata["input_tokens"] == 108
    assert "answers" in metadata  # probabilities/raw answers 全量落 metadata


def test_observe_decision_metadata_carries_answerable_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """outcome 携带 answerable_source → 平铺进 generation metadata（M4 护栏介入率
    分桶的观测可达面——guard/decision/conservative 三值）。"""
    recorded: dict[str, Any] = {}

    class _FakeSpan:
        def __init__(self, **kwargs: Any) -> None:
            recorded.update(kwargs)

        def end(self) -> None:
            recorded["ended"] = True

    class _FakeClient:
        def start_observation(self, **kwargs: Any) -> _FakeSpan:
            recorded["started"] = True
            return _FakeSpan(**kwargs)

    monkeypatch.setattr(decision_client, "_langfuse_configured", lambda: True)
    monkeypatch.setattr(decision_client, "_langfuse_client", lambda: _FakeClient())
    outcome = DecisionOutcome(
        query_type="comparison",
        query_type_source="decision",
        search_strategy="verify",
        search_strategy_source="decision",
        answerable=True,
        answerable_source="guard",
        safety_shadow=None,
    )
    decision_client.observe_decision(
        turn_id="2b5e1f0a-0000-0000-0000-000000000003",
        alias="decision-model",
        state_text="q",
        raw=_systemone_response({}),
        outcome=outcome,
        latency_ms=10.0,
        error=None,
    )
    assert recorded["metadata"]["answerable_source"] == "guard"
    assert recorded["metadata"]["answerable"] is True


def test_observe_decision_reuses_same_client_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同进程多次 observe 复用同一客户端实例（批次 2 评审 P2：每次直构新 Langfuse()
    且引用即弃、无 flush → 模块级单例 + SDK 全局客户端通道）。构造通道
    langfuse.get_client 打桩计数：两次 observe 仅构造一次、且复用同一实例对象
    （修复前每次 observe 各建一例）。"""
    import langfuse

    constructed: list[Any] = []

    class _FakeSpan:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def end(self) -> None:
            pass

    class _FakeClient:
        def start_observation(self, **kwargs: Any) -> _FakeSpan:
            return _FakeSpan(**kwargs)

    def _fake_get_client() -> _FakeClient:
        client = _FakeClient()
        constructed.append(client)
        return client

    monkeypatch.setattr(decision_client, "_langfuse_configured", lambda: True)
    monkeypatch.setattr(langfuse, "get_client", _fake_get_client)
    decision_client._langfuse_client.cache_clear()  # 隔离进程级缓存（ finally 复原）
    try:
        kwargs: dict[str, Any] = dict(
            turn_id="2b5e1f0a-0000-0000-0000-000000000002",
            alias="decision-model",
            state_text="鸡心倒吊是什么",
            raw=_systemone_response({}),
            outcome=None,
            latency_ms=1.0,
            error=None,
        )
        decision_client.observe_decision(**kwargs)
        decision_client.observe_decision(**kwargs)
        assert len(constructed) == 1  # 仅构造一次（直构形态下为 2）
        assert decision_client._langfuse_client() is constructed[0]
        assert decision_client._langfuse_client() is decision_client._langfuse_client()
    finally:
        decision_client._langfuse_client.cache_clear()
