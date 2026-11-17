"""OQ-33 探针：deepagents 0.7.32 per-subagent 工具收窄能力最小可运行复现。

结论前置（源码证据见 docs/m2-batch2-probe-oq33-oq34.md）：
- SubAgent TypedDict（deepagents/middleware/subagents.py:84-227）必填键 {name,description}，
  可选 ``tools``（NotRequired，:112，缺省继承主 agent 工具）；**无 custom_tools 字段**（grep 全包零命中）。
- create_deep_agent 装配面（deepagents/graph.py）：
  - :805 ``raw_subagent_tools = spec.get("tools") if "tools" in spec else tools``
    → per-subagent 工具收窄原生支持（spec["tools"] 出现即覆盖继承）。
  - :736-744 每个声明式子代理默认栈**无条件前置 FilesystemMiddleware** → 文件工具默认泄漏进子代理可见面。
  - :840-841 默认自动追加 ``general-purpose`` 子代理（继承主 agent 全量工具），除非
    调用方自带同名 spec 顶替（create_deep_agent 无 general_purpose_subagent 直传参数，
    GP 开关在 model-based harness profile 注册表内，自定义模型一律落默认空 profile=开）。
- 收窄入口（SubAgent docstring :149-151 + graph.py:222-249 _apply_custom_middleware 按 .name 原位替换）：
  SubAgent["middleware"] 里放自己的 ``FilesystemMiddleware(tools=[...])`` 替换默认栈。
  FilesystemMiddleware tools 白名单（middleware/filesystem.py:1902/:2012-2016/:2027-2044，
  FsToolName :1608）= {ls,read_file,write_file,edit_file,delete,glob,grep,execute}，read_file 必含（:1969-1971）。
- 卸载取回只读形态：大结果 eviction 在 wrap_model_call 钩子内经 backend.write 落盘
  （filesystem.py:3442 + middleware/_message_eviction.py:275），与 write_file 工具是否暴露无关
  → tools=["ls","read_file","glob","grep"] 即「模型侧只读 + 自动卸载回路完好」。

本脚本四个 Phase（红绿互证）：
  Phase 1  默认装配 vs 契约要求 → 预期 RED（文件工具泄漏 + GP 泄漏）
  Phase 2  契约装配（middleware 覆盖 + 同名 GP 顶替）→ GREEN（可见面=3 工具+只读文件工具）
  Phase 3  只读形态下卸载回路功能性验证（oversized 工具结果→落盘→read_file 取回）
  Phase 4  边界：read_file 必含白名单（ValueError）+ SubAgent 必填键内省

零网络零真 LLM：模型用 GenericFakeChatModel 脚本桩。
运行：uv run --package search-api python scripts/probe_oq33_subagent_tools.py
"""

from __future__ import annotations

import sys
from typing import Any, cast

from deepagents import SubAgent, create_deep_agent
from deepagents.backends import StateBackend
from deepagents.middleware.filesystem import FilesystemMiddleware, FsToolName
from deepagents.middleware.subagents import create_sub_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

READ_ONLY_FS_TOOLS: list[FsToolName] = ["ls", "read_file", "glob", "grep"]
CONTRACT_TOOL_NAMES = {"web_search", "firecrawl_read", "save_snapshot"}
EXPECTED_SUBAGENT_FACE = CONTRACT_TOOL_NAMES | set(READ_ONLY_FS_TOOLS)


def make_mock_tools() -> list:
    @tool
    def web_search(query: str) -> str:
        """Mock web search tool (contract deep tool #1)."""
        return "result: mock-search-hit"

    @tool
    def firecrawl_read(url: str) -> str:
        """Mock url reader tool (contract deep tool #2)."""
        return "content: mock-page-body"

    @tool
    def save_snapshot(url: str, content: str) -> str:
        """Mock snapshot tool (contract deep tool #3)."""
        return "saved: mock-ref"

    return [web_search, firecrawl_read, save_snapshot]


class _ScriptedModel(GenericFakeChatModel):
    """GenericFakeChatModel + bind_tools（基类未实现，chat_models.py:2426）。

    bind_tools 返回 self：探针只断言结构面（ToolNode 可见工具），不断言模型侧绑定。
    """

    def bind_tools(self, tools: Any, **kwargs: Any):  # noqa: ANN201
        return self


def scripted_model(calls: list[dict], final_text: str = "done") -> GenericFakeChatModel:
    """脚本桩模型：按给定 tool_calls 序列依次产出 AIMessage，结尾纯文本。"""
    msgs: list[AIMessage] = [AIMessage(content="", tool_calls=[tc]) for tc in calls]
    msgs.append(AIMessage(content=final_text))
    return _ScriptedModel(messages=iter(msgs))


