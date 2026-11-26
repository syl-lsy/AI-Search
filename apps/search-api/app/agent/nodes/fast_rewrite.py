"""rewrite 节点（B3 节点 1/5——ARCH §4.1 rewrite 行 / DEV §5.1 五步闭环）。

**职责再分配（v1.33 Decision Model 批次 2，ADR-016 串行先判——拓扑零改动）**：
- `model.mapping.decision` 非空（轮内映射快照）→ 节点内**前置 decision 步骤**
  （`decision_client` 单次 systemone 调用，四题并行：query_type/search_strategy/
  answerable/safety_shadow〔仅观测〕）。判定生效（choice confidence ≥
  `fast.decision.confidence_floor`，逐字段保守回落）时：
  `strategy ∈ {direct, recall}` → **跳过 light 调用**直达 synthesis（省 1.5~6s）；
  `verify/web` → light **瘦身调用**只生成 queries/time_range/memory_directive
  （+safety 初判——判定面本批留 light，契约 v1.33 冻结钉注），decision 回填三分类字段；
- kill switch（decision 空串/缺失，**缺省态**）→ decision 整体跳过，light **全职责
  路径**（分类+判据+queries+safety）= 现状行为，存量轮零行为变化（沿 search_strategy
  v1.20 灰度先例）——done.extra **零 decision_source 键**（api.md v1.26 键可省略口径）；
- decision 失败/超时/解析失败/低置信 → light 全职责路径兜底（**零能力回退**——原
  五件事全量模板 `_load_full_rewrite_template` 逐字节保留，判据面 =
  `common.type_rules` 热更），`decision_source=light_fallback` 留痕，**不置 degraded**；
- shadow（`fast.decision.shadow=true`）→ decision 只观测不生效（light 全职责照常），
  双结果落 done.extra（`decision_shadow` 观测载荷 + light 侧 update）与 Langfuse
  generation metadata。

light 职责面（减面后三件事，v1.33 批次 2 实施指引）：①指代消解 ②queries 产出
（type_query_map 夹取）③safety 初判；query_type/answerable_from_knowledge/
search_strategy 三字段在 decision 生效轮由 decision 承载、在 light 全职责轮仍由
light 判定（原全量模板 + type_rules 判据面逐字节保留——e2e 实证瘦身 schema 串与
全量工具 schema 冲突会致本地 light 校验失败率陡增，prompt schema 串必须与工具
schema 同形）。语言判定已前置纯代码（TurnRunner 注入 `query_lang`，改写不改判
——PRD OQ-2）。

**策略灰度安全（v1.20 硬约束，v1.51 沿用）**：字段缺失（旧提示词 v1.18）/ 值非法 / 两兜底
路径一律 `web`——pydantic 缺省 `web` + 修复回喂失败走机制兜底（strategy=web,
answerable=false）。对齐纪律（nacos §1「两字段必须一致」）：`verify → answerable=True`；
`direct`/`recall → answerable=False`；`web → 保留原值`（回滚态 answerable 驱动的
synthesis 变体不变——ARCH §8 B9①）。recall 强制 false 的理由=**历史 ≠ 知识**。
decision 生效轮同纪律（`_aligned_answerable` 复用）。

失败语义（ARCH §4.1/§6.2，不终止两路径）：
- **校验失败**（结构化输出解析/校验不过）→ 修复回喂 1 次（§6.2 P1）→ 仍失败按
  factoid 档 / 3 query 机制兜底（显式常量，**不经 type_query_map 夹取**）；
- **传输失败**（网络/超时类，`default_retry_on` 分类判据）→ 按 spec.retry（轮内快照，
  OQ-11 同源）重试 + 退避 jitter → 耗尽 → 降级原始 query 单发；deadline-aware（§6.2 P0）；
- decision 生效轮的**瘦身调用失败**（校验/传输）→ light 全职责路径接管
  （decision_source=light_fallback——轮判定源以最终产出方为准）。

兜底可见面（2026-11-13 B 裁决）：两兜底路径**不再置 FastState.degraded**——rewrite
兜底不属 api.md §7.2 degraded_reason 枚举（timeout/budget/source_failure）任一语义；
降级观测面收敛在 stage done.extra={fallback} + state.errors[] + AgentLog 埋点。

**Layer 2 输入安全拦截（B13，api.md §7.4 v1.23）**：safety 初判字段
（`none|danger|profanity|privacy|politics`，缺省 `none` 灰度安全）。仅 LLM 成功路径
判读：`spec.safety` enabled 且 mode=hybrid 且初判 ∈ {danger, profanity, privacy,
politics} → 类别化拒答话术 → stage(rewrite, failed, detail="安全拦截") →
ContentRejectedError。**机制兜底/传输兜底路径与 decision direct/recall 轮（零 light
判读面）不判 safety**：Layer 1（主图壳 pre_route 词表硬命中 / grey LLM 复核）已对该
query 前置覆盖；decision 的 safety_shadow 仅观测记录、不参与拦截（契约保守口径）。

纯函数边界：节点无 DB/EventBus；提示词经 AssetCenter 内存快照解析（未启动回落内置
默认）；LLM 工厂（`build_light_model`）与 decision 调用（`run_decision`）可注入可
mock——单测全 mock 零网络。
memory_directive 为 M5 记忆通道：解析但**不写 state**（FastState 无该字段），仅经
stage done.extra 留档。`strategy` 恒随 stage(rewrite,done).extra 留痕（api.md §7.4
v1.14 direct 形态钉死 `extra.strategy=direct`；verify/web 轮为观测增量，M4 按策略
分桶）；decision 启用轮 `extra.decision_source` 留痕（api.md v1.26：
decision | light_fallback | shadow 三值，键可省略——kill switch 轮与存量轮不携带）。
"""

