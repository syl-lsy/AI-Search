"""unit 层：direct 免检索直答轮事件流（v1.44 B9⑤/⑧——api.md §7.4 direct 形态）。

钉住的语义（冻结契约，逐条对应断言）：
- **零检索**：`search_strategy=direct` 时 rewrite 条件边直达 synthesis——真主图运行中
  registry.search 零调用、零 stage(search)、零 sources/images 事件（api.md §7.4 v1.14
  direct 形态「无 stage(search)/sources/images 事件」）；
- **stage 序列两形态**：direct 形态 = `stage(rewrite,running) → stage(rewrite,done,
  extra={"strategy":"direct"}) → stage(write,running) → stage(write,done)`；
- **done 对账**：`finish=completed`、`source_count=0`、`degraded=false`（不触搜索链 →
  500201 天然不可达；api.md §7.2 v1.14 done 条）；
- **followups 前提**：direct 轮 done 后仍有非空答案、且 done 逻辑零改（REQ-046 触发
  条件不含轮型排除；`{{sources_block}}` 空串为模板既有支持——本用例锁住前提而非生成
  钩子本身，生成钩子归 M4 实验线）；
- 图纯函数边界不变：全程 LLM/registry 假体零网络。
- **失败收束**（api.md §7.1 v1.10 例外条）：direct 轮恒零 sources ⇒ 综合失败必然属
  「无部分内容」形态——error 后**直接关闭、不发 done**（failed 终态经 §2.5 对账），
  绝不走有部分内容轮的 `done(finish=degraded)`；
- **搜索源全断**（registry 三级兜底全断形态 sources=[]/degraded=True）：direct 轮仍
  completed、零 error、零 search_calls（resolve 于装配段统一调用，但链不被 direct
  路径消费）。
"""

from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from app.agent.graphs.main import graph as main_graph
from app.agent.nodes import fast_rewrite as rewrite_module
from app.agent.nodes import synthesis as synthesis_module
from app.agent.tools.registry import SearchChain
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


class _DirectChain:
    """rewrite 结构化输出假体：返回 search_strategy=direct 的零事实面判定。"""

    def __init__(self, captured: list[Any]) -> None:
        self._captured = captured

    async def ainvoke(self, prompt: Any, config: Any = None) -> dict[str, Any]:
        self._captured.append(config)
        return {
            "queries": ["你好 能做什么"],
            "query_type": "other",
            "answerable_from_knowledge": False,
            "search_strategy": "direct",
            "time_range": None,
            "memory_directive": None,
        }


class _FakeStreamChatModel(BaseChatModel):
    """synthesis 流式假体（真 BaseChatModel）：_astream 逐片产出文本增量。"""

    pieces: list[str] = []

    @property
    def _llm_type(self) -> str:
        return "direct-round-fake-chat"

    async def _astream(self, messages: Any, stop: Any = None, **kwargs: Any) -> Any:
        for text in self.pieces:
            yield ChatGenerationChunk(message=AIMessageChunk(content=text))

    async def _generate(self, messages: Any, stop: Any = None, **kwargs: Any) -> Any:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="".join(self.pieces)))])


class _AllSourcesDownRegistry(_R14FakeRegistry):
    """registry 假体：搜索链全断形态（熔断开/无可用源）——resolve 返回空链 + degraded。

    与 `SearchChainUnavailable`（连接指针缺失=配置故障）区分：生产「源全断」正常
    形态是 resolve 成功但 sources 为空且 degraded=True（registry 三级兜底全断）。
    """

    def resolve(self, mcp_payload: Any, query_lang: str) -> SearchChain:
        return SearchChain(
            sources=[], backup=None, degraded=True, detail="全部搜索源不可用（假体）"
        )


def _install_models(
    monkeypatch: pytest.MonkeyPatch,
    *,
    synth_pieces: list[str] | None = None,
    rewrite_configs: list[Any] | None = None,
) -> None:
    """direct 轮假体：rewrite 判 direct + synthesis 流式产出（节点内 call-time 解析）。"""
    chain = _DirectChain(rewrite_configs if rewrite_configs is not None else [])
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
        pieces=synth_pieces if synth_pieces is not None else ["你好！", "我可以帮你联网搜索或做深度研究。"]
    )
    monkeypatch.setattr(synthesis_module, "build_chat_model", lambda alias: model)


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


async def test_direct_round_has_zero_search_and_no_sources_images_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """direct 形态全序列：零 stage(search)/sources/images；stage 序列 rewrite→write。"""
    _install_models(monkeypatch)
    registry = _R14FakeRegistry()
    bus = _bus()
    turn = _TurnRow(status="running", query="你好，能做什么？")
    session = _FakeSession(turn=turn)

    await _scheduler(bus, session, registry)._execute_turn(turn)

    published = await _drain_all(bus, str(turn.id))
    event_types = [event.event_type for event in published]
    assert "sources" not in event_types  # api.md §7.2 v1.14：direct 轮不发 sources
    assert "images" not in event_types  # 同 images「无图不发」抑制先例
    assert registry.search_calls == 0  # 网络层零搜索调用（A1 direct GWT 判据）

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
    # api.md §7.4 direct 形态：stage(rewrite,done) 的 extra 携带 strategy（§2.5 留痕）
    assert stage_events[1].data["extra"] == {"strategy": "direct"}


async def test_direct_round_done_source_count_zero_and_not_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """done 对账：source_count=0、degraded=false、finish=completed（不触搜索链）。"""
    _install_models(monkeypatch)
    bus = _bus()
    turn = _TurnRow(status="running", query="你好，能做什么？")
    session = _FakeSession(turn=turn)

    await _scheduler(bus, session, _R14FakeRegistry())._execute_turn(turn)

    published = await _drain_all(bus, str(turn.id))
    assert published[-1].event_type == "done"
    done = published[-1].data
    assert done["finish"] == "completed"
    assert done["source_count"] == 0
    assert done["degraded"] is False
    assert done["degraded_reason"] is None


