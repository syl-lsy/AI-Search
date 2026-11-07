#!/usr/bin/env python3
"""S0 基建 smoke · nacos —— 鉴权四件套就位 + plugin-states 挂载 + ro 登录/读配置/403 实证 + 内容抽查。

范围（DEV §3.1 第 3/7 项 / DEV §3.2 ① / plan 阶段 1 AI 测试）：
    - 容器 healthy；8849/8081/9849 可达；控制台 8081 HTTP 响应；
    - plugin-states.json 挂载且内容 = {"visibility:nacos": false}；
    - 容器 env 四件套在位（仅 exit code 判定，值不打印）；
    - ro 账号（.env NACOS_USERNAME/NACOS_PASSWORD，非占位时）登录 → 五 dataId 读取（未发布=pending）
      → 内容抽查（spec.routing / max_answer_chars=3000 / 模型矩阵三键 / 备源条目）
      → probe dataId 发布应 403（只读实证；200=授权配错=FAIL）。
纪律：仅标准库；docker 参数表全字面量；URL 仅指 127.0.0.1（协议+host 白名单校验）；
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
DATA_IDS = (
    "ai_search.agent.prompts.json",
    "ai_search.agent.mcp.json",
    "ai_search.agent.spec.json",
    "ai_search.agent.skills.json",
    "ai_search.model.mapping.json",
)
LOGIN_URL = "http://127.0.0.1:8849/nacos/v3/auth/user/login"
PROBE_DATA_ID = "ai_search.probe.write_test.json"
PUBLISH_URL = "http://127.0.0.1:8849/nacos/v3/admin/cs/config"


# ── 通用：.env 解析（值绝不打印） ─────────────────────────────────────────────

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
    """剥内联注释与引号。"""
    v = value.strip()
    idx = v.find(" #")
    if idx == -1:
        idx = v.find("\t#")
    if idx != -1:
        v = v[:idx]
    return v.strip().strip('"').strip("'")


def _is_placeholder(v: str) -> bool:
    """<secret>/<GEN>/<专用只读账号>/空 = 占位（未就位）。"""
    c = _clean(v)
    return (not c) or c.startswith("<")


# ── 通用：docker compose（参数表全字面量） ───────────────────────────────────

def _ps_nacos() -> tuple[bool, bool | None, str]:
    try:
        r = subprocess.run(
            ["docker", "compose", "--project-directory", ".", "-f", "deploy/docker-compose.yml",
             "ps", "--format", "json", "nacos"],
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


# ── 通用：仅本机 HTTP（协议 + host 白名单 + 禁重定向） ───────────────────────

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
          headers: dict[str, str] | None = None, timeout: int = 6) -> tuple[int, str, bytes]:
    """返回 (status, content_type, body)。仅允许 127.0.0.1；body 不外泄到输出。"""
    _validate_local_url(url)
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, (resp.headers.get("Content-Type") or ""), resp.read(65536)
    except urllib.error.HTTPError as e:
        return e.code, (e.headers.get("Content-Type") if e.headers else "") or "", e.read(4096)
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, "", b""


def _extract_payload(obj: object) -> dict | None:
    """从 v3 client config 响应中提取业务 payload（含 schema_version 的 dict）。

    实测形态（nacos v3.2.4 /v3/client/cs/config，2026-11-06；与 nacos_init/gen_env 同族形态坑）：
      {"data": "<整段 JSON 字符串>"}            → 解析 data 字符串
      {"data": {"content": "<JSON 字符串>", ...}} → 解析 content 字符串
      {"data": {直装 dict}}                     → 原样
      直装（无 data 包装，schema_version 在顶层）→ 原样（兼容旧形态）
    解析失败或形态不符 → None。
    """
    if not isinstance(obj, dict):
        return None
    if "schema_version" in obj:              # 直装形态（无包装层）
        return obj
    data = obj.get("data")
    if isinstance(data, str):
        try:
            payload = json.loads(data)
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None
    if isinstance(data, dict):
        content = data.get("content")
        if isinstance(content, str):
            try:
                payload = json.loads(content)
            except ValueError:
                return None
            return payload if isinstance(payload, dict) else None
        return data                          # 直装 dict
    return None


# ── 检查项 ──────────────────────────────────────────────────────────────────

def main() -> int:
    env = _load_env()
    checks: list[dict] = []
    pending: list[str] = []

    running, healthy, detail = _ps_nacos()
    checks.append({
        "name": "container_healthy",
        "pass": running and (healthy is not False),
        "detail": detail + ("（仅要求 running）" if healthy is None else ""),
    })

    for port, label in ((8849, "openapi"), (8081, "console"), (9849, "grpc")):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                ok = True
        except OSError:
            ok = False
        checks.append({"name": f"port_{port}_open", "pass": ok,
                       "detail": f"127.0.0.1:{port}（{label}）" + ("可达" if ok else "不可达")})

    # 控制台 HTTP（8081 根路径任意 <500 状态即可）
    status, _, _ = _http("http://127.0.0.1:8081/")
    checks.append({
        "name": "console_http_up",
        "pass": 0 < status < 500,
        "detail": f"GET http://127.0.0.1:8081/ → HTTP {status}" if status else "8081 无 HTTP 响应",
    })

    # plugin-states 挂载与内容（内容非敏感——契约预置值）
    if running:
        r = subprocess.run(
            ["docker", "compose", "--project-directory", ".", "-f", "deploy/docker-compose.yml",
             "exec", "-T", "nacos", "cat", "/home/nacos/data/plugin/plugin-states.json"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=20,
        )
        content = (r.stdout or "").strip()
        checks.append({
            "name": "plugin_states_mounted",
            "pass": r.returncode == 0 and content == '{"visibility:nacos": false}',
            "detail": ("挂载内容正确（禁 visibility 插件）" if r.returncode == 0 and content == '{"visibility:nacos": false}'
                       else f"挂载内容不符（returncode={r.returncode}，长度={len(content)}）"),
        })
        # 鉴权四件套在位（仅 exit code 判定，值不打印）
        r = subprocess.run(
            ["docker", "compose", "--project-directory", ".", "-f", "deploy/docker-compose.yml",
             "exec", "-T", "nacos", "sh", "-c",
             'test -n "$NACOS_AUTH_TOKEN" && test -n "$NACOS_AUTH_IDENTITY_KEY" && '
             'test -n "$NACOS_AUTH_IDENTITY_VALUE" && test "$NACOS_AUTH_ENABLE" = "true"'],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=20,
        )
        checks.append({
            "name": "auth_env_present",
            "pass": r.returncode == 0,
            "detail": "容器 env 四件套在位" if r.returncode == 0 else "容器 env 四件套缺失或不完整",
        })
    else:
        pending.append("容器未运行——挂载/鉴权 env 检查待 make infra-up 后复跑")

    # ── ro 登录 → 五 dataId 读取 + 内容抽查 + 403 实证（凭据未就位则 pending） ──
    user = _clean(env.get("NACOS_USERNAME", ""))
    password = _clean(env.get("NACOS_PASSWORD", ""))
    if _is_placeholder(user) or _is_placeholder(password):
        pending.append("NACOS_USERNAME/NACOS_PASSWORD 未就位（占位）——ro 登录/读配置/403 实证"
                       "待 §3.2 ①（管理员改密、建 ro 账号、.env 收口）后复跑")
        return _emit(checks, pending)

    login_status, _, login_body = _http(
        LOGIN_URL, method="POST",
        data=urllib.parse.urlencode({"username": user, "password": password}).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    token = ""
    try:
        obj = json.loads(login_body.decode("utf-8", errors="replace"))
        for container in (obj, obj.get("data") or {}):
            if isinstance(container, dict):
                tok = container.get("accessToken") or container.get("access_token")
                if isinstance(tok, str) and tok:
                    token = tok
                    break
    except ValueError:
        token = ""
    checks.append({
        "name": "ro_login",
        "pass": login_status == 200 and bool(token),
        "detail": f"POST /nacos/v3/auth/user/login → HTTP {login_status}" + ("，accessToken 已取得（不回显）" if token else "，未取得 accessToken"),
    })
    if not token:
        return _emit(checks, pending)

    # 五 dataId 读取（200+schema_version=1=过；未发布=pending）
    contents: dict[str, dict] = {}
    for data_id in DATA_IDS:
        url = ("http://127.0.0.1:8849/nacos/v3/client/cs/config?dataId=" + urllib.parse.quote(data_id, safe="")
               + "&groupName=ai-search&namespaceId=ai-search-dev")
        status, _, body = _http(url, headers={"accessToken": token})
        obj = None
        try:
            obj = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            obj = None
        payload = _extract_payload(obj)      # 剥 v3 包装层（{"data": ...}/{"data":{"content": ...}}）
        if status == 200 and isinstance(payload, dict) and payload.get("schema_version") == 1:
            contents[data_id] = payload
            checks.append({"name": f"read_{data_id}", "pass": True, "detail": "读取成功且 schema_version=1"})
        elif status in (404, 400, 403) and not (isinstance(payload, dict) and payload.get("schema_version")):
            pending.append(f"{data_id} 未发布或不可读（HTTP {status}）——跑 make nacos-init 或控制台手工建")
        else:
            checks.append({"name": f"read_{data_id}", "pass": False,
                           "detail": f"读取异常：HTTP {status}" + ("，schema_version≠1" if isinstance(payload, dict) else "")})

    # 内容抽查（§3.4 第 2 条；仅在对应 dataId 可读时）
    spec = contents.get("ai_search.agent.spec.json")
    if spec:
        routing = spec.get("routing") or {}
        modes = routing.get("modes") or {}
        spot_ok = (
            bool(modes)
            and routing.get("default_mode") in modes
            and spec.get("fast", {}).get("max_answer_chars") == 3000
            and spec.get("kb", {}).get("top_k_per_query") == 8
            and "rerank_timeout_s" in (spec.get("kb") or {})
        )
        checks.append({"name": "spot_spec", "pass": spot_ok,
                       "detail": "routing/modes、max_answer_chars=3000、kb 节齐" if spot_ok
                       else "routing 或 max_answer_chars=3000 或 kb 节不符契约"})
    mapping = contents.get("ai_search.model.mapping.json")
    if mapping:
        options = (mapping.get("chat") or {}).get("options") or {}
        spot_ok = (
            set(options.keys()) >= {"qwen", "glm", "deepseek"}
            and (mapping.get("chat") or {}).get("default") == "qwen"
            and mapping.get("light") == "qwen3.5-4b-ollama"
            and mapping.get("memory") == "chat-speed"
        )
        checks.append({"name": "spot_model_mapping", "pass": spot_ok,
                       "detail": "chat.options 三键 + default=qwen + light=qwen3.5-4b-ollama + memory=chat-speed"
                       if spot_ok else "模型映射矩阵与契约不符"})
    mcp = contents.get("ai_search.agent.mcp.json")
    if mcp:
        servers = mcp.get("servers") or {}
        anysearch = servers.get("anysearch") or {}
        crawl4ai = servers.get("crawl4ai") or {}
        spot_ok = (
            anysearch.get("role") == "fallback_search"
            and crawl4ai.get("role") == "url_reader_backup"
            and crawl4ai.get("transport") == "sse"
        )
        checks.append({"name": "spot_mcp_backup", "pass": spot_ok,
                       "detail": "anysearch(role=fallback_search)+crawl4ai(role=url_reader_backup,sse) 在位"
                       if spot_ok else "备源条目与契约不符"})

    # ro 只读实证：probe dataId 发布应 403（DEV §3.2 ①；200=授权配错=FAIL）
    probe_status, _, _ = _http(
        PUBLISH_URL, method="POST",
        data=urllib.parse.urlencode({
            "dataId": PROBE_DATA_ID, "groupName": "ai-search", "namespaceId": "ai-search-dev",
            "content": json.dumps({"schema_version": 1, "probe": True}, ensure_ascii=False),
            "type": "json", "accessToken": token,
        }).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded", "accessToken": token},
    )
    if probe_status == 403:
        checks.append({"name": "ro_write_denied_403", "pass": True,
                       "detail": "probe 发布返回 403（只读边界实证，probe 未落库）"})
    elif probe_status == 200:
        checks.append({"name": "ro_write_denied_403", "pass": False,
                       "detail": "probe 发布返回 200——只读账号竟能写=授权配错！"
                                 "须回查权限，并手工删除 probe dataId ai_search.probe.write_test.json"})
    elif probe_status == 404:
        pending.append("probe 发布端点 404——v3.2.4 admin 发布路径形态复核后再做 403 实证")
    else:
        checks.append({"name": "ro_write_denied_403", "pass": False,
                       "detail": f"probe 发布异常：HTTP {probe_status}（预期 403）"})

    return _emit(checks, pending)


def _emit(checks: list[dict], pending: list[str]) -> int:
    passed = sum(1 for c in checks if c["pass"])
    result = {
        "component": "nacos",
        "pass": all(c["pass"] for c in checks),
        "summary": f"{passed}/{len(checks)} checks 过",
        "checks": checks,
        "pending": pending,
    }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
