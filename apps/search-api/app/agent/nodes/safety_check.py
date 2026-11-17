"""safety_check 主图节点（B13 输入安全 Layer 1 pre_route——api.md §7.4 v1.23 / guards.ContentRejectedError）。

主图壳首节点（START→safety_check→route_by_mode 条件边——route_by_mode 函数与 targets
完全不动，仅入口后移）。三层流程（nacos-config.md §1/§3 v1.30）：

- **直通门**（零 LLM 零正则）：`ensure_not_canceled` → `load_safety_config(state.spec)`；
  None（节缺失 / enabled=false / mode 非法，fail-open——安全检查不挡检索）→ 返回 {}
  直通；
- **Stage A（代码层直拦，内核纯函数）**：`scan_lexicon` 词表硬命中 danger/profanity →
  拒答路径（见下，硬命中压 grey）；`detect_pii` 硬命中 → 脱敏返回 `{"query": 脱敏文本,
  "safety_category": "pii", "safety_action": "sanitize"}`（AgentLog 记录，日志零 PII
  原文；脱敏轮 query 覆写后照常检索，不产生 error 事件）；零硬命中且有 grey → Stage B；
  扫描在工作线程执行并受 classify_timeout_s 超时包络（ReDoS 第二道防线——validator
  静态拒收灾难性回溯形态之外的残余不挂死事件循环，超预算 fail-open 直通）；
- **Stage B（grey LLM 复核，仅 hybrid）**：light 模型结构化分类 `SafetyVerdict`
  （提示词 `common.safety_check` 占位符 {{query}}/{{query_lang}}；method="function_calling"；
  classify_timeout_s 包络；RunnableConfig 透传保 Langfuse 观测）。pass → {}；
  sanitize → 脱敏返回；reject（仅 danger|profanity）→ 拒答路径。失败/超时/校验不过 →
  fail-open 返回 {} + AgentLog warning（宁可漏判不可误伤，同提示词口径）。

拒答路径（节点内完成，不设独立 safe_reply 节点——设计定稿）：
`generate_rejection_reply` 生成类别化话术 → `emit_stage(首stage_key, "failed",
detail="安全拦截", extra={"safety": category, "layer": "pre_route"})`（首stage_key 按
mode：deep=plan / 其余=rewrite——TurnRunner 图前已发 running，此处 failed 合档）→
`raise ContentRejectedError(category, reply, layer="pre_route")`——TurnRunner 捕获后走
安全拒绝轮收束（error 403002，无 done，api.md §7.4 v1.23）。Layer 2（子图内
rewrite/planner safety 初判）同形上抛不上 state——故子图 state 不加 safety_* 键。

处置优先级（同句多形态命中）：danger > profanity > pii > grey（词表硬命中代码层直拦、
不经 grey LLM 复核；PII 命中压 grey——lexicon.py「硬命中压 grey」同构语义）。

组合违规防御：LLM 输出 reject+pii（提示词契约禁止的组合）→ 防御归一为 sanitize 路径
+ warning；reject+none 视同 pass（拿不准一律放行）。

纯函数边界：节点无 DB/EventBus；内核四件（config/lexicon/sanitizer/reply）真实实现，
LLM 工厂（`build_light_model`）模块级引用可注入可 mock——单测全 mock 零网络。
"""

import asyncio
from collections.abc import Mapping
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel

from app.agent.graphs.signals import emit_stage
from app.agent.guards import ContentRejectedError, TurnCanceledSignal, ensure_not_canceled
from app.agent.nodes.fast_rewrite import build_light_model
from app.agent.prompts import render_prompt, resolve_prompt
from app.agent.safety import SafetyConfig, detect_pii, load_safety_config, sanitize, scan_lexicon
from app.agent.safety.reply import generate_rejection_reply
from app.core.logger import AgentLog

__all__ = [
    "SAFETY_CHECK_PROMPT_KEY",
    "SafetyVerdict",
    "safety_check",
]

#: grey 区分类提示词键（占位符集 = {query, query_lang}，契约 §1 v1.30 锁死）
SAFETY_CHECK_PROMPT_KEY = "common.safety_check"

#: 脱敏轮 state 动作标注（pass 直通零更新、reject 走异常不落 state——仅 sanitize 落键）
_SAFETY_ACTION_SANITIZE = "sanitize"


