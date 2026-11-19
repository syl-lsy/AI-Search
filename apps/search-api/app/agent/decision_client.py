"""decision_client：SystemOne 决策模型薄客户端（Decision Model 工单批次 2——ADR-016）。

职责三件（薄客户端纪律——不自研协议、不直连百炼，全走 litellm 网关通道）：
① **参数解析**：`model.mapping.decision`（轮内映射快照，OQ-11 同源）非空才启用
   （空串/缺失 = kill switch 关闭——存量轮零行为变化，沿 search_strategy v1.20 灰度
   先例）；题面 `fast.decision.questions_json` 与标量键组（confidence_floor/timeout_ms/
   shadow）从 prompts 快照读（AssetCenter 内存快照，未启动/键缺失/非法回落代码内置
   默认并告警——nacos-config.md §1 v1.33 灰度安全硬约束）；
② **单次调用**：httpx POST `<litellm_base_url>/v1/systemone`（base_url/key 复用
   `litellm_settings` 现行值——R19 探针定案：decision 与 chat 档同网关、.env 零新增）；
   输入装配 = query 全文 + recall 判定语境的近轮历史摘编（ARCH §6.5 v1.62 决策步骤
   输入构成注，装配变量集单测锁死）；**deadline-aware**（沿 rewrite 现口径：轮级剩余
   预算 < decision 超时即放弃调用）；**单次尝试零重试**（单上游不配 fallback——失败由
   rewrite 侧回退 light 全职责路径，重试只会烧预算不救判定）；
③ **响应解析**：answers 按四约定题键取值——choice 题 `choice` 值 + `confidence`
   （独立校准量，≠max(probabilities)，R19 探针实证）；noul 题 `noul` 概率即答案
   （P(yes) ≥ 0.5 → True）；**阈值闸（逐字段保守回落）**：choice confidence <
   `confidence_floor` → 该字段回落保守缺省（search_strategy → web = R17 第三重压缩
   「拿不准一律 web」数值化；query_type → other 兜底题型）；**混合规则护栏**：
   answerable 回填处按 nacos v1.20 两字段一致纪律强制（verify→True /
   direct/recall→False / web 保留原值），override 以 `answerable_source=guard`
   标注（fast_rewrite 消费侧 `_aligned_answerable` 委托同一规则函数——幂等双防线）；
   safety_shadow 仅观测记录（契约保守口径：safety 判定面本批留 light，不参与拦截）。

失败语义：超时/连接类/HTTP 4xx（鉴权与方言拒收——错误信封校验类/鉴权类两套并存，
R19 探针）/解析失败一律返回 None 并按 guards 分类留痕（connection/transient/
non_retryable/timeout），**不抛穿**——rewrite 侧 None 即回退 light 全职责路径
（含分类+safety，行为等同现状，不置 degraded）。

观测：`observe_decision` 挂 Langfuse 独立 generation（model=decision 别名、
usage/latency/confidence/probabilities 落 metadata；trace_id=turn_id hex 化对账）——
未配置零开销 no-op、SDK 异常告警不阻塞主链路（观测不阻塞口径，core/observability
同款）；观测双通道中的网关 OTel 面归 litellm（environment=gateway），本模块只负责
应用侧 generation。
"""

import asyncio
import functools
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

import httpx

from app.agent.guards import is_connection_failure
from app.agent.prompts import PROMPTS_DATA_ID, load_builtin_prompt
from app.config_center import get_asset_center
from app.core.config import langfuse_settings, litellm_settings
from app.core.logger import AgentLog
from app.agent.state import QueryType, SearchStrategy

__all__ = [
    "DECISION_SYSTEMONE_PATH",
    "DEFAULT_CONFIDENCE_FLOOR",
    "DEFAULT_TIMEOUT_MS",
    "GENERATION_NAME",
    "DecisionOutcome",
    "DecisionParams",
    "align_answerable",
    "build_state_text",
    "decision_alias",
    "load_questions",
    "parse_answers",
    "resolve_decision_params",
    "run_decision",
    "observe_decision",
]

#: 网关 SystemOne 端点路径（litellm 1.106.4 decisions 路由；R19 探针 §四 schema）
DECISION_SYSTEMONE_PATH = "/v1/systemone"

