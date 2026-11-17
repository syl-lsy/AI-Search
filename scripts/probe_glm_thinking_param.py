"""B13 增量二探针：GLM 腿 thinking 参数剥离（网关侧）红绿对照。

验证面（docs/ARCHITECTURE.md v1.57 §7「GLM 腿 thinking 参数剥离」钉注）：
- 案例 A（被测面）：向 `chat-standard-glm` 别名发带 `enable_thinking: false` 的最小
  chat 请求——**判据 = HTTP 200 且响应 model 落在 GLM 腿**。
  红态实质是「GLM 腿 400 → 网关 fallback 到 chat-standard-deepseek → 客户端仍见
  200」，故仅看状态码会被降级链掩盖（2026-11-17 首次红跑实证：status=200 但
  response.model=deepseek-v4-pro）；本探针以「实际承接模型」为判据。
- 案例 B（对照面）：向 `chat-standard`（百炼 qwen 主腿）发同形态请求——预期 200 且
  响应 model 不落在任何备位腿（主腿不受钩子影响，`enable_thinking=false` 仍原样
  送达 qwen）。

凭证纪律：`LITELLM_MASTER_KEY` / `LITELLM_BASE_URL` 从仓库根 `.env` 读入进程内，
**从不打印值**（只打印 host 与状态码）；错误体截断打印，便于看上游错误签名。

请求形态说明：应用侧（apps/search-api/app/agent/llm.py `build_chat_model`）以
`extra_body={"enable_thinking": False}` 调用 langchain-openai → openai SDK 把
extra_body 合并进请求体顶层，故对网关的线上形态就是 `{"enable_thinking": false}`
顶层键；本探针按该形态构造（不同时带顶层键与 extra_body，避免双重注入掩盖判定）。

生效机制（实测后钉死，2026-11-17）：`litellm_settings.callbacks` 注册
`deploy/litellm/litellm_callbacks.py::glm_thinking_guard`（CustomLogger 实例），
实现 `async_pre_call_deployment_hook`（部署级，覆盖 fallback 换腿——此时
kwargs["model"] 已是 `openai/GLM-5.3(-flash)`，按基名 glm 前缀识别）与
`async_pre_call_hook`（proxy 边界，按 `-glm` 别名识别）。判定为 GLM 腿后剥离
`enable_thinking` / `think`（顶层与 extra_body 两处）。注册改动后需
`docker compose --project-directory . -f deploy/docker-compose.yml restart litellm`。

实测留档（2026-11-17）：
- 红（注册前）: case A HTTP 200 但 response.model=deepseek-v4-pro —— GLM 腿 400 被
  fallback 掩盖；case B 200/chat-standard；
- 绿（restart 注册后）: case A 200/chat-standard-glm、chat-speed-glm 同批 200 且
  由 GLM 腿承接；case B 200/chat-standard（主腿不受影响）。

运行（仓库根）：
    python3 scripts/probe_glm_thinking_param.py
退出码：两案例均符合预期 = 0；否则 1（红态 1 即修复前证据，无需视为脚本故障）。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"

#: 被测 GLM 腿别名（案例 A）
GLM_ALIAS = "chat-standard-glm"
#: 对照主腿别名（案例 B，百炼 qwen）
QWEN_ALIAS = "chat-standard"

_PROBE_MESSAGES = [{"role": "user", "content": "用一句话回答：1+1 等于几？"}]


def _read_env(path: Path) -> dict[str, str]:
    """解析 .env 的 KEY=VALUE（容错：跳过空行/注释/无等号行；不打印任何值）。"""
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


def _post_chat(base_url: str, api_key: str, model: str) -> tuple[int, dict | str]:
    """发一条最小 chat 请求（带顶层 enable_thinking=false），返回 (status, body)。"""
    payload = {
        "model": model,
        "messages": _PROBE_MESSAGES,
        "max_tokens": 64,
        "stream": False,
        # 生产形态：synthesis build_chat_model 的 extra_body 在网关请求体上的落点
        "enable_thinking": False,
        # Ollama 原生形态的另一键（同批剥离对象，验证不触发上游 400）
        "think": False,
    }
    request = urllib.request.Request(
        url=f"{base_url.rstrip('/')}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",  # 值不打印、不落盘
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw[:500]


def _error_signature(body: dict | str) -> str:
    """错误体 → 一行可读签名（截断 300 字符，便于对照上游话术）。"""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or error)
            return message[:300]
        return json.dumps(body, ensure_ascii=False)[:300]
    return str(body)[:300]


def _model_field(body: dict | str) -> str:
    """响应体里的 model 字段（确证实际落到的上游/别名）。"""
    if isinstance(body, dict):
        return str(body.get("model") or "-")
    return "-"


def main() -> int:
    env = _read_env(ENV_FILE)
    api_key = env.get("LITELLM_MASTER_KEY", "")
    base_url = env.get("LITELLM_BASE_URL", "") or "http://127.0.0.1:4100"
    if not api_key:
        print("[probe] 缺 .env LITELLM_MASTER_KEY（脚本不打印任何凭据值）")
        return 2

    host = base_url.split("://", 1)[-1]
    print(f"[probe] gateway host={host}")

    status_a, body_a = _post_chat(base_url, api_key, GLM_ALIAS)
    model_a = _model_field(body_a)
    glm_served = "glm" in model_a.casefold()
    print(
        f"[case A] {GLM_ALIAS} + enable_thinking=false -> HTTP {status_a}, "
        f"response.model={model_a} -> {'GLM 腿承接' if glm_served else '非 GLM 腿（fallback 疑似触发）'}"
    )
    if status_a != 200:
        print(f"[case A] error={_error_signature(body_a)}")

    status_b, body_b = _post_chat(base_url, api_key, QWEN_ALIAS)
    model_b = _model_field(body_b)
    primary_served = status_b == 200 and "deepseek" not in model_b.casefold() and "glm" not in model_b.casefold()
    verdict_b = "OK(主腿承接)" if primary_served else "FAIL"
    print(
        f"[case B] {QWEN_ALIAS} + enable_thinking=false -> HTTP {status_b}, "
        f"response.model={model_b} {verdict_b}"
    )
    if status_b != 200:
        print(f"[case B] error={_error_signature(body_b)}")

    ok = status_a == 200 and glm_served and primary_served
    print(
        "[probe] RESULT:",
        "GREEN (GLM 腿已剥离 thinking 参数并实际承接；主腿不受影响)"
        if ok
        else "RED (GLM 腿未承接：修复前形态=上游 400 被 fallback 掩盖)",
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
