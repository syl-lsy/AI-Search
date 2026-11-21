"""deep regenerate 微图（api.md §2.12 v1.28 / §7.4 深度 regenerate 形态段 :937）。

「仅重新生成」deep 分支的执行形态：**进程内单例微图**——重放起点=write（writer
节点），**不重新检索、不重跑 planner/research/reflect**（subagent_reports/outline 为
state-only 不落库〔ADR-003 落库子集不变〕结构性不可重建；outline 缺省空、writer 以
该轮 sources 元数据自组织分节——`{{sections_hint}}` 渲染空串为模板既有支持，入口
label 回落「撰写报告」）。

拓扑：START → writer → 条件边 → finalize | fallback_done → END。

- **writer/finalize 原样复用**（deep 子图真节点零改动）：writer 只发 running 推进帧
  （入口「撰写第 1 节」/无大纲「撰写报告」+ 分节推进）+ token 经 messages 域冒泡；
  finalize 承载引用统一编号去重 + [n]↔sources 双向核对 + **write done 帧唯一发射**
  （extra.sections=大纲节数公式——regenerate 轮无大纲，成功轮 sections=0 为诚实值）；
- **条件边（本模块唯一新增面）**：writer 失败兜底轮（deep_writer 失败语义——errors
  增量 {stage:"write", reason:"llm_call_failed"}，D2：兜底不置 degraded）路由到
  fallback_done，**跳过 finalize**——否则会双发 write done 帧（finalize sections=0
  + 兜底收束帧），破坏「write done 帧唯一归属」纪律（盘点 decisions[3] 在 regenerate
  微图保持）。fallback_done 发射面与 fast synthesis 兜底收束同构（§7.4:946 再失败
  形态「兜底收束对 deep 分支同构适用」）：
  - digest（零可用部分正文——deep_writer._REPORT_DIGEST_PREAMBLE 机械前缀判定）：
    `fallback_token` 整块单事件（内容未流式过，补发承载完整来源速览）+
    `stage(write, done, extra.fallback=synthesis_digest)`（synthesis.py:641-642 同构）；
  - partial（部分正文已随流式 token 出域）：只发
    `stage(write, done, extra.fallback=synthesis_partial)`（synthesis.py:632 同构，
    不重发已流式内容）；**正文在本节点复用 `_normalize_citations` 按账本 ref_no 归一**
    （越界 [n] 不落库出域——兜底轮跳过 finalize，v1.70 P2）；
  终态归 TurnRunner：updates 携带兜底标记 → `_finish_synthesis_fallback`（零 error、
  done(finish=degraded, degraded_reason=None)、regenerate 轮恒有 sources 故 500101
  不可达口径通用）。
- **非 GRAPH_REGISTRY**：那张表是 mode 路由面（fast/deep 用户模式），regenerate 不是
  用户 mode——污染语义即破坏「前端点模式仅为本地状态」契约（regenerate.py 决策 10
  备选否决同款）；节点名 "writer" 保持 = messages 门控判据对齐面（微图直跑零段
  namespace，TurnRunner regenerate 深度分支按 langgraph_node 放行）；
- 编译时机：import 一次 = 编译一次（三图编译同口径），服务进程内单例复用。

StateGraph 的 reducer/字段沿用 DeepState（writer/finalize 只写 answer_md/report_md/
usage_total/errors——DeepState 子集，投影零差异）。
"""

from collections.abc import Mapping
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent.graphs.signals import emit_fallback_token, emit_stage
from app.agent.nodes.deep_finalize import (
    _ledger_refnos,
    _normalize_citations,
)
from app.agent.nodes.deep_finalize import finalize as finalize_node
from app.agent.nodes.deep_writer import (
    _REPORT_DIGEST_PREAMBLE,
    writer as writer_node,
)
from app.agent.state import DeepState
from app.core.logger import AgentLog

__all__ = [
    "build_deep_regenerate_graph",
    "fallback_write_done",
    "graph",
    "has_writer_fallback",
    "is_writer_fallback_digest",
    "route_after_writer",
]


