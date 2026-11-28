"""unit 层：deep 门节点接 deep 子图（B22 批次 4 第一段）。

契约锚点（见各用例 docstring）：
- plan §3.3 / ARCH v1.83 §4.2 分期注：deep_enabled **独立灰度**（hitl.enabled 零
  消费——约束①互不牵动）；planner 后门节点；零命中/通道失败/门内异常 → research
  同一执行继续（deep 无 Send 形态——web/kb 消费面=子代理工具，桩化归第二段 R36）；
  has_hit → approval_wait 空操作终结（复用批次 2 approval_wait）；
- planner 产物持久化（R30 兼容扩展）：gate_payload +`planner` 键 {subtopics, outline}
  ——schema_version=1 不变（data-model.md §1.20 :622 planner? 预留；批次 2 fast
  payload 无 planner 键零回归，由 test_hitl_gate_node 既有断言锁定）；
- G3：deep_enabled=false 逐字节现状（门分支不可达、存量 deep 测试零改动通过）；
- R14①：门 update 键 ⊆ DeepState 声明（批次 4 增 approval/kb_gate 八键）。
"""

from typing import Any

import pytest

from app.agent.graphs.deep import build_deep_graph
from app.agent.guards import TurnCanceledSignal
from app.agent.nodes import kb_gate as kb_gate_module
from app.agent.nodes.kb_gate import (
    approval_wait,
    deep_hitl_gate_armed,
    deep_kb_gate,
    deep_route_after_gate,
    deep_route_after_planner,
    deep_route_by_approval,
)
from app.agent.state import DeepState


# ---- 假体（与 test_hitl_gate_node 同构——纯本地使用）----


def _row(
    chunk_id: str,
    *,
    channel: str,
    score: float,
    title: str,
    content: str = "内容",
    query_index: int = 0,
) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "kb_doc_id": f"d{chunk_id}",
        "content": content,
        "score": score,
        "heading_path": [f"文档{chunk_id}", title],
        "node_id": None,
        "channel": channel,
        "search_query_index": query_index,
    }


class _FakeRetriever:
    """按调用序返回预设行集/异常（逐子课题索引驱动——与门内 gather 对位）。"""

    def __init__(
        self,
        *,
        per_query_bm25: list[list[dict[str, Any]]] | None = None,
        per_query_vec: list[list[dict[str, Any]]] | None = None,
        bm25_exc: BaseException | None = None,
        vec_exc: BaseException | None = None,
    ) -> None:
        self.per_query_bm25 = per_query_bm25 or []
        self.per_query_vec = per_query_vec or []
        self.bm25_exc = bm25_exc
        self.vec_exc = vec_exc
        self.bm25_calls = 0
        self.vec_calls = 0

    async def bm25_search(self, query: str, *, user_id: Any, k: int) -> list[dict[str, Any]]:
        idx = self.bm25_calls
        self.bm25_calls += 1
        if self.bm25_exc is not None:
            raise self.bm25_exc
        return list(self.per_query_bm25[idx]) if idx < len(self.per_query_bm25) else []

    async def vector_search(self, query_text: str, *, user_id: Any, k: int) -> list[dict[str, Any]]:
        idx = self.vec_calls
        self.vec_calls += 1
        if self.vec_exc is not None:
            raise self.vec_exc
        return list(self.per_query_vec[idx]) if idx < len(self.per_query_vec) else []


class _FakeRerank:
    """rerank 网关假体（每次调用消耗 scores_by_call 一档，缺省恒 0.5）。"""

    def __init__(self, scores_by_call: list[list[float]] | None = None) -> None:
        self.scores_by_call = scores_by_call or []
        self.calls = 0

    async def __call__(
        self, query: str, documents: list[str], *, model_alias: str, timeout_s: float
    ) -> list[float | None]:
        scores = (
            self.scores_by_call[self.calls]
            if self.calls < len(self.scores_by_call)
            else [0.5] * len(documents)
        )
        self.calls += 1
        assert len(documents) == len(scores)
        return list(scores)


class _FailingRerank:
    async def __call__(
        self, query: str, documents: list[str], *, model_alias: str, timeout_s: float
    ) -> list[float | None]:
        raise RuntimeError("rerank 网关 500")