#: prompts dataId 内的题面键与标量键组（nacos-config.md §1 v1.33）
QUESTIONS_KEY = "fast.decision.questions_json"
CONFIDENCE_FLOOR_KEY = "fast.decision.confidence_floor"
TIMEOUT_MS_KEY = "fast.decision.timeout_ms"
SHADOW_KEY = "fast.decision.shadow"

#: 代码内置默认（= 契约建议初值；键缺失/非法回落——「回落代码内置默认=旧行为」）
DEFAULT_CONFIDENCE_FLOOR = 0.5
DEFAULT_TIMEOUT_MS = 1000
DEFAULT_SHADOW = False

#: 标量键边界（与 validator 侧同口径的第二道防线；guard 不抛、越界回落默认）
_CONFIDENCE_FLOOR_MIN, _CONFIDENCE_FLOOR_MAX = 0.0, 1.0
_TIMEOUT_MS_MIN, _TIMEOUT_MS_MAX = 100, 10000

#: noul 题 P(yes) 阈值（概率即答案——R19 探针 §四响应形态）
_NOUL_YES_THRESHOLD = 0.5

#: Langfuse 独立 generation 名（观测树：trace root=轮 → decision generation）
GENERATION_NAME = "decision.systemone"


# ---- 参数解析（kill switch + 标量键组）----


@dataclass(frozen=True)
class DecisionParams:
    """单轮 decision 参数包（轮内快照解析产物，OQ-11 同源——整轮沿用）。"""

    alias: str
    questions: dict[str, Any]
    confidence_floor: float
    timeout_ms: int
    shadow: bool


@dataclass
class DecisionOutcome:
    """decision 单轮判定结果（rewrite 侧消费 + 观测面双载荷）。

    - 业务字段：query_type / search_strategy / answerable / safety_shadow——
      阈值闸与枚举校验已就地应用（保守回落字段 `*_source` 标注来源）；
      `answerable` 另经混合规则护栏对齐（v1.20 两字段一致纪律）——`answerable_source`
      标注终值来源：guard（护栏 override 模型答案）/ decision（模型答案）/ conservative
      （答案题缺失保守缺省）；
    - `extra_observation`：stage done.extra 的紧凑观测载荷（shadow 双跑对比与
      decision_source 留痕消费）；
    - `metadata`：Langfuse generation metadata 载荷（usage/latency/confidence/
      probabilities 全量——评测可达面）。
    """

    query_type: str
    query_type_source: str
    search_strategy: str
    search_strategy_source: str
    answerable: bool
    safety_shadow: str | None
    answerable_source: str = "decision"
    extra_observation: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


def decision_alias(mapping: Any) -> str:
    """`model.mapping.decision` 别名解析：空串/缺失/非字符串 → ""（kill switch 关闭）。"""
    if not isinstance(mapping, Mapping):
        return ""
    alias = mapping.get("decision")
    if not isinstance(alias, str):
        return ""
    return alias.strip()


def resolve_decision_params(mapping: Any, questions: Mapping[str, Any] | None = None) -> DecisionParams | None:
    """轮内映射快照 → DecisionParams；kill switch 关闭（空/缺失别名）→ None。

    questions/标量键组在本处一次性解析（OQ-11：整轮沿用同一份解析产物，热更仅对
    后续轮生效）；`questions` 可由调用方预载传入（rewrite 节点同轮的 light 全职责
    附录与 decision 题面共用一份解析产物——单次快照读，告警不重复刷）。
    """
    alias = decision_alias(mapping)
    if not alias:
        return None
    return DecisionParams(
        alias=alias,
        questions=dict(questions) if questions is not None else load_questions(),
        confidence_floor=_resolve_confidence_floor(),
        timeout_ms=_resolve_timeout_ms(),
        shadow=_resolve_shadow(),
    )


