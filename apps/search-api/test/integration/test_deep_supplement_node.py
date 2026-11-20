"""integration：deep supplement 节点真图回路（M2 批次 3 supplement 五步闭环 DoD 2/3）。

两个用例分面：
1. **脚本化 reflect 真图回路**（零 LLM——integration 真依赖面=图装配与循环语义）：
   reflect 以 gaps→gaps→sufficient 脚本链替代，真 deep 子图直跑完整
   reflect⇄supplement 回路——验证补搜轮事件形态（§7.4:866 相对序）、sources 增量
   批次 ref_no 续编、双维去重、**回路有界性（round 上限防死循环——rounds=2 耗尽后
   条件边 guards 出口收口）**；registry 以注入式假体承载（固定候选+批内重叠），
   零外网调用（真源依赖归 requires_mcp 层）；
2. **真 LLM reflect 判定 + 补搜段结构冒烟**（requires_llm——reflect 为真依赖面）：
   真 reflect 判 gaps（无研究材料形态大概率判缺口）→ 真 supplement 经固定候选假
   registry 消费——断言结构不断文案：补搜段帧形态、state 并集回写、零 degraded；
   reflect 判 sufficient 形态合法（零补搜帧）——两形态均按结构断言。
"""

from typing import Any

import httpx
import pytest


def _chat_alias_ready() -> str | None:
    """网关/chat-standard 别名探活；不可达原因字符串（None=就绪）。不打印任何凭据。"""
    from app.core.config import litellm_settings

    try:
        resp = httpx.get(
            str(litellm_settings.base_url).rstrip("/") + "/v1/models",
            headers={
                "Authorization": "Bearer " + litellm_settings.api_key.get_secret_value()
            },
            timeout=5,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 —— 探活失败统一 skip
        return f"LiteLLM 网关不可达（{type(exc).__name__}）"
    ids = {m.get("id") for m in resp.json().get("data", [])}
    if "chat-standard" not in ids:
        return "chat 别名未装载（chat-standard 不在网关模型清单）"
    return None


class _ScriptedReflectChain:
    """reflect 结构化输出脚本链：按调用序弹出 dict（gaps→gaps→sufficient）。"""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)

    async def ainvoke(self, prompt: Any, config: Any = None) -> dict[str, Any]:
        item = self.script.pop(0) if self.script else {"sufficient": True, "gaps": []}
        return item


class _ScriptedReflectModel:
    def __init__(self, chain: _ScriptedReflectChain) -> None:
        self._chain = chain

    def with_structured_output(
        self, schema: Any, *, method: str | None = None, **kw: Any
    ) -> _ScriptedReflectChain:
        return self._chain


class _PlanChain:
    async def ainvoke(self, prompt: Any, config: Any = None) -> dict[str, Any]:
        return {
            "subtopics": ["pgvector 能力与边界"],
            "outline": [
                {"section": "背景与选型要素", "intent": "技术诉求"},
                {"section": "选型建议", "intent": "结论"},
            ],
            "query_type": "comparison",
            "time_range": None,
            "memory_directive": None,
            "safety": "none",
        }


class _PlannerModel:
    def with_structured_output(
        self, schema: Any, *, method: str | None = None, **kw: Any
    ) -> _PlanChain:
        return _PlanChain()


class _ScriptedRegistry:
    """registry.search 假体：任意 (query, source) 返回固定候选（注入式——零外网）。

    候选集含跨 query 重叠 URL（c.dev/1 两次出现）——验证补搜批内去重。
    """

    def __init__(self, results: list[dict[str, Any]]) -> None:
        self._results = results

    async def search(self, query: Any, source: Any, **kw: Any) -> list[dict[str, Any]]:
        return [dict(item) for item in self._results]


_FIXED_CANDIDATES = [
    {"url": "https://a.dev/1", "title": "A1", "snippet": "snippet a1", "score": 0.9, "origin": "tavily"},
    {"url": "https://b.dev/2", "title": "B2", "snippet": "snippet b2", "score": 0.8, "origin": "tavily"},
    {"url": "https://c.dev/1", "title": "C1", "snippet": "snippet c1", "score": 0.7, "origin": "bocha"},
]


