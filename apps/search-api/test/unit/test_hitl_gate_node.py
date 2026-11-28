"""unit 层：kb_gate 门节点接 fast 子图（B22 批次 2 第三段）。

契约锚点（见各用例 docstring）：
- 拓扑增量（plan §3.2 / ARCH §4.1 :484——子图内扩展主图零改动，ADR-012 第三例）：
  rewrite 条件边 +`kb_gate` 分支（hitl 启用∧首趟∧非跳过题型∧KB 可用——零 IO 前置）；
  kb_gate 出条件边三叉（await→approval_wait 空操作终结 / web_only→web Send×N /
  hybrid→现状 web+kb 双 Send）；START 条件入口两阶段恢复（approval_choice 分支——
  kb_only 直达 merge_dedupe 不发 kb Send〔约束④：KB 候选已随 stage-2 初始 state
  注入，TurnRunner 段接线〕）；
- 判定树（api.md §7.4:1086-1094 / plan §4.1）：地板后有值=弹窗（await）/
  地板后全空=web 直达（KB 候选不进 candidates + errors[] hitl_no_hit）/
  任一子问题全部通道失败=回落现状 hybrid（errors[] hitl_gate_error，不判无）/
  shadow=行为一律现状 hybrid（零 awaiting 零 errors——留痕 extra.hitl.gate="shadow"）；
- G3 灰度纪律：hitl 关闭=现状逐字节（扇出形态 Send 逐一相等、门分支不可达、
  存量 fast 单测零改动通过）；
- 取消感知（R14② 同款）：门节点入口 ensure_not_canceled——门在阶段 1 内有协程
  （plan §9 门段途中取消走既有节点边界感知）。
"""

from typing import Any

import pytest

from app.agent.graphs.fast import build_fast_graph
from app.agent.guards import TurnCanceledSignal
from app.agent.nodes import kb_gate as kb_gate_module
from app.agent.nodes.kb_gate import (
    GATE_CONCURRENCY_LIMIT,
    approval_wait,
    kb_gate,
    route_after_gate,
    route_by_approval,
    hitl_gate_armed,
)
from app.agent.nodes.web_search import search_fanout


# ---- 假体（本地形态——与 test_hitl_gate_pipeline 同构，纯本地使用）----


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
    """按 query 序返回预设行集/异常（逐子问题索引驱动——与门内 gather 对位）。"""

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
    def __init__(self, scores_by_call: list[list[float]] | None = None) -> None:
        self.scores_by_call = scores_by_call or []
        self.calls = 0

    async def __call__(self, query: str, documents: list[str], *, model_alias: str, timeout_s: float) -> list[float | None]:
        scores = (
            self.scores_by_call[self.calls]
            if self.calls < len(self.scores_by_call)
            else [0.5] * len(documents)
        )
        self.calls += 1
        assert len(documents) == len(scores)
        return list(scores)