def _snapshot_prompts() -> Mapping[str, Any] | None:
    """prompts 快照 prompts 节（资产中心未启动/快照缺失 → None，静默回落内置）。"""
    try:
        snapshot = get_asset_center().snapshot(PROMPTS_DATA_ID)
    except Exception:  # noqa: BLE001 —— 回落属预期运行形态（组装层未启动）
        return None
    if snapshot is None:
        return None
    payload = getattr(snapshot, "payload", None)
    prompts = payload.get("prompts") if isinstance(payload, Mapping) else None
    return prompts if isinstance(prompts, Mapping) else None


def _builtin_questions() -> dict[str, Any]:
    """内置题面（defaults.py 与内置文件双源同步，test_prompts 锁漂移）。"""
    return dict(json.loads(load_builtin_prompt(QUESTIONS_KEY)))


def load_questions() -> dict[str, Any]:
    """题面载入：快照 `fast.decision.questions_json`（JSON 字符串）→ 非法/缺失回落内置。

    快照可用但键缺失/值非法按 §1 告警（配置面缺键应当被看见）；资产中心未启动属
    预期形态（Studio 直跑/单测），静默走内置默认。
    """
    prompts = _snapshot_prompts()
    if prompts is None:
        return _builtin_questions()
    raw = prompts.get(QUESTIONS_KEY)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and parsed:
                return parsed
        except ValueError:
            pass
    AgentLog(
        message=f"decision 题面键缺失或非法 JSON，回落内置默认：{QUESTIONS_KEY}",
        graph="fast",
        node="rewrite",
    ).warning()
    return _builtin_questions()


def _resolve_scalar(
    key: str, default: Any, validate: Any, cast: Any
) -> tuple[Any, bool]:
    """标量键解析共用骨架：返回 (值, 是否需要告警)。非法/缺失回落 default。"""
    prompts = _snapshot_prompts()
    if prompts is None:
        return default, False
    raw = prompts.get(key)
    if raw is None:
        return default, True
    if validate(raw):
        try:
            return cast(raw), False
        except (TypeError, ValueError):
            pass
    return default, True


def _resolve_confidence_floor() -> float:
    """`fast.decision.confidence_floor`：0 < 值 ≤ 1；缺失/非法回落 0.5 并告警。"""
    value, warn = _resolve_scalar(
        CONFIDENCE_FLOOR_KEY,
        DEFAULT_CONFIDENCE_FLOOR,
        lambda raw: (
            not isinstance(raw, bool)
            and isinstance(raw, (int, float))
            and _CONFIDENCE_FLOOR_MIN < float(raw) <= _CONFIDENCE_FLOOR_MAX
        ),
        float,
    )
    if warn:
        AgentLog(
            message=f"decision 置信阈值键缺失或非法，回落内置默认：{CONFIDENCE_FLOOR_KEY}",
            graph="fast",
            node="rewrite",
        ).warning()
    return value


def _resolve_timeout_ms() -> int:
    """`fast.decision.timeout_ms`：{_TIMEOUT_MS_MIN}~{_TIMEOUT_MS_MAX} 整数；缺失/非法回落 1000。"""
    value, warn = _resolve_scalar(
        TIMEOUT_MS_KEY,
        DEFAULT_TIMEOUT_MS,
        lambda raw: (
            not isinstance(raw, bool)
            and isinstance(raw, (int, float))
            and float(raw).is_integer()
            and _TIMEOUT_MS_MIN <= float(raw) <= _TIMEOUT_MS_MAX
        ),
        int,
    )
    if warn:
        AgentLog(
            message=f"decision 超时键缺失或非法，回落内置默认：{TIMEOUT_MS_KEY}",
            graph="fast",
            node="rewrite",
        ).warning()
    return value


def _resolve_shadow() -> bool:
    """`fast.decision.shadow`：严格布尔（字符串/数字形态回落 False 并告警——kill
    switch 同款防形态漂移纪律，bool("false") 反向失效陷阱）。"""
    value, warn = _resolve_scalar(
        SHADOW_KEY, DEFAULT_SHADOW, lambda raw: isinstance(raw, bool), bool
    )
    if warn:
        AgentLog(
            message=f"decision shadow 开关键缺失或非法，回落内置默认：{SHADOW_KEY}",
            graph="fast",
            node="rewrite",
        ).warning()
    return value


# ---- 输入装配（变量集单测锁死，ARCH §6.5 v1.62 决策步骤输入构成注）----


