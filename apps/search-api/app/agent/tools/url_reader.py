"""url_reader：读取链消费 + `firecrawl_read` 工具（M2 批次 1 工具面——ARCH §6.3/§4.2、
nacos-config §2 v1.8、R12；deep 子代理三工具之一，批次 2 research 接入 deepagents）。

- **读取链**：主位 Firecrawl → 备位 Crawl4AI（registry.resolve_read / resolve_read_chain，
  备位条件装载语义与搜索链一致——R12/nacos §2 v1.8）；主备全断 → 空链 → 本工具抛
  SearchChainUnavailable，**跳过读原文的决策由调用方（deep 节点）消费**（读取链空链
  不等价搜索链 500201）。
- **head 限幅**：正文窗口 = `spec.deep.compact.url_read_head_tokens`（默认 4000，
  defaults.py:173 同源回落）+ offset/limit 参数（token 估算窗口，翻页续读面）；
- **`<external_content>` 包裹**：经共享 helper（§6.4.3 铁律——属性由 helper 转义、
  content 开/闭标记在本工具中性化，提前闭合包裹不可达）；
- **全量与窗口分面**：`registry.read` 返回正文全量（快照「对象全量存 MinIO」依赖），
  本工具只产出模型可见窗口——全量→save_snapshot、窗口→上下文，两消费面互不影响。
- **链序读取守卫 `read_via_chain`**（B15 架构不变量）：KB URL 收藏通道（api.md §4.2）
  的护栏面入口——每源读取前复验 url_guard（DNS rebinding TOCTOU）、链序换源；
  `registry.read` 调用点只允许存在于本模块与 deep_research 护栏面（AST 哨兵锁死）。

节点纯函数边界不破：本模块是**工具面**（同 web_search 消费 ToolRegistry 的 IO 形态），
registry/chain/spec 全部显式注入，零全局单例直连、零 DB/EventBus。
"""
import math
import unicodedata
from collections.abc import Mapping
from typing import Any

from app.agent.external_content import external_content
from app.agent.safety.content_guard import (
    UrlRejectedError,
    scan_text_async,
    validate_url_async,
)
from app.agent.safety.config import load_content_guard_config
from app.agent.tools.registry import (
    READ_BACKUP_CHAIN,
    READ_PRIMARY_SOURCES,
    READ_SOURCE_NAMES,
    READ_TOOL_NAMES,
    ReadChain,
    SearchChainUnavailable,
    ToolRegistry,
    get_tools_registry,
    resolve_read_chain,
)

__all__ = [
    "DEFAULT_URL_READ_HEAD_TOKENS",
    "READ_BACKUP_CHAIN",
    "READ_PRIMARY_SOURCES",
    "READ_SOURCE_NAMES",
    "READ_TOOL_NAMES",
    "UrlRejectedError",
    "estimate_tokens",
    "firecrawl_read",
    "read_via_chain",
    "resolve_read_chain",
    "token_window",
]

#: MCP 清单 dataId（nacos-config §0 命名规则；与 turn_service._MCP_DATA_ID 缝字面同源、
#: config_center.defaults.DATA_IDS 同值——读取链解析的配置面）
_MCP_DATA_ID = "ai_search.agent.mcp.json"

#: head 窗口默认值（spec.deep.compact.url_read_head_tokens 缺失/非法回落——与
#: config_center/defaults.py:173 内置默认同源，灰度安全）
DEFAULT_URL_READ_HEAD_TOKENS = 4000

#: 全宽字符判定集（east_asian_width：W=Wide/F=Fullwidth——CJK 与全宽标点）
_WIDE_WIDTHS = frozenset({"W", "F"})


def _char_token_cost(ch: str) -> float:
    """单字符 token 代价（文档化近似）：全宽字符 1 token/字，其余 4 字符 ≈ 1 token。

    中文为主的研究正文按 1:1 保守估计（避免低估截掉尾部），ASCII 按 GPT 系 4:1 经验值。
    """
    return 1.0 if unicodedata.east_asian_width(ch) in _WIDE_WIDTHS else 0.25


def estimate_tokens(text: str) -> int:
    """全文 token 估算（向上取整；空串为 0）。"""
    if not text:
        return 0
    return math.ceil(sum(_char_token_cost(ch) for ch in text))


