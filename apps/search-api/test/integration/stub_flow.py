"""桩流假图（B3 节点 5/5 synthesis 接真后——REST/SSE 流测试的确定性替身）。

ThreadScheduler 已支持 graph 注入（B2 起）；本模块提供脚本化假图：按 api.md §7.4 快速
主干产出完整事件序列（stage/sources 走 custom 流、token 走 messages 流——§6.1 token 域
收口后的真实承载形态），零 LLM/零真源/零网络依赖。

- FAKE_DELAY_S：运行窗口旋钮（monkeypatch 之制造心跳/取消/超时窗口；默认 0）；
- FAKE_IMAGES：images 事件旋钮（A6——None=不发（默认）；非空 list 时在 sources 之后、
  write(running) 之前发射 `{"type": "images", "images": [...]}` custom 信号，
  api.md §7.4 快速序列 images(可选) 位置）；
- requires_llm / requires_search 标记用例豁免（走真节点链路，见 conftest autouse 夹具）。
"""

from typing import Any

#: 运行窗口旋钮（秒）：假图 search 阶段在此处睡眠——心跳/取消/超时语义测试用
FAKE_DELAY_S: float = 0.0

#: images 事件旋钮（A6）：None=不发（默认——api.md §7.2「无图不发」现状基线）；
#: 非 list|None 值非法（测试侧自律），list 非空时按 §7.4 位置发射
FAKE_IMAGES: list[dict[str, Any]] | None = None

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
            # images（可选——A6 旋钮开启时按 §7.4 位置发射：sources 之后、write(running) 之前）
            if FAKE_IMAGES:
                yield (("fast:stub",), "custom", {"type": "images", "images": list(FAKE_IMAGES)})
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


# ---- regenerate 微图假体（B11 P2 / api.md §7.4 regenerate 形态）----

#: 运行窗口旋钮（秒）：事件间睡眠——重复点击 409006 / 取消联动的确定性窗口
FAKE_REGENERATE_DELAY_S: float = 0.0

#: 失败旋钮：非 None 时在 write(running)+token 之后抛出（再失败 → error(500101)+degraded）
FAKE_REGENERATE_ERROR: Exception | None = None

#: 重生成答案（token 拼接一致——SSE token/对账断言基准；带 [1] 引用核对映射不变）
FAKE_REGENERATE_TOKEN_DELTAS: list[str] = ["【重生成答案】", "仅重跑综合，来源不变[1]。"]
FAKE_REGENERATE_ANSWER_MD: str = "".join(FAKE_REGENERATE_TOKEN_DELTAS)


class RegenerateFakeGraph:
    """regenerate 微图假体：stage(write) custom + token messages + updates(answer_md)。

    契约事件面（§7.4 regenerate 形态）：无 stage(rewrite)/stage(search)/sources/images
    ——不重检索；同一 turn 通道内第二个 done 由 TurnRunner 续发。
    """

    def astream(
        self, payload: Any, config: Any = None, stream_mode: Any = None,
        subgraphs: bool = False,
    ) -> Any:
        import asyncio

        async def gen():
            delay = FAKE_REGENERATE_DELAY_S
            error = FAKE_REGENERATE_ERROR
            yield ((), "custom", {
                "type": "stage", "stage_key": "write", "status": "running", "label": "综合中",
            })
            if delay:
                await asyncio.sleep(delay)
            if error is not None:
                raise error
            for delta in FAKE_REGENERATE_TOKEN_DELTAS:
                yield ((), "messages", (_chunk(delta), {"langgraph_node": "synthesis"}))
                if delay:
                    await asyncio.sleep(delay)
            yield ((), "custom", {"type": "stage", "stage_key": "write", "status": "done"})
            yield ((), "updates", {
                "synthesis": {"answer_md": FAKE_REGENERATE_ANSWER_MD, "degraded": False},
            })

        return gen()


# ---- deep 脚本化假图（M2 批次 5 deep SSE 联调——api.md §7.4 深度主干）----

#: deep 桩流运行窗口旋钮（秒）：深度段各阶段间睡眠——心跳/断线重连/取消窗口测试
FAKE_DEEP_DELAY_S: float = 0.0

#: deep guards 熔断降级旋钮：None=正常 writer 收束（completed）；"timeout"|"budget"
#: =降级路径收束（degraded_writer 同构形态——§7.2:845 guards trip 面，
#: TurnRunner 分流 done(finish=degraded, degraded_reason=同值) 零 error）
FAKE_DEEP_DEGRADED_REASON: str | None = None

