"""Agent State 定义（B2 桩期共享态；B3 拆 FastState/DeepState 投影）。

当前形态：桩主图与两张桩子图共用同一最小 TypedDict（total=False，节点纯函数——
只返回增量 dict，持久化由 TurnRunner 消费事件流完成，图内不做任何 IO 落库）。

B3 演进路径（ARCH §4.0 / ADR-012 / R14）：主图 state 为投影薄壳——拆出 FastState
（mode: Literal["fast"]，§4.1）与 DeepState（§4.2），子图以编译产物直挂节点，
LangGraph 按 input/output schema 做投影映射；字段映射定稿时补状态契约测试
（R14 子图边界三风险之「state 投影映射」）。
"""

from typing import Any, TypedDict


class MainState(TypedDict, total=False):
    """主图/桩子图共享最小状态（B3 拆投影后本类仅留主图壳所需键）。

    - `mode`：路由键——TurnRunner 注入；桩期直查 GRAPH_REGISTRY（B3 改经
      spec.routing ∩ registry），API 边界已拦非法值（api.md 400001），不达图内；
    - `query`：本轮用户问题（TurnRunner 注入，含历史装配的追问原文）；
    - `answer_md`：答案 Markdown——桩期由 write 节点返回固定值，TurnRunner 落库；
    - `sources`：来源列表（api.md §7.2 sources batch 同构 dict），TurnRunner 落库。
    """

    mode: str
    query: str
    answer_md: str
    sources: list[dict[str, Any]]
