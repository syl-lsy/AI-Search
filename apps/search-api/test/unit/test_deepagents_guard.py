"""unit 层：deepagents 0.7.32 公共 API 护栏（R8 同款「锁版本 + 单测护栏」，M2 批次 0）。

护栏目的：升级 deepagents/langgraph 前先跑本文件，防公共 API 断裂
（apps/search-api/pyproject.toml 已双双 == 锁定，见 test_versions_match_pins）。

契约名 → 实际名映射（deepagents 0.7.32 以 import 实测为准，2026-11-16）：
- create_deep_agent    → deepagents.create_deep_agent（顶层导出，同名不变）；
- SubAgentMiddleware   → deepagents.SubAgentMiddleware（顶层导出，同名不变）；
- FilesystemMiddleware → deepagents.FilesystemMiddleware（顶层导出，同名不变）；
- TodoListMiddleware   → 0.7.32 顶层导出面无 todo（`__all__` 实证）；todo 面由
  langchain.agents.middleware.TodoListMiddleware 承载（deepagents 自身 harness profile
  亦从该处 import——`profiles/harness/_openai_codex.py:19`，2026-11-16 复核实测）。
  若未来 deepagents 重新自带 todo 面，升级者应回改本映射并重跑本护栏。

零网络零 mock：编译测试用 GenericFakeChatModel 桩模型（构造不触网），
create_deep_agent 仅编译不执行；模块级 import 即触发，导入断裂在收集期即红。
"""

import importlib.metadata

from langchain.agents.middleware import TodoListMiddleware
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.graph.state import CompiledStateGraph

from deepagents import (
    FilesystemMiddleware,
    SubAgentMiddleware,
    create_deep_agent,
)

#: 版本契约（与 apps/search-api/pyproject.toml 的 == 锁定值一致；升版须同步改两处并重跑本护栏）
_DEEPAGENTS_PIN = "0.7.32"
_LANGGRAPH_PIN = "1.2.19"


def test_create_deep_agent_importable() -> None:
    """测试 A：create_deep_agent 经模块级 import 实测可导入且可调用
    （M2 deep 子图的唯一入口 API）。"""
    assert callable(create_deep_agent)


def test_public_middleware_importable() -> None:
    """测试 B：公共 middleware 三面可导入——SubAgentMiddleware / FilesystemMiddleware
    在 deepagents 顶层同名在位；TodoListMiddleware 按 docstring 映射断言 langchain
    侧等价公共 API（0.7.32 自身已无 todo 面）。"""
    for cls in (SubAgentMiddleware, FilesystemMiddleware, TodoListMiddleware):
        assert isinstance(cls, type)


def test_versions_match_pins() -> None:
    """测试 C：实测安装版本与 pyproject == 锁定值一致（venv 与锁漂移即红）。"""
    assert importlib.metadata.version("deepagents") == _DEEPAGENTS_PIN
    assert importlib.metadata.version("langgraph") == _LANGGRAPH_PIN


def test_minimal_deep_agent_compiles() -> None:
    """测试 D：最小 deep agent 可编译（零网络桩模型 + 1 工具 + 1 最小 subagent）——
    公共入口到 CompiledStateGraph 全链路通，核心节点 model/tools 在位
    （不断言具体节点全集：中间件内部节点属实现细节，向上升级不因内部改名误红）。"""

    @tool
    def echo(text: str) -> str:
        """echo back"""
        return text

    model = GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))
    agent = create_deep_agent(
        model=model,
        tools=[echo],
        subagents=[{"name": "researcher", "description": "guard probe"}],
    )
    assert isinstance(agent, CompiledStateGraph)
    assert {"model", "tools"} <= set(agent.nodes)
