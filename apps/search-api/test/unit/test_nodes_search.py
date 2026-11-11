"""unit 层：web_search（Send 扇出副本）与 merge_dedupe（纯函数，例外批次归并节点）。

钉住的语义（冻结契约，逐条对应测试）：
- Send 扇出：rewrite 出边按 rewritten_queries 产出 Send("web_search", payload)×N（payload
  携带 copy 的 query/query_index/query_total——额外键经 Send 输入投影进节点、不回写 state）；
- 语言门控在链解析面完成（registry.resolve_search_chain），节点按冻结链逐源调用：
  zh→[tavily, bocha] 双源并行（asyncio.gather）、非 zh→[tavily]；time_range 仅透传 Tavily
  （ARCH §4.1「time_range 透传 Tavily」）；
- stage(search) 事件（api.md §7.2/§7.4）：index 0 副本发 running（detail=链摘要，单对主干），
  每副本发 done（progress={current: idx+1, total} + detail 源摘要）——同 key 多次出现合法；
- 单源失败互兜底：副本内单源异常 → 其余源结果保留 + degraded=True + errors 记录
  （ARCH §4.1 失败语义「不终止」）；全部副本零候选且 errors 非空 → merge_dedupe 抛
  SearchChainUnavailable → TurnRunner 短路 500201（synthesis 前短路——A1 情形 3，事件由
  TurnRunner 出、图不发 error）；
- merge_dedupe：URL 归并去重（保首见序）、origin 合并为列表保留 KB 标记、不失败语义
  （ARCH §4.1「纯函数，不失败」）；sources 事件 §7.2 形态（ref_no 首现冻结 1..k、
  source_id 占位、source_type=web、snapshot_available=false）；
- state 写入与 FastState 一致；candidates 经 add reducer 跨副本合并。
"""

import asyncio
from typing import Any

import pytest

from app.agent.nodes import merge_dedupe as merge_module
from app.agent.nodes import web_search as ws_module
from app.agent.nodes.merge_dedupe import merge_dedupe
from app.agent.nodes.web_search import web_search
from app.agent.tools.registry import SearchChain, SearchChainUnavailable


class _FakeRegistry:
    """registry 假体：search 按源返回预设结果/异常；记录全部调用（含 time_range）。"""

    def __init__(self, results: dict[str, Any]) -> None:
        self._results = results
        self.calls: list[tuple[str, str, str | None]] = []

    async def search(
        self, query: str, source: str, *, time_range: str | None = None, top_k: int = 5
    ) -> list[dict[str, Any]]:
        self.calls.append((query, source, time_range))
        item = self._results.get(source)
        if isinstance(item, Exception):
            raise item
        return list(item)

    async def aclose(self) -> None:  # pragma: no cover —— 生命周期面，测试不触
        return None


def _chain(sources: list[str], *, degraded: bool = False, detail: str = "") -> SearchChain:
    return SearchChain(
        sources=list(sources),
        backup=None,
        degraded=degraded,
        detail=detail or (" / ".join(sources) or "搜索链不可用"),
    )


def _ws_config(registry: _FakeRegistry, chain: SearchChain) -> dict[str, Any]:
    return {"configurable": {"tools_registry": registry, "search_chain": chain}}


def _payload(
    query: str,
    *,
    index: int = 0,
    total: int = 1,
    time_range: str | None = None,
    query_lang: str = "zh",
) -> dict[str, Any]:
    return {
        "query": query,
        "query_lang": query_lang,
        "time_range": time_range,
        "search_query_index": index,
        "search_query_total": total,
    }


def _cand(url: str, *, origin: str = "tavily", score: float = 0.9) -> dict[str, Any]:
    return {"url": url, "title": f"t:{url}", "snippet": "s", "score": score, "origin": origin}


