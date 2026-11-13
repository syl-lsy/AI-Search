"""unit 层：LangGraph Studio 工具接线（langgraph.json 入口形态 + 单次编译不变量）。

钉住的语义（本工单验收，逐条对应测试）：
- langgraph.json 三入口用**模块路径形态**（`app.agent.graphs.<mod>:graph`）——
  langgraph_api 对模块路径 entry 走 `importlib.import_module`（命中 `sys.modules`，
  模块单例生效）；对文件路径 entry（`./app/agent/graphs/main.py:graph`）走
  `spec_from_file_location + exec_module`（绕过 `sys.modules`）——文件路径形态下
  Studio 侧重复编译与 registry 侧各成一份，启动期三图共编译 5 次（main 1 + fast 2 +
  deep 2），违反 `app/agent/graphs/registry.py` docstring「import 即编译一次、进程内
  单例复用」不变量（ARCH §4.0 / ADR-012）；
- 同一性不变量：GRAPH_REGISTRY 注册的图对象与各图模块级 `graph` **同实例**——
  Studio 暴露的图与 TurnRunner 经 GRAPH_REGISTRY 消费的图是同一编译产物。

零网络零 mock：import 图模块即触发编译，但 import 链零网络（Nacos/DB/LLM 全懒加载，
registry.py docstring 同口径），直接裸 import 断言即可。
"""

import json
from pathlib import Path
from typing import Any

from app.agent.graphs import deep as deep_mod
from app.agent.graphs import fast as fast_mod
from app.agent.graphs import main as main_mod
from app.agent.graphs import registry as registry_mod

#: langgraph.json 位置（相对本测试文件定位，不依赖运行 cwd——Makefile 命令一律从仓库根
#: 跑，但单测不应假设调用方 cwd）
_LANGGRAPH_JSON = Path(__file__).resolve().parents[2] / "langgraph.json"


def test_studio_entries_use_module_path_form() -> None:
    """测试 A：graphs 恰三键 main/fast/deep，且每条 entry 为 module:variable 模块路径
    形态（当前文件路径形态下此断言红——`./app/...` 前缀不等于模块路径）。"""
    config: dict[str, Any] = json.loads(_LANGGRAPH_JSON.read_text(encoding="utf-8"))
    assert set(config["graphs"]) == {"main", "fast", "deep"}
    for name in ("main", "fast", "deep"):
        assert config["graphs"][name] == f"app.agent.graphs.{name}:graph"


def test_registry_graphs_are_module_singletons() -> None:
    """测试 B（同一性/单次编译不变量）：registry 两键与图模块级 graph 同实例；
    main 模块 import 产物为编译图（含 nodes）；main 模块 import 后 registry 两键
    不变（主图壳不向 registry 注册，注册面仅 fast/deep——ARCH §4.0）。"""
    assert registry_mod.GRAPH_REGISTRY["fast"] is fast_mod.graph
    assert registry_mod.GRAPH_REGISTRY["deep"] is deep_mod.graph
    assert hasattr(main_mod.graph, "nodes")  # CompiledStateGraph 编译产物
    assert set(registry_mod.GRAPH_REGISTRY) == {"fast", "deep"}
