"""unit 层：synthesis 节点（B3 节点 5/5——ARCH §4.1 synthesis 行 / §6.4.3 包裹铁律 / §6.2）。

钉住的语义（冻结契约，逐条对应测试）：
- chat 模型（chat_model_key → 轮内映射快照 chat.options 别名，Standard 档）流式生成；
  token/thinking 经 messages 流出（LangGraph messages 模式捕获——节点只累积 text，
  思维链透传由 ReasoningChatModel+TurnRunner 承担）；
- ≤3000 字硬上限（spec fast.max_answer_chars 兜底截断——超出部分截去，单测锁死）；
- [n] 引用越界处理：n > 来源数或 <1 的引用标记剔除（不伪造编号——引用规则第 1/3 条）；
- 最小检索分支（answerable_from_knowledge=true）→ 提示词确定性切「先知识后取证」
  （组织方式段行级切换，键位与占位符集零变更——§4.1「提示词切」）；
- 外部内容逐条 <external_content source="{url}" type="web"> 包裹（§6.4.3 铁律——包裹
  归代码、声明行归提示词）；
- 全源失败短路防御（candidates/fused 空 + errors.source_failure → SearchChainUnavailable
  ——主短路在 merge_dedupe，本节点防御性复验）；
- LLM 重试耗尽（传输类按 spec.retry 重试 + deadline 门控）→ SynthesisError →
  TurnRunner error 500101（LLM 调用重试耗尽）+ done(finish=degraded)——已收集 sources
  保留（§4.1 synthesis 行；api.md §7.2 error 收束「有部分内容时 error 后跟 done(finish=degraded)」）；
- stage(write, running, label=综合中 → done) 夹全部动作（api.md §7.4）；
- 节点纯函数：模型工厂可注入可 mock。
"""

import asyncio
from typing import Any

import httpx

import pytest

from app.agent.nodes import synthesis as synthesis_module
from app.agent.nodes.synthesis import SynthesisError, synthesis

_SPEC: dict[str, Any] = {
    "fast": {
        "max_answer_chars": 3000,
        "retry": {
            "max_attempts": 2,
            "initial_interval_s": 0.01,
            "backoff_factor": 2.0,
            "jitter": False,
        },
    },
    "kb": {"rerank_timeout_s": 2},
}


def _cand(url: str, *, origin: str = "tavily", snippet: str = "正文片段") -> dict[str, Any]:
    return {"url": url, "title": f"t:{url}", "snippet": snippet, "score": 0.9, "origin": origin}


class _FakeChunk:
    """AIMessageChunk 假体：content str 或 v1 blocks（reasoning/text）。"""

    def __init__(self, content: Any) -> None:
        self.content = content


class _FakeModel:
    """chat 模型假体：astream 按脚本产出 chunk/异常；记录收到的 messages。"""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.prompts: list[list[Any]] = []

    def astream(self, messages: list[Any], config: Any = None, **kw: Any) -> Any:
        self.prompts.append(list(messages))
        if not self.script:  # 脚本耗尽即失败（防被测代码静默多调）
            raise AssertionError("FakeModel 脚本耗尽：astream 调用次数超出预期")
        item = self.script.pop(0)  # 每次调用消费一项（一项=单次尝试的 chunk 序列）

        async def gen():
            for piece in item:
                if isinstance(piece, Exception):
                    raise piece
                yield piece

        return gen()

    def with_structured_output(self, schema: Any, **kw: Any) -> Any:  # pragma: no cover
        raise AssertionError("synthesis 不使用结构化输出")


def _install(
    monkeypatch: pytest.MonkeyPatch,
    script: list[Any],
) -> tuple[_FakeModel, list[dict[str, Any]], list[dict[str, Any]]]:
    model = _FakeModel(script)
    monkeypatch.setattr(synthesis_module, "build_chat_model", lambda alias: model)
    stages: list[dict[str, Any]] = []
    monkeypatch.setattr(
        synthesis_module,
        "emit_stage",
        lambda key, status, **kw: stages.append({"stage_key": key, "status": status, **kw}),
    )
    return model, stages, []


