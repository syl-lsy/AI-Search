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
- **外部内容包裹**（§6.4.3 铁律）：来源逐条 `<external_content source="{url}" type="web">`
  包裹（声明行在提示词内）；memory_block M5 前恒空串；
- **失败语义**：全源失败防御性短路（主短路在 merge_dedupe）；LLM 失败按**错误三档
  分级**重试（B11 P1a / ARCH §6.2 v1.49）——连接类（httpx transport / 单调用包络超时 /
  openai 连接形态）走 `spec.fast.retry.connection` 长窗指数退避（1/2/4/8，总窗
  total_window_s 硬闸，与 TurnDeadline 双闸先到先放弃）；5xx 类沿用 `spec.fast.retry`
  主节短窗；业务类（4xx/结构化解析失败）不重试。耗尽 → SynthesisError → TurnRunner
  error 500101（LLM 调用重试耗尽）+ done(finish=degraded)——已收集 sources 保留
  （api.md §7.2 error 收束「有部分内容时 error 后跟 done(finish=degraded)」）。
- **单调用超时包络**（决策 4）：每次 astream 包 `asyncio.timeout(min(llm_timeout_s,
  剩余轮预算))`——无包络时单次挂起会直接吃光 60s 兜底、重试窗形同虚设。

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
    classify_retry_tier,
    connection_retry_policy_from_spec,
    ensure_not_canceled,
    llm_timeout_s,
    retry_policy_from_spec,
)
from app.agent.graphs.signals import emit_stage
from app.agent.llm import build_chat_model, resolve_chat_alias
from app.agent.prompts import render_prompt, resolve_prompt
from app.agent.tools.registry import SearchChainUnavailable

__all__ = ["SynthesisError", "build_chat_model", "synthesis"]

_CITATION_RE = re.compile(r"\[(\d+)\]")

#: 组织方式段行前缀（确定性切分的锚点——键位与占位符集零变更）
_REGULAR_LINE_PREFIX = "- 常规路径"
_KNOWLEDGE_FIRST_LINE_PREFIX = "- 最小检索路径"
_DIRECT_LINE_PREFIX = "- 免检索直答路径"  # v1.20 第三变体段（direct 免检索直答轮）

#: 检索策略三值（v1.44 B9——缺省/非法一律按 web 处理，灰度安全）
_SEARCH_STRATEGIES = ("direct", "verify", "web")

#: 单调用超时包络下限（秒）：剩余轮预算为 0 时仍留一次极短尝试，让重试闸门裁决
_MIN_CALL_TIMEOUT_S = 0.001

#: 退避等待（测试注入点：单测断言退避序列/总窗闸时替换；生产恒 asyncio.sleep）
_sleep = asyncio.sleep

#: 单调钟（测试注入点：RetryWindow 总窗闸的确定性断言）
_clock = time.monotonic


class SynthesisError(Exception):
    """综合生成失败（LLM 重试耗尽/流中断）——TurnRunner 捕获 → error 500101 + 降级收束。"""


def _resolve_strategy(state: Mapping[str, Any]) -> str:
    """state 侧策略归一：只认三值，其余（旧提示词/未声明）一律 web。"""
    strategy = state.get("search_strategy")
    return strategy if strategy in _SEARCH_STRATEGIES else "web"


def _apply_answerable_variant(
    template: str, answerable: bool, strategy: str = "web"
) -> str:
    """组织方式段确定性三态切分（行级过滤，键位与占位符集零变更）。

    消费面（nacos-config.md §1 v1.20 钉死）：`direct` 按 strategy 切「免检索直答」
    段（该轮零来源，无 [n] 义务）；`verify`/`web` 沿用既有 answerable 两段切换——
    回滚态（web + answerable=true）仍切「先知识后取证」段=现行行为零变化。
    """
    if strategy == "direct":
        keep_prefix = _DIRECT_LINE_PREFIX
    elif answerable:
        keep_prefix = _KNOWLEDGE_FIRST_LINE_PREFIX
    else:
        keep_prefix = _REGULAR_LINE_PREFIX
    lines = template.splitlines()
    kept = [
        line
        for line in lines
        if not line.startswith(
            (_REGULAR_LINE_PREFIX, _KNOWLEDGE_FIRST_LINE_PREFIX, _DIRECT_LINE_PREFIX)
        )
        or line.startswith(keep_prefix)
    ]
    return "\n".join(kept)


