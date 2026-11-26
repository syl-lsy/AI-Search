#!/usr/bin/env python3
"""S0 基建 smoke · elasticsearch —— 部署面 + jieba 分词插件在位 + 分词黄金断言（M3 B18）。

范围（设计 plan-m3-b18-es-write-20261125.md §10「七组件→八组件」缺口补齐）：
    - 19200 可达；GET / → 200 且 `version.number == 8.4.1`（镜像 tag 硬钉——插件
      descriptor `elasticsearch.version=8.4.1` 与基底镜像互锁，升版须 client+插件+
      重构建三联动，ARCH R23）；
    - `_cat/plugins?format=json` → jieba 插件 1.0.0 在位（自构建产物入库 + 薄镜像
      叠加装载的部署面证据）；
    - `_analyze` 黄金断言：analyzer=`jieba_search` + 「中华人民共和国国歌」→ tokens
      必须同时含『中华人民共和国』『国歌』（jieba 分词回归哨兵——装错字典/装空壳插件
      的典型表现是逐字切分，本断言即挡）；
    - `auth_enforced_401`（2026-11-26 basic auth 批增）：带凭据 GET / 已 200（鉴权
      开启形态确认）时，无凭据 GET / 必须 401——防 compose security 改回 false 后
      鉴权静默失效无感知；无凭据裸跑（回退形态）不执行不误杀（记 pending）；
    - 容器状态不在此脚本判定：compose healthcheck 已用 `_cluster/health` 承
      担，本脚本只验 HTTP 面（避免 smoke 循环额外依赖 docker CLI）。
纪律：仅标准库；URL 仅 127.0.0.1（协议+host 白名单）；读 .env（ES_USERNAME/
    ES_PASSWORD 非占位时带 basic auth）但绝不打印任何值；stdout 最后一行 = 单行
    紧凑 JSON 裁决；exit 0 = 全过。
"""

from __future__ import annotations

import base64
import json
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# 宿主端口 19200（9200+10000 惯例；ARCH §5 / preflight PORTS 同口径钉版）
ES_BASE = "http://127.0.0.1:19200"
ES_PORT = 19200
EXPECTED_VERSION = "8.4.1"
EXPECTED_PLUGIN = ("jieba", "1.0.0")
ANALYZER = "jieba_search"
GOLDEN_TEXT = "中华人民共和国国歌"
GOLDEN_TOKENS = ("中华人民共和国", "国歌")


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
        "component": "elasticsearch",
        "pass": all(c["pass"] for c in checks),
        "summary": f"{passed}/{len(checks)} checks 过",
        "checks": checks,
        "pending": pending,
    }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["pass"] else 1


def _json_body(body: bytes) -> object | None:
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return None


def _auth_headers(env: dict[str, str]) -> dict[str, str]:
    """ES_USERNAME/ES_PASSWORD 非占位时带 basic auth（值经 header 注入，不打印）。"""
    user = _clean(env.get("ES_USERNAME", ""))
    password = _clean(env.get("ES_PASSWORD", ""))
    if _is_placeholder(user):
        return {}
    raw = f"{user}:{password}".encode("utf-8")
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


# ── 组件检查 ────────────────────────────────────────────────────────────────

