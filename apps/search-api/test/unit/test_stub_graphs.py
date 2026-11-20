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
- deep 序列（M2 批次 4：planner/research/reflect/supplement/guards/writer 接真——write
  桩已被 writer 真节点替换，LLM 假体直注）：plan done 带 extra.subtopics
  （api.md §7.4 钉死形态），planner LLM 假体直注（test_deep_planner.py 锁节点语义）；
- 状态契约（R14①）：MainState 投影薄壳 ⊇ FastState 输入字段（TurnRunner 注入面），
  产出键同名声明经顶层 updates 回填；candidates/errors 带 add、degraded 带或 reducer
  （Send 扇出并行副本正确合并——键集不变）；
- deep 产出投影（M2 批次 1，R14①）：MainState 同名声明 DeepState 全部产出键且类型
  逐键对齐（deep 子图产出→主图回填不丢失）；degraded/errors 沿用既有横切声明，
  deep 扇出 reducer 留批次 2 research 接入时再定；
- 主图拓扑契约（B13）：START→safety_check（普通边，输入安全 Layer 1 pre_route）→
  route_by_mode 条件边原样后移（fast/deep 两目标 conditional=True，无 START 直达
  旧边）；主图 channel 含 safety 两键（safety_category/safety_action——拒绝语义走
  ContentRejectedError 异常通道，safety_blocked 死键已删）
  且 FastState/DeepState 不加（Layer 2 走异常不上 state）；直通轮零 safety 信号——
  事件序列与 B3 形态逐字节一致（fixture spec 显式装载空词表 safety 节零告警）。
