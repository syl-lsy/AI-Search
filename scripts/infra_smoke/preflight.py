#!/usr/bin/env python3
"""S0 基建 smoke · preflight —— 起栈前端口占用预检。

范围（plan 阶段 0 措施 / DEV §3.1）：检查 11 个 host 端口未被占用——
    15432(postgres) / 9100,9101(minio) / 8081,8849,9849(nacos) / 4100(litellm) /
    3100(langfuse-web) / 11235(crawl4ai) / 16379(redis) / 8801(opensandbox)。
    （2026-11-06 用户裁决宿主端口重映射后的口径；容器内端口不变，不在预检范围）
纪律：从仓库根运行；仅标准库；输出不含任何凭据；stdout 最后一行 = 单行紧凑 JSON 裁决。
注意：本检查在【起栈前】运行——栈已在跑时「端口被占」属预期（先 make infra-down 或跳过本项）。
"""

from __future__ import annotations

import json
import socket
import sys

PORTS: dict[int, str] = {
    15432: "postgres",
    9100: "minio-api",
    9101: "minio-console",
    8081: "nacos-console",
    8849: "nacos-openapi",
    9849: "nacos-grpc",
    4100: "litellm",
    3100: "langfuse-web",
    11235: "crawl4ai",
    16379: "redis",
    8801: "opensandbox",
}


def _port_free(port: int, timeout: float = 1.0) -> bool:
    """True = 端口空闲（无法建立连接）。仅连 127.0.0.1（本机栈口径）。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return False  # 连得上 = 被占用
    except OSError:
        return True


def main() -> int:
    checks = []
    for port, name in sorted(PORTS.items()):
        free = _port_free(port)
        checks.append({
            "name": f"port-{port}-free",
            "pass": free,
            "detail": f"{name} 127.0.0.1:{port} " + ("空闲" if free else "被占用（若为本栈容器先 infra-down）"),
        })
    passed = sum(1 for c in checks if c["pass"])
    result = {
        "component": "preflight",
        "pass": all(c["pass"] for c in checks),
        "summary": f"{passed}/{len(checks)} 端口空闲",
        "checks": checks,
        "pending": [],
    }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
