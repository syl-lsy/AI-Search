"""桩流假图（B3 节点 5/5 synthesis 接真后——REST/SSE 流测试的确定性替身）。

ThreadScheduler 已支持 graph 注入（B2 起）；本模块提供脚本化假图：按 api.md §7.4 快速
主干产出完整事件序列（stage/sources 走 custom 流、token 走 messages 流——§6.1 token 域
收口后的真实承载形态），零 LLM/零真源/零网络依赖。

- FAKE_DELAY_S：运行窗口旋钮（monkeypatch 之制造心跳/取消/超时窗口；默认 0）；
- requires_llm / requires_search 标记用例豁免（走真节点链路，见 conftest autouse 夹具）。
"""

from typing import Any

#: 运行窗口旋钮（秒）：假图 search 阶段在此处睡眠——心跳/取消/超时语义测试用
FAKE_DELAY_S: float = 0.0

#: 假答案（token 拼接一致——SSE token_concat 断言基准）
FAKE_TOKEN_DELTAS: list[str] = [
    "【桩流答案】这是桩流假图固定返回的 Markdown 答案（确定性、零 LLM 依赖）。\n\n",
    "LangGraph 1.x 通过 StateGraph 编排节点[1]，token 经 messages 流承载。",
]
FAKE_ANSWER_MD: str = "".join(FAKE_TOKEN_DELTAS)

#: 假来源（1 条——与桩流断言 source_count==1 对齐；score 0.9 固定值）
FAKE_SOURCE: dict[str, Any] = {
    "source_id": "11111111-1111-4111-8111-111111111111",  # 占位（TurnRunner 以行 id 回写）
    "ref_no": 1,
    "source_type": "web",
    "title": "桩流来源：LangGraph 文档",
    "url": "https://stub.example/langgraph",
    "snippet": "桩流假图固定来源片段（确定性）。",
    "score": 0.9,
    "snapshot_available": False,
    "kb_doc_id": None,
}




class ModeRoutingFakeGraph:
    """按 mode 分流的桩流假图：fast → 脚本序列；deep → 真 deep 桩子图（B2 桩保持）。"""

    def __init__(self, real_graph: Any) -> None:
        self._real = real_graph

    def astream(self, payload: Any, config: Any = None, stream_mode: Any = None,
                subgraphs: bool = False) -> Any:
        if isinstance(payload, dict) and payload.get("mode") == "deep":
            return self._real.astream(payload, config=config, stream_mode=stream_mode,
                                      subgraphs=subgraphs)
        return FakeScriptedGraph().astream(payload, config=config, stream_mode=stream_mode,
                                           subgraphs=subgraphs)


class FakeScriptedGraph:
    """脚本化 fast 主干假图：astream 按 §7.4 序列产出 (namespace, stream_mode, chunk)。"""

    def astream(self, payload: Any, config: Any = None, stream_mode: Any = None,
                subgraphs: bool = False) -> Any:
        import asyncio

        delay = FAKE_DELAY_S

        async def gen():
            # rewrite（running 已由 TurnRunner 图前发布——§4.3，无重复 seq）
            yield (("fast:stub",), "custom", {"type": "stage", "stage_key": "rewrite", "status": "done"})
            # search（delay 制造运行窗口）
            yield (("fast:stub",), "custom", {"type": "stage", "stage_key": "search", "status": "running", "detail": "tavily（假体）"})
            if delay:
                await asyncio.sleep(delay)
            yield (("fast:stub",), "custom", {
                "type": "stage", "stage_key": "search", "status": "done",
                "progress": {"current": 1, "total": 1}, "detail": "tavily×1",
            })
            yield (("fast:stub",), "custom", {"type": "sources", "batch": [dict(FAKE_SOURCE)], "total": 1})
            # write（token 走 messages 流——§6.1）
            yield (("fast:stub",), "custom", {"type": "stage", "stage_key": "write", "status": "running", "label": "综合中"})
            for delta in FAKE_TOKEN_DELTAS:
                # 实测形态：messages 帧 namespace 为空、节点标识在 metadata（R14③ 过滤判定面）
                yield ((), "messages", (_chunk(delta), {"langgraph_node": "synthesis"}))
            yield (("fast:stub",), "custom", {"type": "stage", "stage_key": "write", "status": "done"})
            # 终态 updates（顶层）
            yield ((), "updates", {"write": {
                "answer_md": FAKE_ANSWER_MD,
                "rewritten_queries": ["桩改写检索 query"],
                "degraded": False,
            }})

        return gen()


def _chunk(text: str) -> Any:
    """最小 AIMessageChunk 形态（TurnRunner _extract_deltas 按 .content str 读）。"""
    from langchain_core.messages import AIMessageChunk

    return AIMessageChunk(content=text)