def build_state_text(query: str, history: Any) -> str:
    """判定输入装配：query 全文 + recall 判定语境的近轮历史摘编。

    历史条目形态与 rewrite 历史通道一致（TurnRunner 注入 {q, a_head}）——决策步骤
    单查共享行集派生、不新增 SQL（ARCH §6.5）。空历史仅 query 全文（回顾类护栏：
    无历史可回顾时 recall 判据自然不命中，判据侧护栏在 questions_json criteria）。
    """
    text = str(query or "")
    if not isinstance(history, list):
        return text
    blocks = [
        f"Q: {item.get('q', '')}\nA: {item.get('a_head', '')}"
        for item in history
        if isinstance(item, Mapping)
    ]
    if not blocks:
        return text
    return f"{text}\n\n近轮对话历史摘编（供回顾/重复提问判定参考）：\n" + "\n".join(blocks)


# ---- 响应解析（answers → 四字段 + 逐字段阈值闸）----


_CONSERVATIVE_QUERY_TYPE = "other"
_CONSERVATIVE_SEARCH_STRATEGY = "web"


def _choice_value(answer: Any) -> tuple[str, float] | None:
    """choice 题答案提取：`(choice 值, confidence)`；形态非法 → None。"""
    if not isinstance(answer, Mapping):
        return None
    value = answer.get("choice")
    confidence = answer.get("confidence")
    if not isinstance(value, str) or not isinstance(confidence, (int, float)) or isinstance(
        confidence, bool
    ):
        return None
    return value, float(confidence)


def _effective_choice(
    answer: tuple[str, float] | None, allowed: Any, floor: float
) -> str | None:
    """choice 题阈值闸：枚举内且 confidence ≥ floor → 判定值；否则 None（保守回落
    由调用方取保守缺省）。`allowed` 为 Literal 枚举元组（QueryType/SearchStrategy
    `__args__`——与 state.py 命名 Literal 同源，test_stub_graphs 锁双源）。"""
    if answer is None:
        return None
    value, confidence = answer
    if value in allowed and confidence >= floor:
        return value
    return None


def align_answerable(strategy: str, answerable: bool) -> bool:
    """策略↔answerable 混合规则护栏（nacos-config.md §1 v1.20 纪律：两字段必须一致）。

    - `verify`：强制 True（模型判 verify 但答 false 时以 strategy 为准修正）；
    - `direct`：强制 False（零事实面轮）；`recall`：强制 False（**历史 ≠ 知识**——
      线程私有内容不属模型通用稳定知识，模型误置 true 亦对齐到 false）；
    - `web`：**保留原值**（回滚态 answerable 驱动的 synthesis 变体不变——ARCH §8 B9①）。

    单一事实源：fast_rewrite 消费侧 `_aligned_answerable` 委托本函数——解析侧回填
    已对齐 + 消费侧复判的幂等双防线（消费侧兜上游漏护形态，判定结果恒一致）。
    """
    if strategy == "verify":
        return True
    if strategy in ("direct", "recall"):
        return False
    return answerable


