"""writer 节点（M2 批次 4——ARCH §4.2 writer 行 / DEV §5.4 五步闭环）。

深度报告撰写（强模型，自由文本流式不用结构化输出——S15 备注明列）：按 outline 分节
流式成文，只消费 subagent_reports（≤1200 tok 结构化摘要）与全量来源元数据，不直接读
网页原文（上下文收敛手段 §6.5）。token/thinking 增量经 LangGraph messages 流冒泡
（TurnRunner messages 过滤映射——仅 deep 顶层 **writer** 节点帧放行，§6.1/R14③；
**节点名注册为 "writer"** 即门控判据 metadata.langgraph_node=="writer" 的对齐面，
stage_key="write" 为 emit_stage 入参与节点名解耦），节点只累积 text → answer_md。

**事件面（api.md §7.4:867 / REQ-014「正在撰写第 x 节」）**：入口
stage(write,running,label=撰写第 1 节) → token 流（messages 流域——节点零 custom
token，深桩期 emit_token 形态已废弃 signals.py:13-24）→ 分节标题（markdown 标题行
含 outline 章节名）出现时推进 running(label=撰写第 x 节)。**write done 帧归
finalize 节点唯一发射**（批次 4 finalize 接线——盘点 decisions[3]：writer 只发
running 推进帧，规避双 done 帧观感；done extra.sections/citation_dropped 归
finalize）。目录列表行不误触发（仅标题行匹配）。

**材料面（ARCH §4.2:513；visited_urls 定案 binding——批次 4 首项）**：
- **只读 subagent_reports + collected_sources 来源元数据——visited_urls「已读」
  语义零依赖**（visited_urls=已读 URL 全局登记 ARCH §4.2:499/:511；来源账本=
  collected_sources ref_no 首现冻结面）；
- 来源块逐条 `<external_content source="{url}" type="web">` 包裹 + title/snippet
  html.escape（synthesis._sources_block 同构——转义硬边界：包裹标记不可被内容击穿）；
  ref_no 即 [n] 引用编号（首现冻结账本直取）；spec.deep.fuse_top_n 截断（缺省 10，
  nacos-config.md §3 deep 节初值）；
- 子代理报告：成功摘要 `_neutralize_content` + 包裹（deep_reflect 材料块同构，
  kind="web" source="subagent_report:{subtopic}"）；失败报告渲染我方状态行零包裹
  （`[研究失败] 子课题…`——reflect :305 先例）；摘要块防御截断 _SUMMARY_MAX_CHARS
  （**批次 3 接受项 P3-2 复核落码**：上游 SummarySchema summary_max_tokens=1200 token
  兜底之外的材料面显式常量，_MATERIAL_LIST_CAP 先例同构——reflect 侧同步同值）；
- 六占位符模板消费（deep.report_writer）：sections_hint（outline 逐节标题+intent）/
  query_lang / cite_rules（=resolve_prompt("common.citation_rules")，fast.synthesis
  同款先例）/ memory_block（M5 前恒空串，synthesis.py:451 先例）/
  subagent_reports_block / sources_block。

**失败语义（沿 reflect 兜底先例——盘点钉：单调用面无重试循环、兜底不终止）**：
LLM 失败（传输/超时/空输出）→ 有可用部分正文走句界 partial（_clip_to_sentence_boundary
——残句丢弃、不伪造补全，fast partial 收束先例），否则纯机械来源速览（零 LLM，deep
说明行 + [n]=ref_no 来源行 ≤5 条 + 转义纪律 _escape_digest_text——fast digest 同款）
→ errors[] 留痕 {stage:"write", reason:"llm_call_failed"}；**兜底不置 degraded/
degraded_reason**（D2：deep 降级单一事实源=guards 的 degraded_reason，api.md §7.2:803
枚举 timeout|budget 不含节点失败语义）；图内零 error 帧（done 帧归 finalize——兜底
标记经 errors 传递，finalize 据此发 done extra.sections=0）。
单调用超时包络 asyncio.timeout(spec.deep.llm_timeout_s，缺省 60——§6.2 分级包络，
无包络则单次挂起吃光轮预算)。

**引用规范化边界**：越界剔除/引用统一编号归 **finalize** 节点（ARCH §4.2:477——
下一闭环接线；本节点产 raw 报告正文，report_md 镜像亦归 finalize 定案）。

纯函数边界：节点无 DB/EventBus；提示词经 AssetCenter 内存快照解析（未启动回落内置
默认）；LLM 工厂可注入可 mock——单测全 mock 零网络；config 全程透传（Langfuse
callbacks/UsageCollector 挂载依赖）。取消感知：ensure_not_canceled 入口接线（R14②，
同 planner/research/reflect 先例）。
"""