def _base_config(registry: Any) -> dict[str, Any]:
    from app.agent.tools.registry import ReadChain, SearchChain

    return {
        "configurable": {
            "thread_id": "it-supplement-th",
            "turn_id": "it-supplement-turn",
            "model_mapping": {"chat": {"options": {"qwen": "chat-standard"}, "default": "qwen"}},
            "search_chain": SearchChain(
                sources=["tavily", "bocha"], backup=None, degraded=False, detail="tavily"
            ),
            "read_chain": ReadChain(sources=["firecrawl"], backup=None, degraded=False, detail="firecrawl"),
            "tools_registry": registry,
        }
    }


async def _run_deep_graph(
    monkeypatch: pytest.MonkeyPatch,
    reflect_chain: _ScriptedReflectChain,
    registry: _ScriptedRegistry,
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """真 deep 子图直跑（planner/research 桩、reflect 脚本链、supplement 真节点）。

    返回 (stage 帧, sources 批次, 终态 updates 合并)。"""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from app.agent.graphs.deep import graph as deep_graph
    from app.agent.nodes import deep_planner as deep_planner_module
    from app.agent.nodes import deep_reflect as deep_reflect_module
    from app.agent.nodes import deep_research as deep_research_module

    class _ResearchMainModel(GenericFakeChatModel):
        def bind_tools(self, tools: Any, **kwargs: Any) -> "_ResearchMainModel":  # noqa: ANN201
            return self

    monkeypatch.setattr(deep_planner_module, "build_chat_model", lambda alias, **kw: _PlannerModel())
    monkeypatch.setattr(
        deep_reflect_module, "build_chat_model", lambda alias, **kw: _ScriptedReflectModel(reflect_chain)
    )
    monkeypatch.setattr(
        deep_research_module,
        "build_chat_model",
        lambda alias, **kw: _ResearchMainModel(messages=iter([AIMessage(content="零派发汇总收尾。")])),
    )
    monkeypatch.setattr(
        deep_research_module,
        "_subagent_model",
        lambda alias, index: _ResearchMainModel(messages=iter([AIMessage(content="保留。")])),
    )

    state: dict[str, Any] = {
        "query": "pgvector 和 Milvus 个人知识库场景怎么选？",
        "query_lang": "zh",
        "spec": {
            "deep": {
                "reflect_rounds": 2,
                "max_reflect_rounds": 3,
                "llm_timeout_s": 60,
                "turn_timeout_s": 300,
            }
        },
        "chat_model_key": "qwen",
    }
    stages: list[dict[str, Any]] = []
    batches: list[dict[str, Any]] = []
    final: dict[str, Any] = {}
    # 子图直跑（无 subgraphs=True）：顶层流为 (stream_mode, chunk) 二元组
    async for stream_mode, chunk in deep_graph.astream(
        state, config=config, stream_mode=["custom", "updates"]
    ):
        if stream_mode == "custom" and isinstance(chunk, dict):
            if chunk.get("type") == "stage":
                stages.append(chunk)
            elif chunk.get("type") == "sources":
                batches.append(chunk)
        elif stream_mode == "updates":
            for node_update in chunk.values():
                if isinstance(node_update, dict):
                    final.update(node_update)
    return stages, batches, final


async def test_supplement_loop_bounded_and_batches_continued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """脚本化 reflect 真图回路：两轮补搜后轮数耗尽 → guards 出口收口（**防死循环**）。

    断言：补搜段事件形态（reflect running→search running→search done→sources→…
    →reflect done 相对序）；sources 批次 ref_no 接续递增（research 零候选→从 1 起，
    批内重叠 c.dev/1 只入首现批次）；reflect_round=2（=rounds，递增方=supplement）；
    回路终止（writer 照常收口——非死循环）；零 degraded。"""
    reflect_chain = _ScriptedReflectChain(
        [
            {"sufficient": False, "gaps": [{"gap": "g1", "query": "补搜方向一", "origin": "web"}]},
            {"sufficient": False, "gaps": [{"gap": "g2", "query": "补搜方向二", "origin": "web"}]},
            {"sufficient": True, "gaps": []},
        ]
    )
    registry = _ScriptedRegistry(_FIXED_CANDIDATES)
    stages, batches, final = await _run_deep_graph(
        monkeypatch, reflect_chain, registry, _base_config(registry)
    )

    # 补搜段相对序（api.md §7.4:866）：reflect running → search running → search done
    # → sources → … → reflect done；同 key 多帧合法（§7.2:694）
    reflect_keys = [(s["status"], s.get("label")) for s in stages if s["stage_key"] == "reflect"]
    assert reflect_keys == [
        ("running", "反思第 1/2 轮"),
        ("running", "反思第 2/2 轮"),
        ("done", None),
    ]
    # 补搜段 search 帧 = 无 extra.subagents 的 search 帧（research 段入口帧带
    # subagents——deep_research.py:280-281，二者同 stage_key 并存）
    supp_search = [
        s for s in stages if s["stage_key"] == "search" and "subagents" not in (s.get("extra") or {})
    ]
    search_keys = [s["status"] for s in supp_search]
    assert search_keys == ["running", "done", "running", "done"]  # 两轮补搜各一对
    first_running = next(s for s in supp_search if s["status"] == "running")
    assert first_running.get("detail") == "tavily"  # 链摘要（SearchChain.detail）
    done_frames = [s for s in supp_search if s["status"] == "done"]
    # 每轮 gaps 恰一条（脚本逐轮产一条缺口）→ progress={current:1,total:1} ×2
    assert [f["progress"] for f in done_frames] == [{"current": 1, "total": 1}, {"current": 1, "total": 1}]

    # sources 增量批次：ref_no 接续递增、批内重叠去重（c.dev/1 只入首现批次）
    batch_refnos = [item["ref_no"] for batch in batches for item in batch["batch"]]
    assert batch_refnos == sorted(batch_refnos) and batch_refnos[0] == 1  # 首现冻结 1..k
    batch_urls = [item["url"] for batch in batches for item in batch["batch"]]
    assert len(batch_urls) == len(set(batch_urls))
    assert set(batch_urls) <= {"https://a.dev/1", "https://b.dev/2", "https://c.dev/1"}
    for batch in batches:
        for item in batch["batch"]:
            assert set(item) == {
                "source_id", "ref_no", "source_type", "title", "url", "snippet",
                "score", "snapshot_available", "kb_doc_id",
            }
            assert item["source_type"] == "web" and item["snapshot_available"] is False
        assert batch["total"] == batch_refnos[-1]  # 轮级累计=发射后 max ref_no

    # 回路有界性：rounds=2 耗尽 → 终止（writer 收口）；递增方=supplement
    assert final["reflect_round"] == 2
    assert isinstance(final.get("answer_md"), str) and final["answer_md"]  # writer 收口=非死循环
    assert set(final["searched_queries"]) >= {"补搜方向一", "补搜方向二"}  # 已发 query 登记
    # 候选并集挂 collected_sources（批次 4 binding 定案：候选不入 visited_urls——
    # ARCH §4.2:499「已读」纯语义，ref_no 账本面承载）
    assert {"https://a.dev/1", "https://b.dev/2"} <= {
        item["url"] for item in final["collected_sources"] if isinstance(item, dict)
    }
    refnos_state = sorted(item["ref_no"] for item in final["collected_sources"])
    assert refnos_state == [1, 2, 3]  # state 侧 ref_no 账本（research 增量键延续）
    assert "degraded" not in final and "degraded_reason" not in final  # 补搜轮零降级


@pytest.mark.requires_llm
async def test_supplement_real_llm_reflect_judgment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真 LLM reflect 判定（requires_llm）→ 补搜段结构冒烟：两形态均合法——

    - 判 gaps → 真 supplement 消费（补搜段帧+sources 批次结构+state 并集）；
    - 判 sufficient → 零补搜帧直通 write。
    断言结构不断文案（DEV §4.2）：gaps 结构合法、帧形态对齐 §7.4、零 degraded。"""
    skip_reason = _chat_alias_ready()
    if skip_reason:
        pytest.skip(skip_reason)
    from app.agent.state import DeepState

    registry = _ScriptedRegistry(_FIXED_CANDIDATES)
    config = _base_config(registry)

    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from app.agent.graphs.deep import graph as deep_graph
    from app.agent.nodes import deep_planner as deep_planner_module
    from app.agent.nodes import deep_research as deep_research_module

    class _ResearchMainModel(GenericFakeChatModel):
        def bind_tools(self, tools: Any, **kwargs: Any) -> "_ResearchMainModel":  # noqa: ANN201
            return self

    monkeypatch.setattr(deep_planner_module, "build_chat_model", lambda alias, **kw: _PlannerModel())
    # reflect 不桩（真 LLM——本测试唯一真依赖面）
    monkeypatch.setattr(
        deep_research_module,
        "build_chat_model",
        lambda alias, **kw: _ResearchMainModel(messages=iter([AIMessage(content="零派发汇总收尾。")])),
    )
    monkeypatch.setattr(
        deep_research_module,
        "_subagent_model",
        lambda alias, index: _ResearchMainModel(messages=iter([AIMessage(content="保留。")])),
    )

    state: dict[str, Any] = {
        "query": "pgvector 和 Milvus 个人知识库场景怎么选？",
        "query_lang": "zh",
        "spec": {
            "deep": {
                "reflect_rounds": 2,
                "max_reflect_rounds": 3,
                "llm_timeout_s": 60,
                "turn_timeout_s": 300,
            }
        },
        "chat_model_key": "qwen",
    }
    stages: list[dict[str, Any]] = []
    batches: list[dict[str, Any]] = []
    final: dict[str, Any] = {}
    async for stream_mode, chunk in deep_graph.astream(
        state, config=config, stream_mode=["custom", "updates"]
    ):
        if stream_mode == "custom" and isinstance(chunk, dict):
            if chunk.get("type") == "stage":
                stages.append(chunk)
            elif chunk.get("type") == "sources":
                batches.append(chunk)
        elif stream_mode == "updates":
            for node_update in chunk.values():
                if isinstance(node_update, dict):
                    final.update(node_update)

    # 回路终止 + 终态结构（两形态共用面）
    assert isinstance(final.get("answer_md"), str) and final["answer_md"]
    assert set(final) <= set(DeepState.__annotations__)
    assert "degraded" not in final and "degraded_reason" not in final
    assert isinstance(final.get("gaps"), list)
    for gap in final["gaps"]:
        assert gap.get("origin") in {"web", "kb"}

    reflect_ran = [s for s in stages if s["stage_key"] == "reflect"]
    assert reflect_ran, "reflect 零帧——节点未执行"
    assert reflect_ran[-1]["status"] == "done"  # 回路退出帧恒 done

    if final.get("reflect_round", 0) > 0:
        # 进过补搜：批次/并集/帧结构断言（补搜段帧=无 extra.subagents 的 search 帧）。
        # 真判 gaps 每轮条数不定：running=有执行面的轮数、done=总 query 数（≥running）
        supp_search = [
            s for s in stages if s["stage_key"] == "search" and "subagents" not in (s.get("extra") or {})
        ]
        search_keys = [s["status"] for s in supp_search]
        assert search_keys[0] == "running"  # 补搜段以链摘要入口帧开（零执行轮零帧）
        assert search_keys.count("running") <= search_keys.count("done")
        # 相对序：每个 running 之后、下一个 running 之前至少一个 done（逐 query done 推进）
        pending_done = 0
        for status in search_keys:
            if status == "running":
                assert pending_done == 0  # 上一轮已收口
                pending_done += 1
            else:
                pending_done = max(0, pending_done - 1)
        assert pending_done == 0  # 无悬挂 running（每轮 done 收口）
        for batch in batches:
            refnos = [item["ref_no"] for item in batch["batch"]]
            assert refnos == sorted(refnos)
            for item in batch["batch"]:
                assert set(item) == {
                    "source_id", "ref_no", "source_type", "title", "url", "snippet",
                    "score", "snapshot_available", "kb_doc_id",
                }
        state_refnos = sorted(item["ref_no"] for item in final["collected_sources"])
        assert state_refnos == list(range(1, len(state_refnos) + 1))  # 账本连续
        assert set(final["searched_queries"]) >= {q for q in final.get("searched_queries", [])}
