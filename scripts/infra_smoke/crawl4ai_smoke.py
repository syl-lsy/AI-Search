#!/usr/bin/env python3
"""S0 基建 smoke · crawl4ai —— 部署面 + Bearer 鉴权墙 + MCP 端点连通（R12 部署面）。

范围（DEV §3.1 第 10 项 / plan 阶段 6）：
    - 容器 healthy（镜像 unclecode/crawl4ai:0.9.5，2026-11-05 锁定当时 0.9.x 最新稳定）；
    - 11235 可达；GET /health → 200；
    - 鉴权墙：无 token POST /md 应 401/403；带 .env CRAWL4AI_API_TOKEN（非占位时）POST /md
      （空 body，仅过鉴权不实抓）应非 401/403；
    - MCP 端点：GET /mcp/sse 带 token + Accept: text/event-stream → 200 且响应头 Content-Type
      含 text/event-stream 即过（只读响应头、不读流式 body；404 = 端点形态复核）。
纪律：仅标准库；docker 参数表全字面量；URL 仅 127.0.0.1（协议+host 白名单）；
    读 .env 但绝不打印任何值；stdout 最后一行 = 单行紧凑 JSON 裁决；exit 0 = 全过。
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HEALTH_URL = "http://127.0.0.1:11235/health"
MD_URL = "http://127.0.0.1:11235/md"
MCP_SSE_URL = "http://127.0.0.1:11235/mcp/sse"


# ── 通用（各 smoke 同款） ────────────────────────────────────────────────────

def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    p = REPO_ROOT / ".env"
    if not p.exists():
        return env
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if not k or " " in k:
            continue
        env[k] = v
    return env


def _clean(value: str) -> str:
    v = value.strip()
    idx = v.find(" #")
    if idx == -1:
        idx = v.find("\t#")
    if idx != -1:
        v = v[:idx]
    return v.strip().strip('"').strip("'")


def _is_placeholder(v: str) -> bool:
    c = _clean(v)
    return (not c) or c.startswith("<")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(newurl, code, "redirect disabled", headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))  # 禁代理直连本机（macOS 系统代理会劫持 127.0.0.1 请求）


def _validate_local_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise ValueError(f"blocked non-http(s) URL scheme: {parts.scheme or '(empty)'}")
    if (parts.hostname or "") != "127.0.0.1":
        raise ValueError(f"blocked non-local target host: {parts.hostname or '(empty)'}")


def _http(url: str, method: str = "GET", data: bytes | None = None,
          headers: dict[str, str] | None = None, timeout: int = 6,
          read_body: bool = True) -> tuple[int, str, bytes]:
    _validate_local_url(url)
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            # read_body=False：只取响应头立即返回、不读 body——SSE 等不结束的流式响应读 body
            # 会阻塞到超时误判失败（判定只看头时一律用它）
            body = resp.read(65536) if read_body else b""
            return resp.status, (resp.headers.get("Content-Type") or ""), body
    except urllib.error.HTTPError as e:
        return e.code, (e.headers.get("Content-Type") if e.headers else "") or "", e.read(4096)
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, "", b""


def _ps_crawl4ai() -> tuple[bool, bool | None, str]:
    try:
        r = subprocess.run(
            ["docker", "compose", "--project-directory", ".", "-f", "deploy/docker-compose.yml",
             "ps", "--format", "json", "crawl4ai"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, None, f"docker compose 不可用：{type(e).__name__}"
    if r.returncode != 0:
        return False, None, "compose ps 失败（栈未启动或 docker 未运行）"
    text = (r.stdout or "").strip()
    entries: list[dict] = []
    if text.startswith("["):
        try:
            entries = json.loads(text)
        except ValueError:
            entries = []
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
    for e in entries:
        state = str(e.get("State") or e.get("state") or "")
        health = e.get("Health") or e.get("health") or {}
        status = health.get("Status") if isinstance(health, dict) else None
        running = state in ("running", "Running")
        detail = f"State={state}" + (f", Health={status}" if status else ", 无 healthcheck")
        return running, (status == "healthy" if status else None), detail
    return False, None, "compose ps 无该服务条目（未启动）"


def _emit(checks: list[dict], pending: list[str]) -> int:
    passed = sum(1 for c in checks if c["pass"])
    result = {
        "component": "crawl4ai",
        "pass": all(c["pass"] for c in checks),
        "summary": f"{passed}/{len(checks)} checks 过",
        "checks": checks,
        "pending": pending,
    }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["pass"] else 1


# ── 组件检查 ────────────────────────────────────────────────────────────────

def main() -> int:
    env = _load_env()
    checks: list[dict] = []
    pending: list[str] = [
        "实抓一页对照 /md 输出（R12 剩余部分，B 组）",
        "AnySearch 配额观测与 MCP 装载（R12，B 组）",
    ]

    running, healthy, detail = _ps_crawl4ai()
    checks.append({
        "name": "container_healthy",
        "pass": running and (healthy is not False),
        "detail": detail + ("（仅要求 running）" if healthy is None else ""),
    })

    try:
        with socket.create_connection(("127.0.0.1", 11235), timeout=2):
            ok = True
    except OSError:
        ok = False
    checks.append({"name": "port_11235_open", "pass": ok,
                   "detail": "127.0.0.1:11235 " + ("可达" if ok else "不可达")})

    status, _, _ = _http(HEALTH_URL)
    checks.append({
        "name": "health_endpoint",
        "pass": status == 200,
        "detail": f"GET /health → HTTP {status}" if status else "11235 无 HTTP 响应",
    })

    # 鉴权墙：无 token 应被拒（401/403）
    status, _, _ = _http(MD_URL, method="POST", data=b"")
    wall_ok = status in (401, 403)
    checks.append({
        "name": "auth_wall_rejects_anonymous",
        "pass": wall_ok,
        "detail": f"无 token POST /md → HTTP {status}（预期 401/403）",
    })

    token = _clean(env.get("CRAWL4AI_API_TOKEN", ""))
    if _is_placeholder(token):
        pending.append("CRAWL4AI_API_TOKEN 未就位（占位）——带 token 放行核对待 .env 收口后复跑")
    else:
        # 带 token 过墙（空 body 不实抓——预期 400/422 等非 401/403 状态即证鉴权通过）
        status, _, _ = _http(MD_URL, method="POST", data=b"",
                             headers={"Authorization": "Bearer " + token,
                                      "Content-Type": "application/json"})
        passed_auth = status not in (0, 401, 403)
        checks.append({
            "name": "auth_token_accepted",
            "pass": passed_auth,
            "detail": f"带 token POST /md（空 body 不实抓）→ HTTP {status}（非 401/403 即过墙）",
        })
        # MCP SSE 端点连通（token 经 header 注入；Accept 声明流式协商）。
        # 判定只看响应头（200 + Content-Type 含 text/event-stream）——read_body=False 不读流式
        # body（SSE 流不结束，读 body 会阻塞到超时误判失败）
        status, ctype, _ = _http(MCP_SSE_URL, read_body=False, timeout=8,
                                 headers={"Authorization": "Bearer " + token,
                                          "Accept": "text/event-stream"})
        if status == 200 and "text/event-stream" in ctype:
            checks.append({"name": "mcp_sse_endpoint", "pass": True,
                           "detail": "GET /mcp/sse → 200 text/event-stream（仅验响应头，未读流式 body）"})
        elif status == 404:
            pending.append("/mcp/sse 返回 404——MCP 端点形态复核（R12；上游支持 streamable http 后可切）")
        else:
            checks.append({"name": "mcp_sse_endpoint", "pass": False,
                           "detail": f"GET /mcp/sse → HTTP {status}, content-type={ctype or '(空)'}"})

    return _emit(checks, pending)


if __name__ == "__main__":
    sys.exit(main())