class SafetyVerdict(BaseModel):
    """grey 区 LLM 结构化分类输出（common.safety_check 输出 JSON 契约，§1 v1.30）。

    组合约束由提示词钉死（reject 仅配 danger|profanity；category=pii 一律 sanitize）——
    运行层对违规组合防御性归一（见 `_apply_verdict`），不信任模型自律。
    """

    action: Literal["pass", "sanitize", "reject"]
    category: Literal["danger", "profanity", "pii", "none"]


def _extract_verdict(raw: Any) -> SafetyVerdict:
    """归一校验：结构化输出（pydantic 实例）或裸 dict → SafetyVerdict（非法即 ValidationError）。"""
    return raw if isinstance(raw, SafetyVerdict) else SafetyVerdict.model_validate(raw)


def _hard_category(scan: Any) -> str | None:
    """词表硬命中类别（danger 前于 profanity——类别优先级钉死，与词表配置序无关）。"""
    if any(hit.kind == "danger" for hit in scan.hard_hits):
        return "danger"
    if any(hit.kind == "profanity" for hit in scan.hard_hits):
        return "profanity"
    return None


async def _reject(
    state: Mapping[str, Any],
    config: RunnableConfig | None,
    safety: SafetyConfig,
    category: str,
) -> ContentRejectedError:
    """拒答路径：话术 → stage(首stage_key, failed) → ContentRejectedError。

    首stage_key 按 mode 对齐 TurnRunner 图前发布的 running（deep=plan / 其余=rewrite），
    failed 帧在 timing.stages 合档；话术生成失败由 reply 层模板兜底（拒答不因 LLM 故障
    失效）。
    """
    query_lang = str(state.get("query_lang") or "en")
    reply = await generate_rejection_reply(
        category, query_lang, safety, runnable_config=config
    )
    first_stage_key = "plan" if str(state.get("mode")) == "deep" else "rewrite"
    emit_stage(
        first_stage_key,
        "failed",
        detail="安全拦截",
        extra={"safety": category, "layer": "pre_route"},
    )
    return ContentRejectedError(category, reply, layer="pre_route")


def _sanitize_update(query: str, category: str) -> dict[str, Any]:
    """脱敏轮 state 增量：query 覆写为脱敏文本 + 类别/动作标注（日志零 PII 原文）。

    脱敏只对 PII 形态有实际改写面（detect_pii+sanitize 纯函数）；无命中的 sanitize 判定
    （grey 区模型判）原样保留 query 并诚实标注，不做内容改写。
    """
    findings = detect_pii(query)
    sanitized = sanitize(query, findings)
    kinds = ",".join(sorted({finding.kind for finding in findings})) or "-"
    AgentLog(
        message=(
            f"输入安全 pre_route：命中已脱敏（category={category}，findings={len(findings)}，"
            f"kinds={kinds}），query 已覆写进入检索"
        ),
        graph="safety",
        node="safety_check",
    ).info()
    return {
        "query": sanitized,
        "safety_category": category,
        "safety_action": _SAFETY_ACTION_SANITIZE,
    }


