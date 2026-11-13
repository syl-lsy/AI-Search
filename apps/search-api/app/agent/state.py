"""Agent State 定义（B3 第 0 步：主图投影薄壳 + FastState/DeepState 子图态——ARCH §4.0/§4.1/§4.2）。

主图壳 state 为投影薄壳（ADR-012 / R14①）：输入侧声明子图所需字段的**同名投影**
（TurnRunner 注入全量 FastState 输入 → 经 LangGraph input schema 映射进入子图），
输出侧声明产出键的同名回填（子图节点更新经顶层 updates 合并回 TurnRunner）。
字段增删必须同步状态契约测试（test/unit/test_stub_graphs.py 的 R14① 投影锁死）。

节点纯函数：只返回增量 dict，持久化由 TurnRunner 消费事件流完成，图内零 DB/EventBus。
"""

from operator import add, or_
from typing import Annotated, Any, Literal, TypedDict

#: rewrite/planner 顺带分类的 6+1 枚举（other 兜底；nacos-config.md §3 common.type_rules）
QueryType = Literal["factoid", "list", "comparison", "multi_facet", "temporal", "how_to", "other"]

#: 时效信号（rewrite/planner 产出，透传 Tavily time_range）
TimeRange = Literal["day", "week", "month", "year"] | None

#: 历史装配条目（ARCH §6.5：最近 6 轮 {q, a_head≤200 chars}，TurnRunner 注入）
HistoryItem = dict[str, str]

#: 检索候选条目（web_search/kb_search 产出：{url,title,snippet,score,origin,kb_doc_id?,chunk_id?}）
Candidate = dict[str, Any]


class MainState(TypedDict, total=False):
    """主图壳投影薄壳（ARCH §4.0）——路由键 + 子图输入投影 + 产出同名回填。

    - `mode`：路由键（api.md 枚举 fast|deep，API 边界已拦非法值不达图内）；
    - `spec`：轮内 Agent Spec 快照（TurnRunner 发起时捕获注入，OQ-11 同源）——
      route_by_mode 读其 `routing` 节分发，子图内读全节参数（超时/预算/映射）；
    - 输入投影键与 FastState/DeepState 输入字段同构（TurnRunner 注入面）；
    - 产出键（answer_md 等）经顶层 updates 同名回填，TurnRunner 落库。
    """

    # 路由（route_by_mode 消费）
    mode: str
    spec: dict[str, Any]
    # 子图输入投影（FastState/DeepState 输入字段同构）
    turn_id: str
    thread_id: str
    user_id: str
    query: str
    chat_model_key: str
    history: list[HistoryItem]
    images: list[str]
    query_lang: Literal["zh", "en"]
    # 子图产出回填（顶层 updates 同名键合并）
    answer_md: str
    query_type: QueryType
    answerable_from_knowledge: bool
    time_range: TimeRange
    rewritten_queries: list[str]
    candidates: list[Candidate]
    images_raw: list[dict[str, Any]]
    fused: list[Candidate]
    # 横切（reducer 承接 Send 扇出并行副本：errors 追加、degraded 逻辑或——§4.1 键集不变）
    errors: Annotated[list[dict[str, Any]], add]
    degraded: Annotated[bool, or_]


class FastState(TypedDict, total=False):
    """fast 模式子图态（ARCH §4.1 全字段；B3 起逐节点实装）。

    - `candidates` 用 `Annotated[list, add]` reducer：web_search/kb_search 经 Send
      扇出的并行副本各自增量写入，superstep 末合并（列表拼接）；
    - `query_lang` 已由 TurnRunner 前置纯代码判定（OQ-2，改写不改判）；
    - `spec` 为轮内快照（OQ-11）——节点参数（type_query_map/max_answer_chars/
      超时/重试）一律读快照，不触 Nacos 实时态。
    """

    # 输入（TurnRunner 注入）
    turn_id: str
    thread_id: str
    user_id: str
    query: str
    mode: Literal["fast"]
    chat_model_key: str  # qwen | glm | deepseek（快照内解析 LiteLLM 别名；M4 视觉轮 vision）
    history: list[HistoryItem]
    images: list[str]  # M4 预留：data URL ≤3，触发 vision 模型（本批恒空）
    spec: dict[str, Any]
    query_lang: Literal["zh", "en"]
    # rewrite 产出（light 模型结构化输出，S14/S15）
    query_type: QueryType
    answerable_from_knowledge: bool
    time_range: TimeRange
    rewritten_queries: list[str]
    # 检索产出（Send 扇出并行写入 → merge_dedupe 消费）
    candidates: Annotated[list[Candidate], add]
    images_raw: list[dict[str, Any]]  # A6：候选图片投影（merge_dedupe 填充——候选附带 img_src 保归并序在前、query_images 池殿后；fuse_rerank 选图消费，ARCH §4.1 images_raw 行）
    fused: list[Candidate]
    # 综合产出
    answer_md: str
    # 横切（reducer 承接 Send 扇出并行副本：errors 追加、degraded 逻辑或）
    errors: Annotated[list[dict[str, Any]], add]
    degraded: Annotated[bool, or_]


class DeepState(TypedDict, total=False):
    """deep 模式子图态（ARCH §4.2 骨架占位，B4 填 deepagents 子代理实装）。

    输入字段与 FastState 同构（mode="deep"；query_lang 同为 TurnRunner 注入）；
    产出字段按 §4.2 预声明（planner/research/reflect/writer 逐节点实装时消费）。
    B2 桩节点仅写 `answer_md`（TurnRunner 终态落库统一读该键）。
    """

    # 输入（与 FastState 同构）
    turn_id: str
    thread_id: str
    user_id: str
    query: str
    mode: Literal["deep"]
    chat_model_key: str
    history: list[HistoryItem]
    images: list[str]
    spec: dict[str, Any]
    query_lang: Literal["zh", "en"]
    # planner 产出（强模型结构化输出，B4）
    query_type: QueryType
    time_range: TimeRange
    subtopics: list[str]
    outline: list[dict[str, Any]]  # [{section, intent}] 报告章节骨架
    # research/reflect 累积（deepagents 子代理，B4）
    subagent_reports: list[dict[str, Any]]
    visited_urls: list[str]
    searched_queries: list[str]
    snapshots: list[dict[str, Any]]  # [{url, object_key, size}]
    collected_sources: list[Candidate]
    reflect_round: int
    gaps: list[dict[str, Any]]  # [{gap, query, origin: "web"|"kb"}]
    # writer/finalize 产出
    report_md: str
    answer_md: str  # TurnRunner 终态落库统一读该键（桩期即由 write 桩产出）
    # 横切
    usage_total: int
    degraded: bool
    degraded_reason: str | None  # "timeout" | "budget" | "source_failure"（api.md §7.2 枚举）
    errors: list[dict[str, Any]]
