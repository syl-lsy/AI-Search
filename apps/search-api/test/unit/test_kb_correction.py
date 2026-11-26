"""unit 层：fast 有界纠错轮（plan-m3-b19 §4.4 / §7 T2 test_kb_correction + 修订注 4）。

钉住的语义：
- fuse_rerank 出条件边 `route_after_fuse`——确定性 gate 判不足 → Send("rewrite")
  回边恰一次（轮帽 1：correction_round ≥1 后不再回边直达 synthesis）；判足/灰度关闭
  → "synthesis"（现状路径）；
- 信号 gate 三阈值触发判定（零命中 / top1 RRF 分 < min_top1_score=0.012〔修订注 4〕/
  KB 来源数 < min_kb_sources）——纯函数零 LLM（判据面详见 test_kb_route.py）；
- 回边 payload 携带 correction_round=round+1（轮帽持久化载体——rewrite update 落
  state）；
- 反馈注入 rewrite（重写 prompt 含 KB 不足反馈块）；纠错第二趟强制 hybrid
  （update.kb_route=hybrid）+ 入口补发 running 帧（首趟 running 由 TurnRunner 发布，
  纠错趟自发布保持 running/done 配对——api.md §7.2「同 key 多次出现合法」）+
  done extra correction:true；
- SSE 零面变化：纠错轮=既有 stage 事件重复发射（零新事件类型零新 stage_key）。
"""

from typing import Any

import pytest

from app.agent.graphs.fast import build_fast_graph, route_after_fuse
from app.agent.graphs import fast as fast_module
from app.agent.nodes import fast_rewrite as rewrite_module
from app.agent.nodes.fast_rewrite import rewrite
from app.agent.tools.registry import SearchChainUnavailable

from langgraph.types import Send


def _spec(**route: Any) -> dict[str, Any]:
    return {
        "kb": {
            "es_enabled": True,
            "embedding_enabled": False,
            "route": {
                "default": "hybrid",
                "correction_enabled": True,
                "min_kb_sources": 2,
                "min_top1_score": 0.012,
                **route,
            },
        }
    }


def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {"spec": _spec(), "kb_route": "kb_only", "fused": []}
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# route_after_fuse：回边恰一次（轮帽）+ 灰度直达
# ---------------------------------------------------------------------------


def test_edge_returns_send_rewrite_with_round_increment():
    """① gate 不足 → Send("rewrite") 回边，payload 携带 correction_round=round+1
    （轮帽持久化载体）。"""
    result = route_after_fuse(_state())
    assert isinstance(result, Send)
    assert result.node == "rewrite"
    assert result.arg["correction_round"] == 1
    assert result.arg["kb_route"] == "kb_only"  # state 全量透传（rewrite 读路由反馈）


def test_edge_cap_second_insufficient_goes_synthesis():
    """② 轮帽：correction_round ≥1 后 gate 仍不足 → 直达 synthesis（不再回边）。"""
    result = route_after_fuse(_state(correction_round=1))
    assert result == "synthesis"


def test_edge_sufficient_goes_synthesis():
    """判足（KB 命中 + top1 ≥ 阈值 + 来源数足）→ 直达 synthesis。"""
    fused = [
        {"origin": "kb", "score": 1 / 61, "chunk_id": "c1"},
        {"origin": "kb", "score": 1 / 61 + 0.001, "chunk_id": "c2"},
    ]
    result = route_after_fuse(_state(fused=fused))
    assert result == "synthesis"


def test_edge_gray_off_goes_synthesis():
    """⑤ correction_enabled=false（灰度缺省）→ 不足直达 synthesis（现状路径）。"""
    spec = _spec()
    spec["kb"]["route"]["correction_enabled"] = False
    result = route_after_fuse({"spec": spec, "kb_route": "kb_only", "fused": []})
    assert result == "synthesis"


def test_edge_hybrid_route_never_corrects():
    """hybrid 路由零纠错（纠错只属 kb_first/kb_only）。"""
    result = route_after_fuse(_state(kb_route="hybrid"))
    assert result == "synthesis"


