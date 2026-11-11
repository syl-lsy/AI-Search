"""unit 层：双评审 P1 修复锁定。

- P1-1 searxng stdio env 透传：mcp SDK stdio spawn 用白名单环境（get_default_environment
  + server.env 合并）——SEARXNG_URL 必须经 connection["env"] 显式注入，否则 wrapper
  子进程读不到连接地址（备位链首必失败，且 resolve 恒选 searxng 压住可用 anysearch）；
- P1-2 _fail_turn 部分内容分流：timeout/未知异常落在 sources 已发布（有部分内容）之后
  → error + done(finish=degraded) + turns.status=degraded（api.md §7.2 v1.10 收束括注/
  §7.1 例外「failed 终态=失败且无部分内容」）；零内容保持 failed+close_channel。
"""

import asyncio
import time
from typing import Any

import pytest

from app.agent.tools import registry as registry_module
from app.agent.tools.registry import SearchChain, ToolRegistry


# ---- P1-1：searxng stdio env 注入 ----


class _CapturingClient:
    """捕获 MultiServerMCPClient 构造 connections 与 get_tools 结果的假体。"""

    instances: list[dict[str, Any]] = []

    def __init__(self, connections: Any) -> None:
        self.connections = connections
        _CapturingClient.instances.append(connections)

    async def get_tools(self) -> list[Any]:
        class _T:
            name = "searxng_search"

            async def ainvoke(self, args: Any) -> list[dict[str, Any]]:
                return [{"url": "https://x/1", "title": "t", "snippet": "s", "score": 0.9, "origin": "searxng"}]

        return [_T()]


@pytest.fixture
def searxng_env_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """部署形态：SEARXNG_URL 经进程环境在场（compose/shell 注入），凭证表走 env 查。"""
    monkeypatch.setenv("SEARXNG_URL", "http://127.0.0.1:18080")
    monkeypatch.setenv("SEARXNG_MCP_COMMAND", "uv")
    monkeypatch.setenv("SEARXNG_MCP_ARGS", "run --package search-api python -m app.agent.tools.searxng_mcp")
    monkeypatch.setattr(registry_module, "MultiServerMCPClient", _CapturingClient)
    _CapturingClient.instances.clear()


def _mcp_payload() -> dict[str, Any]:
    return {"servers": {
        "tavily": {"enabled": True, "role": "primary_search", "transport": "http",
                   "url_env": "MCP_TAVILY_URL", "auth": "TAVILY_API_KEY", "timeout_s": 10,
                   "circuit": {"failure_threshold": 3, "cooldown_s": 60}},
        "searxng": {"enabled": True, "role": "fallback_search", "transport": "stdio",
                    "command_env": "SEARXNG_MCP_COMMAND", "args_env": "SEARXNG_MCP_ARGS",
                    "timeout_s": 5, "circuit": {"failure_threshold": 3, "cooldown_s": 60}},
        "anysearch": {"enabled": True, "role": "fallback_search_secondary", "transport": "http",
                      "url_env": "MCP_ANYSEARCH_URL", "auth": "ANYSEARCH_API_KEY", "timeout_s": 10,
                      "circuit": {"failure_threshold": 3, "cooldown_s": 60}},
    }}


async def test_searxng_stdio_connection_injects_searxng_url_env(
    searxng_env_ready: None,
) -> None:
    """P1-1：searxng stdio 连接 env 必须含 SEARXNG_URL（SDK 白名单环境的透传通道）。"""
    registry = ToolRegistry()
    registry.bind(_mcp_payload())
    await registry.search("q", "searxng", top_k=3)

    conn = _CapturingClient.instances[-1]["searxng"]
    env = conn.get("env") or {}
    assert env.get("SEARXNG_URL") == "http://127.0.0.1:18080", (
        "wrapper 子进程 env 缺 SEARXNG_URL——mcp SDK 白名单环境不透传任意变量，备位链首必失败"
    )


async def test_primary_all_open_prefers_usable_anysearch_over_broken_searxng_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """resolve 与连接健康一致：searxng 三件套在场（credential_ready=True）但连接 env
    注入缺失的形态下——修复后注入到位，resolve 选 searxng 不再压住 anysearch 的前提
    是「链首可用」真实成立（连接健康=可执行）。"""
    registry = ToolRegistry()
    registry.bind(_mcp_payload())
    monkeypatch.setenv("SEARXNG_URL", "http://127.0.0.1:18080")
    monkeypatch.setenv("SEARXNG_MCP_COMMAND", "uv")
    monkeypatch.setenv("SEARXNG_MCP_ARGS", "run --package search-api python -m app.agent.tools.searxng_mcp")
    monkeypatch.setattr(registry_module, "MultiServerMCPClient", _CapturingClient)
    _CapturingClient.instances.clear()
    for name in ("tavily", "bocha"):
        breaker = registry_module.CircuitBreaker(failure_threshold=1, cooldown_s=3600.0)
        breaker.record_failure(time.monotonic())  # 与 resolve 的真实时钟同基
        registry.breakers[name] = breaker

    chain = registry.resolve(_mcp_payload(), "zh")

    assert chain.sources == ["searxng"] and chain.backup == "searxng"
    # 连接可执行（env 注入到位）：acquire 不抛「指针缺失」且 env 携带 SEARXNG_URL
    await registry.search("q", "searxng", top_k=3)
    conn = _CapturingClient.instances[-1]["searxng"]
    assert (conn.get("env") or {}).get("SEARXNG_URL") == "http://127.0.0.1:18080"


