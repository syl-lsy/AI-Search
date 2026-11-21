"""unit 层：deep regenerate 微图（api.md §2.12 v1.28 / §7.4 深度 regenerate 形态段 :937）。

钉住的语义（冻结契约，逐条对应测试）：
- 拓扑=START → writer → 条件边 → finalize|fallback_done → END（重放起点=write，
  不重新检索、不重跑 planner/research/reflect；writer/finalize 为 deep 子图真节点
  原样复用——write done 帧唯一归 finalize 的既有纪律保持）；
- 成功事件面：stage(write,running,label=撰写报告) →（messages 域 token）→
  stage(write,done,extra.sections)——outline 缺省空、writer 以 sources 元数据
  自组织分节（{{sections_hint}} 渲染空串），extra.sections=大纲节数公式下 0 为
  诚实值（regenerate 轮无大纲）；
- 再失败事件面（§7.4:946 再失败形态，fast 兜底收束同构适用 deep）：digest=
  fallback_token 整块单事件 + stage(write,done,extra.fallback=synthesis_digest)；
  partial=内容已随流式 token 出域 → stage(write,done,extra.fallback=
  synthesis_partial) 不重发；
- 非 GRAPH_REGISTRY：mode 路由面不污染（fast regenerate 微图先例同款）。

零网络：writer LLM 工厂 monkeypatch（deep_writer 命名空间）——真拓扑 + 真信号面，
仅模型假体替换（test_synthesis.py _install 同款手法）。
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk

from app.agent.graphs.deep_regenerate import graph, route_after_writer

_REPORT_DIGEST_PREAMBLE_ZH = "报告撰写未能完成（生成超时或失败），以下是本次研究收集到的来源速览，可先参考来源面板获取完整信息。"

_SOURCES: list[dict[str, Any]] = [
    {"ref_no": 1, "url": "https://a/1", "title": "来源 1", "snippet": "片段", "source_type": "web", "score": 0.9},
    {"ref_no": 2, "url": "https://b/2", "title": "来源 2", "snippet": "片段", "source_type": "web", "score": 0.8},
]

_INPUTS: dict[str, Any] = {
    "turn_id": "t",
    "thread_id": "th",
    "user_id": "u",
    "mode": "deep",
    "query": "问题",
    "query_lang": "zh",
    "spec": {},
    "collected_sources": _SOURCES,
    # outline/subagent_reports 缺省空（state-only 不落库结构性不可重建）
}


class _FakeWriterModel:
    """writer LLM 假体：astream 产出脚本 chunk 后可抛错（deep_writer.build_chat_model 替换面）。"""

    def __init__(self, chunks: list[str], fail: Exception | None = None) -> None:
        self._chunks = chunks
        self._fail = fail

    def astream(self, prompt: Any, config: Any = None, **kw: Any) -> Any:
        chunks = self._chunks
        fail = self._fail

        async def gen() -> AsyncIterator[AIMessageChunk]:
            for text in chunks:
                yield AIMessageChunk(content=text)
            if fail is not None:
                raise fail

        return gen()


def _custom_events(frames: list[Any]) -> list[dict[str, Any]]:
    return [chunk for _ns, mode, chunk in frames if mode == "custom"]


def _stage_payloads(frames: list[Any]) -> list[dict[str, Any]]:
    return [chunk for chunk in _custom_events(frames) if chunk.get("type") == "stage"]


async def _run(frames_out: list[Any]) -> None:
    config = {"configurable": {"thread_id": "th", "turn_id": "t"}}
    async for namespace, mode, chunk in graph.astream(
        dict(_INPUTS), config=config, stream_mode=["messages", "updates", "custom"], subgraphs=True
    ):
        frames_out.append((namespace, mode, chunk))


# ---- 路由纯函数 ----


def test_route_after_writer_routes_fallback_and_finalize() -> None:
    """writer 出口路由：errors 含兜底标记 → fallback_done；正常 → finalize。"""
    assert route_after_writer({"errors": [{"stage": "write", "reason": "llm_call_failed"}]}) == "fallback_done"
    assert route_after_writer({"errors": [{"stage": "research", "reason": "x"}]}) == "finalize"
    assert route_after_writer({}) == "finalize"


def test_fallback_write_done_digest_detection() -> None:
    """digest/partial 判定与 deep_writer 兜底装配同源（机械前缀）——导出面可判定。"""
    from app.agent.graphs.deep_regenerate import is_writer_fallback_digest

    assert is_writer_fallback_digest(_REPORT_DIGEST_PREAMBLE_ZH + "\n\n- [1] 来源：x")
    assert not is_writer_fallback_digest("正常部分正文。")  # partial 非机械前缀
    assert not is_writer_fallback_digest("")
    assert not is_writer_fallback_digest(None)


# ---- 真拓扑 + 真信号面（LLM 假体）----


async def test_micrograph_success_emits_write_running_then_done_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功：stage(write,running,label=撰写报告) →（writer token messages）→
    stage(write,done,extra.sections)（finalize 发——write done 唯一归属保持）；
    finalize 引用归一消费 collected_sources 账本。"""
    import app.agent.nodes.deep_writer as writer_module

    monkeypatch.setattr(
        writer_module, "build_chat_model", lambda alias, **kw: _FakeWriterModel(["# 报告\n", "正文[1]。"])
    )
    frames: list[Any] = []
    await _run(frames)

    stages = _stage_payloads(frames)
    assert [s["status"] for s in stages] == ["running", "done"]
    assert stages[0]["stage_key"] == "write" and stages[0]["label"] == "撰写报告"  # 无大纲回落
    assert stages[1]["extra"] == {"sections": 0}  # 大纲节数公式下 regenerate 无大纲=诚实 0
    # messages 域（writer token）断言不在本文件：假体模型不经回调系统，真 LangGraph
    # messages 帧仅在真 chat model 下产生——writer 帧门控放行覆盖在 TurnRunner 层
    # （test_regenerate.py scripted 假体，零段 namespace + langgraph_node="writer"）
    # 与集成面（stub_flow DeepRegenerateFakeGraph）锁定。
    # 终态 answer_md=正文（finalize 归一透传）
    updates = [chunk for _ns, mode, chunk in frames if mode == "updates"]
    answer = None
    for update in updates:
        for _node, payload in update.items():
            if isinstance(payload, dict) and "answer_md" in payload:
                answer = payload["answer_md"]
    assert answer == "# 报告\n正文[1]。"