import asyncio
import random
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field, ValidationError

from app.agent.decision_client import (
    align_answerable,
    resolve_decision_params,
    run_decision,
)
from app.agent.graphs.signals import emit_stage
from app.agent.nodes.kb_route import ROUTE_FALLBACK, correction_round_of
from app.agent.guards import (
    ContentRejectedError,
    TurnCanceledSignal,
    TurnDeadline,
    default_retry_on,
    ensure_not_canceled,
    llm_timeout_s,
    retry_policy_from_spec,
)
from app.agent.prompts import render_prompt, resolve_prompt
from app.agent.safety import generate_rejection_reply, load_safety_config
from app.config_center.defaults import MODEL_MAPPING_CONFIG
from app.core.config import litellm_settings
from app.core.logger import AgentLog

__all__ = [
    "MECHANISM_FALLBACK_QUERY_COUNT",
    "MECHANISM_FALLBACK_QUERY_TYPE",
    "FALLBACK_SEARCH_STRATEGY",
    "TRANSPORT_FALLBACK_QUERY_TYPE",
    "RewriteOutput",
    "RewriteSlimOutput",
    "SafetyFlag",
    "SearchStrategy",
    "build_light_model",
    "rewrite",
]

#: 机制兜底显式常量（ARCH §4.1 v1.4 澄清：类型按 factoid、query 数固定 3，不经夹取）
MECHANISM_FALLBACK_QUERY_TYPE: str = "factoid"
MECHANISM_FALLBACK_QUERY_COUNT: int = 3
#: 传输降级路径的类型标注（未分类诚实标注，评测按 other 分桶；单发原始 query）
TRANSPORT_FALLBACK_QUERY_TYPE: str = "other"
#: 两兜底路径的检索策略（nacos §1 v1.20 回落规则：兜底态 strategy=web、answerable=false）
FALLBACK_SEARCH_STRATEGY: str = "web"

#: 纠错重写反馈块（B19 plan §4.4「带反馈重写」——rewrite prompt 尾部追加；系统注入
#: 声明行防混淆，零用户文本回显。第二趟强制 hybrid=web+kb 并行补查）
_CORRECTION_FEEDBACK = (
    "\n\n【纠错重写提示（系统注入，非用户输入）】上一趟知识库检索供给不足"
    "（KB 零命中或得分过低）。本轮强制 hybrid：请重写 queries 以兼顾网络检索与"
    "知识库检索（queries 将同时发 web 与 kb 两通道），纠正只查知识库导致的覆盖缺口。"
)

QueryType = Literal[
    "factoid",
    "list",
    "comparison",
    "multi_facet",
    "temporal",
    "how_to",
    "navigational",  # v1.51：导航查询（官网/下载/入口/登录页类——结论单一，clamp 1 query）
    "other",
]

#: 检索策略四值（v1.44 B9 三值 / v1.51 +recall——「搜不搜」正交维度）
#: - direct：免检索直答轮（零事实面）；recall：回顾整理轮（历史重新生成，免检索直达）；
#: - verify ≡ answerable=true；web 默认全管道。
SearchStrategy = Literal["direct", "verify", "web", "recall"]

#: KB 检索档位三值（B19 plan §4.4 / nacos-config §3 kb.route v1.46——与
#: kb_route.KB_ROUTE_VALUES 同域；hybrid 缺省灰度=存量轮零行为变化）
KbRoute = Literal["kb_only", "kb_first", "hybrid"]

#: 输入安全初判四值（B13：none=正常 / danger=危险意图 / profanity=纯辱骂发泄 /
#: privacy=隐私侵害意图；politics=涉政敏感话题讨论请求（v1.32 +，reject-only）；
#: reject 组合约束同 safety.reply——pii 不在本通道，sanitize 面归 sanitizer）
SafetyFlag = Literal["none", "danger", "profanity", "privacy", "politics"]

#: Layer 2 拦截白名单（safety 初判命中且 enabled+hybrid 才拒；none 一律直通）
_REJECT_CATEGORIES: tuple[str, ...] = (
    "danger", "profanity", "privacy", "politics",
)


