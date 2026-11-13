"""unit 层：utils/reasoning_chat_model.py ReasoningChatModel 思维链透传补丁（R8 护栏）。

钉住的语义（ARCHITECTURE §6.7 + §10 R8 行 + s0-batch1-handoff 续 10 R7，逐条对应测试）：
- invoke 路径（覆写 `_create_chat_result`，invoke/ainvoke 共用）：choices[].message.
  reasoning_content 非空 → content 升 v1 content blocks [{reasoning}, {text}]；
  content 为空（思考耗尽全部 max_tokens 预算的实测形态，R7）→ 仅 [{reasoning}]，
  不补空 text 块；
- stream 路径（覆写 `_convert_chunk_to_generation_chunk`，stream/astream 共用）：
  delta.reasoning_content 增量 → [{reasoning, index 0}] 块；同帧携带 content → 同帧补
  {text, index 1}；index 是 AIMessageChunk 聚合的合并锚点（langchain-core merge_lists
  按 index 匹配、字符串字段拼接）——聚合后 reasoning 增量拼为单块、正文拼为单块；
- 普通路径零扰动（补丁唯一允许的行为差异边界）：无 reasoning_content 时 invoke 返回
  str content、stream chunk 保持 str content，与上游 ChatOpenAI 行为一致；
- 注册护栏：import 本模块即注册 openai_reasoning 进 langchain `_BUILTIN_PROVIDERS`
  （init_chat_model 的私有注册表），init_chat_model(model_provider="openai_reasoning")
  产出 ReasoningChatModel 实例（ARCH §6.7「供 init_chat_model 使用」）；
- 默认参数不写死 max_tokens（None）——思考模型部署口径 ≥8192（R7：2048 实测思考
  耗尽空输出、GLM-5.3 同病见 ARCH R11 行注），实际值由上游模型映射注入，代码侧不设
  默认值；
- R8 锁版本护栏本体：两处私有覆写必须在位——langchain-openai==1.7.4 锁定，升级
  必须先跑本护栏测试再合入（ARCH §10 R8）。

桩形态（零网络）：实例化后直接覆盖 model.client / model.async_client 为桩对象（构造
不触网），让父类真实 _generate/_stream/_agenerate/_astream 代码路径跑完——护栏必须
穿透父类行为（上游重构若改掉覆写点即红），不是只测覆写方法本身。响应/分块用裸 dict
与 openai pydantic 模型两种形态，覆盖 LiteLLM 网关透传的两条解析路径。
"""

import functools
import operator
import os
from types import SimpleNamespace

from langchain.chat_models import base as lc_chat_models_base
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice

from app.utils.reasoning_chat_model import _PROVIDER_NAME, ReasoningChatModel

# ---------------------------------------------------------------------------
# 桩：父类 client 消费形态逐一镜像（_generate→with_raw_response.create().parse()；
# _stream→create() 返回「上下文管理器+迭代器」；async 侧对应 await/async with）
# ---------------------------------------------------------------------------


class _StubRawResponse:
    """with_raw_response.create() 产物：parse() 返回预置响应（dict 或 openai pydantic）。"""

    def __init__(self, response: object) -> None:
        self._response = response

    def parse(self) -> object:
        return self._response


class _SyncChunkStream:
    """sync create() 产物：上下文管理器与迭代器二合一（父类 `with ... for ...` 用法）。"""

    def __init__(self, chunks: list[dict]) -> None:
        self._iter = iter(chunks)

    def __iter__(self) -> "_SyncChunkStream":
        return self

    def __next__(self) -> dict:
        return next(self._iter)

    def __enter__(self) -> "_SyncChunkStream":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class _AsyncChunkStream:
    """async create() 产物：async with + async for 二合一。"""

    def __init__(self, chunks: list[dict]) -> None:
        self._iter = iter(chunks)

    def __aiter__(self) -> "_AsyncChunkStream":
        return self

    async def __anext__(self) -> dict:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None

    async def __aenter__(self) -> "_AsyncChunkStream":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _StubSyncCompletions:
    """sync chat.completions 桩（client 字段覆盖物）。"""

    def __init__(self, *, response: object = None, chunks: list[dict] | None = None):
        self._response = response
        self._chunks = chunks or []

    @property
    def with_raw_response(self) -> SimpleNamespace:
        return SimpleNamespace(
            create=lambda **kwargs: _StubRawResponse(self._response)
        )

    def create(self, **kwargs: object) -> _SyncChunkStream:
        return _SyncChunkStream(self._chunks)