def introspect(agent_graph: Any, subagent_name: str) -> tuple[list[str], list[str]]:
    """内省主图 task 工具闭包 → subagent_graphs → 子代理 ToolNode 可见工具面。

    闭包结构：_build_task_tool 以 subagent_graphs（name→compiled runnable）捕获于
    task/atask 协程（subagents.py:898-902/:904-911）。主图 tools 节点 ToolNode
    的 tools_by_name 挂 task StructuredTool。
    """
    tool_node = agent_graph.nodes["tools"].bound
    task_tool = tool_node.tools_by_name["task"]
    graphs = None
    for cell in task_tool.coroutine.__closure__ or []:
        v = cell.cell_contents
        if isinstance(v, dict) and v and all(hasattr(x, "ainvoke") for x in v.values()):
            graphs = v
    if graphs is None:
        raise AssertionError("task 工具闭包内未找到 subagent_graphs（上游结构变更？）")
    sub_tool_map = graphs[subagent_name].nodes["tools"].bound.tools_by_name
    return sorted(sub_tool_map.keys()), sorted(graphs.keys())


def phase1_default_form() -> list[str]:
    """Phase 1（预期 RED）：默认装配（不做收窄）对契约要求断言 → 记录失败项。"""
    failures: list[str] = []
    model = scripted_model([])
    researcher: SubAgent = {
        "name": "researcher",
        "description": "research subtopic",
        "system_prompt": "You are a researcher.",
        "model": model,
        "tools": make_mock_tools(),  # 只给 3 工具，未动 middleware
    }
    agent = create_deep_agent(model=model, tools=[], subagents=[researcher], system_prompt="main")
    face, all_subs = introspect(agent, "researcher")
    leaked = sorted(set(face) - EXPECTED_SUBAGENT_FACE)
    if leaked:
        failures.append(
            f"[泄漏] 默认装配子代理可见面多出文件工具 {leaked}（契约要求=3 工具+只读文件工具）"
            f"——证据：graph.py:736-744 默认栈无条件前置 FilesystemMiddleware"
        )
    if "general-purpose" in all_subs:
        failures.append(
            f"[泄漏] 默认自动追加 general-purpose 子代理（subagents={all_subs}）"
            f"——证据：graph.py:840-841 + create_deep_agent 无 GP 直传开关"
        )
    return failures


def phase2_contract_form() -> list[str]:
    """Phase 2（预期 GREEN）：契约装配 → 子代理可见面=指定集。"""
    failures: list[str] = []
    model = scripted_model([])
    researcher: SubAgent = {
        "name": "researcher",
        "description": "research subtopic",
        "system_prompt": "You are a researcher.",
        "model": model,
        "tools": make_mock_tools(),
        # cast：deepagents 0.7.32 上游 typing 缺口——FilesystemMiddleware[None, Any] 因
        # StateT 不变性装不进 SubAgent.middleware: list[AgentMiddleware]（运行时形态正确）
        "middleware": cast(
            list[AgentMiddleware],
            [
                FilesystemMiddleware(  # 按 .name 原位替换默认 FS 中间件（graph.py:222-249）
                    backend=StateBackend(), tools=READ_ONLY_FS_TOOLS
                )
            ],
        ),
    }
    gp_override: SubAgent = {  # 同名顶替自动 GP（graph.py:841 not any(name==...) 分支）
        "name": "general-purpose",
        "description": "reserved",
        "system_prompt": "",
        "model": model,
        "tools": [],
        "middleware": cast(
            list[AgentMiddleware],
            [FilesystemMiddleware(backend=StateBackend(), tools=READ_ONLY_FS_TOOLS)],
        ),
    }
    agent = create_deep_agent(model=model, tools=[], subagents=[researcher, gp_override], system_prompt="main")

    face, all_subs = introspect(agent, "researcher")
    if set(face) != EXPECTED_SUBAGENT_FACE:
        failures.append(f"[收窄失败] researcher 可见面={face} 期望={sorted(EXPECTED_SUBAGENT_FACE)}")
    for banned in ("write_file", "edit_file", "delete", "execute"):
        if banned in face:
            failures.append(f"[收窄失败] researcher 可见面含写工具 {banned}")
    gp_face, _ = introspect(agent, "general-purpose")
    if set(gp_face) != set(READ_ONLY_FS_TOOLS):
        failures.append(f"[GP 顶替失败] general-purpose 可见面={gp_face} 期望={READ_ONLY_FS_TOOLS}")

    # 主 agent 面：task 工具在位 + 文件工具全集（主面不收窄）
    # cast：create_deep_agent 返回类型上 bound 为 Runnable，ToolNode.tools_by_name 未在类型面暴露
    main_face = sorted(cast(Any, agent.nodes["tools"].bound).tools_by_name.keys())
    if "task" not in main_face or "write_file" not in main_face:
        failures.append(f"[主面异常] 主 agent 工具面={main_face}")
    return failures


