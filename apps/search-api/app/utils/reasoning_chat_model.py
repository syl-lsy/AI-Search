"""思维链透传补丁（R8）：ReasoningChatModel 让 GLM/DeepSeek 的 reasoning_content 穿透
langchain-openai 的静默丢弃（ARCHITECTURE §6.7 / §10 R8 行 / §3.1 utils 目录锚点）。

背景与边界：ChatOpenAI 官方行为是丢弃 reasoning_content 等非标准字段（库内 docstring
明示 "not extracted or preserved"，langchain_openai/chat_models/base.py 模块头）——
GLM 与 DeepSeek 经 LiteLLM 出口的思维链被静默丢失（R11 实证：非流式
message.reasoning_content 独立字段、流式 delta.reasoning_content 增量、无 <think>
内嵌、网关全透传）。落地 = ChatOpenAI 子类覆写两个私有转换点，产出 v1 content
blocks（reasoning/text）：

- invoke 路径：`_create_chat_result`（invoke/ainvoke → _generate/_agenerate 共用）——
  choices[].message.reasoning_content 非空时 content 升 blocks
  [{type: reasoning}, {type: text}]（content 为空——思考耗尽全部 max_tokens 的实测
  形态——仅 [{type: reasoning}]，不补空 text 块）；
- stream 路径：`_convert_chunk_to_generation_chunk`（stream/astream 共用）——
  delta.reasoning_content 增量 → [{type: reasoning, index: 0}]；同帧携带 content 补
  {type: text, index: 1}。index 是 AIMessageChunk 聚合的合并锚点（langchain-core
  merge_lists 按 index 匹配、字符串字段拼接）——聚合后思考增量拼为单块、正文拼为单块；
- 普通路径零扰动：无 reasoning_content 时输出与上游逐字节一致（str content）——补丁
  唯一允许的行为差异边界；qwen 同形字段（handoff 续 10 R11 四路实证）自动并入覆盖。

注册：import 本模块即把 openai_reasoning 写入 langchain `_BUILTIN_PROVIDERS`
（init_chat_model 的私有注册表，entry=(module, class, creator)）——模型工厂后续经
init_chat_model(model_provider="openai_reasoning", ...) 取本类。

R8 锁版本口径（ARCH §10 R8 行）：本模块覆写 langchain-openai 私有 API，锁
langchain-openai==1.7.4（pyproject），升级前必须先跑护栏测试
test/unit/test_reasoning_chat_model.py（invoke 与 stream 双路断言 reasoning 块）。

默认参数口径（R7）：不写死 max_tokens 等任何采样参数（全部继承上游默认 None，实际值
由 Nacos 模型映射注入）。部署口径注记：思考模型 max_tokens 必须 ≥8192——思考 token
可耗尽全部输出预算（R7 实测 2048 下 3/3 思考耗尽空输出；GLM-5.3 同病，ARCH §10 R11
行注「glm 档设 max_tokens 须预留推理份额」），冷启动 23~149s 由调用方超时预算兜底
（s0-batch1-handoff 续 10 R7）。
"""

from collections.abc import Mapping
from typing import Any, cast

from langchain.chat_models import base as _lc_chat_models_base
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI

# LiteLLM 网关透传的思维链字段名（GLM/DeepSeek/qwen 同形，R11 四路实证）
_REASONING_FIELD = "reasoning_content"
# init_chat_model 注册名（ARCH §6.7 定名）
_PROVIDER_NAME = "openai_reasoning"
# 流式块合并锚点：思考块 index 0、正文块 index 1（merge_lists 按 index 匹配后拼接）
_REASONING_BLOCK_INDEX = 0
_TEXT_BLOCK_INDEX = 1


def _extract_reasoning_content(message_obj: Any) -> str | None:
    """从单条 message/delta 提取 reasoning_content（兼容 dict 与 openai pydantic 两形态）。

    pydantic 侧 SDK extra="allow"：未声明字段落 model_extra 且 getattr 可达，两路都试
    以覆盖 SDK 版本差异。非字符串或空值视为无思维链（返回 None，走上游原样路径）。
    """
    if message_obj is None:
        return None
    if isinstance(message_obj, Mapping):
        value = message_obj.get(_REASONING_FIELD)
    else:
        value = getattr(message_obj, _REASONING_FIELD, None)
        if value is None:
            extras = getattr(message_obj, "model_extra", None)
            if isinstance(extras, Mapping):
                value = extras.get(_REASONING_FIELD)
    if not isinstance(value, str) or not value:
        return None
    return value


