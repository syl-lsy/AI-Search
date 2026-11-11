"""searxng 薄 stdio MCP wrapper——搜索兜底链首 fallback_search（B3 第 0 步实装）。

契约溯源（冻结，不改语义）：
- nacos-config.md §2 `searxng` 条目（v1.18）：transport=stdio 自研薄 MCP wrapper，同
  BoCha `BOCHA_MCP_COMMAND` 模式；`SEARXNG_MCP_COMMAND` / `SEARXNG_MCP_ARGS` 指向本模块
  入口，`SEARXNG_URL` 由 wrapper 子进程经**进程环境**读取（mcp.json 不承载端点值）；
- ARCHITECTURE §5 searxng 行 / §7 SEARXNG_* 三变量 / §10 R12（残余上游依赖随链传递，
  CAPTCHA/上游改版由链上次级 anysearch + enabled=false 回退门兜底）；
- wrapper 为 registry 内一等 MCP 源（熔断/条件装载/降级披露全套复用，S1 装载），本模块
  **只做协议桥接**：仅暴露 search 类工具、透传 SearXNG 原生 JSON API
  （GET {SEARXNG_URL}/search?q=…&format=json），不做字段裁剪/结果策略/语言门控——
  那些是节点/registry 侧策略，不在 wrapper 层。

启动方式（.env 定形值同此命令，SEARXNG_MCP_COMMAND=uv）：
    uv run --package search-api python -m app.agent.tools.searxng_mcp

失败语义（单测钉住 test_searxng_mcp.py）：上游 403 / 429 / 超时 / 非 JSON（上游改版迹象）
全部转 ToolError 且带可操作修复提示，不向 registry 冒裸 httpx 异常。

HTTP 出口安全口径：SEARXNG_URL 为运维在 .env 配置的自部署环回服务（127.0.0.1:18080），
属受信配置值而非用户输入——环回即本服务设计形态，不做内网地址拒绝（Mimosa SSRF 通用
约束针对用户可注入 URL，与此处威胁模型不同）；仍执行 scheme 白名单（仅 http/https），
入参 q 经 httpx params 编码，无 URL 拼接注入面。密钥纪律：本 wrapper 无密钥
（自部署环回无鉴权，nacos-config.md §2 searxng 条目无 auth 字段）。
"""

import json
import os
from typing import Any, Literal

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

# ── 代码常量（与环境无关的纯逻辑值，配置三分法归代码层）──
SEARCH_TIMEOUT_S = 4.5      # wrapper 侧 HTTP 超时：registry timeout_s=5（nacos-config §2），
                            #   贴边留 0.5s 裕量给 stdio 序列化与往返
MAX_QUERY_CHARS = 256       # q sanity 上限（与 SearXNG 自身 q 输入约束同级，防误用超长串）

mcp = FastMCP("searxng_mcp")


def _searxng_base_url() -> str:
    """读进程环境 SEARXNG_URL（wrapper 子进程 env），返回去尾斜杠 base；非法即 ToolError。"""
    raw = os.environ.get("SEARXNG_URL", "").strip()
    if not raw:
        raise ToolError(
            "SEARXNG_URL is not set in the process environment. "
            "Set it in .env (e.g. SEARXNG_URL=http://127.0.0.1:18080) and restart the backend."
        )
    if not (raw.startswith("http://") or raw.startswith("https://")):
        raise ToolError(
            f"SEARXNG_URL must start with http:// or https://, got: {raw.split(':', 1)[0]}://"
        )
    return raw.rstrip("/")


def _build_client() -> httpx.AsyncClient:
    """HTTP 客户端唯一构造处（单测经替换本函数注入 MockTransport，零真网）。"""
    return httpx.AsyncClient(timeout=SEARCH_TIMEOUT_S)


