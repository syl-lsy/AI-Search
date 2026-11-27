"""探针：langgraph==1.2.19 无 checkpointer 下 interrupt() 的真实行为。

结论前置（2026-11-27 实测，证据档 docs/research/b22-probe-interrupt-20261127.md）：
- 实验环境：langgraph 1.2.19 + langgraph-checkpoint 4.3.1 + Python 3.12.14
  （uv run --package search-api，仓库根执行）。
- 【实验①】裸 compile()（无 checkpointer）+ 节点内 interrupt() + astream：
  interrupt() **照常生效**——__interrupt__ 事件正常发射
  （Interrupt(value={'question': 'approve?', 'payload': {'x': 1}}, id=...））、流优雅结束、
  node_a 未完成、node_b 未跑、零异常零挂起零静默放行。即：中断面（事件发射）不依赖 checkpointer。
- 【实验②】无 checkpointer 下 Command(resume=...) 续跑：**不可恢复**，硬错误在输入校验层：
  RuntimeError("Cannot use Command(resume=...) without checkpointer")
  （langgraph/pregel/_loop.py:1023 _first），节点零进入、事件零输出。
- 【实验③】对照组 InMemorySaver：首跑 __interrupt__ 事件正常发射、流优雅结束；
  Command(resume="APPROVED-BY-PROBE") 续跑后 node_a 内 interrupt() 返回该值、node_a 完成、
  node_b 跑、正常 END——文档行为完全成立，差异确由 checkpointer 缺失引起（对照实证）。
- 【实验④·补充】无 checkpointer 中断后用普通输入（非 Command）重跑：node_a 从零重入、
  再次 __interrupt__——中断态零持久化，不存在任何「隐形恢复」路径。
- 【对计划假定的裁定】plan-hitl-kb-gate-20261126.md §3.1（:148/:155）「方案 A interrupt 不可恢复
  （无 checkpointer）」被实测支持：恢复面确死（RuntimeError 硬拒绝）+ 状态不落（普通重跑从零重入）。
  唯一与「机制原理直觉」不同的是中断面：__interrupt__ 事件在无 checkpointer 下也发射——
  若误以为「事件能出=可恢复」会在 TurnRunner 消费面上踩坑，此为偏差面非裁决面（详见报告 anomalies）。
  「本项目不使用图内 interrupt（选方案 B 两阶段执行）」的裁决方向被实测支持。

本脚本四段实验（0 网络 0 真模型纯内存）：
  Phase 1  裸 builder.compile() 两节点图，node_a 内 interrupt()，astream 观察真实形态
           （异常类型？静默通过？挂起？事件流输出什么？）
  Phase 2  流走到中断面后：Command(resume=...) 是否可续跑（无 checkpointer 下的恢复面）
  Phase 3  对照组：同一图挂 InMemorySaver，interrupt/resume 是否按文档行为工作
  Phase 4  补充：无 checkpointer 中断后普通输入重跑——验证中断态是否残留（ADR-019 补强证据）

运行：uv run --package search-api python scripts/probe_interrupt_no_checkpointer.py
（从仓库根跑；脚本零依赖项目 app 代码，仅用 langgraph 公共面。）
"""

from __future__ import annotations

import asyncio
import traceback
from importlib.metadata import version
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

RESUME_VALUE = "APPROVED-BY-PROBE"
PHASE_TIMEOUT_S = 20  # 防挂起；若命中说明「挂起」是真实形态


def banner(msg: str) -> None:
    print(f"\n{'=' * 78}\n== {msg}\n{'=' * 78}")


class ProbeState(TypedDict):
    x: int


def node_a(state: ProbeState) -> dict:
    print("  [node_a] ENTER")
    ret = interrupt({"question": "approve?", "payload": {"x": state["x"]}})
    print(f"  [node_a] interrupt() returned: {ret!r}")
    return {"x": state["x"] + 1}


