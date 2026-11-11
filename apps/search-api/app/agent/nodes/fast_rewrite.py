"""rewrite 节点（B3 节点 1/5——ARCH §4.1 rewrite 行 / DEV §5.1 五步闭环）。

职责四合一（light 模型，S14/S15 基元）：
① 指代消解 ② 顺带分类（6+1）③ 参数可答判定 ④ 检索 query 集（type_query_map 夹取）。
语言判定已前置纯代码（TurnRunner 注入 `query_lang`，改写不改判——PRD OQ-2）。

失败语义（ARCH §4.1/§6.2，不终止两路径）：
- **校验失败**（结构化输出解析/校验不过）→ 修复回喂 1 次（§6.2 P1：校验错误文本回喂）
  → 仍失败按 factoid 档 / 3 query 机制兜底（显式常量，**不经 type_query_map 夹取**，
  ARCH §4.1 v1.4 澄清）；
- **传输失败**（网络/超时类，`default_retry_on` 分类判据）→ 按 spec.retry（轮内快照，
  OQ-11 同源）重试 + 退避 jitter → 耗尽 → 降级原始 query 单发；每次重试尝试前查轮级
  剩余预算（deadline-aware，§6.2 P0——剩余 < 预计单调用成本即放弃重试）。

纯函数边界：节点无 DB/EventBus；提示词经 AssetCenter 内存快照解析（未启动回落内置
默认）；LLM 工厂（`build_light_model`）可注入可 mock——单测全 mock 零网络。
memory_directive 为 M5 记忆通道：解析但**不写 state**（FastState 无该字段），仅经
stage done.extra 留档（api.md §7.2 stage 可选业务字段，TurnRunner 吸收进 timing）。
"""

import asyncio
import random
from collections.abc import Mapping
from typing import Any, Literal

from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field, ValidationError

from app.agent.graphs.signals import emit_stage
from app.agent.guards import (
    TurnDeadline,
    default_retry_on,
    ensure_not_canceled,
    llm_timeout_s,
    retry_policy_from_spec,
)
from app.agent.prompts import render_prompt, resolve_prompt
from app.config_center.defaults import MODEL_MAPPING_CONFIG
from app.core.config import litellm_settings

__all__ = [
    "MECHANISM_FALLBACK_QUERY_COUNT",
    "MECHANISM_FALLBACK_QUERY_TYPE",
    "TRANSPORT_FALLBACK_QUERY_TYPE",
    "RewriteOutput",
    "build_light_model",
    "rewrite",
]

#: 机制兜底显式常量（ARCH §4.1 v1.4 澄清：类型按 factoid、query 数固定 3，不经夹取）
MECHANISM_FALLBACK_QUERY_TYPE: str = "factoid"
MECHANISM_FALLBACK_QUERY_COUNT: int = 3
#: 传输降级路径的类型标注（未分类诚实标注，评测按 other 分桶；单发原始 query）
TRANSPORT_FALLBACK_QUERY_TYPE: str = "other"

QueryType = Literal[
    "factoid", "list", "comparison", "multi_facet", "temporal", "how_to", "other"
]


class RewriteOutput(BaseModel):
    """rewrite 结构化输出（nacos-config.md §1 fast.rewrite 输出 JSON 契约，S15）。"""

    queries: list[str] = Field(min_length=1)
    query_type: QueryType
    answerable_from_knowledge: bool
    time_range: Literal["day", "week", "month", "year"] | None = None
    memory_directive: str | None = None


def build_light_model(mapping: Mapping[str, Any] | None = None) -> Any:
    """light 模型实例：别名 = 轮内模型映射快照 `model.mapping.light`（OQ-11 同源）。

    映射缺省（Studio 直跑/无注入面）回落 config_center 内置默认。import
    reasoning_chat_model 即注册 openai_reasoning provider（R8）——qwen 同形 reasoning
    字段自动并入、普通路径零扰动；采样参数不写死（§6.7：由网关模型配置注入）。
    """
    import app.utils.reasoning_chat_model  # noqa: F401 —— 注册副作用（幂等）
    from langchain.chat_models import init_chat_model

    if not isinstance(mapping, Mapping):
        mapping = MODEL_MAPPING_CONFIG
    alias = str(mapping.get("light") or MODEL_MAPPING_CONFIG["light"])
    return init_chat_model(
        model=alias,
        model_provider="openai_reasoning",
        base_url=str(litellm_settings.base_url),
        api_key=litellm_settings.api_key.get_secret_value(),
        # R7 验证/部署口径（ARCH §10 R7 行 S0 结论 + reasoning_chat_model docstring 部署注）：
        # ① temperature=0——结构化输出合法率 19/20 于 temperature=0 实证；
        # ② max_tokens ≥8192——思考模型思维链可耗尽全部输出预算（实测 2048 下 3/3 空输出）；
        # ③ think=false（B3 rewrite 实证）——qwen3.5 思考期 60~150s 远超 fast.llm_timeout_s=30
        #    契约值与 P50 ≤30s 预算；light 通道消费面 = S15 结构化输出（无需思维链），关思考后
        #    单调用 ~1.5s 且工具调用稳定。litellm_params 不透传 think（实测），故逐请求 extra_body
        #    注入；num_ctx=8192 在网关 light 条目（deploy/litellm/config.yaml）。
        temperature=0,
        max_tokens=8192,
        extra_body={"think": False},
    )


