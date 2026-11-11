"""unit 层：R14 跨子图边界两项实测锁死（批次 5 闭环条件⑤⑥——ARCH §10 R14 行）。

R14①（messages 过滤，§6.1 v1.20 过滤规则 / api.md §7.2 token/thinking 产生面）：
- `_is_fast_synthesis_messages` 行为锁死：非 synthesis 节点帧不映射（token/thinking
  零误产）、thinking 与 token 按帧分流（reasoning_content→thinking / content→token）；
- 真主图 astream(subgraphs=True) 帧形态实测锁死：messages 帧仅产自 fast 子图
  synthesis 节点（namespace=() + metadata.langgraph_node 判定——B3 探针形态），
  rewrite/web_search/merge_dedupe/fuse_rerank 与路由壳零 messages 产出；
- deep 桩零泄漏：真 deep 子图经主图壳运行零 messages 帧、TurnRunner 侧零
  token/thinking 事件（R14③ 现状基线，deepagents 实装时收敛）。

R14②（取消透传，ARCH R14② / guards.py 注入面口径）：
- guards 节点边界感知：进程级 `_cancel_flags` dict 引用经 astream configurable
  注入，跨主图壳→子图边界后节点 `pop_cancel_flag` 命中即上抛 TurnCanceledSignal
  （取消非故障；图内只感知不落库，canceled 收尾归 TurnRunner）；
- TurnRunner 捕获 TurnCanceledSignal → `_finish_canceled`（非 error 路径）；
- 三注入点实测：rewrite 前（组装段后即时取消）/ Send 扇出进行中 / synthesis
  流式中——真主图（LLM 假体）+ ThreadScheduler 全链路。
"""

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from app.agent.graphs.deep import graph as deep_graph
from app.agent.graphs.main import graph as main_graph
from app.agent.nodes import fast_rewrite as rewrite_module
from app.agent.nodes import fuse_rerank as fuse_module
from app.agent.nodes import merge_dedupe as merge_module
from app.agent.nodes import synthesis as synthesis_module
from app.agent.nodes import web_search as ws_module
from app.agent.tools.registry import SearchChain, ToolRegistry
from app.services.event_bus import EventBus
from test.unit.test_turn_service import (
    _MAPPING_PAYLOAD,
    _SPEC_PAYLOAD,
    _FakeGraph,
    _FakeSession,
    _FakeSessionFactory,
    _MessageChunk,
    _TurnRow,
    _bus,
    _drain_all,
    _snapshot,
)

#: R14 用 spec 快照：带 routing（路由正常路径）+ fast 节（synthesis max_answer_chars）
_R14_SPEC: dict[str, Any] = {
    **_SPEC_PAYLOAD,
    "routing": {"default_mode": "fast", "modes": {"fast": "fast", "deep": "deep"}},
    "fast": {**_SPEC_PAYLOAD["fast"], "max_answer_chars": 3000},
}


# ---- LLM 假体（真主图运行零网络；test_stub_graphs 同构）----


class _R14FakeRegistry(ToolRegistry):
    """registry 假体：resolve 冻结链 [tavily]、search 返回确定性候选 + 可挂 hook。"""

    def __init__(self, on_search: Callable[[int], None] | None = None) -> None:
        super().__init__()
        self._on_search = on_search
        self.search_calls = 0

    def resolve(self, mcp_payload: Any, query_lang: str) -> SearchChain:
        return SearchChain(sources=["tavily"], backup=None, degraded=False, detail="tavily")

    async def search(
        self, query: str, source: str, *, time_range: str | None = None, top_k: int = 5
    ) -> list[dict[str, Any]]:
        self.search_calls += 1
        if self._on_search is not None:
            self._on_search(self.search_calls)
        if source == "tavily":
            return [
                {"url": "https://docs.langgraph.dev/send", "title": "Send API",
                 "snippet": "Send 扇出", "score": 0.92, "origin": "tavily"},
                {"url": "https://docs.langgraph.dev/state", "title": "State",
                 "snippet": "状态归并", "score": 0.85, "origin": "tavily"},
            ]
        return []

    def credential_ready(self, server: str) -> bool:  # 单测环境零凭证依赖
        return True


