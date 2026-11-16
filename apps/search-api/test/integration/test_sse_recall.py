"""integration：recall 回顾整理轮 SSE 全链路（v1.51 / api.md §7.4 v1.22）。

口径（DEV §4 integration / api.md §7.2~§7.4）：
- **真库 + 真主图 + 真 EventBus**：fast 子图走真节点（rewrite → 条件边 → synthesis），
  仅 LLM 与搜索 registry 假体（零网络、确定性）；conftest 默认注入的脚本化假图在本文件
  被替换回真主图（`turn_service.main_graph` + 新建 scheduler）——条件边四目标与
  stage 序列是**被测对象本身**，不能用脚本图替代；
- **recall 轮**：stage 序列 = rewrite(running→done) → write(running→done)（无 search /
  sources / images）；`stage(rewrite,done).extra={"strategy":"recall"}`；done
  `source_count=0` / `finish=completed` / `degraded=false`；2.5 对账 `sources=[]`、
  `answer_md` 非空、stages 两段；
- **navigational 轮**：完整检索形态（rewrite → search → sources → write），改写 query
  钳制 1 条（fake rewrite 给 3 条 → clamp 表 navigational:[1,1]）；
- 断言结构不断言文案（答案措辞归 M4 评测线）。
"""

from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from app.agent.tools.registry import SearchChain, ToolRegistry
from test.integration.test_sse import parse_sse_frames

pytestmark = pytest.mark.requires_db

_RECALL_QUERY = "你之前聊了什么？"
_NAV_QUERY = "Vue 官网在哪？"


