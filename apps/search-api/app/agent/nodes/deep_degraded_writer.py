"""degraded_writer 节点（M2 批次 4——ARCH §4.2 degraded_writer 行 / DEV §5.4 五步闭环）。

熔断降级报告撰写（强模型**单调用**）：仅 timeout/budget 触发（guards 条件边
`route_guards` 两出口——ARCH §4.2:475-480，正常轮结构性不可达本节点）；已收集内容
（subagent_reports + 来源元数据）降级报告 + 显著标注「研究未完成」；`degraded_reason`
落 turns（TurnRunner _finish_completed 分流 done(finish=degraded, degraded_reason=同值)
零 error——api.md §7.2:803/:845 v1.21 注；turn_service.py:1441-1454 批次 1 已接）。

**一次 LLM 调用 + 降级包络（ARCH :514「超时包络 30s」）**：ainvoke 单调用（非逐字流
——盘点 decisions[4]，不扩 _messages_gate），asyncio.timeout 包络 =
**min(30, max(remaining−15, 5))**（guards trip 判据=remaining ≤ 30+15 使本节点总有
窗口；包络随剩余收缩并有 5s 下限兜底，非恒等 30s——权威注释见 deep_guards.py 常量
定义与本文件 envelope 计算处)；**thinking off**（包络约束下最大化产出概率——一次性
压缩产出不需要思维链，偏离 deep 档 on 缺省属包络
驱动决策）；调用失败/超时/空输出 → 纯机械来源速览兜底（零 LLM——fast digest 同款
转义纪律）+ errors[] 留痕，不终止（降级路径恒有产出）。

**「研究未完成」显著标注（REQ-014/A2；ARCH :514）**：机械前置说明行（代码常量——
fast digest `_DIGEST_PREAMBLE` 先例，零 LLM 不可被模型吞掉）+ 提示词 `reason_note`
注入（模型如实呈现缺口）；前端显著标注另由 turns.status=degraded 承载（api.md
§2.5:262 DegradedBanner——本标注为内容内 belt-and-suspenders）。

**降级路径直通 END（ARCH §4.2:480 DG --> E）**：不进 finalize——引用越界剔除由本
节点内联承载（`_normalize_citations`/`_ledger_refnos` 复用 finalize 单一事实源，
有效性=账本 ref_no 集合——与 finalize 同语义）；**write done 帧由本节点自发（裸帧
零 extra）**——降级报告非分节正文，批次 3 reflect done 裸帧先例；writer 路径的
done 帧归 finalize（批次 4 接线）——write done 帧是否携带 extra.sections 即两路径
的可观测判别面。

**事件面（盘点 decisions[4]）**：stage(write,running,label=撰写降级报告) →
emit_fallback_token 整块单事件（publish-only 不落库——turn_service.py:2301-2307
既有翻译面零改动，fast digest v1.59 先例）→ stage(write,done) 裸帧。不走 messages
流（R14③ 门控零改动——单调用非逐字流）。

**提示词（nacos-config.md §1 deep.degraded_writer 行，批次 4 增量键）**：六占位符
{reason_note, query_lang, cite_rules, memory_block, subagent_reports_block,
sources_block}——cite_rules 承载 common.citation_rules（fast.synthesis 同款先例）；
memory_block M5 前恒空串（synthesis.py:451 先例）；材料面复用 writer 装配器单一
事实源（_subagent_reports_block/_sources_block——外部内容包裹铁律同构），
fuse_top_n 截断同款；不读 outline（降级报告按材料组织非按大纲成文）。

**材料面（visited_urls 定案延续）**：只读 subagent_reports + collected_sources——
visited_urls「已读」语义零依赖（批次 4 binding 定案延续）。

usage_total 终值回填（注入面可选——finalize 同款；本路径无 finalize，guards 快照
之后由本节点补终值）；D2：本节点零写 degraded/degraded_reason（降级单一事实源=
guards 置位，本节点只消费）。取消感知：ensure_not_canceled 入口接线（R14②——命中
即抛穿透，TurnRunner _finish_canceled 收尾，取消不进降级 ADR-010）。update 键 ⊆
DeepState.__annotations__（R14①）。节点纯函数边界：模型工厂可注入可 mock——单测
全 mock 零网络；config 全程透传（Langfuse/UsageCollector 挂载依赖）。
"""

