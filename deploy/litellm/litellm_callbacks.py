"""LiteLLM 自定义回调：GLM 腿 thinking 参数剥离（B13 增量批·增量二）+ OTel decisions 兼容垫片
+ 网关失败告警（post-M2 resilience 工单③）。

溯源：docs/ARCHITECTURE.md v1.57 §7「GLM 腿 thinking 参数剥离」钉注；
OTel 垫片 = plan-decision-model-20261118 批次 2 接线实测发现（2026-11-19）；
失败告警 = plan-post-m2-resilience-20261121 工单③（慢性 500「NoneType.__aiter__」
失败瞬间真实上游行为不可见，2026-11-22）。

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

## 上游失败告警（本文件第三职责）

慢性 500「'NoneType' object has no attribute '__aiter__'」实证缺口：上游失败在
流式 + fallbacks 耗尽路径被 litellm 包装成晦涩异常，失败瞬间的真实上游行为
（哪条腿、什么异常、什么状态码）在网关日志不可见。`GatewayFailureAlerter` 挂
failure 侧（`async_log_failure_event`）：每次上游调用失败——含 fallback 链
每一腿的失败与最终耗尽——输出一行 `[gateway-failure-alert]` 前缀的结构化告警
（grep 友好）。字段全为元数据：请求别名 / 部署模型 / 异常类名 / 失败分类
（timeout〔含 "timed out" 文本〕/ connection / rate_limit / auth / other，纯文本
匹配零副作用；数字状态码 429/401/403 按 \\b 词边界匹配防 "1429ms"/"4013" 误命中）/
状态码 / stream / call_type / 耗时秒 + 异常文本截 200 字符（先脱敏再截断，三道：
litellm 自带 redact_string〔Bearer 空格分隔 token / 裸 sk- / JWT 等形态，容器
1.106.4 实测整段替换 REDACTED、完整 URL 不消除〕→ URL 掩码 → 自带 key 片段
兜底；msg 内双引号替换为单引号——活体实证双引号破坏 key=value 解析）。
**B15 告警纪律：零 prompt payload、零完整 URL、零密钥**。4xx 状态码记 WARNING
（客户端/配置侧），其余（5xx / 连接 / 超时 / 无状态码）记 ERROR。请求别名口径：
部署级（fallback 换腿后 kwargs["model"] 已被 Router 改写、与 litellm_params.model
同值）取 metadata.model_group，proxy 边界取 kwargs["model"]。钩子自身异常
fail-open（只记 WARNING 不打断失败处理链——与 glm_thinking_guard 同纪律）。
签名与 litellm 1.106.4 容器内实读对齐：
`async_log_failure_event(self, kwargs, response_obj, start_time, end_time)`
——exception 挂在 kwargs["exception"]（`_failure_handler_helper_fn` 注入）、
response_obj 恒 None；与新版「独立 exception 形参」签名不同，升级 litellm
时须复检（R8/R16 同型护栏理念）。

## OTel decisions 兼容垫片（本文件第二职责）

litellm 1.106.4 上游 bug：`integrations/opentelemetry.py` 的 success/failure 全链路
（set_attributes:2530 起、_record_metrics:1709 等消费点）把 `response_obj` 当 dict 调
`.get()`——decisions 路由（`/v1/systemone`）返回的是 `DecisionsResponse`（pydantic
v2 模型、无 `.get`），每次 decisions 调用必产一条 `AttributeError` traceback 且该 span
属性中断（探针 200 不受影响，纯观测面损伤）。聊天等常规路由的 `ModelResponse` 是
dict-like（有 `.get`），不受影响。上游修复版本未定（1.107.0 供应链投毒事件后升级面
冻结，批次 0 digest 钉版），故在本模块 import 时对 OTel 四个 logging 入口方法打
defensive 垫片：仅当响应对象**非 dict-like 且可 `model_dump()`** 时转 dict 再进原实现
——chat 路径零触碰（ModelResponse 有 `.get`，守卫直接放行）；dump 失败 fail-open 保持
原对象。升级 litellm 时此处须复检（入口签名与 1.106.4 对齐；R8/R16 同型护栏理念）。

## 注册（config.yaml）

```yaml
litellm_settings:
  callbacks:
    - "litellm_callbacks.glm_thinking_guard"
    - "litellm_callbacks.gateway_failure_alerter"
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

import re
from typing import Any

from litellm._logging import verbose_proxy_logger
from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils.secret_redaction import redact_string

__all__ = [
    "GlmThinkingParamGuard",
    "glm_thinking_guard",
    "GatewayFailureAlerter",
    "gateway_failure_alerter",
    "patch_otel_decisions_response_compat",
]

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


# ---------------------------------------------------------------------------
# OTel decisions 兼容垫片（模块 import 时自动应用一次）
# ---------------------------------------------------------------------------


def _normalize_response_for_otel(response_obj: Any) -> Any:
    """非 dict-like 的 pydantic 响应转 dict；dict-like / 无 model_dump 的对象原样放行。

    守卫口径：`hasattr(obj, "get")` 为真（dict、ModelResponse 等既有链路）零触碰；
    仅 DecisionsResponse 这类「pydantic v2 但不模拟 dict」的响应需要转换。dump 失败
    fail-open 返回原对象——垫片自身故障不得改变观测链路的失败语义。
    """
    if response_obj is None or hasattr(response_obj, "get") or not hasattr(response_obj, "model_dump"):
        return response_obj
    try:
        return response_obj.model_dump()
    except Exception as exc:  # noqa: BLE001 —— fail-open：dump 失败维持上游原行为
        verbose_proxy_logger.warning(
            "otel_decisions_compat: model_dump failed (passthrough original): %s", exc
        )
        return response_obj


def patch_otel_decisions_response_compat() -> None:
    """给 OTel 四个 logging 入口打 decisions 响应归一化垫片（幂等、防御式）。

    背景：litellm 1.106.4 OTel 集成对 decisions 路由的 response_obj 假设为 dict
    （opentelemetry.py set_attributes:2530 / _record_metrics:1709 等消费点直接
    `.get()`），DecisionsResponse 无 `.get` → 每次 decisions 调用一条 AttributeError
    traceback 且 span 属性中断。入口四方法是 logging core 统一通道（success/failure
    × sync/async），在此归一化一次即覆盖全部下游消费点。签名与 1.106.4 对齐
    （(self, kwargs, response_obj, start_time, end_time)），升级 litellm 时须复检。
    """
    import asyncio
    import functools

    try:
        from litellm.integrations.opentelemetry import OpenTelemetry
    except Exception as exc:  # noqa: BLE001 —— 导入失败仅降级（无 OTel 观测面）
        verbose_proxy_logger.warning("otel_decisions_compat: OpenTelemetry import failed, skip: %s", exc)
        return

    entry_methods = (
        "log_success_event",
        "log_failure_event",
        "async_log_success_event",
        "async_log_failure_event",
    )
    patched: list[str] = []
    for name in entry_methods:
        original = getattr(OpenTelemetry, name, None)
        if original is None or getattr(original, "_decisions_compat", False):
            continue

        if asyncio.iscoroutinefunction(original):

            @functools.wraps(original)
            async def wrapper(
                self: Any, kwargs: Any, response_obj: Any, start_time: Any, end_time: Any,
                __orig: Any = original,
            ) -> Any:
                return await __orig(self, kwargs, _normalize_response_for_otel(response_obj), start_time, end_time)

        else:

            @functools.wraps(original)
            def wrapper(
                self: Any, kwargs: Any, response_obj: Any, start_time: Any, end_time: Any,
                __orig: Any = original,
            ) -> Any:
                return __orig(self, kwargs, _normalize_response_for_otel(response_obj), start_time, end_time)

        wrapper._decisions_compat = True  # type: ignore[attr-defined] —— 幂等标记
        setattr(OpenTelemetry, name, wrapper)
        patched.append(name)

    if patched:
        verbose_proxy_logger.info(
            "otel_decisions_compat: normalized pydantic responses at entry methods: %s", ", ".join(patched)
        )


patch_otel_decisions_response_compat()


# ---------------------------------------------------------------------------
# 网关失败告警（failure 侧 CustomLogger；post-M2 resilience 工单③）
# ---------------------------------------------------------------------------

#: 异常文本告警截断长度（工单钉点：截 200 字符）
_EXC_MSG_MAX_LEN = 200

#: 异常对象状态码属性探测序——对齐 litellm 1.106.4 `get_error_information` 自述口径：
#: ProxyException 用 .code、LiteLLM SDK 异常用 .status_code、httpx 异常只在
#: .response.status_code 上暴露。
_STATUS_CODE_ATTRS: tuple[str, ...] = ("code", "status_code")


def _extract_failure_status_code(exception: Any) -> str:
    """从异常对象尽力提取状态码；全部缺失返回空串（不做任何网络/副作用操作）。

    探测序：.code（ProxyException）→ .status_code（LiteLLM SDK 异常）→
    .response.status_code（httpx.HTTPStatusError）。字符串化后仍可能无值
    （如 None/空串）则跳过该属性。
    """
    for attr in _STATUS_CODE_ATTRS:
        value = getattr(exception, attr, None)
        if value is not None and str(value) not in ("", "None"):
            return str(value)
    response = getattr(exception, "response", None)
    value = getattr(response, "status_code", None) if response is not None else None
    return str(value) if value is not None else ""


def _sanitize_exception_text(text: str) -> str:
    """告警行脱敏（B15 告警纪律：零完整 URL、零密钥）。三道顺序不可换：

    ① redact_string（litellm 自带）先行——覆盖空格分隔 Bearer token / 裸 sk- /
    JWT 等自有正则够不着的形态；须在 URL 掩码之前跑，否则 URL 整段被掩掉会
    吞掉 ？api_key= 查询参数形态的匹配上下文。
    ② URL 掩码——redact_string 不消除完整 URL（容器 1.106.4 实测）。
    ③ 自带 key 形态兜底（redact_string 已覆盖大部分，此处保底防御）。
    """
    text = redact_string(text)
    text = re.sub(r"https?://\S+", "<url-redacted>", text)
    text = re.sub(r"(?i)(api[_-]?key|authorization|bearer)\s*[=:]\s*\S+", r"\1=<redacted>", text)
    return text


def _classify_failure(exception_class: str, message: str) -> str:
    """失败原因分类（工单钉点：纯文本匹配、零副作用）：timeout > connection > rate_limit > auth > other。

    匹配源 = 异常类名 + 异常文本（casefold；类名如 APITimeoutError 亦可在文本
    缺失时命中 timeout）。按序命中即返回，供 grep 聚合与告警路由初判。
    数字状态码（429/401/403）按 \\b 词边界匹配——"after 1429ms"/"attempt 4013"
    这类时长/计数值含相同子串，裸子串匹配会误判 rate_limit/auth（评审 P1 边界）。
    """
    haystack = f"{exception_class} {message}".casefold()
    if "timeout" in haystack or "timed out" in haystack:
        return "timeout"
    if "connect" in haystack:  # connect 是 connection 的子串，单查覆盖两形态
        return "connection"
    if "rate" in haystack or re.search(r"\b429\b", haystack):
        return "rate_limit"
    if "auth" in haystack or re.search(r"\b401\b|\b403\b", haystack):
        return "auth"
    return "other"


class GatewayFailureAlerter(CustomLogger):
    """上游失败告警回调（failure 侧；零 payload 零 URL 零密钥）。

    见模块 docstring「上游失败告警（本文件第三职责）」：把「上游失败 / fallback
    链耗尽」翻译成一行 grep 友好的结构化告警，钉住失败瞬间的真实上游行为证据。
    钩子自身异常一律吞掉只记 WARNING——网关横切面故障不得放大失败处理链。
    """

    async def async_log_failure_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        """failure 侧钩子（签名 = litellm 1.106.4 容器实读；exception 在 kwargs["exception"]）。

        dispatch（litellm_logging.py）传 response_obj=None，只消费 kwargs 里的
        元数据；不影响失败语义，只追加一条告警日志。
        """
        try:
            self._emit_alert(kwargs=kwargs, start_time=start_time, end_time=end_time)
        except Exception as exc:  # noqa: BLE001 —— 网关横切面防御：告警钩子故障不打断失败处理链
            verbose_proxy_logger.warning(
                "gateway_failure_alerter: failure hook failed (ignored): %s", exc
            )

    def _emit_alert(self, kwargs: dict[str, Any], start_time: Any, end_time: Any) -> None:
        """拼装并落一行 [gateway-failure-alert] 结构化告警（4xx→WARNING，其余→ERROR）。"""
        exception = kwargs.get("exception")
        exception_class = type(exception).__name__ if exception is not None else "None"
        message = str(exception) if exception is not None else ""
        # 双引号先行替换（活体实证：msg 内双引号破坏 key=value 解析）再截断
        sanitized_msg = _sanitize_exception_text(message).replace('"', "'")[:_EXC_MSG_MAX_LEN]
        status_code = _extract_failure_status_code(exception) if exception is not None else ""

        litellm_params = kwargs.get("litellm_params")
        if not isinstance(litellm_params, dict):
            litellm_params = {}
        metadata = litellm_params.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}

        # 部署模型：litellm_params.model（Router 选定部署的 litellm_params.model 形态）
        deployment = litellm_params.get("model") or kwargs.get("model") or "unknown"
        # 请求别名：proxy 边界 kwargs["model"] 即别名；部署级（fallback 换腿后）
        # kwargs["model"] 已被 Router 改写为部署模型（与 litellm_params.model 同值），
        # 真别名只剩 metadata.model_group 在场——该形态下别名取 model_group，
        # 否则 alias 与 deployment 两字段重复浪费。
        model = kwargs.get("model")
        model_group = metadata.get("model_group")
        if model_group and (model is None or model == litellm_params.get("model")):
            alias = model_group
        else:
            alias = model or model_group or "unknown"

        try:
            duration_s = (
                f"{(end_time - start_time).total_seconds():.3f}"
                if start_time is not None and end_time is not None
                else ""
            )
        except Exception:  # noqa: BLE001 —— 耗时字段缺失不阻塞告警行
            duration_s = ""

        stream = bool(kwargs.get("stream") or litellm_params.get("stream") or False)
        call_type = kwargs.get("call_type") or litellm_params.get("call_type") or "unknown"
        category = _classify_failure(exception_class, message)

        line = (
            f"[gateway-failure-alert] alias={alias} deployment={deployment} "
            f"exception_class={exception_class} category={category} "
            f"status_code={status_code or '-'} stream={stream} call_type={call_type} "
            f"duration_s={duration_s or '-'} msg=\"{sanitized_msg}\""
        )
        if status_code.isdigit() and int(status_code) < 500:
            verbose_proxy_logger.warning(line)  # 4xx：客户端/配置侧（401/403/429 等）
        else:
            verbose_proxy_logger.error(line)  # 5xx / 连接 / 超时 / 无状态码：基础设施侧


#: config.yaml `litellm_settings.callbacks` 消费的模块级实例
#: （`litellm_callbacks.gateway_failure_alerter` 点分路径 → 本实例）
gateway_failure_alerter = GatewayFailureAlerter()