import asyncio
import html
import re
from collections.abc import Mapping
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.agent.external_content import external_content
from app.agent.graphs.signals import emit_stage
from app.agent.guards import TurnCanceledSignal, ensure_not_canceled, llm_timeout_s
from app.agent.llm import build_chat_model, resolve_chat_alias, resolve_thinking
from app.agent.nodes.synthesis import (
    _DIGEST_SNIPPET_MAX_CHARS,
    _DIGEST_TITLE_MAX_CHARS,
    _clip_to_sentence_boundary,
    _digest_lang,
    _escape_digest_text,
    _text_of,
)
from app.agent.prompts import render_prompt, resolve_prompt
from app.agent.tools.url_reader import _neutralize_content
from app.core.logger import AgentLog

__all__ = ["DEFAULT_SOURCES_TOP_N", "SUMMARY_MAX_CHARS", "writer"]

#: spec.deep.fuse_top_n 缺失/非法回落（nacos-config.md §3 deep 节契约初值 10）
DEFAULT_SOURCES_TOP_N: int = 10

#: 子代理报告摘要块防御截断（字符）——批次 3 接受项 P3-2 复核落码：上游
#: SummarySchema summary_max_tokens=1200（token 口径）兜底之外的材料面显式常量
#: （deep_reflect._MATERIAL_LIST_CAP 先例同构；deep_reflect 侧同步同值）
SUMMARY_MAX_CHARS: int = 1200

#: markdown 标题行（分节 label 推进锚点——仅标题行匹配，目录列表行不误触发；
#: 捕获组为标题文本，含 outline 章节名即判定该节开写）
_HEADING_RE = re.compile(r"(?m)^#{1,6}\s+(.+?)\s*$")

#: 兜底速览说明行（deep 语义——query_lang 驱动双语；fast digest 前例同构、措辞按
#: 报告语境改写，零 LLM 机械装配）
_REPORT_DIGEST_PREAMBLE: dict[str, str] = {
    "zh": "报告撰写未能完成（生成超时或失败），以下是本次研究收集到的来源速览，可先参考来源面板获取完整信息。",
    "en": (
        "The report could not be completed (generation timed out or failed). "
        "Here is an overview of the sources collected in this round; see the "
        "sources panel for details."
    ),
}

#: 兜底速览来源条数上限（fast digest _DIGEST_MAX_SOURCES 同值——OQ-6 代码常量先例）
_FALLBACK_MAX_SOURCES = 5


def _deep_section(spec: Any) -> Mapping[str, Any]:
    """spec.deep 节（结构非法视同缺失——guard 不抛口径）。"""
    if isinstance(spec, Mapping):
        section = spec.get("deep")
        if isinstance(section, Mapping):
            return section
    return {}


def _sources_top_n(spec: Any) -> int:
    """spec.deep.fuse_top_n 解析（非法/非正回落缺省 10——guard 不抛口径）。"""
    raw = _deep_section(spec).get("fuse_top_n")
    try:
        value = int(raw)  # type: ignore[arg-type]
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    return DEFAULT_SOURCES_TOP_N


def _clean_outline(outline: Any) -> list[dict[str, str]]:
    """大纲归一（planner 产出的防御面）：[{section, intent}] 逐节 strip、空白节丢弃、
    保序；非 list/条目非 dict 视同空大纲（入口 label 回落「撰写报告」）。"""
    if not isinstance(outline, list):
        return []
    cleaned: list[dict[str, str]] = []
    for item in outline:
        if not isinstance(item, Mapping):
            continue
        section = str(item.get("section") or "").strip()
        intent = str(item.get("intent") or "").strip()
        if not section and not intent:
            continue
        cleaned.append({"section": section, "intent": intent})
    return cleaned