async def _classify(
    query: str,
    state: Mapping[str, Any],
    config: RunnableConfig | None,
    safety: SafetyConfig,
) -> SafetyVerdict | None:
    """grey 区 LLM 结构化分类；失败/超时/校验不过 → None（fail-open 直通）。

    提示词 `common.safety_check` 渲染（占位符 {query, query_lang}）；light 模型
    with_structured_output(method="function_calling")；classify_timeout_s 包络；
    RunnableConfig 显式透传（callbacks/langfuse_* 契约键随 LLM run 到 Langfuse——
    fast_rewrite._invoke 同口径）。TurnCanceledSignal 先于 except Exception 重抛
    （取消非故障，不落 fail-open）。
    """
    try:
        prompt = render_prompt(
            resolve_prompt(SAFETY_CHECK_PROMPT_KEY),
            {"query": query, "query_lang": str(state.get("query_lang") or "en")},
        )
        configurable = (config or {}).get("configurable") or {}
        mapping = configurable.get("model_mapping")
        chain = build_light_model(
            mapping if isinstance(mapping, Mapping) else None
        ).with_structured_output(SafetyVerdict, method="function_calling")
        async with asyncio.timeout(safety.classify_timeout_s):
            raw = await chain.ainvoke(prompt, config=config)
        return _extract_verdict(raw)
    except TurnCanceledSignal:
        raise  # 取消非故障（R14③）：不落 fail-open，向上传播收束
    except Exception as exc:  # noqa: BLE001 —— LLM 故障/超时/渲染失败/校验失败一律 fail-open
        AgentLog(
            message=(
                f"safety_check grey 分类失败（fail-open 直通）："
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
            graph="safety",
            node="safety_check",
        ).warning()
        return None


async def safety_check(state: Any, config: RunnableConfig | None = None) -> dict[str, Any]:
    """safety_check 主图节点：START 后、route_by_mode 前的输入安全检查（Layer 1 pre_route）。

    直通路径返回 {}（绝大多数轮零成本）；脱敏路径返回三键增量（query 覆写进检索）；
    拒绝路径 raise ContentRejectedError（TurnRunner 收束 403002）。
    """
    ensure_not_canceled(config)  # 节点边界感知协作式取消（取消先于一切安全逻辑）

    spec = state.get("spec")
    safety = load_safety_config(spec if isinstance(spec, Mapping) else None)
    if safety is None:
        return {}  # 直通门（零 LLM 零正则）

    query = str(state.get("query") or "")
    # Stage A 扫描（词表 + grey 正则）放工作线程 + 超时包络（ReDoS 第二道防线）：
    # 正则是运营面可热更资产，validator 静态拒收灾难性回溯形态之外的残余（多项式
    # 级复杂度等）不能挂死事件循环——同步 search 阻塞 asyncio 会冻结全部在途 SSE
    # 轮；超预算（复用 classify_timeout_s 预算）→ fail-open 直通 + 告警。线程内
    # 超时无法强杀，残余成本有界（query 经 API 边界 ≤2000 字符 + 规则 ≤120 字符）。
    try:
        scan = await asyncio.wait_for(
            asyncio.to_thread(scan_lexicon, query, safety),
            safety.classify_timeout_s,
        )
    except TimeoutError:  # 3.11+ asyncio.TimeoutError 同类
        AgentLog(
            message="safety_check 词表/grey 扫描超预算（fail-open 直通，ReDoS 兜底）",
            graph="safety",
            node="safety_check",
        ).warning()
        return {}
    except Exception as exc:  # noqa: BLE001 —— 扫描面异常防御（安全检查不挡检索）
        AgentLog(
            message=(
                f"safety_check 词表/grey 扫描失败（fail-open 直通）："
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
            graph="safety",
            node="safety_check",
        ).warning()
        return {}

    # Stage A：词表硬命中（danger > profanity）→ 拒答路径（代码层直拦，压 grey/PII）
    hard = _hard_category(scan)
    if hard is not None:
        raise await _reject(state, config, safety, hard)

    # Stage A：PII 硬命中 → 脱敏返回（压 grey；脱敏轮照常检索不产生 error 事件）
    if detect_pii(query):
        return _sanitize_update(query, "pii")

    # Stage B：grey 且 hybrid → light LLM 结构化分类（lexicon 模式=仅词表，零 LLM）
    if scan.grey and safety.mode == "hybrid":
        verdict = await _classify(query, state, config, safety)
        if verdict is not None:
            return await _apply_verdict(query, state, config, safety, verdict)

    return {}


async def _apply_verdict(
    query: str,
    state: Mapping[str, Any],
    config: RunnableConfig | None,
    safety: SafetyConfig,
    verdict: SafetyVerdict,
) -> dict[str, Any]:
    """分类裁决落地：pass → {}；sanitize → 脱敏返回；reject（仅 danger|profanity）→
    拒答路径。组合违规防御：reject+pii 归一 sanitize + warning（pii 一律 sanitize）；
    reject+none 视同 pass（拿不准一律放行）。"""
    if verdict.action == "pass":
        return {}
    if verdict.action == "sanitize":
        return _sanitize_update(query, verdict.category)
    # action == "reject"
    if verdict.category in ("danger", "profanity"):
        raise await _reject(state, config, safety, verdict.category)
    if verdict.category == "pii":
        AgentLog(
            message=(
                "safety_check 分类组合违规（reject+pii，契约禁止）——防御归一为 sanitize "
                "路径（category=pii 一律 sanitize 不得 reject）"
            ),
            graph="safety",
            node="safety_check",
        ).warning()
        return _sanitize_update(query, "pii")
    return {}  # reject+none 视同 pass（宁可漏判不可误伤）