# ---------------------------------------------------------------------------
# rewrite 纠错第二趟：反馈注入 / 强制 hybrid / running 帧 / extra correction
# ---------------------------------------------------------------------------


class _FakeChain:
    """结构化输出假链（返回真 RewriteOutput 实例——pydantic 校验同形）。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.prompts: list[str] = []

    def with_structured_output(self, *_: Any, **__: Any) -> "_FakeChain":
        return self

    async def ainvoke(self, prompt: str, config: Any = None) -> Any:
        self.prompts.append(prompt)
        return rewrite_module.RewriteOutput.model_validate(self.payload)


def _stages(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(
        rewrite_module, "emit_stage",
        lambda key, status, **kw: emitted.append({"stage_key": key, "status": status, **kw}),
    )
    return emitted


async def test_rewrite_correction_pass_feedback_and_forced_hybrid(
    monkeypatch: pytest.MonkeyPatch,
):
    """③ 纠错第二趟：prompt 含 KB 不足反馈块；update 强制 hybrid + 轮帽持久化；
    入口补发 running 帧 + done extra correction=true（SSE 零面——重复发射合法）。"""
    chain = _FakeChain({
        "queries": ["q1"], "query_type": "factoid", "answerable_from_knowledge": False,
        "search_strategy": "web", "kb_route": "kb_only", "safety": "none",
        "time_range": None, "memory_directive": None,
    })
    monkeypatch.setattr(rewrite_module, "build_light_model", lambda *_: chain)
    stages = _stages(monkeypatch)
    update = await rewrite({"query": "测试问题", "query_lang": "zh", "spec": {}, "correction_round": 1})
    # 反馈注入（plan §4.4「带反馈重写」）
    assert chain.prompts and "纠错重写提示" in chain.prompts[0]
    # 强制 hybrid + 轮帽持久化（update 落 state——第二次 gate 不再回边的承载）
    assert update["kb_route"] == "hybrid"
    assert update["correction_round"] == 2
    # running/done 配对（纠错趟自发布 running）
    keys = [(s["stage_key"], s["status"]) for s in stages]
    assert ("rewrite", "running") in keys
    done = next(s for s in stages if s["status"] == "done")
    assert done["extra"]["correction"] is True
    assert "kb_route" not in done["extra"]  # hybrid=缺省值——键可省（api.md v1.33 判定行）


async def test_rewrite_first_pass_kb_only_extra_kb_route(monkeypatch: pytest.MonkeyPatch):
    """首趟 kb_only 合法输出 → done extra 携带 kb_route=kb_only（非缺省值才带键）；
    零纠错帧（running 由 TurnRunner 发布）。"""
    chain = _FakeChain({
        "queries": ["q1"], "query_type": "factoid", "answerable_from_knowledge": False,
        "search_strategy": "web", "kb_route": "kb_only", "safety": "none",
        "time_range": None, "memory_directive": None,
    })
    monkeypatch.setattr(rewrite_module, "build_light_model", lambda *_: chain)
    stages = _stages(monkeypatch)
    update = await rewrite({"query": "测试问题", "query_lang": "zh", "spec": {}})
    assert update["kb_route"] == "kb_only"
    assert "correction_round" not in update
    keys = [(s["stage_key"], s["status"]) for s in stages]
    assert ("rewrite", "running") not in keys  # 首趟零自发布 running
    done = next(s for s in stages if s["status"] == "done")
    assert done["extra"]["kb_route"] == "kb_only"
    assert "correction" not in done["extra"]


async def test_rewrite_fallback_paths_hybrid_and_keyless(monkeypatch: pytest.MonkeyPatch):
    """两兜底路径 kb_route=hybrid（plan §4.4）且 extra 零 kb_route 键（缺省键可省）。"""
    from langchain_core.exceptions import OutputParserException

    class _BoomChain:
        def with_structured_output(self, *_, **__):
            return self

        async def ainvoke(self, prompt: str, config: Any = None) -> Any:
            raise OutputParserException("bad output")

    monkeypatch.setattr(rewrite_module, "build_light_model", lambda *_: _BoomChain())
    stages = _stages(monkeypatch)
    update = await rewrite({"query": "测试问题", "query_lang": "zh", "spec": {}})
    assert update["kb_route"] == "hybrid"
    done = next(s for s in stages if s["status"] == "done")
    assert "kb_route" not in done["extra"]  # hybrid=缺省——键可省（存量轮零键）


# ---------------------------------------------------------------------------
# 图结构（子图内条件边——ADR-012 拓扑边界）
# ---------------------------------------------------------------------------


def test_fast_graph_structure_kb_search_and_correction_edge():
    """④ stage 序列承载面：kb_search 节点在图（WS→MG←KS 汇聚）；fuse_rerank 出
    条件边目标含 synthesis/rewrite（纠错回边）；节点集零新增 stage_key 面。"""
    graph = build_fast_graph()
    nodes = set(graph.get_graph().nodes)
    assert {"rewrite", "web_search", "kb_search", "merge_dedupe", "fuse_rerank", "synthesis"} <= nodes
    edge_pairs = {(edge.source, edge.target) for edge in graph.get_graph().edges}
    assert ("kb_search", "merge_dedupe") in edge_pairs
    # 条件边分支（langgraph 以 branch 伪节点呈现 source→branch→target）
    conditional_targets = {
        target
        for (source, target) in edge_pairs
        if source == "fuse_rerank"
    }
    assert {"synthesis", "rewrite"} <= conditional_targets


# ---------------------------------------------------------------------------
# 图级短路谓词（merge_dedupe——KB 通道语义性零候选不得撞 500201）
# ---------------------------------------------------------------------------


def _patch_graph_nodes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    kb_update: dict[str, Any],
    web_update: dict[str, Any],
) -> dict[str, int]:
    """图级节点替换（rewrite/kb_search/web_search/fuse_rerank/synthesis 假体；
    merge_dedupe 与条件边保持真实现——被测面）。"""
    calls = {"rewrite": 0, "web": 0, "kb": 0, "synthesis": 0}

    async def fake_rewrite(state: Any, config: Any = None) -> dict[str, Any]:
        calls["rewrite"] += 1
        return {
            "rewritten_queries": ["q1"],
            "query_type": "other",
            "search_strategy": "web",
            "kb_route": state.get("kb_route") or "kb_only",
            "correction_round": state.get("correction_round", 0),
        }

    async def fake_kb(state: Any, config: Any = None) -> dict[str, Any]:
        calls["kb"] += 1
        return kb_update

    async def fake_web(state: Any, config: Any = None) -> dict[str, Any]:
        calls["web"] += 1
        return web_update

    async def fake_fuse(state: Any, config: Any = None) -> dict[str, Any]:
        return {"fused": state.get("fused") or []}

    async def fake_synthesis(state: Any, config: Any = None) -> dict[str, Any]:
        calls["synthesis"] += 1
        return {"answer_md": "ok"}

    monkeypatch.setattr(fast_module, "rewrite", fake_rewrite)
    monkeypatch.setattr(fast_module, "kb_search", fake_kb)
    monkeypatch.setattr(fast_module, "web_search", fake_web)
    monkeypatch.setattr(fast_module, "fuse_rerank", fake_fuse)
    monkeypatch.setattr(fast_module, "synthesis", fake_synthesis)
    return calls


def _graph_state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "query": "测试问题",
        "query_lang": "zh",
        "user_id": "00000000-0000-4000-8000-000000000001",
        "spec": _spec(),
        "kb_route": "kb_only",
        "rewritten_queries": ["q1"],
        "query_type": "other",
        "search_strategy": "web",
    }
    state.update(overrides)
    return state


def _web_candidate() -> dict[str, Any]:
    return {
        "url": "https://example.com/a",
        "title": "网络结果",
        "snippet": "网络摘要",
        "score": 1.0,
        "origin": "web",
    }


async def test_graph_kb_only_corpus_empty_falls_back_to_web_not_500201(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kb_only + 语料空（kb_corpus_empty）：merge_dedupe 不抛 SearchChainUnavailable
    ——放行到纠错回边补 web（用户显式「只查我的文档」零候选时的兜底语义）；纠错
    第二趟=hybrid（web+kb 并行）→ synthesis。"""
    calls = _patch_graph_nodes(
        monkeypatch,
        kb_update={
            "candidates": [],
            "errors": [{"stage": "search", "reason": "kb_corpus_empty"}],
        },
        web_update={"candidates": [_web_candidate()], "errors": []},
    )
    graph = fast_module.build_fast_graph()
    result = await graph.ainvoke(_graph_state())

    assert calls["rewrite"] == 2  # 首趟 + 纠错第二趟
    assert calls["web"] == 1  # 纠错第二趟 hybrid 补 web
    assert calls["synthesis"] == 1
    assert [c["url"] for c in result["fused"]] == ["https://example.com/a"]
    # errors 既有形态保留（留痕不吞）
    assert any(e.get("reason") == "kb_corpus_empty" for e in result.get("errors", []))