def _format_history(history: Any) -> str:
    """历史装配 → 提示词文本块（Q:/A: 逐轮；空历史空串）。"""
    if not isinstance(history, list):
        return ""
    blocks = [
        f"Q: {item.get('q', '')}\nA: {item.get('a_head', '')}"
        for item in history
        if isinstance(item, Mapping)
    ]
    return "\n".join(blocks)


def _render_prompt(state: Mapping[str, Any]) -> str:
    """fast.rewrite 模板渲染（五占位符契约集；memory_hints 为 M5 通道、启用前恒空串）。"""
    return render_prompt(
        resolve_prompt("fast.rewrite"),
        {
            "type_rules": resolve_prompt("common.type_rules"),
            "query_lang": str(state.get("query_lang") or "en"),
            "memory_hints": "",  # M5 记忆→查询改写通道；启用前恒空（memory-api.md §2）
            "history": _format_history(state.get("history")),
            "query": str(state.get("query") or ""),
        },
    )


def _dedupe_queries(queries: list[str]) -> list[str]:
    """strip + 丢弃空白项 + 保序去重（「禁止同义重复凑数」）。"""
    seen: set[str] = set()
    out: list[str] = []
    for query in queries:
        text = query.strip() if isinstance(query, str) else ""
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _clamp_queries(queries: list[str], spec: Mapping[str, Any], query_type: str) -> list[str]:
    """type_query_map 夹取（仅 LLM 合法输出路径）：去重保序 + 上限截断。

    下限不足不伪造（query 内容权威在 LLM）；未命中类型回落 other 档（§3 v1.3）；
    表缺失/结构非法回落 other 档 [2,3]（validator 拒收之外的路由面防线）。
    """
    deduped = _dedupe_queries(queries)
    fast_section = spec.get("fast") if isinstance(spec, Mapping) else None
    type_query_map = (
        fast_section.get("type_query_map")
        if isinstance(fast_section, Mapping)
        else None
    )
    bounds: Any = None
    if isinstance(type_query_map, Mapping):
        bounds = type_query_map.get(query_type) or type_query_map.get("other")
    cap = 3  # other 档上限兜底
    if isinstance(bounds, (list, tuple)) and len(bounds) == 2:
        try:
            cap = max(1, int(bounds[1]))
        except (TypeError, ValueError):
            cap = 3
    return deduped[:cap]


def _backoff_delay(policy: Any, attempt_index: int) -> float:
    """退避 = initial_interval_s × backoff_factor^attempt；jitter 默认开（§6.2 v1.3）。"""
    base = policy.initial_interval_s * (policy.backoff_factor**attempt_index)
    return random.uniform(0, base) if policy.jitter else base


def _extract(raw: Any) -> RewriteOutput:
    """归一校验：结构化输出（pydantic 实例）或裸 dict → RewriteOutput；全空白 queries 视为解析失败。"""
    parsed = raw if isinstance(raw, RewriteOutput) else RewriteOutput.model_validate(raw)
    if not _dedupe_queries(parsed.queries):
        raise OutputParserException("queries 全为空白——视同结构化输出校验失败")
    return parsed


async def _invoke(
    chain: Any, prompt: str, *, timeout_s: float, config: Any = None
) -> Any:
    """单次结构化调用（LLM 单调用分级超时包络，§6.2）。

    config 显式透传（langgraph 注入的 RunnableConfig：callbacks/metadata/langfuse_*
    契约键随 LLM run 到 Langfuse——trace root 层级与 session=thread_id 依赖此传递）。
    """
    async with asyncio.timeout(timeout_s):
        return await chain.ainvoke(prompt, config=config)


async def _invoke_with_retry(
    chain: Any,
    prompt: str,
    *,
    policy: Any,
    llm_timeout: float,
    budget: TurnDeadline,
    config: Any = None,
) -> Any:
    """带 spec RetryPolicy 的调用：传输类可重试（退避+jitter），校验类即时上抛。

    deadline-aware（§6.2 P0）：重试尝试前查轮级剩余预算——剩余 < 预计单调用成本
    即放弃重试（首试不受预算门控，保证至少一次尝试）。
    """
    last: Exception | None = None
    for attempt in range(policy.max_attempts):
        if attempt:
            if not budget.can_attempt(llm_timeout):
                break
            await asyncio.sleep(_backoff_delay(policy, attempt - 1))
        try:
            return await _invoke(chain, prompt, timeout_s=llm_timeout, config=config)
        except (OutputParserException, ValidationError):
            raise  # 确定性失败——重试同样失败（default_retry_on 同判据），直入修复回喂
        except Exception as exc:
            if not default_retry_on(exc):
                raise
            last = exc
    raise last if last is not None else RuntimeError("重试循环异常退出")


