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
- **失败语义**：全源失败防御性短路（主短路在 merge_dedupe）；LLM 传输类失败按 spec.retry
  重试（退避+jitter+deadline 门控）→ 耗尽 → SynthesisError → TurnRunner error 500101
  （LLM 调用重试耗尽）+ done(finish=degraded)——已收集 sources 保留（api.md §7.2 error
  收束「有部分内容时 error 后跟 done(finish=degraded)」）。

stage(write, running, label=综合中 → done) 夹全部动作（api.md §7.4）。节点纯函数：模型
工厂可注入可 mock，零 DB/EventBus。
"""

import asyncio
import html
import random
import re
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import SecretStr

from app.agent.guards import TurnDeadline, default_retry_on, ensure_not_canceled, retry_policy_from_spec
from app.agent.graphs.signals import emit_stage
from app.agent.prompts import render_prompt, resolve_prompt
from app.agent.tools.registry import SearchChainUnavailable
from app.config_center.defaults import MODEL_MAPPING_CONFIG
from app.core.config import litellm_settings

__all__ = ["SynthesisError", "build_chat_model", "synthesis"]

_CITATION_RE = re.compile(r"\[(\d+)\]")

#: 组织方式段行前缀（确定性切分的锚点——键位与占位符集零变更）
_REGULAR_LINE_PREFIX = "- 常规路径"
_KNOWLEDGE_FIRST_LINE_PREFIX = "- 最小检索路径"
_DIRECT_LINE_PREFIX = "- 免检索直答路径"  # v1.20 第三变体段（direct 免检索直答轮）

#: 检索策略三值（v1.44 B9——缺省/非法一律按 web 处理，灰度安全）
_SEARCH_STRATEGIES = ("direct", "verify", "web")


class SynthesisError(Exception):
    """综合生成失败（LLM 重试耗尽/流中断）——TurnRunner 捕获 → error 500101 + 降级收束。"""


def build_chat_model(alias: str) -> Any:
    """chat 模型实例：别名经映射解析后的 LiteLLM 别名（Standard/Speed 档）。

    import reasoning_chat_model 即注册 openai_reasoning provider（R8）——GLM/DeepSeek
    思维链经 messages 流透传（§6.1 thinking 事件面）；采样参数不写死（§6.7）。

    `extra_body={"enable_thinking": False}`（专项① 2026-11-13 synthesis 提速）：
    chat-standard 通道（百炼 qwen3.7-plus，DashScope OpenAI 兼容端点）生效键形态经
    网关探针钉死——`enable_thinking: false` 流式 delta 零 reasoning_content（1.0~1.7s），
    rewrite 先例的 `think: false`（ollama 原生形态）被该通道上游忽略（思维链仍在，
    ~4.3~5.1s ≈ 基线）。仅关思维链不关流式（disable_streaming 为 rewrite 结构化输出
    通道专属，不外溢）；透传 reasoning_content 的 R8 补丁保持不变——GLM/DeepSeek 等未
    受该键影响的通道若有思维链仍正常透出。
    """
    import app.utils.reasoning_chat_model  # noqa: F401 —— 注册副作用（幂等）
    from langchain.chat_models import init_chat_model

    return init_chat_model(
        model=alias,
        model_provider="openai_reasoning",
        base_url=str(litellm_settings.base_url),
        api_key=SecretStr(litellm_settings.api_key.get_secret_value()),
        extra_body={"enable_thinking": False},
    )


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

    **转义硬边界（P1 修复）**：url/title/snippet 经 html.escape(quote=True) 转义后插值——
    搜索内容含 `</external_content>`/引号/尖括号时被实体化为惰性文本，隔离边界不可被
    内容击穿（提前闭合包裹→指令文本抬升为提示词正文）。声明行（软防线）在提示词内。
    """
    blocks: list[str] = []
    for index, item in enumerate(fused, start=1):
        url = html.escape(str(item.get("url") or ""), quote=True)
        title = html.escape(str(item.get("title") or ""), quote=True)
        snippet = html.escape(str(item.get("snippet") or ""), quote=True)
        blocks.append(
            f'<external_content source="{url}" type="web">\n[{index}] {title}\n{snippet}\n</external_content>'
        )
    return "\n".join(blocks)


def _strip_out_of_range_citations(text: str, source_count: int) -> str:
    """[n] 越界剔除：n<1 或 n>来源数的标记移除（不伪造编号——citation_rules 1/3 条）。"""

    def _replace(match: re.Match[str]) -> str:
        n = int(match.group(1))
        return match.group(0) if 1 <= n <= source_count else ""

    return _CITATION_RE.sub(_replace, text)


def _chat_alias(state: Mapping[str, Any], config: RunnableConfig | None) -> str:
    """chat 别名解析：chat_model_key → 轮内映射快照 chat.options（缺省 chat.default）。"""
    configurable = (config or {}).get("configurable") or {}
    mapping = configurable.get("model_mapping")
    mapping = mapping if isinstance(mapping, Mapping) else MODEL_MAPPING_CONFIG
    chat = mapping.get("chat") or {}
    options: dict[str, str] = dict(chat.get("options") or {})
    key = str(state.get("chat_model_key") or "") or str(chat.get("default") or "")
    alias = options.get(key) or (options.get(str(chat.get("default"))) if chat.get("default") else None)
    if not alias:
        alias = str(MODEL_MAPPING_CONFIG["chat"]["options"]["qwen"])
    return alias


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
    budget = TurnDeadline(
        float(fast_section.get("turn_timeout_s", 60.0))
        if isinstance(fast_section, Mapping)
        else 60.0
    )
    model = build_chat_model(_chat_alias(state, config))

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
    for attempt in range(policy.max_attempts):
        if attempt:
            if not budget.can_attempt(30.0):
                break
            delay = policy.initial_interval_s * (policy.backoff_factor ** (attempt - 1))
            await asyncio.sleep(random.uniform(0, delay) if policy.jitter else delay)
        accumulated: list[str] = []
        try:
            async for chunk in model.astream(
                messages, config=_with_strategy_metadata(config, state)
            ):
                accumulated.append(_text_of(chunk))
            answer = "".join(accumulated)
            break
        except Exception as exc:
            if not default_retry_on(exc):
                raise SynthesisError(f"综合生成失败（不可重试）：{exc}") from exc
            last_error = exc
    if not answer:
        raise SynthesisError(
            f"综合生成失败（LLM 调用重试耗尽）：{last_error!r}" if last_error else "综合生成失败（空输出）"
        )

    answer = answer[:max_chars]
    answer = _strip_out_of_range_citations(answer, len(fused))
    emit_stage("write", "done")
    return {"answer_md": answer, "degraded": False}