def _hitl_spec(**overrides: Any) -> dict[str, Any]:
    hitl: dict[str, Any] = {
        "enabled": True,
        "deep_enabled": False,
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
    return {"kb": {"per_query_top_k": 30, "rrf_k": 60, "kb_fuse_top_k": 20, "type_weights": {"factoid": [0.7, 0.3]}, "es_enabled": True, "embedding_enabled": True}, "hitl": hitl}


def _gate_state(spec: dict[str, Any], queries: list[str], **extra: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "spec": spec,
        "rewritten_queries": queries,
        "query_type": "factoid",
        "query_lang": "zh",
        "time_range": None,
        "user_id": "u1",
        "correction_round": 0,
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


def _hit_subquery_retriever() -> _FakeRetriever:
    """两子问题均有命中（子问题 1 chunk x、子问题 2 chunk y+重复 x 变体）。"""
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


# ---- 判定树形态一：has_hit → await（弹窗）----


async def test_gate_has_hit_awaits_with_materialized_previews(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """地板后有值=弹窗（api §7.4 带审批形态）：action=await、verdict=has_hit、
    预览物化 ≤3 条 excerpt ≤200 字跨子问题去重、payload 带 schema_version+候选、
    零 errors（enforce 非 shadow 无异常面）。"""
    retriever = _hit_subquery_retriever()
    fake = _FakeRerank([[0.9], [0.8]])
    monkeypatch.setattr(kb_gate_module, "rerank_via_gateway", fake)
    stages: list[dict[str, Any]] = []
    monkeypatch.setattr(
        kb_gate_module, "emit_stage", lambda *a, **kw: stages.append({"args": a, "kw": kw})
    )

    update = await kb_gate(
        _gate_state(_hitl_spec(), ["问题一", "问题二"]),
        _gate_config(retriever),
    )

    assert update["kb_gate_action"] == "await"
    assert update["kb_gate_verdict"] == "has_hit"
    assert update["kb_gate_ms"] >= 0
    subqueries = update["kb_gate_subqueries"]
    assert [s["top_k_count"] for s in subqueries] == [1, 1]
    assert subqueries[0]["top1_score"] == 0.9
    assert "errors" not in update  # has_hit enforce 零异常面（errors 通道空则零键）
    # 预览物化：跨子问题去重 ≤3 条（此形态恰 2 条）；excerpt ≤200 字
    previews = update["kb_gate_previews"]
    assert len(previews) == 2
    assert len({p["chunk_id"] for p in previews}) == 2
    assert all(len(p["excerpt"]) <= 200 for p in previews)
    assert all(set(p) == {"kb_doc_id", "chunk_id", "title", "heading_path", "excerpt", "query_index"} for p in previews)
    # 阶段 2 恢复载荷：schema_version + KB 候选并集去重（约束④）
    payload = update["kb_gate_payload"]
    assert payload["schema_version"] == 1
    assert payload["rewritten_queries"] == ["问题一", "问题二"]
    chunk_ids = [c["chunk_id"] for c in payload["kb_candidates"]]
    assert sorted(chunk_ids) == ["x", "y"]
    # 判定面留痕：第二帧 stage(rewrite,done).extra.hitl（gate=triggered）
    assert len(stages) == 1
    assert stages[0]["args"][:2] == ("rewrite", "done")
    hitl_extra = stages[0]["kw"]["extra"]["hitl"]
    assert hitl_extra["gate"] == "triggered"
    assert hitl_extra["verdict"] == "has_hit"
    assert hitl_extra["gate_ms"] >= 0


async def test_gate_preview_dedupe_and_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    """预览物化（OQ-02：3 条/200 字）：跨子问题按 chunk_id 去重、按 rerank 分全局
    排序、excerpt 服务端截断 ≤preview_chars。"""
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
    fake = _FakeRerank([[0.95], [0.85], [0.3]])  # 每子问题恰一次 rerank 调用（单 chunk）
    monkeypatch.setattr(kb_gate_module, "rerank_via_gateway", fake)
    monkeypatch.setattr(kb_gate_module, "emit_stage", lambda *a, **kw: None)

    update = await kb_gate(
        _gate_state(_hitl_spec(), ["问题一", "问题二", "问题三"]),
        _gate_config(retriever),
    )

    previews = update["kb_gate_previews"]
    assert len(previews) == 2  # chunk x（双子问题重复）+ z → 去重后 2 条
    assert [p["chunk_id"] for p in previews] == ["x", "z"]  # rerank 分降序 0.95 > 0.85
    assert all(p["excerpt"] == "长" * 200 for p in previews)  # 服务端截断 ≤200


# ---- 判定树形态二：no_hit → web 直达 ----


async def test_gate_no_hit_web_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    """地板后全空=web 直达（图不中断不落 awaiting）：action=web_only、verdict=no_hit、
    errors[] hitl_no_hit（契约 detail.subqueries 全 0 形态）、payload=None（no_hit
    轻量无载荷）、KB 候选不进 candidates（本节点零 candidates 写入）。"""
    retriever = _FakeRetriever(per_query_bm25=[[]], per_query_vec=[[]])
    monkeypatch.setattr(kb_gate_module, "rerank_via_gateway", _FakeRerank([]))
    stages: list[dict[str, Any]] = []
    monkeypatch.setattr(
        kb_gate_module, "emit_stage", lambda *a, **kw: stages.append(kw)
    )

    update = await kb_gate(
        _gate_state(_hitl_spec(), ["冷门问题"]),
        _gate_config(retriever),
    )

    assert update["kb_gate_action"] == "web_only"
    assert update["kb_gate_verdict"] == "no_hit"
    assert update["kb_gate_payload"] is None
    assert update["kb_gate_previews"] == []
    assert update["errors"] == [
        {"code": "hitl_no_hit", "detail": {"subqueries": [{"query_index": 0, "top_k_count": 0}]}}
    ]
    assert stages[0]["extra"]["hitl"]["verdict"] == "no_hit"
    assert "candidates" not in update  # KB 候选不进 candidates（plan §3.2 弃用口径）


# ---- 判定树形态三：通道全败 → 回落现状 hybrid ----


async def test_gate_channel_failed_falls_back_hybrid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任一子问题全部通道失败=「不知道」≠「没有」（plan §4.1/R28）：回落现状
    hybrid + errors[] hitl_gate_error（契约 detail {"stage":"kb_gate"}）——不判无、
    无 verdict 判定面帧。"""
    retriever = _FakeRetriever(bm25_exc=ValueError("es down"), vec_exc=ValueError("pg down"))
    monkeypatch.setattr(kb_gate_module, "rerank_via_gateway", _FakeRerank([]))
    monkeypatch.setattr(kb_gate_module, "emit_stage", lambda *a, **kw: None)  # 防御性（本分支零 emit）

    update = await kb_gate(
        _gate_state(_hitl_spec(), ["问题一"]),
        _gate_config(retriever),
    )

    assert update["kb_gate_action"] == "hybrid"
    assert update["errors"] == [{"code": "hitl_gate_error", "detail": {"stage": "kb_gate"}}]
    assert "kb_gate_verdict" not in update  # 判定未完成——无 verdict 帧


# ---- 判定树形态四：shadow → 行为一律现状 hybrid ----


async def test_gate_shadow_keeps_hybrid_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shadow（api §7.4:1094）：门照常检索落留痕、行为一律现状 hybrid——has_hit
    也不弹窗（action=hybrid）、extra.hitl.gate="shadow"、零 errors 零 awaiting。"""
    retriever = _hit_subquery_retriever()
    monkeypatch.setattr(kb_gate_module, "rerank_via_gateway", _FakeRerank([[0.9], [0.8]]))
    stages: list[dict[str, Any]] = []
    monkeypatch.setattr(
        kb_gate_module, "emit_stage", lambda *a, **kw: stages.append(kw)
    )

    update = await kb_gate(
        _gate_state(_hitl_spec(shadow=True), ["问题一", "问题二"]),
        _gate_config(retriever),
    )

    assert update["kb_gate_action"] == "hybrid"
    assert update["kb_gate_verdict"] == "has_hit"
    assert "errors" not in update
    assert stages[0]["extra"]["hitl"]["gate"] == "shadow"


async def test_gate_shadow_no_hit_zero_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shadow+no_hit：行为现状（KB 照跑）→ KB 未被跳过 → 零 hitl_no_hit 诊断
    （诊断条只属 enforce web 直达轮）。"""
    retriever = _FakeRetriever(per_query_bm25=[[]], per_query_vec=[[]])
    monkeypatch.setattr(kb_gate_module, "rerank_via_gateway", _FakeRerank([]))
    monkeypatch.setattr(kb_gate_module, "emit_stage", lambda *a, **kw: None)

    update = await kb_gate(
        _gate_state(_hitl_spec(shadow=True), ["冷门问题"]),
        _gate_config(retriever),
    )

    assert update["kb_gate_action"] == "hybrid"
    assert update["kb_gate_verdict"] == "no_hit"
    assert "errors" not in update


# ---- 门内代码异常 → 回落现状 hybrid（R38）----


async def test_gate_internal_error_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """R38：门内代码异常（rerank 失败等）→ 回落现状 hybrid + hitl_gate_error——
    异常与空结果类型上分开，绝不落「空列表」分支解释为判无。"""
    retriever = _hit_subquery_retriever()
    monkeypatch.setattr(kb_gate_module, "rerank_via_gateway", _FailingRerank())
    monkeypatch.setattr(kb_gate_module, "emit_stage", lambda *a, **kw: None)

    update = await kb_gate(
        _gate_state(_hitl_spec(), ["问题一"]),
        _gate_config(retriever),
    )
    assert update["kb_gate_action"] == "hybrid"
    assert update["errors"] == [{"code": "hitl_gate_error", "detail": {"stage": "kb_gate"}}]


class _FailingRerank:
    async def __call__(self, query: str, documents: list[str], *, model_alias: str, timeout_s: float) -> list[float | None]:
        raise RuntimeError("rerank 网关 500")


# ---- 门关闭防御 + 取消感知 + 并发帽 ----


async def test_gate_disabled_defensive_hybrid(monkeypatch: pytest.MonkeyPatch) -> None:
    """门前置竞态防御：hitl 关闭时门节点被意外到达（spec 快照漂移）→ 现状 hybrid
    直通、零检索零 rerank（G3 缺省态纵深）。"""
    retriever = _hit_subquery_retriever()
    spec = _hitl_spec()
    spec["hitl"]["enabled"] = False
    monkeypatch.setattr(kb_gate_module, "rerank_via_gateway", _FakeRerank([]))

    update = await kb_gate(_gate_state(spec, ["问题一"]), _gate_config(retriever))

    assert update == {"kb_gate_action": "hybrid"}
    assert retriever.bm25_calls == 0 and retriever.vec_calls == 0


async def test_gate_cancel_aware_at_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """取消感知（R14② 同款）：门在阶段 1 内有协程——入口 ensure_not_canceled
    抛 TurnCanceledSignal（plan §9 门段途中取消走既有节点边界感知）。"""
    retriever = _hit_subquery_retriever()
    config = _gate_config(retriever)
    config["configurable"]["cancel_flags"] = {"t1": "user"}
    config["configurable"]["turn_id"] = "t1"
    monkeypatch.setattr(kb_gate_module, "rerank_via_gateway", _FakeRerank([]))

    with pytest.raises(TurnCanceledSignal):
        await kb_gate(_gate_state(_hitl_spec(), ["问题一"]), config)


def test_gate_concurrency_limit_aligned_with_fanout() -> None:
    """并发帽（约束⑤）：门内逐子问题并发上限对齐 kb_search Send×N 量级
    （type_query_map 夹取上限 4）。"""
    assert GATE_CONCURRENCY_LIMIT == 4


# ---- 路由器 ----


def test_route_after_gate_three_branches() -> None:
    """kb_gate 出条件边三叉（plan §3.2）：await→approval_wait；web_only→web Send×N；
    hybrid→现状 web+kb 双 Send（web 段在前供给序）。"""
    state = _gate_state(_hitl_spec(), ["q1", "q2"], kb_gate_action="await")
    assert route_after_gate(state) == "approval_wait"

    state["kb_gate_action"] = "web_only"
    sends = route_after_gate(state)
    assert [s.node for s in sends] == ["web_search", "web_search"]
    assert [s.arg["search_query_index"] for s in sends] == [0, 1]

    state["kb_gate_action"] = "hybrid"
    sends = route_after_gate(state)
    assert [s.node for s in sends] == ["web_search", "web_search", "kb_search", "kb_search"]


def test_entry_router_stage_two_branches() -> None:
    """START 条件入口两阶段恢复（plan §3.4/ARCH :484）：无 approval_choice=阶段 1
    走 rewrite；kb_only=直达 merge_dedupe（零 kb Send——KB 候选已随初始 state 注入，
    约束④）；web_only=web Send×N；both/custom=现状 web+kb 双 Send。"""
    assert route_by_approval({"approval_choice": None}) == "rewrite"
    assert route_by_approval({}) == "rewrite"

    state = _gate_state(_hitl_spec(), ["q1", "q2"], approval_choice="kb_only")
    assert route_by_approval(state) == "merge_dedupe"

    state["approval_choice"] = "web_only"
    sends = route_by_approval(state)
    assert [s.node for s in sends] == ["web_search", "web_search"]

    for choice in ("both", "custom"):
        state["approval_choice"] = choice
        sends = route_by_approval(state)
        assert [s.node for s in sends] == ["web_search", "web_search", "kb_search", "kb_search"]


# ---- 扇出条件边门分支（G3：关闭=现状逐字节）----


def test_hitl_gate_armed_conditions() -> None:
    """门前置（plan §4.1 第一级零 IO 判定）：enabled∧首趟〔correction_round=0〕∧
    query_type ∉ skip_query_types∧KB 可用 → 进门；任一不满足=现状路径。"""
    spec = _hitl_spec()
    base = _gate_state(spec, ["q1"])
    assert hitl_gate_armed(base, spec) is True

    # 跳过题型（缺省 navigational/temporal 不进门——hitl-OQ-15「不进门≠web 直达」）
    assert hitl_gate_armed(_gate_state(spec, ["q1"], query_type="navigational"), spec) is False
    # 纠错第二趟不进门（kb_route.py:99-100 同源）
    assert hitl_gate_armed(_gate_state(spec, ["q1"], correction_round=1), spec) is False
    # KB 双通道全关不进门（kb_available=false——「不可用」与「零命中」两件事）
    spec_off = _hitl_spec()
    spec_off["kb"]["es_enabled"] = False
    spec_off["kb"]["embedding_enabled"] = False
    assert hitl_gate_armed(_gate_state(spec_off, ["q1"]), spec_off) is False
    # hitl 关闭（缺省灰度态）
    spec_closed = _hitl_spec()
    spec_closed["hitl"]["enabled"] = False
    assert hitl_gate_armed(_gate_state(spec_closed, ["q1"]), spec_closed) is False
    # hitl 节缺失（存量 spec 形态）
    assert hitl_gate_armed(_gate_state({"kb": spec["kb"]}, ["q1"]), {"kb": spec["kb"]}) is False


def test_search_fanout_g3_byte_equal_with_gate_closed() -> None:
    """G3 灰度纪律（逐字节等价）：hitl.enabled=false 与 hitl 节缺失两种形态下，
    search_fanout 返回与无 hitl 键的现状**逐一相等**的 Send 列表（节点名+入参
    全等）；enabled=true 时返回 "kb_gate" 分支。"""
    spec_with_kb = {"kb": {"es_enabled": False, "embedding_enabled": False}}
    state_no_hitl = _gate_state(spec_with_kb, ["q1", "q2"])

    def _wire(sends: Any) -> list[tuple[str, dict[str, Any]]]:
        # Send 管道面比对剔除 spec 内嵌（spec 快照随 Send 投影——hitl 节在场与否属
        # 快照内容差异，非行为差异；G3 等价面=节点序+业务入参）
        return [
            (s.node, {k: v for k, v in dict(s.arg).items() if k != "spec"}) for s in sends
        ]

    spec_off = {"kb": spec_with_kb["kb"], "hitl": _hitl_spec()["hitl"] | {"enabled": False}}
    baseline = search_fanout(state_no_hitl)
    gated_off = search_fanout(_gate_state(spec_off, ["q1", "q2"]))
    assert _wire(gated_off) == _wire(baseline)

    spec_missing = {"kb": spec_with_kb["kb"]}
    gated_missing = search_fanout(_gate_state(spec_missing, ["q1", "q2"]))
    assert _wire(gated_missing) == _wire(baseline)

    # enabled=true（KB 可用态）→ 门分支
    spec_on = _hitl_spec()
    assert search_fanout(_gate_state(spec_on, ["q1", "q2"])) == "kb_gate"


# ---- 拓扑接线（子图内扩展主图零改动）----


def test_fast_graph_topology_includes_gate_nodes() -> None:
    """拓扑增量（plan §3.2）：kb_gate/approval_wait 节点在图；kb_gate 出边可达
    approval_wait/web_search/kb_search；approval_wait→END；并发帽常量对齐。"""
    graph = build_fast_graph()
    nodes = set(graph.get_graph().nodes)
    assert {"kb_gate", "approval_wait"} <= nodes

    edge_pairs = {(edge.source, edge.target) for edge in graph.get_graph().edges}
    assert ("approval_wait", "__end__") in edge_pairs
    # kb_gate 条件边分支（langgraph 以 branch 伪节点呈现 source→branch→target）
    kb_gate_targets = {target for source, target in edge_pairs if source == "kb_gate"}
    assert {"approval_wait", "web_search", "kb_search"} <= kb_gate_targets
    # 条件入口分支（START→branch→rewrite 默认臂）
    rewrite_reachable = any(
        source == "__start__" and target == "rewrite" for source, target in edge_pairs
    )
    assert rewrite_reachable
    assert GATE_CONCURRENCY_LIMIT == 4


def test_approval_wait_is_zero_op() -> None:
    """approval_wait 空操作终结节点（plan §3.2）：零 state 更新、图自然 END。"""
    assert approval_wait({}, None) == {}
