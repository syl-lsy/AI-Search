"""deep 模式子图（M2 批次 2：planner/research 接真，write 仍为桩——ARCH §4.2 逐节点实装）。

节点序列 planner → research → write（节点名 = 实装进度；stage_key 见 api.md §7.2）。
planner 为真节点（强模型结构化输出，`nodes/deep_planner.py`——子课题拆分 + 报告
大纲 + 顺带分类，history 指代消解，兜底单子课题不终止）；research 为真节点
（deepagents 首次接入，`nodes/deep_research.py`——create_deep_agent 装配子代理并行
研究，OQ-33 装配形态 / OQ-34 Standard 档裁决，搜索/读取阶段与 sources 批次事件；
reflect/supplement 补搜回路批次 3 接入）；write 仍为 B2 桩（批次 4 writer 实装替换）。
发射的 custom 信号序列对齐 api.md §7.4 深度主干（701-708 行）的现行子集。M2 批次 1
起 plan 的 running 归 TurnRunner 图前发布（同 fast rewrite 先例——§4.3「首个 stage
≤1s」，图内不再自发 running 避免重复 seq），图内信号序列（现行子集）：

    stage(plan,done,extra.subtopics=N)
    → stage(search,running,extra.subagents[]) → sources(batch…)（research 内即时发射）
    → stage(read,running,label=阅读第 N 篇)… stage(read,done)
    → stage(write,running,label=撰写报告) → token…token → stage(write,done)

- reflect 阶段、extra.sections 为批次 3/4 实装产物，桩期不携带；
- `graph` 模块级编译产物供 langgraph.json `deep` 入口与 registry 注册共用。
"""

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agent.graphs.signals import emit_stage, emit_token
from app.agent.nodes.deep_planner import planner
from app.agent.nodes.deep_research import research
from app.agent.state import DeepState

#: 固定 token 增量 2 段；答案 = 增量拼接，保证一致性
STUB_DEEP_TOKEN_DELTAS: list[str] = [
    "【桩答案·B2·deep】深度模式桩子图固定答案（write 桩不接真模型，批次 4 writer 实装）。",
    "桩期 plan/write 两阶段信号与 token 增量即可撑起完整事件管线。",
]

STUB_DEEP_ANSWER_MD: str = "".join(STUB_DEEP_TOKEN_DELTAS)


def write(state: DeepState) -> dict[str, Any]:
    """撰写桩：stage(write, running→done) 夹 token 流；终态 answer_md 固定 Markdown
    （批次 4 writer 实装替换——按 outline 分节流式，只读 subagent_reports）。"""
    emit_stage("write", "running", label="撰写报告")
    for delta in STUB_DEEP_TOKEN_DELTAS:
        emit_token(delta)
    emit_stage("write", "done")
    return {"answer_md": STUB_DEEP_ANSWER_MD}


def build_deep_graph() -> Any:
    """编译 deep 子图（M2 批次 2：planner/research 真节点 + write 桩；批次 3~4
    reflect→supplement→guards→writer→finalize 真拓扑逐批替换）。"""
    builder = StateGraph(DeepState)
    builder.add_node("planner", planner)
    builder.add_node("research", research)
    builder.add_node("write", write)
    builder.add_edge(START, "planner")
    builder.add_edge("planner", "research")
    builder.add_edge("research", "write")
    builder.add_edge("write", END)
    return builder.compile()


graph = build_deep_graph()
