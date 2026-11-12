"""tools/registry：MCP 源注册表 + 搜索链解析 + 熔断器（ARCH §3.1/§6.2、nacos-config §2）。

职责三层：
- **熔断器**（进程内状态）：连续 failure_threshold 次失败 → open cooldown_s → 冷却期满
  半开（下次尝试放行）→ 成功关闭 / 失败重开；参数来自 mcp.json circuit 节（热更）；
- **搜索链解析**（轮启动一次、轮内冻结）：语言门控（zh→[tavily,bocha]、非 zh→[tavily]，
  REQ-002）∩ enabled ∩ 凭证在场 ∩ 熔断实时态 → 主位互兜底；本轮适用主位全断 → 备位链
  按链序取第一个可用者（searxng 链首 → anysearch 次级，v1.18）；备位全断 → 空链（该轮
  search 失败，节点侧抛 SearchChainUnavailable → TurnRunner 500201）。enabled=false ≡
  熔断 open ≡ 凭证缺失（*_env 指针为空）。轮内中途熔断不切换备位（下一轮 resolve 反映）。
- **guarded 源调用**（schema_guard 双向，§6.2）：入参 pydantic 校验后发调用（编程错误
  不进熔断）；出参归一失败计该源一次失败（进熔断）、异常上抛由节点记 errors——不产
  state 写入、不污染上下文。

**fast 工具面白名单**：SEARCH_SOURCE_NAMES 物理不含 firecrawl/crawl4ai（url_reader 组
属 deep/读取链——共识约束的代码级保证）。凭证只经 .env（settings 单例 / 子进程 env 指针
注入），绝不进日志/断言/配置字面量（ADR-005）。
"""

import asyncio
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pydantic import SecretStr
from typing import Any

from langchain_mcp_adapters.client import MultiServerMCPClient
from pydantic import BaseModel, ValidationError, model_validator

__all__ = [
    "SEARCH_SOURCE_NAMES",
    "BACKUP_SEARCH_CHAIN",
    "CircuitBreaker",
    "SearchChain",
    "SearchChainUnavailable",
    "SourceCallError",
    "ToolRegistry",
    "guarded_source_call",
    "resolve_search_chain",
]

#: fast 搜索源白名单（物理不含 url_reader 组——firecrawl/crawl4ai 不在集合内）
SEARCH_SOURCE_NAMES = {"tavily", "bocha", "searxng", "anysearch"}

#: 搜索备位链（v1.18 链式：链首 searxng → 次级 anysearch，按链序取第一个可用者）
BACKUP_SEARCH_CHAIN: tuple[str, ...] = ("searxng", "anysearch")

#: 语言门控主位源集（REQ-002：zh 双源并行；非 zh 仅 Tavily——BoCha 不跨语言兜用）
ZH_PRIMARY_SOURCES: tuple[str, ...] = ("tavily", "bocha")
EN_PRIMARY_SOURCES: tuple[str, ...] = ("tavily",)

#: 各源搜索工具名（S1/B3 实测收敛口径，代码常量；nacos-config §2 键位不含工具名。
#: tavily=下划线形态 2026-11-10 实测；bocha=R1 双工具 snake_case；searxng=wrapper 工具名）
SEARCH_TOOL_NAMES: dict[str, str] = {
    "tavily": "tavily_search",
    "bocha": "bocha_web_search",
    "searxng": "searxng_search",
    "anysearch": "search",
}

#: 凭证/连接 env 指针（searxng = wrapper 子进程三件套；其余 = API key）
_SOURCE_ENV_POINTERS: dict[str, tuple[str, ...]] = {
    "tavily": ("TAVILY_API_KEY",),
    "bocha": ("BOCHA_API_KEY",),
    "searxng": ("SEARXNG_URL", "SEARXNG_MCP_COMMAND", "SEARXNG_MCP_ARGS"),
    "anysearch": ("ANYSEARCH_API_KEY",),
}

#: stdio 源子进程额外 env 注入表（P1 修复：mcp SDK stdio spawn 用白名单环境
#: get_default_environment()+server.env——任意变量不透传，必须显式注入连接指针。
#: searxng wrapper 经进程环境读 SEARXNG_URL，不注入则备位链首必失败）
_SOURCE_EXTRA_CHILD_ENV: dict[str, tuple[str, ...]] = {
    "searxng": ("SEARXNG_URL",),
}