class _CaptureChain:
    """rewrite 结构化输出假体：记录收到的 config（注入面断言用）。"""

    def __init__(self, captured: list[Any]) -> None:
        self._captured = captured

    async def ainvoke(self, prompt: Any, config: Any = None) -> dict[str, Any]:
        self._captured.append(config)
        return {
            "queries": ["r14 跨子图检索"],
            "query_type": "factoid",
            "answerable_from_knowledge": False,
            "time_range": None,
            "memory_directive": None,
        }


class _HookedFakeChatModel(BaseChatModel):
    """synthesis 流式假体（真 BaseChatModel）：_astream 逐片 yield ChatGenerationChunk——
    基类 astream 自动发 on_llm_new_token 回调，LangGraph messages 流经 callback tap
    捕获（duck-typed astream 不触发 messages 帧——实测验证点）。每片前可挂 hook。"""

    pieces: list[tuple[str, str]] = []  # (text, kind)；kind ∈ text | reasoning
    on_chunk: Any = None  # Callable[[], None] | None

    @property
    def _llm_type(self) -> str:
        return "r14-fake-chat"

    async def _astream(self, messages: Any, stop: Any = None, **kwargs: Any) -> Any:
        for text, kind in self.pieces:
            result = self.on_chunk() if self.on_chunk is not None else None
            if inspect.isawaitable(result):
                await result  # 异步 hook：可等待（登记前让消费侧排空先前帧——确定性交错）
            if kind == "reasoning":
                message = AIMessageChunk(content="", additional_kwargs={"reasoning_content": text})
            else:
                message = AIMessageChunk(content=text)
            yield ChatGenerationChunk(message=message)

    async def _generate(self, messages: Any, stop: Any = None, **kwargs: Any) -> Any:
        text = "".join(t for t, k in self.pieces if k == "text")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])


def _mock_llm(
    monkeypatch: pytest.MonkeyPatch,
    *,
    synth_on_chunk: Callable[[], None] | None = None,
    synth_pieces: list[tuple[str, str]] | None = None,
    rewrite_captured_configs: list[Any] | None = None,
) -> None:
    """rewrite/synthesis 假体直注（节点内 call-time 解析，编译图零重编译）。"""
    chain = _CaptureChain(rewrite_captured_configs if rewrite_captured_configs is not None else [])
    monkeypatch.setattr(
        rewrite_module, "build_light_model", lambda mapping: type("M", (), {
            "with_structured_output": lambda self, schema, *, method=None, **kw: chain,
        })()
    )
    model = _HookedFakeChatModel(
        pieces=synth_pieces
        if synth_pieces is not None
        else [("综合回答正文[1]。", "text"), ("结论[2]。", "text"), ("补充说明。", "text")],
        on_chunk=synth_on_chunk,
    )
    monkeypatch.setattr(synthesis_module, "build_chat_model", lambda alias: model)


def _r14_scheduler(
    bus: EventBus,
    session: _FakeSession,
    *,
    graph: Any = main_graph,
    registry: _R14FakeRegistry | None = None,
) -> Any:
    """真主图 + 假 registry 的 ThreadScheduler（构造注入，快照直构）。"""
    from app.services.turn_service import ThreadScheduler

    return ThreadScheduler(
        bus=bus,
        session_factory=_FakeSessionFactory(session),
        graph=graph,
        spec_snapshot=_snapshot(_R14_SPEC),
        mapping_snapshot=_snapshot(_MAPPING_PAYLOAD),
        tools_registry=registry if registry is not None else _R14FakeRegistry(),
    )


# ==== R14①：messages 过滤锁死 ====


