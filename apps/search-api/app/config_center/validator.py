"""资产校验层：五 dataId 的 pydantic 结构校验 + 密钥字面量嗅探。

结构、字段范围与初值依据 docs/contract/nacos-config.md §1~§5（键位即契约）；逐条校验
规则见 §3 校验规则段与 §2 安全条款，每个模型/校验器 docstring 标注契约出处。

安全口径：
- 密钥嗅探：值中出现疑似密钥形态（`^(sk-|tvly-|fc-|as_sk-)`，§2）或敏感字段名
  （key/secret/token/password）携带非 env 名的值 → 整版拒收（ADR-005：密钥绝不入 Nacos）；
- 违规描述只含 JSON 路径，绝不含值本身——校验错误会进 ERROR 日志，值泄露即缺陷。

容错口径：
- pydantic 模型对未知键宽容（extra="allow"）——契约「新增节点提示词新增键，不改结构」；
  已知键的类型/范围错误拒收，未知键不拒收（热更兼容优先）。
"""
import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from app.config_center.defaults import DATA_IDS


class AssetValidationError(ValueError):
    """资产校验失败：violations 为违规定位（JSON 路径），绝不含值本身。"""

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__("；".join(violations))


# ---- 密钥嗅探（§2 ADR-005 安全条款）----

SECRET_PREFIX_RE = re.compile(r"^(sk-|tvly-|fc-|as_sk-)")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
SENSITIVE_FIELD_RE = re.compile(r"key|secret|token|password", re.IGNORECASE)


def sniff_secret_violations(payload: Any) -> list[str]:
    """递归嗅探整份配置：违规返回定位路径列表（如 servers.tavily.auth），无值泄露。

    两条规则：
    1. 任何字符串值以疑似密钥前缀开头（sk-/tvly-/fc-/as_sk-）→ 违规；
    2. 字段名含 key/secret/token/password 且值为字符串 → 值必须为空或 env 变量名
       形态（UPPER_SNAKE）——mcp 条目的指针语义（§2「值只能是指针」）由此统一兜住。
    """
    violations: list[str] = []

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                field_path = f"{path}.{key}" if path else str(key)
                if (
                    isinstance(key, str)
                    and SENSITIVE_FIELD_RE.search(key)
                    and isinstance(value, str)
                    and value
                    and not ENV_NAME_RE.fullmatch(value)
                ):
                    violations.append(f"{field_path}：敏感字段名携带非 env 名值")
                _walk(value, field_path)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                _walk(item, f"{path}[{index}]")
        elif isinstance(node, str):
            if SECRET_PREFIX_RE.match(node):
                violations.append(f"{path}：疑似密钥字面量")

    _walk(payload, "")
    return violations


# ---- 顶层信封（契约 §0：每个 dataId 顶层必带）----


class _Envelope(BaseModel):
    model_config = ConfigDict(extra="allow")
    schema_version: int = Field(ge=1)
    updated_at: str = Field(min_length=1)
    note: str = ""


# ---- §1 prompts ----


class PromptsPayload(_Envelope):
    """提示词模板：键位即契约（{graph}.{node} / memory.* / home.* / common.*）。

    值为 Jinja2 子集模板字符串；home.suggest_pool 为运营内容数组（§1 v1.11）。
    键缺失不拒收（§1「Nacos 键缺失时回落默认」），键集不锁定（新增键不改结构）。
    """

    model_config = ConfigDict(extra="allow")
    prompts: dict[str, Any]

    @field_validator("prompts")
    @classmethod
    def _check_prompt_values(cls, value: dict[str, Any]) -> dict[str, Any]:
        for key, item in value.items():
            if key == "home.suggest_pool":
                if not isinstance(item, list) or not all(isinstance(s, str) for s in item):
                    raise ValueError("home.suggest_pool 必须为字符串数组")
                if len(item) > 8:
                    raise ValueError("home.suggest_pool 长度上限 8")
                for entry in item:
                    if not entry:
                        raise ValueError("home.suggest_pool 条目不得为空串")
                    if len(entry) > 200:
                        raise ValueError("home.suggest_pool 单条 ≤200 字符")
                continue
            if not isinstance(item, str):
                raise ValueError(f"{key}：模板值必须是字符串")
        return value


# ---- §2 mcp ----

McpRole = Literal[
    "primary_search",
    "chinese_search",
    "fallback_search",
    "fallback_search_secondary",  # §2 v1.18：anysearch 降为搜索备位链次级（searxng 链首）
    "url_reader",
    "url_reader_backup",
]
McpTransport = Literal["http", "sse", "stdio"]


