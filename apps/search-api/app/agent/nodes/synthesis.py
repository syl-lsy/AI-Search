"""synthesis 节点（B3 节点 5/5——ARCH §4.1 synthesis 行 / §6.4.3 包裹铁律 / §6.5 上下文口径）。

chat 模型（chat_model_key → 轮内映射快照 chat.options 别名）流式生成 Markdown 答案：
token/thinking 增量经 LangGraph messages 流冒泡（TurnRunner messages 过滤映射——仅 fast
synthesis 节点放行，§6.1/R14③），节点只累积 text → answer_md。

- **硬上限**：spec fast.max_answer_chars 兜底截断（§4.1「≤3000 字上限」——软引导归提示
  词，硬截断归代码）；
- **引用越界剔除**：[n] 越界（n<1 或 n>来源数）标记移除——不伪造编号（citation_rules
  第 1/3 条）；
- **最小检索分支**：answerable_from_knowledge=true → 提示词确定性切「先知识后取证」
  （组织方式段行级切换，键位与占位符集零变更——§4.1「提示词切」）；
- **免检索直答分支（v1.44 B9④）**：search_strategy=direct → 切「免检索直答」段
  （无 sources_block、无 [n] 义务、含误判兜底句；空 fused 不触发 500201 防御短路，
  [n] 经越界剔除全量清空——nacos-config.md §1 v1.20）；verify/web 轮沿用 answerable
  两段切换（回滚态行为零变化）；
- **回顾整理分支（v1.51）**：search_strategy=recall → 切「- 回顾整理路径」段（行级
  第四段），`{{history_block}}` 注入 `history_recall` 骨架块（ARCH §6.5 双通道）——
  **仅本变体非空**（direct/verify/web 恒空串，token 纪律）；零 sources 场景同 direct
  （越界剔除全量清空 [n]）；模板缺该段时回落 direct 形态（灰度安全）；
- **外部内容包裹**（§6.4.3 铁律）：来源逐条 `<external_content source="{url}" type="web">`
  包裹（声明行在提示词内）；历史块/memory_block 为**声明行软防线**（模型转述产物，
  低风险层级——P2 裁决）；memory_block M5 前恒空串；
- **失败语义（v1.59 增量 A 重写收束面）**：全源失败防御性短路（主短路在 merge_dedupe）；
  LLM 失败按**错误三档分级**重试（B11 P1a / ARCH §6.2 v1.49）——连接类（httpx transport /
  单调用包络超时 / openai 连接形态）走 `spec.fast.retry.connection` 长窗指数退避
  （1/2/4/8，总窗 total_window_s 硬闸，与 TurnDeadline 双闸先到先放弃）；5xx 类沿用
  `spec.fast.retry` 主节短窗；业务类（4xx/结构化解析失败）不重试。**终局失败二分**：
  - **fused 非空**（有来源可用）→ 抛控制流信号 `SynthesisFallback`（不抛 SynthesisError）：
    有可用部分正文（跨尝试最长前缀经句界清理）走 partial；否则走 digest（纯机械来源速览，
    单事件整块 token 发射）——TurnRunner 零 error 事件收束 done(finish=degraded,
    degraded_reason=null)（api.md §7.4 兜底收束形态；ARCH §6.2 v1.59 兜底产出注）；
  - **零源轮**（direct/recall/防御性短路漏网）→ `SynthesisError` → TurnRunner error 500101
    （语义收窄为零源专属，message「综合生成失败，请稍后重试」）+ 流结束（无 done）。
- **单调用超时包络**（决策 4）：每次 astream 包 `asyncio.timeout(min(llm_timeout_s,
  剩余轮预算))`——无包络时单次挂起会直接吃光轮预算、重试窗形同虚设。

stage(write, running, label=综合中 → done) 夹全部动作（api.md §7.4）。节点纯函数：模型
工厂可注入可 mock，零 DB/EventBus。
"""

import asyncio
import html
import random
import re
import time
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from app.agent.external_content import external_content
from app.agent.guards import (
    RetryWindow,
    TurnDeadline,
    classify_failure_kind,
    classify_retry_tier,
    connection_retry_policy_from_spec,
    ensure_not_canceled,
    llm_timeout_s,
    retry_policy_from_spec,
)
from app.agent.graphs.signals import emit_fallback_token, emit_stage
from app.agent.llm import build_chat_model, resolve_chat_alias, resolve_thinking
from app.agent.prompts import render_prompt, resolve_prompt
from app.agent.tools.registry import SearchChainUnavailable
from app.utils.history_recall import render_history_recall

