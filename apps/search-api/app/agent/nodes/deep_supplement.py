"""supplement 节点（M2 批次 3——ARCH §4.2 supplement 行 / DEV §5.4 五步闭环）。

定向补搜（强模型 reflect 产出的 gaps → 逐条补搜 query 经冻结搜索链执行），复用
fast 搜索链基建（web_search 节点同款链路：冻结链逐源 asyncio.gather 并行 →
`registry.search(query, source, time_range, top_k, retry_sink)`——web_search.py:97-107
同款形态），**不重开完整子代理**（零 LLM 调用——本节点无模型面，补搜候选直接并入
collected_sources 由 reflect 下一轮评审/writer 消费）。tools_registry/search_chain
经 configurable 注入（research deep_research.py:846-856 同款注入面）——节点纯函数，
零 DB/EventBus/全局单例直连。

**双维去重（ARCH §4.2:511）**：query 维度 vs state.searched_queries（strip 精确
匹配——research record_search 精确键先例 deep_research.py:285-287）、候选维度 vs
三账本并集（评审修复 1——发现 1/4）：①state.visited_urls（已读 URL——「已读」语义，
research record_visited 仅 read/snapshot 登记）、②**state.collected_sources url 集
（registered——research/supplement 两段已发射 URL 的单一事实源，ref_no 冻结语义天然
承载：已发射未读 URL 重现时跳过，不 emit 不占 ref_no 不入册，防同 URL 双 ref_no 违
反 api.md §7.2:739）**、③批内重复取首个（_merge_candidates 同款口径）。去重跳过的
query 不登记 searched_queries（未发不登记）。

**每轮执行上限（评审修复 1——发现 6）**：MAX_GAPS_PER_ROUND=5 代码常量——模型漂移
或被外部内容诱导产百级 gaps 时防放大为百次搜索调用；执行面截断、state.gaps 保留
全量诊断面、errors[] 留痕（reason=gaps_truncated）；契约面 gaps 条数边界钉文字归
收官环节。

**origin="kb" gap 本批跳过**（M2 只产 web——plan批次3 item1：KB 分支 M3 前不激活；
kb 取向由 reflect 保留原样 deep_reflect.py:24-25，本节点跳过不搜不登记）。

**回路有界性（防死循环）**：reflect_round 递增不依赖补搜产出——全部 gap 被去重/
全 kb/零执行面时仍 +1（reflect 条件边 `_loop_continues` 的 round<rounds 判据终将
为假 → guards 出口）；递增方=supplement 唯一（reflect 节点零写入）。

**sources 增量批次（api.md §7.2:739 ref_no 首现冻结只增不改）**：ref_no 从
state.collected_sources 现有 max(ref_no) 续编（research recorder 册子是调用内实例
跨节点不可达）；批次条目形态=add_candidates 同款（deep_research.py:297-308 八键，
source_id 为占位 uuid 由 TurnRunner 以行 id 回写）；批次 total=轮级 ref_no 计数
（=本批发射后的 max ref_no，research recorder total=len(ref_by_url) 语义的跨节点
延续）。

**stage 事件（api.md §7.4:866 反思轮形态）**：有执行面 → 入口
stage(search,running,detail=链摘要)（web_search.py:92-93 首副本同款）+ 逐 query
stage(search,done,progress={current,total},detail=源摘要+降级标注，extra.
source_retries 无重试零键)（query 间串行——progress 递增可观测；query 内逐源并行
与 fast 副本同款）；**零执行面（全去重/全 kb/gaps 残缺）→ 零 stage 帧**（不伪造
搜索发生——§7.4 为典型序列非全量义务）。本节点不发 reflect 帧（running/done 归
reflect 节点事件面）。

**可选读原文+快照：本批钉跳过**（批次条目 snapshot_available=False 合法——快照
消费面归 research 结构化摘要链；§7.4 反思轮形态无 read 帧；读原文正文进 LLM 的
消费面不存在，零新增外部内容包裹点）。

**state 回写语义（与 DeepState 声明一致）**：searched_queries/collected_sources 为
普通覆盖键 → **全量并集**返回（_merge_urls/_merge_candidates 复用 research 实现
——单一事实源防漂移）；**visited_urls 回写=原值并集（候选不入）——批次 4 binding
定案：ARCH §4.2:499/:511「已读」纯语义恢复，visited_recorded 仅作批内去重账本，
跨轮去重由 registered 账本（collected_sources url 集）承担**；errors 为
Annotated[list, add] reducer →
增量追加（单源失败 web_search.py:113 同款 {stage,reason,source}，每 query 一条不
聚合——web_search.py:20-24 各自留痕裁决口径同源）；update 键 ⊆
DeepState.__annotations__（R14①）。外部内容包裹面：本节点零 LLM 零 prompt——搜索
结果以候选元数据（url/title/snippet/score）入册 state，LLM 消费点（reflect 材料
块不读 candidates、批次 4 writer sources_block）由消费侧包裹（research/reflect
先例同构——包裹发生在「进 prompt」处而非「入 state」处）。

**B15 内容侧护栏（批次 7 评审 P2-3 接线）**：候选入册前逐条过
`filter_candidates`（guards 经 `load_content_guard_config(spec)` 装载——spec 缺
safety 节回落 defaults 种子=部署即启用；与 fast web_search / deep research 闭包
三面同判据）。flag=保留+留痕（`errors[] {stage:"search", reason:"injection_blocked",
count}`）；drop=丢弃（过滤先于批次装配 → 不占 ref_no、不入册、不发卡，来源卡/[n]
自然一致）；护栏动作不置 degraded（degraded_reason 枚举封闭）。

失败语义：链/registry 未注入或链空源 → SearchChainUnavailable（research :851-854
同款，TurnRunner 500201 收轮面）；单源失败 → 其余源候选保留 + errors 增量 + detail
降级标注（不终止——补搜轮面与 fast 副本同构）。ensure_not_canceled 入口接线
（R14②，同 research/reflect 先例）。
"""