def _state(
    *,
    fused: list[dict[str, Any]] | None = None,
    answerable: bool = False,
    query: str = "pgvector 和 Milvus 怎么选？",
    max_chars: int = 3000,
    errors: list[dict[str, Any]] | None = None,
    chat_model_key: str = "qwen",
) -> dict[str, Any]:
    return {
        "query": query,
        "fused": fused if fused is not None else [_cand("https://a/1"), _cand("https://b/2")],
        "answerable_from_knowledge": answerable,
        "query_lang": "zh",
        "spec": {"fast": {**_SPEC["fast"], "max_answer_chars": max_chars}},
        "errors": errors or [],
        "chat_model_key": chat_model_key,
    }


def _config(mapping: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "configurable": {
            "model_mapping": mapping
            if mapping is not None
            else {
                "chat": {"options": {"qwen": "chat-standard"}, "default": "qwen"},
                "rerank": "qwen3-rerank",
            }
        }
    }


def _text_chunks(*pieces: str) -> list[_FakeChunk]:
    return [_FakeChunk(p) for p in pieces]


# ---- 成功路径：流式累积 / 映射别名 / stage 事件 ----


async def test_synthesis_streams_accumulates_and_emits_write_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chat 别名经映射解析；流式累积成 answer_md；stage(write, running→done) 夹全部动作。"""
    model, stages, _ = _install(monkeypatch, [_text_chunks("答案第一段[1]。", "答案第二段[2]。")])

    update = await synthesis(_state(), _config())

    assert update["answer_md"] == "答案第一段[1]。答案第二段[2]。"
    assert model.prompts[0][-1].content == "pgvector 和 Milvus 怎么选？"  # 末条消息=用户问题
    assert [(s["stage_key"], s["status"]) for s in stages] == [
        ("write", "running"),
        ("write", "done"),
    ]
    assert stages[0]["label"] == "综合中"


async def test_synthesis_reasoning_blocks_not_in_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1 content blocks：text 块累积进答案；reasoning 块不进（思维链走 messages 流）。"""
    _install(monkeypatch, [[
        _FakeChunk([{"type": "reasoning", "reasoning": "先想一步"}]),
        _FakeChunk([{"type": "text", "text": "正文[1]"}]),
        _FakeChunk("更多[2]。"),
    ]])

    update = await synthesis(_state(), _config())

    assert update["answer_md"] == "正文[1]更多[2]。"


async def test_synthesis_uses_chat_alias_from_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """别名 = 轮内映射快照 chat.options[chat_model_key]（OQ-11 同源）。"""
    seen: dict[str, str] = {}

    def _spy(alias: str) -> _FakeModel:
        seen["alias"] = alias
        return _FakeModel([_text_chunks("ok")])

    monkeypatch.setattr(synthesis_module, "build_chat_model", _spy)
    monkeypatch.setattr(synthesis_module, "emit_stage", lambda *a, **kw: None)

    await synthesis(_state(chat_model_key="glm"), _config({
        "chat": {"options": {"glm": "chat-standard-glm"}, "default": "qwen"},
    }))

    assert seen["alias"] == "chat-standard-glm"


# ---- 硬上限 / 引用越界 ----


async def test_synthesis_truncates_to_max_answer_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    """≤max_answer_chars 硬上限：超出部分截去（兜底截断，单测锁死）。"""
    _install(monkeypatch, [_text_chunks("字" * 50)])
    state = _state(max_chars=10)

    update = await synthesis(state, _config())

    assert len(update["answer_md"]) == 10


async def test_synthesis_strips_out_of_range_citations(monkeypatch: pytest.MonkeyPatch) -> None:
    """[n] 越界剔除：n>来源数或 <1 的标记移除（不伪造编号）；合法 [n] 保留。"""
    _install(monkeypatch, [_text_chunks("事实一[1]；事实二[3]；事实三[0]；结论（推断）[2]。")])
    state = _state(fused=[_cand("https://a/1"), _cand("https://b/2")])  # 仅 2 条来源

    update = await synthesis(state, _config())

    assert update["answer_md"] == "事实一[1]；事实二；事实三；结论（推断）[2]。"


# ---- 提示词组装：包裹 / 最小检索切分 ----