def _raw_choice_messages(response: Any) -> list[Any]:
    """按序取出响应各 choice 的原始 message（dict 与 openai pydantic 两形态兼容）。"""
    if isinstance(response, Mapping):
        choices = response.get("choices")
    else:
        choices = getattr(response, "choices", None)
    messages: list[Any] = []
    for choice in choices or []:
        if isinstance(choice, Mapping):
            messages.append(choice.get("message"))
        else:
            messages.append(getattr(choice, "message", None))
    return messages


def _reasoning_text_blocks(
    reasoning: str, text: str, *, indexed: bool
) -> list[dict[str, Any]]:
    """组装 v1 content blocks：reasoning 块在前、非空正文块随后。

    indexed=True 供流式块携带合并锚点（index 0/1）；invoke 侧单条消息无聚合，不带
    index 保持块面干净。content 为空不产出 text 块（R7 思考耗尽形态不伪造空正文）。
    """
    blocks: list[dict[str, Any]] = [
        {"type": "reasoning", "reasoning": reasoning},
    ]
    if text:
        blocks.append({"type": "text", "text": text})
    for index, block in enumerate(blocks):
        if indexed:
            block["index"] = index
    return blocks


def _chunk_delta(chunk: Any) -> Any:
    """取流式分块首 choice 的 delta（镜像父类取法，兼容 beta.stream 嵌套形态）。"""
    if not isinstance(chunk, Mapping):
        return None
    nested = chunk.get("chunk")
    choices = chunk.get("choices") or (
        nested.get("choices") if isinstance(nested, Mapping) else None
    )
    if not choices:
        return None
    first = choices[0]
    if not isinstance(first, Mapping):
        return None
    delta = first.get("delta")
    return delta if isinstance(delta, Mapping) else None


class ReasoningChatModel(ChatOpenAI):
    """ChatOpenAI 子类：invoke 与 stream 双路透出 reasoning_content（v1 content blocks）。

    仅覆写思维链提取，不引入任何采样默认值——max_tokens 等 kept 上游语义（None，由
    Nacos 模型映射注入）；部署口径思考模型须 ≥8192（R7，见模块 docstring）。同步与
    异步入口共用同两处覆写（_generate/_agenerate → _create_chat_result；
    _stream/_astream → _convert_chunk_to_generation_chunk），无第二套实现。
    """

    def _create_chat_result(
        self,
        response: Any,
        generation_info: dict | None = None,
    ) -> ChatResult:
        """invoke 路径：父类转换后，按 choice 序号回填 reasoning_content 为 content blocks。"""
        result = super()._create_chat_result(response, generation_info=generation_info)
        raw_messages = _raw_choice_messages(response)
        for position, generation in enumerate(result.generations):
            if position >= len(raw_messages):
                break
            message = generation.message
            # 非 AIMessage（不应出现在 choices 里）与列表 content 一律不动：守普通路径
            if not isinstance(message, AIMessage) or not isinstance(
                message.content, str
            ):
                continue
            reasoning = _extract_reasoning_content(raw_messages[position])
            if reasoning is None:
                continue
            # langchain 的 content 声明为 str | list[str | dict[Any, Any]]（宽松 Any 键），
            # 本模块产出 str 键 dict 块——仅在赋值边界按上游声明形态收敛（运行期零变化）
            message.content = cast(
                "list[str | dict[Any, Any]]",
                _reasoning_text_blocks(reasoning, message.content, indexed=False),
            )
        return result

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: Any,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        """stream 路径：父类转换后，对携带 reasoning_content 增量的帧补思考块（带 index）。"""
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if generation_chunk is None:
            return None
        reasoning = _extract_reasoning_content(_chunk_delta(chunk))
        if reasoning is None:
            return generation_chunk
        message = generation_chunk.message
        if not isinstance(message, AIMessageChunk) or not isinstance(
            message.content, str
        ):
            return generation_chunk
        # 同 invoke 侧：仅在本模块 → langchain content 声明类型的赋值边界收敛
        message.content = cast(
            "list[str | dict[Any, Any]]",
            _reasoning_text_blocks(reasoning, message.content, indexed=True),
        )
        return generation_chunk


def _create_reasoning_model_instance(
    cls: type, **kwargs: Any
) -> Any:
    """`_BUILTIN_PROVIDERS` creator 约定：被 partial(creator, cls=...) 包裹后按 kwargs 构造
    （镜像 langchain.chat_models.base._call）。"""
    return cls(**kwargs)


# 注册（import 副作用，幂等）：init_chat_model(model_provider="openai_reasoning") 由此
# 生效——消费方 import 本模块即完成注册；setdefault 防重复导入/热载下覆写。
_lc_chat_models_base._BUILTIN_PROVIDERS.setdefault(
    _PROVIDER_NAME,
    (
        ReasoningChatModel.__module__,
        ReasoningChatModel.__name__,
        _create_reasoning_model_instance,
    ),
)

__all__ = ["ReasoningChatModel"]