async def test_filter_blocks_non_synthesis_messages_frames() -> None:
    """非 synthesis 节点帧不映射：rewrite/web_search/merge_dedupe/fuse_rerank 与
    无 metadata 帧一律不放行——token/thinking 零误产；仅 synthesis 帧映射。"""
    from app.services.turn_service import _is_fast_synthesis_messages  # noqa: SLF001

    bus = _bus()
    turn = _TurnRow(status="running")
    session = _FakeSession(turn=turn)
    frames = [
        ("custom", {"type": "stage", "stage_key": "rewrite", "status": "done"}),
        ("messages", (_MessageChunk("改写泄漏"), {"langgraph_node": "rewrite"})),
        ("messages", (_MessageChunk("搜索泄漏"), {"langgraph_node": "web_search"})),
        ("messages", (_MessageChunk("归并泄漏"), {"langgraph_node": "merge_dedupe"})),
        ("messages", (_MessageChunk("融合泄漏"), {"langgraph_node": "fuse_rerank"})),
        ("messages", (_MessageChunk("路由壳泄漏"), {"langgraph_node": "route"})),
        ("messages", (_MessageChunk("无元数据泄漏"), None)),
        ("messages", (_MessageChunk("综合正文", reasoning="综合思考"), {"langgraph_node": "synthesis"})),
    ]

    class _ScriptedGraph(_FakeGraph):
        def astream(self, payload: Any, config: Any = None, stream_mode: Any = None,
                    subgraphs: bool = False) -> Any:
            async def gen():
                for stream, chunk in frames:
                    yield ((), stream, chunk)

            return gen()

    from app.services.turn_service import ThreadScheduler

    scheduler = ThreadScheduler(
        bus=bus,
        session_factory=_FakeSessionFactory(session),
        graph=_ScriptedGraph([("custom", {"type": "stage", "stage_key": "rewrite", "status": "done"})]),
        spec_snapshot=_snapshot(_R14_SPEC),
        mapping_snapshot=_snapshot(_MAPPING_PAYLOAD),
        tools_registry=ToolRegistry(),  # 裸 registry：假图不触网（缺省装配未启动会抛）
    )
    await scheduler._execute_turn(turn)

    published = await _drain_all(bus, str(turn.id))
    token_events = [e for e in published if e.event_type in ("token", "thinking")]
    assert [(e.event_type, e.data["delta"]) for e in token_events] == [
        ("thinking", "综合思考"),
        ("token", "综合正文"),
    ]  # 仅 synthesis 帧（先 thinking 后 token 分流）；其余节点帧零泄漏

    # 过滤判定面直锁：非 synthesis 元数据恒 False（含 metadata 缺失/无 langgraph_node）
    assert _is_fast_synthesis_messages((), (_MessageChunk("x"), {"langgraph_node": "rewrite"})) is False
    assert _is_fast_synthesis_messages(("fast:run",), (_MessageChunk("x"), {"langgraph_node": "synthesis"})) is True
    assert _is_fast_synthesis_messages((), (_MessageChunk("x"), None)) is False
    assert _is_fast_synthesis_messages((), _MessageChunk("x")) is False