#: deep 快照回填旋钮：None=updates 不带 snapshots（默认——backfill 零触发，免
#: snapshots 宿主行 FK）；非空=list 原样进 updates（测试侧须先播种 snapshots
#: 宿主行——Source.snapshot_id FK 指向 snapshots.id，回填 UPDATE 受约束）
FAKE_DEEP_SNAPSHOTS: list[dict[str, Any]] | None = None

#: deep usage 累计旋钮（dict|None）：非空时假图在 write 段经
#: collector.on_llm_end 注入 usage_metadata——done.usage / turns.usage 的
#: 数据源消费面（M2 批次 1 方案 A）；None=零累计（usage={} 旧形态）
FAKE_DEEP_USAGE: dict[str, int] | None = {
    "input_tokens": 320,
    "output_tokens": 180,
    "total_tokens": 500,
}

#: 假子课题（planner 产出——stage(plan,done,extra.subtopics) 与 extra.subagents 数据源）
DEEP_FAKE_SUBTOPICS: list[str] = [
    "子课题A：LangGraph 编排",
    "子课题B：deepagents 装配",
]

#: 假报告 token 增量（writer messages 流——拼接一致 == answer_md 断言基准）
DEEP_FAKE_TOKEN_DELTAS: list[str] = [
    "# 深度调研报告\n\n## 摘要\n\nLangGraph 是编排层，deepagents 在其上装配研究代理[1]。\n\n",
    "## 结论\n\n两者层次互补，按可控性需求选型[2]。",
]
DEEP_FAKE_ANSWER_MD: str = "".join(DEEP_FAKE_TOKEN_DELTAS)

#: 假来源 URL（批次账本：batch1=2 条、batch2=1 条、supplement batch3=1 条——ref_no 1..4）
DEEP_FAKE_SOURCE_URLS: list[str] = [
    "https://deep-stub.example/langgraph",
    "https://deep-stub.example/deepagents",
    "https://deep-stub.example/middleware",
    "https://deep-stub.example/supplement",
]

#: 假 context_compact 载荷（§7.2 四键钉面——summarized/released/retained/offloaded）
DEEP_FAKE_CONTEXT_COMPACT: dict[str, Any] = {
    "summarized_messages": 12,
    "released_tokens": 58000,
    "retained_tokens": 9000,
    "offloaded_files": [
        {"ref": "/large_tool_results/abc123.md", "url": DEEP_FAKE_SOURCE_URLS[0]}
    ],
}

#: 降级报告整块 token（degraded_writer 同构——机械前置 + 已收集内容速览）
DEEP_FAKE_DEGRADED_DIGEST: str = (
    "⚠️ 研究未完成（预算熔断，桩流降级报告）。\n\n## 已收集内容速览\n\n"
    "- [1] LangGraph 编排（deep-stub.example）\n"
    "- [2] deepagents 装配（deep-stub.example）"
)


def _deep_source_item(url: str, ref_no: int) -> dict[str, Any]:
    """单条来源 payload（9 键钉面——source_id 占位，TurnRunner 以行 id 回写）。"""
    return {
        "source_id": "22222222-2222-4222-8222-222222222222",  # 占位（TurnRunner 回写）
        "ref_no": ref_no,
        "source_type": "web",
        "title": f"深度桩流来源：{url.rsplit('/', 1)[-1]}",
        "url": url,
        "snippet": "深度桩流假图固定来源片段（确定性）。",
        "score": 0.9,
        "snapshot_available": False,
        "kb_doc_id": None,
    }


def _usage_response(usage: dict[str, int]) -> Any:
    """collector.on_llm_end 消费的最小 LLMResult 形态（usage_metadata 读面）。

    UsageCollector.on_llm_end 读 generations[*].message.usage_metadata（键位双形态
    input_tokens/prompt_tokens）——桩图直接注入等价于真实 on_llm_end callback 面。
    """
    from types import SimpleNamespace

    message = SimpleNamespace(usage_metadata=dict(usage))
    generation = SimpleNamespace(message=message)
    return SimpleNamespace(generations=[[generation]])