def _strategy_metadata(state: Mapping[str, Any]) -> dict[str, str]:
    """轮策略观测元数据（ARCH §6.6 v1.44：search_strategy 与 query_type 同通道）。

    时序约束：本决策在 rewrite 之后才可知，而 Langfuse 轮级 trace 属性只能随 span
    创建写入（CallbackHandler 在根 run 启动时读 RunnableConfig.metadata）——故挂在
    **synthesis 生成 span**（本文最晚的 LLM run）的 metadata 上：M4 按策略分桶可从
    generation metadata 过滤；轮级留痕另走 stage(rewrite,done).extra（turn_events→§2.5）。
    """
    metadata = {"search_strategy": _resolve_strategy(state)}
    query_type = state.get("query_type")
    if isinstance(query_type, str) and query_type:
        metadata["query_type"] = query_type
    return metadata


def _with_strategy_metadata(
    config: RunnableConfig | None, state: Mapping[str, Any]
) -> RunnableConfig:
    """config 浅拷贝 + metadata 合并（callbacks/configurable 原样保留）。"""
    base: dict[str, Any] = dict(config or {})
    metadata = dict((config or {}).get("metadata") or {})
    metadata.update(_strategy_metadata(state))
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
        blocks.append(
            external_content(
                f"[{index}] {title}\n{snippet}",
                source=str(item.get("url") or ""),
                kind="web",
            )
        )
    return "\n".join(blocks)


def _strip_out_of_range_citations(text: str, source_count: int) -> str:
    """[n] 越界剔除：n<1 或 n>来源数的标记移除（不伪造编号——citation_rules 1/3 条）。"""

    def _replace(match: re.Match[str]) -> str:
        n = int(match.group(1))
        return match.group(0) if 1 <= n <= source_count else ""

    return _CITATION_RE.sub(_replace, text)


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
    model = build_chat_model(resolve_chat_alias(state, config))

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
            "memory_block": "",  # M5 记忆→prompt 通道；启用前恒空（memory-api.md §2）
            "sources_block": _sources_block(fused),
            "query": query,
        },
    )
    messages = [SystemMessage(content=prompt_text), HumanMessage(content=query)]

    answer = ""
    last_error: Exception | None = None
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
                    messages, config=_with_strategy_metadata(config, state)
                ):
                    accumulated.append(_text_of(chunk))
            answer = "".join(accumulated)
            break
        except Exception as exc:
            tier = classify_retry_tier(exc)
            if tier == "non_retryable":
                raise SynthesisError(f"综合生成失败（不可重试）：{exc}") from exc
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
                    break  # 尝试耗时吃满窗口：先到先放弃（既有 SynthesisError 语义）
                continue
            # 短窗（维持现状）：spec.<mode>.retry 主节，可选 jitter
            short_failures += 1
            if short_failures >= policy.max_attempts:
                break
            if total_attempts >= attempts_cap:
                break  # 混档共享上限（同 connection 分支注释）
            if not budget.can_attempt(30.0):
                break
            delay = policy.initial_interval_s * (
                policy.backoff_factor ** (short_failures - 1)
            )
            await _sleep(random.uniform(0, delay) if policy.jitter else delay)
    if not answer:
        raise SynthesisError(
            f"综合生成失败（LLM 调用重试耗尽）：{last_error!r}" if last_error else "综合生成失败（空输出）"
        )

    answer = answer[:max_chars]
    answer = _strip_out_of_range_citations(answer, len(fused))
    emit_stage("write", "done")
    return {"answer_md": answer, "degraded": False}
