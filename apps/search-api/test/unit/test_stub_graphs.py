"""unit 层：主图壳接真路由（B3 第 0 步接真 + 节点 1-3/5 接真后复验——ARCH §4.0/ADR-012）
+ fast/deep 子图事件序列 + 状态契约（R14①）。

钉住的语义（冻结契约，逐条对应测试）：
- 路由双层兜底（ARCH §4.0 / nacos-config.md §3 routing 节 v1.19 键域口径）：
  - 正常路径：读**轮内 spec.routing 快照**（state["spec"]，TurnRunner 发起时捕获注入，
    OQ-11 同源）∩ GRAPH_REGISTRY → mode → 图名（targets={mode: graphName}，mode 键与
    图名 value 分离，不假设同名）；
  - 层① routing 整节缺失/读取异常/校验失败 → 代码内置默认表 + AgentLog WARNING 一次；
  - 层② 节有效但 mode 行缺失或 modes[mode] 指向未注册图名 → 经 modes[default_mode]
    解析图名兜底 + WARNING 一次；
  - 路由函数无 LLM、无 IO、亚毫秒、零图内信号；兜底不重写 turns.mode（审计口径）；
- fast 事件序列（B3 节点 1-3/5 接真后）：rewrite（LLM 假体）→ search 扇出
  （running×1 + done×N 带 progress）→ sources(batch=归并候选) → write 桩 token 流；
  web_search 走假 registry（可冻结链经 configurable 注入，零网络）；
- deep 桩序列不变（B4 实装）；
- 状态契约（R14①）：MainState 投影薄壳 ⊇ FastState 输入字段（TurnRunner 注入面），
  产出键同名声明经顶层 updates 回填；candidates/errors 带 add、degraded 带或 reducer
  （Send 扇出并行副本正确合并——键集不变）。
"""

import operator
import time
import typing
from typing import Any

import pytest

from app.agent.graphs.deep import STUB_DEEP_ANSWER_MD, STUB_DEEP_TOKEN_DELTAS
from app.agent.graphs.main import DEFAULT_GRAPH, graph as main_graph, route_by_mode
from app.agent.graphs.registry import GRAPH_REGISTRY
from app.agent.nodes import fuse_rerank as fuse_module
from app.agent.nodes import merge_dedupe as merge_module
from app.agent.nodes import web_search as ws_module
from app.agent.state import DeepState, FastState, MainState
from app.agent.tools.registry import SearchChain, ToolRegistry


async def _run(
    payload: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """跑主图：收集 custom 信号序列 + 最终 state（updates 顶层合并）。

    调用面镜像 TurnRunner（ARCH §6.1）：astream + subgraphs=True，事件形状
    (namespace, mode, chunk)——custom 即图内信号，updates 顶层 (namespace=())
    按 node 名合并得最终 state 投影。
    """
    signals: list[dict[str, Any]] = []
    final: dict[str, Any] = {}
    async for namespace, stream_mode, chunk in main_graph.astream(
        payload, config=config, stream_mode=["custom", "updates"], subgraphs=True
    ):
        if stream_mode == "custom":
            signals.append(chunk)
        elif namespace == ():  # 顶层 updates：{node_name: state_delta}
            for node_update in chunk.values():
                final.update(node_update)
    return signals, final


#: 接真语义的标准 spec 快照（TurnRunner 注入形态；routing 初值恒等 fast→fast/deep→deep）
_ROUTING_SPEC: dict[str, Any] = {
    "routing": {"default_mode": "fast", "modes": {"fast": "fast", "deep": "deep"}}
}


@pytest.fixture(autouse=True)
def _mock_llm_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    """rewrite/synthesis 已接真（节点 1/5+5/5）：路由/序列测试不依赖真 LLM——fixture 假体
    直注（rewrite 结构化输出 + synthesis 流式 content，零网络）。"""
    from app.agent.nodes import fast_rewrite as rewrite_module
    from app.agent.nodes import synthesis as synthesis_module

    class _Chain:
        async def ainvoke(self, prompt: Any, config: Any = None) -> dict[str, Any]:
            return {
                "queries": ["langgraph 条件边检索"],
                "query_type": "factoid",
                "answerable_from_knowledge": False,
                "time_range": None,
                "memory_directive": None,
            }

    class _RewriteModel:
        def with_structured_output(
            self, schema: Any, *, method: str | None = None, **kw: Any
        ) -> _Chain:
            return _Chain()

    class _SynthModel:
        async def astream(self, messages: Any, config: Any = None, **kw: Any) -> Any:
            from langchain_core.messages import AIMessageChunk

            for piece in ("综合回答正文[1]。", "结论[2]。"):
                yield AIMessageChunk(content=piece)

    monkeypatch.setattr(rewrite_module, "build_light_model", lambda mapping: _RewriteModel())
    monkeypatch.setattr(synthesis_module, "build_chat_model", lambda alias: _SynthModel())


def _warnings(log_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in log_records if r["level"].name == "WARNING"]


# ---- 假搜索源（web_search 可冻结链经 configurable 注入，零网络）----


#: synthesis 假体答案（fixture astream 拼接；引用 [1][2] 对应假候选两条）
SYNTH_MOCK_ANSWER_MD = "综合回答正文[1]。结论[2]。"

#: 假 tavily 结果（确定性两候选；a/b 两 URL 供归并断言）
FAKE_TAVILY_RESULTS: list[dict[str, Any]] = [
    {"url": "https://docs.langgraph.dev/send", "title": "Send API", "snippet": "Send 扇出", "score": 0.92},
    {"url": "https://docs.langgraph.dev/state", "title": "State", "snippet": "状态归并", "score": 0.85},
]


class _FakeRegistry(ToolRegistry):
    """registry 假体：tavily 返回确定性候选；凭证全绿（凭证面由白名单测试单独锁）。"""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, str, str | None]] = []

    async def search(
        self, query: str, source: str, *, time_range: str | None = None, top_k: int = 5
    ) -> list[dict[str, Any]]:
        self.calls.append((query, source, time_range))
        if source == "tavily":
            return [
                {**item, "origin": "tavily"} for item in FAKE_TAVILY_RESULTS
            ]
        return []

    def credential_ready(self, server: str) -> bool:  # 单测环境零凭证依赖
        return True