class _StubAsyncCompletions:
    """async chat.completions 桩（async_client 字段覆盖物）。"""

    def __init__(self, *, response: object = None, chunks: list[dict] | None = None):
        self._response = response
        self._chunks = chunks or []

    @property
    def with_raw_response(self) -> SimpleNamespace:
        outer = self

        class _Raw:
            def create(self, **kwargs: object) -> object:
                async def _call() -> _StubRawResponse:
                    return _StubRawResponse(outer._response)

                return _call()

        return _Raw()

    def create(self, **kwargs: object) -> object:
        async def _call() -> _AsyncChunkStream:
            return _AsyncChunkStream(self._chunks)

        return _call()


# 占位 api_key：root_client 在构造期即建，openai SDK 要求非空值；零网络桩下无真实
# 凭据——值从环境变量取（缺省为无意义假值，不写可用凭据字面量）
_STUB_API_KEY = os.environ.get("SEARCH_API_TEST_STUB_API_KEY", "fake-placeholder")


def _build_model(
    *, response: object = None, chunks: list[dict] | None = None
) -> ReasoningChatModel:
    """构造接桩模型：不触网（占位 api_key 仅过 SDK 构造校验，构造期零请求）。"""
    model = ReasoningChatModel(model="stub-model", api_key=_STUB_API_KEY)
    model.client = _StubSyncCompletions(response=response, chunks=chunks)
    model.async_client = _StubAsyncCompletions(response=response, chunks=chunks)
    return model


# ---------------------------------------------------------------------------
# 桩数据：LiteLLM 网关透传形态（R11 实证：非流式 message.reasoning_content 独立
# 字段、流式 delta.reasoning_content 增量、无 <think> 内嵌）
# ---------------------------------------------------------------------------


def _invoke_response(
    reasoning: str | None, content: str = "你好"
) -> dict:
    """非流式响应（dict 形态）。"""
    message: dict = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return {
        "id": "chatcmpl-stub",
        "model": "stub-model",
        "object": "chat.completion",
        "created": 1,
        "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
    }


def _invoke_response_pydantic(
    reasoning: str | None = "思考过程", content: str = "你好"
) -> ChatCompletion:
    """非流式响应（openai pydantic 形态，extra 字段落 model_extra——SDK 真实行为）。"""
    extra: dict = {}
    if reasoning is not None:
        extra["reasoning_content"] = reasoning
    message = ChatCompletionMessage(role="assistant", content=content, **extra)
    return ChatCompletion(
        id="chatcmpl-stub",
        choices=[Choice(index=0, finish_reason="stop", message=message)],
        created=1,
        model="stub-model",
        object="chat.completion",
    )


def _delta_chunk(
    delta: dict, *, finish_reason: str | None = None
) -> dict:
    """流式分块（dict 形态，父类循环里逐个消费）。"""
    return {
        "id": "chatcmpl-stub",
        "model": "stub-model",
        "object": "chat.completion.chunk",
        "created": 1,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
                "logprobs": None,
            }
        ],
    }


_STREAM_CHUNKS_WITH_REASONING = [
    _delta_chunk({"role": "assistant", "content": ""}),
    _delta_chunk({"reasoning_content": "先想"}),
    _delta_chunk({"reasoning_content": "一步"}),
    _delta_chunk({"content": "你"}),
    _delta_chunk({"content": "好"}),
    _delta_chunk({}, finish_reason="stop"),
]

_STREAM_CHUNKS_PLAIN = [
    _delta_chunk({"role": "assistant", "content": ""}),
    _delta_chunk({"content": "你"}),
    _delta_chunk({"content": "好"}),
    _delta_chunk({}, finish_reason="stop"),
]

# ---------------------------------------------------------------------------
# invoke 路径（_create_chat_result 覆写）
# ---------------------------------------------------------------------------


class TestInvokeReasoningExtraction:
    def test_reasoning_and_content_yield_v1_blocks(self):
        model = _build_model(response=_invoke_response(reasoning="思考过程", content="你好"))
        result = model.invoke([HumanMessage(content="问")])
        assert isinstance(result, AIMessage)
        assert result.content == [
            {"type": "reasoning", "reasoning": "思考过程"},
            {"type": "text", "text": "你好"},
        ]

    def test_reasoning_only_yields_no_empty_text_block(self):
        # R7 实测形态：思考耗尽全部 max_tokens → content 为空，仅透出 reasoning 块
        model = _build_model(response=_invoke_response(reasoning="思考过程", content=""))
        result = model.invoke([HumanMessage(content="问")])
        assert result.content == [{"type": "reasoning", "reasoning": "思考过程"}]

    def test_without_reasoning_keeps_plain_content(self):
        # 普通路径零扰动：content 保持上游原样 str
        model = _build_model(response=_invoke_response(reasoning=None, content="你好"))
        result = model.invoke([HumanMessage(content="问")])
        assert result.content == "你好"

    def test_pydantic_response_extracts_reasoning(self):
        # openai SDK extra="allow"：reasoning_content 落 model_extra，getattr 可达
        model = _build_model(response=_invoke_response_pydantic())
        result = model.invoke([HumanMessage(content="问")])
        assert result.content == [
            {"type": "reasoning", "reasoning": "思考过程"},
            {"type": "text", "text": "你好"},
        ]


