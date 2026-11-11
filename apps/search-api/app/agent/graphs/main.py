"""主图壳（B3 第 0 步接真：ARCH §4.0 / ADR-012 / DEV §5.1）。

单入口编译图：按 GRAPH_REGISTRY 迭代挂子图节点（拓扑零改动口径），条件边
`route_by_mode` 读**轮内 spec.routing 快照**（`state["spec"]`，TurnRunner 发起时
捕获注入——OQ-11 同源，热更仅对新轮生效）∩ GRAPH_REGISTRY 分发——亚毫秒、零
LLM·IO、**不发任何 SSE 信号**（api.md §7 stage_key 枚举零变化）。

双层兜底（ARCH §4.0 / nacos-config.md §3 routing 节 v1.19 键域口径）：
① `routing` 整节缺失/读取异常/校验失败 → 代码内置默认表（registry.BUILTIN_ROUTING）
路由 + AgentLog WARNING 一次；② 节有效但该 mode 行缺失或 modes[mode] 指向未注册
图名 → 经 `modes[default_mode]` 解析图名兜底 + WARNING 一次。兜底路由不重写
`turns.mode`（保持用户请求原值，审计口径）；mode 非法已被 API 边界拦截
（api.md 400001），不达图内。

targets 建模（mode 键与图名 value 分离）：路由解析表 = `{mode: graphName}`（值必须
是主图节点名 = registry 键），来自轮内 spec.routing ∩ registry / 内置默认表——B2 桩
曾以 `{registry键: 同名}` 同时充当路由表与边表（mode==图名 隐含假设），接真后解耦；
条件边 path_map 声明 route_by_mode 的返回域（解析出的图名 → 同名节点），并以
BUILTIN_ROUTING 的 {mode: 图名} 行前置保形。

TurnRunner 统一 `main_graph.astream(subgraphs=True)` 消费（ARCH §6.1）；子图
custom 信号经 (namespace, mode, payload) 形状冒泡，namespace 首段=子图注册名。
R14 余项（interrupt·cancel 跨子图透传实测 / 事件冒泡过滤）随批次 5 实测闭环。
"""

from collections.abc import Hashable, Mapping
from typing import Any

from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent.graphs.registry import (
    BUILTIN_DEFAULT_MODE,
    BUILTIN_ROUTING,
    DEFAULT_GRAPH,
    GRAPH_REGISTRY,
)
from app.agent.state import MainState
from app.core.logger import AgentLog


def _read_routing(spec: Any) -> tuple[dict[str, str], str] | None:
    """读轮内 spec.routing 快照 → 校验后的 (modes, default_mode)；不可用返回 None（层①）。

    校验口径（nacos-config.md §3 v1.19 键域，路由面防御性复验，与 config_center
    validator 拒收独立成防线）：modes 为非空映射、default_mode 为 modes 的键、
    值均为字符串。读取异常（任意 Exception）同按不可用处理。
    """
    if not isinstance(spec, Mapping):
        return None
    try:
        routing = spec.get("routing")
        if not isinstance(routing, Mapping):
            return None
        modes = routing.get("modes")
        default_mode = routing.get("default_mode")
        if not isinstance(modes, Mapping) or not modes:
            return None
        if not isinstance(default_mode, str) or default_mode not in modes:
            return None
        if not all(isinstance(value, str) for value in modes.values()):
            return None
        return {str(mode_key): str(value) for mode_key, value in modes.items()}, default_mode
    except Exception:  # noqa: BLE001 —— routing 读取按层①兜底，不向图运行泄漏异常
        return None


def route_by_mode(state: MainState) -> str:
    """条件边路由：轮内 spec.routing 快照 ∩ GRAPH_REGISTRY → 目标图名（双层兜底）。

    返回「目标图名」（= 主图节点名 = GRAPH_REGISTRY 键）。无 LLM、无外部 IO、
    亚毫秒、不发任何图内信号；兜底不重写 turns.mode（TurnRunner 侧保持 turns 行
    原值，审计口径——ARCH §4.0）。层①/层②各自至多告警一次（叠加场景两层级语义
    独立，可同轮各出一条）。
    """
    mode = state.get("mode")
    mode_key = str(mode) if mode is not None else ""
    routing = _read_routing(state.get("spec"))
    if routing is None:
        # 层①：routing 整节缺失/读取异常/校验失败 → 内置默认表（收敛为 registry 模块常量）
        modes, default_mode = BUILTIN_ROUTING, BUILTIN_DEFAULT_MODE
        AgentLog(
            message=(
                f"spec.routing 不可读（整节缺失/校验失败），回落内置默认路由表"
                f"（default_mode={default_mode}）"
            ),
            graph="main",
            mode=mode_key,
        ).warning()
    else:
        modes, default_mode = routing
    # 路由表 = {mode: graphName} ∩ registry：value 必须是主图节点名（不假设 mode==图名）
    route_targets = {
        mode_key_: graph_name
        for mode_key_, graph_name in modes.items()
        if graph_name in GRAPH_REGISTRY
    }
    if mode_key in route_targets:
        return route_targets[mode_key]
    # 层②：mode 行缺失或 modes[mode] 指向未注册图名 → 经 modes[default_mode] 解析图名兜底
    fallback = route_targets.get(default_mode, DEFAULT_GRAPH)
    AgentLog(
        message=(
            f"路由回落默认子图 {fallback}：mode={mode!r} 的映射目标 "
            f"{modes.get(mode_key)!r} 未注册或行缺失，经 modes[{default_mode!r}] 解析兜底"
        ),
        graph="main",
        mode=mode_key,
    ).warning()
    return fallback


def build_main_graph() -> CompiledStateGraph:
    """编译主图壳：按 registry 迭代挂子图节点 + START 条件边路由（无显式 END 边）。"""
    builder = StateGraph(MainState)
    for name, compiled in GRAPH_REGISTRY.items():
        builder.add_node(name, compiled)
    # path_map：{mode: graphName} 保形（BUILTIN_ROUTING ∩ registry，value=主图节点名）
    # + registry 键自映（route_by_mode 返回域=图名，直落同名节点）
    targets: dict[Hashable, str] = {
        **{
            mode_key: graph_name
            for mode_key, graph_name in BUILTIN_ROUTING.items()
            if graph_name in GRAPH_REGISTRY
        },
        **{name: name for name in GRAPH_REGISTRY},
    }
    builder.add_conditional_edges(START, route_by_mode, targets)
    return builder.compile()


graph = build_main_graph()