def main() -> int:
    env = _load_env()
    auth = _auth_headers(env)
    checks: list[dict] = []
    pending: list[str] = [
        "ES 实占内存复核（docker stats：heap 512m + mem_limit 1g，ARCH §5 / R22）",
        "分词/词库质量正式结论归 M4 评测线（探针 §6⑤ 同口径）",
    ]

    try:
        with socket.create_connection(("127.0.0.1", ES_PORT), timeout=2):
            ok = True
    except OSError:
        ok = False
    checks.append({"name": f"port_{ES_PORT}_open", "pass": ok,
                   "detail": f"127.0.0.1:{ES_PORT} " + ("可达" if ok else "不可达")})

    # ① 版本（镜像 tag 硬钉 + 插件 descriptor 互锁）
    status, _, body = _http(ES_BASE + "/", headers=auth)
    root_status = status  # ④ 鉴权强制断言的前提值（带凭据 GET / 形态）
    payload = _json_body(body) if status == 200 else None
    version = ""
    if isinstance(payload, dict):
        ver = payload.get("version")
        if isinstance(ver, dict):
            version = str(ver.get("number") or "")
    checks.append({
        "name": "root_version",
        "pass": status == 200 and version == EXPECTED_VERSION,
        "detail": (f"GET / → HTTP {status}, version.number={version or '(缺失)'}"
                   f"（预期 {EXPECTED_VERSION}）") if status else f"{ES_PORT} 无 HTTP 响应",
    })

    # ② 插件在位（自构建 jieba 产物 + 薄镜像叠加的部署面证据）
    status, _, body = _http(ES_BASE + "/_cat/plugins?format=json", headers=auth)
    plugins = _json_body(body) if status == 200 else None
    name, pver = EXPECTED_PLUGIN
    hit = None
    if isinstance(plugins, list):
        for item in plugins:
            if isinstance(item, dict) and str(item.get("component") or "") == name:
                hit = str(item.get("version") or "")
                break
    checks.append({
        "name": "plugin_jieba_present",
        "pass": status == 200 and hit is not None and hit.startswith(pver),
        "detail": (f"_cat/plugins → jieba {hit}（预期 {pver}）" if hit is not None
                   else f"_cat/plugins → HTTP {status}，无 jieba 插件条目"),
    })

    # ③ 分词黄金断言（jieba 回归哨兵：装空壳/字典错时会逐字切分）
    analyze_url = ES_BASE + "/_analyze"
    data = json.dumps({"analyzer": ANALYZER, "text": GOLDEN_TEXT},
                      ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", **auth}
    status, _, body = _http(analyze_url, method="POST", data=data, headers=headers)
    analyzed = _json_body(body) if status == 200 else None
    tokens: list[str] = []
    if isinstance(analyzed, dict):
        raw_tokens = analyzed.get("tokens")
        if isinstance(raw_tokens, list):
            for t in raw_tokens:
                if isinstance(t, dict) and t.get("token") is not None:
                    tokens.append(str(t["token"]))
    missing = [t for t in GOLDEN_TOKENS if t not in tokens]
    checks.append({
        "name": "analyze_jieba_search_golden",
        "pass": status == 200 and not missing,
        "detail": (f"_analyze[{ANALYZER}]「{GOLDEN_TEXT}」→ {tokens}"
                   if status == 200
                   else f"POST /_analyze → HTTP {status}（{ES_PORT} 无 HTTP 响应或无该 analyzer）")
                  + (f"；缺 token {missing}" if missing else ""),
    })

    # ④ 鉴权强制断言（2026-11-26 basic auth 批增，security-auditor P2）：仅在
    # 「带凭据 GET / 已 200」（鉴权开启形态确认）后执行——此时无凭据必须 401；
    # 无凭据裸跑（.env 占位/回退形态）不执行不误杀，记 pending
    if root_status == 200 and auth:
        anon_status, _, _ = _http(ES_BASE + "/")  # 零 Authorization 头
        checks.append({
            "name": "auth_enforced_401",
            "pass": anon_status == 401,
            "detail": (f"无凭据 GET / → HTTP {anon_status}（预期 401，鉴权强制在位）"
                       if anon_status == 401 else
                       f"无凭据 GET / → HTTP {anon_status}（预期 401——security 疑似关闭，"
                       "鉴权静默失效）"),
        })
    elif not auth:
        pending.append("auth_enforced_401 未执行：.env 无 ES 凭据（无鉴权裸跑/回退形态）")
    else:
        pending.append("auth_enforced_401 未执行：带凭据 GET / 未达 200（root_version 已记失败）")

    return _emit(checks, pending)


if __name__ == "__main__":
    sys.exit(main())
