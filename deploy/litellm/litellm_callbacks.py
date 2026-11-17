"""LiteLLM 自定义回调：GLM 腿 thinking 参数剥离（B13 增量批·增量二）。

溯源：docs/ARCHITECTURE.md v1.57 §7「GLM 腿 thinking 参数剥离」钉注。

## 为什么需要这个钩子

批次 9 synthesis 提速方案给 chat 强模型钉了 `extra_body={"enable_thinking": False}`
（apps/search-api/app/agent/llm.py `build_chat_model`——DashScope qwen 通道的关思维链
键）。该键随 litellm 降级链（fallbacks）转发给 chat-standard-glm 腿时，GLM-5.3-Flash
上游直接 400：「该模型始终思考，不支持关闭思考；请使用 low、high 或 max」。
config.yaml 的 `drop_params: true` 只覆盖 OpenAI 规范键，不覆盖 `extra_body` 里的
自定义键——故在网关侧做请求体变换。

## 机制

`litellm_settings.callbacks` 注册的自定义 `CustomLogger` 实例，实现两级 pre-call：

- `async_pre_call_deployment_hook(kwargs, call_type)` —— **部署级，主力通道**。
  每次实际出站前调用（`litellm/utils.py::async_pre_call_deployment_hook`），
  定位在 Router 选定部署之后——因此 **fallback 换腿同样生效**（chat-standard 失败
  落到 chat-standard-glm 的那一跳也走本钩子），kwargs["model"] 为该部署的
  litellm_params.model（如 `openai/GLM-5.3-flash`），另以 metadata.model_group
  （别名，如 `chat-standard-glm`）兜底判定。
- `async_pre_call_hook(user_api_key_dict, cache, data, call_type)` —— proxy 请求
  边界（`ProxyLogging.pre_call_hook`），data["model"] 为请求别名（`chat-standard-glm`
  / `chat-speed-glm`）。直调 GLM 别名的请求在此即可剥离，不必等路由。

判定口径（两形态，任一命中即 GLM 腿）：
- **别名**：`chat-standard-glm` / `chat-speed-glm`（以 `-glm` 结尾）——proxy 边界
  的 `data["model"]` 与 `metadata.model_group` 形态；
- **上游模型 id**：`openai/GLM-5.3` / `openai/GLM-5.3-flash`（取 provider 后基名，
  `glm` 前缀）——部署级 kwargs 的形态。`litellm/router.py` 在选中部署后把
  `kwargs["model"]` 改写成 `deployment.litellm_params.model`，别名不再在场，
  **fallback 换腿（chat-standard → chat-standard-glm）只在这一形态可识别**。

命中后从请求体剥离 `enable_thinking` 与 `think`（顶层与 `extra_body` 内两处都剥，
上游不接受任何形态的「关思考」）。GLM 腿允许思考（更慢更贵，ARCH v1.57 §7 已接受）。

## 注册（config.yaml）

```yaml
litellm_settings:
  callbacks: ["litellm_callbacks.glm_thinking_guard"]
```

`get_instance_fn` 会把点分路径解析为 **config.yaml 同目录下的本地模块**（本文件随
`deploy/litellm` 目录级 bind mount 进容器 `/app/litellm-config/`），无需改镜像或
PYTHONPATH；目录挂载的 inode 好处见 docker-compose.yml litellm 服务注。

## 契约边界

- 本文件属网关静态配置面（与 config.yaml 同批），**不是 Nacos 五 dataId**——改动走 git。
  **改注册或改实现均需 `docker compose --project-directory . restart litellm`**：`callbacks`
  列表在启动时解析、本模块也在启动时 import 一次（目录挂载只保证新文件可见，Python
  不会重新加载已导入模块）。
- 只做「剥离键」，不改 messages/采样参数/模型别名；非 GLM 腿零触碰（返回 None，
  litellm 保持原 kwargs）。异常一律不外抛（配置面防御：钩子故障不能打断所有请求），
  仅记一条 WARNING。
"""

from typing import Any

from litellm._logging import verbose_proxy_logger
from litellm.integrations.custom_logger import CustomLogger

__all__ = ["GlmThinkingParamGuard", "glm_thinking_guard"]

#: GLM 腿不接受的「关思考」键（DashScope qwen 的 enable_thinking / Ollama 的 think）
_THINKING_KEYS: tuple[str, ...] = ("enable_thinking", "think")

#: GLM 腿**请求别名**的后缀（只有别名以此收尾：chat-standard-glm / chat-speed-glm）。
#: 上游真实 model id（openai/GLM-5.3、openai/GLM-5.3-flash）不带 `-glm` 尾——那一形态
#: 由 `_is_glm_upstream_id` 按「provider 后基名 glm 前缀」判定，两条判定不可混用。
_GLM_SUFFIX = "-glm"