import asyncio
import uuid as uuid_module
from collections.abc import Mapping
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.agent.graphs.signals import emit_sources, emit_stage
from app.agent.guards import ensure_not_canceled
from app.agent.nodes.deep_research import _merge_candidates, _merge_urls
from app.agent.safety.config import load_content_guard_config
from app.agent.safety.content_guard import filter_candidates
from app.agent.tools.registry import SearchChain, SearchChainUnavailable

__all__ = ["MAX_GAPS_PER_ROUND", "SUPPLEMENT_TOP_K", "supplement"]

#: 单源单 query 候选上限（web_search.WEB_SEARCH_TOP_K 同值——入口控量先例 §6.5）
SUPPLEMENT_TOP_K = 5

#: 每轮补搜 query 执行上限（评审修复 1——模型漂移/外部内容诱导产百级 gaps 时防止
#: 放大为百次搜索调用；执行面截断、state.gaps 保留全量诊断面、errors 留痕。代码
#: 常量 OQ-6 先例；契约面 gaps 条数边界钉文字归收官环节）
MAX_GAPS_PER_ROUND = 5


def _as_str(value: Any) -> str:
    """防御字符串化（None/非字符串 → 空串）。"""
    return value.strip() if isinstance(value, str) else ""


def _safe_round(state: Any) -> int:
    """state.reflect_round 防御解析（缺失/非法按 0——deep_reflect 同款口径）。"""
    try:
        return max(0, int(state.get("reflect_round")))  # type: ignore[arg-type, union-attr]
    except (TypeError, ValueError, AttributeError):
        return 0


def _usable_gaps(gaps: Any, searched: set[str]) -> tuple[list[dict[str, Any]], int]:
    """过滤出本批要补搜的 gap（kb 跳过 + 空白丢弃 + query 维度去重 + 每轮上限截断）：

    - origin 必须 == "web"（kb 跳过——M2 只产 web；非法 origin 已由 reflect 归一 web）；
    - query strip 非空；
    - query strip 后不在 searched（已搜过 → 跳过且不重复登记）；
    - 执行面上限 MAX_GAPS_PER_ROUND（评审修复 1——发现 6；超限截断计入返回值第二元，
      调用方 errors 留痕；state.gaps 保留全量诊断面）。
    """
    usable: list[dict[str, Any]] = []
    considered = 0
    if not isinstance(gaps, list):
        return usable, 0
    for item in gaps:
        if not isinstance(item, dict):
            continue
        if item.get("origin") != "web":
            continue  # kb（与非法形态）本批跳过
        query = _as_str(item.get("query"))
        if not query or query in searched:
            continue
        considered += 1
        if len(usable) >= MAX_GAPS_PER_ROUND:
            continue  # 评审修复 1：执行面每轮上限（state.gaps 保留全量诊断面）
        usable.append(item)
    return usable, max(0, considered - len(usable))


