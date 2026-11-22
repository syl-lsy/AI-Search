"""unit 层：deploy/litellm/litellm_callbacks.py 纯判定逻辑（B13 增量二 GLM 腿 thinking 剥离
+ post-M2 resilience 工单③ 网关失败告警）。

为何要桩：litellm 不在 app venv（`uv run --package search-api` 环境装的是应用依赖，
网关镜像里的 litellm 不参与后端门禁）；本模块顶层只依赖 `litellm._logging`、
`litellm.integrations.custom_logger` 与 `litellm.litellm_core_utils.secret_redaction`
三个 import——测试先往 sys.modules 注入同名桩模块，再用 importlib 从 `deploy/litellm/`
加载被测文件，纯函数与钩子全程零网络零容器。secret_redaction.redact_string 的桩
按容器 1.106.4 实测行为钉最小替身（Bearer 空格分隔 token / 裸 JWT eyJ / 裸 sk- →
REDACTED；完整 URL 不消除）——真实脱敏质量归 litellm 自身测试面，本文件只钉
「回调确实调用它 + 自有掩码不回退」。

钉住的判定矩阵（契约面见 ARCHITECTURE v1.57 §7 / 模块 docstring）：
- 别名判定：只有 `*-glm` 收尾（chat-standard-glm / chat-speed-glm）命中，casefold 归一；
- 部署级判定：上游真实 model id 取 provider 后基名，`glm` 前缀命中
  （openai/GLM-5.3、openai/GLM-5.3-flash）——fallback 换腿只在这一形态可识别；
- 非 GLM 腿（qwen / deepseek / 非字符串形态）一律零触碰；
- 剥离面：顶层 `enable_thinking` / `think` 与 `extra_body` 嵌套两处；
- 两钩子（proxy 边界 / 部署级）命中即剥离并返回请求体，未命中/无可剥离返回 None
  （litellm 保持原 kwargs）；钩子异常 fail-open 返回 None + 一条 WARNING（网关横切面
  故障不得打断所有请求）；
- 边界：extra_body 非 dict、metadata 非 dict、model 非字符串均不崩。

工单③ 失败告警矩阵（deploy/ 不在 pyright/ruff 范围——本文件是唯一自动化防线）：
- 失败分类五类 + 数字误命中边界（"after 1429ms" 含 429 不得判 rate_limit；
  "attempt 4013" 含 401 不得判 auth——数字按词边界匹配）；
- 脱敏三形态（URL / key=value / 纯文本直通）+ redact_string 漏网样例（JWT / sk-）；
- 状态码三通道（.code → .status_code → .response.status_code）+ 全缺失 → 空串；
- 告警路由：4xx → WARNING，5xx / 连接 / 超时 / 无状态码 → ERROR；
- kwargs 缺 exception / 缺 litellm_params 的降级字段形态；
- msg 内双引号替换（活体实证破坏 key=value 解析）+ 200 字符截断；
- async 钩子自身异常被吞（fail-open，只记 WARNING）。
"""

from __future__ import annotations

import importlib.util
import re
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
CALLBACKS_PATH = REPO_ROOT / "deploy" / "litellm" / "litellm_callbacks.py"


class _FakeCustomLogger:
    """litellm.integrations.custom_logger.CustomLogger 的替身（只需可继承）。"""

    async def async_pre_call_hook(
        self, user_api_key_dict: Any, cache: Any, data: dict, call_type: Any
    ) -> None:  # pragma: no cover - 被测类覆写，桩基类不带逻辑
        return None

    async def async_pre_call_deployment_hook(
        self, kwargs: dict, call_type: Any
    ) -> None:  # pragma: no cover - 同上
        return None


class _FakeOpenTelemetry:
    """litellm.integrations.opentelemetry.OpenTelemetry 的替身（四个 logging 入口）。

    被测模块 import 时会执行 patch_otel_decisions_response_compat()——桩提供四
    入口即可让垫片幂等套上（不再落 skip WARNING 污染 fake_logger.warnings 断言）；
    asyncio.iscoroutinefunction 依赖 sync/async 形态区分，两对入口分别如实声明。
    """

    def log_success_event(self, kwargs: Any, response_obj: Any, start_time: Any, end_time: Any) -> None:
        return None

    def log_failure_event(self, kwargs: Any, response_obj: Any, start_time: Any, end_time: Any) -> None:
        return None

    async def async_log_success_event(self, kwargs: Any, response_obj: Any, start_time: Any, end_time: Any) -> None:
        return None

    async def async_log_failure_event(self, kwargs: Any, response_obj: Any, start_time: Any, end_time: Any) -> None:
        return None