def _sections_hint(outline: list[dict[str, str]]) -> str:
    """章节规划投影（{{sections_hint}} 变量）：逐节「第 x 节「标题」——intent」行。"""
    lines = [
        f"第 {index} 节「{item['section']}」——{item['intent']}"
        for index, item in enumerate(outline, start=1)
    ]
    return "\n".join(lines)


def _subagent_reports_block(reports: Any) -> str:
    """子课题报告块装配（{{subagent_reports_block}} 变量）。

    成功报告 = 外部内容派生 → 摘要防御截断（SUMMARY_MAX_CHARS）→ _neutralize_content
    → `<external_content source="subagent_report:{subtopic}" type="web">` 包裹
    （deep_reflect 材料块同构）；失败报告/空摘要 = 我方状态行零包裹（[研究失败]
    ——reflect :305 先例，失败隔离语义直达撰写面）。
    """
    blocks: list[str] = []
    if not isinstance(reports, list):
        return ""
    for report in reports:
        if not isinstance(report, Mapping):
            continue
        subtopic = str(report.get("subtopic") or "")
        summary = str(report.get("summary") or "")
        if report.get("failed") or not summary.strip():
            blocks.append(f"[研究失败] 子课题「{subtopic}」：无可用摘要（子代理失败隔离）。")
            continue
        body = f"[研究摘要] 子课题「{subtopic}」：{summary[:SUMMARY_MAX_CHARS]}"
        blocks.append(
            external_content(
                _neutralize_content(body), source=f"subagent_report:{subtopic}", kind="web"
            )
        )
    return "\n".join(blocks)


def _sources_block(sources: Any, top_n: int) -> str:
    """来源块装配（{{sources_block}} 变量；synthesis._sources_block 同构）。

    ref_no 账本序取前 top_n 条（collected_sources 即 ref_no 首现冻结序——防御面
    按 ref_no 升序排序后截断）；逐条 `<external_content source="{url}" type="web">`
    包裹，[n]=ref_no（与来源卡编号对齐——引用统一编号去重归 finalize）；
    title/snippet html.escape(quote=True)（content 侧转义硬边界，属性侧由 helper 承担）。
    """
    if not isinstance(sources, list):
        return ""
    ledger = sorted(
        (item for item in sources if isinstance(item, Mapping)),
        key=lambda item: int(item["ref_no"]) if isinstance(item.get("ref_no"), (int, float)) else 0,
    )[:top_n]
    blocks: list[str] = []
    for item in ledger:
        title = html.escape(str(item.get("title") or "")[:_DIGEST_TITLE_MAX_CHARS], quote=True)
        snippet = html.escape(
            str(item.get("snippet") or "")[:200], quote=True
        )
        ref_no = item.get("ref_no")
        label = f"[{int(ref_no)}]" if isinstance(ref_no, (int, float)) else "-"
        blocks.append(
            external_content(
                f"{label} {title}\n{snippet}",
                source=str(item.get("url") or ""),
                kind="web",
            )
        )
    return "\n".join(blocks)


def _heading_titles(buffer: str) -> set[str]:
    """已出现缓冲区中的 markdown 标题文本集（分节 label 推进检测面）。"""
    return {match.group(1) for match in _HEADING_RE.finditer(buffer)}


def _fallback_answer(query_lang: Any, sources: Any, partial: str) -> str:
    """兜底正文装配（失败语义收束——纯机械零 LLM）。

    有可用部分正文（句界清理非空）→ partial 优先（fast partial 收束先例——残句丢弃
    不伪造补全）；否则来源速览 digest（deep 说明行 + [n]=ref_no 来源行 ≤5 条；
    title/snippet 经 _escape_digest_text——markdown 控制字符转义 + 半角 [ ] 全角化，
    [n] 伪造防御与 fast digest 同款纪律）。
    """
    clipped = _clip_to_sentence_boundary(partial)
    if clipped:
        return clipped
    lang = _digest_lang(query_lang)
    lines: list[str] = [_REPORT_DIGEST_PREAMBLE[lang], ""]
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


