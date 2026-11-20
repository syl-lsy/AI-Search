"""deep 模式子图（M2 批次 4：ARCH §4.2 全拓扑接真——writer/finalize/degraded_writer
实装收口）。

正常路径 planner → research → reflect ⇄（supplement → reflect 回路）→ guards →
writer → finalize → END；降级路径 guards -.-> degraded_writer → END（stage_key 见
api.md §7.2）。
planner 为真节点（强模型结构化输出，`nodes/deep_planner.py`——子课题拆分 + 报告大纲 +
顺带分类，history 指代消解，兜底单子课题不终止）；research 为真节点（deepagents 首次
接入，`nodes/deep_research.py`——create_deep_agent 装配子代理并行研究，OQ-33 装配形态 /
OQ-34 Standard 档裁决，搜索/读取阶段与 sources 批次事件）；reflect 为真节点（批次 3，
`nodes/deep_reflect.py`——对照大纲找缺口，结构化 {sufficient, gaps}，修复回喂 →
兜底 sufficient，回路判据 `route_reflect` 单一事实源）；supplement 为真节点（批次 3，
`nodes/deep_supplement.py`——gaps 定向补搜 + 三维去重 + sources 增量批次 ref_no 续编 +
reflect_round 递增方【回路有界性】）；guards 为真节点（批次 3，`nodes/deep_guards.py`
——取消感知/硬超时/预算三检查点，writer 前最后防线：trip → degraded_reason(timeout|
budget) 置位 + TurnRunner 分流 done(degraded) 零 error——api.md §7.2:845 v1.21 注；
无注入面零行为直通）；writer 为真节点（批次 4，`nodes/deep_writer.py`——按 outline
分节流式，只读 subagent_reports + 来源元数据 fuse_top_n 截断；**节点名注册 "writer"**
=R14③ messages 门控判据对齐面，stage_key="write" 与节点名解耦；失败兜底沿 reflect
先例不终止）；finalize 为真节点（批次 4，`nodes/deep_finalize.py`——引用统一编号
去重 + [n]↔sources 双向核对（越界剔除 + warning 三通道：done extra.citation_dropped
+ errors[] + AgentLog）+ 账本核验 + answer_md/report_md 持久化投影 + usage_total
终值回填；纯编排面零 LLM 零 IO；**write done 帧唯一归属本节点**——writer 只发
running 推进帧，规避双 done 帧观感〔盘点 decisions[3]〕）；degraded_writer 为真节点
（批次 4，`nodes/deep_degraded_writer.py`——仅 timeout/budget 触发〔route_guards 条件
边隔离，正常轮结构性不可达〕、一次 LLM 调用 30s 包络〔thinking off——包络驱动〕、
已收集内容降级报告 + 机械前置「研究未完成」显著标注、失败机械速览兜底不终止、
引用核对内联承载〔降级路径直通 END 不进 finalize〕、fallback_token 整块单事件 +
write done 裸帧〔盘点 decisions[4]〕）。
发射的 custom 信号序列对齐 api.md §7.4 深度主干（863-868 行）的现行子集。M2 批次 1
起 plan 的 running 归 TurnRunner 图前发布（同 fast rewrite 先例——§4.3「首个 stage
≤1s」，图内不再自发 running 避免重复 seq），图内信号序列（现行子集）：

    stage(plan,done,extra.subtopics=N)
    → stage(search,running,extra.subagents[]) → sources(batch…)（research 内即时发射）
    → stage(read,running,label=阅读第 N 篇)… stage(read,done)
    → stage(reflect,running,label=反思第 n/{rounds} 轮,detail=补搜方向)（进补搜轮）
      → stage(search,running)（supplement 段——§7.4:866 相对序）…
      → stage(reflect,done)（回路退出帧）
    → stage(write,running,label=撰写第 1 节) → token…token（messages 流域）…（分节
      label 推进，writer 发）→ stage(write,done,extra.sections=N[,citation_dropped])
      （finalize 发——write 段唯一收束帧）

降级路径（guards trip → 条件边 → degraded_writer——ARCH §4.2:475-480/:480 DG --> E）：

    stage(write,running,label=撰写降级报告) → token（fallback_token 整块单事件）
    → stage(write,done)（裸帧——writer 路径 done 带 extra.sections 即两路径判别面）

- TurnRunner 分流：degraded_reason∈{timeout,budget} → turns.status=degraded +
  done(finish=degraded, degraded_reason=同值) 零 error（api.md §7.2:803/:845）；
- `graph` 模块级编译产物供 langgraph.json `deep` 入口与 registry 注册共用。
"""

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agent.nodes.deep_degraded_writer import degraded_writer as degraded_writer_node
from app.agent.nodes.deep_finalize import finalize as finalize_node
from app.agent.nodes.deep_guards import guards as guards_node, route_guards
from app.agent.nodes.deep_planner import planner
from app.agent.nodes.deep_reflect import reflect, route_reflect
from app.agent.nodes.deep_research import research
from app.agent.nodes.deep_supplement import supplement as supplement_node
from app.agent.nodes.deep_writer import writer as writer_node
from app.agent.state import DeepState


def build_deep_graph() -> Any:
    """编译 deep 子图（M2 批次 4：ARCH §4.2 全拓扑接真——planner/research/reflect/
    supplement/guards/writer/finalize/degraded_writer 真节点 + 两处条件边（reflect
    出口 supplement/guards；guards 出口 writer/degraded_writer）。降级路径 guards →
    degraded_writer → END 直通（:480 DG --> E——不进 finalize，引用核对由
    degraded_writer 内联承载）；正常路径 guards → writer → finalize → END。"""
    builder = StateGraph(DeepState)
    builder.add_node("planner", planner)
    builder.add_node("research", research)
    builder.add_node("reflect", reflect)
    builder.add_node("supplement", supplement_node)
    builder.add_node("guards", guards_node)
    builder.add_node("writer", writer_node)
    builder.add_node("finalize", finalize_node)
    builder.add_node("degraded_writer", degraded_writer_node)
    builder.add_edge(START, "planner")
    builder.add_edge("planner", "research")
    builder.add_edge("research", "reflect")
    builder.add_conditional_edges(
        "reflect", route_reflect, {"supplement": "supplement", "guards": "guards"}
    )
    builder.add_edge("supplement", "reflect")
    builder.add_conditional_edges(
        "guards", route_guards, {"writer": "writer", "degraded_writer": "degraded_writer"}
    )
    builder.add_edge("writer", "finalize")
    builder.add_edge("finalize", END)
    builder.add_edge("degraded_writer", END)
    return builder.compile()


graph = build_deep_graph()
