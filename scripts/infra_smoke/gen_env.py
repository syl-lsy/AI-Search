#!/usr/bin/env python3
"""S0 基建 smoke · gen_env —— .env 就位与容器侧生成型密钥回填（幂等）。

行为：
    1. .env 缺失或为空 → 从 .env.example 复制（不覆盖已有 .env）。
    2. 把【值为空或 <占位> 模式（^<.+>$，如 <GEN>/<hex32>/<secret>）】的容器侧密钥回填（幂等：
       非占位真实值一律保留——已生成过的密钥首启即定型，SALT/ENCRYPTION_KEY 等更换会致数据不可解）：
       GEN_KEYS = secrets 随机生成（含 NACOS_AUTH_TOKEN 的 base64(32B) 特殊形态——解码 ≥32 字节
       是 Nacos JWT 硬校验，短值致 nacos 启动崩溃）；
       CONST_KEYS = 固定常量回填（非随机——NACOS_AUTH_IDENTITY_KEY / LITELLM_UI_USERNAME 标识值）。
    2b. LITELLM_MASTER_KEY ↔ LITELLM_API_KEY 同值镜像（.env.example 契约：应用调网关凭证 = 网关
       Master Key，两键不独立随机）——一侧真实值、另一侧空/占位时镜像；双侧真实值不一致则
       pending 提醒、不擅自覆盖。
    3. LANGSMITH_TRACING 固定为 false（数据不出本机）；缺失则追加该行。
    4. 上游 7 家厂商密钥（DASHSCOPE_*/ZHIPU_*/DEEPSEEK_/TAVILY_/BOCHA_/FIRECRAWL_/ANYSEARCH_API_KEY）
       与 LANGSMITH_API_KEY 保持留空——脚本不代填，由用户从厂商控制台申请后人工填写。

纪律：从仓库根运行；仅标准库；绝不打印任何变量【值】——stdout 只打印被填充的变量【名】清单；
    stdout 最后一行 = 单行紧凑 JSON 裁决。
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / ".env"
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"

# 容器侧生成型密钥（<GEN> 口径）：name → 生成形态。
# 幂等语义：值为空或匹配 ^<.+>$ 占位模式（<GEN>/<hex32>/<secret> 等均视为未就位）才回填；
#   非占位真实值一律保留——已生成过的密钥首启即定型（SALT/ENCRYPTION_KEY 等更换会致数据不可解）；
#   不在 GEN_KEYS 的键（上游厂商 Key 等 NEVER_FILL）永不代填。
GEN_KEYS: dict[str, str] = {
    "MINIO_ROOT_USER": "user",          # minio-root-<hex12>
    "MINIO_ROOT_PASSWORD": "secret",    # hex32
    "MINIO_ACCESS_KEY": "access",       # ai-search-svc-<hex12>（Service Account 对，§3.2 ④ 按此值建账号）
    "MINIO_SECRET_KEY": "secret",       # hex32
    "LANGFUSE_SALT": "long",            # hex64
    "LANGFUSE_ENCRYPTION_KEY": "key32",  # hex64 = 32 字节（Langfuse v4 要求）
    "LANGFUSE_NEXTAUTH_SECRET": "long",  # hex64
    "CLICKHOUSE_PASSWORD": "secret",    # hex32
    "LANGFUSE_DB_PASSWORD": "secret",   # hex32
    "OPEN_SANDBOX_API_KEY": "token",    # hex48
    "CRAWL4AI_API_TOKEN": "token",      # hex48（与 crawl4ai 容器同变量注入，Bearer 鉴权）
    "NACOS_AUTH_TOKEN": "b64_32",       # 32 字节 → base64（解码后 ≥32 字节，Nacos JWT 签名密钥硬校验）
    "NACOS_AUTH_IDENTITY_VALUE": "urlsafe",  # 鉴权身份对值（与 CONST_KEYS 的 IDENTITY_KEY 成对）
    "NACOS_PASSWORD": "urlsafe",        # 只读账号密码（§3.2 ① 建账号须用同值，两侧一致）
    "DB_PASSWORD": "urlsafe",           # postgres 业务库 ai_search 密码（init.sh 建角色）
    "LITELLM_DB_PASSWORD": "urlsafe",   # postgres 第三库 litellm 密码（init.sh 建角色）
    "LITELLM_MASTER_KEY": "urlsafe",    # 网关 Master Key（与 LITELLM_API_KEY 同值，见 2b 镜像）
    "LITELLM_SALT_KEY": "urlsafe",      # 虚拟 key 加密盐——首启定型勿再改
    "LITELLM_UI_PASSWORD": "urlsafe",   # 控制台登录密码（与 CONST_KEYS 的 UI_USERNAME 成对）
}

# 常量回填项（非随机生成——固定标识值，语义上不保密；值 ∈ {"", "<占位>"} 时回填等号右侧常量。
# .env.example 同步直书同值作模板；gen_env 兜底旧 .env 里的 <自定义键>/<非默认值> 占位）
CONST_KEYS: dict[str, str] = {
    "NACOS_AUTH_IDENTITY_KEY": "nacos-identity",
    "LITELLM_UI_USERNAME": "ai-search-admin",
}

# 绝不代填（保持留空/占位，人工填写）：上游 7 家 + LangSmith
NEVER_FILL = (
    "DASHSCOPE_TOKEN_PLAN_API_KEY", "DASHSCOPE_API_KEY",
    "ZHIPU_CODING_PLAN_API_KEY", "ZHIPU_API_KEY",
    "DEEPSEEK_API_KEY", "TAVILY_API_KEY", "BOCHA_API_KEY",
    "FIRECRAWL_API_KEY", "ANYSEARCH_API_KEY", "LANGSMITH_API_KEY",
)

_LINE_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)(?P<sep>=)(?P<value>.*)$")


def _gen(kind: str) -> str:
    if kind == "user":
        return "minio-root-" + secrets.token_hex(6)
    if kind == "access":
        return "ai-search-svc-" + secrets.token_hex(6)
    if kind == "secret":
        return secrets.token_hex(16)
    if kind == "long":
        return secrets.token_hex(32)
    if kind == "key32":
        return secrets.token_hex(32)  # 32 字节 hex（Langfuse ENCRYPTION_KEY）
    if kind == "token":
        return secrets.token_hex(24)
    if kind == "b64_32":
        # 32 字节随机数的 base64（解码后恰 32 字节 ≥ Nacos JWT 硬校验下限——短值致 nacos 启动崩溃）
        return base64.b64encode(secrets.token_bytes(32)).decode()
    if kind == "urlsafe":
        return secrets.token_urlsafe(30)  # 30 字节熵，URL 安全字母表（.env 友好）
    raise ValueError(f"unknown kind: {kind}")


def _clean(value: str) -> str:
    """剥掉内联注释（.env.example 口径：注释以空白+# 起）与包裹引号，取纯值。"""
    v = value.strip()
    idx = v.find(" #")
    if idx == -1:
        idx = v.find("\t#")
    if idx != -1:
        v = v[:idx]
    return v.strip().strip('"').strip("'")