async def test_reasoning_blocks_map_to_thinking_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ReasoningChatModel v1 content 块形态实测（批次 5 R14① 探针）：qwen 经
    openai_reasoning provider 的思维链以 content 块 `type='reasoning'`（键 `reasoning`）
    到达（reasoning_chat_model.py 文档形态）——映射为 thinking 事件；text 块映射 token
    （thinking/token 分流双形态锁死：reasoning 块 + reasoning_content kw + thinking 块）。"""
    from app.services.turn_service import ThreadScheduler

    bus = _bus()
    turn = _TurnRow(status="running")
    session = _FakeSession(turn=turn)
    frames = [
        ("messages", (_MessageChunk("综合回答正文"), {"langgraph_node": "synthesis"})),
        # R8 补丁实发形态：v1 content 块 type=reasoning（键 reasoning）
        (
            "messages",
            (
                _MessageChunk([{"type": "reasoning", "reasoning": "先想一步", "index": 0}]),
                {"langgraph_node": "synthesis"},
            ),
        ),
    ]

    class _ScriptedGraph(_FakeGraph):
        def astream(self, payload: Any, config: Any = None, stream_mode: Any = None,
                    subgraphs: bool = False) -> Any:
            async def gen():
                for stream, chunk in frames:
                    yield ((), stream, chunk)

            return gen()

    scheduler = ThreadScheduler(
        bus=bus,
        session_factory=_FakeSessionFactory(session),
        graph=_ScriptedGraph([]),
        spec_snapshot=_snapshot(_R14_SPEC),
        mapping_snapshot=_snapshot(_MAPPING_PAYLOAD),
        tools_registry=ToolRegistry(),
    )
    await scheduler._execute_turn(turn)

    published = await _drain_all(bus, str(turn.id))
    token_events = [(e.event_type, e.data["delta"]) for e in published if e.event_type in ("token", "thinking")]
    assert token_events == [
        ("token", "综合回答正文"),  # 帧 1：str content → token
        ("thinking", "先想一步"),  # 帧 2：reasoning 块 → thinking（此前被静默丢弃——实测修复）
    ]


async def test_real_main_graph_messages_frames_only_from_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真主图 astream(subgraphs=True) 帧形态实测锁死（B3 探针形态回归）：
    messages 帧仅产自 synthesis（namespace=()、节点标识在 metadata.langgraph_node）；
    rewrite/web_search/merge_dedupe/fuse_rerank 与路由壳零 messages 产出
    （rewrite/search/sources/write 信号照常，证明节点确实运行过）。"""
    _mock_llm(monkeypatch)
    registry = _R14FakeRegistry()
    payload: dict[str, Any] = {
        "turn_id": "r14-turn",
        "thread_id": "r14-thread",
        "user_id": "00000000-0000-4000-8000-000000000001",
        "mode": "fast",
        "query": "R14 messages 过滤实测",
        "chat_model_key": "qwen",
        "history": [],
        "query_lang": "zh",
        "spec": dict(_R14_SPEC),
        "images": [],
    }
    config = {
        "configurable": {
            "search_chain": SearchChain(sources=["tavily"], backup=None, degraded=False, detail="tavily"),
            "tools_registry": registry,
        }
    }
    message_nodes: set[str] = set()
    message_namespaces: set[Any] = set()
    custom_types: list[str] = []
    inner_update_nodes: set[str] = set()  # 子图命名空间帧的内层节点键
    top_update_nodes: set[str] = set()  # 顶层（namespace=()）updates 键
    async for namespace, stream_mode, chunk in main_graph.astream(
        payload, config=config, stream_mode=["messages", "custom", "updates"], subgraphs=True
    ):
        if stream_mode == "messages":
            chunk_pair = chunk
            meta = chunk_pair[1] if isinstance(chunk_pair, tuple) and len(chunk_pair) > 1 else None
            node = meta.get("langgraph_node") if isinstance(meta, dict) else None
            message_nodes.add(str(node))
            message_namespaces.add(tuple(namespace))
        elif stream_mode == "custom":
            custom_types.append(str(chunk.get("type")))
        elif stream_mode == "updates":
            if namespace == ():
                top_update_nodes.update(chunk.keys())  # 子图聚合帧：{'fast': <子图终态>}
            elif str(namespace[0]).startswith("fast:"):
                inner_update_nodes.update(chunk.keys())  # 内层节点帧：{'rewrite': {...}} 等

    assert message_nodes == {"synthesis"}  # 仅 synthesis 产 messages（路由壳/其余节点零产出）
    # 主图壳运行形态实测（2026-11-11）：subgraphs=True 下 messages 帧 namespace 为
    # 子图段 ('fast:<run_id>',)——B3 探针「空 namespace」系子图直跑形态；过滤器按
    # metadata.langgraph_node 判定、与 namespace 形态无关（两形态均正确放行/拦截）
    assert message_namespaces and all(
        len(ns) == 1 and str(ns[0]).startswith("fast:") for ns in message_namespaces
    )
    assert set(custom_types) == {"stage", "sources"}  # custom 仅 stage/sources（token 不在 custom 域）
    # updates 双层形态（TurnRunner final_state 消费面）：顶层键=子图节点名（聚合投影回填）、
    # 子图命名空间帧键=内层节点名（五节点全运行的证明）
    assert top_update_nodes == {"fast"}
    assert inner_update_nodes == {"rewrite", "web_search", "merge_dedupe", "fuse_rerank", "synthesis"}


async def test_deep_stub_zero_messages_and_runner_zero_token_events() -> None:
    """deep 桩零泄漏（R14③ 现状基线）：真 deep 子图经主图壳运行——
    (a) raw 帧面零 messages 帧（桩 token 走 custom、被翻译器丢弃）；
    (b) TurnRunner 侧零 token/thinking SSE 事件（stage 序列 + done 正常）。"""
    # (a) raw 帧面
    payload: dict[str, Any] = {
        "turn_id": "r14-deep",
        "thread_id": "r14-thread",
        "user_id": "00000000-0000-4000-8000-000000000001",
        "mode": "deep",
        "query": "R14 deep 桩零泄漏实测",
        "chat_model_key": "qwen",
        "history": [],
        "query_lang": "zh",
        "spec": dict(_R14_SPEC),
        "images": [],
    }
    message_count = 0
    async for _namespace, stream_mode, _chunk in main_graph.astream(
        payload, config={"configurable": {}}, stream_mode=["messages", "custom"], subgraphs=True
    ):
        if stream_mode == "messages":
            message_count += 1
    assert message_count == 0

    # (b) TurnRunner 侧
    bus = _bus()
    turn = _TurnRow(status="running", mode="deep")
    session = _FakeSession(turn=turn)
    scheduler = _r14_scheduler(bus, session, graph=deep_graph)
    await scheduler._execute_turn(turn)
    published = await _drain_all(bus, str(turn.id))
    assert [e.event_type for e in published if e.event_type in ("token", "thinking")] == []
    assert published[-1].event_type == "done" and published[-1].data["finish"] == "completed"
    assert turn.status == "completed"  # 桩答案经 updates 回填（custom token 被丢弃不影响终态）