def phase3_offload_readonly() -> list[str]:
    """Phase 3（预期 GREEN）：只读形态下卸载回路功能性验证。

    oversized 工具结果经 wrap_model_call eviction 落 backend（backend.write，与
    write_file 暴露无关），ToolMessage 被替换为截断预览；read_file 可取回全量。
    """
    failures: list[str] = []

    @tool
    def big_result() -> str:
        """Return an oversized payload to trigger eviction."""
        return "x" * 4096

    mw = FilesystemMiddleware(
        backend=StateBackend(), tools=READ_ONLY_FS_TOOLS, tool_token_limit_before_evict=64
    )
    face = sorted(t.name for t in mw.tools)
    if set(face) != set(READ_ONLY_FS_TOOLS):
        failures.append(f"[白名单失败] FilesystemMiddleware.tools={face}")

    sub = create_sub_agent(
        {
            "name": "r",
            "description": "d",
            "system_prompt": "",
            "model": scripted_model([{"name": "big_result", "args": {}, "id": "c1"}]),
            "tools": [big_result],
            "middleware": cast(list[AgentMiddleware], [mw]),  # 上游 typing 缺口，同上
        }
    )
    result = sub.invoke({"messages": [HumanMessage("go")]})
    big_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "big_result"]
    if not big_msgs:
        failures.append("[卸载失败] 未找到 big_result ToolMessage")
        return failures
    content = big_msgs[0].text if hasattr(big_msgs[0], "text") else str(big_msgs[0].content)
    evicted = ("large_tool_results" in content) or ("truncated" in content.lower())
    files_state: dict = result.get("files") or {}
    stored = any("large_tool_results" in k for k in files_state)
    if not (evicted and stored):
        failures.append(
            f"[卸载失败] evicted_marker={evicted} state_stored={stored} "
            f"content_head={content[:120]!r} files_keys={sorted(files_state)[:3]}"
        )
        return failures
    path = next(k for k in files_state if "large_tool_results" in k)
    stored = files_state[path]
    stored_content = stored["content"] if isinstance(stored, dict) else stored  # StateBackend 存 FileData dict
    if stored_content != "x" * 4096:
        failures.append(f"[取回失败] 落盘内容非全量（head={str(stored_content)[:60]!r}）")
    return failures


def phase4_edges() -> list[str]:
    """Phase 4：边界语义——read_file 必含白名单 + SubAgent 必填键/无 custom_tools。"""
    failures: list[str] = []
    try:
        FilesystemMiddleware(tools=["ls", "glob", "grep"])  # 缺 read_file
        failures.append("[边界失败] 缺 read_file 未触发 ValueError（filesystem.py:1969-1971）")
    except ValueError:
        pass
    required = getattr(SubAgent, "__required_keys__", frozenset())
    optional = getattr(SubAgent, "__optional_keys__", frozenset())
    if required != {"name", "description"}:
        failures.append(f"[边界失败] SubAgent 必填键={sorted(required)} 期望 ['description','name']")
    if "tools" not in optional:
        failures.append("[边界失败] tools 不在 SubAgent 可选键中")
    if "custom_tools" in (required | optional):
        failures.append("[边界失败] custom_tools 字段存在？（0.7.32 应无）")
    return failures


def main() -> int:
    results: dict[str, list[str]] = {
        "Phase1_default_form(预期RED)": phase1_default_form(),
        "Phase2_contract_form(预期GREEN)": phase2_contract_form(),
        "Phase3_offload_readonly(预期GREEN)": phase3_offload_readonly(),
        "Phase4_edges(预期GREEN)": phase4_edges(),
    }
    print("=" * 72)
    exit_code = 0
    for name, fails in results.items():
        status = "PASS" if not fails else "FAIL"
        if fails and "预期RED" not in name:
            exit_code = 1
        print(f"{name}: {status}")
        for f in fails:
            print(f"  - {f}")
    print("=" * 72)
    if results["Phase1_default_form(预期RED)"]:
        print("RED 侧成立：默认装配确有文件工具/GP 泄漏 → 收窄必要性强证据。")
    else:
        print("异常：Phase1 未复现泄漏（上游行为变更，需重审源码结论）")
        exit_code = 1
    if exit_code == 0:
        print("GREEN 侧成立：契约装配形态（middleware 覆盖 + 同名 GP 顶替 + 只读白名单）原生可行。")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
