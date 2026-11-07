#!/usr/bin/env python3
"""S0 基建 smoke · minio —— 健康 + 三桶 + 可写性 + Service Account 凭据核对。

范围（DEV §3.1 第 2 项 / DEV §3.2 ④ / plan 阶段 2 AI 测试）：
    - 容器 healthy；9100 API /minio/health/live 200；9101 console HTTP 响应；
      （宿主端口 2026-11-06 重映射 9000→9100/9001→9101，容器内 9000/9001 不变）；
    - 三桶齐（ai-search-snapshots / ai-search-kb / langfuse；未建=pending——§3.2 ④ 手工步骤）；
    - snapshots 桶写/读/删测试对象成功（root 凭据经容器内 env 引用，不进 argv、不打印）；
    - Service Account 对（.env MINIO_ACCESS_KEY/MINIO_SECRET_KEY 非占位时）可写 langfuse 桶
      （脚本经 stdin 传入容器，不进 argv；langfuse 桶未建=pending——§3.2 ④ 手工步骤不算部署失败；
      三桶已存在且凭据非占位仍写失败=FAIL——policy/凭据真错误）。
纪律：仅标准库；docker 参数表全字面量；URL 仅 127.0.0.1（协议+host 白名单）；读 .env 绝不打印任何值；
    stdout 最后一行 = 单行紧凑 JSON 裁决；exit 0 = 全过。
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
BUCKETS = ("ai-search-snapshots", "ai-search-kb", "langfuse")
HEALTH_URL = "http://127.0.0.1:9100/minio/health/live"


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
    env = _load_env()
    checks: list[dict] = []
    pending: list[str] = []

    running, healthy, detail = _ps("minio")
    checks.append({
        "name": "container_healthy",
        "pass": running and (healthy is not False),
        "detail": detail + ("（仅要求 running）" if healthy is None else ""),
    })

    for port, label in ((9100, "api"), (9101, "console")):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                ok = True
        except OSError:
            ok = False
        checks.append({"name": f"port_{port}_open", "pass": ok,
                       "detail": f"127.0.0.1:{port}（{label}）" + ("可达" if ok else "不可达")})

    status, _, _ = _http(HEALTH_URL)
    checks.append({
        "name": "api_health_live",
        "pass": status == 200,
        "detail": f"GET /minio/health/live → HTTP {status}" if status else "9100 无 HTTP 响应",
    })

    buckets_found: set[str] = set()
    if running:
        # 桶清单（root 凭据引用容器内 env，不进 argv、不打印；mc alias 的 127.0.0.1:9000
        # 为 docker exec 容器内执行——容器内端口不随宿主重映射，保持 9000 勿改）
        r = subprocess.run(
            ["docker", "compose", "--project-directory", ".", "-f", "deploy/docker-compose.yml",
             "exec", "-T", "minio", "sh", "-c",
             'mc alias set smokelocal http://127.0.0.1:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null 2>&1 '
             '&& mc ls --json smokelocal'],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            checks.append({"name": "buckets_listed", "pass": False,
                           "detail": f"mc 列桶失败（returncode={r.returncode}）——检查容器与凭据"})
        else:
            for line in (r.stdout or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                key = obj.get("key") or ""
                if key:
                    buckets_found.add(key.rstrip("/"))
        if buckets_found:
            missing = [b for b in BUCKETS if b not in buckets_found]
            checks.append({
                "name": "three_buckets_exist",
                "pass": not missing,
                "detail": ("三桶齐：" + ",".join(sorted(buckets_found)) if not missing
                           else "缺桶：" + ",".join(missing) + "（§3.2 ④ 手工建桶后复跑）"),
            })
            # snapshots 桶写/读/删测试对象（root）
            r = subprocess.run(
                ["docker", "compose", "--project-directory", ".", "-f", "deploy/docker-compose.yml",
                 "exec", "-T", "minio", "sh", "-c",
                 'echo ai-search-smoke | mc pipe smokelocal/ai-search-snapshots/.smoke-test '
                 '&& mc stat smokelocal/ai-search-snapshots/.smoke-test >/dev/null '
                 '&& mc rm smokelocal/ai-search-snapshots/.smoke-test'],
                cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
            )
            checks.append({
                "name": "root_write_test",
                "pass": r.returncode == 0,
                "detail": "snapshots 桶写/读/删测试对象成功" if r.returncode == 0 else "写/读/删测试对象失败",
            })
        else:
            # 本脚本不建桶（无 mc mb）——三桶为 §3.2 ④ 手工步骤，未列到任何桶属预期 pending
            pending.append("未列到任何桶——三桶未建（§3.2 ④ 手工步骤）或 mc 不可用，建齐后复跑")

        # Service Account 对核对（凭据经 stdin 传入容器 sh -s，不进 argv；值不打印）
        access = _clean(env.get("MINIO_ACCESS_KEY", ""))
        secret = _clean(env.get("MINIO_SECRET_KEY", ""))
        if _is_placeholder(access) or _is_placeholder(secret):
            pending.append("MINIO_ACCESS_KEY/MINIO_SECRET_KEY 未就位（占位）——Service Account 读写核对待 §3.2 ④")
        else:
            # 写删链退出码必须显式保留：末尾 alias remove 清理命令恒成功，sh 退出码取最后一个
            # 命令——不先捕获 rc，写删失败也会被清理掩蔽成 PASS（2026-11-06 实测假阳性根因：
            # 桶未建时 pipe 实败，判定却过）。写删测试链路本身不变
            script = (
                "mc alias set svclocal http://127.0.0.1:9000 "
                + "'" + access.replace("'", "'\\''") + "' '" + secret.replace("'", "'\\''") + "' "
                ">/dev/null 2>&1 && echo smoke | mc pipe svclocal/langfuse/.smoke-svc-test "
                "&& mc rm svclocal/langfuse/.smoke-svc-test >/dev/null 2>&1; rc=$?; "
                "mc alias remove svclocal >/dev/null 2>&1 || true; exit $rc"
            )
            r = subprocess.run(
                ["docker", "compose", "--project-directory", ".", "-f", "deploy/docker-compose.yml",
                 "exec", "-T", "minio", "sh", "-s"],
                cwd=REPO_ROOT, input=script, capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0:
                checks.append({"name": "service_account_write", "pass": True,
                               "detail": "Service Account 对 langfuse 桶写/删成功（policy 生效）"})
            elif "langfuse" in buckets_found:
                # 三桶已存在（上方 buckets_found 已核实）且 SA 凭据非占位（占位已在本分支前置拦截）
                # ——写失败 = policy 未绑/凭据真错误，判 FAIL 而非 pending（终审修 2）
                checks.append({
                    "name": "service_account_write",
                    "pass": False,
                    "detail": "三桶已存在+SA 凭据非占位，写失败 = policy/凭据真错误"
                              "（§3.2 ④ 核对账号与 policy 绑定）",
                })
            else:
                pending.append("Service Account 对写 langfuse 桶未通过——langfuse 桶未建（§3.2 ④ 前置，"
                               "本脚本不建桶；账号/凭据问题待桶建齐后复跑再分诊，手工步骤不算部署失败）")
    else:
        pending.append("容器未运行——桶/可写性检查待 make infra-up 后复跑")

    return _emit("minio", checks, pending)


if __name__ == "__main__":
    sys.exit(main())