# ==== R14②：取消透传（guards 节点边界感知 + TurnRunner 收尾）====


async def test_fast_nodes_entry_senses_registered_cancel_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """guards 节点边界感知（ARCH 口径）：进程级 cancel_flags 经 configurable 注入面
    跨主图壳→子图边界可达——五个 fast 节点入口 pop 命中即上抛 TurnCanceledSignal
    （取消非故障；pop 即感知+清理）。"""
    from app.agent.guards import TurnCanceledSignal

    turn_id = "r14-node-cancel"

    cases: list[tuple[Callable[[Any, Any], Any], dict[str, Any]]] = [
        (rewrite_module.rewrite, {"query": "q", "spec": {}, "history": [], "query_lang": "zh"}),
        (ws_module.web_search, {"query": "q", "search_query_index": 0, "search_query_total": 1}),
        (merge_module.merge_dedupe, {"candidates": []}),
        (fuse_module.fuse_rerank, {"fused": [], "query": "q", "spec": {}}),
        (synthesis_module.synthesis, {"query": "q", "fused": [], "errors": [], "spec": {}}),
    ]
    for node, state in cases:
        flags = {turn_id: "superseded"}  # 每用例独立 dict（pop 即感知+清理——不可共享）
        config = {"configurable": {"turn_id": turn_id, "cancel_flags": flags}}
        with pytest.raises(TurnCanceledSignal) as exc_info:
            if asyncio.iscoroutinefunction(node):
                await node(state, config)
            else:
                node(state, config)
        assert exc_info.value.reason == "superseded"
        assert flags == {}  # pop 即感知+清理（与 TurnRunner _cancel_flags 同语义）

    # 清理后同节点正常执行不误伤（merge_dedupe 纯函数路径）
    clean_config = {"configurable": {"turn_id": turn_id, "cancel_flags": {}}}
    update = merge_module.merge_dedupe({"candidates": []}, clean_config)
    assert isinstance(update, dict)


async def test_runner_finishes_canceled_when_node_raises_turn_canceled_signal() -> None:
    """TurnRunner 捕获 TurnCanceledSignal（节点边界感知上抛形态）→ canceled 收尾：
    done(finish=canceled, cancel_reason=...)、非 error 路径（打断不是故障）。"""
    from app.agent.guards import TurnCanceledSignal

    bus = _bus()
    turn = _TurnRow(status="running")
    session = _FakeSession(turn=turn)

    class _NodeSensedGraph(_FakeGraph):
        """首事件后上抛 TurnCanceledSignal——模拟子图节点边界感知后的透传形态。"""

        def astream(self, payload: Any, config: Any = None, stream_mode: Any = None,
                    subgraphs: bool = False) -> Any:
            async def gen():
                yield (("fast:run",), "custom", {"type": "stage", "stage_key": "rewrite", "status": "done"})
                raise TurnCanceledSignal("superseded")

            return gen()

    from app.services.turn_service import ThreadScheduler

    scheduler = ThreadScheduler(
        bus=bus,
        session_factory=_FakeSessionFactory(session),
        graph=_NodeSensedGraph([]),
        spec_snapshot=_snapshot(_R14_SPEC),
        mapping_snapshot=_snapshot(_MAPPING_PAYLOAD),
        tools_registry=ToolRegistry(),
    )
    await scheduler._execute_turn(turn)

    published = await _drain_all(bus, str(turn.id))
    # 信号前已产帧保留（api.md「打断保留已产出」）：first_stage + rewrite done + done
    assert [e.event_type for e in published] == ["stage", "stage", "done"]
    done = published[-1]
    assert done.data["finish"] == "canceled" and done.data["cancel_reason"] == "superseded"
    assert turn.status == "canceled" and turn.cancel_reason == "superseded"
    assert turn.error is None  # 取消不产生 error（api.md §7.2 打断非故障）
    channel = bus._channels.get(str(turn.id))
    assert channel is not None and channel.terminal is True