class RewriteOutput(BaseModel):
    """rewrite 结构化输出（nacos-config.md §1 fast.rewrite 输出 JSON 契约，S15）。

    `search_strategy` 缺省 `web`（v1.20 灰度安全：旧提示词无本字段即全量 web，
    存量轮零行为变化——字段由提示词热更启用）。
    `safety` 缺省 `none`（B13 灰度安全同款：旧提示词无本字段即全量放行，
    存量轮零行为变化；命中拦截面仅成功路径，见 `_safety_gate`）。
    `kb_route` 缺省 `hybrid`（B19 灰度安全同款：旧提示词无本字段即全量 hybrid，
    存量轮零行为变化；值域 kb_only|kb_first|hybrid——nacos-config §3 kb.route
    v1.46；缺省/非法由 pydantic 枚举拒收进修复回喂，兜底路径落 ROUTE_FALLBACK）。

    v1.33 注：本 schema = **light 全职责路径**（kill switch 关闭态 / decision 失败
    兜底态）的输出契约——三分类字段（query_type/answerable/search_strategy）在
    decision 生效轮由 decision 回填、在本路径仍由 light 判定（判据 = questions_json
    附录，见 `_classification_appendix`）。
    """

    queries: list[str] = Field(min_length=1)
    query_type: QueryType
    answerable_from_knowledge: bool
    search_strategy: SearchStrategy = "web"
    kb_route: KbRoute = "hybrid"
    safety: SafetyFlag = "none"
    time_range: Literal["day", "week", "month", "year"] | None = None
    memory_directive: str | None = None


class RewriteSlimOutput(BaseModel):
    """light **瘦身调用**输出 schema（v1.33 决策生效 verify/web 轮——ADR-016②）。

    只生成 decision 输出长度 0 天然不可承接的文本生成面字段：queries /
    time_range / memory_directive / safety（**safety 判定面本批留 light**——契约
    v1.33 冻结钉注，B13 两层防御不动）；query_type/answerable/search_strategy 三
    分类字段由 decision 回填（`_decision_slim_update`）。缺省值口径与 RewriteOutput
    一致（safety=none 灰度安全）。
    `kb_route`（B19，修订注 3 Nacos fast.rewrite 键增量）：KB 检索档位由 light 判定
    （decision 未承接该新增判定面）——schema 串与工具 schema 同形纪律（e2e 教训），
    缺省 hybrid 灰度安全。
    """

    queries: list[str] = Field(min_length=1)
    kb_route: KbRoute = "hybrid"
    safety: SafetyFlag = "none"
    time_range: Literal["day", "week", "month", "year"] | None = None
    memory_directive: str | None = None


class _LightPhaseFailure(Exception):
    """瘦身调用终局失败信号（校验回喂仍败/传输耗尽）——rewrite 捕获后回退 light
    全职责路径（decision_source=light_fallback），不抛穿节点。"""