"""

import operator
import time
import typing
from collections.abc import Mapping
from typing import Any

import pytest

from app.agent.graphs.main import DEFAULT_GRAPH, graph as main_graph, route_by_mode
from app.agent.graphs.registry import GRAPH_REGISTRY
from app.agent.nodes import fuse_rerank as fuse_module
from app.agent.nodes import merge_dedupe as merge_module
from app.agent.state import DeepState, FastState, MainState
from app.agent.tools.registry import ReadChain, SearchChain, ToolRegistry


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
                # B13：safety_check 直通返回 {} → LangGraph updates 记 {node: None}
                # （空更新帧形态，turn_service 顶层合并同防护）——None 非 state 增量，跳过
                if isinstance(node_update, Mapping):
                    final.update(node_update)
    return signals, final


#: 接真语义的标准 spec 快照（TurnRunner 注入形态；routing 初值恒等 fast→fast/deep→deep）。
#: B13：safety 节显式装载（load_safety_config 合法形态零告警——缺节会刷 fail-open
#: WARNING 打破「正常路径零告警」断言）；空词表零命中零 grey → safety_check 零更新直通。
_ROUTING_SPEC: dict[str, Any] = {
    "routing": {"default_mode": "fast", "modes": {"fast": "fast", "deep": "deep"}},
    "safety": {
        "enabled": True,
        "mode": "hybrid",
        "danger_terms": [],
        "profanity_terms": [],
        "grey_rules": [],
    },
}


@pytest.fixture(autouse=True)
def _mock_llm_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    """rewrite/synthesis/deep planner/deep research/deep reflect 已接真（fast 节点
    1/5+5/5 + deep planner+research+reflect）：路由/序列测试不依赖真 LLM——fixture
    假体直注（rewrite/planner/reflect 结构化输出 + synthesis 流式 content + research
    派发桩，零网络）。

    research 桩形态：主协调器模型单帧纯文本收尾（零 task 派发）→ research 节点零子代理
    运行（序列测试只验事件骨架——子代理语义由 test_deep_research.py 锁定）。
    reflect 桩形态：首判 sufficient（gaps 清空）→ 回路退出发 reflect done（补搜回路
    循环语义由 test_deep_reflect.py 锁定）。"""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    from app.agent.nodes import deep_planner as deep_planner_module
    from app.agent.nodes import deep_reflect as deep_reflect_module
    from app.agent.nodes import deep_research as deep_research_module
    from app.agent.nodes import deep_writer as deep_writer_module
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

    class _PlanChain:
        async def ainvoke(self, prompt: Any, config: Any = None) -> dict[str, Any]:
            return {
                "subtopics": ["deep planner 桩期子课题一", "deep planner 桩期子课题二"],
                "outline": [
                    {"section": "背景", "intent": "问题背景与信息面"},
                    {"section": "分析", "intent": "各子课题证据综合"},
                ],
                "query_type": "multi_facet",
                "time_range": None,
                "memory_directive": None,
            }

    class _PlannerModel:
        def with_structured_output(
            self, schema: Any, *, method: str | None = None, **kw: Any
        ) -> _PlanChain:
            return _PlanChain()

    class _ReflectChain:
        async def ainvoke(self, prompt: Any, config: Any = None) -> dict[str, Any]:
            return {"sufficient": True, "gaps": []}  # 首判 sufficient → 回路退出

    class _ReflectModel:
        def with_structured_output(
            self, schema: Any, *, method: str | None = None, **kw: Any
        ) -> _ReflectChain:
            return _ReflectChain()

    class _SynthModel:
        async def astream(self, messages: Any, config: Any = None, **kw: Any) -> Any:
            from langchain_core.messages import AIMessageChunk

            for piece in ("综合回答正文[1]。", "结论[2]。"):
                yield AIMessageChunk(content=piece)

    class _WriterModel:
        """deep writer 流式假体（批次 4 writer 接真）：三片报告正文，标题与 planner
        假体大纲逐字一致（背景/分析——分节 label 推进可观测）。"""

        async def astream(self, prompt: Any, config: Any = None, **kw: Any) -> Any:
            from langchain_core.messages import AIMessageChunk

            for piece in DEEP_WRITER_PIECES:
                yield AIMessageChunk(content=piece)

    class _ResearchMainModel(GenericFakeChatModel):
        """派发桩：单帧纯文本收尾（零 task 派发）——bind_tools 返回 self（探针同款）。"""

        def bind_tools(self, tools: Any, **kwargs: Any) -> "_ResearchMainModel":  # noqa: ANN201
            return self

    monkeypatch.setattr(rewrite_module, "build_light_model", lambda mapping: _RewriteModel())
    monkeypatch.setattr(synthesis_module, "build_chat_model", lambda alias, **kw: _SynthModel())
    monkeypatch.setattr(deep_planner_module, "build_chat_model", lambda alias, **kw: _PlannerModel())
    monkeypatch.setattr(deep_reflect_module, "build_chat_model", lambda alias, **kw: _ReflectModel())
    monkeypatch.setattr(deep_writer_module, "build_chat_model", lambda alias, **kw: _WriterModel())
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


def _warnings(log_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in log_records if r["level"].name == "WARNING"]


def _route_warnings(log_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """路由域（extra.graph == "main"）WARNING——层①/层②告警断言面。

    B13 起 safety_check 在 spec 缺 safety 节时另发 safety 域 fail-open 告警
    （AgentLog.graph="safety"，独立防线独立告警）——域过滤后互不串扰。
    """
    return [
        r
        for r in log_records
        if r["level"].name == "WARNING" and r["extra"].get("graph") == "main"
    ]


# ---- 假搜索源（web_search 可冻结链经 configurable 注入，零网络）----


#: synthesis 假体答案（fixture astream 拼接；引用 [1][2] 对应假候选两条）
SYNTH_MOCK_ANSWER_MD = "综合回答正文[1]。结论[2]。"

#: deep writer 假体报告（批次 4 writer 接真——分片含「## 背景」「## 分析」标题，
#: 与 planner 假体大纲逐字一致 → 分节 label 推进可观测；**零引用标记**——research
#: 零派发桩形态账本为空，finalize 引用核对会把 [n] 作越界剔除，假体正文按无源
#: 报告形态构造）
DEEP_WRITER_PIECES: list[str] = [
    "# 调研报告\n\n## 摘要\n\n核心发现。\n\n## 目录\n\n- 背景\n- 分析\n\n",
    "## 背景\n\n背景内容。\n\n",
    "## 分析\n\n分析内容。\n\n## 结论\n\n收束。",
]
DEEP_WRITER_MOCK_ANSWER_MD = "".join(DEEP_WRITER_PIECES)

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
        self, query: str, source: str, *, time_range: str | None = None, top_k: int = 5,
            retry_sink: dict[str, int] | None = None
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


def _deep_config(registry: _FakeRegistry | None = None) -> dict[str, Any]:
    """deep 轮 TurnRunner 注入形态（M2 批次 2 增量 read_chain——research 消费面）。

    research 节点 fail-fast 面（搜索链未注入/为空 → SearchChainUnavailable 500201 族）
    需要链在场；读取链本配置给主位 [firecrawl]（派发桩不派发 task，读取面不触网）。"""
    return {
        "configurable": {
            "search_chain": SearchChain(
                sources=["tavily"], backup=None, degraded=False, detail="tavily"
            ),
            "read_chain": ReadChain(
                sources=["firecrawl"], backup=None, degraded=False, detail="firecrawl"
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
        # running 已由 TurnRunner 图前发布（§4.3）；v1.20 起 done.extra 恒带 strategy
        {
            "type": "stage",
            "stage_key": "rewrite",
            "status": "done",
            "extra": {"strategy": "web"},
        },
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


# ---- 主图拓扑契约（B13：START→safety_check→条件边后移）----


def test_main_graph_topology_start_safety_check_then_route() -> None:
    """拓扑契约（B13）：START→safety_check 普通边；route_by_mode 条件边原样后移至
    safety_check（fast/deep 两目标 + conditional=True）；无 START 直达子图的旧边；
    route_by_mode 函数与 targets 完全不动（双层兜底语义不变）。"""
    assert {"safety_check", "fast", "deep"} <= set(main_graph.nodes)
    edges = {(e.source, e.target, e.conditional) for e in main_graph.get_graph().edges}
    assert ("__start__", "safety_check", False) in edges
    assert ("safety_check", "fast", True) in edges
    assert ("safety_check", "deep", True) in edges
    assert not any(
        source == "__start__" and target != "safety_check"
        for source, target, _ in edges
    )


def test_main_graph_channels_include_safety_keys_only_on_main() -> None:
    """主图 channel 含 safety 两键（safety_check 节点产出/标注通道；safety_blocked
    死键已删——拒绝轮走 ContentRejectedError 异常，无 state 消费面）；子图 state 不加
    （Layer 2 拦截走 ContentRejectedError 异常不上 state——B13 契约行）。"""
    from app.agent.state import DeepState, FastState, MainState

    keys = {"safety_category", "safety_action"}
    assert keys <= set(MainState.__annotations__)
    assert keys <= set(main_graph.channels)
    assert "safety_blocked" not in MainState.__annotations__
    assert "safety_category" not in FastState.__annotations__
    assert "safety_category" not in DeepState.__annotations__


#: FastState 输入字段（TurnRunner 注入面；ARCH §4.1）
_FAST_INPUT_KEYS = {
    "turn_id",
    "thread_id",
    "user_id",
    "mode",
    "query",
    "chat_model_key",
    "history",
    "history_recall",  # v1.51：回顾装配第二通道（recall 轮 synthesis 消费）
    "images",
    "spec",
    "query_lang",
}
#: FastState 产出键（子图节点更新 → 主图同名回填，TurnRunner 经顶层 updates 消费）
_FAST_OUTPUT_KEYS = {
    "answer_md",
    "query_type",
    "answerable_from_knowledge",
    "search_strategy",  # v1.44 B9②：rewrite 顺带产出（direct 条件边 + synthesis 变体消费）
    "time_range",
    "rewritten_queries",
    "candidates",
    "images_raw",
    "fused",
    "errors",
    "degraded",
}
#: DeepState 产出键（M2 批次 1 投影扩展：deep 子图产出 → 主图同名回填不丢失——
#: planner/research/reflect/writer 产出 + 横切，api.md §7.4 深度序列的状态面）。
#: degraded/errors 在 MainState 沿用既有横切声明（fast 扇出 add/or reducer 语义不变，
#: DeepState 侧为普通类型）——deep 扇出 reducer 留批次 2 research 接入时再定（不预设计）。
_DEEP_OUTPUT_KEYS = {
    "subtopics",
    "outline",
    "subagent_reports",
    "visited_urls",
    "searched_queries",
    "snapshots",
    "collected_sources",
    "reflect_round",
    "gaps",
    "report_md",
    "answer_md",
    "usage_total",
    "degraded",
    "degraded_reason",
    "errors",
}
#: 本批新增的 deep 投影键（普通类型逐键对齐 DeepState）；degraded/subagent_reports
#: 不在内——degraded 维持普通布尔（两侧同型但批次 3 guards 再定）；subagent_reports
#: 两侧形态有意不同（M2 批次 2：DeepState 改 Annotated[add] 扇出合流、MainState 保持
#: 批次 1 普通类型零改动——deep 子图终态回填全量值，普通覆盖语义即正确投影）。
#: errors 两侧自此同注解（M2 批次 2 DeepState 同步改 Annotated[add]——在对齐域内）。
_DEEP_TYPE_ALIGNED_KEYS = _DEEP_OUTPUT_KEYS - {"degraded", "subagent_reports"}


def test_main_state_projects_all_fast_inputs() -> None:
    """R14①：主图投影 ⊇ FastState 输入字段——TurnRunner 注入键零丢失直达子图。"""
    assert _FAST_INPUT_KEYS <= set(MainState.__annotations__)
    assert _FAST_INPUT_KEYS <= set(FastState.__annotations__)


def test_history_recall_projection_type_aligned_across_three_states() -> None:
    """R14①（v1.51）：`history_recall` 在主图壳与两张子图**同注解**——投影映射零类型
    漂移（键集归属已由上一条覆盖，本条补类型面：一处改注解另一处不跟 → 回填形态漂移）。

    DeepState 侧为「输入字段同构」契约声明，deep 不消费（P1 裁决：navigational/recall
    均只走 fast）。
    """
    annotations = {
        "MainState": MainState.__annotations__["history_recall"],
        "FastState": FastState.__annotations__["history_recall"],
        "DeepState": DeepState.__annotations__["history_recall"],
    }

    assert len(set(annotations.values())) == 1, annotations
    assert next(iter(annotations.values())) == list[dict[str, str]]  # HistoryItem 形态


def test_main_state_backfills_all_fast_outputs() -> None:
    """R14①：FastState 产出键在主图壳同名声明——子图更新经顶层 updates 回填。"""
    assert _FAST_OUTPUT_KEYS <= set(MainState.__annotations__)
    assert _FAST_OUTPUT_KEYS <= set(FastState.__annotations__)


def test_main_state_backfills_all_deep_outputs() -> None:
    """R14①（M2 批次 1）：DeepState 产出键在主图壳同名声明——deep 子图产出经顶层
    updates 回填不丢失（report_md/usage_total/degraded_reason 等 TurnRunner 终态消费面）。"""
    assert _DEEP_OUTPUT_KEYS <= set(MainState.__annotations__)
    assert _DEEP_OUTPUT_KEYS <= set(DeepState.__annotations__)


def test_main_state_deep_output_types_align_with_deep_state() -> None:
    """deep 产出投影类型逐键对齐 DeepState（M2 批次 1 立；批次 2 域修订）：投影键注解
    一致——回填值类型零漂移。errors 两侧自此同注解（批次 2 DeepState 同步 Annotated[add]
    ——扇出合流对齐 MainState 既有横切）；subagent_reports 两侧形态有意不同（DeepState
    add、MainState 批次 1 普通类型零改动——子图终态回填全量值），不在对齐域。"""
    for key in _DEEP_TYPE_ALIGNED_KEYS:
        assert (
            MainState.__annotations__[key] == DeepState.__annotations__[key]
        ), f"MainState.{key} 类型与 DeepState.{key} 不一致"


def test_deep_state_fanout_reducers_batch2() -> None:
    """M2 批次 2 reducer 改造（派单裁决）：DeepState.subagent_reports/errors 用
    Annotated[list, add] 支持扇出合流（research 门控/后续 supplement 轮各自增量）；
    degraded 维持普通布尔（批次 3 guards 再定）；MainState 投影零改动
    （subagent_reports 仍为普通 list——子图终态回填全量值即正确投影）。"""
    hints = typing.get_type_hints(DeepState, include_extras=True)
    assert operator.add in getattr(hints["subagent_reports"], "__metadata__", ())
    assert operator.add in getattr(hints["errors"], "__metadata__", ())
    assert getattr(hints["degraded"], "__metadata__", ()) == ()  # 普通布尔（无 reducer）
    main_hints = typing.get_type_hints(MainState, include_extras=True)
    assert getattr(main_hints["subagent_reports"], "__metadata__", ()) == ()  # 投影零改动
    assert operator.add in getattr(main_hints["errors"], "__metadata__", ())  # 横切既有面


def test_state_query_type_literal_matches_rewrite_node() -> None:
    """state 层 QueryType 与 fast_rewrite 层逐字一致（v1.51 +navigational）。

    双源漂移防线（code-reviewer P1）：state 的 Literal 是图内 state 契约注解，node 侧
    Schema 是 LLM 输出校验面——两者任一缺失新枚举都会让「能产出但标注不认」或反之
    静默偏差；deep 批次按 state 注解对齐时尤易漏语义（navigational 仅 fast 输出）。
    """
    import typing as _typing

    from app.agent.nodes.fast_rewrite import QueryType as NodeQueryType
    from app.agent.state import QueryType as StateQueryType

    assert _typing.get_args(StateQueryType) == _typing.get_args(NodeQueryType)
    assert "navigational" in _typing.get_args(StateQueryType)


def test_state_search_strategy_literal_matches_rewrite_node() -> None:
    """MainState/FastState 的 search_strategy Literal 与 fast_rewrite 四值逐字一致。

    v1.51 +recall：state 侧缺值会让「rewrite 产出 recall、state 注解三值」的漂移
    在静态检查面静默（runtime 仍走得通，故必须单测锁死）。
    """
    import typing as _typing

    from app.agent.nodes.fast_rewrite import SearchStrategy
    from app.agent.state import FastState, MainState

    expected = _typing.get_args(SearchStrategy)
    assert _typing.get_args(MainState.__annotations__["search_strategy"]) == expected
    assert _typing.get_args(FastState.__annotations__["search_strategy"]) == expected
    assert "recall" in expected


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
    """编译产物投影通道：主图壳与 fast 子图均建有 FastState 全字段 channel（R14① 物理通道）；
    deep 产出键在主图壳建有同名 channel（M2 批次 1——deep 回填的物理通道面）。"""
    fast_keys = _FAST_INPUT_KEYS | _FAST_OUTPUT_KEYS
    assert fast_keys <= set(main_graph.channels)
    from app.agent.graphs.fast import graph as fast_compiled

    assert fast_keys <= set(fast_compiled.channels)
    assert _DEEP_OUTPUT_KEYS <= set(main_graph.channels)


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
    """路由二：轮内 spec.routing 快照 mode=deep → deep 子图（M2 批次 4 planner+
    research+reflect+writer 接真——reflect 桩首判 sufficient → 回路退出发 done，条件边
    guards 出口经 guards 直通 writer）；序列对齐 §7.4（plan done 带 extra.subtopics
    → search running 带 extra.subagents——派发桩零 task，无后续 read/sources 帧 →
    reflect done → write 分节 running×2 + done extra.sections）；产出经顶层 updates
    回填；零告警。"""
    signals, final = await _run(
        {"mode": "deep", "query": "多框架对比研究报告", "spec": _ROUTING_SPEC},
        config=_deep_config(),
    )

    assert final["answer_md"] == DEEP_WRITER_MOCK_ANSWER_MD
    assert final["subtopics"] == ["deep planner 桩期子课题一", "deep planner 桩期子课题二"]
    assert final["query_type"] == "multi_facet"
    # research 桩零派发：五产出键全量回写（空集——覆盖语义下显式空投影）
    assert final["subagent_reports"] == []
    assert final["visited_urls"] == [] and final["searched_queries"] == []
    assert final["snapshots"] == [] and final["collected_sources"] == []
    # reflect 桩首判 sufficient：gaps 清空、回路退出（supplement 未运行 → reflect_round 不落 state）
    assert final["gaps"] == []
    assert "reflect_round" not in final
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
        ("deep", DEEP_WRITER_MOCK_ANSWER_MD),
    ]
    for mode, answer_md in cases:
        log_records.clear()
        registry = _FakeRegistry()
        signals, final = await _run(
            {"mode": mode, "query": "x"},
            config=_search_config(registry) if mode == "fast" else _deep_config(registry),
        )

        assert final["answer_md"] == answer_md
        warnings = _route_warnings(log_records)
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
    warnings = _route_warnings(log_records)
    assert len(warnings) == 1
    assert "内置默认" in warnings[0]["message"]
    assert warnings[0]["extra"]["mode"] == "fast"


async def test_default_mode_resolved_via_modes(log_records: list[dict[str, Any]]) -> None:
    """default_mode 经 modes 解析为图名（v1.19 键域口径）：default_mode=deep 且 mode 缺失 → deep 兜底。"""
    spec = {"routing": {"default_mode": "deep", "modes": {"fast": "fast", "deep": "deep"}}}
    signals, final = await _run({"query": "x", "spec": spec}, config=_deep_config())

    assert final["answer_md"] == DEEP_WRITER_MOCK_ANSWER_MD
    warnings = _route_warnings(log_records)
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
    warnings = _route_warnings(log_records)
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
    """api.md §7.4 深度模式主干（701-708 行）的现行展开（M2 批次 3：planner+research+
    reflect 接真——plan done 带 extra.subtopics、search running 带 extra.subagents、
    reflect 桩首判 sufficient → 回路退出发 reflect done（api.md §7.4:866「reflect
    done」帧，补搜回路循环语义由 test_deep_reflect.py 锁定）；supplement/guards/write
    仍为桩；派发桩零 task → 无 read/sources 后续帧）。

    M2 批次 1 起：plan running 归 TurnRunner 图前发布（同 fast rewrite 先例——§7.4
    序列的 stage(plan,running) 仍在，但发射源改为 TurnRunner，图内 plan 只发 done，
    避免双 running；本函数只覆盖图内信号面，TurnRunner 图前 running 由
    test_first_stage.py 锁定）。
    """
    return [
        {
            "type": "stage",
            "stage_key": "plan",
            "status": "done",
            "extra": {"subtopics": 2},  # api.md §7.4「stage(plan,done,extra.subtopics)」钉死形态
        },
        {
            "type": "stage",
            "stage_key": "search",
            "status": "running",  # api.md §7.4「stage(search,running,extra.subagents[3])」形态
            "extra": {
                "subagents": [
                    {"subtopic": "deep planner 桩期子课题一", "status": "pending"},  # §7.2 钉死键面（name 未钉键已移除）
                    {"subtopic": "deep planner 桩期子课题二", "status": "pending"},
                ]
            },
        },
        # reflect 桩首判 sufficient（gaps 空）→ 回路退出：仅 done 帧（§7.4:866 形态；
        # running 帧仅在进补搜轮时发射——test_deep_reflect 锁定）
        {"type": "stage", "stage_key": "reflect", "status": "done"},
        # writer 接真（批次 4）：入口 running(label=撰写第 1 节) → 分节标题出现推进
        # running(撰写第 2 节) → done(extra.sections=2)——api.md §7.4:867 形态；
        # token 走 messages 流域（custom 流零 token——signals.py:13-24 深桩期形态已废）
        {"type": "stage", "stage_key": "write", "status": "running", "label": "撰写第 1 节"},
        {"type": "stage", "stage_key": "write", "status": "running", "label": "撰写第 2 节"},
        {"type": "stage", "stage_key": "write", "status": "done", "extra": {"sections": 2}},
    ]