async def test_turn_runner_injects_process_level_cancel_flags_reference() -> None:
    """TurnRunner astream configurable 注入进程级 _cancel_flags dict 引用
    （guards pop_cancel_flag 注入面——ARCH 口径「引用注入、跨子图可达」）。"""
    bus = _bus()
    turn = _TurnRow(status="running")
    session = _FakeSession(turn=turn)
    graph = _FakeGraph([
        ("custom", {"type": "stage", "stage_key": "rewrite", "status": "done"}),
        ("updates", {"synthesis": {"answer_md": "答案"}}),
    ])
    from app.services.turn_service import ThreadScheduler

    scheduler = ThreadScheduler(
        bus=bus,
        session_factory=_FakeSessionFactory(session),
        graph=graph,
        spec_snapshot=_snapshot(_R14_SPEC),
        mapping_snapshot=_snapshot(_MAPPING_PAYLOAD),
        tools_registry=ToolRegistry(),
    )
    await scheduler._execute_turn(turn)

    config = graph.configs[0]
    assert config["configurable"]["cancel_flags"] is scheduler._cancel_flags  # 同一 dict 引用（进程级）
    assert config["configurable"]["turn_id"] == str(turn.id)


async def test_cancel_after_assembly_before_rewrite_stage_then_done_canceled() -> None:
    """注入点 1——rewrite 前（组装段完成后、首个图事件前即时取消）：
    [stage(rewrite,running), done(canceled,user)]，零 error、零图内事件。"""
    bus = _bus()
    turn = _TurnRow(status="running")
    session = _FakeSession(turn=turn)

    class _FlagAtStartGraph(_FakeGraph):
        """astream 启动即登记取消（组装段已完成——first_stage 已发布）。"""

        def __init__(self, flags: dict[str, str]) -> None:
            super().__init__([])
            self._flags = flags

        def astream(self, payload: Any, config: Any = None, stream_mode: Any = None,
                    subgraphs: bool = False) -> Any:
            self.payloads.append(payload)
            self.configs.append(config)

            async def gen():
                self._flags[str(payload["turn_id"])] = "user"  # 首个图事件前登记（scheduler 进程级 dict）
                yield (("fast:run",), "custom", {"type": "stage", "stage_key": "rewrite", "status": "done"})

            return gen()

    from app.services.turn_service import ThreadScheduler

    scheduler = ThreadScheduler(
        bus=bus,
        session_factory=_FakeSessionFactory(session),
        graph=_FakeGraph([]),  # 占位：下方构造真图后再覆写
        spec_snapshot=_snapshot(_R14_SPEC),
        mapping_snapshot=_snapshot(_MAPPING_PAYLOAD),
        tools_registry=ToolRegistry(),
    )
    graph = _FlagAtStartGraph(scheduler._cancel_flags)  # noqa: SLF001 —— 登记面=进程级 dict
    scheduler._graph = graph  # noqa: SLF001
    await scheduler._execute_turn(turn)

    published = await _drain_all(bus, str(turn.id))
    assert [e.event_type for e in published] == ["stage", "done"]
    assert published[0].data == {"stage_key": "rewrite", "status": "running"}
    assert published[1].data["finish"] == "canceled" and published[1].data["cancel_reason"] == "user"
    assert turn.status == "canceled" and turn.cancel_reason == "user" and turn.error is None