class SearxngSearchInput(BaseModel):
    """searxng_search 入参模型（Pydantic 边界校验，未知参数拒收）。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    q: str = Field(
        ...,
        min_length=1,
        max_length=MAX_QUERY_CHARS,
        description="Search query text (required, 1-256 chars, e.g. 'langgraph streaming' or '智谱 GLM 发布').",
    )
    language: str | None = Field(
        default=None,
        max_length=16,
        description="Optional UI language hint passed to engines (e.g. 'zh-CN', 'en').",
    )
    categories: str | None = Field(
        default=None,
        max_length=64,
        description="Optional comma-separated SearXNG categories (default 'general'; e.g. 'general,it').",
    )
    engines: str | None = Field(
        default=None,
        max_length=128,
        description="Optional comma-separated engine names overriding defaults (e.g. 'bing,duckduckgo').",
    )
    time_range: Literal["day", "week", "month", "year"] | None = Field(
        default=None,
        description="Optional freshness filter supported by SearXNG.",
    )
    safesearch: int = Field(
        default=1,
        ge=0,
        le=2,
        description="Safe-search level: 0=off, 1=moderate, 2=strict.",
    )


async def _fetch_json(base_url: str, params: dict[str, str]) -> dict[str, Any]:
    """GET {base_url}/search?format=json&…；透传语义——只做状态码/JSON 合法性把关。"""
    request_params = {"format": "json", **params}
    async with _build_client() as client:
        try:
            resp = await client.get(f"{base_url}/search", params=request_params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 403:
                raise ToolError(
                    "SearXNG returned HTTP 403 — likely 'json' is missing from "
                    "search.formats in settings.yml or the limiter blocked this request. "
                    "Fix: redeploy deploy/searxng/settings.yml (formats must include json, limiter off)."
                ) from exc
            if status == 429:
                raise ToolError(
                    "SearXNG returned HTTP 429 (rate limited by upstream engines or limiter). "
                    "Retry later; persistent 429 means trip the circuit and fall to "
                    "fallback_search_secondary (anysearch)."
                ) from exc
            raise ToolError(
                f"SearXNG returned unexpected HTTP {status} — possible upstream change."
            ) from exc
        except httpx.TimeoutException as exc:
            raise ToolError(
                f"SearXNG request timed out after {SEARCH_TIMEOUT_S}s — is the searxng "
                "container running? Check: docker compose ps searxng"
            ) from exc
        except ValueError as exc:
            # resp.json() 对非 JSON 响应（CAPTCHA HTML 页/上游改版）抛 ValueError
            raise ToolError(
                "SearXNG response is not valid JSON — the instance may be serving a CAPTCHA "
                "page or has been upgraded upstream. Inspect: curl -s "
                f"'{base_url}/search?q=test&format=json'"
            ) from exc


async def searxng_search(
    q: str,
    language: str | None = None,
    categories: str | None = None,
    engines: str | None = None,
    time_range: Literal["day", "week", "month", "year"] | None = None,
    safesearch: int = 1,
) -> str:
    """Search the web via the self-hosted SearXNG metasearch API (JSON passthrough).

    Thin wrapper over the native SearXNG JSON endpoint
    (GET {SEARXNG_URL}/search?format=json). The upstream JSON body is passed through
    verbatim: `results` (url/title/content/engine/score per item), `unresponsive_engines`
    (engines that failed for this query — treat non-empty as degraded-quality signal),
    plus `query`/`number_of_results`/`suggestions`/`answers`/`infoboxes` when present.

    Args:
        q (str, required): search query, 1-256 chars (e.g. 'langgraph streaming' or '智谱 GLM 发布').
        language (str, optional): engine language hint, e.g. 'zh-CN' or 'en'.
        categories (str, optional): comma-separated SearXNG categories, default 'general'.
        engines (str, optional): comma-separated engine allowlist (e.g. 'bing,duckduckgo').
        time_range (str, optional): freshness filter, 'day'|'week'|'month'|'year'.
        safesearch (int, optional): 0=off / 1=moderate / 2=strict (default 1).

    Returns:
        str: JSON string — the verbatim SearXNG JSON response (ensure_ascii=False).

    Error Handling:
        - Missing/invalid SEARXNG_URL env → ToolError naming the variable.
        - HTTP 403 → ToolError with settings.yml remediation hint.
        - HTTP 429 → ToolError (rate limited; fall down the fallback chain).
        - Timeout → ToolError with container check hint.
        - Non-JSON body → ToolError (possible CAPTCHA page / upstream change).
    """
    # 扁平工具面（LLM 直调 {q, ...} 无嵌套包装）+ Pydantic 模型完整校验（strip/min/max/
    # 枚举/未知参数拒收全在模型层，单测钉住）
    validated = SearxngSearchInput(
        q=q, language=language, categories=categories, engines=engines,
        time_range=time_range, safesearch=safesearch,
    )
    base_url = _searxng_base_url()
    forward: dict[str, str] = {"q": validated.q}
    if validated.language is not None:
        forward["language"] = validated.language
    if validated.categories is not None:
        forward["categories"] = validated.categories
    if validated.engines is not None:
        forward["engines"] = validated.engines
    if validated.time_range is not None:
        forward["time_range"] = validated.time_range
    forward["safesearch"] = str(validated.safesearch)

    payload = await _fetch_json(base_url, forward)
    return json.dumps(payload, ensure_ascii=False)


# 工具注册：仅暴露 search 类工具（契约注记「wrapper 仅暴露 search 类工具」——工具面唯一）
mcp.tool(
    name="searxng_search",
    annotations=ToolAnnotations(
        title="Search via self-hosted SearXNG",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)(searxng_search)


def main() -> None:
    """stdio 传输启动入口（python -m app.agent.tools.searxng_mcp）。"""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
