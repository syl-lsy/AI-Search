"""unit 层共享假体（跨模块唯一事实源，勿在测试模块内重复定义）。

背景（B9 测试审计 #9）：`test_direct_round.py` / `test_r14_cross_subgraph.py` 曾以
`from test.unit.test_turn_service import _FakeSession, ...` 跨模块引用另一个测试模块的
私有 helper——测试模块间隐式耦合（改名/搬移即连锁红，且 helper 归属不清）。本批把这些
**被跨模块复用**的假体收敛到本文件，各测试模块统一从 `test.unit.turn_fakes` 导入；
纯本地使用（如 `_BoomGraph`/`_custom`/`_updates`/`_message`）仍留在原模块。断言语义零变更。

本模块不是测试模块（无 `test_` 前缀）——pytest 不收集；全部假体零网络。
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.agent.tools.registry import SearchChain, ToolRegistry
from app.config_center.cache import AssetSnapshot
from app.core.config import SseSettings
from app.services.event_bus import EventBus, SseEvent


def _bus() -> EventBus:
    """真 EventBus（内存实现零网络）：seq 盖章/缓冲/订阅/清扫走生产行为。"""
    return EventBus(settings=SseSettings(event_ttl_minutes=30, buffer_max_events=20000))


# ---- 假体（零网络；语句对象由被测实现构造后传入，假体只按调用形态返回预设行集）----


class _FakeResult:
    """scalars/execute 结果假体：scalars() 链式返回行集。"""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> "_FakeResult":
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeSession:
    """最小 AsyncSession 假体：get/scalars/execute/add/commit 全记录。"""

    def __init__(self, *, turn: Any = None, rows: list[Any] | None = None,
                 execute_rows: list[Any] | None = None) -> None:
        self.turn = turn
        self.rows = rows or []
        self.execute_rows = execute_rows or []
        self.added: list[Any] = []
        self.commits = 0
        self.executed: list[Any] = []  # execute 收到的语句（FOR UPDATE 守卫断言用）

    async def get(self, model: Any, key: Any, *, with_for_update: bool = False) -> Any:
        return self.turn

    async def scalars(self, statement: Any) -> _FakeResult:
        return _FakeResult(self.rows)

    async def execute(self, statement: Any) -> _FakeResult:
        self.executed.append(statement)
        return _FakeResult(self.execute_rows)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class _FakeSessionFactory:
    """短事务假工厂：每次 async with 返回同一假会话（跨事务累积写入记录）。"""

    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def __call__(self) -> _FakeSession:
        return self._session


class _TurnRow:
    """最小 turns 行假体：暴露 TurnRunner 读写触碰的字段。"""

    def __init__(self, *, status: str = "running", mode: str = "fast",
                 thread_id: str = "0b1f3a2e-0000-4000-8000-000000000001",
                 user_id: str = "00000000-0000-4000-8000-000000000001",
                 query: str = "桩问题", chat_model_key: str = "qwen") -> None:
        self.id = uuid4()
        self.thread_id = thread_id
        self.user_id = user_id
        self.status = status
        self.mode = mode
        self.query = query
        self.chat_model_key = chat_model_key
        self.query_lang: str | None = None  # TurnRunner 运行时写入（§2.5 事实源）
        self.rewritten_queries: list[Any] = []
        self.created_at = datetime.now(UTC)
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self.duration_ms: int | None = None
        self.answer_md: str | None = None
        self.error: dict[str, Any] | None = None
        self.cancel_reason: str | None = None
        self.timing: dict[str, Any] = {}
        self.usage: dict[str, Any] = {}


class _FakeGraph:
    """假主图：astream 按脚本产出 (namespace, stream_mode, chunk) 三元组；步间可插 delay。

    记录每次收到的初始 state payload（B3 第 0 步注入断言用）。
    VR-2 关闭追踪：生成器帧终结（aclose 注入 GeneratorExit / 耗尽 / 异常穿透）时
    置 closed=True；streams 持生成器引用阻断 GC 终结器——closed 断言与 GC 时机无关
    （现状无显式 aclose 时 closed 恒 False，即取消收口护栏的红点）。
    """

    def __init__(self, steps: list[tuple[str, Any]], *, delay: float = 0.0) -> None:
        self._steps = steps
        self._delay = delay
        self.payloads: list[Any] = []
        self.configs: list[Any] = []
        self.closed = False
        self.streams: list[Any] = []

    def astream(self, payload: Any, config: Any = None, stream_mode: Any = None,
                subgraphs: bool = False) -> Any:
        self.payloads.append(payload)
        self.configs.append(config)
        steps = self._steps
        delay = self._delay

        async def gen():
            try:
                for stream, chunk in steps:
                    if delay:
                        await asyncio.sleep(delay)
                    # updates 一律产顶层 ns（=()）——TurnRunner 仅吸收顶层 updates 最终 state；
                    # messages 帧 namespace 带 synthesis 节点段（R14③ 过滤判定面，§6.1）
                    if stream == "updates":
                        namespace = ()
                    elif stream == "messages":
                        namespace = ("fast:run", "synthesis")
                    else:
                        namespace = ("fast:run",)
                    yield (namespace, stream, chunk)
            finally:
                self.closed = True

        gen_stream = gen()
        self.streams.append(gen_stream)
        return gen_stream


class _MessageChunk:
    """最小 AIMessageChunk 假体：content + additional_kwargs.reasoning_content。"""

    def __init__(self, content: Any, *, reasoning: str | None = None) -> None:
        self.content = content
        self.additional_kwargs: dict[str, Any] = (
            {"reasoning_content": reasoning} if reasoning else {}
        )


# ---- 公共编排助手 ----

_SPEC_PAYLOAD: dict[str, Any] = {
    "turn": {"default_on_conflict": "reject"},
    "fast": {"turn_timeout_s": 5},
    "deep": {"turn_timeout_s": 8},
}

_MAPPING_PAYLOAD: dict[str, Any] = {
    "chat": {"options": {"qwen": "qwen3.5-4b-ollama", "glm": "glm-4.7"}, "default": "qwen"}
}

_SRC: dict[str, Any] = {
    "source_id": "11111111-1111-4111-8111-111111111111",
    "ref_no": 1,
    "source_type": "web",
    "title": "LangGraph 官方文档",
    "url": "https://langchain-ai.github.io/langgraph/",
    "snippet": "StateGraph 以节点与边编排。",
    "score": 0.91,
    "snapshot_available": False,
    "kb_doc_id": None,
}


def _snapshot(payload: dict[str, Any]) -> AssetSnapshot:
    return AssetSnapshot(
        data_id="ai_search.agent.spec.json",
        version=1,
        md5="m",
        updated_at="t",
        payload=payload,
    )


#: R14 用 spec 快照：带 routing（路由正常路径）+ fast 节（synthesis max_answer_chars）
_R14_SPEC: dict[str, Any] = {
    **_SPEC_PAYLOAD,
    "routing": {"default_mode": "fast", "modes": {"fast": "fast", "deep": "deep"}},
    "fast": {**_SPEC_PAYLOAD["fast"], "max_answer_chars": 3000},
}


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


async def _drain_all(bus: EventBus, turn_id: str) -> list[SseEvent]:
    """订阅通道取全部已缓冲事件（重放路径）；无事件时静默返回空表。"""
    gen = bus.subscribe(turn_id)
    events: list[SseEvent] = []
    while True:
        try:
            events.append(await asyncio.wait_for(gen.__anext__(), timeout=0.2))
        except (StopAsyncIteration, TimeoutError):
            break
    await gen.aclose()
    return events
