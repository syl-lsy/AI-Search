"""共享 chat 强模型工厂（M2 批次 1——plan-m2 v1 批次 1 第 5 件「共享模型工厂」）。

从 `app/agent/nodes/synthesis.py` 抽出（原 `build_chat_model` / `_chat_alias`，构造
参数与解析语义逐字节保留=行为零变更），供 M2 deep 的 planner/reflect/writer 复用
（批次 2~4 各节点接入）；fast synthesis 节点继续消费同一工厂。light 通道不在此处——
`fast_rewrite.build_light_model` 为 S15 结构化输出专属形态（disable_streaming /
think:false / temperature=0，批次 8 A 裁决），保持既有落位不动。

- `resolve_chat_alias`：chat 别名解析——state `chat_model_key` → 轮内映射快照
  `chat.options`（OQ-11 同源：轮内不重读，热更只对后续轮生效），缺省回落
  `chat.default`，全缺回落内置 MODEL_MAPPING_CONFIG 兜底；
- `build_chat_model`：chat 强模型实例（openai_reasoning provider + LiteLLM 网关），
  `stream_usage=True`（M2 批次 1 usage 方案 A，docs/m2-usage-probe-batch0.md §4）。
"""

from collections.abc import Mapping
from typing import Any

from langchain_core.runnables import RunnableConfig
from pydantic import SecretStr

from app.config_center.defaults import MODEL_MAPPING_CONFIG
from app.core.config import litellm_settings

__all__ = ["build_chat_model", "resolve_chat_alias"]


def resolve_chat_alias(state: Mapping[str, Any], config: RunnableConfig | None) -> str:
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


def build_chat_model(alias: str) -> Any:
    """chat 模型实例：别名经映射解析后的 LiteLLM 别名（Standard/Speed 档）。

    import reasoning_chat_model 即注册 openai_reasoning provider（R8）——GLM/DeepSeek
    思维链经 messages 流透传（§6.1 thinking 事件面）；采样参数不写死（§6.7）。

    `stream_usage=True`（M2 批次 1 usage 方案 A，docs/m2-usage-probe-batch0.md §4）：
    显式 base_url（LiteLLM 网关）下 langchain-openai 不自动置位（仅默认 OpenAI base_url
    才自动开——probe §2.2 因果链 2，`_should_stream_usage()`=False），流式调用不带
    `stream_options.include_usage` → 上游 usage 尾帧缺失 → `AIMessageChunk.usage_metadata`
    永不填充（done.usage 轮级累计、Langfuse GENERATION usage 同源此缺口）。网关两通道
    （chat-standard / qwen3.5-4b-ollama）流式透传已活体探针实证；非流式调用不受影响。

    `extra_body={"enable_thinking": False}`（专项① 2026-11-13 synthesis 提速）：
    chat-standard 通道（百炼 qwen3.7-plus，DashScope OpenAI 兼容端点）生效键形态经
    网关探针钉死——`enable_thinking: false` 流式 delta 零 reasoning_content（1.0~1.7s），
    rewrite 先例的 `think: false`（ollama 原生形态）被该通道上游忽略（思维链仍在，
    ~4.3~5.1s ≈ 基线）。仅关思维链不关流式（disable_streaming 为 rewrite 结构化输出
    通道专属，不外溢）；透传 reasoning_content 的 R8 补丁保持不变——GLM/DeepSeek 等
    未受该键影响的通道若有思维链仍正常透出。
    """
    import app.utils.reasoning_chat_model  # noqa: F401 —— 注册副作用（幂等）
    from langchain.chat_models import init_chat_model

    return init_chat_model(
        model=alias,
        model_provider="openai_reasoning",
        base_url=str(litellm_settings.base_url),
        api_key=SecretStr(litellm_settings.api_key.get_secret_value()),
        stream_usage=True,
        extra_body={"enable_thinking": False},
    )