def _deep_hitl_spec(**overrides: Any) -> dict[str, Any]:
    """deep 轮 spec 假体：enabled=false（fast 键关）+ deep_enabled=true（被测键）。"""
    hitl: dict[str, Any] = {
        "enabled": False,
        "deep_enabled": True,
        "shadow": False,
        "no_hit_action": "web",
        "skip_query_types": ["navigational", "temporal"],
        "preview_count": 3,
        "preview_chars": 200,
        "timeout_s": 120,
        "on_timeout": "hybrid",
        "note_max_chars": 500,
        "noise_floor": {"enabled": False, "vector_sim_min": 0.0, "rerank_top1_min": 0.0},
    }
    hitl.update(overrides)
    return {
        "kb": {
            "per_query_top_k": 30,
            "rrf_k": 60,
            "kb_fuse_top_k": 20,
            "type_weights": {"factoid": [0.7, 0.3]},
            "es_enabled": True,
            "embedding_enabled": True,
        },
        "hitl": hitl,
    }


def _deep_gate_state(
    spec: dict[str, Any],
    subtopics: list[str],
    *,
    outline: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "spec": spec,
        "subtopics": list(subtopics),
        "outline": outline
        if outline is not None
        else [{"section": "S1", "intent": "直接回答"}],
        "query_type": "factoid",
        "query_lang": "zh",
        "time_range": None,
        "user_id": "u1",
        "mode": "deep",
    }
    state.update(extra)
    return state


def _gate_config(retriever: _FakeRetriever) -> dict[str, Any]:
    return {
        "configurable": {
            "kb_retriever": retriever,
            "model_mapping": {"rerank": "qwen3-rerank"},
        }
    }


def _hit_subtopics_retriever() -> _FakeRetriever:
    """两子课题均有命中（子课题 1 chunk x、子课题 2 chunk y）。"""
    return _FakeRetriever(
        per_query_bm25=[
            [_row("x", channel="bm25", score=10.0, title="命中X")],
            [_row("y", channel="bm25", score=8.0, title="命中Y")],
        ],
        per_query_vec=[
            [_row("x", channel="vector", score=0.7, title="命中X")],
            [_row("y", channel="vector", score=0.6, title="命中Y")],
        ],
    )


# ---- 判定树形态一：has_hit → await（弹窗）+ planner 产物持久化 ----


async def test_deep_gate_has_hit_awaits_with_planner_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """地板后有值=弹窗（plan §3.3）：action=await、verdict=has_hit、预览物化、
    payload 带 **planner 键 {subtopics, outline}**（R30 兼容扩展——schema_version=1
    不变）+ rewritten_queries=子课题（deep 的「子问题」=planner 子课题，plan :220）
    + 判定帧 emit stage(plan,done).extra.hitl（api.md §7.2 同构形态——rewrite 帧
    为 fast 归属，deep 侧 plan 帧归属随批次 4 契约增量钉正）；R14①：update 键 ⊆
    DeepState 声明。"""
    retriever = _hit_subtopics_retriever()
    fake = _FakeRerank([[0.9], [0.8]])
    monkeypatch.setattr(kb_gate_module, "rerank_via_gateway", fake)
    stages: list[dict[str, Any]] = []
    monkeypatch.setattr(
        kb_gate_module, "emit_stage", lambda *a, **kw: stages.append({"args": a, "kw": kw})
    )

    outline = [{"section": "S1", "intent": "直接回答"}, {"section": "S2", "intent": "对照"}]
    update = await deep_kb_gate(
        _deep_gate_state(
            _deep_hitl_spec(), ["子课题一", "子课题二"], outline=outline
        ),
        _gate_config(retriever),
    )

    assert update["kb_gate_action"] == "await"
    assert update["kb_gate_verdict"] == "has_hit"
    assert update["kb_gate_ms"] >= 0
    subqueries = update["kb_gate_subqueries"]
    assert [s["top_k_count"] for s in subqueries] == [1, 1]
    assert subqueries[0]["top1_score"] == 0.9
    assert "errors" not in update  # has_hit enforce 零异常面
    # 预览物化：跨子课题去重 ≤3 条、excerpt ≤200 字、契约六键
    previews = update["kb_gate_previews"]
    assert len(previews) == 2
    assert len({p["chunk_id"] for p in previews}) == 2
    assert all(len(p["excerpt"]) <= 200 for p in previews)
    assert all(
        set(p) == {"kb_doc_id", "chunk_id", "title", "heading_path", "excerpt", "query_index"}
        for p in previews
    )
    # 阶段 2 恢复载荷：schema_version=1 不变 + planner 产物（R30 兼容扩展）
    payload = update["kb_gate_payload"]
    assert payload["schema_version"] == 1
    assert payload["rewritten_queries"] == ["子课题一", "子课题二"]
    assert payload["query_type"] == "factoid"
    assert payload["query_lang"] == "zh"
    assert payload["planner"] == {"subtopics": ["子课题一", "子课题二"], "outline": outline}
    chunk_ids = [c["chunk_id"] for c in payload["kb_candidates"]]
    assert sorted(chunk_ids) == ["x", "y"]
    # 判定面留痕：stage(plan,done).extra.hitl（gate=triggered）——deep 侧 plan 帧归属
    assert len(stages) == 1
    assert stages[0]["args"][:2] == ("plan", "done")
    hitl_extra = stages[0]["kw"]["extra"]["hitl"]
    assert hitl_extra["gate"] == "triggered"
    assert hitl_extra["verdict"] == "has_hit"
    assert hitl_extra["gate_ms"] >= 0
    # R14①：门 update 键 ⊆ DeepState 声明（批次 4 增键后投影可达）
    assert set(update) <= set(DeepState.__annotations__)


