"""unit 层：B2 桩主图（agent/graphs/{main,registry,fast,deep}.py）路由与事件序列。

钉住的语义（冻结契约，逐条对应测试）：
- 路由四条（ARCH §4.0 / ADR-012 / DEV B3 第 0 步口径，B2 桩先落）：
  fast→fast / deep→deep / mode 缺失→default(fast) / 指向未注册图→default+WARNING；
- 桩图事件序列与 api.md §7.4 主干一致：快速模式
  stage(rewrite,r→d) → stage(search,r→d) → sources(batch) → stage(write,running,label=综合中)
  → token…token → stage(write,done)——done/images 由 TurnRunner/图外承担，图不发 done；
  深度模式（701-708 行）：stage(plan,r→d) → stage(write,running) → token…token →
  stage(write,done)（search/read/reflect 为 B4 deepagents 实装产物，桩不含）；
- 信号承载 = custom 流（langgraph 1.x get_stream_writer，subgraphs=True 下冒泡），
  TurnRunner 统一 astream 翻译为 SSE 事件（ARCH §6.1）；
- search 节点可控 delay 两途径：RunnableConfig configurable["delay"] 优先，
  缺省读模块常量 fast.SEARCH_DELAY_S（默认 0.0，测试可 monkeypatch）。

路由不读 Nacos spec.routing、不接真模型——B3/B4 替换，桩期 mode==图名直查。
"""

import time
from typing import Any

import pytest

from app.agent.graphs import fast as fast_module
from app.agent.graphs.deep import (
    STUB_DEEP_ANSWER_MD,
    STUB_DEEP_TOKEN_DELTAS,
)
from app.agent.graphs.fast import (
    STUB_FAST_ANSWER_MD,
    STUB_FAST_SOURCES,
    STUB_FAST_TOKEN_DELTAS,
)
from app.agent.graphs.main import DEFAULT_GRAPH, graph as main_graph
from app.agent.graphs.registry import GRAPH_REGISTRY


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


def _fast_expected() -> list[dict[str, Any]]:
    """api.md §7.4 快速模式主干（710 行）的桩展开：token 在 write running 之后。"""
    return [
        {"type": "stage", "stage_key": "rewrite", "status": "running"},
        {"type": "stage", "stage_key": "rewrite", "status": "done"},
        {"type": "stage", "stage_key": "search", "status": "running"},
        {"type": "stage", "stage_key": "search", "status": "done"},
        {"type": "sources", "batch": STUB_FAST_SOURCES, "total": 2},
        {"type": "stage", "stage_key": "write", "status": "running", "label": "综合中"},
        *({"type": "token", "delta": d} for d in STUB_FAST_TOKEN_DELTAS),
        {"type": "stage", "stage_key": "write", "status": "done"},
    ]


def _deep_expected() -> list[dict[str, Any]]:
    """api.md §7.4 深度模式主干（701-708 行）的桩展开：plan/write 两阶段。"""
    return [
        {"type": "stage", "stage_key": "plan", "status": "running"},
        {"type": "stage", "stage_key": "plan", "status": "done"},
        {"type": "stage", "stage_key": "write", "status": "running", "label": "撰写报告"},
        *({"type": "token", "delta": d} for d in STUB_DEEP_TOKEN_DELTAS),
        {"type": "stage", "stage_key": "write", "status": "done"},
    ]


def test_registry_exposes_two_stub_graphs() -> None:
    """GRAPH_REGISTRY 注册 fast/deep 两张编译图（langgraph.json 三入口的子图键）。"""
    assert set(GRAPH_REGISTRY) == {"fast", "deep"}


async def test_route_fast_to_fast_subgraph() -> None:
    """路由一：mode=fast → fast 桩子图，事件序列与 api.md §7.4 快速主干一致。"""
    signals, final = await _run({"mode": "fast", "query": "langgraph 条件边怎么写"})

    assert final["answer_md"] == STUB_FAST_ANSWER_MD
    assert final["sources"] == STUB_FAST_SOURCES
    assert signals == _fast_expected()


async def test_route_deep_to_deep_subgraph() -> None:
    """路由二：mode=deep → deep 桩子图，事件序列与 api.md §7.4 深度主干一致。"""
    signals, final = await _run({"mode": "deep", "query": "多框架对比研究报告"})

    assert final["answer_md"] == STUB_DEEP_ANSWER_MD
    assert signals == _deep_expected()
    # deep 桩不产 sources（子代理研究为 B4 实装）
    assert all(signal["type"] != "sources" for signal in signals)


async def test_route_missing_mode_falls_back_to_default() -> None:
    """路由三：mode 缺失 → 回落 default 子图（fast），序列不变。"""
    signals, final = await _run({"query": "没有 mode 字段的输入"})

    assert final["answer_md"] == STUB_FAST_ANSWER_MD
    assert signals == _fast_expected()


async def test_route_unregistered_mode_falls_back_with_warning(
    log_records: list[dict[str, Any]],
) -> None:
    """路由四：mode 指向未注册图 → 回落 default + WARNING（AgentLog，走 LogRecord 体系）。"""
    signals, final = await _run({"mode": "hyper", "query": "未注册模式"})

    assert final["answer_md"] == STUB_FAST_ANSWER_MD
    assert signals == _fast_expected()

    warnings = [r for r in log_records if r["level"].name == "WARNING"]
    assert len(warnings) == 1
    assert "hyper" in warnings[0]["message"]
    assert DEFAULT_GRAPH in warnings[0]["message"]
    assert warnings[0]["extra"]["graph"] == "main"
    assert warnings[0]["extra"]["mode"] == "hyper"


async def test_sources_payload_matches_api_contract() -> None:
    """sources 信号 batch 字段对齐 api.md §7.2：ref_no 冻结 1/2、source_type=web。"""
    signals, _ = await _run({"mode": "fast", "query": "x"})
    sources_signal = next(s for s in signals if s["type"] == "sources")

    assert sources_signal["total"] == 2
    assert [item["ref_no"] for item in sources_signal["batch"]] == [1, 2]
    for item in sources_signal["batch"]:
        assert item["source_type"] == "web"
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


async def test_search_delay_via_configurable_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """delay 途径一：configurable["delay"] 优先于模块常量（常量置大值作哨兵验证）。"""
    monkeypatch.setattr(fast_module, "SEARCH_DELAY_S", 0.5)  # 哨兵：若被采用会超时判差

    started = time.perf_counter()
    signals, final = await _run(
        {"mode": "fast", "query": "x"}, config={"configurable": {"delay": 0.05}}
    )
    elapsed = time.perf_counter() - started

    assert 0.05 <= elapsed < 0.5  # 用了 0.05（configurable），没用哨兵 0.5
    assert final["answer_md"] == STUB_FAST_ANSWER_MD
    assert signals == _fast_expected()


def test_search_delay_via_module_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    """delay 途径二：configurable 缺省时读模块常量 fast.SEARCH_DELAY_S（可 monkeypatch）。"""
    monkeypatch.setattr(fast_module, "SEARCH_DELAY_S", 0.05)

    started = time.perf_counter()
    final = main_graph.invoke({"mode": "fast", "query": "x"})
    elapsed = time.perf_counter() - started

    assert elapsed >= 0.05
    assert final["answer_md"] == STUB_FAST_ANSWER_MD