__all__ = ["SynthesisError", "SynthesisFallback", "build_chat_model", "synthesis"]

_CITATION_RE = re.compile(r"\[(\d+)\]")

#: 组织方式段行前缀（确定性切分的锚点——键位与占位符集零变更）
_REGULAR_LINE_PREFIX = "- 常规路径"
_KNOWLEDGE_FIRST_LINE_PREFIX = "- 最小检索路径"
_DIRECT_LINE_PREFIX = "- 免检索直答路径"  # v1.20 第三变体段（direct 免检索直答轮）
_RECALL_LINE_PREFIX = "- 回顾整理路径"  # v1.51 第四变体段（recall 回顾整理轮）

#: 检索策略四值（v1.44 B9 三值 / v1.51 +recall——缺省/非法一律按 web 处理，灰度安全）
_SEARCH_STRATEGIES = ("direct", "verify", "web", "recall")

#: 单调用超时包络下限（秒）：剩余轮预算为 0 时仍留一次极短尝试，让重试闸门裁决
_MIN_CALL_TIMEOUT_S = 0.001

#: 退避等待（测试注入点：单测断言退避序列/总窗闸时替换；生产恒 asyncio.sleep）
_sleep = asyncio.sleep

#: 单调钟（测试注入点：RetryWindow 总窗闸的确定性断言）
_clock = time.monotonic


class SynthesisError(Exception):
    """综合生成失败——**500101 语义收窄后仅零源轮形态**（fused 空且终局失败）。

    TurnRunner 捕获 → error 500101 + 直接关闭（无 done：零源轮无部分内容可降级，
    api.md §7.1 v1.10 例外条）。fused 非空轮的一切终局失败走 `SynthesisFallback`。
    """


class SynthesisFallback(Exception):
    """synthesis 终局失败但 fused 非空的**控制流信号**（v1.59 增量 A，非故障）。

    沿 `TurnCanceledSignal` / `ContentRejectedError` 信号族先例：节点内已完成兜底产出
    （digest 机械速览 / partial 句界清理正文）并发射 `stage(write, done, extra.fallback)`，
    再上抛本信号——TurnRunner 捕获后走 `_finish_synthesis_fallback`：落 answer_md +
    done(finish=degraded, degraded_reason=null)、**零 error 事件**（api.md §7.4 v1.25）。
    为什么不走 state.degraded：`_finish_completed` 对 fast degraded=True 一律派生
    degraded_reason=source_failure（ARCH §6.2 v1.41 注②），兜底轮 sources 完好、该值是误标。

    字段：
    - `answer_md`：兜底全文（digest 全文 / 句界清理后的部分正文）；
    - `kind`：∈ {synthesis_digest, synthesis_partial}（stage extra 与 M4 采样判据同源）；
    - `failure`：终局失败类别 ∈ {timeout, connection, non_retryable, llm_call_failed}
      （轮末 AgentLog 留痕）。前三值由本节点 `_raise_terminal_failure` 经
      `classify_failure_kind(last_error)` 分类产出（流内异常分类）；`llm_call_failed`
      仅由 TurnRunner 的 **deep regenerate 分流**构造（turn_service `_run_regenerate_graph`
      ——deep writer 兜底轮的失败类别来自图内 `errors[]` 标记而非流内异常，无异常可分类，
      固定标注 llm_call_failed，语义=writer 阶段 LLM 调用失败）；
    - `attempts`：物理尝试次数；`source_count`：fused 来源条数（digest 截断前总量）；
    - `fused`：供给面 fused（含 REQ-017 父轮扩展卡，与成功路径返回值同口径）；仅
      parented fast 轮的终局失败携带——TurnRunner `_finish_synthesis_fallback` 据此补建
      父轮扩展卡 sources 行（[N+1..] 引用不悬空）；None=无补行面（非父轮/deep/regenerate）。
    """

    def __init__(
        self,
        *,
        answer_md: str,
        kind: str,
        failure: str,
        attempts: int,
        source_count: int,
        fused: list[Any] | None = None,
    ) -> None:
        self.answer_md = answer_md
        self.kind = kind
        self.failure = failure
        self.attempts = attempts
        self.source_count = source_count
        self.fused = fused
        super().__init__(
            f"synthesis 兜底收束（kind={kind}, failure={failure}, attempts={attempts}）"
        )


#: 句末标点（partial 句界清理锚点——api.md §7.4「截断到最近句末标点（。！？!?.）或换行」）
_SENTENCE_END_CHARS = frozenset("。！？!?.")
# 注：句点 `.`/`!`/`?` 为英文形态；中文全角标点单字符即句界。

