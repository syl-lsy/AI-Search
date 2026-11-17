"""safety.reply：拒答话术生成（B13 reject 通道——安全拒绝轮的 error.message 源头）。

流水线（nacos-config.md §1 v1.30 / api.md §7.4 v1.23）：
resolve_prompt("common.safety_reply") → 行级类别切分（**沿 synthesis._apply_answerable_variant
手法：只删另一类别的段行**，公共行为引导保留）→ render `{{query_lang}}` → light 模型
非流式单调用（`disable_streaming` 形态由 `build_light_model` 自带，函数内延迟 import
防循环依赖）→ `asyncio.timeout(reply_timeout_s)` 包络。

兜底纪律（拒答不因 LLM 故障失效）：LLM 失败/超时/空输出 → 回落**切分后的模板段文本**
（本类别段行去前缀去空白；段缺失回落整份切分后模板；渲染 fail-fast 不浪费 LLM 调用
直接回落原始段文本）→ 截断 `reply_max_chars`。

观测：`runnable_config` 原样透传 `ainvoke`（Langfuse GENERATION 挂轮次 trace）。
取消纪律（R14③）：`TurnCanceledSignal` 先于 `except Exception` 重抛——取消非故障，
不落兜底话术。
"""

import asyncio
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.agent.guards import TurnCanceledSignal
from app.agent.prompts import PromptRenderError, render_prompt, resolve_prompt
from app.config_center.validator import SAFETY_REPLY_LINE_PREFIXES
from app.core.logger import AgentLog

__all__ = [
    "generate_rejection_reply",
]

#: 拒答话术模板键（占位符集 = {query_lang}，契约 §1 v1.30 锁死）
SAFETY_REPLY_PROMPT_KEY = "common.safety_reply"

#: 类别 → 段行前缀（validator 硬校验锚点同源：0=危险 / 1=辱骂；reject 仅允许这两类，
#: safety_check 契约「category=pii 一律 sanitize 不得 reject」）
_CATEGORY_LINE_PREFIXES: dict[str, str] = {
    "danger": SAFETY_REPLY_LINE_PREFIXES[0],
    "profanity": SAFETY_REPLY_LINE_PREFIXES[1],
}


def _build_light_model() -> Any:
    """light 模型工厂（延迟 import 防循环依赖；单测注入点——monkeypatch 本函数）。

    import 放函数体内：fast_rewrite 链上引 config_center/litellm settings 等重组件，
    顶层 import 会让 safety 包在纯词表/脱敏消费面也背上模型链路。
    """
    from app.agent.nodes.fast_rewrite import build_light_model

    return build_light_model()


def _keep_category_section(template: str, category: str) -> tuple[str, str]:
    """行级类别切分：删除另一类别的段行，保留其余全部行（公共引导不丢）。

    返回 `(kept, body)`——kept 为切分后模板（送 LLM），body 为本类别段行去前缀去
    空白的段正文（兜底话术源）；段行缺失时 body 为空串（调用方回落 kept）。
    """
    keep_prefix = _CATEGORY_LINE_PREFIXES[category]
    drop_prefix = next(
        prefix for prefix in _CATEGORY_LINE_PREFIXES.values() if prefix != keep_prefix
    )
    kept_lines: list[str] = []
    body = ""
    for line in template.splitlines():
        if line.startswith(drop_prefix):
            continue
        if line.startswith(keep_prefix):
            body = line[len(keep_prefix) :].strip()
        kept_lines.append(line)
    return "\n".join(kept_lines), body


def _message_text(message: Any) -> str:
    """AIMessage.content → 纯文本（None/空 → 空串走兜底；str 直取；分块列表拼 text 块）。"""
    if message is None:
        return ""
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in message
        )
    return str(message)


async def generate_rejection_reply(
    category: str,
    query_lang: str,
    config: Any,
    runnable_config: RunnableConfig | None = None,
) -> str:
    """生成类别化拒答话术（失败/超时/空输出回落模板段文本，恒返回非空尽力话术）。

    category ∈ danger|profanity（ValueError fail-fast——reject 组合约束在
    safety_check 契约钉死，pii 不走本通道）；query_lang 为代码判定的问题语言。
    config 为 `safety.config.SafetyConfig`（提供 reply_timeout_s / reply_max_chars）。
    """
    if category not in _CATEGORY_LINE_PREFIXES:
        raise ValueError(f"拒答话术类别非法：{category!r}（仅 danger|profanity）")
    raw = resolve_prompt(SAFETY_REPLY_PROMPT_KEY)
    kept, body = _keep_category_section(raw, category)
    try:
        prompt = render_prompt(kept, {"query_lang": query_lang})
        fallback = (
            render_prompt(body, {"query_lang": query_lang}) if body else prompt
        )
    except PromptRenderError:
        # 模板含未知占位符（渲染 fail-fast）→ 不浪费 LLM 调用，回落原始段文本
        AgentLog(
            message=f"safety_reply 模板渲染失败，回落原始段文本：{SAFETY_REPLY_PROMPT_KEY}",
            graph="safety",
        ).warning()
        return (body or kept)[: config.reply_max_chars]
    try:
        async with asyncio.timeout(config.reply_timeout_s):
            model = _build_light_model()
            message = await model.ainvoke(prompt, config=runnable_config)
            text = _message_text(getattr(message, "content", message)).strip()
    except TurnCanceledSignal:
        raise  # 取消非故障（R14③）：不落兜底话术，向上传播收束
    except Exception:  # noqa: BLE001 —— LLM 故障/超时一律回落模板段（拒答不失效）
        text = ""
    return (text or fallback).strip()[: config.reply_max_chars]
