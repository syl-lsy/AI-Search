"""unit 层：共享强模型工厂 + usage 工厂侧（M2 批次 1——plan-m2 批次 1 第 5 件 / 方案 A）。

锁面三组：
- **stream_usage 透传**（docs/m2-usage-probe-batch0.md §4 方案 A 接线）：两工厂必须向
  init_chat_model 传 `stream_usage=True`——显式 base_url 下 langchain-openai 不自动置位
  （probe §2.2 因果链 2），流式调用不带 stream_options.include_usage → 上游 usage 尾帧
  缺失 → `AIMessageChunk.usage_metadata` 永不填充（done.usage 轮级累计与 Langfuse
  GENERATION usage 同源此缺口）。断言双形态：Fake init 记录 kwargs + 真实例字段值；
- **别名解析回归**：`resolve_chat_alias` 三档语义（映射快照 key → chat.default → 内置
  兜底）与抽离前逐行为一致（零行为变更）；
- **既有通道形态零回退**：chat 实例 extra_body={'enable_thinking': False}（专项①）、
  light 实例 disable_streaming=True（批次 8 A 裁决）——stream_usage 不得挤掉既有参数。

unit 全 mock 零网络：init_chat_model 假体只记录 kwargs 不构造；真实例断言仅读 pydantic
字段（构造不出网）。
"""

from typing import Any

import pytest

from app.agent import llm as llm_module
from app.agent.nodes import fast_rewrite, synthesis as synthesis_module
from app.utils.reasoning_chat_model import ReasoningChatModel


class _RecordingInit:
    """init_chat_model 假体：记录 (model, kwargs)，返回标记对象（零网络）。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, model: str, **kwargs: Any) -> Any:
        self.calls.append({"model": model, **kwargs})
        return object()


def _patch_init(monkeypatch: pytest.MonkeyPatch, recorder: _RecordingInit) -> None:
    import langchain.chat_models as chat_models

    monkeypatch.setattr(chat_models, "init_chat_model", recorder)


# ---- stream_usage 透传（Fake init 记录 kwargs 形态）----


def test_build_chat_model_passes_stream_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _RecordingInit()
    _patch_init(monkeypatch, recorder)

    llm_module.build_chat_model("chat-standard")

    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["model"] == "chat-standard"
    assert call["stream_usage"] is True
    # 既有通道形态零回退（专项① 关思维链锁面）
    assert call["extra_body"] == {"enable_thinking": False}


def test_build_light_model_passes_stream_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _RecordingInit()
    _patch_init(monkeypatch, recorder)

    fast_rewrite.build_light_model({"light": "qwen3.5-4b-ollama"})

    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["model"] == "qwen3.5-4b-ollama"
    assert call["stream_usage"] is True
    # 既有通道形态零回退（批次 8 A 裁决：结构化输出与流式互斥）
    assert call["disable_streaming"] is True


# ---- stream_usage 落到真实例字段（构造不出网，读 pydantic 字段）----


def test_chat_model_real_instance_stream_usage() -> None:
    model = llm_module.build_chat_model("chat-standard")
    assert isinstance(model, ReasoningChatModel)  # openai_reasoning provider 注册面（R8）
    assert model.stream_usage is True
    assert model.extra_body == {"enable_thinking": False}


def test_light_model_real_instance_stream_usage() -> None:
    model = fast_rewrite.build_light_model({"light": "qwen3.5-4b-ollama"})
    assert model.stream_usage is True
    assert model.disable_streaming is True


# ---- 别名解析回归（resolve_chat_alias：抽离前 _chat_alias 三档语义）----


def test_alias_resolves_from_mapping_snapshot_key() -> None:
    alias = llm_module.resolve_chat_alias(
        {"chat_model_key": "glm"},
        {"configurable": {"model_mapping": {
            "chat": {"options": {"glm": "chat-standard-glm"}, "default": "qwen"},
        }}},
    )
    assert alias == "chat-standard-glm"


def test_alias_falls_back_to_chat_default_when_key_missing() -> None:
    alias = llm_module.resolve_chat_alias(
        {},
        {"configurable": {"model_mapping": {
            "chat": {"options": {"qwen": "chat-standard", "glm": "chat-standard-glm"},
                     "default": "glm"},
        }}},
    )
    assert alias == "chat-standard-glm"


def test_alias_falls_back_to_default_when_key_unknown() -> None:
    alias = llm_module.resolve_chat_alias(
        {"chat_model_key": "nope"},
        {"configurable": {"model_mapping": {
            "chat": {"options": {"qwen": "chat-standard"}, "default": "qwen"},
        }}},
    )
    assert alias == "chat-standard"


def test_alias_falls_back_to_builtin_when_mapping_absent() -> None:
    """config 无 model_mapping 快照（Studio 直跑形态）→ 内置 MODEL_MAPPING_CONFIG 兜底。"""
    assert llm_module.resolve_chat_alias({}, {}) == "chat-standard"
    assert llm_module.resolve_chat_alias({}, None) == "chat-standard"


def test_alias_degenerate_mapping_uses_builtin_fallback() -> None:
    """快照存在但 chat 节残缺（无 options/default）→ 内置 qwen 选项兜底。"""
    alias = llm_module.resolve_chat_alias(
        {}, {"configurable": {"model_mapping": {"chat": {}}}}
    )
    assert alias == "chat-standard"


# ---- synthesis 与共享工厂的接线面（既有打桩形态保持）----


def test_synthesis_module_still_exposes_build_chat_model() -> None:
    """既有测试与 TurnRunner 经 synthesis.build_chat_model 打桩/调用——属性必须在。"""
    assert synthesis_module.build_chat_model is llm_module.build_chat_model