async def test_synthesis_wraps_sources_in_external_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """§6.4.3：每个来源条目 <external_content source="{url}" type="web"> 包裹；编号可见。"""
    model, _, _ = _install(monkeypatch, [_text_chunks("答案[1]")])

    await synthesis(_state(), _config())

    prompt_text = model.prompts[0][0].content
    assert '<external_content source="https://a/1" type="web">' in prompt_text
    assert '<external_content source="https://b/2" type="web">' in prompt_text
    assert prompt_text.count("</external_content>") == 2
    assert "[1] t:https://a/1" in prompt_text  # 编号可见（[n] 对应来源列表第 n 条）


async def test_synthesis_default_path_uses_regular_paragraph(monkeypatch: pytest.MonkeyPatch) -> None:
    """answerable=false → 常规路径段保留、最小检索段剔除。"""
    model, _, _ = _install(monkeypatch, [_text_chunks("答案")])

    await synthesis(_state(answerable=False), _config())

    prompt_text = model.prompts[0][0].content
    assert "常规路径" in prompt_text
    assert "最小检索路径" not in prompt_text


async def test_synthesis_answerable_switches_to_knowledge_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """answerable=true → 最小检索段保留（「先知识后取证」）、常规段剔除（§4.1 提示词切）。"""
    model, _, _ = _install(monkeypatch, [_text_chunks("知识先答[1]")])

    await synthesis(_state(answerable=True), _config())

    prompt_text = model.prompts[0][0].content
    assert "最小检索路径" in prompt_text and "先知识后取证" in prompt_text
    assert "常规路径" not in prompt_text


async def test_synthesis_cite_rules_and_lang_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    """cite_rules（内置默认全文）与 query_lang 注入；memory_block 为空串（M5 前）。"""
    model, _, _ = _install(monkeypatch, [_text_chunks("答案")])

    await synthesis(_state(), _config())

    prompt_text = model.prompts[0][0].content
    assert "编号对应" in prompt_text  # citation_rules 内置全文注入
    assert "zh" in prompt_text
    assert "3000" in prompt_text  # max_answer_chars 注入


# ---- 短路 / 失败 ----


async def test_synthesis_defensive_short_circuit_on_all_sources_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """防御性复验主短路：fused 空 + errors.source_failure → SearchChainUnavailable（500201 面）。"""
    _install(monkeypatch, [_text_chunks("不应生成")])

    with pytest.raises(synthesis_module.SearchChainUnavailable):
        await synthesis(
            _state(fused=[], errors=[{"stage": "search", "reason": "source_failure"}]),
            _config(),
        )


async def test_synthesis_retry_exhausted_raises_synthesis_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """传输类失败按 spec.retry 重试（max_attempts=2）→ 耗尽 → SynthesisError（500101 面）。"""
    request = httpx_error = ConnectionError("网关断开")
    _install(monkeypatch, [httpx_error, httpx_error])  # 两次流式尝试均失败

    with pytest.raises(SynthesisError):
        await synthesis(_state(), _config())


async def test_synthesis_midstream_retry_restarts_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """流中失败（已累积部分文本后断开）→ 重试整段重流（累积重置不拼接残段）。"""
    _install(monkeypatch, [
        [*_text_chunks("残段"), ConnectionError("断")],  # 第 1 次尝试：先产出残段后断开
        _text_chunks("完整答案[1]。"),  # 第 2 次尝试成功
    ])

    update = await synthesis(_state(), _config())

    assert update["answer_md"] == "完整答案[1]。"


def test_synthesis_error_is_dedicated_type() -> None:
    """SynthesisError 独立类型（TurnRunner 捕获 → 500101 降级收束面）。"""
    assert issubclass(SynthesisError, Exception)
    assert synthesis_module.SynthesisError is SynthesisError


# ---- state 契约 ----


async def test_synthesis_writes_only_faststate_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """state 写入 ⊆ FastState 声明（answer_md/degraded）。"""
    _install(monkeypatch, [_text_chunks("答案[1]。")])

    update = await synthesis(_state(), _config())

    from app.agent.state import FastState

    assert set(update) <= set(FastState.__annotations__)
