"""B14 增量 C 前置探针：thinking 分档的逐腿禁用键形态（GLM low 档 / DeepSeek 禁思考）。

验证面（docs/ARCHITECTURE.md v1.59 §6.7 thinking 按模式分档条）：
- **GLM low 档键**：GLM 无法完全关思考，只存在档位；候选 `reasoning_effort=low` 或
  `thinking` 对象。判定标准 = HTTP 200 且实际由 GLM 腿承接（response.model 含 glm——
  否则上游 400 被 LiteLLM fallback 掩盖）且 reasoning 增量显著低于默认档。
- **DeepSeek 禁思考键**：判定标准 = 200 且流式零 reasoning 增量；键被忽略仍出思考流
  → 如实钉注（该腿超时面由增量 A 单调用包络 + 兜底产出兜住）。
- **钩子互锁**：B13 网关 pre-call 钩子只剥 `enable_thinking` / `think` 两键名——low 档
  键若为其他键名必须原样透传（high/max 档能触发思考即证钩子未剥）。

凭证纪律：`LITELLM_MASTER_KEY` / `LITELLM_BASE_URL` 从仓库根 `.env` 读入进程内，
**从不打印值**（只打印 host / 状态码 / reasoning 字符数）。

实测留档（2026-11-18，本机 litellm 网关注册 B13 钩子后）：
- **GLM**：默认档 reasoning 增量 0（standard 腿）/ 2020 字符（flash 腿）；`reasoning_effort`
  取 minimal/low/medium 三档均 0、high/max 分别 1138/1644 字符（standard 腿）——
  **键活、low 为最低档**；`thinking: {"type": "low"}` 亦 200 但无档位回显。钉死形态 =
  **`reasoning_effort: "low"`**（HTTP 200 + GLM 腿承接 + 最低档/零 reasoning）。
- **DeepSeek**：顶层 `thinking: {...}` 被 LiteLLM 丢弃（reasoning 仍 131~174 字符）；
  `enable_thinking:false` / `reasoning_effort:low` / `thinking_budget:0` 同样被忽略；
  **嵌套 `extra_body: {"thinking": {"type": "disabled"}}` 使 reasoning 归零（0 字符）**
  ——LiteLLM 的 provider 级 extra_body 才透传。`thinking: {"type":"off"}` 触发上游 500
  （非法取值，禁用）。钉死形态 = **`extra_body: {"thinking": {"type": "disabled"}}`**。
- **组合形态经 langchain 应用路径（ReasoningChatModel）复验**：GLM 基线 332 字符 →
  off 形态 0；DeepSeek 基线 131 字符 → off 形态 0。六别名（Standard/Speed 两档 ×
  qwen/glm/deepseek）全部 200 且各自腿承接、reasoning 全零（无 fallback、无 400）。

**两段验证面（本脚本固化的两部分）**：
1. **wire 形态**（stdlib，真实网络）：直发 `/v1/chat/completions`，逐腿判定键接受度 /
   承接腿 / reasoning 增量；
2. **langchain 应用路径**（需项目环境）：走生产工厂 `build_chat_model(alias,
   thinking="off")` + `astream`，断言六别名 reasoning 全零，并跑三 Standard 别名 on 档
   作为**对照基线**（至少一条 >0 才说明「off 归零」有判别力——若上游全线零思考，
   off 结果无意义、如实 NOTE 不判红）。

运行（仓库根，任选其一）：
    python3 scripts/probe_thinking_mode_params.py          # 自动经 uv 进入项目环境跑全量
    uv run --package search-api python scripts/probe_thinking_mode_params.py
（首条在缺 uv / 项目环境不可用时自动退化为 wire-only，并打印 langchain 段 SKIPPED 与
  上方命令——不会误报 GREEN。）
退出码：wire 形态达判定标准且 langchain 段（若执行）通过 = 0；否则 1。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"

#: 被测别名（两档三节链的全部 chat 腿）
CHAT_ALIASES: tuple[str, ...] = (
    "chat-standard",
    "chat-standard-glm",
    "chat-standard-deepseek",
    "chat-speed",
    "chat-speed-glm",
    "chat-speed-deepseek",
)

_PROBE_PROMPT = "简要说明：为什么天空是蓝色的？"

#: 重入标记：langchain 段需项目环境（uv workspace），缺环境时以该标记避免无限重入
_REEXEC_ENV = "PROBE_THINKING_LANGCHAIN_REEXEC"


def _read_env(path: Path) -> dict[str, str]:
    """解析 .env 的 KEY=VALUE（容错跳过空行/注释；不打印任何值）。"""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _stream_probe(
    base_url: str, api_key: str, model: str, extra: dict[str, object]
) -> tuple[str, str, int]:
    """发一条最小流式 chat 请求，返回 (状态, 承接 model, reasoning 字符数)。

    reasoning 字符数 = 流式 delta 中 `reasoning_content` 累计长度（经 ReasoningChatModel
    的思考块同源字段）；非 200 时状态含错误码、model 为错误签名截断。
    """
    payload: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": _PROBE_PROMPT}],
        "max_tokens": 600,
        "stream": True,
    }
    payload.update(extra)
    request = urllib.request.Request(
        url=f"{base_url.rstrip('/')}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",  # 值不打印、不落盘
        },
        method="POST",
    )
    seen_model = "-"
    reasoning_chars = 0
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            for raw in response:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[len("data:") :].strip()
                if chunk == "[DONE]":
                    break
                try:
                    obj = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                seen_model = str(obj.get("model") or seen_model)
                choices = obj.get("choices") or [{}]
                delta = choices[0].get("delta") or {}
                reasoning_chars += len(str(delta.get("reasoning_content") or ""))
            return "200", seen_model, reasoning_chars
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return f"HTTP {exc.code}", body[:200], -1


def _langchain_available() -> bool:
    """项目环境可用性探测（app 包可导入 = uv workspace 依赖就位）。"""
    try:
        import app.agent.llm  # noqa: F401 —— 仅探测导入面
    except Exception:  # noqa: BLE001 —— 缺 venv/依赖即视为不可用
        return False
    return True


def _reexec_via_uv() -> int | None:
    """在项目环境内重跑本脚本（langchain 段需 uv workspace 依赖）。不可用返回 None。"""
    if os.environ.get(_REEXEC_ENV):
        return None  # 已重入仍不可用：不再递归
    uv = shutil.which("uv")
    if not uv:
        return None
    print(
        "[langchain] 当前解释器无项目依赖 → 经 uv 重入项目环境执行全量验证", flush=True
    )
    return subprocess.call(
        [uv, "run", "--package", "search-api", "python", str(Path(__file__).resolve())],
        cwd=REPO_ROOT,
        env={**os.environ, _REEXEC_ENV: "1"},
    )


def _chunk_reasoning_text(chunk: object) -> tuple[int, int]:
    """单 chunk 的 (reasoning 字符数, 正文文字符数)——v1 blocks 与 reasoning_content 双形态。"""
    reasoning = text = 0
    kwargs = getattr(chunk, "additional_kwargs", None) or {}
    if kwargs.get("reasoning_content"):
        reasoning += len(str(kwargs["reasoning_content"]))
    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        text += len(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype in ("reasoning", "thinking"):
                payload = block.get("reasoning") or block.get("thinking") or block.get("text")
                reasoning += len(str(payload or ""))
            elif btype == "text":
                text += len(str(block.get("text") or ""))
    return reasoning, text


async def _langchain_probe() -> tuple[bool, list[str]]:
    """langchain 应用路径复验（生产工厂 + astream，六别名 off 档断言 reasoning 归零）。

    返回 (是否通过, 附注列表)。对照纪律：三 Standard 别名先跑 on 档——至少一条 >0 才
    说明「off 归零」具判别力；上游全线零思考时如实 NOTE 不判红（wire 段已独立证明
    键活）。单别名异常（鉴权/网络）记 FAIL 不抛——脚本收敛为可读结论。
    """
    from app.agent.llm import build_chat_model

    async def _run(alias: str, thinking: str) -> tuple[int, int, str | None]:
        try:
            model = build_chat_model(alias, thinking=thinking)
            reasoning = text = 0
            async for chunk in model.astream(_PROBE_PROMPT):
                chunk_reasoning, chunk_text = _chunk_reasoning_text(chunk)
                reasoning += chunk_reasoning
                text += chunk_text
            return reasoning, text, None
        except Exception as exc:  # noqa: BLE001 —— 网络/鉴权异常收敛为断言失败
            return -1, -1, f"{type(exc).__name__}: {str(exc)[:160]}"

    notes: list[str] = []
    ok = True

    on_reasoning: dict[str, int] = {}
    for alias in CHAT_ALIASES[:3]:  # Standard 三腿对照基线（on 档）
        reasoning, text, error = await _run(alias, "on")
        on_reasoning[alias] = reasoning
        print(f"[langchain] on  {alias} -> reasoning={reasoning} text={text}"
              + (f" error={error}" if error else ""))
        if error:
            ok = False
    if ok and not any(value > 0 for value in on_reasoning.values()):
        notes.append(
            "NOTE：on 档三 Standard 别名 reasoning 全为 0（上游当前无思考流）——"
            "off 归零无判别力，如实留档不判红（wire 段已独立证明键活）"
        )

    for alias in CHAT_ALIASES:  # off 档六别名：reasoning 必须归零且正文非空
        reasoning, text, error = await _run(alias, "off")
        good = error is None and reasoning == 0 and text > 0
        ok = ok and good
        print(
            f"[langchain] off {alias} -> reasoning={reasoning} text={text} "
            f"{'OK' if good else 'FAIL'}" + (f" error={error}" if error else "")
        )
    return ok, notes


def main() -> int:
    os.chdir(REPO_ROOT)  # env_file=".env" 相对运行时 cwd 解析（与 Makefile 口径一致）
    langchain_available = _langchain_available()
    if not langchain_available:
        reexec_code = _reexec_via_uv()
        if reexec_code is not None:
            return reexec_code
        print(
            "[langchain] SKIPPED：项目环境不可用（uv/venv 缺失）——wire-only 模式；"
            "安装 uv 后用 `uv run --package search-api python scripts/"
            "probe_thinking_mode_params.py` 跑全量"
        )
    env = _read_env(ENV_FILE)
    api_key = env.get("LITELLM_MASTER_KEY", "")
    base_url = env.get("LITELLM_BASE_URL", "") or "http://127.0.0.1:4100"
    if not api_key:
        print("[probe] 缺 .env LITELLM_MASTER_KEY（脚本不打印任何凭据值）")
        return 2
    print(f"[probe] gateway host={base_url.split('://', 1)[-1]}")

    # 形态 A：GLM low 档键（上层已由 B13 钩子剥离 enable_thinking，本键不受影响）
    glm_alias = "chat-standard-glm"
    glm_base = _stream_probe(base_url, api_key, glm_alias, {})
    glm_low = _stream_probe(base_url, api_key, glm_alias, {"reasoning_effort": "low"})
    glm_high = _stream_probe(base_url, api_key, glm_alias, {"reasoning_effort": "high"})
    glm_ok = (
        glm_low[0] == "200"
        and "glm" in glm_low[1].casefold()
        and glm_low[2] <= glm_high[2]
        and (glm_high[2] > glm_low[2] or glm_low[2] == 0)
    )
    print(
        f"[GLM] 默认={glm_base} / low={glm_low} / high={glm_high} -> "
        f"{'OK(reasoning_effort=low 生效)' if glm_ok else 'FAIL'}"
    )

    # 形态 B：DeepSeek 禁思考键（必须经 litellm extra_body 嵌套透传；顶层 thinking 被丢弃）
    ds_alias = "chat-standard-deepseek"
    ds_base = _stream_probe(base_url, api_key, ds_alias, {})
    ds_off = _stream_probe(
        base_url, api_key, ds_alias, {"extra_body": {"thinking": {"type": "disabled"}}}
    )
    ds_ok = ds_off[0] == "200" and "deepseek" in ds_off[1].casefold() and ds_off[2] == 0
    print(
        f"[DeepSeek] 默认={ds_base} / thinking-disabled={ds_off} -> "
        f"{'OK(禁用键生效)' if ds_ok else 'FAIL(键被忽略仍思考)'}"
    )

    # 组合形态跨六别名（qwen/glm/deepseek × 两档）：200 + 各自腿承接 + reasoning 归零
    off_form: dict[str, object] = {
        "enable_thinking": False,
        "reasoning_effort": "low",
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    combo_ok = True
    for alias in CHAT_ALIASES:
        status, served, reasoning = _stream_probe(base_url, api_key, alias, off_form)
        leg_ok = status == "200" and alias.split("-")[-1] in served.casefold()
        combo_ok = combo_ok and leg_ok and reasoning == 0
        print(f"[组合] {alias} -> {status} model={served} reasoning={reasoning}")

    langchain_ok = True
    if langchain_available:
        langchain_ok, notes = asyncio.run(_langchain_probe())
        for note in notes:
            print(f"[langchain] {note}")

    ok = glm_ok and ds_ok and combo_ok and langchain_ok
    print(
        "[probe] RESULT:",
        "GREEN (GLM low 键生效；DeepSeek 禁用键生效；六别名组合形态零 reasoning 且无 "
        "fallback；langchain 应用路径 off 档六别名 reasoning 归零)"
        if ok and langchain_available
        else (
            "GREEN-WIRE (wire 形态全达标准；langchain 段 SKIPPED——见上方提示)"
            if ok
            else "RED (存在形态未达判定标准——见上方逐条输出)"
        ),
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