#: digest 结构常量（机械兜底无热更诉求——OQ-6 IMAGES_MAX_COUNT 先例，api.md §7.4）
_DIGEST_MAX_SOURCES = 5
#: snippet 截断（契约明文 ≤120 字符）
_DIGEST_SNIPPET_MAX_CHARS = 120
#: title 截断（安全审计 P3：与 snippet 对称的显式上限——上游 API/DB String(512) 只是
#: 隐性约束，超长标题会撑爆单行列表并挤占展示预算）
_DIGEST_TITLE_MAX_CHARS = 120

#: digest 说明行（query_lang 驱动双语）
_DIGEST_PREAMBLE: dict[str, str] = {
    "zh": (
        "综合回答未能完成（生成超时或失败），以下是本次检索到的来源速览，"
        "可先参考；稍后可点击「重新生成」重试。"
    ),
    "en": (
        "The synthesized answer could not be completed (generation timed out or "
        'failed). Here is a quick overview of the sources retrieved in this round; '
        'you can retry with "Regenerate" later.'
    ),
}

#: digest 尾注（fused > 前 5 条时）
_DIGEST_MORE: dict[str, str] = {
    "zh": "（其余 {n} 条来源见来源面板）",
    "en": "({n} more sources are available in the sources panel)",
}

#: markdown 控制字符（digest 来源文本转义集——防版式注入；`[`/`]` 单独走全角转换）
_DIGEST_MARKDOWN_ESCAPE_CHARS = frozenset("\\`*_#<>|~")

#: 空白折叠（含换行——列表行必须单行）
_DIGEST_WHITESPACE_RE = re.compile(r"\s+")


def _digest_lang(query_lang: Any) -> str:
    """digest 语言档：仅 zh 走中文，其余（en/缺失/非法）一律 en（与全站语言跟随口径一致）。"""
    return "zh" if str(query_lang or "") == "zh" else "en"


def _escape_digest_text(text: str) -> str:
    """来源文本（外部内容）进 digest 前的转义：空白折叠 + markdown 控制字符 + [n] 防伪造。

    **安全敏感点（ARCH §6.4）**：title/snippet 来自外部网页——
    - `[`/`]` 转全角 `［`/`］`：前端 `[n]` 角标解析只认半角 `[\\d{1,2}]`（renderer.ts
      CITE_SPLIT_PATTERN），半角残留即伪造引用/点开错误来源卡；全角形态视觉相近但不可解析
      （反斜杠转义不成立——markdown-it 先还原 `\\[` 为 `[`，仍会被前端切成角标）；
    - markdown 控制字符反斜杠转义：防标题/加粗/代码/表格等版式注入；
    - 换行折叠为空格：列表行保持单行。
    """
    collapsed = _DIGEST_WHITESPACE_RE.sub(" ", str(text)).strip()
    escaped = "".join(
        f"\\{char}" if char in _DIGEST_MARKDOWN_ESCAPE_CHARS else char
        for char in collapsed
    )
    return escaped.replace("[", "［").replace("]", "］")


def _build_digest(query_lang: Any, fused: list[Any]) -> str:
    """来源速览装配器（**纯机械零 LLM**，api.md §7.4 digest 结构）。

    结构 = 说明行（双语）+ 前 5 条 `- [n] title：snippet` 列表行（[n] 与已发 sources
    卡编号同序，由装配器生成）+ 尾注（fused 超过 5 条时）；title/snippet 各截断
    ≤120 字符（title 上限为显式代码约束——空标题回落 URL 同样受该上限）。
    调用方保证 fused 非空（零源轮不走本形态）。
    """
    lang = _digest_lang(query_lang)
    lines: list[str] = [_DIGEST_PREAMBLE[lang], ""]
    head = fused[:_DIGEST_MAX_SOURCES]
    for index, item in enumerate(head, start=1):
        # 截断在转义之前：上限语义按原始字符计（转义符不计入用户可见长度预算）
        raw_title = str(item.get("title") or "")[:_DIGEST_TITLE_MAX_CHARS]
        raw_url = str(item.get("url") or "")[:_DIGEST_TITLE_MAX_CHARS]
        raw_snippet = str(item.get("snippet") or "")
        title = _escape_digest_text(raw_title) or _escape_digest_text(raw_url)
        snippet = _escape_digest_text(raw_snippet[:_DIGEST_SNIPPET_MAX_CHARS])
        lines.append(f"- [{index}] {title}：{snippet}" if snippet else f"- [{index}] {title}")
    remaining = len(fused) - len(head)
    if remaining > 0:
        lines.extend(["", _DIGEST_MORE[lang].format(n=remaining)])
    return "\n".join(lines)