def node_b(state: ProbeState) -> dict:
    print(f"  [node_b] ENTER (x={state['x']})")
    return {"x": state["x"] + 100}


def build_graph() -> StateGraph:
    g = StateGraph(ProbeState)
    g.add_node("node_a", node_a)
    g.add_node("node_b", node_b)
    g.add_edge(START, "node_a")
    g.add_edge("node_a", "node_b")
    g.add_edge("node_b", END)
    return g


async def drain(stream_aiter: Any, label: str) -> tuple[list, Exception | None]:
    """消费事件流，逐事件原样打印；区分「正常耗尽」与「异常冒泡」。"""
    events: list = []
    try:
        async for ev in stream_aiter:
            print(f"  [{label}] EVENT: {ev!r}")
            events.append(ev)
    except Exception as exc:  # noqa: BLE001 —— 探针要的就是异常形态本身
        print(f"  [{label}] EXCEPTION-CLASS: {type(exc).__module__}.{type(exc).__qualname__}")
        print(f"  [{label}] EXCEPTION-MSG  : {exc!s}")
        tail = "\n".join(traceback.format_exc().splitlines()[-5:])
        print(f"  [{label}] TRACEBACK-TAIL:\n{tail}")
        return events, exc
    print(f"  [{label}] STREAM-EXHAUSTED (正常耗尽, {len(events)} events)")
    return events, None


async def phase1(graph: Any) -> dict:
    banner("Phase 1 裸 compile()（无 checkpointer）+ 节点内 interrupt() + astream 首跑")
    out: dict = {"events": [], "exc": None}
    try:
        async with asyncio.timeout(PHASE_TIMEOUT_S):
            events, exc = await drain(
                graph.astream({"x": 1}, config={"configurable": {"thread_id": "t1"}}),
                "P1",
            )
        out["events"], out["exc"] = events, exc
    except TimeoutError:
        print(f"  [P1] TIMEOUT after {PHASE_TIMEOUT_S}s —— interrupt 在无 checkpointer 下挂起（真实形态）")
        out["exc"] = TimeoutError("phase-timeout")
    return out


async def phase2(graph: Any) -> dict:
    banner(f"Phase 2 无 checkpointer 下 Command(resume={RESUME_VALUE!r}) 尝试续跑（同一编译图对象）")
    out: dict = {"events": [], "exc": None}
    try:
        async with asyncio.timeout(PHASE_TIMEOUT_S):
            events, exc = await drain(
                graph.astream(
                    Command(resume=RESUME_VALUE),
                    config={"configurable": {"thread_id": "t1"}},
                ),
                "P2",
            )
        out["events"], out["exc"] = events, exc
    except TimeoutError:
        print(f"  [P2] TIMEOUT after {PHASE_TIMEOUT_S}s —— resume 挂起（真实形态）")
        out["exc"] = TimeoutError("phase-timeout")
    return out


async def phase3() -> dict:
    banner("Phase 3 对照组：同一图挂 InMemorySaver，interrupt → resume 文档行为验证")
    graph = build_graph().compile(checkpointer=InMemorySaver())
    cfg: RunnableConfig = {"configurable": {"thread_id": "t3"}}
    out: dict = {"run1_events": [], "run1_exc": None, "run2_events": [], "run2_exc": None}

    print("-- P3 run1 首跑（期望 __interrupt__ 事件 + 流优雅结束 + node_b 未跑）")
    try:
        async with asyncio.timeout(PHASE_TIMEOUT_S):
            events, exc = await drain(graph.astream({"x": 1}, config=cfg), "P3.1")
        out["run1_events"], out["run1_exc"] = events, exc
    except TimeoutError:
        print(f"  [P3.1] TIMEOUT after {PHASE_TIMEOUT_S}s（真实形态）")
        out["run1_exc"] = TimeoutError("phase-timeout")
        return out

    has_interrupt_ev = any("__interrupt__" in ev if isinstance(ev, dict) else False for ev in out["run1_events"])
    print(f"  [P3.1] __interrupt__ 事件出现: {has_interrupt_ev}")
    if not has_interrupt_ev:
        return out

    print(f"-- P3 run2 续跑 Command(resume={RESUME_VALUE!r})（期望 node_a 内返回该值 + node_b 跑 + END）")
    try:
        async with asyncio.timeout(PHASE_TIMEOUT_S):
            events, exc = await drain(graph.astream(Command(resume=RESUME_VALUE), config=cfg), "P3.2")
        out["run2_events"], out["run2_exc"] = events, exc
    except TimeoutError:
        print(f"  [P3.2] TIMEOUT after {PHASE_TIMEOUT_S}s（真实形态）")
        out["run2_exc"] = TimeoutError("phase-timeout")
    return out