def token_window(text: str, *, offset: int = 0, limit: int | None = None) -> tuple[str, int, int]:
    """按 token 估算切正文窗口（返回 (窗口文本, start, end)——token 区间，闭开语境）。

    - `offset`：起点 token（翻页续读，如首窗 end=4000 → 次窗 offset=4000）；
    - `limit`：本窗 token 上限（None=不限，由调用方与 head 限幅合成）；
    - 边界余数（ASCII 不足 0.25 token 的零头）不产出，属下一窗（续窗不丢字节）；
    - offset ≥ 全文 token 数 → 空窗口（(offset, total)——不越界、零截断歧义）；
    - offset<0 / limit<1 → ValueError（编程错误面，先于任何调用）。
    """
    if offset < 0:
        raise ValueError("offset 不可为负（token 起点）")
    if limit is not None and limit < 1:
        raise ValueError("limit 须 ≥ 1（token 数）")
    chars: list[str] = []
    cum = 0.0  # 已消耗 token 代价（含跳过段）
    skipping = True
    for ch in text:
        cost = _char_token_cost(ch)
        if skipping and cum + cost <= offset:
            cum += cost
            continue
        skipping = False
        if limit is not None and cum + cost > offset + limit:
            break
        chars.append(ch)
        cum += cost
    total = estimate_tokens(text)
    start = offset
    end = math.ceil(cum) if chars else min(offset, total)
    return "".join(chars), start, end


def _resolve_head_tokens(spec: Mapping[str, Any] | None) -> int:
    """head 限幅解析：spec.deep.compact.url_read_head_tokens → 非法/缺失回落默认。"""
    compact = ((spec or {}).get("deep") or {}).get("compact") or {}
    try:
        tokens = int(compact.get("url_read_head_tokens", DEFAULT_URL_READ_HEAD_TOKENS))
    except (TypeError, ValueError):
        return DEFAULT_URL_READ_HEAD_TOKENS
    return tokens if tokens > 0 else DEFAULT_URL_READ_HEAD_TOKENS


def _neutralize_content(content: str) -> str:
    """内容侧标记中性化（§6.4.3 硬边界——包裹体标记不可被页面正文伪造）。

    开/闭标记实体化为惰性文本：提前闭合包裹会把指令文本抬升为提示词正文；伪造开
    标记会把后续内容框进「外部资料」语境。属性侧转义由共享 helper 承担（分工见
    external_content 模块 docstring）。
    """
    return content.replace("</external_content>", "&lt;/external_content&gt;").replace(
        "<external_content", "&lt;external_content"
    )


async def firecrawl_read(
    url: str,
    *,
    offset: int = 0,
    limit: int | None = None,
    registry: ToolRegistry,
    read_chain: ReadChain,
    spec: Mapping[str, Any] | None = None,
) -> str:
    """读取 URL 正文（deep 子代理工具；批次 2 research 经 deepagents 暴露）。

    流程：读取链取可用源（主 firecrawl / 备 crawl4ai）→ **URL 护栏全量校验（B15——
    拒收抛 UrlRejectedError，零物理调用零熔断计数）** → registry.read 全量正文 →
    **窗口注入扫描（B15——drop 返回系统注零包裹；顺序锁：先扫描后转义）** → token
    窗口（limit 缺省 = head 限幅）→ 开/闭标记中性化 → `<external_content>` 包裹 →
    窗口非全文时追加系统截断注记（模型必须知道只读到局部，续读带 offset）。

    编程错误（offset<0 / limit<1 / url 空白）→ ValueError，零物理调用；链不可用
    → SearchChainUnavailable（调用方决定跳过读原文——nacos §2 v1.8）；URL 被护栏
    拒收 → UrlRejectedError（B15 未接线工具面同判据——防未来接线漂移，接线方捕转
    系统注 + errors 留痕，deep_research.firecrawl_read 闭包即此形态）。
    """
    url = url.strip()
    if not url:
        raise ValueError("url 必填（非空白）")
    if offset < 0:
        raise ValueError("offset 不可为负（token 起点）")
    if limit is not None and limit < 1:
        raise ValueError("limit 须 ≥ 1（token 数）")
    if not read_chain.sources:
        raise SearchChainUnavailable(
            read_chain.detail or "读取链不可用（主位与备位链全部不可用）"
        )
    guards = load_content_guard_config(spec if isinstance(spec, Mapping) else None)
    if guards.url_guard is not None:
        result = await validate_url_async(url, guards.url_guard)
        if not result.ok:
            raise UrlRejectedError(
                f"url 被安全护栏拒绝（{result.reason}）——零物理调用",
                reason=result.reason,
            )

    source = read_chain.sources[0]
    text = await registry.read(url, source)
    head_tokens = _resolve_head_tokens(spec)
    window, start, end = token_window(
        text, offset=offset, limit=head_tokens if limit is None else limit
    )
    total = estimate_tokens(text)
    # B15 顺序锁：注入扫描作用于原始窗口文本（先于中性化转义——转义后结构种子不可
    # 检出）；drop=零包裹返回系统注（零 payload 进上下文），flag=照常包裹（留痕由
    # 接线方 errors 面承担）
    injection = guards.injection_filter
    if injection is not None:
        scan = await scan_text_async(window, injection)
        if scan.hits and injection.on_hit == "drop":
            return (
                "（系统注：该页正文命中注入特征（injection_blocked），已整页拦截——"
                "请改读其他来源。）"
            )
    wrapped = external_content(_neutralize_content(window), source=url, kind="web")
    if end < total:
        wrapped += (
            f"\n\n[系统注：正文窗口截断——以上仅为该页正文 token {start}~{end}，"
            f"全文约 {total} token；携带 offset={end} 可续读后续内容。]"
        )
    return wrapped


