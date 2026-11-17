"""unit 层：deploy/litellm/litellm_callbacks.py 纯判定逻辑（B13 增量二 GLM 腿 thinking 剥离）。

为何要桩：litellm 不在 app venv（`uv run --package search-api` 环境装的是应用依赖，
网关镜像里的 litellm 不参与后端门禁）；本模块顶层只依赖 `litellm._logging` 与
`litellm.integrations.custom_logger` 两个 import——测试先往 sys.modules 注入同名桩模块，
再用 importlib 从 `deploy/litellm/` 加载被测文件，纯函数与两钩子全程零网络零容器。

钉住的判定矩阵（契约面见 ARCHITECTURE v1.57 §7 与模块 docstring）：
- 别名判定：只有 `*-glm` 收尾（chat-standard-glm / chat-speed-glm）命中，casefold 归一；
- 部署级判定：上游真实 model id 取 provider 后基名，`glm` 前缀命中
  （openai/GLM-5.3、openai/GLM-5.3-flash）——fallback 换腿只在这一形态可识别；
- 非 GLM 腿（qwen / deepseek / 非字符串形态）一律零触碰；
- 剥离面：顶层 `enable_thinking` / `think` 与 `extra_body` 嵌套两处；
- 两钩子（proxy 边界 / 部署级）命中即剥离并返回请求体，未命中/无可剥离返回 None
  （litellm 保持原 kwargs）；钩子异常 fail-open 返回 None + 一条 WARNING（网关横切面
  故障不得打断所有请求）；
- 边界：extra_body 非 dict、metadata 非 dict、model 非字符串均不崩。
"""

from __future__ import annotations

import importlib.util
import sys
import types
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


class _FakeVerboseLogger:
    """litellm._logging.verbose_proxy_logger 的替身：记录 info/warning 文案。"""

    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warnings: list[str] = []

    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        self.infos.append(message % args if args else message)

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        self.warnings.append(message % args if args else message)


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

    for name, module in {
        "litellm": litellm_pkg,
        "litellm._logging": logging_mod,
        "litellm.integrations": integrations_pkg,
        "litellm.integrations.custom_logger": custom_logger_mod,
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