def _next_ref_no(collected: list[dict[str, Any]]) -> int:
    """ref_no 续编起点：state.collected_sources 现有 max(ref_no)+1（空表从 1 起）。"""
    refnos = [
        int(item["ref_no"])
        for item in collected
        if isinstance(item, dict) and isinstance(item.get("ref_no"), (int, float))
    ]
    return (max(refnos) + 1) if refnos else 1


async def supplement(state: Any, config: RunnableConfig | None = None) -> dict[str, Any]:
    """supplement 节点（deep 子图）：gaps 定向补搜 → 候选去重并入 + sources 增量批次
    + reflect_round 递增（有界性）；零执行面零帧直通。"""
    ensure_not_canceled(config)  # 节点边界感知协作式取消（R14②：同 research/reflect）

    configurable = (config or {}).get("configurable") or {}
    chain = configurable.get("search_chain")
    if not isinstance(chain, SearchChain):
        raise SearchChainUnavailable("搜索链未注入（registry 未启动）——无法执行 supplement")
    if not chain.sources:
        raise SearchChainUnavailable(chain.detail or "搜索链不可用")
    registry = configurable.get("tools_registry")
    if registry is None:
        raise SearchChainUnavailable("搜索链未注入（registry 未启动）——无法执行 supplement")

    is_mapping = isinstance(state, dict)
    prior_searched: list[str] = (
        [str(q) for q in state.get("searched_queries") or [] if isinstance(q, str) and q]
        if is_mapping
        else []
    )
    searched = {q.strip() for q in prior_searched}
    prior_visited: list[str] = (
        [str(u) for u in state.get("visited_urls") or [] if isinstance(u, str) and u]
        if is_mapping
        else []
    )
    prior_collected: list[dict[str, Any]] = (
        [dict(item) for item in state.get("collected_sources") or [] if isinstance(item, dict)]
        if is_mapping
        else []
    )
    visited = set(prior_visited)  # 已读 URL 账本（「已读」语义——research record_visited 仅 read/snapshot 登记）
    # 评审修复 1：registered 账本 = state.collected_sources url 集（research/supplement
    # 两段**已发射** URL 的单一事实源，ref_no 冻结语义天然承载——已发射未读 URL 重现时
    # 跳过：不 emit、不占 ref_no、不入册，防同 URL 双 ref_no 违反 api.md §7.2:739）
    registered = {
        item["url"]
        for item in prior_collected
        if isinstance(item.get("url"), str) and item["url"]
    }

    usable, truncated = _usable_gaps(state.get("gaps") if is_mapping else None, searched)
    reflect_round = _safe_round(state) + 1  # 有界性：递增不依赖补搜产出（防死循环核心）

    if not usable:
        # 零执行面：零 stage 帧零搜索零批次（不伪造搜索发生）；回写键=原值并集
        # （覆盖键全量并集语义，等价原值）+ reflect_round 推进
        return {
            "searched_queries": _merge_urls(prior_searched, {}),
            "visited_urls": _merge_urls(prior_visited, {}),
            "collected_sources": _merge_candidates(prior_collected, []),
            "reflect_round": reflect_round,
        }

    emit_stage("search", "running", detail=chain.detail)  # 入口帧（web_search.py:92-93 同款）

    ref_no = _next_ref_no(prior_collected)
    # 本批候选去重账本（批内重复取首个）——**仅批内去重用，不回写 state.visited_urls**
    # （批次 4 binding 定案：visited_urls=「已读」纯语义，跨轮去重走 registered 账本）
    visited_recorded: dict[str, None] = {}
    searched_recorded: dict[str, None] = {}  # 本批实际发出 query（未发不登记）
    errors: list[dict[str, Any]] = []  # Annotated[add] 增量（每 query 一条，不聚合）
    new_raw_all: list[dict[str, Any]] = []  # state 入册原始候选累积（附 ref_no 账本键）
    # B15 内容侧护栏装载（批次 7 评审 P2-3）：spec 缺失/形态漂移回落 defaults 种子
    # （部署即启用，与 web_search.py:128-129 同口径）——本节点一次装载、逐候选消费
    guard_spec = state.get("spec") if isinstance(state, Mapping) else None
    guards = load_content_guard_config(guard_spec if isinstance(guard_spec, Mapping) else None)
    total = len(usable)
    for index, gap in enumerate(usable):
        query = _as_str(gap.get("query"))
        time_range = state.get("time_range") if isinstance(state.get("time_range"), str) else None
        retry_sink: dict[str, int] = {}  # 源名→同源延迟重试次数（P1b 留痕，无重试零键）
        query_errors: list[str] = []  # 本 query 失败源（detail 降级标注面）

        def _call(source: str) -> Any:
            # time_range 仅透传 Tavily（§4.1/research 子代理同款）
            return registry.search(
                query,
                source,
                time_range=(time_range if source == "tavily" else None),
                top_k=SUPPLEMENT_TOP_K,
                retry_sink=retry_sink,
            )

        outcomes = await asyncio.gather(
            *(_call(source) for source in chain.sources), return_exceptions=True
        )
        candidates: list[dict[str, Any]] = []
        ok_summary: list[str] = []
        for source, outcome in zip(chain.sources, outcomes):
            if isinstance(outcome, BaseException):
                query_errors.append(source)
                errors.append({"stage": "search", "reason": "source_failure", "source": source})
            else:
                # B15 内容侧护栏（批次 7 评审 P2-3）：补搜面与 fast web_search /
                # deep research 闭包同判据——逐候选注入扫描后入册（drop 不占 ref_no：
                # 过滤先于批次装配，来源卡/[n] 自然一致）；flag=保留+留痕；
                # 护栏动作不置 degraded（degraded_reason 枚举封闭）。
                filtered, filter_errors = filter_candidates(outcome, guards, stage="search")
                errors.extend(filter_errors)
                candidates.extend(filtered)
                ok_summary.append(f"{source}×{len(filtered)}")
        detail = " / ".join(ok_summary) or "全部源失败"
        if query_errors:
            detail += f"（降级：失败源 {','.join(query_errors)}）"
        emit_stage(
            "search",
            "done",
            progress={"current": index + 1, "total": total},
            detail=detail,
            extra={"source_retries": dict(retry_sink)} if retry_sink else None,
        )

        # 候选维度去重（visited + 批内重复取首个）+ ref_no 续编 + 批次条目
        # （batch=发射条目；new_raw=state 入册原始候选——research recorder.candidates 同形态）
        batch: list[dict[str, Any]] = []
        new_raw: list[dict[str, Any]] = []
        for candidate in candidates:
            url = candidate.get("url")
            if (
                not isinstance(url, str)
                or not url
                or url in visited
                or url in visited_recorded
                or url in registered  # 评审修复 1：已发射 URL（含未读）零重复发射
            ):
                continue
            visited_recorded[url] = None
            batch.append(
                {
                    "source_id": str(uuid_module.uuid4()),  # 占位——TurnRunner 以行 id 回写
                    "ref_no": ref_no,
                    "source_type": "web",
                    "title": str(candidate.get("title") or url),
                    "url": url,
                    "snippet": str(candidate.get("snippet") or ""),
                    "score": float(candidate["score"]) if isinstance(candidate.get("score"), (int, float)) else 0.0,
                    "snapshot_available": False,  # 本批无快照（可选读原文+快照钉跳过）
                    "kb_doc_id": None,
                }
            )
            new_raw.append({**candidate, "ref_no": ref_no})  # state 侧账本键（research 同款）
            ref_no += 1
        if batch:
            emit_sources(batch=batch, total=ref_no - 1)  # 轮级 ref_no 计数（发射后 max）
        searched_recorded[query] = None
        new_raw_all.extend(new_raw)

    # 回写全量并集（覆盖键语义——_merge_urls/_merge_candidates 复用 research 单一事实源）；
    # state 侧候选附 ref_no（账本延续——research deep_research.py:296-299 同款增量键）。
    # **visited_urls 停止并入候选（批次 4 binding 定案——ARCH §4.2:499/:511「已读」
    # 纯语义恢复）**：visited_recorded 仅作本批批内去重账本、不回写 state.visited_urls
    # （跨轮去重由 registered 账本=collected_sources url 集承担——评审修复 1 面）；
    # reflect 评审材料块「已读 URL：」标签自此恢复真实语义。
    update: dict[str, Any] = {
        "searched_queries": _merge_urls(prior_searched, searched_recorded),
        "visited_urls": _merge_urls(prior_visited, {}),
        "collected_sources": _merge_candidates(prior_collected, new_raw_all),
        "reflect_round": reflect_round,
    }
    if errors:
        update["errors"] = errors  # Annotated[add] 增量（每 query 一条，不聚合）
    if truncated:
        update.setdefault("errors", []).append(
            {
                "stage": "search",
                "reason": "gaps_truncated",
                "limit": MAX_GAPS_PER_ROUND,
                "truncated": truncated,  # 评审修复 1：执行面上限留痕（state.gaps 保留全量）
            }
        )
    return update
