"""fast 模式桩子图（B2 桩，B3 起逐节点替换为真图——ARCH §4.1）。

节点序列 rewrite → search → write（桩节点名 = api.md §7.2 stage_key），发射的
custom 信号序列对齐 api.md §7.4 快速主干（710 行）：

    stage(rewrite,r) → stage(rewrite,d) → stage(search,r) → stage(search,d)
    → sources(batch) → stage(write,running,label=综合中) → token…token → stage(write,d)

- done / images 不由图发射：done 归 TurnRunner 落库终态后自发布；images 可选，
  桩不发（B3 检索实装后按 OQ-6 判定）；
- 桩不接真模型、不读 Nacos spec.routing（B3/B4 事）；节点纯函数，持久化零介入；
- `graph` 模块级编译产物：langgraph.json `fast` 入口与 registry 注册共用。

search 节点可控 delay（测试演练短事务/取消语义用）：优先读 RunnableConfig
configurable 的 `delay` 键（TurnRunner 传 config 可控）；缺省读模块常量
`SEARCH_DELAY_S`（默认 0.0，测试可 monkeypatch）。
"""

import time
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from app.agent.graphs.signals import emit_sources, emit_stage, emit_token
from app.agent.state import MainState

#: search 节点缺省延时（秒）——模块级常量，测试可 monkeypatch
SEARCH_DELAY_S: float = 0.0

#: 固定 2 条 web 来源（结构与 api.md §7.2 sources 一致）；source_id 为图侧占位值——
#: TurnRunner 以 sources 行 id 为单一事实源回写事件 batch（turn_service.py，§1.5）
STUB_FAST_SOURCES: list[dict[str, Any]] = [
    {
        "source_id": "11111111-1111-4111-8111-111111111111",
        "ref_no": 1,
        "source_type": "web",
        "title": "LangGraph 官方文档：StateGraph API",
        "url": "https://langchain-ai.github.io/langgraph/",
        "snippet": "StateGraph 以节点与边编排 LLM 应用，条件边在运行期按 state 动态选路。",
        "score": 0.91,
        "snapshot_available": False,
        "kb_doc_id": None,
    },
    {
        "source_id": "22222222-2222-4222-8222-222222222222",
        "ref_no": 2,
        "source_type": "web",
        "title": "LangGraph 官方文档：Streaming",
        "url": "https://langchain-ai.github.io/langgraph/concepts/streaming/",
        "snippet": "stream_mode=custom 允许节点内向下游推送应用自定义事件。",
        "score": 0.87,
        "snapshot_available": False,
        "kb_doc_id": None,
    },
]

#: 固定 token 增量 3 段（练习 token 不落库路径）；答案 = 增量拼接，保证一致性
STUB_FAST_TOKEN_DELTAS: list[str] = [
    "【桩答案·B2】这是桩主图固定返回的 Markdown 答案（不接真模型，B4 接入）。\n\n",
    "LangGraph 1.x 通过 StateGraph 编排节点，条件边在运行期按 state 动态选路[1]。",
    "节点内经 get_stream_writer 推送 custom 信号，由 TurnRunner 统一翻译为 SSE 事件[2]。",
]

STUB_FAST_ANSWER_MD: str = "".join(STUB_FAST_TOKEN_DELTAS)


def _resolve_search_delay(config: RunnableConfig | None) -> float:
    """delay 两途径：configurable["delay"] 优先，缺省回落模块常量 SEARCH_DELAY_S。"""
    configurable = (config or {}).get("configurable") or {}
    raw = configurable.get("delay")
    if raw is not None:
        return float(raw)
    return SEARCH_DELAY_S


def rewrite(state: MainState) -> dict[str, Any]:
    """意图改写桩：stage(rewrite, running→done)，无 LLM（B3 接 light 模型）。"""
    emit_stage("rewrite", "running")
    emit_stage("rewrite", "done")
    return {}


def search(state: MainState, config: RunnableConfig) -> dict[str, Any]:
    """搜索桩：stage(search, running→done) + 固定 sources；delay 在两态之间模拟耗时。"""
    emit_stage("search", "running")
    delay = _resolve_search_delay(config)
    if delay > 0:
        time.sleep(delay)
    emit_stage("search", "done")
    emit_sources(batch=STUB_FAST_SOURCES, total=len(STUB_FAST_SOURCES))
    return {"sources": list(STUB_FAST_SOURCES)}


def write(state: MainState) -> dict[str, Any]:
    """综合桩：stage(write, running→done) 夹 token 流；终态 answer_md 固定 Markdown。"""
    emit_stage("write", "running", label="综合中")
    for delta in STUB_FAST_TOKEN_DELTAS:
        emit_token(delta)
    emit_stage("write", "done")
    return {"answer_md": STUB_FAST_ANSWER_MD}


def build_fast_graph() -> Any:
    """编译 fast 桩子图（B3 起各节点替换为真实现，拓扑按 §4.1 演进）。"""
    builder = StateGraph(MainState)
    builder.add_node("rewrite", rewrite)
    builder.add_node("search", search)
    builder.add_node("write", write)
    builder.add_edge(START, "rewrite")
    builder.add_edge("rewrite", "search")
    builder.add_edge("search", "write")
    builder.add_edge("write", END)
    return builder.compile()


graph = build_fast_graph()
