#!/usr/bin/env python3
"""S0 基建 smoke · litellm —— 网关健康 + Master Key 列别名（DB 模式可见性）。

范围（DEV §3.1 第 4 项 / DEV §3.2 ② / plan 阶段 4 AI 测试）：
    - litellm / postgres / redis 容器 healthy；4100 可达；
      （宿主端口 2026-11-06 重映射 4000→4100，容器内 4000 不变）；
    - GET /health/liveliness → 200（判据：'I'm alive!'）；
    - .env LITELLM_MASTER_KEY 非占位时：GET /v1/models（Bearer Master Key）→
      9 别名齐（chat-standard(-glm/-deepseek) / chat-speed(-glm/-deepseek) /
      qwen3.5-4b-ollama / text-embedding-v4 / qwen3-rerank）——DB 模式别名注册实证。
纪律：仅标准库；docker 参数表全字面量；URL 仅 127.0.0.1（协议+host 白名单）；
    读 .env 但绝不打印任何值（别名/健康文案非敏感可输出）；stdout 最后一行 = 单行紧凑 JSON 裁决。
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
LIVENESS_URL = "http://127.0.0.1:4100/health/liveliness"
MODELS_URL = "http://127.0.0.1:4100/v1/models"
EXPECTED_ALIASES = (
    "chat-standard", "chat-standard-glm", "chat-standard-deepseek",
    "chat-speed", "chat-speed-glm", "chat-speed-deepseek",
    "qwen3.5-4b-ollama", "text-embedding-v4", "qwen3-rerank",
)


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
          headers: dict[str, str] | None = None, timeout: int = 6) -> tuple[int, str, bytes]:
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


def _ps(service_literal: str) -> tuple[bool, bool | None, str]:
    try:
        r = subprocess.run(
            ["docker", "compose", "--project-directory", ".", "-f", "deploy/docker-compose.yml",
             "ps", "--format", "json", service_literal],
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
        "component": "litellm",
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
        "五类调用 smoke（chat-standard / chat-speed / light / embedding / rerank）——B 组（上游密钥备齐后）",
        "light 本地 qwen3.5:4b 调用与宿主 Ollama 连通——B 组（R7/DEV §3.1 第 5 项）",
        "UI :4100/ui 登录与 DB 模式实证（Models 页/Spend Logs）——用户兜底测试",
    ]

    for service in ("litellm", "postgres", "redis"):
        running, healthy, detail = _ps(service)
        checks.append({
            "name": f"container_healthy_{service}",
            "pass": running and (healthy is not False),
            "detail": detail + ("（仅要求 running）" if healthy is None else ""),
        })

    try:
        with socket.create_connection(("127.0.0.1", 4100), timeout=2):
            ok = True
    except OSError:
        ok = False
    checks.append({"name": "port_4100_open", "pass": ok,
                   "detail": "127.0.0.1:4100 " + ("可达" if ok else "不可达")})

    status, _, body = _http(LIVENESS_URL)
    alive = status == 200 and b"alive" in body
    checks.append({
        "name": "liveliness_endpoint",
        "pass": alive,
        "detail": f"GET /health/liveliness → HTTP {status}" + ("，'I'm alive!'" if alive else ""),
    })

    master_key = _clean(env.get("LITELLM_MASTER_KEY", ""))
    if _is_placeholder(master_key):
        pending.append("LITELLM_MASTER_KEY 未就位（占位）——/v1/models 别名核对待 .env 收口后复跑")
    else:
        status, _, body = _http(MODELS_URL, headers={"Authorization": "Bearer " + master_key})
        found: list[str] = []
        if status == 200:
            try:
                obj = json.loads(body.decode("utf-8", errors="replace"))
                found = [d.get("id", "") for d in obj.get("data", []) if isinstance(d, dict)]
            except ValueError:
                found = []
        missing = [a for a in EXPECTED_ALIASES if a not in found]
        checks.append({
            "name": "nine_aliases_registered",
            "pass": status == 200 and not missing,
            "detail": (f"HTTP {status}，别名齐（{len(found)} 个）" if not missing and found
                       else f"HTTP {status}，缺别名：" + (",".join(missing) if missing else "未解析到模型列表")),
        })

    return _emit(checks, pending)


if __name__ == "__main__":
    sys.exit(main())