class DeepFakeScriptedGraph:
    """脚本化 deep 主干假图（M2 批次 5 deep SSE 联调载体——对标 FakeScriptedGraph）：

    astream 按 api.md §7.4 深度序列产出 (namespace, stream_mode, chunk)——零 LLM/
    零真源/零网络。形态对齐真图实测面：
    - custom 信号 namespace=("deep:stub",)（主图壳嵌套单段形态）；
    - messages 帧 metadata.langgraph_node="writer"（_messages_gate deep 侧唯一
      放行判据——turn_service._is_deep_writer_messages）；
    - updates 零段 namespace、值形态 {"deep": {...}}（主图壳子图投影回填形态）。

    旋钮（monkeypatch stub_flow 模块属性）：FAKE_DEEP_DELAY_S（运行窗口）/
    FAKE_DEEP_DEGRADED_REASON（guards 熔断降级形态）/ FAKE_DEEP_SNAPSHOTS（快照
    回填）/ FAKE_DEEP_USAGE（usage 累计注入）。context_compact 恒发射一次
    （§7.2 v1.3 research/reflect 长阶段——重放边界与 publish-only 断言载体）。
    """

    def astream(self, payload: Any, config: Any = None, stream_mode: Any = None,
                subgraphs: bool = False) -> Any:
        if isinstance(payload, dict) and payload.get("mode") != "deep":
            # 非 deep 分流回落 fast 脚本假图（本假体替换整个调度器 graph 时的保形面）
            return FakeScriptedGraph().astream(payload, config=config, stream_mode=stream_mode,
                                               subgraphs=subgraphs)
        import asyncio

        delay = FAKE_DEEP_DELAY_S
        degraded_reason = FAKE_DEEP_DEGRADED_REASON
        snapshots = FAKE_DEEP_SNAPSHOTS
        usage = FAKE_DEEP_USAGE

        async def gen():
            def _emit(signal: dict[str, Any]) -> tuple[Any, str, dict[str, Any]]:
                return (("deep:stub",), "custom", signal)

            # plan done（running 归 TurnRunner 图前发布——deep.py 模块 docstring 序列注）
            yield _emit({
                "type": "stage", "stage_key": "plan", "status": "done",
                "extra": {"subtopics": len(DEEP_FAKE_SUBTOPICS)},
            })
            # search running 入口帧（extra.subagents 全 running——§7.2 枚举形态）
            yield _emit({
                "type": "stage", "stage_key": "search", "status": "running",
                "extra": {"subagents": [{"subtopic": t, "status": "running"} for t in DEEP_FAKE_SUBTOPICS]},
            })
            if delay:
                await asyncio.sleep(delay)
            # sources 分批（§7.4：batch1/batch2——ref_no 1..3 首现冻结）
            for ref_no, url in enumerate(DEEP_FAKE_SOURCE_URLS[:3], start=1):
                yield _emit({
                    "type": "sources",
                    "batch": [_deep_source_item(url, ref_no)],
                    "total": ref_no,
                })
            # 子任务状态推进（§7.2「同 key 重复发射合法」——research complete() 面：
            # 每报告收口一推，终帧全 done）
            yield _emit({
                "type": "stage", "stage_key": "search", "status": "running",
                "extra": {"subagents": [
                    {"subtopic": DEEP_FAKE_SUBTOPICS[0], "status": "done"},
                    {"subtopic": DEEP_FAKE_SUBTOPICS[1], "status": "running"},
                ]},
            })
            yield _emit({
                "type": "stage", "stage_key": "search", "status": "running",
                "extra": {"subagents": [
                    {"subtopic": DEEP_FAKE_SUBTOPICS[0], "status": "done"},
                    {"subtopic": DEEP_FAKE_SUBTOPICS[1], "status": "done"},
                ]},
            })
            # read 段（§7.4:874 label 形态）+ context_compact（research 长阶段）
            yield _emit({"type": "stage", "stage_key": "read", "status": "running", "label": "阅读第 1 篇"})
            if delay:
                await asyncio.sleep(delay)
            yield _emit({"type": "context_compact", **DEEP_FAKE_CONTEXT_COMPACT})
            yield _emit({
                "type": "stage", "stage_key": "read", "status": "done",
                "progress": {"current": 1, "total": 1},
            })
            # reflect⇄supplement 回路（§7.4:875 相对序：reflect running → search running
            # （supplement 段）→ sources 增量批次 → reflect done）
            yield _emit({
                "type": "stage", "stage_key": "reflect", "status": "running",
                "label": "反思第 1/2 轮", "detail": "补搜方向：中间件装配形态",
            })
            yield _emit({"type": "stage", "stage_key": "search", "status": "running"})
            if delay:
                await asyncio.sleep(delay)
            yield _emit({
                "type": "sources",
                "batch": [_deep_source_item(DEEP_FAKE_SOURCE_URLS[3], 4)],
                "total": 4,
            })
            yield _emit({"type": "stage", "stage_key": "reflect", "status": "done"})

            collector = ((config or {}).get("configurable") or {}).get("usage_collector")
            if degraded_reason is not None:
                # 降级路径（guards trip → degraded_writer 同构形态）：fallback_token
                # 整块单事件 + write done 裸帧（无 extra.sections——两路径判别面）+
                # updates 带 degraded_reason（TurnRunner guard_degraded 分流消费）
                yield _emit({"type": "stage", "stage_key": "write", "status": "running", "label": "撰写降级报告"})
                if usage is not None and collector is not None:
                    collector.on_llm_end(_usage_response(usage))
                yield _emit({"type": "fallback_token", "delta": DEEP_FAKE_DEGRADED_DIGEST})
                if delay:
                    await asyncio.sleep(delay)
                yield _emit({"type": "stage", "stage_key": "write", "status": "done"})
                yield ((), "updates", {"deep": {
                    "answer_md": DEEP_FAKE_DEGRADED_DIGEST,
                    "rewritten_queries": ["深度桩流补搜 query"],
                    "degraded": False,
                    "degraded_reason": degraded_reason,
                }})
                return
            # 正常 writer 段（§7.4:876：write running → token…token → write done extra.sections）
            yield _emit({"type": "stage", "stage_key": "write", "status": "running", "label": "撰写第 1 节"})
            for delta in DEEP_FAKE_TOKEN_DELTAS:
                yield (("deep:stub",), "messages", (_chunk(delta), {"langgraph_node": "writer"}))
                if delay:
                    await asyncio.sleep(delay)
            if usage is not None and collector is not None:
                collector.on_llm_end(_usage_response(usage))
            yield _emit({
                "type": "stage", "stage_key": "write", "status": "done",
                "extra": {"sections": 2},
            })
            updates: dict[str, Any] = {
                "answer_md": DEEP_FAKE_ANSWER_MD,
                "rewritten_queries": ["深度桩流改写检索 query"],
                "degraded": False,
            }
            if snapshots is not None:
                updates["snapshots"] = snapshots
            yield ((), "updates", {"deep": updates})

        return gen()


