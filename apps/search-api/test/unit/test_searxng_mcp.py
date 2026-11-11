"""unit 层：searxng 薄 stdio MCP wrapper（B3 第 0 步——nacos-config.md §2 searxng 条目注记）。

钉住的语义（冻结契约，逐条对应测试）：
- 仅暴露 search 类工具（工具面唯一：searxng_search，readOnlyHint=true）——wrapper 薄，
  不做任何字段裁剪/结果策略（透传语义，registry 侧策略归 S1）；
- 透传 SearXNG 原生 JSON API：GET {SEARXNG_URL}/search?q=…&format=json，返回上游 JSON
  原文（json.dumps ensure_ascii=False，中文不被转义）；
- SEARXNG_URL 从进程环境读取（wrapper 子进程 env，nacos-config.md §2 v1.18——mcp.json
  不承载端点值）；缺失/非 http(s) scheme 可操作报错；
- 入参校验：q 必填非空（strip 后）、≤256 字符；time_range 限枚举；未知参数拒收；
- 失败语义四类（先出红测试）：上游 403（format 未白名单/limiter 拦截）/ 超时 /
  非 JSON 响应（上游改版迹象）/ 上游 429——全部 ToolError 且带可操作修复提示；
- HTTP 全 mock（httpx.MockTransport，零真网——unit 层纪律）。

stdio 握手冒烟（initialize/tools/list/tools/call 真子进程）归接入性验证环节执行，
不进 unit（子进程 spawn 非纯逻辑）。
"""

import json
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

from app.agent.tools import searxng_mcp

SEARXNG_URL = "http://127.0.0.1:18080"


def _client_with(handler: Any) -> None:
    """把模块 HTTP 出口换到 MockTransport（mock HTTP，零真网）。"""

    def _factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=searxng_mcp.SEARCH_TIMEOUT_S,
        )

    searxng_mcp._build_client = _factory  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _searxng_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARXNG_URL", SEARXNG_URL)


async def test_surface_only_search_tool() -> None:
    """工具面唯一：仅暴露 searxng_search 一个 search 类工具，readOnly=true。"""
    tools = await searxng_mcp.mcp.list_tools()
    assert [t.name for t in tools] == ["searxng_search"]
    tool = tools[0]
    assert tool.annotations is not None and tool.annotations.readOnlyHint is True
    assert tool.description  # 可操作描述


async def test_tool_schema_flat_not_nested() -> None:
    """工具 schema 扁平：q 为顶层必填属性，无 params 嵌套包装（LLM 工具面友好）。

    stdio 真子进程冒烟实证过嵌套形态的问题：签名取 params: Model 时 FastMCP 生成
    {params: {...}} 嵌套 schema，客户端须传 {"params": {"q": ...}}——本测试钉死扁平形态。
    """
    tools = await searxng_mcp.mcp.list_tools()
    schema = tools[0].inputSchema
    assert "q" in schema.get("properties", {})
    assert schema["properties"]["q"]["type"] == "string"
    assert "q" in schema.get("required", [])
    assert "params" not in schema.get("properties", {})


async def test_search_success_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """正常 JSON：透传上游原文；GET {URL}/search 且 format=json 与 q 在查询串。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "query": "langgraph",
                "results": [
                    {
                        "url": "https://langchain-ai.github.io/langgraph/",
                        "title": "LangGraph",
                        "content": "Build resilient language agents as graphs.",
                        "engine": "bing",
                    }
                ],
                "unresponsive_engines": [],
            },
        )

    _client_with(handler)
    out = await searxng_mcp.searxng_search(
        q="langgraph"
    )

    assert captured["url"].startswith(f"{SEARXNG_URL}/search")
    assert captured["params"]["q"] == "langgraph"
    assert captured["params"]["format"] == "json"
    payload = json.loads(out)
    assert payload["results"][0]["url"] == "https://langchain-ai.github.io/langgraph/"
    assert payload["query"] == "langgraph"
    assert payload["unresponsive_engines"] == []


async def test_search_passthrough_chinese_not_escaped() -> None:
    """透传语义：中文原文不被 ensure_ascii 转义（query 与 content 保真）。"""
    body = {"query": "智谱 GLM", "results": [{"title": "智谱 GLM 模型", "content": "中文内容保真"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    _client_with(handler)
    out = await searxng_mcp.searxng_search(q="智谱 GLM")
    assert "智谱 GLM" in out and "中文内容保真" in out


async def test_search_optional_params_forwarded() -> None:
    """可选参数转发：language/categories/time_range/safesearch 进查询串。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"results": []})

    _client_with(handler)
    await searxng_mcp.searxng_search(
        q="python", language="zh-CN", categories="general", time_range="month", safesearch=0,
    )
    assert captured["params"]["language"] == "zh-CN"
    assert captured["params"]["categories"] == "general"
    assert captured["params"]["time_range"] == "month"
    assert captured["params"]["safesearch"] == "0"


