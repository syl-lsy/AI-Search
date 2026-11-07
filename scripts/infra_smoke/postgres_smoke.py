#!/usr/bin/env python3
"""S0 基建 smoke · postgres —— 三库三角色 + pgvector + 健康检查。

范围（DEV §3.1 第 1 项 / plan 阶段 0 AI 测试）：
    - 完器 running 且 healthy；host 端口 15432 可达；
    - 三库存在：ai_search / langfuse / litellm；三角色存在（只列名，不查密码）；
    - ai_search 库 vector 扩展有版本号。
纪律：仅标准库；docker 交互统一 `docker compose --project-directory . -f deploy/docker-compose.yml
    exec -T …`（参数表全字面量，无变量拼进命令）；读 .env 但绝不打印任何值；
    stdout 最后一行 = 单行紧凑 JSON 裁决；exit 0 = 全过。
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _compose_ps_postgres() -> tuple[bool, bool | None, str]:
    """返回 (running, healthy_or_None, 状态描述)。"""
    try:
        r = subprocess.run(
            ["docker", "compose", "--project-directory", ".", "-f", "deploy/docker-compose.yml",
             "ps", "--format", "json", "postgres"],
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


def _exec_psql(db_args: list[str]) -> subprocess.CompletedProcess:
    # 参数表全字面量拼接在调用处（无变量进 argv——安全扫描口径）
    return subprocess.run(
        ["docker", "compose", "--project-directory", ".", "-f", "deploy/docker-compose.yml",
         "exec", "-T", "postgres", "psql", "-U", "postgres"] + db_args,
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
    )


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


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


def main() -> int:
    checks: list[dict] = []
    pending: list[str] = ["迁移可用性（make db-migrate/db-upgrade）归 B1 工单验收（DEV §3.1 第 8 项）"]

    running, healthy, detail = _compose_ps_postgres()
    checks.append({
        "name": "container_healthy",
        "pass": running and (healthy is not False),
        "detail": detail + ("（仅要求 running）" if healthy is None else ""),
    })
    open_15432 = _port_open(15432)
    checks.append({"name": "port_15432_open", "pass": open_15432,
                   "detail": "127.0.0.1:15432 " + ("可达" if open_15432 else "不可达")})

    if running:
        # 三库存在（只打印库名，非敏感）
        r = _exec_psql(["-tAc",
                        "SELECT datname FROM pg_database WHERE datname IN ('ai_search','langfuse','litellm') ORDER BY datname;"])
        found = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
        checks.append({
            "name": "three_databases_exist",
            "pass": all(db in found for db in ("ai_search", "langfuse", "litellm")),
            "detail": "发现库：" + (",".join(found) if found else "（无）") + "；期望：ai_search,langfuse,litellm",
        })

        # pgvector 扩展（版本号即 §3.1 第 1 项通过判据；版本号非敏感）
        r = _exec_psql(["-d", "ai_search", "-tAc",
                        "SELECT extversion FROM pg_extension WHERE extname='vector';"])
        ver = (r.stdout or "").strip()
        checks.append({
            "name": "pgvector_extension",
            "pass": bool(ver),
            "detail": f"vector extversion={ver}" if ver else "vector 扩展不存在（检查 init.sh 是否执行）",
        })

        # 三角色存在（只列角色名，绝不查/打密码）
        r = _exec_psql(["-tAc",
                        "SELECT rolname FROM pg_roles WHERE rolname IN ('ai_search','langfuse','litellm') ORDER BY rolname;"])
        roles = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
        checks.append({
            "name": "three_roles_exist",
            "pass": all(db in roles for db in ("ai_search", "langfuse", "litellm")),
            "detail": "发现角色：" + (",".join(roles) if roles else "（无）"),
        })
    else:
        pending.append("容器未运行——三库/角色/扩展检查待 make infra-up 后复跑")

    return _emit("postgres", checks, pending)


if __name__ == "__main__":
    sys.exit(main())