async def test_cancel_during_send_fanout_real_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """注入点 2——Send 扇出进行中取消（真主图全链路）：搜索副本执行中登记标志 →
    事件边界感知 → 子图正常退出、done(canceled,user)、无 sources/write/error。"""
    _mock_llm(monkeypatch)
    bus = _bus()
    turn = _TurnRow(status="running")
    session = _FakeSession(turn=turn)
    scheduler = _r14_scheduler(bus, session)
    turn_id = str(turn.id)
    registry = _R14FakeRegistry(
        on_search=lambda _call: scheduler._cancel_flags.__setitem__(turn_id, "user")  # noqa: SLF001
    )
    scheduler._tools_registry = registry  # noqa: SLF001 —— 注入带 hook 的假 registry

    await scheduler._execute_turn(turn)

    published = await _drain_all(bus, str(turn.id))
    types = [e.event_type for e in published]
    # 序无关断言（登记→感知的调度窗口内，登记后已排队帧可能被感知方丢弃——两种
    # 交错均合法）：first_stage+rewrite done(+search running) → done(canceled)；
    # 核心 = 取消被边界感知、无 sources/token/error、synthesis 未跑、终态 canceled
    assert types[0] == "stage" and types[-1] == "done"
    assert set(types) <= {"stage", "done"}  # 扇出中取消：无 sources/token
    done = published[-1]
    assert done.data["finish"] == "canceled" and done.data["cancel_reason"] == "user"
    assert turn.status == "canceled" and turn.cancel_reason == "user"
    assert turn.error is None and turn.answer_md is None  # 已产出未含答案（synthesis 未达）
    assert not [e for e in published if e.event_type in ("sources", "error", "token")]
    assert registry.search_calls == 1  # 扇出副本已执行（取消发生在副本内部/之后边界）


async def test_cancel_during_synthesis_streaming_partial_tokens_real_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注入点 3——synthesis 流式中取消（真主图全链路）：首个 token 已发布（只发布
    不落库），后续边界感知 → done(canceled,user)；无 write done、无 error。"""
    bus = _bus()
    turn = _TurnRow(status="running")
    session = _FakeSession(turn=turn)
    scheduler = _r14_scheduler(bus, session)
    turn_id = str(turn.id)
    calls: list[int] = []

    async def on_chunk() -> None:
        calls.append(1)
        if len(calls) == 2:  # 第二片前：先让消费侧排空先前帧（write running + token1 已发布）
            await asyncio.sleep(0.05)
            scheduler._cancel_flags[turn_id] = "user"  # noqa: SLF001

    _mock_llm(monkeypatch, synth_on_chunk=on_chunk)
    registry = _R14FakeRegistry()
    scheduler._tools_registry = registry  # noqa: SLF001

    await scheduler._execute_turn(turn)

    published = await _drain_all(bus, str(turn.id))
    types = [e.event_type for e in published]
    # 确定性交错（登记前 sleep(0.05) 排空）：first + rewrite done + search r/d + sources
    # + write running + token×1（首片）→ done(canceled,user)；登记后的 token 帧被感知丢弃
    assert types == [
        "stage", "stage", "stage", "stage", "sources", "stage", "token", "done",
    ]
    assert published[6].data["delta"] == "综合回答正文[1]。"  # 首片增量已到 SSE（只发布不落库）
    done = published[-1]
    assert done.data["finish"] == "canceled" and done.data["cancel_reason"] == "user"
    assert turn.status == "canceled" and turn.cancel_reason == "user"
    assert turn.answer_md is None  # 流式未完成的答案不入库（token 只发布不落库）
    assert not [e for e in published if e.event_type == "error"]
    events = [obj for obj in session.added if type(obj).__name__ == "TurnEvent"]
    # token 只发布不落库（ADR-003 第 4 条）：落库序列含 write running、不含 token
    assert [e.event_type for e in events] == [
        "stage", "stage", "stage", "stage", "sources", "stage", "done",
    ]


async def test_configurable_cancel_flags_cross_subgraph_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注入面跨子图身份实测：节点代码内收到的 configurable.cancel_flags 与
    ThreadScheduler._cancel_flags 为同一 dict 对象（进程级引用、主图壳→子图零拷贝）。"""
    bus = _bus()
    turn = _TurnRow(status="running")
    session = _FakeSession(turn=turn)
    scheduler = _r14_scheduler(bus, session)
    captured: list[Any] = []
    _mock_llm(monkeypatch, rewrite_captured_configs=captured)
    registry = _R14FakeRegistry()
    scheduler._tools_registry = registry  # noqa: SLF001

    await scheduler._execute_turn(turn)

    assert turn.status == "completed"
    assert captured, "rewrite 节点未收到 config"
    node_config = captured[0]["configurable"]
    assert node_config["cancel_flags"] is scheduler._cancel_flags  # noqa: SLF001 —— 同一 dict 引用
    assert node_config["turn_id"] == str(turn.id)