class McpCircuit(BaseModel):
    """熔断参数（§2 circuit 节）。"""

    model_config = ConfigDict(extra="allow")
    failure_threshold: int = Field(ge=1)
    cooldown_s: int = Field(ge=1)


class McpServerEntry(BaseModel):
    """MCP 条目：值只能是指针（*_env 指向 .env 变量名，§2）。

    transport 形态校验（v1.8 三枚举）：http/sse → url_env 必填；stdio → command_env
    必填；其余指针字段允许留空。所有非空指针值必须是 .env 变量名形态。
    """

    model_config = ConfigDict(extra="allow")
    enabled: bool
    role: McpRole
    transport: McpTransport
    url_env: str = ""
    command_env: str = ""
    args_env: str = ""
    auth: str = ""
    timeout_s: int = Field(ge=1)
    circuit: McpCircuit

    @model_validator(mode="after")
    def _check_pointer_by_transport(self) -> "McpServerEntry":
        pointer_by_transport = {"http": "url_env", "sse": "url_env", "stdio": "command_env"}
        required = pointer_by_transport[self.transport]
        if not getattr(self, required):
            raise ValueError(f"{self.transport} 传输形态要求 {required} 指针必填")
        for pointer in (self.url_env, self.command_env, self.args_env, self.auth):
            if pointer and not ENV_NAME_RE.fullmatch(pointer):
                raise ValueError("指针字段必须是 .env 变量名形态")
        return self


class McpPayload(_Envelope):
    """MCP 清单：servers 字典键为源名（tavily/bocha/…，新增源不改结构）。"""

    model_config = ConfigDict(extra="allow")
    servers: dict[str, McpServerEntry]


# ---- §3 spec ----

QUERY_TYPES = ("factoid", "list", "comparison", "multi_facet", "temporal", "how_to", "other")


class TypeQueryPair(BaseModel):
    """类型→query 数区间（§3：1≤min≤max≤4；契约值为 [min, max] 二元数组）。"""

    min_count: int = Field(ge=1, le=4)
    max_count: int = Field(ge=1, le=4)

    @model_validator(mode="before")
    @classmethod
    def _accept_pair_array(cls, value: Any) -> Any:
        """契约形态 [min, max] 数组 → 内部 min_count/max_count 字段。"""
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return {"min_count": value[0], "max_count": value[1]}
        return value

    @model_validator(mode="after")
    def _check_order(self) -> "TypeQueryPair":
        if self.min_count > self.max_count:
            raise ValueError("type_query_map 区间要求 min ≤ max")
        return self


class RetrySpec(BaseModel):
    """§3 v1.3：initial_interval_s × backoff_factor + jitter 退避写法。"""

    model_config = ConfigDict(extra="allow")
    max_attempts: int = Field(ge=1)
    initial_interval_s: float = Field(gt=0)
    backoff_factor: float = Field(ge=1)
    jitter: bool = True


class FastSpec(BaseModel):
    model_config = ConfigDict(extra="allow")
    type_query_map: dict[
        Literal["factoid", "list", "comparison", "multi_facet", "temporal", "how_to", "other"],
        TypeQueryPair,
    ]
    fuse_top_n: int = Field(ge=1)
    max_answer_chars: int = Field(gt=0)  # §3 v1.14：正整数（3000 为硬上限兜底值）
    llm_timeout_s: int = Field(gt=0)
    tool_timeout_s: int = Field(gt=0)
    turn_timeout_s: int = Field(gt=0)
    recursion_limit: int = Field(gt=0)
    retry: RetrySpec

    @field_validator("type_query_map")
    @classmethod
    def _check_seven_types_covered(cls, value: dict) -> dict:
        missing = [t for t in QUERY_TYPES if t not in value]
        if missing:
            raise ValueError(f"type_query_map 须覆盖 7 类枚举，缺 {missing}")
        return value


class CompactSpec(BaseModel):
    """§3 v1.4：压缩管线参数（0 < keep_fraction < trigger_fraction < 1）。"""

    model_config = ConfigDict(extra="allow")
    trigger_fraction: float = Field(gt=0, lt=1)
    keep_fraction: float = Field(gt=0, lt=1)
    truncate_args_max_length: int = Field(gt=0)
    evict_token_limit: int = Field(gt=0)
    url_read_head_tokens: int = Field(gt=0)

    @model_validator(mode="after")
    def _check_fraction_order(self) -> "CompactSpec":
        if not (self.keep_fraction < self.trigger_fraction):
            raise ValueError("compact 要求 0 < keep_fraction < trigger_fraction < 1")
        return self