def build_light_model(mapping: Mapping[str, Any] | None = None) -> Any:
    """light 模型实例：别名 = 轮内模型映射快照 `model.mapping.light`（OQ-11 同源）。

    映射缺省（Studio 直跑/无注入面）回落 config_center 内置默认。import
    reasoning_chat_model 即注册 openai_reasoning provider（R8）——qwen 同形 reasoning
    字段自动并入、普通路径零扰动；采样参数不写死（§6.7：由网关模型配置注入）。

    `stream_usage=True`（M2 批次 1 usage 方案 A，docs/m2-usage-probe-batch0.md §4）：
    显式 base_url 下 langchain-openai 不自动置位（probe §2.2 因果链 2）——本通道现走
    disable_streaming 非流式（usage 由响应体自带，不受此参数影响）；若 light 通道未来
    被流式消费（如批次 2 子代理档位探针），流式调用的 `AIMessageChunk.usage_metadata`
    即有填充源（网关 ollama 通道透传已活体探针实证）。
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
        # ④ disable_streaming=True（2026-11-13 A 裁决根因修复，Langfuse GENERATION 实证）：
        #    langgraph stream_mode=["messages",...] 向图内 runnable 注入 token handler，
        #    langchain 据此强制本调用走流式 → litellm ollama provider 流式路由 /api/generate
        #    （ndjson）→ tools 语义降级 → qwen3.5:4b 把工具调用写成文本 JSON（无 tool_calls）
        #    → parser 返回 None → ValidationError → mechanism 兜底（性能抽样 41/41 全兜底、
        #    +~2.8s/轮双重调用）。light 通道 = S15 结构化输出，零流式消费面；禁流式后注入不再
        #    劫持，请求回落 /api/chat（tools 原生支持）。synthesis 流式走 chat-standard 实例
        #    （resolve_chat_model），不受本参数影响；R14③ 本就只放行 synthesis token 事件。
        temperature=0,
        max_tokens=8192,
        stream_usage=True,  # usage 方案 A（M2 批次 1）：见本函数 docstring——非流式通道不受影响
        extra_body={"think": False},
        disable_streaming=True,
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


def _render_prompt(state: Mapping[str, Any], query_lang: str) -> str:
    """fast.rewrite 模板渲染（v1.33 减面版四占位符——decision 生效 verify/web 轮的
    light 瘦身调用专用）。

    减面后模板零 {{type_rules}} 引用（decision 生效轮判据面 = fast.decision.questions_json
    经 decision_client 装配）；变量映射仍携带 type_rules——Nacos 发布过渡窗的存量旧值
    （v1.32 全量模板）引用它时渲染不炸（render_prompt 只对「缺变量」fail-fast，多变量
    无害），两形态兼容。
    query_lang 由调用方解析后传入（TurnRunner 前置纯代码判定值，改写不改判——PRD
    OQ-2；Layer 2 拒答话术消费同一值，单一来源防漂移）。
    """
    return render_prompt(
        resolve_prompt("fast.rewrite"),
        {
            "type_rules": resolve_prompt("common.type_rules"),
            "query_lang": query_lang,
            "memory_hints": "",  # M5 记忆→查询改写通道；启用前恒空（memory-api.md §2）
            "history": _format_history(state.get("history")),
            "query": str(state.get("query") or ""),
        },
    )


@lru_cache(maxsize=1)
def _load_full_rewrite_template() -> str:
    """light 全职责路径模板（v1.33——原五件事全量模板逐字节保留）。

    减面后 `fast.rewrite`（Nacos 键）承载瘦身版（decision 生效 verify/web 轮专用）；
    kill switch 关闭态 / decision 失败兜底态的 light 全职责路径**不消费该键**，改用
    本内置全量模板（原模板逐字节保留——含 {{type_rules}} 引用，判据面 =
    `common.type_rules`（Nacos 热更，与 questions_json 双侧同步纪律不变））。保留
    理由（e2e 实证，test_fast_round_contract direct 判例）：瘦身 schema 串 + 分类附录
    与全量 RewriteOutput 工具 schema 冲突，本地 light 模型（qwen3.5:4b）校验失败率
    陡增直入机制兜底——**prompt schema 串与工具 schema 必须同形**。本文件为无键
    内置资产（不注册 BUILTIN_PROMPT_FILES、不经 Nacos——兜底路径的模板面静态，
    判据面仍经 {{type_rules}} 热更；变更随代码发布）。
    """
    return (
        Path(__file__).resolve().parent.parent / "prompts" / "fast" / "rewrite_full.txt"
    ).read_text(encoding="utf-8")


def _render_full_prompt(state: Mapping[str, Any], query_lang: str) -> str:
    """light 全职责路径 prompt：原五件事全量模板渲染（= v1.33 前现状逐字节）。

    变量集五件套（type_rules/query_lang/memory_hints/history/query）与减面前
    `_render_prompt` 完全一致——kill switch 关闭态 = 现状行为回归锁（e2e
    test_fast_round_contract 三判例 + unit kill switch 回归锁同证）。
    """
    return render_prompt(
        _load_full_rewrite_template(),
        {
            "type_rules": resolve_prompt("common.type_rules"),
            "query_lang": query_lang,
            "memory_hints": "",  # M5 记忆→查询改写通道；启用前恒空（memory-api.md §2）
            "history": _format_history(state.get("history")),
            "query": str(state.get("query") or ""),
        },
    )


def _extract_slim(raw: Any) -> RewriteSlimOutput:
    """瘦身输出归一校验：结构化输出（pydantic 实例）或裸 dict → RewriteSlimOutput；
    全空白 queries 视为解析失败（与 RewriteOutput 同判）。"""
    parsed = raw if isinstance(raw, RewriteSlimOutput) else RewriteSlimOutput.model_validate(raw)
    if not _dedupe_queries(parsed.queries):
        raise OutputParserException("queries 全为空白——视同结构化输出校验失败")
    return parsed


def _decision_update(outcome: Any) -> dict[str, Any]:
    """decision 生效 direct/recall 轮的 state 增量（跳过 light——ADR-016②）。

    rewritten_queries 固定空表：零检索轮 queries 不参与路由（search_fanout 判定
    先于空 queries）；answerable 经 `_aligned_answerable` 对齐（direct/recall →
    False——历史 ≠ 知识纪律，decision 误判 true 亦被修正）；safety 不写 state
    （decision 的 safety_shadow 仅观测不参与拦截，Layer 1 已前置覆盖）。
    """
    strategy = outcome.search_strategy
    return {
        "rewritten_queries": [],
        "query_type": outcome.query_type,
        "answerable_from_knowledge": _aligned_answerable(strategy, outcome.answerable),
        "search_strategy": strategy,
        "kb_route": ROUTE_FALLBACK,  # B19：decision 未承接 kb_route 判定面——兜底 hybrid
        "time_range": None,
        "degraded": False,
    }


def _decision_slim_update(outcome: Any, slim: RewriteSlimOutput, spec: Mapping[str, Any]) -> dict[str, Any]:
    """decision 生效 verify/web 轮的 state 增量：decision 回填三分类字段 + light
    瘦身字段合并；queries 经 type_query_map 夹取（夹取键 = decision 的 query_type，
    下游读取点零变化）。"""
    strategy = outcome.search_strategy
    return {
        "query_type": outcome.query_type,
        "answerable_from_knowledge": _aligned_answerable(strategy, outcome.answerable),
        "search_strategy": strategy,
        "kb_route": slim.kb_route,  # B19：kb_route 判定留 light（slim schema 增量同形）
        "time_range": slim.time_range,
        "rewritten_queries": _clamp_queries(slim.queries, spec, outcome.query_type),
        "degraded": False,
    }


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
    """两路径共同形态：不终止、strategy=web、answerable 保守 false、errors 留痕（§4.1 横切字段）。

    **不置 FastState.degraded**（2026-11-13 B 裁决）：rewrite 兜底不属 api.md §7.2
    degraded_reason 枚举（timeout/budget/source_failure）任一语义——置位会被
    TurnRunner._finish_completed 一律派生为 source_failure（turn_service.py:856），
    把答案完整的正常轮误标「研究未完成/源失败」；轮内降级可见面 = stage
    done.extra={fallback} + errors[]（§6.2 stage detail 半句合规）。

    **search_strategy 回落 web**（nacos §1 v1.20 灰度安全硬约束）：两兜底路径与
    缺省/非法值同口径——旧提示词 + 新代码全量轮零行为变化。
    """
    return {
        "rewritten_queries": [query] * count if query else [],
        "query_type": query_type,
        "answerable_from_knowledge": False,
        "search_strategy": FALLBACK_SEARCH_STRATEGY,
        "kb_route": ROUTE_FALLBACK,  # B19：两兜底路径一律 hybrid（plan §4.4 灰度硬约束）
        "time_range": None,
        "errors": [{"stage": "rewrite", "reason": reason}],
    }


def _aligned_answerable(strategy: str, answerable: bool) -> bool:
    """策略↔answerable 对齐（nacos-config.md §1 v1.20 纪律：两字段必须一致）——
    v1.62 混合规则护栏：委托 `decision_client.align_answerable` 单一事实源
    （解析侧回填已对齐 + 本消费侧复判的幂等双防线；护栏介入由 outcome 的
    `answerable_source=guard` 标注）。

    - `verify`：强制 True（模型判 verify 但答 false 时以 strategy 为准修正）；
    - `direct`：强制 False（零事实面轮；synthesis 变体消费 strategy，不读该位）；
    - `recall`：强制 False（v1.51——**历史 ≠ 知识**：线程私有内容不属模型通用稳定知识，
      不宣称「参数可答」；即使模型误置 true 亦对齐到 false）；
    - `web`：**保留原值**——回滚态（web + answerable=true）仍切「先知识后取证」段，
      旧提示词 + 新代码的合成行为零变化（ARCH §8 B9① 明文）。
    """
    return align_answerable(strategy, answerable)


def _success_update(parsed: RewriteOutput, spec: Mapping[str, Any]) -> dict[str, Any]:
    """合法输出 → state 增量（strategy 透传 + answerable 对齐 + query 夹取）。"""
    return {
        "query_type": parsed.query_type,
        "answerable_from_knowledge": _aligned_answerable(
            parsed.search_strategy, parsed.answerable_from_knowledge
        ),
        "search_strategy": parsed.search_strategy,
        "kb_route": parsed.kb_route,  # B19：合法输出路径透传（缺省 hybrid）
        "time_range": parsed.time_range,
        "rewritten_queries": _clamp_queries(parsed.queries, spec, parsed.query_type),
        "degraded": False,
    }


async def _safety_gate(
    parsed: RewriteOutput,
    spec: Mapping[str, Any],
    query_lang: str,
    config: RunnableConfig | None,
) -> None:
    """Layer 2 输入安全拦截（B13，api.md §7.4 v1.23 错误码 403002 拒绝轮）。

    调用时机 = LLM 成功路径拿到 parsed 后（主调用与修复回喂共用）；**机制兜底/
    传输兜底路径不进本函数**——Layer 1（主图壳 pre_route 词表硬命中 / grey LLM
    复核 reject）已对该 query 前置覆盖，兜底路径本就无可靠 safety 判读面（LLM
    输出未成形），不重复拦截。

    直通三条件（任一命中即放行，判定按灰度安全顺序排布）：初判 ∉ {danger,
    profanity, privacy, politics}（**先行短路**——none 直通 + reject 组合约束同
    safety.reply，pii
    不走本通道；旧提示词全量 none → 零配置判读、零告警面，存量轮日志面亦零
    变化）；`load_safety_config` 返回 None（fail-open：spec 缺 safety 节 /
    enabled=false / mode 非法——本分支仅在模型真打出 flag 时可达，缺节告警
    不刷正常轮）；mode=lexicon（词表模式不消费模型初判，Layer 1 词表为准）。

    命中顺序：类别化拒答话术（SafetyConfig 透传保观测 + RunnableConfig 透传挂
    Langfuse 轮次 trace）→ stage(rewrite, failed, detail="安全拦截",
    extra={safety, layer}) → ContentRejectedError。**raise 点在 rewrite 的
    try/except 兜底族之外**——ContentRejectedError 属 Exception 族，若在兜底
    try 内上抛会被传输兜底 except 吞掉降级，拦截语义即失效；直达 TurnRunner
    走安全拒绝轮收束（error 403002，reply 作为 error.message）。
    """
    if parsed.safety not in _REJECT_CATEGORIES:
        return
    safety_config = load_safety_config(spec)
    if safety_config is None or safety_config.mode != "hybrid":
        return
    reply = await generate_rejection_reply(
        parsed.safety, query_lang, safety_config, config
    )
    emit_stage(
        "rewrite",
        "failed",
        detail="安全拦截",
        extra={"safety": parsed.safety, "layer": "rewrite"},
    )
    raise ContentRejectedError(parsed.safety, reply, layer="rewrite")


async def rewrite(state: Any, config: RunnableConfig | None = None) -> dict[str, Any]:
    """rewrite 节点（fast 子图）：stage(rewrite, done) 收口（running 由 TurnRunner
    图前同步发布——ARCH §4.3 首 stage ≤1s 且无重复 seq）。

    三阶段（v1.33 ADR-016 串行先判，拓扑零改动）：
    ① decision 前置步骤（kill switch 缺省跳过）——direct/recall 生效轮跳过 light
      直接收口；② verify/web 生效轮 light 瘦身调用；③ light 全职责路径（kill
      switch / decision 失败 / shadow / 瘦身失败兜底——现状代码路径逐字保留）。
    """
    ensure_not_canceled(config)  # 节点边界感知协作式取消（R14②：跨子图注入面）

    # B19 纠错第二趟（plan §4.4）：fuse_rerank 后确定性 gate 判不足 → 条件边 Send 回边
    # （payload 携带 correction_round=1）——本趟入口补发 running 帧（首趟 running 由
    # TurnRunner 图前发布；纠错趟自发布保持 running/done 配对——api.md §7.2「同 key
    # 多次出现合法」），第二趟强制 hybrid（update 钉 ROUTE_FALLBACK + 持久化轮帽）。
    correction_pass = correction_round_of(state) >= 1
    if correction_pass:
        emit_stage("rewrite", "running", detail="KB 供给不足——纠错重写（强制 hybrid 补查 web）")

    query = str(state.get("query") or "")
    query_lang = str(state.get("query_lang") or "en")
    spec = state.get("spec")
    spec = spec if isinstance(spec, Mapping) else {}
    fast_section = spec.get("fast") if isinstance(spec, Mapping) else None
    turn_timeout = (
        float(fast_section.get("turn_timeout_s", 120.0))
        if isinstance(fast_section, Mapping)
        else 120.0
    )
    policy = retry_policy_from_spec(spec, "fast")
    llm_timeout = llm_timeout_s(spec, "fast")
    budget = TurnDeadline(turn_timeout)

    configurable = (config or {}).get("configurable") or {}
    mapping = configurable.get("model_mapping")

    # ---- 阶段① decision 前置步骤（ADR-016；kill switch 缺省关闭——params None 直通）----
    params = resolve_decision_params(mapping)
    decision_source: str | None = None
    extra_parts: dict[str, Any] = {}
    update: dict[str, Any] | None = None
    #: Layer 2 判读对象（B13）——仅成功路径非 None；兜底路径置 None 即不判 safety
    accepted: Any | None = None
    outcome = None
    if params is not None:
        outcome = await run_decision(
            query=query,
            history=state.get("history"),
            params=params,
            budget=budget,
            config=config,
        )
        if outcome is None:
            decision_source = "light_fallback"  # 失败/超时/解析失败 → light 全职责兜底
        elif params.shadow:
            # shadow 双跑：decision 只观测不生效（light 全职责照常），双结果落
            # stage extra（decision 侧观测载荷）与 Langfuse generation metadata
            decision_source = "shadow"
            extra_parts["decision_shadow"] = outcome.extra_observation
            outcome = None
        elif outcome.search_strategy in ("direct", "recall"):
            # decision 判免检索轮 → 跳过 light 调用直达 synthesis（省 1.5~6s）；
            # safety 不判读（零 light 判读面——Layer 1 已前置覆盖，safety_shadow 仅观测）
            update = _decision_update(outcome)
            emit_stage(
                "rewrite",
                "done",
                extra={"strategy": outcome.search_strategy, "decision_source": "decision"},
            )
            return update
        else:
            decision_source = "decision"  # verify/web 生效轮 → 瘦身调用

    # ---- 阶段② decision 生效 verify/web 轮：light 瘦身调用（queries/time_range/ ----
    # ---- memory_directive/safety——三分类字段由 decision 回填）                  ----
    if decision_source == "decision" and outcome is not None:
        try:
            update, accepted, slim_parts = await _light_slim_phase(
                state,
                query,
                query_lang,
                spec,
                mapping,
                policy=policy,
                llm_timeout=llm_timeout,
                budget=budget,
                config=config,
                outcome=outcome,
            )
            extra_parts.update(slim_parts)
        except (TurnCanceledSignal, ContentRejectedError):
            raise  # 取消非故障 / 安全拒绝直达 TurnRunner（不在兜底族内吞掉）
        except Exception:
            # 瘦身调用终局失败 → light 全职责路径接管（零能力回退；轮判定源以最终
            # 产出方为准）
            decision_source = "light_fallback"
            update = None
            accepted = None

    # ---- 阶段③ light 全职责路径（kill switch / decision 失败 / shadow / 瘦身失败）----
    if update is None:
        update, accepted, extra_parts = await _light_full_phase(
            state,
            query,
            query_lang,
            spec,
            mapping,
            policy=policy,
            llm_timeout=llm_timeout,
            budget=budget,
            config=config,
            extra_parts=extra_parts if decision_source == "shadow" else {},
        )

    # Layer 2 安全拦截门（B13）：仅 LLM 成功路径判 safety；命中即 emit failed +
    # raise ContentRejectedError——raise 点在兜底 except 族之外（该异常属
    # Exception 族，兜底 try 内上抛会被传输兜底吞掉），直达 TurnRunner 拒绝轮；
    # 兜底路径与 decision direct/recall 轮 accepted=None 自然跳过（Layer 1 已前置
    # 覆盖，见 _safety_gate）
    if accepted is not None:
        await _safety_gate(accepted, spec, query_lang, config)

    # B19 纠错第二趟收口：强制 hybrid（与 kb_route.resolve 第二趟判定同谓词双防线）
    # + 轮帽持久化（correction_round ≥1 → 后续 fuse_rerank 条件边不再回边）。
    if correction_pass:
        update["kb_route"] = ROUTE_FALLBACK
        update["correction_round"] = correction_round_of(state) + 1

    # strategy 恒随 done.extra 留痕（direct 形态为 api.md §7.4 钉死形态；verify/web
    # 轮为观测增量）+ 分支标注（fallback / memory_directive / decision_source /
    # decision_shadow）合并——decision_source 仅 decision 启用轮携带（api.md v1.26
    # 键可省略口径：kill switch 轮与存量轮零携带）。
    # B19：kb_route 仅非缺省值携带（api.md v1.33 判定行「键可省」——hybrid 存量轮
    # 零键）；纠错趟 extra.correction=true 留痕（键可省同口径）。
    extra = {"strategy": update["search_strategy"], **extra_parts}
    if update.get("kb_route") and update["kb_route"] != ROUTE_FALLBACK:
        extra["kb_route"] = update["kb_route"]
    if correction_pass:
        extra["correction"] = True
    if decision_source is not None:
        extra["decision_source"] = decision_source
    emit_stage("rewrite", "done", extra=extra)
    return update


async def _light_slim_phase(
    state: Any,
    query: str,
    query_lang: str,
    spec: Mapping[str, Any],
    mapping: Any,
    *,
    policy: Any,
    llm_timeout: float,
    budget: TurnDeadline,
    config: RunnableConfig | None,
    outcome: Any,
) -> tuple[dict[str, Any], RewriteSlimOutput, dict[str, Any]]:
    """decision 生效 verify/web 轮的 light 瘦身调用（ADR-016②）。

    减面模板渲染（零分类判据注入——判据面已迁 decision）；失败语义对齐全职责路径：
    校验失败修复回喂 1 次，仍失败/传输耗尽上抛 `_LightPhaseFailure`（调用方回退
    light 全职责路径）。safety 初判随瘦身输出产出（判定面留 light——契约 v1.33
    冻结钉注），Layer 2 门在 rewrite 主函数对瘦身输出照常判读。
    """
    chain = build_light_model(mapping if isinstance(mapping, Mapping) else None).with_structured_output(
        RewriteSlimOutput, method="function_calling"
    )
    prompt_text = _render_prompt(state, query_lang)
    if correction_round_of(state) >= 1:
        prompt_text += _CORRECTION_FEEDBACK  # B19 纠错重写反馈（plan §4.4「带反馈重写」）
    try:
        raw = await _invoke_with_retry(
            chain,
            prompt_text,
            policy=policy,
            llm_timeout=llm_timeout,
            budget=budget,
            config=config,
        )
        slim = _extract_slim(raw)
    except (OutputParserException, ValidationError) as exc:
        # 修复回喂 1 次（§6.2 P1）：校验错误文本回喂 LLM 修复 → 仍失败回退全职责
        AgentLog(
            message=(
                f"rewrite 瘦身调用结构化输出校验失败（进修复回喂）："
                f"{type(exc).__name__}: {str(exc)[:800]}"
            ),
            graph="fast",
            node="rewrite",
        ).warning()
        repair_prompt = (
            f"{prompt_text}\n\n上一次输出未通过校验，错误信息：{exc}。"
            "请调用结构化输出工具重新提交结果，严格按 schema 填写每个字段；"
            "不要在文本回复里书写 JSON。"
        )
        try:
            slim = _extract_slim(
                await _invoke(chain, repair_prompt, timeout_s=llm_timeout, config=config)
            )
        except Exception as repair_exc:
            AgentLog(
                message=(
                    f"rewrite 瘦身调用修复回喂仍失败（回退 light 全职责路径）："
                    f"{type(repair_exc).__name__}: {str(repair_exc)[:200]}"
                ),
                graph="fast",
                node="rewrite",
            ).warning()
            raise _LightPhaseFailure("slim repair failed") from repair_exc
    except Exception as exc:
        # 传输类重试耗尽 → 回退 light 全职责路径（不在此降级单发——分类能力需保留）
        AgentLog(
            message=(
                f"rewrite 瘦身调用失败（回退 light 全职责路径）："
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
            graph="fast",
            node="rewrite",
        ).warning()
        raise _LightPhaseFailure("slim transport failed") from exc

    update = _decision_slim_update(outcome, slim, spec)
    extra_parts: dict[str, Any] = {}
    if slim.memory_directive:
        extra_parts = {"memory_directive": slim.memory_directive}
    return update, slim, extra_parts


async def _light_full_phase(
    state: Any,
    query: str,
    query_lang: str,
    spec: Mapping[str, Any],
    mapping: Any,
    *,
    policy: Any,
    llm_timeout: float,
    budget: TurnDeadline,
    config: RunnableConfig | None,
    extra_parts: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Any, dict[str, Any]]:
    """light 全职责路径（kill switch 关闭态 / decision 失败兜底态——现状代码路径）。

    prompt = 原五件事全量模板渲染（`_render_full_prompt`——v1.33 前现状逐字节，
    含 {{type_rules}} 判据热更面）；七字段全由 light 判定（含三分类字段与 safety
    初判）。失败语义（机制兜底/传输降级）与本函数重构前逐字节一致（kill switch
    关闭态 = 现状行为回归锁）。
    `extra_parts` 透传（shadow 轮的 decision_shadow 观测载荷随 done.extra 续留）。
    """
    chain = build_light_model(mapping if isinstance(mapping, Mapping) else None).with_structured_output(
        RewriteOutput, method="function_calling"
    )
    prompt_text = _render_full_prompt(state, query_lang)
    if correction_round_of(state) >= 1:
        prompt_text += _CORRECTION_FEEDBACK  # B19 纠错重写反馈（plan §4.4「带反馈重写」）

    parts: dict[str, Any] = dict(extra_parts or {})
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
        update = _success_update(parsed, spec)
        if parsed.memory_directive:
            parts = {**parts, "memory_directive": parsed.memory_directive}
        return update, parsed, parts
    except (OutputParserException, ValidationError) as exc:
        # 修复回喂 1 次（§6.2 P1）：校验错误文本回喂 LLM 修复 → 仍失败走机制兜底
        AgentLog(
            message=(
                f"rewrite 结构化输出校验失败（进修复回喂）："
                f"{type(exc).__name__}: {str(exc)[:800]}"
            ),
            graph="fast",
            node="rewrite",
        ).warning()
        repair_prompt = (
            f"{prompt_text}\n\n上一次输出未通过校验，错误信息：{exc}。"
            "请调用结构化输出工具重新提交结果，严格按 schema 填写每个字段；"
            "不要在文本回复里书写 JSON。"
        )
        repaired: RewriteOutput | None
        try:
            repaired = _extract(
                await _invoke(chain, repair_prompt, timeout_s=llm_timeout, config=config)
            )
        except Exception as repair_exc:
            repaired = None
            # 机制兜底前最后观测点：主调用+回喂双重失败形态（2026-11-13 A 裁决埋点
            # ——41/41 兜底事故复发时此处直接给出失败原因，零埋点缺口收口）
            AgentLog(
                message=(
                    f"rewrite 修复回喂仍失败（进机制兜底，双重 LLM 调用）："
                    f"{type(repair_exc).__name__}: {str(repair_exc)[:200]}"
                ),
                graph="fast",
                node="rewrite",
            ).warning()
        if repaired is not None:
            update = _success_update(repaired, spec)
            if repaired.memory_directive:
                parts = {**parts, "memory_directive": repaired.memory_directive}
            return update, repaired, parts
        # 机制兜底：factoid 档 / 3 query 固定（显式常量，不经夹取，不终止）
        update = _fallback_update(
            query,
            query_type=MECHANISM_FALLBACK_QUERY_TYPE,
            count=MECHANISM_FALLBACK_QUERY_COUNT,
            reason="structured_output_invalid",
        )
        parts = {**parts, "fallback": "mechanism"}
        return update, None, parts
    except Exception as exc:
        # LLM 调用重试耗尽（传输类经 RetryPolicy + deadline 门控）→ 降级原始 query 单发
        AgentLog(
            message=(
                f"rewrite 传输失败重试耗尽（降级原始 query 单发）："
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
            graph="fast",
            node="rewrite",
        ).warning()
        update = _fallback_update(
            query,
            query_type=TRANSPORT_FALLBACK_QUERY_TYPE,
            count=1,
            reason="llm_retry_exhausted",
        )
        parts = {**parts, "fallback": "degraded"}
        return update, None, parts