# ---- deep regenerate 微图假体（api.md §2.12 v1.28 / §7.4 深度 regenerate 形态）----

#: deep 重生成运行窗口旋钮（秒）——重复点击/取消联动的确定性窗口
FAKE_DEEP_REGENERATE_DELAY_S: float = 0.0

#: deep 重生成失败旋钮：非 None 时走 §7.4:946 再失败形态（writer 兜底——digest 全文
#: 经 fallback_token 整块 + stage done extra.fallback + errors 标记 updates，
#: TurnRunner 据标记分流兜底产出收束 degraded）
FAKE_DEEP_REGENERATE_FALLBACK: str | None = None

#: deep 重生成答案（writer token 拼接一致——SSE token/对账断言基准）
FAKE_DEEP_REGENERATE_TOKEN_DELTAS: list[str] = ["【深度重生成报告】", "按既有来源重新撰写[1]。"]
FAKE_DEEP_REGENERATE_ANSWER_MD: str = "".join(FAKE_DEEP_REGENERATE_TOKEN_DELTAS)


class DeepRegenerateFakeGraph:
    """deep regenerate 微图假体（api.md §7.4 深度 regenerate 形态 :937）：

    stage(write,running,label=撰写报告) custom → writer token（messages 域——
    零段 namespace + metadata.langgraph_node="writer"，与真微图直跑形态一致）→
    stage(write,done,extra.sections) custom → updates(writer.answer_md)。
    无 plan/search/read/reflect 段（不重检索、不重规划）；第二 done 由 TurnRunner 续发。
    """

    def astream(
        self, payload: Any, config: Any = None, stream_mode: Any = None,
        subgraphs: bool = False,
    ) -> Any:
        import asyncio

        async def gen():
            delay = FAKE_DEEP_REGENERATE_DELAY_S
            fallback = FAKE_DEEP_REGENERATE_FALLBACK
            yield ((), "custom", {
                "type": "stage", "stage_key": "write", "status": "running", "label": "撰写报告",
            })
            if delay:
                await asyncio.sleep(delay)
            if fallback is not None:
                yield ((), "custom", {"type": "fallback_token", "delta": fallback})
                yield ((), "custom", {
                    "type": "stage", "stage_key": "write", "status": "done",
                    "extra": {"fallback": "synthesis_digest"},
                })
                yield ((), "updates", {
                    "writer": {
                        "answer_md": fallback,
                        "errors": [{"stage": "write", "reason": "llm_call_failed"}],
                    },
                })
                return
            for delta in FAKE_DEEP_REGENERATE_TOKEN_DELTAS:
                yield ((), "messages", (_chunk(delta), {"langgraph_node": "writer"}))
                if delay:
                    await asyncio.sleep(delay)
            yield ((), "custom", {
                "type": "stage", "stage_key": "write", "status": "done", "extra": {"sections": 0},
            })
            yield ((), "updates", {"writer": {"answer_md": FAKE_DEEP_REGENERATE_ANSWER_MD}})

        return gen()