# ---- B15 护栏面：链序读取守卫（KB URL 收藏通道 / api.md §4.2）----


def _capture_mcp_payload() -> dict[str, Any] | None:
    """MCP 清单快照 payload（读取链解析的配置面）。

    组装层未启动（单测/Studio 直跑/资产中心异常）或 store 无该 dataId → **None**
    （调用方不得 bind 空表：进程级共享 registry 的条目会被清空，殃及并发 deep 轮
    的读取链）。
    """
    from app.config_center import get_asset_center

    try:
        snapshot = get_asset_center().snapshot(_MCP_DATA_ID)
    except Exception:  # noqa: BLE001 —— 回落属预期运行形态（组装层未启动）
        return None
    return dict(snapshot.payload) if snapshot is not None else None


async def read_via_chain(
    url: str,
    spec: Mapping[str, Any] | None = None,
    *,
    registry: ToolRegistry | None = None,
    chain: ReadChain | None = None,
) -> str:
    """链序读取 URL 正文全量（**护栏面内**——registry.read 调用点守卫）。

    消费面：KB URL 收藏通道（api.md §4.2「Firecrawl 抓正文后走 4.1 同一流水线」）。
    读取链解析与 deep_research / firecrawl_read 同装配：MCP 清单快照 → registry.bind
    → resolve_read_chain（主位 firecrawl / 备位 crawl4ai 条件装载）。

    **校验时序（TOCTOU 复验）**：service.collect_url 的抓取前护栏是第一道门
    （fail-closed 零物理调用），本函数在**每个 source 读取前**再过一次
    `validate_url_async`——DNS 两次解析间隙的重绑定不可被一次性前置校验覆盖；
    guard 拒收 → 换下一源，全源拒收 → UrlRejectedError（零物理读取）；guard
    None（kill switch）→ 直通读取。

    失败语义：链空 → SearchChainUnavailable（调用方归一 422001）；**MCP 清单快照
    不可得（异常/None/空 payload）→ 不 bind 空表、抛 SearchChainUnavailable**
    （进程级共享 registry 的条目不被清空，防殃及并发 deep 轮）；链内单源读取
    失败 → 换下一源，全败上抛末次异常（与原路由缝实现一致）。

    `registry` / `chain` 为显式注入缝（同 firecrawl_read 形态）：chain 缺省 =
    生产解析路径（快照 + bind）；单测注入假体零网络。
    """
    if registry is None:
        registry = get_tools_registry()
    if chain is None:
        mcp_payload = _capture_mcp_payload()
        if not mcp_payload:
            # 快照不可得（异常/None/空 payload）→ **不 bind**：进程级共享 registry
            # 的 _entries 被清空会殃及并发 deep 轮的读取链（P2 定正）
            raise SearchChainUnavailable(
                "读取链配置快照不可用（MCP 清单缺失——跳过读原文）"
            )
        registry.bind(mcp_payload)
        chain = resolve_read_chain(mcp_payload, registry)
    if not chain.sources:
        raise SearchChainUnavailable(
            chain.detail or "读取链不可用（主位与备位链全部不可用）"
        )
    guards = load_content_guard_config(spec if isinstance(spec, Mapping) else None)
    guard = guards.url_guard
    last_exc: Exception | None = None
    rejection: str | None = None
    for source in chain.sources:
        if guard is not None:  # 每次读取前复验（DNS rebinding TOCTOU 面）
            verdict = await validate_url_async(url, guard)
            if not verdict.ok:
                rejection = verdict.reason
                continue  # 拒收 → 换下一源（全源拒收→UrlRejectedError）
        try:
            return await registry.read(url, source)
        except Exception as exc:  # noqa: BLE001 —— 链序换源（失败换下一源）
            last_exc = exc
    if rejection is not None:
        raise UrlRejectedError(
            f"url 被安全护栏拒绝（{rejection}）——零物理读取", reason=rejection
        )
    if last_exc is not None:
        raise last_exc
    raise SearchChainUnavailable(chain.detail or "读取链不可用（主位与备位链全部不可用）")