class DeepSpec(BaseModel):
    model_config = ConfigDict(extra="allow")
    max_subagents: int = Field(ge=1)
    read_min: int = Field(ge=0)
    read_max: int = Field(ge=1)
    reflect_rounds: int = Field(ge=0)
    max_reflect_rounds: int = Field(ge=0)
    fuse_top_n: int = Field(ge=1)
    summary_max_tokens: int = Field(gt=0)
    token_budget: int = Field(gt=0)
    llm_timeout_s: int = Field(gt=0)
    tool_timeout_s: int = Field(gt=0)
    turn_timeout_s: int = Field(gt=0)
    code_exec_enabled: bool = False
    code_exec_timeout_s: int = Field(gt=0)
    code_exec_cpu_limit: float = Field(gt=0, le=4)  # §3 v1.5：0 < cpu ≤ 4（核）
    code_exec_mem_limit_mb: int = Field(ge=256, le=8192)  # §3 v1.5：256 ≤ mem ≤ 8192（MB）
    recursion_limit: int = Field(gt=0)
    retry: RetrySpec
    compact: CompactSpec

    @model_validator(mode="after")
    def _check_deep_order(self) -> "DeepSpec":
        if self.read_min > self.read_max:
            raise ValueError("deep 要求 read_min ≤ read_max")
        if self.reflect_rounds > self.max_reflect_rounds:
            raise ValueError("deep 要求 reflect_rounds ≤ max_reflect_rounds")
        return self


class TurnSpec(BaseModel):
    """§3 v1.6：节缺失时组装层回落内置默认 reject——校验层仅拒收非法值。"""

    model_config = ConfigDict(extra="allow")
    default_on_conflict: Literal["reject", "enqueue", "interrupt", "rollback"]


class RoutingSpec(BaseModel):
    """§3 v1.13：default_mode 经 modes[default_mode] 解析为图名——须存在于 modes 键域。"""

    model_config = ConfigDict(extra="allow")
    default_mode: str = Field(min_length=1)
    modes: dict[str, str]

    @model_validator(mode="after")
    def _check_default_in_modes(self) -> "RoutingSpec":
        if not self.modes:
            raise ValueError("routing.modes 键不得为空")
        if self.default_mode not in self.modes:
            raise ValueError("routing.default_mode 须存在于 routing.modes 键域")
        return self


class FollowupsSpec(BaseModel):
    """§3 v1.10：实验功能参数（默认关闭）+ v1.10 P1 强制裕量。"""

    model_config = ConfigDict(extra="allow")
    enabled: bool
    count: int = Field(ge=1, le=5)  # 与 turns.followups ≤5 对齐（data-model.md §1.3）
    generate_timeout_ms: int = Field(ge=1000)
    sse_hold_window_ms: int = Field(ge=1)

    @model_validator(mode="after")
    def _check_hold_window_margin(self) -> "FollowupsSpec":
        margin_required = self.generate_timeout_ms + 2000
        if self.sse_hold_window_ms < margin_required:
            raise ValueError(
                f"sse_hold_window_ms ≥ generate_timeout_ms + 2000（强制裕量 {margin_required}ms）"
            )
        return self


class KbSpec(BaseModel):
    model_config = ConfigDict(extra="allow")
    top_k_per_query: int = Field(ge=1)
    rerank_timeout_s: int = Field(ge=1)


class HistorySpec(BaseModel):
    """§3 v1.1：弃用迁移期与 memory.short_term 并存（以新为准），校验保持宽容。"""

    model_config = ConfigDict(extra="allow")
    window_turns: int = Field(ge=1)
    answer_head_chars: int = Field(ge=1)


class ShortTermSpec(BaseModel):
    model_config = ConfigDict(extra="allow")
    window_turns: int = Field(ge=1)
    max_tokens_before_summary: int = Field(gt=0)
    max_summary_tokens: int = Field(gt=0)


class ExtractionSpec(BaseModel):
    model_config = ConfigDict(extra="allow")
    trigger_idle_minutes: int = Field(ge=1)
    max_turns_per_job: int = Field(ge=1)
    candidate_min_confidence: float = Field(gt=0, le=1)  # §3：0 < candidate_min_confidence ≤ 1
    update_min_confidence: float = Field(gt=0, le=1)
    similar_top_k: int = Field(ge=1)
    answer_head_chars: int = Field(ge=1)


class TtlSpec(BaseModel):
    model_config = ConfigDict(extra="allow")
    stale_after_days_no_hit: int = Field(ge=1)
    sweep_interval_hours: int = Field(ge=1)