#: 桩流延时扩展点（integration 心跳/取消窗口测试用；生产链路零读取——测试流经
#: conftest 假体 search 睡眠本值模拟运行窗口）
STUB_SEARCH_DELAY_S: float = 0.0


class SearchChainUnavailable(Exception):
    """搜索链不可用（主位+备位全断 / 全源调用失败）——TurnRunner 捕获后 500201 收轮。"""


class SourceCallError(Exception):
    """单源调用失败（出参校验失败/超时/传输异常）——计熔断一次、由节点记 errors。"""


@dataclass
class CircuitBreaker:
    """单源熔断器（§6.2：连续失败≥threshold → open cooldown → 半开探测）。"""

    failure_threshold: int = 3
    cooldown_s: float = 60.0
    consecutive_failures: int = 0
    opened_at: float | None = None

    def is_open(self, now: float) -> bool:
        """open=冷却期内；冷却期满即半开（False——下次尝试放行，成败在 record_* 定夺）。"""
        if self.opened_at is None:
            return False
        return now < self.opened_at + self.cooldown_s

    def record_success(self, now: float) -> None:
        """成功：关闭并重置连续计数。"""
        self.consecutive_failures = 0
        self.opened_at = None

    def record_failure(self, now: float) -> None:
        """失败：连续计数 +1；达阈值（或半开期再败）→ open 新一轮冷却。"""
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failure_threshold:
            self.opened_at = now


@dataclass(frozen=True)
class SearchChain:
    """轮内冻结的搜索链（轮启动解析一次；熔断翻转不改本对象——下一轮 resolve 反映）。

    sources=本轮可用源（语言门控 + 互兜底/备位装载后）；backup=备位源名（无则 None）；
    degraded=True 表示发生了互兜底/备位装载（必须出现在 stage detail 与 done.degraded，
    §6.2）；detail=stage(search, running) 的链摘要。
    """

    sources: list[str]
    backup: str | None
    degraded: bool
    detail: str


def resolve_search_chain(
    mcp_payload: Mapping[str, Any] | None,
    query_lang: str,
    registry: "ToolRegistry",
    *,
    clock: Callable[[], float] = time.monotonic,
) -> SearchChain:
    """搜索链解析（轮启动一次；纯决策函数，零网络）。"""
    now = clock()
    primaries = ZH_PRIMARY_SOURCES if query_lang == "zh" else EN_PRIMARY_SOURCES
    servers = mcp_payload.get("servers") if isinstance(mcp_payload, Mapping) else None

    def usable(name: str) -> bool:
        entry = servers.get(name) if isinstance(servers, Mapping) else None
        if not isinstance(entry, Mapping) or entry.get("enabled") is not True:
            return False  # enabled=false ≡ 缺条目 ≡ 不装载（A10 口径）
        breaker = registry.breakers.get(name)
        if breaker is not None and breaker.is_open(now):
            return False  # 熔断 open
        return registry.credential_ready(name)

    available = [name for name in primaries if usable(name)]
    if available:
        degraded = len(available) < len(primaries)
        if degraded:
            missing = [name for name in primaries if name not in available]
            detail = f"{'/'.join(available)}（降级：{'/'.join(missing)} 熔断）"
        else:
            detail = "+".join(available)
        return SearchChain(sources=available, backup=None, degraded=degraded, detail=detail)

    for name in BACKUP_SEARCH_CHAIN:  # 链序：searxng 链首 → anysearch 次级（v1.18）
        if usable(name):
            return SearchChain(
                sources=[name],
                backup=name,
                degraded=True,
                detail=(
                    f"{name}（备位装载：主位 {'/'.join(primaries)} 全部不可用；"
                    f"备位链序 {'→'.join(BACKUP_SEARCH_CHAIN)}）"
                ),
            )
    return SearchChain(sources=[], backup=None, degraded=True, detail="主位与备位链全部不可用")


class _SearchArgs(BaseModel):
    """搜索调用入参边界（本节点构造的编程面——extra 容忍各源扩展参数）。

    查询文本跨源两种参数名：通用 `query` / searxng wrapper 扁平 `q`——二选一非空。
    """

    model_config = {"extra": "allow"}
    query: str | None = None
    q: str | None = None

    @model_validator(mode="after")
    def _query_text_present(self) -> "_SearchArgs":
        if not (self.query or "").strip() and not (self.q or "").strip():
            raise ValueError("query/q 至少一项非空（查询文本在场）")
        return self