async def test_deep_gate_preview_dedupe_and_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预览物化复用批次 2 物化器（OQ-02：3 条/200 字）：跨子课题按 chunk_id 去重、
    rerank 分全局降序、excerpt 服务端截断。"""
    long_content = "长" * 500
    retriever = _FakeRetriever(
        per_query_bm25=[
            [_row("x", channel="bm25", score=10.0, title="X", content=long_content)],
            [_row("x", channel="bm25", score=9.0, title="X", content=long_content)],
            [_row("z", channel="bm25", score=7.0, title="Z", content=long_content)],
        ],
        per_query_vec=[
            [_row("x", channel="vector", score=0.7, title="X", content=long_content)],
            [_row("x", channel="vector", score=0.6, title="X", content=long_content)],
            [_row("z", channel="vector", score=0.5, title="Z", content=long_content)],
        ],
    )
    monkeypatch.setattr(kb_gate_module, "rerank_via_gateway", _FakeRerank([[0.95], [0.85], [0.3]]))
    monkeypatch.setattr(kb_gate_module, "emit_stage", lambda *a, **kw: None)

    update = await deep_kb_gate(
        _deep_gate_state(_deep_hitl_spec(), ["子课题一", "子课题二", "子课题三"]),
        _gate_config(retriever),
    )

    previews = update["kb_gate_previews"]
    assert [p["chunk_id"] for p in previews] == ["x", "z"]  # rerank 分降序
    assert all(p["excerpt"] == "长" * 200 for p in previews)


# ---- 判定树形态二：no_hit → research 同一执行继续（deep 无 Send 形态）----


async def test_deep_gate_no_hit_research_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """地板后全空=action=web_only：deep 侧**不终结图**——路由面（deep_route_after_gate）
    送 research 同一执行继续（plan §3.3 `G-->|全部子课题 Top K 均空|R`；子代理
    kb_search 桩化归第二段 R36）；errors[] hitl_no_hit、payload=None（no_hit 轻量
    无载荷）。"""
    retriever = _FakeRetriever(per_query_bm25=[[]], per_query_vec=[[]])
    monkeypatch.setattr(kb_gate_module, "rerank_via_gateway", _FakeRerank([]))
    stages: list[dict[str, Any]] = []
    monkeypatch.setattr(kb_gate_module, "emit_stage", lambda *a, **kw: stages.append(kw))

    update = await deep_kb_gate(
        _deep_gate_state(_deep_hitl_spec(), ["冷门子课题"]), _gate_config(retriever)
    )

    assert update["kb_gate_action"] == "web_only"
    assert update["kb_gate_verdict"] == "no_hit"
    assert update["kb_gate_payload"] is None
    assert update["kb_gate_previews"] == []
    assert update["errors"] == [
        {
            "code": "hitl_no_hit",
            "detail": {
                "subqueries": [{"query_index": 0, "top_k_count": 0}]
            },
        }
    ]
    assert stages[0]["extra"]["hitl"]["verdict"] == "no_hit"
    assert "candidates" not in update  # deep 无 candidates 消费通道——零写入
    # web_only 路由=继续 research（非 fast 的 web Send 形态）
    assert deep_route_after_gate(update) == "research"


# ---- 判定树形态三：通道全败 → 回落现状（research 照跑，不判无）----


async def test_deep_gate_channel_failed_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任一子课题全部通道失败=「不知道」（plan §4.1/R28 同源）：回落现状 hybrid +
    errors[] hitl_gate_error——不判无、无 verdict 判定面帧；路由=继续 research。"""
    retriever = _FakeRetriever(bm25_exc=ValueError("es down"), vec_exc=ValueError("pg down"))
    monkeypatch.setattr(kb_gate_module, "rerank_via_gateway", _FakeRerank([]))
    monkeypatch.setattr(kb_gate_module, "emit_stage", lambda *a, **kw: None)  # 本分支零 emit

    update = await deep_kb_gate(
        _deep_gate_state(_deep_hitl_spec(), ["子课题一"]), _gate_config(retriever)
    )

    assert update["kb_gate_action"] == "hybrid"
    assert update["errors"] == [{"code": "hitl_gate_error", "detail": {"stage": "kb_gate"}}]
    assert "kb_gate_verdict" not in update
    assert deep_route_after_gate(update) == "research"  # 现状路径照跑