def parse_answers(answers: Mapping[str, Any], *, confidence_floor: float) -> DecisionOutcome:
    """answers 四约定题键解析 + 逐字段阈值闸（ask 口径：该字段回落保守缺省）。

    - query_type / search_strategy（choice）：confidence < floor 或枚举外值或题缺失
      → 保守缺省（other / web）；safety_shadow（choice）仅观测——低置信不拦截不回落，
      原样记录；
    - answerable（noul）：概率即答案，P(yes) ≥ 0.5 → True；题缺失/形态非法 → False
      （保守，与机制兜底 answerable=false 同口径）；随后经 `align_answerable` 混合
      规则护栏对齐（v1.20 两字段一致纪律）——终值 ≠ 模型原始答案时
      `answerable_source=guard`，否则 decision（答案题有效）/ conservative（缺省）。
    """
    query_type_value = _effective_choice(
        _choice_value(answers.get("query_type")),
        getattr(QueryType, "__args__", ()),
        confidence_floor,
    )
    query_type = (
        query_type_value if query_type_value is not None else _CONSERVATIVE_QUERY_TYPE
    )
    query_type_source = "decision" if query_type_value is not None else "conservative"

    strategy_value = _effective_choice(
        _choice_value(answers.get("search_strategy")),
        getattr(SearchStrategy, "__args__", ()),
        confidence_floor,
    )
    search_strategy = (
        strategy_value if strategy_value is not None else _CONSERVATIVE_SEARCH_STRATEGY
    )
    strategy_source = "decision" if strategy_value is not None else "conservative"

    answerable_answer = answers.get("answerable")
    answerable_probability = (
        answerable_answer.get("noul")
        if isinstance(answerable_answer, Mapping)
        else None
    )
    if isinstance(answerable_probability, (int, float)) and not isinstance(
        answerable_probability, bool
    ):
        answerable_valid = True
        raw_answerable = float(answerable_probability) >= _NOUL_YES_THRESHOLD
    else:
        answerable_valid = False
        raw_answerable = False
    # 混合规则护栏（v1.20 两字段一致纪律——decision 输出回填处强制）：verify→True /
    # direct/recall→False / web 保留原值；override（终值 ≠ 模型原始答案）→ source=guard
    answerable = align_answerable(search_strategy, raw_answerable)
    if answerable != raw_answerable:
        answerable_source = "guard"
    elif answerable_valid:
        answerable_source = "decision"
    else:
        answerable_source = "conservative"

    safety_answer = _choice_value(answers.get("safety_shadow"))
    safety_shadow = safety_answer[0] if safety_answer is not None else None

    confidences: dict[str, float] = {}
    for key in ("query_type", "search_strategy", "safety_shadow"):
        answer = _choice_value(answers.get(key))
        if answer is not None:
            confidences[key] = answer[1]
    return DecisionOutcome(
        query_type=query_type,
        query_type_source=query_type_source,
        search_strategy=search_strategy,
        search_strategy_source=strategy_source,
        answerable=answerable,
        safety_shadow=safety_shadow,
        answerable_source=answerable_source,
        extra_observation={
            "query_type": query_type,
            "search_strategy": search_strategy,
            "answerable": answerable,
            "safety_shadow": safety_shadow,
            "decision_source_fields": {
                "query_type": query_type_source,
                "search_strategy": strategy_source,
                "answerable": answerable_source,
            },
            "confidence": confidences,
        },
        metadata={
            "answers": dict(answers),
            "confidence": confidences,
        },
    )


# ---- 调用面（httpx 打网关 /v1/systemone；单次尝试零重试）----