def has_writer_fallback(errors: Any) -> bool:
    """writer 兜底标记判定（deep_writer 失败语义单一事实源——finalize 同款判据）：
    errors 含 {stage:"write", reason:"llm_call_failed"}。"""
    if not isinstance(errors, list):
        return False
    return any(
        isinstance(item, Mapping)
        and item.get("stage") == "write"
        and item.get("reason") == "llm_call_failed"
        for item in errors
    )


def is_writer_fallback_digest(answer_md: Any) -> bool:
    """writer 兜底产出 digest/partial 判定：deep_writer 机械速览说明行前缀
    （_REPORT_DIGEST_PREAMBLE 双语）——digest 由此装配（deep_writer._fallback_answer
    单一事实源），partial 为 LLM 已流出正文的句界截断（与本前缀撞车仅当模型逐字复述
    我方兜底文案，忽略不计）。"""
    text = answer_md if isinstance(answer_md, str) else ""
    return any(text.startswith(preamble) for preamble in _REPORT_DIGEST_PREAMBLE.values())


def route_after_writer(state: Any) -> str:
    """writer 出口条件边：失败兜底轮 → fallback_done（fast 兜底收束同构事件面，
    finalize 跳过防双 write done 帧）；正常轮 → finalize（write done 帧唯一归属）。"""
    errors = state.get("errors") if isinstance(state, Mapping) else None
    return "fallback_done" if has_writer_fallback(errors) else "finalize"


def fallback_write_done(state: Any) -> dict[str, Any]:
    """writer 兜底轮的 write done 收束（deep regenerate 专用——§7.4:946 再失败形态）。

    digest（零可用部分正文，未流式过）→ `fallback_token` 整块单事件补发完整速览 +
    `stage(write, done, extra.fallback=synthesis_digest)`；partial（已随流式 token
    出域）→ 仅 `stage(write, done, extra.fallback=synthesis_partial)`。
    **引用归一内联承载（v1.70 P2 修复）**：兜底轮跳过 finalize（条件边直落本节点），
    此前 partial 正文的越界 [n] 原样落库出域——本节点按账本 ref_no 集合复用
    `_normalize_citations`（finalize 单一事实源：相邻重复折叠 + 越界剔除），归一后
    正文回写 state（answer_md/report_md 双键等值，finalize 同口径），越界剔除留痕
    errors[] + AgentLog（deep_degraded_writer 降级路径同款）。digest 为机械速览
    （未经过 LLM 生成正文，无引用面）不参与归一。
    纯信号面零 LLM 零 IO；终态归 TurnRunner（updates errors 标记 → _finish_synthesis_fallback）。
    """
    answer_md = state.get("answer_md") if isinstance(state, Mapping) else None
    text = answer_md if isinstance(answer_md, str) else ""
    if is_writer_fallback_digest(answer_md):
        emit_fallback_token(text)
        emit_stage("write", "done", extra={"fallback": "synthesis_digest"})
        return {}
    valid = _ledger_refnos(
        state.get("collected_sources") if isinstance(state, Mapping) else None
    )
    normalized, dropped = _normalize_citations(text, valid)
    emit_stage("write", "done", extra={"fallback": "synthesis_partial"})
    update: dict[str, Any] = {"answer_md": normalized, "report_md": normalized}
    if dropped:
        update["errors"] = [
            {"stage": "write", "reason": "citation_out_of_range", "dropped": dropped}
        ]
        AgentLog(
            message=(
                f"deep regenerate 兜底部分正文引用越界剔除 {dropped} 处"
                "（finalize 不在兜底路径）"
            ),
            graph="deep",
            node="fallback_done",
        ).warning()
    return update


def build_deep_regenerate_graph() -> CompiledStateGraph:
    """编译 deep regenerate 微图（START → writer → 条件边 → finalize|fallback_done → END）。"""
    builder = StateGraph(DeepState)
    builder.add_node("writer", writer_node)
    builder.add_node("finalize", finalize_node)
    builder.add_node("fallback_done", fallback_write_done)
    builder.add_edge(START, "writer")
    builder.add_conditional_edges(
        "writer",
        route_after_writer,
        {"finalize": "finalize", "fallback_done": "fallback_done"},
    )
    builder.add_edge("finalize", END)
    builder.add_edge("fallback_done", END)
    return builder.compile()


graph: Any = build_deep_regenerate_graph()