def _clip_to_sentence_boundary(text: str) -> str:
    """截断到最近句末标点/换行（partial 收束：**残句丢弃、不伪造补全**）。

    无任何句界（不足一句）→ 空串（调用方按零可用正文走 digest——api.md §7.4 明文）。
    """
    last_boundary = -1
    for index, char in enumerate(text):
        if char in _SENTENCE_END_CHARS or char == "\n":
            last_boundary = index
    if last_boundary < 0:
        return ""
    return text[: last_boundary + 1].strip()


def _resolve_strategy(state: Mapping[str, Any]) -> str:
    """state 侧策略归一：只认四值（direct/verify/web/recall，B12 +recall），
    其余（旧提示词/未声明）一律 web。"""
    strategy = state.get("search_strategy")
    return strategy if strategy in _SEARCH_STRATEGIES else "web"


def _apply_answerable_variant(
    template: str, answerable: bool, strategy: str = "web"
) -> str:
    """组织方式段确定性四态切分（行级过滤，键位与占位符集零变更）。

    消费面（nacos-config.md §1 v1.27 钉死）：
    - `direct` 按 strategy 切「免检索直答」段（该轮零来源，无 [n] 义务）；
    - `recall` 切「回顾整理」段（该轮零来源、历史经 history_block 注入）；
      **变体缺段兜底（灰度安全）**：旧模板无「- 回顾整理路径」时回落 direct 形态切分
      （零 sources 场景 direct 段语义安全可用，行为退化不中断）；
    - `verify`/`web` 沿用既有 answerable 两段切换——回滚态（web + answerable=true）仍切
      「先知识后取证」段=现行行为零变化。
    """
    prefixes = (
        _REGULAR_LINE_PREFIX,
        _KNOWLEDGE_FIRST_LINE_PREFIX,
        _DIRECT_LINE_PREFIX,
        _RECALL_LINE_PREFIX,
    )
    if strategy == "direct":
        keep_prefix = _DIRECT_LINE_PREFIX
    elif strategy == "recall":
        keep_prefix = _RECALL_LINE_PREFIX
        if not any(line.startswith(_RECALL_LINE_PREFIX) for line in template.splitlines()):
            keep_prefix = _DIRECT_LINE_PREFIX  # 旧模板缺 recall 段 → 回落 direct 形态
    elif answerable:
        keep_prefix = _KNOWLEDGE_FIRST_LINE_PREFIX
    else:
        keep_prefix = _REGULAR_LINE_PREFIX
    lines = template.splitlines()
    kept = [
        line
        for line in lines
        if not line.startswith(prefixes) or line.startswith(keep_prefix)
    ]
    return "\n".join(kept)


#: 观测 metadata 单值截断上限（ARCH §6.6 v1.43「metadata 单值 ≤200 字符」口径——
#: trace 层钉立的值形态纪律，span 层同规；snippet 与 user_query 共用）
_METADATA_VALUE_MAX_CHARS = 200


def _sources_compact(fused: list[Any]) -> list[dict[str, Any]]:
    """fused 检索列表紧凑投影（ref_no 与 [n] 引用编号对齐——_sources_block 同序）。"""
    return [
        {
            "ref_no": index,
            "url": str(item.get("url") or ""),
            "title": str(item.get("title") or ""),
            "snippet": str(item.get("snippet") or "")[:_METADATA_VALUE_MAX_CHARS],
        }
        for index, item in enumerate(fused, start=1)
    ]