async def test_graph_kb_first_channel_failure_falls_back_to_web(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kb_first + 通道失败（kb_es_unavailable/kb_vector_unavailable）：同属配置/
    通道态非「源调用失败」——不短路 500201，走纠错回边补 web。"""
    calls = _patch_graph_nodes(
        monkeypatch,
        kb_update={
            "candidates": [],
            "errors": [
                {"stage": "search", "reason": "kb_es_unavailable"},
                {"stage": "search", "reason": "kb_vector_unavailable"},
            ],
        },
        web_update={"candidates": [_web_candidate()], "errors": []},
    )
    graph = fast_module.build_fast_graph()
    result = await graph.ainvoke(_graph_state(kb_route="kb_first"))

    assert calls["rewrite"] == 2
    assert calls["web"] == 1
    assert result["fused"]


async def test_graph_hybrid_real_source_failure_still_short_circuits_500201(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hybrid + 真源失败（source_failure）：既有 500201 短路语义保持（回归锁——
    短路谓词豁免只属 KB 通道语义性条目，真源失败照抛）。"""
    _patch_graph_nodes(
        monkeypatch,
        kb_update={
            "candidates": [],
            "errors": [{"stage": "search", "reason": "kb_corpus_empty"}],
        },
        web_update={
            "candidates": [],
            "errors": [
                {"stage": "search", "reason": "source_failure", "source": "searxng"}
            ],
        },
    )
    graph = fast_module.build_fast_graph()
    with pytest.raises(SearchChainUnavailable):
        await graph.ainvoke(_graph_state(kb_route="hybrid"))


async def test_graph_all_zero_candidates_kb_semantic_errors_go_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两趟全零候选、errors 仅 KB 语义条目（含 kb_only_fallback_web 路由留痕）→
    不抛 SearchChainUnavailable、直达 synthesis 零源路径（B19 前同情形语义：
    零候选零错放行）。kb_only_fallback_web 随 Send("rewrite", dict(state)) 带入
    纠错第二趟——漏豁免会误判「全部搜索源失败」500201。"""
    calls = _patch_graph_nodes(
        monkeypatch,
        kb_update={
            "candidates": [],
            "errors": [{"stage": "search", "reason": "kb_corpus_empty"}],
        },
        web_update={"candidates": [], "errors": []},  # web 正常返回但零候选零错
    )

    async def fake_fuse(state: Any, config: Any = None) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        if state.get("kb_route") == "kb_only":  # 首趟：真实 fuse_rerank 的兜底留痕
            errors.append({"stage": "search", "reason": "kb_only_fallback_web"})
        update: dict[str, Any] = {"fused": list(state.get("fused") or [])}
        if errors:
            update["errors"] = errors
        return update

    monkeypatch.setattr(fast_module, "fuse_rerank", fake_fuse)
    graph = fast_module.build_fast_graph()
    result = await graph.ainvoke(_graph_state())

    assert calls["rewrite"] == 2  # 首趟 + 纠错第二趟
    assert calls["web"] == 1  # 第二趟强制 hybrid（web 零候选零错）
    assert calls["synthesis"] == 1  # 零源路径直达 synthesis，不抛 500201
    assert result["fused"] == []
    reasons = [e.get("reason") for e in result.get("errors", [])]
    assert "kb_only_fallback_web" in reasons  # 留痕不吞
    assert "kb_corpus_empty" in reasons