async def test_micrograph_writer_failure_digest_fallback_face(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """writer 失败（零产出）→ 条件边走 fallback_done：fallback_token 整块单事件 +
    stage(write,done,extra.fallback=synthesis_digest)（§7.4:946 再失败形态；
    fast synthesis :641-642 同构）；finalize 不运行（无双 write done 帧）。"""
    import app.agent.nodes.deep_writer as writer_module

    monkeypatch.setattr(
        writer_module, "build_chat_model", lambda alias, **kw: _FakeWriterModel([], fail=RuntimeError("llm 挂"))
    )
    frames: list[Any] = []
    await _run(frames)

    custom = _custom_events(frames)
    types = [c.get("type") for c in custom]
    assert types == ["stage", "fallback_token", "stage"]  # running → 整块 → done
    stages = _stage_payloads(frames)
    assert stages[0]["status"] == "running" and stages[0]["label"] == "撰写报告"
    assert stages[1]["extra"] == {"fallback": "synthesis_digest"}
    fallback_token = next(c for c in custom if c.get("type") == "fallback_token")
    assert fallback_token["delta"].startswith("报告撰写未能完成")  # 机械速览（零 LLM）
    updates = [chunk for _ns, mode, chunk in frames if mode == "updates"]
    answer = None
    for update in updates:
        for _node, payload in update.items():
            if isinstance(payload, dict) and "answer_md" in payload:
                answer = payload["answer_md"]
    assert answer is not None and answer.startswith("报告撰写未能完成")


async def test_micrograph_writer_failure_partial_fallback_face(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """writer 失败（有可用部分正文）→ partial：stage(write,done,
    extra.fallback=synthesis_partial)、无 fallback_token（内容已随流式 token 出域）。"""
    import app.agent.nodes.deep_writer as writer_module

    monkeypatch.setattr(
        writer_module,
        "build_chat_model",
        lambda alias, **kw: _FakeWriterModel(["## 背景\n研究已成文。", "残句"], fail=RuntimeError("llm 挂")),
    )
    frames: list[Any] = []
    await _run(frames)

    custom = _custom_events(frames)
    types = [c.get("type") for c in custom]
    assert "fallback_token" not in types  # partial 不重发已流式内容
    stages = _stage_payloads(frames)
    assert stages[-1]["extra"] == {"fallback": "synthesis_partial"}


async def test_micrograph_partial_fallback_normalizes_out_of_range_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2（v1.70）：partial 兜底跳过 finalize → `fallback_done` 按账本 ref_no 归一。

    缺陷：兜底轮越界 [n]（如 [9] > 账本 2 条）原样落库出域、前端引用悬空。修复断言：
    落库正文剔除 [9]、合法 [1] 保留，errors[] 留痕 citation_out_of_range（finalize 同口径）。
    """
    import app.agent.nodes.deep_writer as writer_module

    monkeypatch.setattr(
        writer_module,
        "build_chat_model",
        lambda alias, **kw: _FakeWriterModel(
            ["## 背景\n研究已成文[1]。", "残句引用[9]。"], fail=RuntimeError("llm 挂")
        ),
    )
    frames: list[Any] = []
    await _run(frames)

    updates = [chunk for _ns, mode, chunk in frames if mode == "updates"]
    payloads = [p for update in updates for p in update.values() if isinstance(p, dict)]
    asserts = [p for p in payloads if "answer_md" in p]
    answer = asserts[-1]["answer_md"]
    assert "[1]" in answer  # 账本内合法引用保留
    assert "[9]" not in answer  # 越界剔除（finalize 不在兜底路径，本节点承载归一）
    errors = [e for p in payloads for e in (p.get("errors") or []) if isinstance(e, dict)]
    assert {"stage": "write", "reason": "citation_out_of_range", "dropped": 1} in errors


async def test_micrograph_finalize_not_run_on_fallback_no_double_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失败路径 finalize 不运行——stage(write,done) 恰一帧（双 done 帧观感规避，
    盘点 decisions[3] 纪律在 regenerate 微图保持）。"""
    import app.agent.nodes.deep_writer as writer_module

    monkeypatch.setattr(
        writer_module, "build_chat_model", lambda alias, **kw: _FakeWriterModel([], fail=RuntimeError("llm 挂"))
    )
    frames: list[Any] = []
    await _run(frames)

    done_frames = [s for s in _stage_payloads(frames) if s["status"] == "done"]
    assert len(done_frames) == 1
    assert done_frames[0]["extra"] == {"fallback": "synthesis_digest"}