def _is_glm_alias(model: Any) -> bool:
    """请求别名是否为 GLM 腿（以 `-glm` 结尾，大小写不敏感）。

    覆盖 chat-standard-glm / chat-speed-glm 两个稳定别名（config.yaml model_list
    的 model_name），proxy 边界 data["model"] 与部署级 metadata.model_group 都是
    这一形态。
    """
    return isinstance(model, str) and model.strip().casefold().endswith(_GLM_SUFFIX)


def _is_glm_upstream_id(model: Any) -> bool:
    """部署级 kwargs["model"] 是否为 GLM 上游（`openai/GLM-5.3`、`openai/GLM-5.3-flash`）。

    Router 选中部署后会把 kwargs["model"] 改写成 deployment.litellm_params.model
    （`litellm/router.py` 的 `kwargs["model"] = deployment["litellm_params"]["model"]`）
    ——此时别名已丢失、`-glm` 尾也不在，故对「provider/模型名」取基名判 `glm` 前缀。
    fallback 换腿（chat-standard → chat-standard-glm）只在这一层可识别，metadata
    里的 model_group 仍是用户请求的原别名。
    """
    if not isinstance(model, str):
        return False
    basename = model.strip().rsplit("/", 1)[-1].casefold()
    return basename.startswith("glm")


def _resolve_glm_reason(payload: dict[str, Any]) -> str | None:
    """判定请求是否 GLM 腿：返回命中理由（alias/upstream），未命中 None。

    判定顺序：上游模型 id（部署级形态，覆盖 fallback）→ 请求别名（proxy 边界形态）
    → metadata.model_group（别名兜底）。
    """
    model = payload.get("model")
    if _is_glm_upstream_id(model):
        return "upstream"
    if _is_glm_alias(model):
        return "alias"
    metadata = payload.get("metadata")
    if isinstance(metadata, dict) and _is_glm_alias(metadata.get("model_group")):
        return "alias"
    return None


def _strip_thinking_keys(payload: dict[str, Any]) -> list[str]:
    """从请求体剥离关思考键（顶层 + extra_body 两处），返回被剥离的键名清单。

    原地修改 payload（litellm 的 modified_kwargs 语义即为就地生效）；返回清单供
    观测日志使用。无命中返回空列表。
    """
    stripped: list[str] = []
    for key in _THINKING_KEYS:
        if key in payload:
            payload.pop(key)
            stripped.append(key)
    extra_body = payload.get("extra_body")
    if isinstance(extra_body, dict):
        for key in _THINKING_KEYS:
            if key in extra_body:
                extra_body.pop(key)
                stripped.append(f"extra_body.{key}")
    return stripped


class GlmThinkingParamGuard(CustomLogger):
    """GLM 腿 thinking 参数剥离回调（两级 pre-call；非 GLM 腿零触碰）。

    见模块 docstring：部署级钩子覆盖 fallback 换腿，proxy 边界钩子覆盖直调别名。
    任一钩子异常一律吞掉并告警——网关横切面故障不得放大为全量请求失败。
    """

    async def async_pre_call_deployment_hook(
        self, kwargs: dict[str, Any], call_type: Any
    ) -> dict[str, Any] | None:
        """部署级 pre-call：每次实际出站前（含 fallback 换腿）剥离关思考键。"""
        try:
            reason = _resolve_glm_reason(kwargs)
            if reason is None:
                return None
            stripped = _strip_thinking_keys(kwargs)
            if stripped:
                verbose_proxy_logger.info(
                    "glm_thinking_guard: stripped %s from GLM leg request "
                    "(model=%s, matched_by=%s, call_type=%s)",
                    ",".join(stripped),
                    kwargs.get("model"),
                    reason,
                    call_type,
                )
            return kwargs if stripped else None
        except Exception as exc:  # noqa: BLE001 —— 网关横切面防御：钩子故障不打断请求
            verbose_proxy_logger.warning(
                "glm_thinking_guard: deployment hook failed (ignored): %s", exc
            )
            return None

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict[str, Any],
        call_type: Any,
    ) -> dict[str, Any] | None:
        """proxy 边界 pre-call：请求别名为 GLM 腿时先剥一轮（直调路径免等路由）。"""
        try:
            reason = _resolve_glm_reason(data)
            if reason is None:
                return None
            stripped = _strip_thinking_keys(data)
            if stripped:
                verbose_proxy_logger.info(
                    "glm_thinking_guard: stripped %s at proxy boundary "
                    "(model=%s, matched_by=%s, call_type=%s)",
                    ",".join(stripped),
                    data.get("model"),
                    reason,
                    call_type,
                )
            return data if stripped else None
        except Exception as exc:  # noqa: BLE001 —— 同上（边界钩子故障不拒绝请求）
            verbose_proxy_logger.warning(
                "glm_thinking_guard: proxy pre_call hook failed (ignored): %s", exc
            )
            return None


#: config.yaml `litellm_settings.callbacks` 消费的模块级实例
#: （`litellm_callbacks.glm_thinking_guard` 点分路径 → 本实例）
glm_thinking_guard = GlmThinkingParamGuard()
