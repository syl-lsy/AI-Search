"""unit 层：recall 回顾整理轮全链路（v1.51——api.md §7.4 v1.22 recall 形态）。

钉住的语义（冻结契约，逐条对应断言）：
- **零检索**：`search_strategy=recall` 时 rewrite 条件边直达 synthesis——真主图运行中
  registry.search 零调用、零 stage(search)、零 sources/images 事件；
- **stage 序列两形态**：recall 形态 = `stage(rewrite,running) → stage(rewrite,done,
  extra={"strategy":"recall"}) → stage(write,running) → stage(write,done)`；
- **done 对账**：`finish=completed`、`source_count=0`、`degraded=false`；
- **历史注入**：`history_recall` 第二通道进 synthesis 提示词（`Q:/A:` 骨架块），
  `history`（rewrite 通道）不被 synthesis 消费；
- 图纯函数边界不变：全程 LLM/registry 假体零网络。
"""

from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from app.agent.graphs.main import graph as main_graph
from app.agent.nodes import fast_rewrite as rewrite_module
from app.agent.nodes import synthesis as synthesis_module
from app.services.event_bus import EventBus
from test.unit.turn_fakes import (
    _MAPPING_PAYLOAD,
    _R14_SPEC,
    _R14FakeRegistry,
    _FakeSession,
    _FakeSessionFactory,
    _TurnRow,
    _bus,
    _drain_all,
    _snapshot,
)

_HISTORY_ROWS = [
    ("我们刚才讨论了什么框架？", "本次讨论的是 LangGraph 与 pgvector 的选型。"),
]


class _RecallChain:
    """rewrite 结构化输出假体：返回 search_strategy=recall 的回顾整理判定。"""

    def __init__(self, captured: list[Any]) -> None:
        self._captured = captured

    async def ainvoke(self, prompt: Any, config: Any = None) -> dict[str, Any]:
        self._captured.append(prompt)
        return {
            "queries": ["回顾刚才聊了什么"],
            "query_type": "other",
            "answerable_from_knowledge": False,
            "search_strategy": "recall",
            "time_range": None,
            "memory_directive": None,
        }


class _FakeStreamChatModel(BaseChatModel):
    """synthesis 流式假体（真 BaseChatModel）：_astream 逐片产出文本增量。"""

    pieces: list[str] = []
    prompts: list[Any] = []

    @property
    def _llm_type(self) -> str:
        return "recall-round-fake-chat"

    async def _astream(self, messages: Any, stop: Any = None, **kwargs: Any) -> Any:
        self.prompts.append(messages)
        for text in self.pieces:
            yield ChatGenerationChunk(message=AIMessageChunk(content=text))

    async def _generate(self, messages: Any, stop: Any = None, **kwargs: Any) -> Any:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="".join(self.pieces)))])


def _install_models(
    monkeypatch: pytest.MonkeyPatch,
    *,
    synth_pieces: list[str] | None = None,
) -> tuple[list[Any], _FakeStreamChatModel]:
    prompts: list[Any] = []
    chain = _RecallChain(prompts)
    monkeypatch.setattr(
        rewrite_module,
        "build_light_model",
        lambda mapping: type(
            "M",
            (),
            {"with_structured_output": lambda self, schema, *, method=None, **kw: chain},
        )(),
    )
    model = _FakeStreamChatModel(
        pieces=synth_pieces if synth_pieces is not None else ["你之前聊了 LangGraph", "与 pgvector 的选型。"],
        prompts=[],
    )
    monkeypatch.setattr(synthesis_module, "build_chat_model", lambda alias: model)
    return prompts, model


def _scheduler(bus: EventBus, session: _FakeSession, registry: Any) -> Any:
    from app.services.turn_service import ThreadScheduler

    return ThreadScheduler(
        bus=bus,
        session_factory=_FakeSessionFactory(session),
        graph=main_graph,
        spec_snapshot=_snapshot(_R14_SPEC),
        mapping_snapshot=_snapshot(_MAPPING_PAYLOAD),
        tools_registry=registry,
    )


def _session(turn: _TurnRow) -> _FakeSession:
    """历史查询行集（`history` + `history_recall` 两通道共用同一 statement）。"""
    return _FakeSession(turn=turn, execute_rows=list(_HISTORY_ROWS))


async def test_recall_round_has_zero_search_and_two_stage_timeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """recall 形态全序列：零 stage(search)/sources/images；stage 序列 rewrite→write。"""
    _install_models(monkeypatch)
    registry = _R14FakeRegistry()
    bus = _bus()
    turn = _TurnRow(status="running", query="你之前聊了什么？")

    await _scheduler(bus, _session(turn), registry)._execute_turn(turn)

    published = await _drain_all(bus, str(turn.id))
    event_types = [event.event_type for event in published]
    assert "sources" not in event_types  # api.md §7.2 v1.22：recall 轮不发 sources
    assert "images" not in event_types
    assert registry.search_calls == 0  # 网络层零搜索调用

    stage_events = [event for event in published if event.event_type == "stage"]
    assert [
        (event.data["stage_key"], event.data["status"]) for event in stage_events
    ] == [
        ("rewrite", "running"),
        ("rewrite", "done"),
        ("write", "running"),
        ("write", "done"),
    ]
    assert "search" not in {event.data["stage_key"] for event in stage_events}
    # api.md §7.4 v1.22 recall 形态：stage(rewrite,done) extra 携带 strategy=recall
    assert stage_events[1].data["extra"] == {"strategy": "recall"}