async def test_direct_round_completes_with_answer_and_zero_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """followups 前提（REQ-046 契约）：direct 轮照常出行、答案非空、零 sources 不阻断 done。"""
    _install_models(monkeypatch, synth_pieces=["你好！", "我可以帮你搜索问题或做深度研究。"])
    bus = _bus()
    turn = _TurnRow(status="running", query="你好，能做什么？")
    session = _FakeSession(turn=turn)

    await _scheduler(bus, session, _R14FakeRegistry())._execute_turn(turn)

    published = await _drain_all(bus, str(turn.id))
    done_events = [event for event in published if event.event_type == "done"]
    assert len(done_events) == 1  # done 后 SSE 才进入 followups 保活窗口（图外钩子）
    assert turn.status == "completed"
    assert isinstance(turn.answer_md, str) and turn.answer_md.strip()
    assert done_events[0].data["source_count"] == 0


async def test_direct_round_completes_when_all_search_sources_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """动机回归（B9 测试审计 #1）：搜索源全断时 direct 轮仍照常完成（零 error/零搜索）。

    架构澄清（如实，按实现写断言）：组装段对**所有模式**统一调 `registry.resolve()`
    冻结本轮搜索链（`turn_service._execute_turn` 装配段），故 direct 轮并非「完全不碰
    registry」。生产「源全断」的形态是 resolve 成功但 `sources=[]` + `degraded=True`
    （registry 三级兜底全断）——该形态只落一条 warning 日志，链随 config 进图后**不被
    direct 路径消费**（rewrite 条件边直达 synthesis），轮次仍 completed。
    反向形态（resolve 抛 `SearchChainUnavailable` = 连接指针缺失/配置故障）属 api.md
    §7.2 500201「全部搜索源不可用」语义、与模式无关，故不在本用例断言面。
    """
    _install_models(monkeypatch)
    registry = _AllSourcesDownRegistry()
    bus = _bus()
    turn = _TurnRow(status="running", query="你好，能做什么？")
    session = _FakeSession(turn=turn)

    await _scheduler(bus, session, registry)._execute_turn(turn)

    published = await _drain_all(bus, str(turn.id))
    event_types = [event.event_type for event in published]
    assert "error" not in event_types  # 源全断不构成 direct 轮故障
    assert published[-1].event_type == "done"
    assert published[-1].data["finish"] == "completed"
    assert turn.status == "completed"
    assert isinstance(turn.answer_md, str) and turn.answer_md.strip()
    assert registry.search_calls == 0  # 零检索：链在但 direct 路径不消费
    assert not [f for f in published if f.event_type in ("sources", "images")]


async def test_direct_round_synthesis_exhausted_fails_without_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """direct 轮综合失败（重试耗尽）：error(500101) 后直接关闭、**不发 done**。

    契约依据（api.md §7.1 v1.10 例外条 + §7.2 error 收束两句 + §7.4 v1.14 direct 形态）：
    direct 轮恒零 sources（零检索）→ 失败**必然无部分内容** → 收束形态必须是
    「error 后直接关闭、无 done（failed 终态经 §2.5 对账）」，而非有部分内容轮的
    `done(finish=degraded)`。修复前实现无条件走 `_finish_degraded_with_error`
    （status=degraded + done(finish=degraded)），与本条冲突——本用例即红测试。
    """
    _install_models(monkeypatch, synth_pieces=[])  # 空产出 → synthesis 重试耗尽
    bus = _bus()
    turn = _TurnRow(status="running", query="你好，能做什么？")
    session = _FakeSession(turn=turn)

    await _scheduler(bus, session, _R14FakeRegistry())._execute_turn(turn)

    published = await _drain_all(bus, str(turn.id))
    event_types = [event.event_type for event in published]
    assert "done" not in event_types, "零内容轮不得发 done（§7.1 v1.10 例外条）"
    assert event_types[-1] == "error"
    error = published[-1].data
    assert error["code"] == "500101"  # LLM 调用重试耗尽（§7.2 SSE 专属码）
    assert turn.status == "failed", "零内容收束终态 = failed（§7.1 例外条）"
    assert turn.error is not None and turn.error["code"] == "500101"
    assert not turn.answer_md
    channel = bus._channels.get(str(turn.id))
    assert channel is not None and channel.terminal is True  # 通道已关闭（直接关闭口径）


async def test_direct_round_logs_strategy_trace(
    monkeypatch: pytest.MonkeyPatch, log_records: list[dict[str, Any]]
) -> None:
    """轮级留痕（ARCH §6.6 v1.44）：轮末结构化日志带 strategy/query_type/source_count。

    背景（如实）：Langfuse 轮级 trace 属性只能随根 run 创建写入，in-graph 决策晚于
    trace 创建且 v4 events_only 不接受事后 trace-create——可检索留痕走
    stage extra（turn_events）+ synthesis 生成 span metadata + 本条轮级日志。
    """
    _install_models(monkeypatch)
    bus = _bus()
    turn = _TurnRow(status="running", query="你好，能做什么？")
    session = _FakeSession(turn=turn)

    await _scheduler(bus, session, _R14FakeRegistry())._execute_turn(turn)

    messages = [record["message"] for record in log_records]
    assert any(
        "策略" in message
        and "strategy=direct" in message
        and "query_type=other" in message
        and "source_count=0" in message
        for message in messages
    )