# ---- 判定树形态四：shadow → 行为一律现状 ----


async def test_deep_gate_shadow_keeps_research(monkeypatch: pytest.MonkeyPatch) -> None:
    """shadow：门照常检索落留痕、行为一律现状（has_hit 也不挂起——action=hybrid →
    research 照跑）；extra.hitl.gate="shadow"、零 errors 零 awaiting。"""
    retriever = _hit_subtopics_retriever()
    monkeypatch.setattr(kb_gate_module, "rerank_via_gateway", _FakeRerank([[0.9], [0.8]]))
    stages: list[dict[str, Any]] = []
    monkeypatch.setattr(kb_gate_module, "emit_stage", lambda *a, **kw: stages.append(kw))

    update = await deep_kb_gate(
        _deep_gate_state(_deep_hitl_spec(shadow=True), ["子课题一", "子课题二"]),
        _gate_config(retriever),
    )

    assert update["kb_gate_action"] == "hybrid"
    assert update["kb_gate_verdict"] == "has_hit"
    assert "errors" not in update
    assert stages[0]["extra"]["hitl"]["gate"] == "shadow"
    assert deep_route_after_gate(update) == "research"


# ---- 门内代码异常 → 回落现状（R38）----


async def test_deep_gate_internal_error_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R38：门内代码异常（rerank 失败等）→ 回落现状 hybrid + hitl_gate_error——
    异常与空结果类型上分开，绝不落「空列表」分支解释为判无。"""
    retriever = _hit_subtopics_retriever()
    monkeypatch.setattr(kb_gate_module, "rerank_via_gateway", _FailingRerank())
    monkeypatch.setattr(kb_gate_module, "emit_stage", lambda *a, **kw: None)

    update = await deep_kb_gate(
        _deep_gate_state(_deep_hitl_spec(), ["子课题一"]), _gate_config(retriever)
    )
    assert update["kb_gate_action"] == "hybrid"
    assert update["errors"] == [{"code": "hitl_gate_error", "detail": {"stage": "kb_gate"}}]


# ---- 门关闭防御 + 取消感知 ----


async def test_deep_gate_disabled_defensive_hybrid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """deep 门前置竞态防御：deep_enabled=false 时门节点被意外到达（spec 快照漂移）
    → 现状 hybrid 直通、零检索零 rerank（G3 缺省态纵深）。"""
    retriever = _hit_subtopics_retriever()
    spec = _deep_hitl_spec()
    spec["hitl"]["deep_enabled"] = False
    monkeypatch.setattr(kb_gate_module, "rerank_via_gateway", _FakeRerank([]))

    update = await deep_kb_gate(_deep_gate_state(spec, ["子课题一"]), _gate_config(retriever))

    assert update == {"kb_gate_action": "hybrid"}
    assert retriever.bm25_calls == 0 and retriever.vec_calls == 0


async def test_deep_gate_cancel_aware_at_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """取消感知（R14② 同款）：deep 门在阶段 1 内有协程——入口 ensure_not_canceled
    抛 TurnCanceledSignal。"""
    retriever = _hit_subtopics_retriever()
    config = _gate_config(retriever)
    config["configurable"]["cancel_flags"] = {"t1": "user"}
    config["configurable"]["turn_id"] = "t1"
    monkeypatch.setattr(kb_gate_module, "rerank_via_gateway", _FakeRerank([]))

    with pytest.raises(TurnCanceledSignal):
        await deep_kb_gate(_deep_gate_state(_deep_hitl_spec(), ["子课题一"]), config)


# ---- deep 门前置判定（零 IO）——deep_enabled 独立灰度（约束①）----


def test_deep_hitl_gate_armed_conditions() -> None:
    """deep 门前置：deep_enabled ∧ query_type ∉ skip_query_types ∧ kb_available
    → 进门；**enabled 键零消费**（互不牵动——enabled=true 而 deep_enabled=false
    不进门）；无 correction_round 判定（deep 无纠错回边）。"""
    spec = _deep_hitl_spec()  # enabled=False + deep_enabled=True
    base = _deep_gate_state(spec, ["子课题一"])
    assert deep_hitl_gate_armed(base, spec) is True

    # 互不牵动：enabled=true 而 deep_enabled=false → 不进门（deep 键独立）
    spec_fast_only = _deep_hitl_spec(deep_enabled=False, enabled=True)
    assert (
        deep_hitl_gate_armed(_deep_gate_state(spec_fast_only, ["s1"]), spec_fast_only)
        is False
    )
    # hitl 节缺失（存量 spec 形态）
    assert (
        deep_hitl_gate_armed(_deep_gate_state({"kb": spec["kb"]}, ["s1"]), {"kb": spec["kb"]})
        is False
    )
    # 跳过题型（deep planner 产 temporal 可达；navigational 对 deep 恒死键——
    # planner 判导航输出 factoid，deep_planner.py:16）
    assert (
        deep_hitl_gate_armed(_deep_gate_state(spec, ["s1"], query_type="temporal"), spec)
        is False
    )
    # KB 双通道全关不进门
    spec_off = _deep_hitl_spec()
    spec_off["kb"]["es_enabled"] = False
    spec_off["kb"]["embedding_enabled"] = False
    assert deep_hitl_gate_armed(_deep_gate_state(spec_off, ["s1"]), spec_off) is False


# ---- 路由器 ----


def test_deep_routers_three_faces() -> None:
    """三路由器：START 条件入口（无 approval_choice=planner〔阶段 1 现状〕/任意
    choice=research〔阶段 2 跳过 planner〕）；planner 出边（armed→kb_gate｜否则
    research 现状）；kb_gate 出边（await→approval_wait｜web_only/hybrid→research
    继续或照跑）。"""
    assert deep_route_by_approval({}) == "planner"
    assert deep_route_by_approval({"approval_choice": None}) == "planner"
    for choice in ("kb_only", "web_only", "both", "custom"):
        assert deep_route_by_approval({"approval_choice": choice}) == "research"

    spec = _deep_hitl_spec()
    assert deep_route_after_planner(_deep_gate_state(spec, ["s1"])) == "kb_gate"
    assert (
        deep_route_after_planner(_deep_gate_state(spec, ["s1"], query_type="temporal"))
        == "research"
    )
    spec_closed = _deep_hitl_spec(deep_enabled=False)
    assert (
        deep_route_after_planner(_deep_gate_state(spec_closed, ["s1"])) == "research"
    )

    assert deep_route_after_gate({"kb_gate_action": "await"}) == "approval_wait"
    assert deep_route_after_gate({"kb_gate_action": "web_only"}) == "research"
    assert deep_route_after_gate({"kb_gate_action": "hybrid"}) == "research"
    assert deep_route_after_gate({}) == "research"  # 防御：无动作键继续 research


# ---- approval_wait 复用 + 拓扑接线 ----


def test_deep_graph_topology_includes_gate_nodes() -> None:
    """拓扑增量（plan §3.3）：deep 图挂 kb_gate/approval_wait 两节点 + 三条件边
    （START 入口分 planner/research 两臂；planner 出 kb_gate/research 两臂；
    kb_gate 出 approval_wait/research 两臂）+ approval_wait→END。"""
    graph = build_deep_graph()
    nodes = set(graph.get_graph().nodes)
    assert {"kb_gate", "approval_wait"} <= nodes

    edge_pairs = {(edge.source, edge.target) for edge in graph.get_graph().edges}
    assert ("approval_wait", "__end__") in edge_pairs
    kb_gate_targets = {target for source, target in edge_pairs if source == "kb_gate"}
    assert {"approval_wait", "research"} <= kb_gate_targets
    planner_targets = {target for source, target in edge_pairs if source == "planner"}
    assert {"kb_gate", "research"} <= planner_targets
    start_targets = {target for source, target in edge_pairs if source == "__start__"}
    assert {"planner", "research"} <= start_targets


def test_deep_approval_wait_reuses_batch2_zero_op() -> None:
    """approval_wait 空操作终结节点复用批次 2 实现（约束⑥复用不重构）。"""
    assert approval_wait({}, None) == {}