async def test_recall_round_done_source_count_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """done 对账：source_count=0、degraded=false、finish=completed。"""
    _install_models(monkeypatch)
    bus = _bus()
    turn = _TurnRow(status="running", query="你之前聊了什么？")

    await _scheduler(bus, _session(turn), _R14FakeRegistry())._execute_turn(turn)

    published = await _drain_all(bus, str(turn.id))
    assert published[-1].event_type == "done"
    done = published[-1].data
    assert done["finish"] == "completed"
    assert done["source_count"] == 0
    assert done["degraded"] is False


async def test_recall_round_injects_history_recall_into_synthesis_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """历史经第二通道注入 synthesis：骨架块在场、recall 变体生效、历史内容非来源。"""
    _prompts, model = _install_models(monkeypatch)
    bus = _bus()
    turn = _TurnRow(status="running", query="你之前聊了什么？")

    await _scheduler(bus, _session(turn), _R14FakeRegistry())._execute_turn(turn)

    prompt_text = model.prompts[0][0].content
    assert "回顾整理路径" in prompt_text  # recall 变体段
    assert "Q: 我们刚才讨论了什么框架？" in prompt_text  # 问题全文
    assert "LangGraph 与 pgvector 的选型。" in prompt_text  # 答案骨架
    assert "不得编造 [n]" in prompt_text  # 零源轮无引用义务（禁编造）
    assert "<external_content" not in prompt_text  # 历史不是外部来源（声明行软防线）


async def test_non_recall_round_does_not_inject_history_recall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """token 纪律：web 轮（默认变体）不渲染历史块——非 recall 轮零增量。"""

    class _WebChain:
        async def ainvoke(self, prompt: Any, config: Any = None) -> dict[str, Any]:
            return {
                "queries": ["LangGraph 条件边"],
                "query_type": "factoid",
                "answerable_from_knowledge": False,
                "search_strategy": "web",
                "time_range": None,
                "memory_directive": None,
            }

    monkeypatch.setattr(
        rewrite_module,
        "build_light_model",
        lambda mapping: type(
            "M",
            (),
            {"with_structured_output": lambda self, schema, *, method=None, **kw: _WebChain()},
        )(),
    )
    model = _FakeStreamChatModel(pieces=["答案"], prompts=[])
    monkeypatch.setattr(synthesis_module, "build_chat_model", lambda alias: model)
    bus = _bus()
    turn = _TurnRow(status="running", query="LangGraph 条件边怎么写？")

    await _scheduler(bus, _session(turn), _R14FakeRegistry())._execute_turn(turn)

    prompt_text = model.prompts[0][0].content
    assert "LangGraph 与 pgvector 的选型。" not in prompt_text  # 历史零泄漏
    assert "常规路径" in prompt_text


async def test_recall_round_still_triggers_followups_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REQ-046 联动回归（D7 / 契约草案 §5.4-4）：recall 轮同样触发 followups 图外钩子。

    钩子判定条件（enabled ∧ mode=fast ∧ completed ∧ answer 非空）不含轮型排除——
    recall 轮零来源（`sources` 空数组）为模板既有支持（direct 轮 v1.44 先例同款）；
    本用例锁「recall 轮照常生成」而非生成内容本身（相关性归 M4 评测线）。
    """
    from app.services import turn_service

    captured: list[dict[str, Any]] = []

    async def _recorder(**kwargs: Any) -> list[str]:
        captured.append(kwargs)
        return []

    monkeypatch.setattr(turn_service, "generate_followups", _recorder)

    spec = {
        **_R14_SPEC,
        "followups": {
            "enabled": True,
            "count": 3,
            "generate_timeout_ms": 8000,
            "sse_hold_window_ms": 10000,
        },
    }
    _install_models(monkeypatch)
    bus = _bus()
    turn = _TurnRow(status="running", query="你之前聊了什么？")
    scheduler = turn_service.ThreadScheduler(
        bus=bus,
        session_factory=_FakeSessionFactory(_session(turn)),
        graph=main_graph,
        spec_snapshot=_snapshot(spec),
        mapping_snapshot=_snapshot(_MAPPING_PAYLOAD),
        tools_registry=_R14FakeRegistry(),
    )

    await scheduler._execute_turn(turn)

    assert len(captured) == 1  # recall 轮照常进钩子
    assert captured[0]["sources"] == []  # 零来源轮 sources 空数组（模板既有支持）
    assert captured[0]["answer"].strip()  # 答案非空（触发条件之一）
    published = await _drain_all(bus, str(turn.id))
    assert published[-1].event_type == "done"