def _search_config(registry: _FakeRegistry | None = None) -> dict[str, Any]:
    """TurnRunner 注入形态的 configurable（冻结链 [tavily] + 假 registry）。"""
    return {
        "configurable": {
            "search_chain": SearchChain(
                sources=["tavily"], backup=None, degraded=False, detail="tavily"
            ),
            "tools_registry": registry if registry is not None else _FakeRegistry(),
        }
    }


def _signals_masking_placeholder_ids(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """掩码 sources.batch[].source_id（uuid 占位每次生成，由 TurnRunner 回写行 id——契约断言其余键）。"""
    masked: list[dict[str, Any]] = []
    for signal in signals:
        if signal.get("type") == "sources":
            signal = {
                **signal,
                "batch": [{**item, "source_id": "<masked>"} for item in signal["batch"]],
            }
        masked.append(signal)
    return masked


def _fast_expected(registry: _FakeRegistry) -> list[dict[str, Any]]:
    """api.md §7.4 快速主干（B3 节点 1-3/5 接真后）：rewrite(LLM 假体) → search 扇出
    （running×1 + done×1 带 progress）→ sources(归并候选) → write 桩 token 流。"""
    batch = fuse_module._source_item  # noqa: SLF001 —— 测试同构复用占位回写前的条目构造
    merged = merge_module.merge_candidates(
        [{**item, "origin": "tavily"} for item in FAKE_TAVILY_RESULTS]
    )
    # token 不在 custom 流（§6.1 messages 域——route 测试只收 custom）
    return [
        {"type": "stage", "stage_key": "rewrite", "status": "done"},  # running 已由 TurnRunner 图前发布（§4.3）
        {"type": "stage", "stage_key": "search", "status": "running", "detail": "tavily"},
        {
            "type": "stage",
            "stage_key": "search",
            "status": "done",
            "progress": {"current": 1, "total": 1},
            "detail": "tavily×2",
        },
        {
            "type": "sources",
            "batch": [
                {**batch(candidate, ref_no), "source_id": "<masked>"}
                for ref_no, candidate in enumerate(merged, start=1)
            ],
            "total": len(merged),
        },
        {"type": "stage", "stage_key": "write", "status": "running", "label": "综合中"},
        {"type": "stage", "stage_key": "write", "status": "done"},
    ]


def test_registry_exposes_two_stub_graphs() -> None:
    """GRAPH_REGISTRY 注册 fast/deep 两张编译图（langgraph.json 三入口的子图键）。"""
    assert set(GRAPH_REGISTRY) == {"fast", "deep"}


#: FastState 输入字段（TurnRunner 注入面；ARCH §4.1）
_FAST_INPUT_KEYS = {
    "turn_id",
    "thread_id",
    "user_id",
    "mode",
    "query",
    "chat_model_key",
    "history",
    "images",
    "spec",
    "query_lang",
}
#: FastState 产出键（子图节点更新 → 主图同名回填，TurnRunner 经顶层 updates 消费）
_FAST_OUTPUT_KEYS = {
    "answer_md",
    "query_type",
    "answerable_from_knowledge",
    "time_range",
    "rewritten_queries",
    "candidates",
    "images_raw",
    "fused",
    "errors",
    "degraded",
}


def test_main_state_projects_all_fast_inputs() -> None:
    """R14①：主图投影 ⊇ FastState 输入字段——TurnRunner 注入键零丢失直达子图。"""
    assert _FAST_INPUT_KEYS <= set(MainState.__annotations__)
    assert _FAST_INPUT_KEYS <= set(FastState.__annotations__)


def test_main_state_backfills_all_fast_outputs() -> None:
    """R14①：FastState 产出键在主图壳同名声明——子图更新经顶层 updates 回填。"""
    assert _FAST_OUTPUT_KEYS <= set(MainState.__annotations__)
    assert _FAST_OUTPUT_KEYS <= set(FastState.__annotations__)


def test_fast_candidates_channel_uses_add_reducer() -> None:
    """candidates/errors 承接 Send 扇出并行副本：Annotated[list, add] reducer；
    degraded 用或 reducer（任一副本降级即整轮降级）。"""
    hints = typing.get_type_hints(FastState, include_extras=True)
    assert operator.add in getattr(hints["candidates"], "__metadata__", ())
    assert operator.add in getattr(hints["errors"], "__metadata__", ())
    assert operator.or_ in getattr(hints["degraded"], "__metadata__", ())


def test_deep_state_skeleton_inputs_isomorphic_with_fast() -> None:
    """DeepState 骨架（B4 填）输入字段与 FastState 同构（ARCH §4.2「输入字段同构」）。"""
    assert _FAST_INPUT_KEYS <= set(DeepState.__annotations__)


def test_compiled_graphs_expose_projected_channels() -> None:
    """编译产物投影通道：主图壳与 fast 子图均建有 FastState 全字段 channel（R14① 物理通道）。"""
    fast_keys = _FAST_INPUT_KEYS | _FAST_OUTPUT_KEYS
    assert fast_keys <= set(main_graph.channels)
    from app.agent.graphs.fast import graph as fast_compiled

    assert fast_keys <= set(fast_compiled.channels)


async def test_main_graph_accepts_full_fast_state_input() -> None:
    """运行期投影：TurnRunner 形态的全量输入注入主图——注入键保留 + 产出回填。"""
    payload: dict[str, Any] = {
        "mode": "fast",
        "query": "langgraph 状态投影",
        "spec": _ROUTING_SPEC,
        "turn_id": "t-1",
        "thread_id": "th-1",
        "user_id": "u-1",
        "chat_model_key": "qwen",
        "history": [{"q": "上一问", "a_head": "上一答"}],
        "query_lang": "zh",
        "images": [],
    }
    final_state = await main_graph.ainvoke(payload, config=_search_config())

    assert final_state["turn_id"] == "t-1"
    assert final_state["thread_id"] == "th-1"
    assert final_state["query_lang"] == "zh"
    assert final_state["history"] == [{"q": "上一问", "a_head": "上一答"}]
    assert final_state["images"] == []
    assert final_state["spec"] == _ROUTING_SPEC
    assert final_state["answer_md"] == SYNTH_MOCK_ANSWER_MD


# ---- 路由接真（双层兜底，ARCH §4.0 / nacos-config §3 routing 节）----


async def test_route_fast_to_fast_subgraph(log_records: list[dict[str, Any]]) -> None:
    """路由一：轮内 spec.routing 快照 mode=fast → fast 子图；序列对齐 §7.4；正常路径零告警。"""
    registry = _FakeRegistry()
    signals, final = await _run(
        {"mode": "fast", "query": "langgraph 条件边怎么写", "spec": _ROUTING_SPEC},
        config=_search_config(registry),
    )

    assert final["answer_md"] == SYNTH_MOCK_ANSWER_MD
    assert [c["url"] for c in final["fused"]] == [item["url"] for item in FAKE_TAVILY_RESULTS]
    assert _signals_masking_placeholder_ids(signals) == _fast_expected(registry)
    assert _warnings(log_records) == []


async def test_route_deep_to_deep_subgraph(log_records: list[dict[str, Any]]) -> None:
    """路由二：轮内 spec.routing 快照 mode=deep → deep 桩子图；序列对齐 §7.4；零告警。"""
    signals, final = await _run({"mode": "deep", "query": "多框架对比研究报告", "spec": _ROUTING_SPEC})

    assert final["answer_md"] == STUB_DEEP_ANSWER_MD
    assert signals == _deep_expected()
    assert all(signal["type"] != "sources" for signal in signals)
    assert _warnings(log_records) == []


async def test_route_missing_mode_falls_back_to_default(log_records: list[dict[str, Any]]) -> None:
    """路由三：mode 行缺失 → 层②经 modes[default_mode] 解析图名兜底（fast）+ WARNING 一次。"""
    signals, final = await _run(
        {"query": "没有 mode 字段的输入", "spec": _ROUTING_SPEC}, config=_search_config()
    )

    assert final["answer_md"] == SYNTH_MOCK_ANSWER_MD
    warnings = _warnings(log_records)
    assert len(warnings) == 1
    assert warnings[0]["extra"]["graph"] == "main"


async def test_route_unregistered_mode_falls_back_with_warning(
    log_records: list[dict[str, Any]],
) -> None:
    """路由四：mode 指向未注册图（routing 无该行）→ 层②回落 default + WARNING（断言与 B2 同形）。"""
    signals, final = await _run(
        {"mode": "hyper", "query": "未注册模式", "spec": _ROUTING_SPEC}, config=_search_config()
    )

    assert final["answer_md"] == SYNTH_MOCK_ANSWER_MD
    warnings = _warnings(log_records)
    assert len(warnings) == 1
    assert "hyper" in warnings[0]["message"]
    assert DEFAULT_GRAPH in warnings[0]["message"]
    assert warnings[0]["extra"]["graph"] == "main"
    assert warnings[0]["extra"]["mode"] == "hyper"


async def test_routing_section_missing_uses_builtin_table_with_warning(
    log_records: list[dict[str, Any]],
) -> None:
    """层①：spec.routing 整节缺失 → 代码内置默认表 + WARNING 一次；内置表
    modes={fast:fast, deep:deep}——mode 按表命中各自子图（default_mode=fast 仅兜未命中行）。"""
    cases = [
        ("fast", SYNTH_MOCK_ANSWER_MD),
        ("deep", STUB_DEEP_ANSWER_MD),
    ]
    for mode, answer_md in cases:
        log_records.clear()
        registry = _FakeRegistry()
        signals, final = await _run(
            {"mode": mode, "query": "x"},
            config=_search_config(registry) if mode == "fast" else None,
        )

        assert final["answer_md"] == answer_md
        warnings = _warnings(log_records)
        assert len(warnings) == 1, f"mode={mode} 应恰有一条层①告警"
        assert "内置默认" in warnings[0]["message"]
        assert warnings[0]["extra"]["graph"] == "main"
        assert warnings[0]["extra"]["mode"] == mode


async def test_routing_validation_failure_uses_builtin_table_with_warning(
    log_records: list[dict[str, Any]],
) -> None:
    """层①之「校验失败」（nacos-config §3 v1.19 键域口径）：default_mode 不在 modes 键域
    → 内置默认表 + WARNING 一次（路由面防御性复验，与 validator 拒收独立成防线）。"""
    bad_spec = {"routing": {"default_mode": "ultra", "modes": {"fast": "fast", "deep": "deep"}}}
    signals, final = await _run(
        {"mode": "fast", "query": "x", "spec": bad_spec}, config=_search_config()
    )

    assert final["answer_md"] == SYNTH_MOCK_ANSWER_MD
    warnings = _warnings(log_records)
    assert len(warnings) == 1
    assert "内置默认" in warnings[0]["message"]
    assert warnings[0]["extra"]["mode"] == "fast"


async def test_default_mode_resolved_via_modes(log_records: list[dict[str, Any]]) -> None:
    """default_mode 经 modes 解析为图名（v1.19 键域口径）：default_mode=deep 且 mode 缺失 → deep 兜底。"""
    spec = {"routing": {"default_mode": "deep", "modes": {"fast": "fast", "deep": "deep"}}}
    signals, final = await _run({"query": "x", "spec": spec})

    assert final["answer_md"] == STUB_DEEP_ANSWER_MD
    warnings = _warnings(log_records)
    assert len(warnings) == 1
    assert "deep" in warnings[0]["message"]
    assert warnings[0]["extra"]["graph"] == "main"
    assert "mode" not in warnings[0]["extra"]  # mode 缺失（假值过滤约定）


async def test_mode_targeting_unregistered_graph_falls_back_with_warning(
    log_records: list[dict[str, Any]],
) -> None:
    """层②之「modes[mode] 指向未注册图名」：deep→ghost 未注册 → 经 modes[default_mode=fast]
    解析图名兜底（fast）+ WARNING 一次（∩ registry 交集语义，deep 行未注册时同此形）。"""
    spec = {"routing": {"default_mode": "fast", "modes": {"fast": "fast", "deep": "ghost"}}}
    signals, final = await _run(
        {"mode": "deep", "query": "x", "spec": spec}, config=_search_config()
    )

    assert final["answer_md"] == SYNTH_MOCK_ANSWER_MD
    warnings = _warnings(log_records)
    assert len(warnings) == 1
    assert "deep" in warnings[0]["message"] and "ghost" in warnings[0]["message"]
    assert DEFAULT_GRAPH in warnings[0]["message"]
    assert warnings[0]["extra"]["graph"] == "main"
    assert warnings[0]["extra"]["mode"] == "deep"


def test_route_maps_mode_to_non_identical_registered_graph() -> None:
    """targets 建模：mode 键与图名 value 分离——spec.routing 把 mode 映射到非同名已注册图
    时按映射走（不假设 mode==图名）。初值恒等仅是巧合；改映射图名属 ADR-012 扩展四步
    同批场景，此处锁建模语义。"""
    spec = {"routing": {"default_mode": "fast", "modes": {"fast": "fast", "deep": "fast"}}}
    assert route_by_mode({"mode": "deep", "spec": spec}) == "fast"
    assert route_by_mode({"mode": "fast", "spec": spec}) == "fast"


def test_route_by_mode_is_submillisecond_pure() -> None:
    """路由函数无 LLM、无 IO、亚毫秒（ARCH §4.0）：万次调用 < 0.5s（纯查表量级）。"""
    state: dict[str, Any] = {"mode": "fast", "spec": _ROUTING_SPEC}
    started = time.perf_counter()
    for _ in range(10_000):
        result = route_by_mode(state)
    elapsed = time.perf_counter() - started

    assert result == "fast"
    assert elapsed < 0.5


# ---- fast 子图接真面（web_search 调用面与 sources 契约）----


async def test_web_search_receives_time_range_passthrough_in_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """time_range 透传（§4.1）：rewrite 假体产出 time_range 经扇出副本到 tavily 调用。"""
    from app.agent.nodes import fast_rewrite as rewrite_module

    class _Chain:
        async def ainvoke(self, prompt: Any, config: Any = None) -> dict[str, Any]:
            return {
                "queries": ["时效检索"],
                "query_type": "temporal",
                "answerable_from_knowledge": False,
                "time_range": "month",
                "memory_directive": None,
            }

    class _Model:
        def with_structured_output(self, schema: Any, *, method: str | None = None, **kw: Any) -> _Chain:
            return _Chain()

    registry = _FakeRegistry()
    monkeypatch.setattr(rewrite_module, "build_light_model", lambda mapping: _Model())
    await _run(
        {"mode": "fast", "query": "x", "spec": _ROUTING_SPEC},
        config=_search_config(registry),
    )
    assert ("时效检索", "tavily", "month") in registry.calls


async def test_sources_payload_matches_api_contract() -> None:
    """sources 信号 batch 字段对齐 api.md §7.2：ref_no 冻结 1..k、source_type=web、
    snapshot_available=false、source_id 占位（TurnRunner 以行 id 回写）。"""
    signals, _ = await _run(
        {"mode": "fast", "query": "x", "spec": _ROUTING_SPEC}, config=_search_config()
    )
    sources_signal = next(s for s in signals if s["type"] == "sources")

    assert sources_signal["total"] == len(FAKE_TAVILY_RESULTS)
    assert [item["ref_no"] for item in sources_signal["batch"]] == [1, 2]  # 首次出现即冻结
    for item in sources_signal["batch"]:
        assert item["source_type"] == "web"
        assert item["snapshot_available"] is False and item["kb_doc_id"] is None
        assert isinstance(item["source_id"], str) and item["source_id"]
        assert set(item) == {
            "source_id",
            "ref_no",
            "source_type",
            "title",
            "url",
            "snippet",
            "score",
            "snapshot_available",
            "kb_doc_id",
        }


# ---- deep 桩序列（B4 前保持）----


def _deep_expected() -> list[dict[str, Any]]:
    """api.md §7.4 深度模式主干（701-708 行）的桩展开：plan/write 两阶段。"""
    return [
        {"type": "stage", "stage_key": "plan", "status": "running"},
        {"type": "stage", "stage_key": "plan", "status": "done"},
        {"type": "stage", "stage_key": "write", "status": "running", "label": "撰写报告"},
        *({"type": "token", "delta": d} for d in STUB_DEEP_TOKEN_DELTAS),
        {"type": "stage", "stage_key": "write", "status": "done"},
    ]