class MemorySpec(BaseModel):
    """§3 v1.1 memory 节（M5 生效）：注入/短期/抽取/TTL 参数，范围越界拒收。"""

    model_config = ConfigDict(extra="allow")
    inject_top_k: int = Field(gt=0, le=20)  # §3：0 < inject_top_k ≤ 20
    inject_min_score: float = Field(gt=0, le=1)
    max_inject_tokens: int = Field(gt=0)
    profile_max_tokens: int = Field(gt=0)
    short_term: ShortTermSpec
    extraction: ExtractionSpec
    ttl: TtlSpec


class SpecPayload(_Envelope):
    """Agent Spec：fast/deep 必带；turn/followups/routing/memory/kb/history 缺失时
    组装层按契约回落内置默认并告警（§3 各节注），校验层不拒收缺节。"""

    model_config = ConfigDict(extra="allow")
    fast: FastSpec
    deep: DeepSpec
    turn: TurnSpec | None = None
    routing: RoutingSpec | None = None
    followups: FollowupsSpec | None = None
    kb: KbSpec | None = None
    history: HistorySpec | None = None
    memory: MemorySpec | None = None


# ---- §4 skills ----


class SkillEntry(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str = Field(min_length=1)
    enabled: bool
    description: str = ""


class SkillsPayload(_Envelope):
    model_config = ConfigDict(extra="allow")
    skills: list[SkillEntry]


# ---- §5 model mapping ----


class ChatMapping(BaseModel):
    """§5：chat.options 键即 GET /api/v1/models 的 options，default 须是 options 键。"""

    model_config = ConfigDict(extra="allow")
    options: dict[str, str]
    default: str = Field(min_length=1)

    @field_validator("options")
    @classmethod
    def _check_options_non_empty(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            raise ValueError("chat.options 不得为空")
        for key, alias in value.items():
            if not key or not alias:
                raise ValueError("chat.options 键与别名均不得为空")
        return value

    @model_validator(mode="after")
    def _check_default_in_options(self) -> "ChatMapping":
        if self.default not in self.options:
            raise ValueError("chat.default 须存在于 chat.options 键域")
        return self


class ModelMappingPayload(_Envelope):
    model_config = ConfigDict(extra="allow")
    chat: ChatMapping
    vision: str = Field(min_length=1)
    embedding: str = Field(min_length=1)
    rerank: str = Field(min_length=1)
    light: str = Field(min_length=1)
    memory: str = Field(min_length=1)


# ---- 校验入口 ----

MODEL_BY_DATA_ID: dict[str, type[BaseModel]] = {
    "ai_search.agent.prompts.json": PromptsPayload,
    "ai_search.agent.mcp.json": McpPayload,
    "ai_search.agent.spec.json": SpecPayload,
    "ai_search.agent.skills.json": SkillsPayload,
    "ai_search.model.mapping.json": ModelMappingPayload,
}


def validate_asset(data_id: str, payload: Any) -> dict[str, Any]:
    """校验单份资产：密钥嗅探 + pydantic 结构校验，通过原样返回 payload，失败整版拒收。

    - dataId 不在五 dataId 清单内 → 拒收（监听只注册五个，意外 dataId 在组装层已拦截）；
    - payload 非 JSON 对象（数组/标量）→ 拒收；
    - 违规信息只含定位路径（嗅探）或字段定位（pydantic errors），绝不含值。
    """
    if data_id not in DATA_IDS:
        raise AssetValidationError([f"未知 dataId：{data_id}"])
    if not isinstance(payload, dict):
        raise AssetValidationError([f"{data_id}：顶层必须是 JSON 对象"])

    violations = sniff_secret_violations(payload)
    if violations:
        raise AssetValidationError(violations)

    model_cls = MODEL_BY_DATA_ID[data_id]
    try:
        model_cls.model_validate(payload)
    except ValidationError as exc:
        raise AssetValidationError(
            [f"{data_id}：{'；'.join(_format_pydantic_error(error) for error in exc.errors())}"]
        ) from exc
    return payload


def _format_pydantic_error(error: Mapping[str, Any]) -> str:
    """pydantic 错误 → 定位串（loc 路径 + msg），不携带输入值。

    形参取 Mapping 视图：exc.errors() 的条目是 pydantic 的 ErrorDetails TypedDict，
    按 Mapping 契约消费（只读 get），与 pydantic 具体字典注解脱耦。
    """
    loc = ".".join(str(part) for part in error.get("loc", ()))
    message = error.get("msg", "")
    custom = error.get("ctx", {}).get("error") if isinstance(error.get("ctx"), dict) else None
    if custom is not None:
        # model_validator 抛出的 ValueError 挂在 ctx.error——只取其 message
        message = str(custom)
    return f"{loc}：{message}" if loc else message