async def phase4(graph: Any) -> dict:
    """补充实验（超出工单三段，为 ADR-019 补最后一块证据）：
    无 checkpointer 下中断后，用普通输入（非 Command）重跑同一图 ——
    观察是否从零重入 node_a（证明中断态未持久化、不存在任何「隐形恢复」路径）。"""
    banner("Phase 4 补充：无 checkpointer 中断后普通输入重跑（非 Command）——验证中断态是否残留")
    out: dict = {"events": [], "exc": None}
    try:
        async with asyncio.timeout(PHASE_TIMEOUT_S):
            events, exc = await drain(
                graph.astream({"x": 1}, config={"configurable": {"thread_id": "t1"}}),
                "P4",
            )
        out["events"], out["exc"] = events, exc
    except TimeoutError:
        print(f"  [P4] TIMEOUT after {PHASE_TIMEOUT_S}s（真实形态）")
        out["exc"] = TimeoutError("phase-timeout")
    return out


async def main() -> int:
    print(f"langgraph={version('langgraph')} langgraph-checkpoint={version('langgraph-checkpoint')}")
    import sys

    print(f"python={sys.version.split()[0]}")

    bare_graph = build_graph().compile()  # 裸编译：无 checkpointer（P1/P2/P4 共用同一对象）
    p1 = await phase1(bare_graph)
    p2 = await phase2(bare_graph)
    p4 = await phase4(bare_graph)
    p3 = await phase3()

    banner("VERDICT 汇总")
    p1_exc = p1["exc"]
    p1_form = (
        "exception-bubbled:" + type(p1_exc).__qualname__
        if p1_exc
        else ("interrupt-event-emitted" if any("__interrupt__" in e for e in p1["events"] if isinstance(e, dict)) else "silent-completed")
    )
    print(f"  Phase1 form = {p1_form} | events={len(p1['events'])}")
    p2_form = "exception-bubbled:" + type(p2["exc"]).__qualname__ if p2["exc"] else (
        "resumed" if any(isinstance(e, dict) and "node_b" in e for e in p2["events"]) else "interrupt-only"
    )
    print(f"  Phase2 form = {p2_form} | events={len(p2['events'])}")
    p4_rerun_node_a = p4["exc"] is None and len(p4["events"]) >= 1 and any(
        isinstance(e, dict) and "__interrupt__" in e for e in p4["events"]
    )
    print(f"  Phase4 form = {'re-run-from-scratch(再次 __interrupt__)' if p4_rerun_node_a else 'other(见 P4 输出)'} | events={len(p4['events'])}")
    p3_ok = (
        p3["run1_exc"] is None
        and any(isinstance(e, dict) and "__interrupt__" in e for e in p3["run1_events"])
        and p3["run2_exc"] is None
        and any(isinstance(e, dict) and "node_b" in e for e in p3["run2_events"])
    )
    print(f"  Phase3 doc-behavior (interrupt 事件 + resume 完成 node_b) = {'PASS' if p3_ok else 'FAIL'}（resume 值是否传入节点内以 P3 输出「interrupt() returned」行为准）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