# ---------------------------------------------------------------------------
# stream 路径（_convert_chunk_to_generation_chunk 覆写；astream 同点共用）
# ---------------------------------------------------------------------------


class TestStreamReasoningExtraction:
    def test_reasoning_deltas_become_indexed_blocks(self):
        model = _build_model(chunks=_STREAM_CHUNKS_WITH_REASONING)
        chunks = list(model.stream([HumanMessage(content="问")]))
        # role 帧：无 reasoning_content → 上游原样 str
        assert chunks[0].content == ""
        # reasoning 增量帧：[{reasoning, index 0}]（index 供聚合合并锚定）
        assert chunks[1].content == [
            {"type": "reasoning", "index": 0, "reasoning": "先想"}
        ]
        assert chunks[2].content == [
            {"type": "reasoning", "index": 0, "reasoning": "一步"}
        ]
        # 正文帧：无 reasoning_content → 上游原样 str
        assert chunks[3].content == "你"
        assert chunks[4].content == "好"

    def test_aggregate_merges_reasoning_and_text(self):
        model = _build_model(chunks=_STREAM_CHUNKS_WITH_REASONING)
        aggregated = functools.reduce(
            operator.add, model.stream([HumanMessage(content="问")])
        )
        # 聚合语义（langchain-core merge_lists/merge_dicts）：思考增量按 index 0 拼为
        # 单块；正文裸串拼接（merge_content 的 str→list 过渡会把 role 帧的 "" 留为
        # 首元素、正文并为其后裸串——content_blocks 归一化会丢裸串，故对 content 直断）
        reasoning_blocks = [
            block
            for block in aggregated.content
            if isinstance(block, dict) and block.get("type") == "reasoning"
        ]
        assert reasoning_blocks == [
            {"type": "reasoning", "reasoning": "先想一步", "index": 0}
        ]
        body = "".join(
            part for part in aggregated.content if isinstance(part, str)
        )
        assert body == "你好"

    def test_without_reasoning_keeps_plain_chunks(self):
        model = _build_model(chunks=_STREAM_CHUNKS_PLAIN)
        chunks = list(model.stream([HumanMessage(content="问")]))
        assert all(isinstance(chunk.content, str) for chunk in chunks)
        aggregated = functools.reduce(operator.add, chunks)
        assert aggregated.content == "你好"


# ---------------------------------------------------------------------------
# 异步双路（与 sync 共用同两处覆写点，护栏一并钉住）
# ---------------------------------------------------------------------------


class TestAsyncPaths:
    async def test_ainvoke_extracts_reasoning_blocks(self):
        model = _build_model(response=_invoke_response(reasoning="思考过程", content="你好"))
        result = await model.ainvoke([HumanMessage(content="问")])
        assert result.content == [
            {"type": "reasoning", "reasoning": "思考过程"},
            {"type": "text", "text": "你好"},
        ]

    async def test_astream_reasoning_deltas_as_blocks(self):
        model = _build_model(chunks=_STREAM_CHUNKS_WITH_REASONING)
        collected = [chunk async for chunk in model.astream([HumanMessage(content="问")])]
        assert collected[1].content == [
            {"type": "reasoning", "index": 0, "reasoning": "先想"}
        ]
        assert collected[3].content == "你"


# ---------------------------------------------------------------------------
# 注册护栏与默认参数（ARCH §6.7 / R7 口径）
# ---------------------------------------------------------------------------


class TestProviderRegistration:
    def test_openai_reasoning_in_builtin_providers(self):
        assert _PROVIDER_NAME in lc_chat_models_base._BUILTIN_PROVIDERS

    def test_init_chat_model_produces_reasoning_model(self):
        # api_key 经 creator kwargs 透传（root_client 构造期即建，占位值防无凭证红）
        model = init_chat_model(
            "stub-model", model_provider=_PROVIDER_NAME, api_key=_STUB_API_KEY
        )
        assert isinstance(model, ReasoningChatModel)


class TestDefaultParams:
    def test_max_tokens_not_hardcoded(self):
        # R7 口径：思考模型须 ≥8192，实际值由上游模型映射注入；补丁不设默认值
        model = ReasoningChatModel(model="stub-model", api_key=_STUB_API_KEY)
        assert model.max_tokens is None
        assert "max_tokens" not in model._default_params