_PLACEHOLDER_RE = re.compile(r"^<.+>$")


def _is_fillable(value: str) -> bool:
    """值为空或匹配 ^<.+>$ 占位模式（如 <GEN>/<hex32>）才回填（先剥内联注释与引号再判）。"""
    v = _clean(value)
    return (not v) or bool(_PLACEHOLDER_RE.match(v))


def main() -> int:
    checks: list[dict] = []
    pending: list[str] = []

    # 1) .env 就位
    if not ENV_PATH.exists() or ENV_PATH.stat().st_size == 0:
        if not ENV_EXAMPLE_PATH.exists():
            checks.append({"name": "env_example_exists", "pass": False,
                           "detail": ".env.example 不存在，无法生成 .env"})
            return _emit(checks, pending)
        shutil.copyfile(ENV_EXAMPLE_PATH, ENV_PATH)
        checks.append({"name": "env_created", "pass": True, "detail": ".env 缺失，已从 .env.example 复制"})
    else:
        checks.append({"name": "env_exists", "pass": True, "detail": ".env 已存在，跳过复制（幂等）"})

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    filled: list[str] = []
    tracing_fixed = False
    out_lines: list[str] = []
    # 预读 LITELLM_API_KEY 现值：MASTER 回填时若 API 侧已有真实值则镜像同值、不独立随机
    # （2b 同值契约；不依赖两键在文件中的先后顺序）
    api_pre = next((_clean(m.group("value")) for l in lines
                    if (m := _LINE_RE.match(l)) and m.group("key") == "LITELLM_API_KEY"), None)
    for line in lines:
        m = _LINE_RE.match(line)
        if not m:
            out_lines.append(line)
            continue
        key = m.group("key")
        value = m.group("value")
        if key in GEN_KEYS and _is_fillable(value):
            if key == "LITELLM_MASTER_KEY" and api_pre and not _is_fillable(api_pre):
                line = f"{key}={api_pre}"
            else:
                line = f"{key}={_gen(GEN_KEYS[key])}"
            filled.append(key)
            out_lines.append(line)
            continue
        if key in CONST_KEYS and _is_fillable(value):
            line = f"{key}={CONST_KEYS[key]}"
            filled.append(key)
            out_lines.append(line)
            continue
        if key == "LANGSMITH_TRACING":
            if _clean(value) != "false":
                line = "LANGSMITH_TRACING=false"
                tracing_fixed = True
            out_lines.append(line)
            continue
        out_lines.append(line)

    # LANGSMITH_TRACING 行缺失则追加
    if not any(l.strip().startswith("LANGSMITH_TRACING=") for l in out_lines):
        out_lines.append("LANGSMITH_TRACING=false")
        tracing_fixed = True

    # LITELLM_MASTER_KEY ↔ LITELLM_API_KEY 同值镜像（见 docstring 2b）：任一侧经本次回填或已有
    # 真实值、另一侧仍为空/占位 → 镜像过去；双侧均为真实值但不一致 → pending 提醒、不覆盖
    def _read_val(key: str) -> str | None:
        for l in out_lines:
            m = _LINE_RE.match(l)
            if m and m.group("key") == key:
                return m.group("value")
        return None

    master_v = _read_val("LITELLM_MASTER_KEY")
    api_v = _read_val("LITELLM_API_KEY")
    if master_v is not None and api_v is not None:
        master_c, api_c = _clean(master_v), _clean(api_v)
        master_real = bool(master_c) and not _is_fillable(master_c)
        api_real = bool(api_c) and not _is_fillable(api_c)
        if master_real and not api_real:
            out_lines = [f"LITELLM_API_KEY={master_c}"
                         if (mm := _LINE_RE.match(l)) and mm.group("key") == "LITELLM_API_KEY" else l
                         for l in out_lines]
            filled.append("LITELLM_API_KEY")
        elif api_real and not master_real:
            out_lines = [f"LITELLM_MASTER_KEY={api_c}"
                         if (mm := _LINE_RE.match(l)) and mm.group("key") == "LITELLM_MASTER_KEY" else l
                         for l in out_lines]
            filled.append("LITELLM_MASTER_KEY")
        elif master_real and api_real and master_c != api_c:
            pending.append("LITELLM_MASTER_KEY 与 LITELLM_API_KEY 均为真实值但不一致——"
                           "同值契约需人工对齐（脚本不覆盖）")

    # 原子写回（保序、保留注释与空行；行尾统一 LF）。
    # 注意：Path(".env") 无后缀（首点按隐藏文件名处理），with_suffix(".env.tmp") 会拼出
    #   ".env.env.tmp"——显式用 parent / ".env.tmp"；.env 含明文密钥，写回收紧权限 0600。
    # tmp 必须在 write_text 之前以 0600 创建（默认 umask 落盘 0644，写入到 chmod 之间存在
    # 明文密钥瞬态可读窗口）；os.open 的 mode 经 umask 只会更严不会更松，末尾 chmod 兜底
    # 上次运行残留的旧 tmp（O_CREAT 不改已有文件 mode）
    tmp = ENV_PATH.parent / ".env.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    tmp.chmod(0o600)
    tmp.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    tmp.replace(ENV_PATH)
    os.chmod(ENV_PATH, 0o600)

    # stdout：只打印被填充的变量名清单（绝不打印值）
    if filled:
        for name in filled:
            print(f"filled: {name}")
    checks.append({
        "name": "gen_keys_backfilled",
        "pass": True,
        "detail": f"回填 {len(filled)} 项（GEN_KEYS 随机 + CONST_KEYS 常量 + MASTER/API 同值镜像；"
                  f"幂等；首启定型勿再改）",
    })
    checks.append({
        "name": "langsmith_tracing_fixed_false",
        "pass": True,
        "detail": "LANGSMITH_TRACING=false" + ("（已修正）" if tracing_fixed else "（本已为 false）"),
    })
    if filled:
        pending.append("上游 7 家厂商密钥与 LANGSMITH_API_KEY 保持留空——人工申请填写（脚本不代填）")
    return _emit(checks, pending)


def _emit(checks: list[dict], pending: list[str]) -> int:
    passed = sum(1 for c in checks if c["pass"])
    result = {
        "component": "gen_env",
        "pass": all(c["pass"] for c in checks),
        "summary": f"{passed}/{len(checks)} checks 过",
        "checks": checks,
        "pending": pending,
    }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