class _RewriteChain:
    """rewrite 结构化输出假体：按注入值返回判定（零网络）。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.prompts: list[Any] = []

    async def ainvoke(self, prompt: Any, config: Any = None) -> dict[str, Any]:
        self.prompts.append(prompt)
        return dict(self._payload)


class _StreamChatModel(BaseChatModel):
    """synthesis 流式假体（真 BaseChatModel）：逐片产出文本增量。"""

    pieces: list[str] = []
    prompts: list[Any] = []

    @property
    def _llm_type(self) -> str:
        return "recall-integration-fake-chat"

    async def _astream(self, messages: Any, stop: Any = None, **kwargs: Any) -> Any:
        self.prompts.append(messages)
        for text in self.pieces:
            yield ChatGenerationChunk(message=AIMessageChunk(content=text))

    async def _generate(self, messages: Any, stop: Any = None, **kwargs: Any) -> Any:
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="".join(self.pieces)))]
        )


class _RecordingRegistry(ToolRegistry):
    """registry 假体：tavily 恒返回两条候选；记录 search 调用次数（零检索判据）。"""

    def __init__(self) -> None:
        super().__init__()
        self.search_calls = 0

    def bind(self, payload: Any) -> None:  # TurnRunner 轮启动调用（快照刷新）
        return None

    def resolve(self, mcp_payload: Any, query_lang: str) -> SearchChain:
        return SearchChain(sources=["tavily"], backup=None, degraded=False, detail="tavily(假体)")

    def credential_ready(self, server: str) -> bool:
        return True

    async def search(
        self,
        query: str,
        source: str,
        *,
        time_range: str | None = None,
        top_k: int = 5,
        retry_sink: dict[str, int] | None = None,
    ) -> list[dict[str, Any]]:
        self.search_calls += 1
        return [
            {
                "url": f"https://stub.example/{self.search_calls}",
                "title": "桩来源",
                "snippet": "桩片段",
                "score": 0.9,
                "origin": source,
            }
        ]


def _install_real_graph(
    monkeypatch: pytest.MonkeyPatch,
    b2_runtime: Any,
    *,
    rewrite_payload: dict[str, Any],
    synth_pieces: list[str],
) -> tuple[_RecordingRegistry, _StreamChatModel]:
    """把 fast 子图换成真节点（LLM/registry 假体），并让端点解析到新 scheduler。"""
    from app.agent.graphs.main import graph as real_main_graph
    from app.agent.nodes import fast_rewrite as rewrite_module
    from app.agent.nodes import synthesis as synthesis_module
    from app.services import turn_service

    chain = _RewriteChain(rewrite_payload)
    monkeypatch.setattr(
        rewrite_module,
        "build_light_model",
        lambda mapping: type(
            "M",
            (),
            {"with_structured_output": lambda self, schema, *, method=None, **kw: chain},
        )(),
    )
    model = _StreamChatModel(pieces=synth_pieces, prompts=[])
    monkeypatch.setattr(synthesis_module, "build_chat_model", lambda alias: model)

    registry = _RecordingRegistry()
    scheduler = turn_service.ThreadScheduler(
        bus=b2_runtime.bus,
        graph=real_main_graph,
        tools_registry=registry,
    )
    monkeypatch.setattr(turn_service, "_thread_scheduler", scheduler)
    return registry, model


async def _run_turn(async_client: Any, b2_rest: Any, query: str) -> tuple[str, str]:
    resp = await async_client.post("/api/v1/threads", json={"query": query, "mode": "fast"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    await b2_rest.wait_turn_status(data["turn_id"], {"completed", "degraded", "failed"})
    return data["thread_id"], data["turn_id"]


@pytest.mark.asyncio
async def test_recall_round_sse_full_chain(
    async_client, b2_rest, b2_runtime, monkeypatch
) -> None:
    """recall 轮全链路：两段 stage、零 sources/images、done 对账、2.5 sources=[]。"""
    registry, model = _install_real_graph(
        monkeypatch,
        b2_runtime,
        rewrite_payload={
            "queries": ["回顾刚才聊了什么"],
            "query_type": "other",
            "answerable_from_knowledge": False,
            "search_strategy": "recall",
            "time_range": None,
            "memory_directive": None,
        },
        synth_pieces=["你之前聊了 LangGraph", "与 pgvector 的选型。"],
    )
    thread_id, turn_id = await _run_turn(async_client, b2_rest, _RECALL_QUERY)

    assert registry.search_calls == 0  # 零检索（条件边直达 synthesis，空历史轮亦成立）

    stream = await async_client.get(f"/api/v1/turns/{turn_id}/events")
    frames, _names = parse_sse_frames(stream.text)
    event_types = [frame["event"] for frame in frames]
    assert "sources" not in event_types
    assert "images" not in event_types

    stage_frames = [f for f in frames if f["event"] == "stage"]
    assert [(f["data"]["stage_key"], f["data"]["status"]) for f in stage_frames] == [
        ("rewrite", "running"),
        ("rewrite", "done"),
        ("write", "running"),
        ("write", "done"),
    ]
    assert stage_frames[1]["data"]["extra"] == {"strategy": "recall"}

    done = frames[-1]
    assert done["event"] == "done"
    assert done["data"]["finish"] == "completed"
    assert done["data"]["source_count"] == 0
    assert done["data"]["degraded"] is False

    detail = (await async_client.get(f"/api/v1/turns/{turn_id}")).json()["data"]
    assert detail["status"] == "completed"
    assert detail["sources"] == []
    assert detail["answer_md"]
    assert {stage["stage_key"] for stage in detail["stages"]} == {"rewrite", "write"}
    assert {stage["stage_key"]: stage.get("extra") for stage in detail["stages"]}[
        "rewrite"
    ] == {"strategy": "recall"}


@pytest.mark.asyncio
async def test_recall_round_second_turn_reads_history_channel(
    async_client, b2_rest, b2_runtime, monkeypatch
) -> None:
    """第二轮回溯：同线程先跑一轮 web 轮建历史，再跑 recall 轮——synthesis 提示词含历史。"""
    # 第一轮：web 轮（真节点 + 假 registry），答案落库成为历史
    _install_web = _install_real_graph(
        monkeypatch,
        b2_runtime,
        rewrite_payload={
            "queries": ["LangGraph StateGraph 架构"],
            "query_type": "factoid",
            "answerable_from_knowledge": False,
            "search_strategy": "web",
            "time_range": None,
            "memory_directive": None,
        },
        synth_pieces=["LangGraph 用 StateGraph 编排[1]。"],
    )
    thread_id, _first = await _run_turn(async_client, b2_rest, "LangGraph 架构是什么？")

    # 第二轮：recall 轮（同线程）
    registry, model = _install_real_graph(
        monkeypatch,
        b2_runtime,
        rewrite_payload={
            "queries": ["回顾刚才聊了什么"],
            "query_type": "other",
            "answerable_from_knowledge": False,
            "search_strategy": "recall",
            "time_range": None,
            "memory_directive": None,
        },
        synth_pieces=["你之前问了 LangGraph 架构。"],
    )
    resp = await async_client.post(
        f"/api/v1/threads/{thread_id}/turns", json={"query": _RECALL_QUERY, "mode": "fast"}
    )
    assert resp.status_code == 200, resp.text
    second_id = resp.json()["data"]["turn_id"]
    await b2_rest.wait_turn_status(second_id, {"completed", "degraded", "failed"})

    assert registry.search_calls == 0  # recall 轮零检索
    prompt_text = model.prompts[0][0].content
    assert "回顾整理路径" in prompt_text
    assert "LangGraph 架构是什么？" in prompt_text  # 上轮问题全文（历史通道）
    assert "LangGraph 用 StateGraph 编排" in prompt_text  # 上轮答案骨架

    detail = (await async_client.get(f"/api/v1/turns/{second_id}")).json()["data"]
    assert detail["status"] == "completed"
    assert detail["sources"] == []


@pytest.mark.asyncio
async def test_navigational_round_full_search_chain(
    async_client, b2_rest, b2_runtime, monkeypatch
) -> None:
    """navigational 轮：完整检索形态（含 search/sources）+ query 钳制 1 条。"""
    registry, _model = _install_real_graph(
        monkeypatch,
        b2_runtime,
        rewrite_payload={
            # LLM 给 3 条 → type_query_map navigational:[1,1] 钳制 1 条
            "queries": ["Vue 官网", "Vue3 官方文档", "Vue 下载页"],
            "query_type": "navigational",
            "answerable_from_knowledge": False,
            "search_strategy": "web",
            "time_range": None,
            "memory_directive": None,
        },
        synth_pieces=["Vue 官网见 [1]。"],
    )
    _thread_id, turn_id = await _run_turn(async_client, b2_rest, _NAV_QUERY)

    assert registry.search_calls == 1  # clamp 1 query（表驱动）

    stream = await async_client.get(f"/api/v1/turns/{turn_id}/events")
    frames, _names = parse_sse_frames(stream.text)
    stage_keys = [
        frame["data"]["stage_key"] for frame in frames if frame["event"] == "stage"
    ]
    assert stage_keys[0] == "rewrite"
    assert "search" in stage_keys
    assert "write" in stage_keys
    assert [frame for frame in frames if frame["event"] == "sources"]

    detail = (await async_client.get(f"/api/v1/turns/{turn_id}")).json()["data"]
    assert detail["status"] == "completed"
    assert len(detail["sources"]) == 1
    assert detail["rewritten_queries"] == ["Vue 官网"]