class _FakeVerboseLogger:
    """litellm._logging.verbose_proxy_logger 的替身：记录 info/warning/error 文案。"""

    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        self.infos.append(message % args if args else message)

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        self.warnings.append(message % args if args else message)

    def error(self, message: str, *args: Any, **kwargs: Any) -> None:
        self.errors.append(message % args if args else message)


def _fake_redact_string(value: str) -> str:
    """litellm secret_redaction.redact_string 的最小替身（钉住容器 1.106.4 实测行为）。

    实测（2026-11-22 容器内逐样例核过）：空格分隔 Bearer token / 裸 JWT / 裸 sk-
    → 整段替换为 REDACTED；完整 URL 不消除。仅覆盖本文档测试所需形态。
    """
    pattern = (
        r"Bearer\s+[A-Za-z0-9\-._~+/]{10,}=*"
        r"|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"
        r"|sk-[A-Za-z0-9\-_]{13,}"
    )
    return re.sub(pattern, "REDACTED", value)


@pytest.fixture
def callbacks(monkeypatch: pytest.MonkeyPatch) -> Any:
    """注入 litellm 桩模块后加载被测文件（返回模块 + 假日志便于断言）。"""
    fake_logger = _FakeVerboseLogger()

    litellm_pkg = types.ModuleType("litellm")
    logging_mod = types.ModuleType("litellm._logging")
    logging_mod.verbose_proxy_logger = fake_logger  # type: ignore[attr-defined]
    integrations_pkg = types.ModuleType("litellm.integrations")
    custom_logger_mod = types.ModuleType("litellm.integrations.custom_logger")
    custom_logger_mod.CustomLogger = _FakeCustomLogger  # type: ignore[attr-defined]
    litellm_pkg.integrations = integrations_pkg  # type: ignore[attr-defined]
    core_utils_pkg = types.ModuleType("litellm.litellm_core_utils")
    secret_redaction_mod = types.ModuleType("litellm.litellm_core_utils.secret_redaction")
    secret_redaction_mod.redact_string = _fake_redact_string  # type: ignore[attr-defined]
    core_utils_pkg.secret_redaction = secret_redaction_mod  # type: ignore[attr-defined]
    litellm_pkg.litellm_core_utils = core_utils_pkg  # type: ignore[attr-defined]
    otel_mod = types.ModuleType("litellm.integrations.opentelemetry")
    otel_mod.OpenTelemetry = _FakeOpenTelemetry  # type: ignore[attr-defined]
    integrations_pkg.opentelemetry = otel_mod  # type: ignore[attr-defined]

    for name, module in {
        "litellm": litellm_pkg,
        "litellm._logging": logging_mod,
        "litellm.integrations": integrations_pkg,
        "litellm.integrations.custom_logger": custom_logger_mod,
        "litellm.integrations.opentelemetry": otel_mod,
        "litellm.litellm_core_utils": core_utils_pkg,
        "litellm.litellm_core_utils.secret_redaction": secret_redaction_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    spec = importlib.util.spec_from_file_location("litellm_callbacks_under_test", CALLBACKS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.fake_logger = fake_logger  # 测试侧附加句柄（不改被测模块语义）
    return module


# ============================== 判定：别名 / 上游 id ==============================


class TestGlmLegDetection:
    @pytest.mark.parametrize(
        "model", ["chat-standard-glm", "chat-speed-glm", "CHAT-STANDARD-GLM", " chat-standard-glm "]
    )
    def test_alias_suffix_hits(self, callbacks: Any, model: str) -> None:
        """别名以 -glm 收尾即 GLM 腿（大小写/首尾空白归一）。"""
        assert callbacks._is_glm_alias(model) is True

    @pytest.mark.parametrize(
        "model",
        ["chat-standard", "chat-speed", "chat-standard-deepseek", "openai/GLM-5.3", "glm-4.5v"],
    )
    def test_alias_suffix_misses(self, callbacks: Any, model: str) -> None:
        """非 -glm 收尾（含上游 id / vision id）不算别名命中。"""
        assert callbacks._is_glm_alias(model) is False

    @pytest.mark.parametrize(
        "model",
        ["openai/GLM-5.3", "openai/GLM-5.3-flash", "openai/glm-5.3", "GLM-5.3"],
    )
    def test_upstream_basename_hits(self, callbacks: Any, model: str) -> None:
        """上游 model id 取 provider 后基名，glm 前缀命中（fallback 换腿唯一可识别形态）。"""
        assert callbacks._is_glm_upstream_id(model) is True

    @pytest.mark.parametrize(
        "model",
        ["openai/qwen3.7-plus", "openai/qwen3.6-flash", "openai/deepseek-v4-pro", "openai/deepseek-flash"],
    )
    def test_upstream_basename_misses(self, callbacks: Any, model: str) -> None:
        """qwen / deepseek 上游 id 不命中。"""
        assert callbacks._is_glm_upstream_id(model) is False

    @pytest.mark.parametrize("value", [None, 123, "", ["chat-standard-glm"]])
    def test_non_string_models_are_not_glm(self, callbacks: Any, value: Any) -> None:
        """非字符串形态（None/数字/列表）两条判定都不崩且不命中。"""
        assert callbacks._is_glm_alias(value) is False
        assert callbacks._is_glm_upstream_id(value) is False

    def test_resolve_reason_order_and_fallbacks(self, callbacks: Any) -> None:
        """命中理由矩阵：上游 id → 别名 → metadata.model_group；qwen/deepseek 为 None。"""
        assert callbacks._resolve_glm_reason({"model": "openai/GLM-5.3-flash"}) == "upstream"
        assert callbacks._resolve_glm_reason({"model": "chat-standard-glm"}) == "alias"
        assert (
            callbacks._resolve_glm_reason(
                {"model": "openai/GLM-5.3", "metadata": {"model_group": "chat-speed-glm"}}
            )
            == "upstream"
        )
        # fallback 主场景：kwargs["model"] 已是上游 id、model_group 仍是原请求别名
        assert (
            callbacks._resolve_glm_reason(
                {"model": "openai/GLM-5.3", "metadata": {"model_group": "chat-standard"}}
            )
            == "upstream"
        )
        assert callbacks._resolve_glm_reason({"model": "openai/qwen3.7-plus"}) is None
        assert callbacks._resolve_glm_reason({"model": "openai/deepseek-v4-pro"}) is None
        assert callbacks._resolve_glm_reason({}) is None

    def test_resolve_reason_ignores_broken_metadata(self, callbacks: Any) -> None:
        """metadata 非 dict 不崩（结构漂移防御）。"""
        assert callbacks._resolve_glm_reason({"model": "chat-standard", "metadata": "on"}) is None
        assert callbacks._resolve_glm_reason({"model": "chat-standard", "metadata": None}) is None


# ============================== 剥离面 ==============================


class TestStripThinkingKeys:
    def test_strips_top_level_keys(self, callbacks: Any) -> None:
        payload = {"model": "chat-standard-glm", "enable_thinking": False, "think": False}
        assert callbacks._strip_thinking_keys(payload) == ["enable_thinking", "think"]
        assert payload == {"model": "chat-standard-glm"}

    def test_strips_nested_extra_body(self, callbacks: Any) -> None:
        """extra_body 嵌套形态同剥（不同客户端落点差异）。"""
        payload = {"model": "chat-standard-glm", "extra_body": {"enable_thinking": False, "top_p": 1}}
        assert callbacks._strip_thinking_keys(payload) == ["extra_body.enable_thinking"]
        assert payload["extra_body"] == {"top_p": 1}

    def test_strips_both_locations(self, callbacks: Any) -> None:
        payload = {"enable_thinking": False, "extra_body": {"think": False}}
        assert callbacks._strip_thinking_keys(payload) == [
            "enable_thinking",
            "extra_body.think",
        ]
        assert payload == {"extra_body": {}}

    def test_no_hit_returns_empty(self, callbacks: Any) -> None:
        payload = {"model": "chat-standard-glm", "temperature": 0}
        assert callbacks._strip_thinking_keys(payload) == []
        assert payload == {"model": "chat-standard-glm", "temperature": 0}

    @pytest.mark.parametrize("extra_body", ["on", None, ["enable_thinking"], 3])
    def test_non_dict_extra_body_is_safe(self, callbacks: Any, extra_body: Any) -> None:
        """extra_body 非 dict（结构漂移）不崩、不误删。"""
        payload = {"model": "chat-standard-glm", "extra_body": extra_body}
        assert callbacks._strip_thinking_keys(payload) == []
        assert payload["extra_body"] == extra_body


# ============================== 钩子：部署级 ==============================


class TestDeploymentHook:
    async def test_glm_upstream_strips_and_returns_kwargs(self, callbacks: Any) -> None:
        """部署级：上游 GLM id + enable_thinking → 剥离并返回新 kwargs（litellm 采用）。"""
        guard = callbacks.glm_thinking_guard
        kwargs = {"model": "openai/GLM-5.3-flash", "enable_thinking": False, "think": False}
        out = await guard.async_pre_call_deployment_hook(kwargs, "acompletion")
        assert out == {"model": "openai/GLM-5.3-flash"}
        assert callbacks.fake_logger.infos  # 剥离有观测日志

    async def test_fallback_shape_strips_even_with_original_alias(
        self, callbacks: Any
    ) -> None:
        """fallback 换腿形态：model=上游 GLM id、model_group=原别名（chat-standard）→ 仍剥离。"""
        guard = callbacks.glm_thinking_guard
        kwargs = {
            "model": "openai/GLM-5.3",
            "metadata": {"model_group": "chat-standard"},
            "enable_thinking": False,
        }
        out = await guard.async_pre_call_deployment_hook(kwargs, "acompletion")
        assert out == {"model": "openai/GLM-5.3", "metadata": {"model_group": "chat-standard"}}

    async def test_non_glm_leg_untouched_returns_none(self, callbacks: Any) -> None:
        """qwen / deepseek 腿零触碰 → None（litellm 保持原 kwargs，主腿行为不变）。"""
        guard = callbacks.glm_thinking_guard
        for model in ("openai/qwen3.7-plus", "openai/deepseek-v4-pro", "chat-standard"):
            assert (
                await guard.async_pre_call_deployment_hook(
                    {"model": model, "enable_thinking": False}, "acompletion"
                )
                is None
            )
        assert callbacks.fake_logger.infos == []

    async def test_glm_without_thinking_keys_returns_none(self, callbacks: Any) -> None:
        """GLM 腿但请求本就不带关思考键 → None（不产生无意义 rebind）。"""
        guard = callbacks.glm_thinking_guard
        assert (
            await guard.async_pre_call_deployment_hook(
                {"model": "openai/GLM-5.3"}, "acompletion"
            )
            is None
        )

    async def test_exception_fails_open_with_warning(
        self, callbacks: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """钩子内部异常 → fail-open 返回 None + WARNING（不打断请求）。"""

        def _boom(payload: dict) -> list[str]:
            raise RuntimeError("strip 内核故障")

        monkeypatch.setattr(callbacks, "_strip_thinking_keys", _boom)
        guard = callbacks.glm_thinking_guard
        out = await guard.async_pre_call_deployment_hook(
            {"model": "openai/GLM-5.3", "enable_thinking": False}, "acompletion"
        )
        assert out is None
        assert any("deployment hook failed" in message for message in callbacks.fake_logger.warnings)


# ============================== 钩子：proxy 边界 ==============================


class TestProxyBoundaryHook:
    async def test_alias_strips_and_returns_data(self, callbacks: Any) -> None:
        """proxy 边界：请求别名 chat-standard-glm → 剥离并返回 data。"""
        guard = callbacks.glm_thinking_guard
        data = {"model": "chat-standard-glm", "enable_thinking": False, "messages": []}
        out = await guard.async_pre_call_hook(None, None, data, "acompletion")
        assert out == {"model": "chat-standard-glm", "messages": []}

    async def test_non_glm_alias_untouched_returns_none(self, callbacks: Any) -> None:
        """主腿别名（chat-standard / chat-speed）零触碰。"""
        guard = callbacks.glm_thinking_guard
        for model in ("chat-standard", "chat-speed", "chat-standard-deepseek"):
            assert (
                await guard.async_pre_call_hook(
                    None, None, {"model": model, "enable_thinking": False}, "acompletion"
                )
                is None
            )

    async def test_exception_fails_open_with_warning(
        self, callbacks: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """边界钩子异常 → fail-open 返回 None + WARNING。"""

        def _boom(payload: dict) -> list[str]:
            raise RuntimeError("strip 内核故障")

        monkeypatch.setattr(callbacks, "_strip_thinking_keys", _boom)
        guard = callbacks.glm_thinking_guard
        out = await guard.async_pre_call_hook(
            None, None, {"model": "chat-standard-glm", "enable_thinking": False}, "acompletion"
        )
        assert out is None
        assert any("proxy pre_call hook failed" in message for message in callbacks.fake_logger.warnings)


# ====================== 失败告警：假异常形态 ======================


class _ProxyShapeError(Exception):
    """ProxyException 系形态：状态码在 .code 上（litellm 1.106.4 实测口径）。"""


class _SdkShapeError(Exception):
    """LiteLLM SDK 异常形态：状态码在 .status_code 上。"""


class _HttpxShapeError(Exception):
    """httpx 形态：状态码只在 .response.status_code 上。"""

    def __init__(self, message: str, response_status: int) -> None:
        super().__init__(message)
        self.response = types.SimpleNamespace(status_code=response_status)


def _emit(callbacks: Any, kwargs: dict[str, Any], duration_s: float = 1.5) -> None:
    """_emit_alert 的便捷入口（时长默认 1.5s，方便断言 duration_s 字段）。"""
    start = datetime(2026, 11, 22, 0, 0, 0)
    callbacks.gateway_failure_alerter._emit_alert(
        kwargs, start_time=start, end_time=start + timedelta(seconds=duration_s)
    )


# ====================== 失败告警：分类 ======================


class TestClassifyFailure:
    @pytest.mark.parametrize(
        "exc_class,message,expected",
        [
            ("APITimeoutError", "Request timed out after 120s", "timeout"),
            ("", "operation timed out", "timeout"),
            ("APIConnectionError", "Connection error.", "connection"),
            ("", "Failed to connect to upstream host", "connection"),
            ("RateLimitError", "429 Too Many Requests", "rate_limit"),
            ("", "Rate limit reached for this deployment", "rate_limit"),
            ("AuthenticationError", "401 Unauthorized", "auth"),
            ("", "403 Forbidden invalid api key", "auth"),
            ("ProxyModelNotFoundError", "Invalid model name passed in", "other"),
            ("InternalServerError", "500 upstream crashed", "other"),
            ("", "", "other"),
        ],
    )
    def test_five_categories(self, callbacks: Any, exc_class: str, message: str, expected: str) -> None:
        """五类分类矩阵：timeout > connection > rate_limit > auth > other（类名参与匹配）。"""
        assert callbacks._classify_failure(exc_class, message) == expected

    @pytest.mark.parametrize(
        "message", ["upstream responded after 1429ms", "attempt 4013 of pool failed", "elapsed 5021ms"]
    )
    def test_digit_false_hits_stay_other(self, callbacks: Any, message: str) -> None:
        """数字误命中边界：1429 含 429 / 4013 含 401——数字必须按词边界匹配，不得误判。"""
        assert callbacks._classify_failure("", message) == "other"


# ====================== 失败告警：脱敏 ======================


class TestSanitizeExceptionText:
    def test_url_is_masked(self, callbacks: Any) -> None:
        out = callbacks._sanitize_exception_text("connect error for https://api.example.com/v1/chat")
        assert "https://" not in out
        assert "<url-redacted>" in out

    def test_key_value_pairs_are_masked(self, callbacks: Any) -> None:
        out = callbacks._sanitize_exception_text("bad api_key=supersecret123 and Bearer: abcdef123456")
        assert "supersecret123" not in out
        assert "abcdef123456" not in out

    def test_plain_text_passes_through(self, callbacks: Any) -> None:
        assert callbacks._sanitize_exception_text("plain failure text") == "plain failure text"

    def test_redact_string_covers_bearer_jwt(self, callbacks: Any) -> None:
        """P2 漏网样例：空格分隔 Bearer JWT（无 = : 分隔符，自有正则够不着）→ REDACTED。"""
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N0"
        out = callbacks._sanitize_exception_text(f"upstream rejected Bearer {jwt}")
        assert "eyJhbGciOiJIUzI1NiIs" not in out

    def test_redact_string_covers_bare_sk_key(self, callbacks: Any) -> None:
        """P2 漏网样例：裸 sk- key（前缀后无分隔符）→ REDACTED。"""
        out = callbacks._sanitize_exception_text("auth failed for sk-proj-abc123def456ghi789jkl")
        assert "sk-proj-abc123def456ghi789jkl" not in out


# ====================== 失败告警：状态码提取 ======================


class TestExtractFailureStatusCode:
    def test_proxy_code_channel(self, callbacks: Any) -> None:
        exc = _ProxyShapeError("err")
        exc.code = "400"  # type: ignore[attr-defined]
        assert callbacks._extract_failure_status_code(exc) == "400"

    def test_sdk_status_code_channel(self, callbacks: Any) -> None:
        exc = _SdkShapeError("err")
        exc.status_code = 503  # type: ignore[attr-defined]
        assert callbacks._extract_failure_status_code(exc) == "503"

    def test_httpx_response_channel(self, callbacks: Any) -> None:
        assert callbacks._extract_failure_status_code(_HttpxShapeError("err", response_status=429)) == "429"

    def test_code_precedes_status_code(self, callbacks: Any) -> None:
        """探测序 .code → .status_code：两者同在时 .code 优先（对齐 litellm get_error_information）。"""
        exc = _ProxyShapeError("err")
        exc.code = "400"  # type: ignore[attr-defined]
        exc.status_code = 500  # type: ignore[attr-defined]
        assert callbacks._extract_failure_status_code(exc) == "400"

    def test_all_missing_returns_empty(self, callbacks: Any) -> None:
        assert callbacks._extract_failure_status_code(RuntimeError("plain")) == ""
        assert callbacks._extract_failure_status_code(None) == ""


# ====================== 失败告警：告警行拼装与路由 ======================


class TestEmitAlert:
    @staticmethod
    def _base_kwargs(**over: Any) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": "chat-standard",
            "litellm_params": {
                "model": "openai/qwen3.7-plus",
                "metadata": {"model_group": "chat-standard"},
            },
            "stream": False,
            "call_type": "acompletion",
        }
        kwargs.update(over)
        return kwargs

    def test_4xx_routes_to_warning_with_fields(self, callbacks: Any) -> None:
        exc = _SdkShapeError("bad request shape")
        exc.status_code = 400  # type: ignore[attr-defined]
        _emit(callbacks, self._base_kwargs(exception=exc))
        assert callbacks.fake_logger.warnings, "4xx 应落 WARNING"
        assert callbacks.fake_logger.errors == []
        line = callbacks.fake_logger.warnings[-1]
        assert "[gateway-failure-alert]" in line
        assert "alias=chat-standard" in line
        assert "deployment=openai/qwen3.7-plus" in line
        assert "exception_class=_SdkShapeError" in line
        assert "status_code=400" in line
        assert "stream=False" in line
        assert "call_type=acompletion" in line
        assert "duration_s=1.500" in line

    def test_deployment_level_alias_falls_back_to_model_group(self, callbacks: Any) -> None:
        """部署级形态：kwargs["model"]=上游 id、别名只剩 metadata.model_group 在场。"""
        exc = _SdkShapeError("upstream boom")
        exc.status_code = 502  # type: ignore[attr-defined]
        _emit(
            callbacks,
            self._base_kwargs(
                model="openai/qwen3.7-plus",
                litellm_params={
                    "model": "openai/qwen3.7-plus",
                    "metadata": {"model_group": "chat-standard"},
                },
                exception=exc,
            ),
        )
        line = callbacks.fake_logger.errors[-1]
        assert "alias=chat-standard" in line
        assert "deployment=openai/qwen3.7-plus" in line

    @pytest.mark.parametrize(
        "exc_factory,category",
        [
            (lambda: _ProxyShapeError("500 internal"), "other"),  # 5xx
            (lambda: _SdkShapeError("Request timed out after 60s"), "timeout"),  # 超时（无状态码）
            (lambda: _SdkShapeError("Connection error"), "connection"),  # 连接（无状态码）
            (lambda: RuntimeError("plain no status"), "other"),  # 无状态码
        ],
    )
    def test_non_4xx_routes_to_error(self, callbacks: Any, exc_factory: Any, category: str) -> None:
        _emit(callbacks, self._base_kwargs(exception=exc_factory()))
        assert callbacks.fake_logger.errors, "5xx/连接/超时/无状态码应落 ERROR"
        assert callbacks.fake_logger.warnings == []
        assert f"category={category}" in callbacks.fake_logger.errors[-1]

    def test_double_quotes_replaced_in_msg_field(self, callbacks: Any) -> None:
        """活体实证：msg 内双引号会破坏 key=value 解析——必须替换为单引号（仅剩包裹引号两枚）。"""
        exc = _SdkShapeError('weird "quoted" upstream failure')
        exc.status_code = 500  # type: ignore[attr-defined]
        _emit(callbacks, self._base_kwargs(exception=exc))
        line = callbacks.fake_logger.errors[-1]
        assert line.count('"') == 2
        assert "'quoted'" in line

    def test_long_message_truncated_to_200(self, callbacks: Any) -> None:
        exc = _SdkShapeError("x" * 250)
        exc.status_code = 500  # type: ignore[attr-defined]
        _emit(callbacks, self._base_kwargs(exception=exc))
        line = callbacks.fake_logger.errors[-1]
        msg_value = line.rsplit('msg="', 1)[1][:-1]
        assert len(msg_value) == 200

    def test_missing_exception_degrades_fields(self, callbacks: Any) -> None:
        """kwargs 无 exception（分发链结构漂移）→ 字段降级不崩：class=None / category=other / 状态码占位。"""
        _emit(
            callbacks,
            {
                "model": "chat-speed",
                "stream": True,
                "call_type": "acompletion",
            },
        )
        line = callbacks.fake_logger.errors[-1]
        assert "exception_class=None" in line
        assert "category=other" in line
        assert "status_code=-" in line
        assert "stream=True" in line

    def test_missing_litellm_params_falls_back_to_model(self, callbacks: Any) -> None:
        """kwargs 无 litellm_params（proxy 边界形态）→ 别名/部署模型同回落 kwargs["model"]。"""
        exc = _SdkShapeError("boom")
        exc.status_code = 500  # type: ignore[attr-defined]
        _emit(
            callbacks,
            {"model": "chat-standard-deepseek", "exception": exc, "stream": False},
        )
        line = callbacks.fake_logger.errors[-1]
        assert "alias=chat-standard-deepseek" in line
        assert "deployment=chat-standard-deepseek" in line
        assert "call_type=unknown" in line

    def test_non_dict_litellm_params_is_safe(self, callbacks: Any) -> None:
        """litellm_params 非 dict（结构漂移）不崩，字段照常降级。"""
        exc = _SdkShapeError("boom")
        exc.status_code = 500  # type: ignore[attr-defined]
        _emit(callbacks, {"model": "chat-speed", "litellm_params": "on", "exception": exc})
        line = callbacks.fake_logger.errors[-1]
        assert "alias=chat-speed" in line


# ====================== 失败告警：钩子 fail-open ======================


class TestFailureHookFailOpen:
    async def test_emit_alert_exception_is_swallowed_with_warning(
        self, callbacks: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """钩子自身故障 → 只记 WARNING 不外抛（失败处理链不得被告警面放大）。"""

        def _boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("emit 内核故障")

        monkeypatch.setattr(callbacks.gateway_failure_alerter, "_emit_alert", _boom)
        await callbacks.gateway_failure_alerter.async_log_failure_event(
            {"exception": RuntimeError("upstream down")}, None, None, None
        )
        assert any("failure hook failed" in message for message in callbacks.fake_logger.warnings)
        assert callbacks.fake_logger.errors == []

    async def test_happy_path_returns_none(self, callbacks: Any) -> None:
        """正常路径：钩子返回 None（litellm 不消费返回值），告警行落 ERROR/WARNING。"""
        out = await callbacks.gateway_failure_alerter.async_log_failure_event(
            {
                "model": "chat-standard",
                "litellm_params": {"model": "openai/qwen3.7-plus", "metadata": {}},
                "exception": RuntimeError("Connection error"),
                "stream": False,
                "call_type": "acompletion",
            },
            None,
            datetime(2026, 11, 22, 0, 0, 0),
            datetime(2026, 11, 22, 0, 0, 3),
        )
        assert out is None
        assert any("[gateway-failure-alert]" in message for message in callbacks.fake_logger.errors)