import asyncio
from collections.abc import Mapping
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.agent.guards import ensure_not_canceled
from app.agent.graphs.signals import emit_fallback_token, emit_stage
from app.agent.llm import build_chat_model, resolve_chat_alias
from app.agent.nodes.deep_finalize import _ledger_refnos, _normalize_citations
from app.agent.nodes.deep_guards import (
    DEGRADED_WRITER_ENVELOPE_S,
    TRIP_MARGIN_S,
    _MIN_ENVELOPE_S,
)
from app.agent.nodes.deep_writer import (
    _subagent_reports_block,
    _sources_block,
    _sources_top_n,
)
from app.agent.nodes.synthesis import (
    _DIGEST_SNIPPET_MAX_CHARS,
    _DIGEST_TITLE_MAX_CHARS,
    _digest_lang,
    _escape_digest_text,
    _text_of,
)
from app.agent.prompts import render_prompt, resolve_prompt
from app.core.logger import AgentLog

__all__ = ["degraded_writer"]  # route_guards 归 deep_guards（路由与检查点同模块单一事实源）

#: 机械前置说明行（(lang) → 模板；label 经 _reason_label 注入——代码常量零 LLM，
#: fast digest _DIGEST_PREAMBLE 先例；「研究未完成」显著标注内容内承载面）
_DEGRADED_PREAMBLE: dict[str, str] = {
    "zh": "⚠️ 研究未完成（{label}）：以下为基于已收集材料的阶段性报告，内容可能不完整。",
    "en": (
        "⚠️ Research incomplete ({label}): the following is a partial report "
        "based on the material collected so far; it may be incomplete."
    ),
}

#: 中断原因标签（(lang, degraded_reason) → 展示文本；未知 reason 防御回落「中断」）
_REASON_LABELS: dict[tuple[str, str | None], str] = {
    ("zh", "timeout"): "轮次超时",
    ("zh", "budget"): "研究预算用尽",
    ("zh", None): "研究中断",
    ("en", "timeout"): "time limit reached",
    ("en", "budget"): "token budget exhausted",
    ("en", None): "interrupted",
}

#: 机械速览来源条数上限（fast digest _DIGEST_MAX_SOURCES 同值——OQ-6 代码常量先例）
_FALLBACK_MAX_SOURCES = 5


def _reason_label(lang: str, degraded_reason: Any) -> str:
    """中断原因展示标签（防御：未知/缺失 reason 回落「中断」态）。"""
    reason = degraded_reason if isinstance(degraded_reason, str) else None
    return _REASON_LABELS.get((lang, reason)) or _REASON_LABELS[(lang, None)]


def _mechanical_digest(sources: Any) -> str:
    """来源速览装配器（纯机械零 LLM——writer 失败兜底同款纪律：[n]=ref_no 账本、
    ≤5 条、title/snippet 转义 + 半角 [ ] 全角化防 [n] 伪造）。"""
    lines: list[str] = []
    ledger = sorted(
        (item for item in (sources or []) if isinstance(item, Mapping)),
        key=lambda item: int(item["ref_no"]) if isinstance(item.get("ref_no"), (int, float)) else 0,
    )
    for item in ledger[:_FALLBACK_MAX_SOURCES]:
        ref_no = item.get("ref_no")
        label = f"[{int(ref_no)}]" if isinstance(ref_no, (int, float)) else "-"
        raw_title = str(item.get("title") or "")[:_DIGEST_TITLE_MAX_CHARS]
        raw_url = str(item.get("url") or "")[:_DIGEST_TITLE_MAX_CHARS]
        title = _escape_digest_text(raw_title) or _escape_digest_text(raw_url)
        snippet = _escape_digest_text(str(item.get("snippet") or "")[:_DIGEST_SNIPPET_MAX_CHARS])
        lines.append(f"- {label} {title}：{snippet}" if snippet else f"- {label} {title}")
    return "\n".join(lines)