_BOCHA_ENTRY_RE = re.compile(
    r"Title:\s*(?P<title>.*?)\nURL:\s*(?P<url>\S+)(?:\nDescription:\s*(?P<desc>.*))?",
    re.S,
)


def _parse_bocha_text(text: str) -> list[dict[str, Any]]:
    """BoCha MCP 文本形态解析（实测出参=「Title:/URL:/Description:」散文块，无 JSON 面）。"""
    out: list[dict[str, Any]] = []
    for entry in re.split(r"\n(?=Title: )", text.strip()):
        match = _BOCHA_ENTRY_RE.match(entry.strip())
        if match is None:
            continue
        out.append({
            "url": match.group("url").strip(),
            "title": match.group("title").strip(),
            "snippet": (match.group("desc") or "").strip(),
            "score": 0.0,  # 文本形态无 score 字段——落 0.0（§2.5 渲染兜底同口径）
        })
    return out


_ANYSEARCH_ENTRY_RE = re.compile(
    r"###\s*\d+\.\s*(?P<title>.*?)\n\s*-\s*\*\*URL\*\*:\s*(?P<url>\S+)"
    r"(?P<rest>.*?)(?=\n###\s*\d+\.\s|\Z)",
    re.S,
)


def _parse_anysearch_text(text: str) -> list[dict[str, Any]]:
    """AnySearch MCP 文本形态解析（实测出参=「### N. Title / - **URL**: …」markdown 块）。"""
    out: list[dict[str, Any]] = []
    for match in _ANYSEARCH_ENTRY_RE.finditer(text):
        rest_lines = [
            line.strip().lstrip("- ").strip()
            for line in match.group("rest").splitlines()
            if line.strip() and not line.strip().startswith(("-", "###"))
        ]
        out.append({
            "url": match.group("url").strip(),
            "title": match.group("title").strip(),
            "snippet": " ".join(rest_lines)[:500],
            "score": 0.0,  # 文本形态无 score 字段——落 0.0（§2.5 渲染兜底同口径）
        })
    return out


def _unwrap_mcp_content(raw: Any) -> Any:
    """MCP content block 解包（langchain-mcp-adapters 出参形态——2026-11-10 实测）：
    str 直接返回；[{"type":"text","text":...}] 单块 JSON 优先解析、多块拼文本。"""
    if isinstance(raw, list) and raw and all(
        isinstance(block, Mapping) and block.get("type") == "text" and isinstance(block.get("text"), str)
        for block in raw
    ):
        texts = [block["text"] for block in raw]
        if len(texts) == 1:
            try:
                return json.loads(texts[0])
            except json.JSONDecodeError:
                return texts[0]
        return "\n\n".join(texts)
    return raw