def _strategy_metadata(
    state: Mapping[str, Any], *, fused_for_prompt: list[Any] | None = None
) -> dict[str, Any]:
    """轮策略观测元数据（ARCH §6.6 v1.44：search_strategy 与 query_type 同通道）。

    时序约束：本决策在 rewrite 之后才可知，而 Langfuse 轮级 trace 属性只能随 span
    创建写入（CallbackHandler 在根 run 启动时读 RunnableConfig.metadata）——故挂在
    **synthesis 生成 span**（本文最晚的 LLM run）的 metadata 上：M4 按策略分桶可从
    generation metadata 过滤；轮级留痕另走 stage(rewrite,done).extra（turn_events→§2.5）。

    Option B 增补键（装配面如实口径——v1.53 评审修正）：
    - `user_query`：本轮用户原始问题（state.query，不取改写后的 rewritten_queries；
      截断 ≤200 字符，同 v1.43 metadata 单值口径）；
    - `sources_compact`：来源供给面非空时的紧凑投影 [{ref_no,url,title,snippet≤200}]，
      ref_no 与 [n] 引用编号对齐；**供给面为空时键省略**（键可省口径，同 api.md images 行）。
      v1.70 修订：**supply 口径传 `fused_for_prompt`**（含 REQ-017 父轮扩展卡——
      parented 轮 [N+1..] 编号的卡进 metadata，M4 引用有效性分桶需要完整供给面）；
      缺省回落 `state.fused`（非父轮轮零变化）。

    **消费面限制（如实，不夸大）**：
    - **序列化形态**：本函数返回结构化值（list/dict），但 Langfuse SDK 对非 str 值经
      json.dumps 序列化后以 `langfuse.observation.metadata.<key>` 字符串属性落库
      （langfuse 4.18.0 attributes.py 实证）——评估器读到的是 JSON 编码字符串，
      消费需 JSON 解析，非「结构化数组直取」；
    - **部署可达性（挂账）**：当前部署 compose 收口 events_only 写模式（v4 默认），
      observation metadata 的 REST 读端点不可达（2026-11-17 活体探针：observations
      列表 metadata 全空、单观测端点返回 HTML 兜底、v2 traces 404；
      seed_online_eval_dataset.py 实证 70,299 行观测 metadata 恒空）——**评估器经
      Langfuse 消费本键待部署写模式裁决修复**（含通道①既有 search_strategy/query_type
      同样不可见），或改走已证可达通道（turn_events stage extra / DatasetItem metadata）。
      装配面保留：写模式修复后本函数产出即生效，零代码变更。
    """
    metadata: dict[str, Any] = {
        "search_strategy": _resolve_strategy(state),
        "user_query": str(state.get("query") or "")[:_METADATA_VALUE_MAX_CHARS],
    }
    fused = fused_for_prompt if fused_for_prompt is not None else state.get("fused") or []
    if fused:
        metadata["sources_compact"] = _sources_compact(fused)
    query_type = state.get("query_type")
    if isinstance(query_type, str) and query_type:
        metadata["query_type"] = query_type
    return metadata


def _with_strategy_metadata(
    config: RunnableConfig | None,
    state: Mapping[str, Any],
    *,
    fused_for_prompt: list[Any] | None = None,
) -> RunnableConfig:
    """config 浅拷贝 + metadata 合并（callbacks/configurable 原样保留）。

    `fused_for_prompt` 透传 sources_compact 的供给面口径（含 REQ-017 父轮扩展卡，
    见 `_strategy_metadata` v1.70 注）；None 回落 state.fused（非父轮轮零变化）。
    """
    base: dict[str, Any] = dict(config or {})
    metadata = dict((config or {}).get("metadata") or {})
    metadata.update(_strategy_metadata(state, fused_for_prompt=fused_for_prompt))
    base["metadata"] = metadata
    return base  # type: ignore[return-value]


def _sources_block(fused: list[dict[str, Any]]) -> str:
    """来源块装配（§6.4.3）：逐条 <external_content source="{url}" type="web"> 包裹，
    编号 [i] 可见（[n] 对应来源列表第 n 条）。

    开闭标记/属性经共享 helper（`app/agent/external_content.py`，M2 批次 1 收敛——与
    followups_service 同源，格式漂移风险收口）；**转义硬边界（P1 修复）**：title/snippet
    （content 侧）与 url（属性侧）均经 html.escape(quote=True) 转义——搜索内容含
    `</external_content>`/引号/尖括号时被实体化为惰性文本，隔离边界不可被内容击穿
    （提前闭合包裹→指令文本抬升为提示词正文）。声明行（软防线）在提示词内。
    """
    blocks: list[str] = []
    for index, item in enumerate(fused, start=1):
        title = html.escape(str(item.get("title") or ""), quote=True)
        snippet = html.escape(str(item.get("snippet") or ""), quote=True)
        origin = item.get("origin")
        is_kb = origin == "kb" or (isinstance(origin, list) and "kb" in (origin or []))
        # B19（§6.4.3 铁律原文 `kb:doc_id` 形态 + plan §4.3）：KB 卡 source="kb:{doc_id}"
        # + type="kb"；web 卡 source=url + type="web"（既有面逐字节不变——负例）
        source_attr = (
            f"kb:{item.get('kb_doc_id')}" if is_kb else str(item.get("url") or "")
        )
        blocks.append(
            external_content(
                f"[{index}] {title}\n{snippet}",
                source=source_attr,
                kind="kb" if is_kb else "web",
            )
        )
    return "\n".join(blocks)