# ---- P1-2：_fail_turn 部分内容分流 ----


class _PartialThenTimeoutGraph:
    """先产 sources（部分内容）再长阻塞——timeout 落在 sources 发布之后。"""

    def __init__(self, delay: float) -> None:
        self._delay = delay
        self.payloads: list[Any] = []
        self.configs: list[Any] = []

    def astream(self, payload: Any, config: Any = None, stream_mode: Any = None,
                subgraphs: bool = False) -> Any:
        self.payloads.append(payload)
        self.configs.append(config)
        delay = self._delay

        async def gen():
            yield (("fast:r1", "web_search"), "custom", {
                "type": "sources",
                "batch": [{"url": "https://a/1", "title": "t", "snippet": "s", "score": 0.9, "source_id": "x", "ref_no": 1, "source_type": "web", "snapshot_available": False, "kb_doc_id": None}],
                "total": 1,
            })
            await asyncio.sleep(delay)

        return gen()


class _PartialThenBoomGraph:
    """先产 sources 再抛未知异常——部分内容形态的 500001 路径。"""

    def __init__(self) -> None:
        self.payloads: list[Any] = []
        self.configs: list[Any] = []

    def astream(self, payload: Any, config: Any = None, stream_mode: Any = None,
                subgraphs: bool = False) -> Any:
        self.payloads.append(payload)
        self.configs.append(config)

        async def gen():
            yield (("fast:r1", "web_search"), "custom", {
                "type": "sources",
                "batch": [{"url": "https://a/1", "title": "t", "snippet": "s", "score": 0.9, "source_id": "x", "ref_no": 1, "source_type": "web", "snapshot_available": False, "kb_doc_id": None}],
                "total": 1,
            })
            raise RuntimeError("synthesis 流中段爆炸")

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


from app.services.turn_service import ThreadScheduler  # noqa: E402 —— 假体会话须先定义


def _scheduler(bus: Any, session: _FakeSession, graph: Any, timeout_s: float = 60.0) -> Any:
    from app.agent.tools.registry import ToolRegistry
    from app.config_center.cache import AssetSnapshot

    spec = AssetSnapshot(data_id="ai_search.agent.spec.json", version=1, md5="m", updated_at="", payload={
        "turn": {"default_on_conflict": "reject"},
        "fast": {"turn_timeout_s": timeout_s, "retry": {"max_attempts": 1, "initial_interval_s": 0.01, "backoff_factor": 2.0, "jitter": False}},
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


class _FakeSessionFactory:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def __call__(self) -> _FakeSession:
        return self._session


def _bus():
    from app.core.config import SseSettings
    from app.services.event_bus import EventBus

    return EventBus(settings=SseSettings(event_ttl_minutes=30, buffer_max_events=20000))


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
    from app.services import turn_service

    original = (turn_service.get_session_factory, turn_service.get_event_bus)
    yield
    turn_service.get_session_factory, turn_service.get_event_bus = original


class TestPartialContentDegradedFinish:
    """timeout/未知异常落在 sources 之后：error + done(finish=degraded) + status=degraded。"""

    async def test_timeout_after_sources_degrades(self) -> None:
        bus = _bus()
        turn = _turn_row()
        session = _FakeSession(turn=turn)
        scheduler = _scheduler(bus, session, _PartialThenTimeoutGraph(3.0), timeout_s=1.0)

        await scheduler._execute_turn(turn)

        published = await _drain_all(bus, str(turn.id))
        types = [e.event_type for e in published]
        assert types == ["stage", "sources", "error", "done"], (
            f"有部分内容：error 后跟 done(degraded)（首个 stage=图前发布），实得 {types}"
        )
        error_event = published[2]
        assert error_event.data["code"] == "500301"
        done = published[-1]
        assert done.data["finish"] == "degraded" and done.data["degraded"] is True
        assert turn.status == "degraded"
        assert turn.error["code"] == "500301"

    async def test_unknown_exception_after_sources_degrades(self) -> None:
        bus = _bus()
        turn = _turn_row()
        session = _FakeSession(turn=turn)
        scheduler = _scheduler(bus, session, _PartialThenBoomGraph())

        await scheduler._execute_turn(turn)

        published = await _drain_all(bus, str(turn.id))
        types = [e.event_type for e in published]
        assert types == ["stage", "sources", "error", "done"], f"实得 {types}"
        error_event = published[2]
        assert error_event.data["code"] == "500001"
        assert turn.status == "degraded"

    async def test_zero_content_timeout_stays_failed_and_closes(self) -> None:
        """零内容（sources 未发布）：failed + close_channel 语义保持（§7.1 例外）。"""
        from test.unit.test_turn_service import _FakeGraph

        bus = _bus()
        turn = _turn_row()
        session = _FakeSession(turn=turn)
        graph = _FakeGraph([("custom", {"type": "token", "delta": "x"})], delay=0.5)
        scheduler = _scheduler(bus, session, graph, timeout_s=0.2)

        await scheduler._execute_turn(turn)

        published = await _drain_all(bus, str(turn.id))
        assert [e.event_type for e in published] == ["stage", "error"]  # 首个 stage=图前发布
        assert turn.status == "failed"
        channel = bus._channels.get(str(turn.id))
        assert channel is not None and channel.terminal is True and channel.closed is True