def _normalize_search_output(source: str, raw: Any) -> list[dict[str, Any]]:
    """各源 MCP 搜索出参 → candidates 归一（url 必备；content/snippet 兜底；score 数值化）。

    图片字段增量透传（A6，REQ-004 / api.md §7.2 v1.12；现有键零改动）：
    - SearXNG `img_src`（+ `img_width`/`img_height` 数值型）逐条附带——主通道，天然带相关性；
    - 顶层 `images` 数组（Tavily）无逐条来源页绑定 → 归一为 `query_images` 池挂载到
      首个候选（次级通道；**不伪造逐条绑定**），零候选时随空集丢弃；
    - BoCha/AnySearch 文本形态无图字段 → 自然不产图（禁止硬造）。
    出参形态异常（无结果集/条目缺 url）→ ValueError（schema_guard 计该源一次失败）。
    """
    if source == "bocha":
        text = raw if isinstance(raw, str) else None
        if text is None and isinstance(raw, list):
            blocks = [b.get("text", "") for b in raw if isinstance(b, Mapping)]
            text = "\n\n".join(str(t) for t in blocks)
        if not isinstance(text, str) or "URL:" not in text:
            raise ValueError("bocha 出参非 Title/URL/Description 文本形态")
        return [{**item, "origin": source} for item in _parse_bocha_text(text)]
    if source == "anysearch":
        text = raw if isinstance(raw, str) else None
        if text is None and isinstance(raw, list):
            blocks = [b.get("text", "") for b in raw if isinstance(b, Mapping)]
            text = "\n\n".join(str(t) for t in blocks)
        if not isinstance(text, str) or "**URL**" not in text:
            raise ValueError("anysearch 出参非 markdown 搜索结果文本形态")
        return [{**item, "origin": source} for item in _parse_anysearch_text(text)]

    items: Any = _unwrap_mcp_content(raw)
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{source} 出参非 JSON") from exc
    payload_map = items if isinstance(items, Mapping) else None
    if isinstance(items, Mapping):
        items = items.get("results") or items.get("data") or items.get("items")
    if not isinstance(items, list):
        raise ValueError(f"{source} 出参缺结果集（results/data）")
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url:
            continue
        score_raw = item.get("score")
        snippet = item.get("content") or item.get("snippet") or item.get("description") or ""
        entry: dict[str, Any] = {
            "url": url,
            "title": str(item.get("title") or url),
            "snippet": str(snippet),
            "score": float(score_raw) if isinstance(score_raw, (int, float)) else 0.0,
            "origin": source,
        }
        img_src = item.get("img_src")  # A6 主通道：逐条附带图（str 才透传，畸形丢弃）
        if isinstance(img_src, str) and img_src.strip():
            entry["img_src"] = img_src
        for dim in ("img_width", "img_height"):  # 尺寸上游可得才携带（数值型）
            value = item.get(dim)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                entry[dim] = value
        out.append(entry)
    if payload_map is not None and out:  # A6 次级通道：顶层 images 池挂载到首个候选
        pool: list[dict[str, Any]] = []
        for image in payload_map.get("images") or []:
            if isinstance(image, str) and image.strip():
                pool.append({"url": image})
            elif isinstance(image, Mapping) and isinstance(image.get("url"), str) and image["url"].strip():
                pool_entry: dict[str, Any] = {"url": image["url"]}
                description = image.get("description") or image.get("title")
                if isinstance(description, str) and description.strip():
                    pool_entry["title"] = description
                pool.append(pool_entry)
        if pool:
            out[0]["query_images"] = pool
    return out


def _search_args(source: str, query: str, time_range: str | None, top_k: int) -> dict[str, Any]:
    """各源调用参数（time_range 仅透传 Tavily——ARCH §4.1；其余源不接收该参数）。

    Tavily 附带 `include_images: true`（A6 次级通道数据前提——顶层 images 数组，
    无逐条排序作池透传）；其余源无该参数面。
    """
    if source == "tavily":
        args: dict[str, Any] = {"query": query, "max_results": top_k, "include_images": True}
        if time_range:
            args["time_range"] = time_range
        return args
    if source == "bocha":
        return {"query": query, "count": top_k, "summary": True}
    if source == "searxng":
        # wrapper 扁平签名 `q`（SearxngSearchInput extra="forbid"，工具名 searxng_search；
        # B3 冒烟证伪嵌套 params 形态后钉死）。top_k 不透传——SearXNG JSON API 无对应参数
        return {"q": query}
    return {"query": query}  # anysearch：最小面


def guarded_source_call(
    *,
    invoke: Callable[[dict[str, Any]], Any],
    args_model: type[BaseModel],
    args: dict[str, Any],
    normalize: Callable[[Any], list[dict[str, Any]]],
    breaker: CircuitBreaker,
    timeout_s: float,
    clock: Callable[[], float] = time.monotonic,
) -> Any:
    """schema_guard 双向校验的单源调用（返回协程）。

    入参校验失败=编程错误 → ValueError（熔断计数不动、不发调用）；出参归一失败/调用
    异常/超时 → 计该源一次失败 + SourceCallError（节点记 errors，不产 state 写入）。
    """
    async def _run() -> list[dict[str, Any]]:
        try:
            args_model.model_validate(args)
        except ValidationError as exc:
            raise ValueError(f"入参校验失败（编程错误）：{exc}") from exc
        try:
            async with asyncio.timeout(timeout_s):
                raw = await invoke(args)
            candidates = normalize(raw)
        except Exception as exc:
            breaker.record_failure(clock())
            raise SourceCallError(f"{type(exc).__name__}: {exc}") from exc
        breaker.record_success(clock())
        return candidates

    return _run()