def _fallback_update(
    query: str, *, query_type: str, count: int, reason: str
) -> dict[str, Any]:
    """两路径共同形态：不终止、answerable 保守 false、degraded/errors 留痕（§4.1 横切字段）。"""
    return {
        "rewritten_queries": [query] * count if query else [],
        "query_type": query_type,
        "answerable_from_knowledge": False,
        "time_range": None,
        "errors": [{"stage": "rewrite", "reason": reason}],
        "degraded": True,
    }


async def rewrite(state: Any, config: RunnableConfig | None = None) -> dict[str, Any]:
    """rewrite 节点（fast 子图）：stage(rewrite, done) 收口（running 由 TurnRunner
    图前同步发布——ARCH §4.3 首 stage ≤1s 且无重复 seq）。"""
    ensure_not_canceled(config)  # 节点边界感知协作式取消（R14②：跨子图注入面）

    query = str(state.get("query") or "")
    spec = state.get("spec")
    spec = spec if isinstance(spec, Mapping) else {}
    fast_section = spec.get("fast") if isinstance(spec, Mapping) else None
    turn_timeout = (
        float(fast_section.get("turn_timeout_s", 60.0))
        if isinstance(fast_section, Mapping)
        else 60.0
    )
    policy = retry_policy_from_spec(spec, "fast")
    llm_timeout = llm_timeout_s(spec, "fast")
    budget = TurnDeadline(turn_timeout)

    configurable = (config or {}).get("configurable") or {}
    mapping = configurable.get("model_mapping")
    chain = build_light_model(mapping if isinstance(mapping, Mapping) else None).with_structured_output(
        RewriteOutput, method="function_calling"
    )
    prompt_text = _render_prompt(state)

    update: dict[str, Any]
    extra: dict[str, Any] | None = None
    try:
        raw = await _invoke_with_retry(
            chain,
            prompt_text,
            policy=policy,
            llm_timeout=llm_timeout,
            budget=budget,
            config=config,
        )
        parsed = _extract(raw)
        update = {
            "query_type": parsed.query_type,
            "answerable_from_knowledge": parsed.answerable_from_knowledge,
            "time_range": parsed.time_range,
            "rewritten_queries": _clamp_queries(parsed.queries, spec, parsed.query_type),
            "degraded": False,
        }
        if parsed.memory_directive:
            extra = {"memory_directive": parsed.memory_directive}
    except (OutputParserException, ValidationError) as exc:
        # 修复回喂 1 次（§6.2 P1）：校验错误文本回喂 LLM 修复 → 仍失败走机制兜底
        repair_prompt = (
            f"{prompt_text}\n\n上一次输出未通过校验，错误信息：{exc}。"
            "请调用 RewriteOutput 工具重新提交结果，严格按 schema 填写每个字段；"
            "不要在文本回复里书写 JSON。"
        )
        repaired: RewriteOutput | None
        try:
            repaired = _extract(
                await _invoke(chain, repair_prompt, timeout_s=llm_timeout, config=config)
            )
        except Exception:
            repaired = None
        if repaired is not None:
            update = {
                "query_type": repaired.query_type,
                "answerable_from_knowledge": repaired.answerable_from_knowledge,
                "time_range": repaired.time_range,
                "rewritten_queries": _clamp_queries(
                    repaired.queries, spec, repaired.query_type
                ),
                "degraded": False,
            }
            if repaired.memory_directive:
                extra = {"memory_directive": repaired.memory_directive}
        else:
            # 机制兜底：factoid 档 / 3 query 固定（显式常量，不经夹取，不终止）
            update = _fallback_update(
                query,
                query_type=MECHANISM_FALLBACK_QUERY_TYPE,
                count=MECHANISM_FALLBACK_QUERY_COUNT,
                reason="structured_output_invalid",
            )
            extra = {"fallback": "mechanism"}
    except Exception:
        # LLM 调用重试耗尽（传输类经 RetryPolicy + deadline 门控）→ 降级原始 query 单发
        update = _fallback_update(
            query,
            query_type=TRANSPORT_FALLBACK_QUERY_TYPE,
            count=1,
            reason="llm_retry_exhausted",
        )
        extra = {"fallback": "degraded"}

    if extra is not None:
        emit_stage("rewrite", "done", extra=extra)
    else:
        emit_stage("rewrite", "done")
    return update