def _strip_out_of_range_citations(text: str, source_count: int) -> str:
    """[n] 越界剔除：n<1 或 n>来源数的标记移除（不伪造编号——citation_rules 1/3 条）。"""

    def _replace(match: re.Match[str]) -> str:
        n = int(match.group(1))
        return match.group(0) if 1 <= n <= source_count else ""

    return _CITATION_RE.sub(_replace, text)


def _parent_source_extension(
    fused: list[Any], state: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """REQ-017 引用映射供给面装配（api.md §2.4 v1.28 / PRD REQ-017 v1.30）。

    parented **web/verify** 轮：父轮来源卡（state.parent_sources）URL 去重后并入
    可引用来源面——URL 命中轮内既有来源（或父轮集内先行条目）的卡片不重复供给
    （PRD「URL 相同的复用同一卡片」：同一 URL 经既有卡片引用）；其余卡片按父轮
    ref_no 序接续（调用方 `_sources_block` enumerate 承载 [N+1..] 连续编号——轮内
    重映射编号；[n]↔sources 双向核对口径不变）。

    direct/recall parented 轮**不扩供给面**（零源轮契约钉字保护：direct 轮 §2.5
    sources[] 恒空、recall 轮 source_count=0——父轮块仍经 {{history_block}} 可见，
    供组织回答，不供引用）；非父轮轮返回空表（零波及——供给面零变化）。
    """
    if state.get("parent_turn_id") is None or _resolve_strategy(state) not in (
        "web",
        "verify",
    ):
        return []
    parent_sources = state.get("parent_sources")
    if not isinstance(parent_sources, list):
        return []
    seen = {str(item.get("url") or "") for item in fused if isinstance(item, Mapping)}
    extension: list[dict[str, Any]] = []
    for card in parent_sources:
        if not isinstance(card, Mapping):
            continue
        url = str(card.get("url") or "")
        if not url or url in seen:
            continue  # URL 命中既有来源：复用同一卡片（不重复供给、不产生第二张卡）
        seen.add(url)
        extension.append(dict(card))
    return extension


def _text_of(chunk: Any) -> str:
    """单 chunk 的 text 提取：str 直取；v1 blocks 仅取 text 块（reasoning 不进答案）。"""
    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


async def synthesis(state: Any, config: RunnableConfig | None = None) -> dict[str, Any]:
    """综合回答节点：stage(write) 夹流式生成；answer_md 兜底截断 + 引用越界剔除。"""
    ensure_not_canceled(config)  # 节点边界感知协作式取消（R14②：跨子图注入面）
    emit_stage("write", "running", label="综合中")
    query = str(state.get("query") or "")
    fused = state.get("fused") or []
    errors = state.get("errors") or []
    if not fused and errors:
        # 防御性复验主短路（主短路在 merge_dedupe——到达此处即链失败漏网形态）
        raise SearchChainUnavailable("全部搜索源调用失败——synthesis 防御性短路")

    spec = state.get("spec")
    spec = spec if isinstance(spec, Mapping) else {}
    fast_section = spec.get("fast") if isinstance(spec, Mapping) else None
    max_chars = (
        int(fast_section.get("max_answer_chars", 3000))
        if isinstance(fast_section, Mapping) and fast_section.get("max_answer_chars")
        else 3000
    )
    policy = retry_policy_from_spec(spec, "fast")
    connection_policy = connection_retry_policy_from_spec(spec, "fast")
    call_timeout_s = llm_timeout_s(spec, "fast")
    budget = TurnDeadline(
        float(fast_section.get("turn_timeout_s", 120.0))
        if isinstance(fast_section, Mapping)
        else 120.0
    )
    model = build_chat_model(
        resolve_chat_alias(state, config),
        # v1.59 增量 C：fast 档缺省 off（spec.fast.thinking 热更可切 on）
        thinking=resolve_thinking(spec, "fast"),
    )
    # REQ-017 引用映射供给面（api.md §2.4 v1.28）：parented web/verify 轮父轮来源卡
    # URL 去重后接续编号并入可引用来源面（[N+1..]——轮内重映射编号）；direct/recall
    # 不扩（零源轮契约保护）；非父轮零变化。**全部扩展卡进供给面与返回 fused**
    # （v1.70 P0 修复：不再按「被引用」紧缩——编号稳定性优先于紧缩，供给面语义与
    # 本轮自有 fused 卡全量落库一致；紧缩会让正文引用编号与补行 ref_no 错位）。
    parent_extension = _parent_source_extension(fused, state)
    fused_for_prompt = fused + parent_extension

    template = _apply_answerable_variant(
        resolve_prompt("fast.synthesis"),
        bool(state.get("answerable_from_knowledge")),
        _resolve_strategy(state),
    )
    prompt_text = render_prompt(
        template,
        {
            "max_answer_chars": str(max_chars),
            "query_lang": str(state.get("query_lang") or "en"),
            "cite_rules": resolve_prompt("common.citation_rules"),
            # 回顾装配第二通道（ARCH §6.5 v1.51）：注入条件自「仅 recall 变体」扩为
            # 「recall ∨ REQ-017 parented 轮」（api.md §2.4 v1.28 / nacos-config §1
            # v1.38）——parented fast 轮的父轮摘要块+URL 表经本通道进提示词；
            # direct/verify/web **非父轮**变体仍恒空串（token 纪律零变化）。
            # 模板缺 recall 段时回落 direct 形态，历史仍注入（历史在场则可见——D4）。
            "history_block": (
                render_history_recall(state.get("history_recall"))
                if (_resolve_strategy(state) == "recall" or state.get("parent_turn_id"))
                else ""
            ),
            "memory_block": "",  # M5 记忆→prompt 通道；启用前恒空（memory-api.md §2）
            "sources_block": _sources_block(fused_for_prompt),
            "query": query,
        },
    )
    messages = [SystemMessage(content=prompt_text), HumanMessage(content=query)]

    answer = ""
    last_error: Exception | None = None
    #: 部分正文跨尝试捕获（v1.59 增量 A）：流内已作为 token 事件发出的最长尝试文本
    #: ——终局失败时取「最长可用前缀」做句界清理（不拼接残段、不伪造补全）
    best_partial = ""
    short_failures = 0  # 短窗（transient / 无 connection 节点的连接类）失败次数
    connection_failures = 0  # 连接类长窗失败次数（含首试失败）
    total_attempts = 0  # 物理尝试总次数（混档共享上限，见下）
    window: RetryWindow | None = None  # 连接类重试总窗（首次连接类失败时开窗）
    # **混档总尝试上限**（评审 P2-3）：两档计数器独立会让交替失败（5xx→连接→5xx…）
    # 叠加出 1+4+2=7 次尝试，超出 connection.max_attempts=5 的契约语义。口径钉死：
    # connection 节在场时**总物理尝试 ≤ connection.max_attempts**（长窗是本次轮的
    # 治理预算）；节缺失时维持主节 max_attempts（=旧行为）。两档各自上限仍先判。
    attempts_cap = (
        connection_policy.max_attempts
        if connection_policy is not None
        else policy.max_attempts
    )
    while True:
        total_attempts += 1
        accumulated: list[str] = []
        try:
            # 单调用超时包络（决策 4）：min(llm_timeout_s, 剩余轮预算)
            per_call = max(
                _MIN_CALL_TIMEOUT_S, min(call_timeout_s, budget.remaining_s())
            )
            async with asyncio.timeout(per_call):
                async for chunk in model.astream(
                    messages,
                    config=_with_strategy_metadata(
                        config, state, fused_for_prompt=fused_for_prompt
                    ),
                ):
                    accumulated.append(_text_of(chunk))
            answer = "".join(accumulated)
            break
        except Exception as exc:
            attempt_text = "".join(accumulated)
            if len(attempt_text) > len(best_partial):
                best_partial = attempt_text
            tier = classify_retry_tier(exc)
            if tier == "non_retryable":
                last_error = exc
                break  # 业务类不重试 → 终局失败（fused 非空走兜底）
            last_error = exc
            if tier == "connection" and connection_policy is not None:
                # 连接类长窗（1/2/4/8，cap max_interval_s）+ 总窗硬闸 + TurnDeadline 双闸
                connection_failures += 1
                if connection_failures >= connection_policy.max_attempts:
                    break
                if total_attempts >= attempts_cap:
                    break  # 混档共享上限：交替失败不放大总尝试
                if window is None:
                    window = RetryWindow(
                        connection_policy.total_window_s, clock=_clock
                    )
                delay = connection_policy.delay_for(connection_failures)
                if not window.can_wait(delay) or not budget.can_attempt(call_timeout_s):
                    break
                await _sleep(delay)
                if window.remaining_s() <= 0.0:
                    break  # 尝试耗时吃满窗口：先到先放弃（终局失败面见收束分支）
                continue
            # 短窗（维持现状）：spec.<mode>.retry 主节，可选 jitter
            short_failures += 1
            if short_failures >= policy.max_attempts:
                break
            if total_attempts >= attempts_cap:
                break  # 混档共享上限（同 connection 分支注释）
            # deadline-aware 重试预算：预计单次成本 = 单调用包络（v1.59 顺手项——原硬编码
            # 30.0 在 llm_timeout_s 改 60 后与其名实不符，改读包络实值）
            if not budget.can_attempt(call_timeout_s):
                break
            delay = policy.initial_interval_s * (
                policy.backoff_factor ** (short_failures - 1)
            )
            await _sleep(random.uniform(0, delay) if policy.jitter else delay)
    if not answer:
        _raise_terminal_failure(
            fused=fused,
            # 兜底信号随带供给面 fused（与成功路径返回值同口径，2026-11-21 终审）：
            # 无扩展卡时传 None——非父轮兜底零补行面。
            supply_fused=fused_for_prompt if parent_extension else None,
            best_partial=best_partial,
            last_error=last_error,
            attempts=total_attempts,
            max_chars=max_chars,
            query_lang=state.get("query_lang"),
            # 剔除基准=供给面（含父轮扩展卡）——parented 轮合法父轮引用不被误剔（v1.70）
            citation_count=len(fused_for_prompt),
        )

    answer = answer[:max_chars]
    answer = _strip_out_of_range_citations(answer, len(fused_for_prompt))
    emit_stage("write", "done")
    # REQ-017 供给面语义（api.md §2.4 v1.28，v1.70 P0 修复——编号稳定零重编号）：
    # **全部**父轮扩展卡随 fused 返回（不按「被引用」紧缩）——本轮自有 fused 卡本就
    # 全量落库（sources=供给面非引用面），扩展卡同口径；TurnRunner
    # `_persist_parent_source_appends` 按 fused 轮内位置补行 ref_no，与提示词供给编号
    # [N+offset] 恒等。若按引用紧缩，模型跳过前置父轮卡只引后面编号时补行 ref_no
    # 会前移、正文编号悬空（评审实证：fused=2+扩展 2 卡只引 [4] → 旧实现补行 ref_no=3）。
    if parent_extension:
        return {"answer_md": answer, "degraded": False, "fused": fused_for_prompt}
    return {"answer_md": answer, "degraded": False}


def _raise_terminal_failure(
    *,
    fused: list[Any],
    best_partial: str,
    last_error: Exception | None,
    attempts: int,
    max_chars: int,
    query_lang: Any,
    citation_count: int | None = None,
    supply_fused: list[Any] | None = None,
) -> None:
    """终局失败二分收束（v1.59 增量 A）：fused 非空 → SynthesisFallback；零源 → SynthesisError。

    - **partial 路径**：最长跨尝试前缀先过 [n] 越界剔除、按 max_chars 截断、再句界清理
      （硬上限不得留下残句）——清理后非空即 partial；不足一句按零可用正文走 digest；
      剔除基准=**供给面计数**（`citation_count`，缺省 len(fused)；parented 轮传
      `len(fused_for_prompt)`——合法父轮编号 [N+1..] 不得被误剔，v1.70）；
    - **digest 路径**：`_build_digest` 机械装配 → `emit_fallback_token` **单事件整块**发射 →
      `stage(write, done, extra={"fallback": kind})`（事件序：token → stage done → done）；
    - **两条路径均抛 `SynthesisFallback`**（零 error 事件，TurnRunner 落 degraded）；
      `supply_fused` 非空时（parented 轮，= `fused_for_prompt`）随信号携带——TurnRunner
      兜底收束按其在轮内位置补建父轮扩展卡 sources 行，与成功路径
      `_persist_parent_source_appends` 同口径（2026-11-21 终审：partial 正文保留的
      [N+1..] 不再悬空）。
    """
    if not fused:
        raise SynthesisError(
            f"综合生成失败（LLM 调用重试耗尽）：{last_error!r}"
            if last_error
            else "综合生成失败（空输出）"
        )
    failure = classify_failure_kind(last_error) if last_error is not None else "non_retryable"
    limit = len(fused) if citation_count is None else citation_count
    partial = _clip_to_sentence_boundary(
        _strip_out_of_range_citations(best_partial, limit)[:max_chars]
    )
    if partial:
        emit_stage("write", "done", extra={"fallback": "synthesis_partial"})
        raise SynthesisFallback(
            answer_md=partial,
            kind="synthesis_partial",
            failure=failure,
            attempts=attempts,
            source_count=len(fused),
            fused=supply_fused,
        )
    digest = _build_digest(query_lang, fused)
    emit_fallback_token(digest)  # 单事件整块（前端既有拼接渲染零新路径）
    emit_stage("write", "done", extra={"fallback": "synthesis_digest"})
    raise SynthesisFallback(
        answer_md=digest,
        kind="synthesis_digest",
        failure=failure,
        attempts=attempts,
        source_count=len(fused),
        fused=supply_fused,
    )
