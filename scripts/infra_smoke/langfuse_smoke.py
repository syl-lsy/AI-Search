#!/usr/bin/env python3
"""S0 基建 smoke · langfuse —— web/worker/clickhouse 健康 + /api/public/health + S3 组态核对。

范围（DEV §3.1 第 6 项 / DEV §3.2 ③ / plan 阶段 3 AI 测试）：
    - langfuse-web / langfuse-worker / clickhouse 容器 healthy；3100 可达；
      （宿主端口 2026-11-06 重映射 3000→3100，容器内 3030 不变）；
    - GET /api/public/health → 200；
    - web 容器 S3 组态核对：三通道桶名 = langfuse 且 S3 凭据已注入（仅 exit code，值不打印）
      —— 桶名错配 = trace 上传静默 403（DEV §3.2 ④ 警示）。
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
HEALTH_URL = "http://127.0.0.1:3100/api/public/health"


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


def _emit(component: str, checks: list[dict], pending: list[str]) -> int:
    passed = sum(1 for c in checks if c["pass"])
    result = {
        "component": component,
        "pass": all(c["pass"] for c in checks),
        "summary": f"{passed}/{len(checks)} checks 过",
        "checks": checks,
        "pending": pending,
    }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["pass"] else 1


# ── 组件检查 ────────────────────────────────────────────────────────────────

def main() -> int:
    _load_env()  # 本 smoke 暂无需 .env 值的检查（S3 凭据走容器 env 核对）；保留读取纪律一致性
    checks: list[dict] = []
    pending: list[str] = [
        "pk/sk 测试事件上报（API Keys 进 .env 后，§3.2 ③）",
        "双 trace（网关侧 + 应用侧）session=thread_id 聚合——B 组（依赖 LiteLLM 调用）",
    ]

    for service in ("langfuse-web", "langfuse-worker", "clickhouse"):
        running, healthy, detail = _ps(service)
        checks.append({
            "name": f"container_healthy_{service}",
            "pass": running and (healthy is not False),
            "detail": detail + ("（仅要求 running）" if healthy is None else ""),
        })

    try:
        with socket.create_connection(("127.0.0.1", 3100), timeout=2):
            ok = True
    except OSError:
        ok = False
    checks.append({"name": "port_3100_open", "pass": ok,
                   "detail": "127.0.0.1:3100 " + ("可达" if ok else "不可达")})

    status, _, body = _http(HEALTH_URL)
    health_ok = status == 200
    checks.append({
        "name": "public_health_endpoint",
        "pass": health_ok,
        "detail": f"GET /api/public/health → HTTP {status}" if status else "3100 无 HTTP 响应",
    })

    # S3 组态核对（web 容器 env 仅 exit code 判定，值不打印）
    r = subprocess.run(
        ["docker", "compose", "--project-directory", ".", "-f", "deploy/docker-compose.yml",
         "exec", "-T", "langfuse-web", "sh", "-c",
         'test "$LANGFUSE_S3_EVENT_UPLOAD_BUCKET" = "langfuse" '
         '&& test -n "$LANGFUSE_S3_EVENT_UPLOAD_ACCESS_KEY_ID" '
         '&& test -n "$LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY" '
         '&& test "$LANGFUSE_S3_MEDIA_UPLOAD_BUCKET" = "langfuse" '
         '&& test "$LANGFUSE_S3_BATCH_EXPORT_BUCKET" = "langfuse"'],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=20,
    )
    checks.append({
        "name": "s3_bucket_config",
        "pass": r.returncode == 0,
        "detail": ("S3 三通道桶名=langfuse 且凭据已注入（防静默 403）" if r.returncode == 0
                   else "S3 组态不符或容器未运行——核对 compose env 与 §3.2 ④ 建桶"),
    })

    return _emit("langfuse", checks, pending)


if __name__ == "__main__":
    sys.exit(main())
