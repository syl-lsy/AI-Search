"""unit 层：首 stage 图前发布（ARCH §4.3「首个 stage 事件 ≤1s：TurnRunner 在图启动前
同步发布 stage(rewrite, running)，不等 LLM」）。

钉住的语义：
- TurnRunner 在 astream 调用**前**同步发布 stage(rewrite, running)——图启动慢/图内
  首帧慢不影响首事件时延（≤1s 的结构性保证）；
- 图内 rewrite 节点不再发布 running（**无重复 running**——seq 只进一次）；
- 发布后仍走 cancel 检查序（启动前已取消的轮零业务事件——发布不得早于取消检查）。
"""

import asyncio
from typing import Any

import pytest

from app.services import turn_service
from app.services.turn_service import ThreadScheduler


class _SlowFirstFrameGraph:
    """首帧前长阻塞假图：图前发布正确时，阻塞期内事件面已有首个 stage。"""

    def __init__(self) -> None:
        self.payloads: list[Any] = []
        self.configs: list[Any] = []

    def astream(self, payload: Any, config: Any = None, stream_mode: Any = None,
                subgraphs: bool = False) -> Any:
        self.payloads.append(payload)
        self.configs.append(config)

        async def gen():
            await asyncio.sleep(0.8)  # 图内首帧延迟（模拟 LLM 冷启动）
            yield (("fast:r1",), "custom", {"type": "sources", "batch": [], "total": 0})

        return gen()


class _BoomBeforeAnyFrameGraph:
    """astream 立即抛错假图：图前发布正确时，事件面仍有首个 stage（发布先于图执行）。"""

    def __init__(self) -> None:
        self.payloads: list[Any] = []

    def astream(self, payload: Any, config: Any = None, stream_mode: Any = None,
                subgraphs: bool = False) -> Any:
        self.payloads.append(payload)

        async def gen():
            raise RuntimeError("图启动即炸")

        return gen()


class _FakeSession:
    def __init__(self, turn: Any = None) -> None:
        self.turn = turn
        self.added: list[Any] = []
        self.commits = 0

    async def get(self, model: Any, key: Any, *, with_for_update: bool = False) -> Any:
        return self.turn

    async def execute(self, statement: Any) -> Any:
        class _R:
            def scalars(self):
                return self

            def all(self):
                return []

        return _R()

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class _FakeSessionFactory:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def __call__(self) -> _FakeSession:
        return self._session


def _turn_row() -> Any:
    import uuid
    from datetime import UTC, datetime

    class _Row:
        pass

    row = _Row()
    row.id = uuid.uuid4()
    row.thread_id = "0b1f3a2e-0000-4000-8000-000000000001"
    row.user_id = "00000000-0000-4000-8000-000000000001"
    row.status = "running"
    row.mode = "fast"
    row.query = "q"
    row.chat_model_key = "qwen"
    row.query_lang = None
    row.rewritten_queries = []
    row.created_at = datetime.now(UTC)
    row.started_at = None
    row.finished_at = None
    row.duration_ms = None
    row.answer_md = None
    row.error = None
    row.cancel_reason = None
    row.timing = {}
    row.usage = {}
    return row


def _bus():
    from app.core.config import SseSettings
    from app.services.event_bus import EventBus

    return EventBus(settings=SseSettings(event_ttl_minutes=30, buffer_max_events=20000))


def _scheduler(bus: Any, session: _FakeSession, graph: Any) -> ThreadScheduler:
    from app.agent.tools.registry import ToolRegistry
    from app.config_center.cache import AssetSnapshot
    from datetime import datetime, timezone

    spec = AssetSnapshot(data_id="ai_search.agent.spec.json", version=1, md5="m", updated_at="", payload={
        "turn": {"default_on_conflict": "reject"},
        "fast": {"turn_timeout_s": 60, "retry": {"max_attempts": 1, "initial_interval_s": 0.01, "backoff_factor": 2.0, "jitter": False}},
        "deep": {"turn_timeout_s": 300},
        "routing": {"default_mode": "fast", "modes": {"fast": "fast", "deep": "deep"}},
    })
    mapping = AssetSnapshot(data_id="ai_search.model.mapping.json", version=1, md5="m", updated_at="", payload={
        "chat": {"options": {"qwen": "chat-standard"}, "default": "qwen"},
    })
    return ThreadScheduler(
        bus=bus,
        session_factory=_FakeSessionFactory(session),
        graph=graph,
        spec_snapshot=spec,
        mapping_snapshot=mapping,
        tools_registry=ToolRegistry(),
    )


async def _drain_all(bus: Any, turn_id: str) -> list[Any]:
    gen = bus.subscribe(turn_id)
    events: list[Any] = []
    while True:
        try:
            events.append(await asyncio.wait_for(gen.__anext__(), timeout=0.3))
        except (StopAsyncIteration, TimeoutError):
            break
    await gen.aclose()
    return events


@pytest.fixture(autouse=True)
def _restore_globals():
    original = (turn_service.get_session_factory, turn_service.get_event_bus)
    yield
    turn_service.get_session_factory, turn_service.get_event_bus = original


async def test_first_stage_published_before_astream_even_when_graph_blocks() -> None:
    """图首帧阻塞 0.8s：首个 stage 已在图前发布（ARCH §4.3 ≤1s 结构性保证）。"""
    bus = _bus()
    turn = _turn_row()
    session = _FakeSession(turn=turn)
    scheduler = _scheduler(bus, session, _SlowFirstFrameGraph())

    await scheduler._execute_turn(turn)

    published = await _drain_all(bus, str(turn.id))
    assert published, "事件面不应为空"
    first = published[0]
    assert first.event_type == "stage"
    assert first.data["stage_key"] == "rewrite"
    assert first.data["status"] == "running"


async def test_first_stage_published_even_when_graph_crashes_on_start() -> None:
    """图启动即炸：首个 stage 已发布（证明发布先于 astream 执行）。"""
    bus = _bus()
    turn = _turn_row()
    session = _FakeSession(turn=turn)
    scheduler = _scheduler(bus, session, _BoomBeforeAnyFrameGraph())

    await scheduler._execute_turn(turn)

    published = await _drain_all(bus, str(turn.id))
    assert published[0].event_type == "stage"
    assert published[0].data["stage_key"] == "rewrite"
    assert published[0].data["status"] == "running"
    # 失败路径收尾：error 在 stage 之后
    assert published[1].event_type == "error"


async def test_rewrite_node_no_longer_emits_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """无重复 running 的源头保证：rewrite 节点不再发射 running（归 TurnRunner 图前发布）。"""
    from app.agent.nodes import fast_rewrite as rewrite_module

    class _Chain:
        async def ainvoke(self, prompt: Any, config: Any = None) -> dict[str, Any]:
            return {
                "queries": ["q"],
                "query_type": "factoid",
                "answerable_from_knowledge": False,
                "time_range": None,
                "memory_directive": None,
            }

    class _Model:
        def with_structured_output(self, schema: Any, *, method: str | None = None, **kw: Any) -> _Chain:
            return _Chain()

    monkeypatch.setattr(rewrite_module, "build_light_model", lambda mapping: _Model())
    emitted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        rewrite_module, "emit_stage", lambda key, status, **kw: emitted.append((key, status))
    )

    await rewrite_module.rewrite({"query": "q", "query_lang": "zh", "spec": {"fast": {}}}, {})

    assert ("rewrite", "running") not in emitted  # running 归 TurnRunner 图前发布
    assert ("rewrite", "done") in emitted
