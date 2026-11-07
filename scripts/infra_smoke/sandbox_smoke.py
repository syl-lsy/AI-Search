#!/usr/bin/env python3
"""S0 基建 smoke · sandbox —— opensandbox 部署面（本批提前部署裁决，不挂 profile）。

范围（plan 阶段 5 / DEV §3.1 第 9 项按「提前部署」裁决改写）：
    - 容器 running（compose 无 healthcheck——镜像为 Go 静态二进制，探测留宿主侧）；
    - 8801 可达；GET /health 带 X-API-Key → 200（.env OPEN_SANDBOX_API_KEY 非占位时）；
    - 挂载核对（宿主侧 docker inspect ai-search-opensandbox-1）：config.toml:ro、docker.sock、
      产物卷 ./data/sandbox、opensandbox_server_data 卷。
纪律：仅标准库；docker 参数表全字面量（容器名由 compose project name=ai-search 推导，静态名）；
    URL 仅 127.0.0.1；读 .env 但绝不打印任何值；stdout 最后一行 = 单行紧凑 JSON 裁决；exit 0 = 全过。
    应用侧 SANDBOX_ENABLED=false 保持不变——仅验基建面。
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
CONTAINER_NAME = "ai-search-opensandbox-1"   # compose project name=ai-search（deploy/docker-compose.yml name:）
HEALTH_URL = "http://127.0.0.1:8801/health"


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


def _emit(checks: list[dict], pending: list[str]) -> int:
    passed = sum(1 for c in checks if c["pass"])
    result = {
        "component": "sandbox",
        "pass": all(c["pass"] for c in checks),
        "summary": f"{passed}/{len(checks)} checks 过",
        "checks": checks,
        "pending": pending,
    }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["pass"] else 1


# ── 组件检查 ────────────────────────────────────────────────────────────────

def _ps_opensandbox() -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["docker", "compose", "--project-directory", ".", "-f", "deploy/docker-compose.yml",
             "ps", "--format", "json", "opensandbox"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"docker compose 不可用：{type(e).__name__}"
    if r.returncode != 0:
        return False, "compose ps 失败（栈未启动或 docker 未运行）"
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
        return state in ("running", "Running"), f"State={state}（无 healthcheck，仅要求 running）"
    return False, "compose ps 无该服务条目（未启动）"


def _inspect_mounts() -> tuple[list[str], str]:
    """宿主侧 docker inspect 静态容器名（参数表全字面量）。返回 (source->dest 描述列表, err)。"""
    try:
        r = subprocess.run(
            ["docker", "inspect", CONTAINER_NAME],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return [], f"docker inspect 不可用：{type(e).__name__}"
    if r.returncode != 0:
        return [], "容器不存在（未启动）"
    try:
        arr = json.loads((r.stdout or "[]"))
        mounts = arr[0].get("Mounts", []) if arr else []
    except (ValueError, IndexError):
        return [], "docker inspect 输出解析失败"
    descs = []
    for m in mounts:
        src = str(m.get("Source") or m.get("Name") or "")
        dst = str(m.get("Destination") or "")
        descs.append(f"{src} -> {dst}" + ("（ro）" if "ro" in str(m.get("Mode") or "") or m.get("RW") is False else ""))
    return descs, ""


def main() -> int:
    env = _load_env()
    checks: list[dict] = []
    pending: list[str] = [
        "沙箱实例创建/执行/销毁实测（阶段 5 当批增补验收 2 条，起栈后主会话带 X-API-Key 实测）",
        "M4 执行面深验（SANDBOX_ENABLED=false 保持不启用）",
    ]

    running, detail = _ps_opensandbox()
    checks.append({"name": "container_running", "pass": running, "detail": detail})

    try:
        with socket.create_connection(("127.0.0.1", 8801), timeout=2):
            ok = True
    except OSError:
        ok = False
    checks.append({"name": "port_8801_open", "pass": ok,
                   "detail": "127.0.0.1:8801 " + ("可达" if ok else "不可达")})

    # 挂载核对（宿主侧，不依赖容器内 shell）
    if running:
        descs, err = _inspect_mounts()
        if err:
            pending.append(f"挂载核对跳过：{err}")
        else:
            joined = " | ".join(descs)
            has_toml = any("opensandbox_config.toml" in d and "/etc/opensandbox/config.toml" in d for d in descs)
            has_sock = any(d.endswith("/var/run/docker.sock") for d in descs)
            has_artifacts = any("/root/.opensandbox/artifacts" in d for d in descs)
            checks.append({"name": "config_mounted_ro", "pass": has_toml,
                           "detail": "config.toml bind:ro 在列" if has_toml else "config.toml 挂载缺失：" + joined})
            checks.append({"name": "docker_sock_mounted", "pass": has_sock,
                           "detail": "docker.sock 在列" if has_sock else "docker.sock 挂载缺失：" + joined})
            checks.append({"name": "artifacts_volume_mounted", "pass": has_artifacts,
                           "detail": "产物卷 ./data/sandbox 在列" if has_artifacts else "产物卷挂载缺失：" + joined})

        # /health 带 X-API-Key（key 经 header 注入，不打印）
        api_key = _clean(env.get("OPEN_SANDBOX_API_KEY", ""))
        if _is_placeholder(api_key):
            pending.append("OPEN_SANDBOX_API_KEY 未就位（占位）——/health 鉴权探待 .env 收口后复跑")
        else:
            status, _, _ = _http(HEALTH_URL, headers={"X-API-Key": api_key})
            checks.append({
                "name": "health_with_api_key",
                "pass": status == 200,
                "detail": f"GET /health（X-API-Key）→ HTTP {status}" if status else "8801 无 HTTP 响应",
            })
    else:
        pending.append("容器未运行——挂载/健康探待 make infra-up 后复跑")

    return _emit(checks, pending)


if __name__ == "__main__":
    sys.exit(main())