async def test_search_upstream_403_is_actionable_tool_error() -> None:
    """上游 403：format 未白名单/limiter 拦截——ToolError 带可操作修复提示。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden")

    _client_with(handler)
    with pytest.raises(ToolError) as ei:
        await searxng_mcp.searxng_search(q="x")
    msg = str(ei.value)
    assert "403" in msg
    assert "format" in msg.lower()  # 指向 settings.yml search.formats 白名单


async def test_search_upstream_429_is_actionable_tool_error() -> None:
    """上游 429：上游引擎限流——ToolError 指明限流语义。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="Too Many Requests")

    _client_with(handler)
    with pytest.raises(ToolError) as ei:
        await searxng_mcp.searxng_search(q="x")
    assert "429" in str(ei.value)


async def test_search_timeout_is_tool_error() -> None:
    """超时：httpx 超时类异常转 ToolError（不向 registry 冒裸 httpx 异常）。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("mocked connect timeout")

    _client_with(handler)
    with pytest.raises(ToolError) as ei:
        await searxng_mcp.searxng_search(q="x")
    msg = str(ei.value)
    assert "timed out" in msg.lower() or "timeout" in msg.lower()


async def test_search_non_json_is_tool_error() -> None:
    """非 JSON（上游改版/CAPTCHA HTML 页）：ToolError 指明响应非 JSON。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="<html>captcha challenge</html>", headers={"content-type": "text/html"}
        )

    _client_with(handler)
    with pytest.raises(ToolError) as ei:
        await searxng_mcp.searxng_search(q="x")
    assert "json" in str(ei.value).lower()


async def test_q_required_rejects_empty() -> None:
    """q 必填：None/空串/纯空白均校验拒绝。"""
    for bad in ("", "   "):
        with pytest.raises(ValidationError):
            searxng_mcp.SearxngSearchInput(q=bad)


def test_q_required_rejects_missing() -> None:
    """q 缺失：模型必填字段拒绝。"""
    with pytest.raises(ValidationError):
        searxng_mcp.SearxngSearchInput()  # type: ignore[call-arg]


def test_q_max_length_bound() -> None:
    """q 上限 256 字符（wrapper 侧 sanity bound，超长拒绝）。"""
    with pytest.raises(ValidationError):
        searxng_mcp.SearxngSearchInput(q="x" * 257)


def test_time_range_enum_restricted() -> None:
    """time_range 限 SearXNG 支持的四枚举，非法值拒绝。"""
    with pytest.raises(ValidationError):
        searxng_mcp.SearxngSearchInput(q="x", time_range="hour")  # type: ignore[arg-type]


def test_extra_fields_forbidden() -> None:
    """未知参数拒收（wrapper 薄透传，不做隐式扩展）。"""
    with pytest.raises(ValidationError):
        searxng_mcp.SearxngSearchInput(q="x", pages=2)  # type: ignore[call-arg]


async def test_missing_searxng_url_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """SEARXNG_URL 缺失：ToolError 指向环境变量名（可操作）。"""
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    with pytest.raises(ToolError) as ei:
        await searxng_mcp.searxng_search(q="x")
    assert "SEARXNG_URL" in str(ei.value)


async def test_searxng_url_scheme_restricted(monkeypatch: pytest.MonkeyPatch) -> None:
    """SEARXNG_URL 仅允许 http/https（非 http scheme 拒绝）。"""
    monkeypatch.setenv("SEARXNG_URL", "ftp://127.0.0.1:18080")
    with pytest.raises(ToolError) as ei:
        await searxng_mcp.searxng_search(q="x")
    assert "http" in str(ei.value).lower()