def _stages(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[dict[str, Any]]:
    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(
        module,
        "emit_stage",
        lambda key, status, **kw: emitted.append({"stage_key": key, "status": status, **kw}),
    )
    return emitted


# ---- web_search：扇出副本 ----


async def test_web_search_zh_calls_both_sources_with_time_range_to_tavily_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """zh 副本：tavily+bocha 并行；time_range 仅透传 tavily；候选双源合并。"""
    registry = _FakeRegistry({
        "tavily": [_cand("https://a/1", origin="tavily")],
        "bocha": [_cand("https://b/1", origin="bocha")],
    })
    stages = _stages(monkeypatch, ws_module)

    update = await web_search(
        _payload("q1", index=0, total=2, time_range="month"),
        _ws_config(registry, _chain(["tavily", "bocha"])),
    )

    assert registry.calls == [("q1", "tavily", "month"), ("q1", "bocha", None)]  # 仅 tavily 透传
    assert {c["origin"] for c in update["candidates"]} == {"tavily", "bocha"}
    assert update["degraded"] is False and update["errors"] == []
    assert [(s["status"], s.get("progress")) for s in stages] == [
        ("running", None),
        ("done", {"current": 1, "total": 2}),
    ]


async def test_web_search_en_never_calls_bocha(monkeypatch: pytest.MonkeyPatch) -> None:
    """非中文副本按冻结链仅 tavily（链解析已语言门控——节点不越链）。"""
    registry = _FakeRegistry({"tavily": [_cand("https://a/1")]})
    _stages(monkeypatch, ws_module)

    await web_search(
        _payload("english query", query_lang="en"),
        _ws_config(registry, _chain(["tavily"])),
    )

    assert [source for _, source, _ in registry.calls] == ["tavily"]


async def test_web_search_single_source_failure_degrades_not_terminates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单源异常 → 其余源候选保留 + degraded=True + errors 记录（不终止）；done.detail 标注降级。"""
    registry = _FakeRegistry({
        "tavily": [_cand("https://a/1")],
        "bocha": RuntimeError("bocha 挂了"),
    })
    stages = _stages(monkeypatch, ws_module)

    update = await web_search(
        _payload("q1", index=0, total=1),
        _ws_config(registry, _chain(["tavily", "bocha"])),
    )

    assert [c["origin"] for c in update["candidates"]] == ["tavily"]
    assert update["degraded"] is True
    assert update["errors"] == [
        {"stage": "search", "reason": "source_failure", "source": "bocha"}
    ]
    done = stages[-1]
    assert done["status"] == "done" and "bocha" in done["detail"]


async def test_web_search_empty_chain_raises_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """冻结链为空（主位+备位全断）→ SearchChainUnavailable——TurnRunner 短路 500201。"""
    registry = _FakeRegistry({})
    _stages(monkeypatch, ws_module)

    with pytest.raises(SearchChainUnavailable):
        await web_search(_payload("q1"), _ws_config(registry, _chain([])))


async def test_web_search_index0_only_emits_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """N 副本仅 index 0 发 running（单对主干）；每副本发 done 带 progress。"""
    stages: list[dict[str, Any]] = []
    for idx in (0, 1):
        registry = _FakeRegistry({"tavily": [_cand(f"https://a/{idx}")]})
        monkeypatch.setattr(
            ws_module,
            "emit_stage",
            lambda key, status, **kw: stages.append({"stage_key": key, "status": status, **kw}),
        )
        await web_search(
            _payload(f"q{idx}", index=idx, total=2),
            _ws_config(registry, _chain(["tavily"])),
        )

    running = [s for s in stages if s["status"] == "running"]
    dones = [s for s in stages if s["status"] == "done"]
    assert len(running) == 1  # index 0 才发 running
    assert [(d["progress"], d["status"]) for d in dones] == [
        ({"current": 1, "total": 2}, "done"),
        ({"current": 2, "total": 2}, "done"),
    ]


async def test_web_search_writes_only_faststate_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """副本 state 写入 ⊆ FastState 声明（candidates/errors/degraded）。"""
    registry = _FakeRegistry({"tavily": [_cand("https://a/1")]})
    monkeypatch.setattr(ws_module, "emit_stage", lambda *a, **kw: None)

    update = await web_search(_payload("q1"), _ws_config(registry, _chain(["tavily"])))

    from app.agent.state import FastState

    assert set(update) <= set(FastState.__annotations__)


# ---- merge_dedupe：纯函数归并节点（例外批次：与 web_search 同批测试，DoD 独立取证）----


def test_merge_dedupe_dedupes_by_url_and_merges_origin() -> None:
    """URL 归并：同 URL 保首见项 + origin 合并为列表（保序去重）；不同 URL 各自成条。"""
    merged = merge_module.merge_candidates([
        _cand("https://a/1", origin="tavily", score=0.9),
        _cand("https://b/1", origin="bocha", score=0.8),
        _cand("https://a/1", origin="bocha", score=0.7),  # 同 URL：origin 归并
    ])
    assert [c["url"] for c in merged] == ["https://a/1", "https://b/1"]
    assert merged[0]["origin"] == ["tavily", "bocha"]  # 合并为列表（保序去重）
    assert merged[1]["origin"] == ["bocha"]
    assert merged[0]["score"] == 0.9  # 首见项字段保留


def test_merge_dedupe_preserves_kb_marker() -> None:
    """origin 合并保留 KB 标记（§4.1「origin 合并保留 KB 标记」）。"""
    merged = merge_module.merge_candidates([
        _cand("https://kb/1", origin="kb"),
        _cand("https://kb/1", origin="tavily"),
    ])
    assert merged[0]["origin"] == ["kb", "tavily"]


def test_merge_dedupe_never_fails_on_garbage() -> None:
    """不失败语义：畸形输入（None/缺 url/非 dict）归一为空集，绝不抛。"""
    assert merge_module.merge_candidates([]) == []
    assert merge_module.merge_candidates([None, {"title": "no-url"}, "junk", 42]) == []


async def test_merge_dedupe_short_circuits_when_all_sources_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全部副本零候选且 errors 非空 → SearchChainUnavailable（synthesis 前短路 500201）。"""
    with pytest.raises(SearchChainUnavailable):
        merge_dedupe(
            {"candidates": [], "errors": [{"stage": "search", "reason": "source_failure", "source": "tavily"}]},
            {},
        )


async def test_merge_dedupe_partial_failure_keeps_remaining_candidates() -> None:
    """部分副本失败（其余有候选）→ 短路不触发，候选照常归并（sources 发射点在 fuse_rerank）。"""
    update = merge_dedupe(
        {
            "candidates": merge_module.merge_candidates([_cand("https://only/1", origin="bocha")]),
            "errors": [{"stage": "search", "reason": "source_failure", "source": "tavily"}],
        },
        {},
    )

    assert update["fused"][0]["url"] == "https://only/1"


def test_merge_dedupe_writes_only_faststate_keys() -> None:
    """state 写入 ⊆ FastState 声明（fused）。"""
    update = merge_dedupe({"candidates": [], "errors": []}, {})
    from app.agent.state import FastState

    assert set(update) <= set(FastState.__annotations__)


# ---- 扇出函数：rewrite 出边 ----


async def test_fanout_returns_send_per_query() -> None:
    """search_fanout：按 rewritten_queries 产 Send×N（payload 携带 copy query/index/total）。"""
    from langgraph.types import Send

    sends = ws_module.search_fanout(
        {"rewritten_queries": ["q1", "q2", "q3"], "query_lang": "zh"}
    )
    assert [type(s) for s in sends] == [Send, Send, Send]
    assert [s.node for s in sends] == ["web_search", "web_search", "web_search"]
    assert [s.arg["query"] for s in sends] == ["q1", "q2", "q3"]
    assert [s.arg["search_query_index"] for s in sends] == [0, 1, 2]
    assert {s.arg["search_query_total"] for s in sends} == {3}


async def test_fanout_empty_queries_returns_default_edge() -> None:
    """空 queries（异常兜底形态）→ 回落默认边 'merge_dedupe'（扇出零副本时不悬挂）。"""
    result = ws_module.search_fanout({"rewritten_queries": []})
    assert result == "merge_dedupe"