class TestR8GuardOverrides:
    def test_overrides_are_in_place(self):
        # R8 前置哨兵：两处私有覆写必须在位（被移除 → 本文件行为用例随之失效）
        assert ReasoningChatModel._create_chat_result is not (
            ChatOpenAI._create_chat_result
        )
        assert ReasoningChatModel._convert_chunk_to_generation_chunk is not (
            ChatOpenAI._convert_chunk_to_generation_chunk
        )


# ---------------------------------------------------------------------------
# 结构化输出交互护栏（2026-11-13 A 裁决）：ReasoningChatModel 覆写 × rewrite 节点
# with_structured_output(method="function_calling") 的真实链路形状——性能抽样
# 41/41 mechanism 兜底事故的形状锁面（真实响应形状 fixture，非 mock 恒真形态）。
# 钉住语义：覆写**不得破坏 tool_calls 面**——tool_calls × reasoning_content 同帧
# （think=false 失效时的真实形态）× content 空/None 两形态下，结构化解析链必须
# 端到端产出合法 RewriteOutput（langchain 解析数据源 = AIMessage.tool_calls）。
# ---------------------------------------------------------------------------


def _invoke_response_with_tool_call(
    *,
    reasoning: str | None,
    content: str | None,
    arguments: str,
    name: str = "RewriteOutput",
) -> dict:
    """工具调用响应（dict 形态，LiteLLM 网关透传路径）：tool_calls + content +
    reasoning_content 同帧——rewrite 结构化调用的真实响应形状。"""
    message: dict = {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": "call_stub_1",
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
        ],
    }
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return {
        "id": "chatcmpl-stub-tool",
        "model": "stub-model",
        "object": "chat.completion",
        "created": 1,
        "choices": [
            {"index": 0, "finish_reason": "tool_calls", "message": message}
        ],
    }


_VALID_TOOL_ARGS = (
    '{"queries": ["TCP 三次握手流程"], "query_type": "factoid", '
    '"answerable_from_knowledge": true, "time_range": null, '
    '"memory_directive": null}'
)


class TestStructuredOutputInteraction:
    """tool_calls × reasoning_content 同帧 × content 空/None 形态矩阵。"""

    def _structured_out(self, response: dict):
        from app.agent.nodes.fast_rewrite import RewriteOutput

        model = _build_model(response=response)
        chain = model.with_structured_output(RewriteOutput, method="function_calling")
        return chain.invoke([HumanMessage(content="q")])

    def test_tool_call_reasoning_with_empty_content_parses(self):
        """content="" + reasoning 非空：覆写守卫触发（str content → blocks）但
        tool_calls 面不丢——结构化解析端到端成功（rewrite 主形态）。"""
        from app.agent.nodes.fast_rewrite import RewriteOutput

        out = self._structured_out(
            _invoke_response_with_tool_call(
                reasoning="思考文本", content="", arguments=_VALID_TOOL_ARGS
            )
        )
        assert isinstance(out, RewriteOutput)
        assert out.queries == ["TCP 三次握手流程"]
        assert out.query_type == "factoid"
        assert out.answerable_from_knowledge is True

    def test_tool_call_reasoning_with_none_content_parses(self):
        """content=None + reasoning 非空：守卫跳过（None 非 str，零扰动）——
        tool_calls 原样保留，结构化解析端到端成功。"""
        from app.agent.nodes.fast_rewrite import RewriteOutput

        out = self._structured_out(
            _invoke_response_with_tool_call(
                reasoning="思考文本", content=None, arguments=_VALID_TOOL_ARGS
            )
        )
        assert isinstance(out, RewriteOutput)
        assert out.queries == ["TCP 三次握手流程"]

    def test_tool_call_without_reasoning_parses(self):
        """content="" 无 reasoning：普通路径零扰动（覆写不触发）——结构化解析
        端到端成功（think=false 生效时的常态形态，B3 实证基线）。"""
        from app.agent.nodes.fast_rewrite import RewriteOutput

        out = self._structured_out(
            _invoke_response_with_tool_call(
                reasoning=None, content="", arguments=_VALID_TOOL_ARGS
            )
        )
        assert isinstance(out, RewriteOutput)
        assert out.queries == ["TCP 三次握手流程"]

    def test_tool_call_preserved_in_message_when_reasoning_present(self):
        """AIMessage.tool_calls 必须在覆写后保留（langchain 解析数据源不丢）——
        覆写仅动 content、不触碰 additional_kwargs/tool_calls 面。"""
        model = _build_model(
            response=_invoke_response_with_tool_call(
                reasoning="思考文本", content="", arguments=_VALID_TOOL_ARGS
            )
        )
        message = model.invoke([HumanMessage(content="q")])
        assert isinstance(message, AIMessage)
        assert len(message.tool_calls) == 1
        assert message.tool_calls[0]["name"] == "RewriteOutput"
        assert message.tool_calls[0]["args"]["query_type"] == "factoid"