async def degraded_writer(state: Any, config: RunnableConfig | None = None) -> dict[str, Any]:
    """degraded_writer 节点（deep 子图，仅 trip 路径可达）：单调用降级报告 →
    answer_md/report_md；失败机械兜底不终止。"""
    ensure_not_canceled(config)  # 取消非故障——穿透重抛（ADR-010：取消不进降级报告）

    state_map = state if isinstance(state, Mapping) else {}
    spec = state_map.get("spec")
    spec = spec if isinstance(spec, Mapping) else {}
    sources = state_map.get("collected_sources")
    lang = _digest_lang(state_map.get("query_lang"))
    label = _reason_label(lang, state_map.get("degraded_reason"))

    prompt_text = render_prompt(
        resolve_prompt("deep.degraded_writer"),
        {
            "reason_note": label,
            "query_lang": str(state_map.get("query_lang") or "en"),
            "cite_rules": resolve_prompt("common.citation_rules"),
            # M5 记忆→prompt 通道；启用前恒空（memory-api.md §2——synthesis 先例）
            "memory_block": "",
            "subagent_reports_block": _subagent_reports_block(state_map.get("subagent_reports")),
            "sources_block": _sources_block(sources, _sources_top_n(spec)),
        },
    )

    alias = resolve_chat_alias(state, config)
    # thinking off：30s 包络约束（一次性压缩产出不需要思维链——偏离 deep 档 on 缺省，
    # 包络驱动决策，模块 docstring 钉注）
    chain = build_chat_model(alias, thinking="off")

    # 事件面入口帧（盘点 decisions[4]——write 段时间线 running→done 完整形态）
    emit_stage("write", "running", label="撰写降级报告")

    text = ""
    llm_failed = False
    # 包络=min(ENVELOPE, remaining−MARGIN)（批次 4 评审 P1——包络与外层轮级
    # asyncio.timeout 零裕量竞态消除：trip 判据已加裕量〔remaining ≤ 30+15s〕，
    # 本节点再按剩余预算收窄包络，慢 LLM 撞满由节点内包络先收口进机械兜底，
    # 不再被外层轮级超时截杀成 error(500301)——api.md §7.2:845 图内熔断零
    # error 形态；无注入面/异常 → 固定 30s，防御下限 5s 保机械兜底窗）
    envelope = DEGRADED_WRITER_ENVELOPE_S
    deadline = (config or {}).get("configurable", {}).get("turn_deadline")
    if deadline is not None:
        try:
            envelope = min(
                DEGRADED_WRITER_ENVELOPE_S,
                max(float(deadline.remaining_s()) - TRIP_MARGIN_S, _MIN_ENVELOPE_S),
            )
        except Exception:  # noqa: BLE001 —— deadline 异常视同包络满额（guards 同款）
            envelope = DEGRADED_WRITER_ENVELOPE_S
    try:
        async with asyncio.timeout(envelope):
            result = await chain.ainvoke(prompt_text, config=config)
            text = _text_of(result)
    except Exception as exc:  # noqa: BLE001 —— 单调用包络内一切失败归兜底（降级路径
        # 恒有产出；CancelledError 属 BaseException 不被吞——取消传播不受影响）
        llm_failed = True
        AgentLog(
            message=(
                f"degraded_writer LLM 调用失败（进机械兜底）："
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
            graph="deep",
            node="degraded_writer",
        ).warning()

    body = text.strip()
    update: dict[str, Any] = {}
    if not body or llm_failed:
        body = _mechanical_digest(sources)
        update["errors"] = [{"stage": "write", "reason": "degraded_writer_fallback"}]

    answer = f"{_DEGRADED_PREAMBLE[lang].format(label=label)}\n\n{body}"

    # 引用越界剔除内联承载（降级路径直通 END 不进 finalize——ARCH §4.2:480；
    # 有效性=账本 ref_no 集合，与 finalize 同语义单一事实源）
    valid = _ledger_refnos(sources)
    answer, dropped = _normalize_citations(answer, valid)
    if dropped:
        update.setdefault("errors", []).append(
            {"stage": "write", "reason": "citation_out_of_range", "dropped": dropped}
        )
        AgentLog(
            message=f"degraded_writer 引用越界剔除 {dropped} 处（finalize 不在本路径）",
            graph="deep",
            node="degraded_writer",
        ).warning()

    update["answer_md"] = answer
    update["report_md"] = answer  # 报告正文双键等值（writer/finalize 产出键语义延续）

    # 整块单事件发射（fast digest v1.59 先例——publish-only 不落库，前端拼接渲染零新路径）
    emit_fallback_token(answer)
    # write done 裸帧收束（降级报告非分节正文——零 extra；writer 路径的 done 带
    # extra.sections 由 finalize 发——两路径可观测判别面）
    emit_stage("write", "done")

    # usage_total 终值回填（注入面可选——本路径无 finalize，guards 快照之后补终值）
    configurable = (config or {}).get("configurable") or {}
    collector = configurable.get("usage_collector")
    if collector is not None:
        try:
            total = collector.total()
        except Exception:  # noqa: BLE001 —— collector 异常视同零用量（guards 同款）
            total = {}
        update["usage_total"] = int(total.get("total_tokens") or 0)
    return update