class ToolRegistry:
    """进程级工具注册表：熔断状态 + 源连接缓存 + guarded 搜索调用。

    生产由 lifespan start_tools_registry() 启动（进程级持有者，与 event_bus/asset_center
    同型）；TurnRunner 轮启动 bind(mcp 快照) + resolve(chain) 一次、轮内冻结；stdio
    子进程与连接按 (server, entry 指纹) 进程内复用（nacos-config §2 装载时机条）。
    """

    def __init__(self) -> None:
        self.breakers: dict[str, CircuitBreaker] = {}
        self._entries: dict[str, Mapping[str, Any]] = {}
        self._clients: dict[str, Any] = {}
        self._tools: dict[str, Any] = {}

    # ---- 凭证在场性（源可用性三条件之一；测试假体覆写本方法）----

    def credential_ready(self, server: str) -> bool:
        """*_env 指针指向的凭证/连接值在场（空值=缺失=该源不可用）。值绝不外泄。

        有意设计（装载门窄于解析面，安全方向）：searxng 等连接指针类走
        _resolve_pointer 双渠道（settings 空值回落 os.environ）；tavily/bocha/
        anysearch 等 API-key 密钥类只信 settings 单例——密钥语义不进进程环境。
        """
        from app.core.config import mcp_settings

        if server == "tavily":
            return bool(mcp_settings.tavily_api_key.get_secret_value())
        if server == "bocha":
            return bool(mcp_settings.bocha_api_key.get_secret_value())
        if server == "anysearch":
            return bool(mcp_settings.anysearch_api_key.get_secret_value())
        if server == "searxng":
            # wrapper 三件套走统一指针渠道（settings 单例优先 → 空值/未声明回落进程
            # 环境）：.env + make dev 形态值在 settings 单例（B3 R12 注遗留②修复——
            # 原实现只查 os.environ，该形态下恒 False、备位链首不可装载）
            return all(
                bool(self._resolve_pointer(name).strip())
                for name in _SOURCE_ENV_POINTERS[server]
            )
        return False

    # ---- 轮启动绑定与链解析 ----

    def bind(self, mcp_payload: Mapping[str, Any]) -> None:
        """绑定本轮 mcp 快照（连接条目 + 熔断参数热更面）；连接对象按指纹惰性重建。"""
        servers = mcp_payload.get("servers") if isinstance(mcp_payload, Mapping) else None
        self._entries = {
            name: entry
            for name, entry in (servers or {}).items()
            if isinstance(entry, Mapping)
        }
        for name, entry in self._entries.items():
            circuit = entry.get("circuit") or {}
            breaker = self.breakers.setdefault(
                name, CircuitBreaker(failure_threshold=3, cooldown_s=60.0)
            )
            try:  # 热更熔断参数（状态保留——进程内连续计数不因配置换版清零）
                breaker.failure_threshold = max(1, int(circuit.get("failure_threshold", 3)))
                breaker.cooldown_s = max(1.0, float(circuit.get("cooldown_s", 60)))
            except (TypeError, ValueError):
                pass

    def resolve(self, mcp_payload: Mapping[str, Any] | None, query_lang: str) -> SearchChain:
        """搜索链解析（轮启动一次；见 resolve_search_chain）。"""
        return resolve_search_chain(mcp_payload, query_lang, self)

    # ---- guarded 搜索调用（web_search 节点消费面）----

    async def search(
        self, query: str, source: str, *, time_range: str | None = None, top_k: int = 5
    ) -> list[dict[str, Any]]:
        """单源 guarded 搜索：mcp.json timeout_s 包络 + 双向 schema_guard + 熔断联动。"""
        if source not in SEARCH_SOURCE_NAMES:
            raise SearchChainUnavailable(f"{source} 不在 fast 搜索源白名单")
        entry = self._entries.get(source) or {}
        timeout_s = float(entry.get("timeout_s", 10))
        breaker = self.breakers.setdefault(
            source, CircuitBreaker(failure_threshold=3, cooldown_s=60.0)
        )
        tool = await self._acquire_tool(source)
        args = _search_args(source, query, time_range, top_k)

        async def _invoke(call_args: dict[str, Any]) -> Any:
            return await tool.ainvoke(call_args)

        return await guarded_source_call(
            invoke=_invoke,
            args_model=_SearchArgs,
            args=args,
            normalize=lambda raw: _normalize_search_output(source, raw),
            breaker=breaker,
            timeout_s=timeout_s,
        )

    @staticmethod
    def _resolve_pointer(name: str) -> str:
        """*_env 指针解析：settings 字段（大小写不敏感同名）非空优先，空值/未声明回落进程环境。

        .env 值经 pydantic-settings 进 settings 单例（不在 os.environ）——标准
        「.env + make dev」形态走单例渠道；部署/compose 注入或测试 setenv 晚于单例
        实例化时，字段空值穿透回落进程环境（两渠道统一收口）。值绝不外泄。
        """
        from app.core.config import mcp_settings

        field_name = name.lower()
        if hasattr(mcp_settings, field_name):
            value = getattr(mcp_settings, field_name)
            resolved = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
            if resolved.strip():
                return resolved
        return os.environ.get(name, "")

    async def aclose(self) -> None:
        """生命周期收口（lifespan finally）：弃持连接，stdio 子进程随会话退出回收。"""
        self._clients.clear()
        self._tools.clear()

    # ---- MCP 连接（langchain-mcp-adapters；按指纹惰性建立）----

    def _entry_fingerprint(self, source: str) -> str:
        entry = self._entries.get(source) or {}
        return json.dumps(entry, sort_keys=True, ensure_ascii=False)

    async def _acquire_tool(self, source: str) -> Any:
        fingerprint = self._entry_fingerprint(source)
        cached = self._tools.get(source)
        if cached is not None and self._clients.get(source, (None,))[0] == fingerprint:
            return cached
        entry = self._entries.get(source) or {}
        transport = entry.get("transport")
        if transport == "stdio":
            command = self._resolve_pointer(str(entry.get("command_env", ""))).strip()
            args_text = self._resolve_pointer(str(entry.get("args_env", ""))).strip()
            if not command or not args_text:
                raise SearchChainUnavailable(f"{source} 连接指针缺失（{entry.get('command_env')}）")
            connection: dict[str, Any] = {
                "transport": "stdio",
                "command": command,
                "args": args_text.split(),
                "env": {},
            }
            auth = entry.get("auth")
            if auth:  # 凭证经子进程 env 注入（.env 值不进日志/配置字面量）
                connection["env"][str(auth)] = self._resolve_pointer(str(auth))
            # 附加连接指针注入（P1 修复）：SDK 白名单环境不透传任意变量——
            # 每源显式声明子进程需要的 env 键（searxng=SEARXNG_URL）
            for env_name in _SOURCE_EXTRA_CHILD_ENV.get(source, ()):
                value = self._resolve_pointer(env_name)
                if value:
                    connection["env"].setdefault(env_name, value)
        else:
            url_env = str(entry.get("url_env", ""))
            url = self._resolve_pointer(url_env).strip()
            if not url:
                raise SearchChainUnavailable(f"{source} 连接指针缺失（{url_env}）")
            connection = {
                "transport": "streamable_http" if transport == "http" else "sse",
                "url": url,
            }
            auth = entry.get("auth")
            if auth:
                secret = self._resolve_pointer(str(auth))
                if secret:
                    connection["headers"] = {"Authorization": f"Bearer {secret}"}
        # Connection 为 adapters 的 TypedDict 形态（运行期即 dict）——构造边界按库契约放行
        client = MultiServerMCPClient({source: connection})  # type: ignore[arg-type]
        tools = await client.get_tools()
        by_name = {tool.name: tool for tool in tools}
        tool_name = SEARCH_TOOL_NAMES[source]
        if tool_name not in by_name:
            raise SearchChainUnavailable(
                f"{source} 未暴露搜索工具 {tool_name}（实测清单：{sorted(by_name)}）"
            )
        self._clients[source] = (fingerprint, client)
        self._tools[source] = by_name[tool_name]
        return self._tools[source]


# ---- 进程级持有者（lifespan 启动注册；TurnRunner 经 get 取）----

_tools_registry: ToolRegistry | None = None


def start_tools_registry() -> ToolRegistry:
    """启动进程级 registry 并注册持有者（main.py lifespan 调用）；幂等返回既有实例。"""
    global _tools_registry
    if _tools_registry is None:
        _tools_registry = ToolRegistry()
    return _tools_registry


def get_tools_registry() -> ToolRegistry:
    """取进程级 registry（未启动 fail-fast——组装层尚未 start 即消费属编程错误）。"""
    if _tools_registry is None:
        raise RuntimeError(
            "ToolRegistry 尚未启动：请在 lifespan 启动钩子调用 start_tools_registry()"
        )
    return _tools_registry


def reset_tools_registry() -> None:
    """复位持有者（测试/ lifespan 逆序收口用）。"""
    global _tools_registry
    _tools_registry = None