async def writer(state: Any, config: RunnableConfig | None = None) -> dict[str, Any]:
    """writer 节点（deep 子图）：分节流式撰写 → answer_md；失败走兜底不终止。"""
    ensure_not_canceled(config)  # 取消非故障——穿透重抛（TurnRunner _finish_canceled 收尾）

    spec = state.get("spec") if isinstance(state, dict) else None
    spec = spec if isinstance(spec, Mapping) else {}
    outline = _clean_outline(state.get("outline") if isinstance(state, dict) else None)
    top_n = _sources_top_n(spec)
    timeout_s = llm_timeout_s(spec, "deep")  # spec.deep.llm_timeout_s，缺省 60（§6.2）

    prompt_text = render_prompt(
        resolve_prompt("deep.report_writer"),
        {
            "sections_hint": _sections_hint(outline),
            "query_lang": str(state.get("query_lang") or "en") if isinstance(state, dict) else "en",
            "cite_rules": resolve_prompt("common.citation_rules"),
            # M5 记忆→prompt 通道；启用前恒空（memory-api.md §2——synthesis 先例）
            "memory_block": "",
            "subagent_reports_block": _subagent_reports_block(
                state.get("subagent_reports") if isinstance(state, dict) else None
            ),
            "sources_block": _sources_block(
                state.get("collected_sources") if isinstance(state, dict) else None, top_n
            ),
        },
    )

    alias = resolve_chat_alias(state, config)
    # deep 档缺省 on（spec.deep.thinking 热更可切 off）——planner/reflect/writer 同档
    # （v1.59 增量 C；OQ-34：deep 全节点 Standard 档经 chat_model_key 解析）
    chain = build_chat_model(alias, thinking=resolve_thinking(spec, "deep"))

    # 事件面入口帧（api.md §7.4:867）：有大纲标「撰写第 1 节」、退化回落「撰写报告」
    entry_label = "撰写第 1 节" if outline else "撰写报告"
    emit_stage("write", "running", label=entry_label)

    accumulated: list[str] = []
    pending = 1  # 下一个待检测分节（0-based；入口帧已覆盖第 1 节）
    outline_titles = [item["section"] for item in outline if item["section"]]
    llm_failed = False
    try:
        async with asyncio.timeout(timeout_s):
            async for chunk in chain.astream(prompt_text, config=config):
                accumulated.append(_text_of(chunk))
                # 分节 label 推进（REQ-014「正在撰写第 x 节」）：标题行含下一节名即
                # 判定该节开写（仅标题行匹配——目录列表行不误触发）
                if pending < len(outline_titles):
                    seen = _heading_titles("".join(accumulated))
                    while pending < len(outline_titles) and outline_titles[pending] in seen:
                        emit_stage("write", "running", label=f"撰写第 {pending + 1} 节")
                        pending += 1
    except TurnCanceledSignal:
        raise  # 取消非故障——穿透重抛（TurnRunner _finish_canceled 收尾）
    except Exception as exc:  # noqa: BLE001 —— 沿 reflect 兜底先例：传输/超时类失败
        # 不终止不置 degraded，errors[] 留痕 + 兜底产出（D2：降级单一事实源=guards）
        llm_failed = True
        AgentLog(
            message=(
                f"writer LLM 调用失败（进兜底产出）："
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
            graph="deep",
            node="writer",
        ).warning()

    text = "".join(accumulated)
    if not text or llm_failed:
        text = _fallback_answer(
            state.get("query_lang") if isinstance(state, dict) else None,
            state.get("collected_sources") if isinstance(state, dict) else None,
            text,
        )
        update: dict[str, Any] = {
            "answer_md": text,
            # 兜底标记（finalize 据此发 done extra.sections=0——writer 失败兜底轮
            # 零节成文诚实留痕；errors Annotated[add] 增量）
            "errors": [{"stage": "write", "reason": "llm_call_failed"}],
        }
    else:
        update = {"answer_md": text}
    # write done 帧归 finalize 唯一发射（批次 4 接线——盘点 decisions[3]；writer
    # 只发 running 推进帧，本节点收口于正文产出）
    return update