async def _post_systemone(
    *, url: str, api_key: str, payload: dict[str, Any], timeout_s: float
) -> dict[str, Any]:
    """单次 systemone POST（注入面：单测经 monkeypatch 打桩，零网络）。

    超时包络 = `fast.decision.timeout_ms`（R19 探针端到端 P50 0.416s 的 ~2 倍余量
    初值 1000ms）；非 2xx raise_for_status（鉴权 401 / 方言 400 → HTTPStatusError）。
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()


def _classify_failure(exc: BaseException) -> str:
    """失败分类对齐 guards 语义（classify_retry_tier 三桶 + timeout 细分；仅留痕用，
    不重试——decision 失败由 rewrite 侧 light 全职责兜底）。"""
    leaves = [exc]
    if isinstance(exc, BaseExceptionGroup):
        leaves = list(exc.exceptions)
    if any(isinstance(leaf, (TimeoutError, asyncio.TimeoutError)) for leaf in leaves):
        return "timeout"
    if any(is_connection_failure(leaf) for leaf in leaves):
        return "connection"
    return "non_retryable"


def _turn_id(config: Any) -> str:
    configurable = (config or {}).get("configurable") or {}
    return str(configurable.get("turn_id") or "")


async def run_decision(
    *,
    query: str,
    history: Any,
    params: DecisionParams,
    budget: Any,
    config: Any = None,
) -> DecisionOutcome | None:
    """执行单次 decision 判定：None = 本轮 decision 不可用（rewrite 回退 light 全职责）。

    deadline-aware（沿 rewrite 现口径）：轮级剩余预算 < decision 超时即放弃调用
    （首试同样受预算门控——decision 是前置可选步骤，预算不足时 light 路径本就
    需要全部预算）。调用成功但解析失败同样 None（结构异常属失败语义）。
    """
    timeout_s = params.timeout_ms / 1000
    if budget is not None and not budget.can_attempt(timeout_s):
        AgentLog(
            message=(
                f"decision 前置步骤跳过（轮级剩余预算不足 {params.timeout_ms}ms）——"
                "回退 light 全职责路径"
            ),
            graph="fast",
            node="rewrite",
        ).warning()
        return None
    state_text = build_state_text(query, history)
    payload = {
        "model": params.alias,
        "state": state_text,
        "questions": params.questions,
    }
    started = time.monotonic()
    try:
        raw = await _post_systemone(
            url=f"{str(litellm_settings.base_url).rstrip('/')}{DECISION_SYSTEMONE_PATH}",
            api_key=litellm_settings.api_key.get_secret_value(),
            payload=payload,
            timeout_s=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001 —— 失败语义：分类留痕 + light 兜底，不抛穿
        _log_failure(exc, timeout_ms=params.timeout_ms)
        _safe_observe(
            turn_id=_turn_id(config),
            alias=params.alias,
            state_text=state_text,
            raw=None,
            outcome=None,
            latency_ms=None,
            error=f"{type(exc).__name__}: {str(exc)[:200]}",
        )
        return None
    latency_ms = (time.monotonic() - started) * 1000

    answers = raw.get("answers") if isinstance(raw, Mapping) else None
    if not isinstance(answers, Mapping):
        AgentLog(
            message="decision 响应缺 answers 对象（解析失败，回退 light 全职责路径）",
            graph="fast",
            node="rewrite",
        ).warning()
        _safe_observe(
            turn_id=_turn_id(config),
            alias=params.alias,
            state_text=state_text,
            raw=raw if isinstance(raw, Mapping) else None,
            outcome=None,
            latency_ms=latency_ms,
            error="parse: answers missing",
        )
        return None

    outcome = parse_answers(dict(answers), confidence_floor=params.confidence_floor)
    usage = raw.get("usage") if isinstance(raw, Mapping) else None
    input_tokens = usage.get("input_tokens") if isinstance(usage, Mapping) else None
    outcome = replace(
        outcome,
        metadata={
            **outcome.metadata,
            "latency_ms": raw.get("latency_ms", latency_ms)
            if isinstance(raw, Mapping)
            else latency_ms,
            "input_tokens": input_tokens if isinstance(input_tokens, int) else None,
            "request_id": raw.get("request_id") if isinstance(raw, Mapping) else None,
            "shadow": params.shadow,
        },
    )
    _safe_observe(
        turn_id=_turn_id(config),
        alias=params.alias,
        state_text=state_text,
        raw=raw if isinstance(raw, Mapping) else None,
        outcome=outcome,
        latency_ms=latency_ms,
        error=None,
    )
    return outcome


def _log_failure(exc: BaseException, *, timeout_ms: int) -> None:
    AgentLog(
        message=(
            f"decision 调用失败（{_classify_failure(exc)}，单次尝试零重试，"
            f"回退 light 全职责路径）：{type(exc).__name__}: {str(exc)[:200]}"
        ),
        graph="fast",
        node="rewrite",
    ).warning()


# ---- Langfuse 独立 generation（观测不阻塞主链路）----


def _langfuse_configured() -> bool:
    secret = langfuse_settings.secret_key.get_secret_value()
    return bool(langfuse_settings.public_key and secret)


@functools.lru_cache(maxsize=1)
def _langfuse_client() -> Any:
    """Langfuse 客户端：模块级单例（functools.lru_cache，同进程复用同一实例）。

    与 core/observability.build_turn_callbacks 的关系如实描述：env 回填口径一致
    （settings → os.environ.setdefault 供 SDK 读取）；客户端通道不同——core/observability
    经 langfuse.langchain.CallbackHandler 内部的 SDK 全局客户端（langfuse.get_client
    按 public_key 单例、复用同一 tracer_provider），本模块显式走同一 get_client() 通道。
    修复批次 2 评审 P2：原实现每次 observe 直构新 Langfuse()（每次新建一份
    LangfuseResourceManager 后台批处理资源）且引用即弃、无显式 flush，事件上报依赖
    GC 时机不可靠；单例化后 flush 生命周期随全局实例收敛（SDK resource manager
    atexit shutdown 兜底）。
    """
    from langfuse import get_client

    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", langfuse_settings.public_key)
    os.environ.setdefault(
        "LANGFUSE_SECRET_KEY", langfuse_settings.secret_key.get_secret_value()
    )
    os.environ.setdefault("LANGFUSE_HOST", str(langfuse_settings.host))
    return get_client()


def _safe_observe(**kwargs: Any) -> None:
    """观测出口统一兜底：任何异常告警不阻塞（rewrite 侧消费面零感知）。"""
    try:
        observe_decision(**kwargs)
    except Exception as exc:  # noqa: BLE001 —— 观测构建/上报失败不阻塞主链路
        AgentLog(
            message=(
                f"decision Langfuse generation 上报失败（零观测继续）："
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
            graph="observability",
        ).warning()


def observe_decision(
    *,
    turn_id: str,
    alias: str,
    state_text: str,
    raw: Mapping[str, Any] | None,
    outcome: DecisionOutcome | None,
    latency_ms: float | None,
    error: str | None,
) -> None:
    """decision 调用挂 Langfuse 独立 generation（ADR-016 后果条 / 工单计划 §2.5）。

    model=decision 别名；usage（input_tokens）/latency_ms/confidence/probabilities
    （raw.answers 全量）落 metadata；trace_id = turn_id hex 化（trace_id_for 同口径
    ——Langfuse trace 与 turns 行同 id 可查）。未配置（单测缺省形态）零开销 no-op。
    """
    if not _langfuse_configured():
        return
    from app.core.observability import trace_id_for

    raw_answers = raw.get("answers") if isinstance(raw, Mapping) else None
    answers: dict[str, Any] = {
        str(key): value for key, value in raw_answers.items()
    } if isinstance(raw_answers, Mapping) else {}
    usage = raw.get("usage") if isinstance(raw, Mapping) else None
    input_tokens = usage.get("input_tokens") if isinstance(usage, Mapping) else None
    raw_confidences = (
        outcome.extra_observation.get("confidence")
        if outcome is not None
        else _choice_confidences(answers)
    )
    confidences: dict[str, float] = (
        dict(raw_confidences) if isinstance(raw_confidences, Mapping) else {}
    )
    metadata: dict[str, Any] = {
        # 逐题平铺置信键（Langfuse metadata 过滤友好）+ 聚合 dict 双形态
        **{f"{key}_confidence": value for key, value in confidences.items()},
        "confidence": confidences,
        "latency_ms": latency_ms,
        "input_tokens": input_tokens if isinstance(input_tokens, int) else None,
        "answers": answers,
    }
    if outcome is not None:
        metadata["query_type"] = outcome.query_type
        metadata["search_strategy"] = outcome.search_strategy
        metadata["answerable"] = outcome.answerable
        metadata["answerable_source"] = outcome.answerable_source
        metadata["safety_shadow"] = outcome.safety_shadow
    if error is not None:
        metadata["error"] = error

    client = _langfuse_client()
    span = client.start_observation(
        trace_context={"trace_id": trace_id_for(turn_id)} if turn_id else None,
        name=GENERATION_NAME,
        as_type="generation",
        model=alias,
        input=state_text,
        output=answers or error,
        metadata=metadata,
        level="ERROR" if error is not None else "DEFAULT",
        status_message=error,
        usage_details={"input": input_tokens}
        if isinstance(input_tokens, int) and input_tokens > 0
        else None,
    )
    span.end()


def _choice_confidences(answers: Mapping[str, Any]) -> dict[str, float]:
    """raw answers → 逐题 confidence（outcome 缺位时的 metadata 兜底提取）。"""
    confidences: dict[str, float] = {}
    for key, answer in answers.items():
        value = _choice_value(answer)
        if value is not None:
            confidences[key] = value[1]
        elif isinstance(answer, Mapping) and isinstance(answer.get("noul"), (int, float)):
            confidences[key] = float(answer["noul"])
    return confidences
