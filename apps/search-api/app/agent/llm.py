"""共享 chat 强模型工厂（M2 批次 1——plan-m2 v1 批次 1 第 5 件「共享模型工厂」）。

从 `app/agent/nodes/synthesis.py` 抽出（原 `build_chat_model` / `_chat_alias`，构造
参数与解析语义逐字节保留=行为零变更），供 M2 deep 的 planner/reflect/writer 复用
（批次 2~4 各节点接入）；fast synthesis 节点继续消费同一工厂。light 通道不在此处——
`fast_rewrite.build_light_model` 为 S15 结构化输出专属形态（disable_streaming /
think:false / temperature=0，批次 8 A 裁决），保持既有落位不动。

- `resolve_chat_alias`：chat 别名解析——state `chat_model_key` → 轮内映射快照
  `chat.options`（OQ-11 同源：轮内不重读，热更只对后续轮生效），缺省回落
  `chat.default`，全缺回落内置 MODEL_MAPPING_CONFIG 兜底；
- `resolve_thinking`：模式级思考档位（`spec.<mode>.thinking`，枚举 on|off）——键缺失/
  非法回落裁决值 **fast=off / deep=on**（nacos-config.md §3 v1.32）；
- `build_chat_model`：chat 强模型实例（openai_reasoning provider + LiteLLM 网关），
  `stream_usage=True`（M2 批次 1 usage 方案 A，docs/m2-usage-probe-batch0.md §4）；
  `thinking` 档位按模式注入 extra_body（v1.59 增量 C，ARCH §6.7）。
"""

from collections.abc import Mapping
from typing import Any

from langchain_core.runnables import RunnableConfig
from pydantic import SecretStr

from app.config_center.defaults import MODEL_MAPPING_CONFIG
from app.core.config import litellm_settings

__all__ = [
    "OFF_THINKING_EXTRA_BODY",
    "THINKING_MODES",
    "build_chat_model",
    "off_thinking_extra_body",
    "resolve_chat_alias",
    "resolve_thinking",
]

#: 思考档位枚举（nacos-config.md §3 v1.32；validator FastSpec/DeepSpec Literal 同源）
THINKING_MODES: tuple[str, ...] = ("on", "off")

#: 键缺失/非法的档位回落（裁决值：快速模式不开思考 / 深度模式开思考——ARCH §6.7 v1.59）
_DEFAULT_THINKING: dict[str, str] = {"fast": "off", "deep": "on"}

#: **off 档禁用键组**（三模型腿逐腿形态，2026-11-18 网关探针钉死——
#: `scripts/probe_thinking_mode_params.py` 实测留档）：
#:
#: - `enable_thinking: false`：百炼 qwen 腿（批次 9 专项①已钉形态，DashScope 生效）；
#: - `reasoning_effort: "low"`：智谱 GLM 腿最低档——GLM 无法完全关思考（网关错误信息
#:   实证档位词 low/high/max 存在）；探针：flash 腿 reasoning 2020→0 字符、standard 腿
#:   high/max 分别 1138/1644 字符而 low 恒 0——键活且 low 为最低档。该键名不在 B13
#:   网关剥离钩子清单（钩子只剥 enable_thinking/think），high 档能触发思考即证未被剥；
#: - `extra_body: {"thinking": {"type": "disabled"}}`：DeepSeek 腿禁思考——**必须嵌套在
#:   LiteLLM 的 provider 级 extra_body 才透传**（顶层 `thinking` 被 LiteLLM 丢弃，探针
#:   实证仍出 131~174 字符思考流）；嵌套形态 reasoning 归零。`thinking.type="off"` 触发
#:   上游 500，禁用该取值。
#:
#: on 档 = **零禁用键注入**（=v1.58 后现状）；本键只管「开关」不管「深度」——on 档 GLM
#: 走模型默认思考深度（如实钉注）。light 路径（rewrite/safety/followups）不经本键。
OFF_THINKING_EXTRA_BODY: dict[str, Any] = {
    "enable_thinking": False,
    "reasoning_effort": "low",
    "extra_body": {"thinking": {"type": "disabled"}},
}


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


def resolve_thinking(spec: Mapping[str, Any] | None, mode: str) -> str:
    """模式思考档位：`spec.<mode>.thinking` ∈ {on, off}；键缺失/非法回落裁决值。

    回落口径 = fast=off / deep=on（nacos-config.md §3 v1.32「初值与代码缺省=裁决值」）
    ——validator 拒收非法值之外的第二道防线（Studio 直跑/旧 spec 无键形态），沿
    guards.llm_timeout_s 先例：guard 不抛、不阻塞主链路。
    """
    section = spec.get(mode) if isinstance(spec, Mapping) else None
    value = section.get("thinking") if isinstance(section, Mapping) else None
    if isinstance(value, str) and value in THINKING_MODES:
        return value
    return _DEFAULT_THINKING.get(mode, "on")


def off_thinking_extra_body() -> dict[str, Any]:
    """off 档禁用键组的独立副本（嵌套 dict 深拷贝——调用方变异不得污染常量）。"""
    return {
        "enable_thinking": OFF_THINKING_EXTRA_BODY["enable_thinking"],
        "reasoning_effort": OFF_THINKING_EXTRA_BODY["reasoning_effort"],
        "extra_body": {
            "thinking": dict(OFF_THINKING_EXTRA_BODY["extra_body"]["thinking"])
        },
    }


def build_chat_model(alias: str, *, thinking: str = "on") -> Any:
    """chat 模型实例：别名经映射解析后的 LiteLLM 别名（Standard/Speed 档）。

    import reasoning_chat_model 即注册 openai_reasoning provider（R8）——GLM/DeepSeek
    思维链经 messages 流透传（§6.1 thinking 事件面）；采样参数不写死（§6.7）。

    `stream_usage=True`（M2 批次 1 usage 方案 A，docs/m2-usage-probe-batch0.md §4）：
    显式 base_url（LiteLLM 网关）下 langchain-openai 不自动置位（仅默认 OpenAI base_url
    才自动开——probe §2.2 因果链 2，`_should_stream_usage()`=False），流式调用不带
    `stream_options.include_usage` → 上游 usage 尾帧缺失 → `AIMessageChunk.usage_metadata`
    永不填充（done.usage 轮级累计、Langfuse GENERATION usage 同源此缺口）。网关两通道
    （chat-standard / qwen3.5-4b-ollama）流式透传已活体探针实证；非流式调用不受影响。

    **`thinking` 按模式分档（v1.59 增量 C，ARCH §6.7）**：
    - `thinking="off"`（fast synthesis / regenerate 微图）→ 注入 `OFF_THINKING_EXTRA_BODY`
      三腿禁用键组（形态经网关注入探针钉死，见常量注释）；
    - `thinking="on"`（deep planner/reflect/writer 等）→ **零禁用键注入**（=v1.58 后现状）。
    任何非 "off" 取值按 on 处理（防御：档位枚举由 validator 与 resolve_thinking 双重收口）。
    """
    import app.utils.reasoning_chat_model  # noqa: F401 —— 注册副作用（幂等）
    from langchain.chat_models import init_chat_model

    kwargs: dict[str, Any] = {
        "model": alias,
        "model_provider": "openai_reasoning",
        "base_url": str(litellm_settings.base_url),
        "api_key": SecretStr(litellm_settings.api_key.get_secret_value()),
        "stream_usage": True,
    }
    if thinking == "off":
        kwargs["extra_body"] = off_thinking_extra_body()
    return init_chat_model(**kwargs)
