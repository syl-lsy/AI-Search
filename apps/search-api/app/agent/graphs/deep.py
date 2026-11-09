"""deep 模式桩子图（B2 桩，B4 起 deepagents 子代理实装——ARCH §4.2）。

节点序列 plan → write（桩节点名 = api.md §7.2 stage_key），发射的 custom 信号
序列对齐 api.md §7.4 深度主干（701-708 行）的桩期子集：

    stage(plan,r) → stage(plan,d) → stage(write,running,label=撰写报告)
    → token…token → stage(write,d)

- search/read/reflect 阶段、sources 批次、extra.subagents/subtopics 均为 B4
  deepagents 子代理实装产物，桩不携带；
- 桩不接真模型；节点纯函数，持久化零介入；`graph` 模块级编译产物供
  langgraph.json `deep` 入口与 registry 注册共用。
"""

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agent.graphs.signals import emit_stage, emit_token
from app.agent.state import MainState

#: 固定 token 增量 2 段；答案 = 增量拼接，保证一致性
STUB_DEEP_TOKEN_DELTAS: list[str] = [
    "【桩答案·B2·deep】深度模式桩子图固定答案（不接真模型，B4 子代理实装）。",
    "桩期 plan/write 两阶段信号与 token 增量即可撑起完整事件管线。",
]

STUB_DEEP_ANSWER_MD: str = "".join(STUB_DEEP_TOKEN_DELTAS)


def plan(state: MainState) -> dict[str, Any]:
    """研究规划桩：stage(plan, running→done)，无 LLM、无子代理（B4 实装）。"""
    emit_stage("plan", "running")
    emit_stage("plan", "done")
    return {}


def write(state: MainState) -> dict[str, Any]:
    """撰写桩：stage(write, running→done) 夹 token 流；终态 answer_md 固定 Markdown。"""
    emit_stage("write", "running", label="撰写报告")
    for delta in STUB_DEEP_TOKEN_DELTAS:
        emit_token(delta)
    emit_stage("write", "done")
    return {"answer_md": STUB_DEEP_ANSWER_MD}


def build_deep_graph() -> Any:
    """编译 deep 桩子图（B4 起替换为 planner→research→reflect→writer 真拓扑）。"""
    builder = StateGraph(MainState)
    builder.add_node("plan", plan)
    builder.add_node("write", write)
    builder.add_edge(START, "plan")
    builder.add_edge("plan", "write")
    builder.add_edge("write", END)
    return builder.compile()


graph = build_deep_graph()
