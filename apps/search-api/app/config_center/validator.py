"""资产校验层：五 dataId 的 pydantic 结构校验 + 密钥字面量嗅探。

结构、字段范围与初值依据 docs/contract/nacos-config.md §1~§5（键位即契约）；逐条校验
规则见 §3 校验规则段与 §2 安全条款，每个模型/校验器 docstring 标注契约出处。

v1.33 决策模型键组（Decision Model 工单批次 2，ADR-016）：`fast.decision.*` 标量键组
白名单定向扩容（home.suggest_pool 非模板先例）+ questions_json 方言硬校验 + shadow
严格布尔——详见下方 DECISION_* 常量区注。

安全口径：
- 密钥嗅探：值中出现疑似密钥形态（`^(sk-|tvly-|fc-|as_sk-)`，§2）或敏感字段名
  （key/secret/token/password）携带非 env 名的值 → 整版拒收（ADR-005：密钥绝不入 Nacos）；
- 违规描述只含 JSON 路径，绝不含值本身——校验错误会进 ERROR 日志，值泄露即缺陷。

容错口径：
- pydantic 模型对未知键宽容（extra="allow"）——契约「新增节点提示词新增键，不改结构」；
  已知键的类型/范围错误拒收，未知键不拒收（热更兼容优先）。
"""
import json
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

#: fast.synthesis 四段变体行前缀（v1.20 三条 + v1.27「- 回顾整理路径」——行级切分锚点，
#: 任一缺失拒收）
SYNTHESIS_VARIANT_PREFIXES: tuple[str, ...] = (
    "- 常规路径",
    "- 最小检索路径",
    "- 免检索直答路径",
    "- 回顾整理路径",
)

#: 【v1.33 退役】fast.rewrite 输出 schema 串枚举 token 硬校验（v1.20/v1.27
#: `"search_strategy":"direct|verify|web|recall"` + `|navigational|other"`）——
#: 减面后 fast.rewrite 不再承载分类 schema（判据唯一事实源 = fast.decision.questions_json），
#: 防漂移载体同步迁移：questions_json 的 search_strategy choice options 缺策略枚举值
#: → decision_prompts_warnings 软告警（枚举内容热更迭代频繁，告警不拒收）。

#: fast.synthesis 必须保留的历史注入占位符（v1.27 / security P3③）：缺失则 recall 轮
#: 静默丢历史注入（`render_prompt` 无该变量时不报错，历史块被无提示弃用）——拒收。
SYNTHESIS_REQUIRED_PLACEHOLDERS: tuple[str, ...] = ("{{history_block}}",)

#: common.safety_reply 段行的类别 → 前缀映射（B13 输入安全工单——行级类别段锚点；
#: v1.31 三类别：danger/profanity/privacy）。**唯一事实源**：validator 据此做前缀
#: 齐备硬校验（缺段拒收、键缺失不拒收回落内置默认），safety.reply 直接 import 同一
#: 映射取段切分——两处不再各自按序号取段（避免位置耦合）。
SAFETY_REPLY_CATEGORY_PREFIXES: dict[str, str] = {
    "danger": "- 危险请求回复段：",
    "profanity": "- 辱骂回复段：",
    "privacy": "- 隐私请求回复段：",
    # v1.32（B14 增量 B）：涉政话题段（中性引导式框架句；politics 一律 reject 直答）
    "politics": "- 涉政话题回复段：",
}

#: 前缀有序元组（校验与测试遍历用；顺序即契约段序，生成自上面的映射）
SAFETY_REPLY_LINE_PREFIXES: tuple[str, ...] = tuple(
    SAFETY_REPLY_CATEGORY_PREFIXES.values()
)

#: fast.rewrite / deep.planner 输出 schema 须含的 safety 字段 token（B13——精确形态
#: 防散文绕过，带引号纪律；软校验见 safety_token_warnings：告警不拒收。v1.31 枚举扩
#: 四值 +privacy；**v1.32 五值 +politics**）。
SAFETY_FIELD_TOKEN = '"safety":"none|danger|profanity|privacy|politics"'

#: safety token 软校验覆盖的两键（B13——rewrite 与 planner 同构产出 safety 初判）
SAFETY_TOKEN_PROMPT_KEYS: tuple[str, ...] = ("fast.rewrite", "deep.planner")


# ---- §1 v1.33 决策模型键组（fast.decision.*——prompts dataId 内非模板标量值，----
# ---- home.suggest_pool 非模板先例的定向扩容；nacos-config.md §1 v1.33）        ----

#: SystemOne questions 题面载体键（纯 JSON 无占位符；消费方 decision_client）
DECISION_QUESTIONS_KEY = "fast.decision.questions_json"
DECISION_CONFIDENCE_FLOOR_KEY = "fast.decision.confidence_floor"
DECISION_TIMEOUT_MS_KEY = "fast.decision.timeout_ms"
DECISION_SHADOW_KEY = "fast.decision.shadow"

#: 标量键组（类型越界拒收该 dataId——validator 白名单扩容的类型闸面）
DECISION_SCALAR_KEYS: tuple[str, ...] = (
    DECISION_CONFIDENCE_FLOOR_KEY,
    DECISION_TIMEOUT_MS_KEY,
    DECISION_SHADOW_KEY,
)

#: SystemOne 方言题型枚举（R19 探针实证——litellm box 等方言上游 400 拒收）
DECISION_QUESTION_TYPES: tuple[str, ...] = ("noul", "choice", "score")

#: 四约定题键（decision_client 按题键取结果；软校验齐备性）
DECISION_CONTRACT_QUESTION_KEYS: tuple[str, ...] = (
    "query_type",
    "search_strategy",
    "answerable",
    "safety_shadow",
)

#: 标量键边界（与 decision_client 代码侧守卫同口径）
_DECISION_CONFIDENCE_BOUNDS = (0.0, 1.0)  # 0 < 值 ≤ 1
_DECISION_TIMEOUT_MS_BOUNDS = (100, 10000)  # 含端点


def decision_questions_violations(value: Any) -> list[str]:
    """questions_json 方言硬校验（R19 探针 §四/§七——违规发上去必 400，坏配置不上线
    沿 ADR-005 语义）：违规返回定位列表（不含值本身）；合法返回空表。

    三条硬规则：题对象 `type` ∈ {noul, choice, score}（litellm box 方言被 400 拒收）；
    `choice` 的 criteria 必须是对象（选项→判据），缺失同拒；`score` 的 criteria 必须
    是数组（锚点按序成 legend）。
    """
    try:
        questions = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return [f"{DECISION_QUESTIONS_KEY}：须为合法 JSON 字符串"]
    if not isinstance(questions, dict) or not questions:
        return [f"{DECISION_QUESTIONS_KEY}：须为非空 JSON 对象"]
    violations: list[str] = []
    for question_key, question in questions.items():
        path = f"{DECISION_QUESTIONS_KEY}.{question_key}"
        if not isinstance(question, dict):
            violations.append(f"{path}：题对象须为 JSON 对象")
            continue
        question_type = question.get("type")
        if question_type not in DECISION_QUESTION_TYPES:
            violations.append(
                f"{path}：type 须 ∈ {list(DECISION_QUESTION_TYPES)}"
                "（litellm box 方言上游 400 拒收）"
            )
            continue
        criteria = question.get("criteria")
        if question_type == "choice" and not isinstance(criteria, dict):
            violations.append(f"{path}：choice 的 criteria 须为对象（选项→判据，上游 400 拒收）")
        elif question_type == "score" and not isinstance(criteria, list):
            violations.append(f"{path}：score 的 criteria 须为数组（锚点按序成 legend，上游 400 拒收）")
    return violations


def decision_prompts_warnings(prompts_section: Mapping[str, Any]) -> list[str]:
    """四约定题键齐备与 query_type other 兜底软校验（§1 v1.33——**告警不拒收**）。

    沿 safety_token_warnings 先例：返回告警文案列表（空 = 无告警），调用方
    （PromptsPayload 校验钩子）逐条落 ConfigLog WARNING。键缺失不告警（运行面
    decision_client 回落内置默认并告警——validator 侧对旧 payload 零噪音）。

    other 兜底建议仅 query_type 题（判据回滚 v0 2026-11-19 澄清：契约 §1 v1.33
    原文中 search_strategy/safety_shadow 选项为原值无 other——一刀切规则会对契约
    原文自身误报；策略拿不准回落 web 在代码侧保守缺省）。
    """
    warnings: list[str] = []
    raw = prompts_section.get(DECISION_QUESTIONS_KEY)
    if not isinstance(raw, str):
        return warnings
    try:
        questions = json.loads(raw)
    except ValueError:
        return warnings  # 非法 JSON 由硬校验拒收，软校验不重复报
    if not isinstance(questions, dict):
        return warnings
    missing = [
        key for key in DECISION_CONTRACT_QUESTION_KEYS if key not in questions
    ]
    if missing:
        warnings.append(
            f"{DECISION_QUESTIONS_KEY}：缺约定题键 {missing}"
            "（软校验不拒收——decision 侧该题回落保守缺省）"
        )
    # v1.20/v1.27 防漂移检查的迁移载体：search_strategy choice options 须含四值策略枚举
    # （缺值 → decision 永不产出该策略 → 全量轮回落 web 的 R17 漂移面）——软告警不拒收
    # （options 与判据同属热更迭代面，硬拒会挡运营迭代）。
    strategy_question = questions.get("search_strategy")
    if isinstance(strategy_question, dict) and strategy_question.get("type") == "choice":
        options = strategy_question.get("options")
        options_set = set(options) if isinstance(options, list) else set()
        absent = [value for value in ("direct", "recall", "verify", "web") if value not in options_set]
        if absent:
            warnings.append(
                f"{DECISION_QUESTIONS_KEY}.search_strategy：options 缺策略枚举值 {absent}"
                "（v1.20/v1.27 防漂移检查迁移载体——软校验不拒收，缺值策略永不产出）"
            )
    # other 兜底建议仅 query_type 题（v1.33 原文形态——search_strategy/safety_shadow
    # 选项为契约原值不含 other，一刀切会对契约原文自身误报）
    query_type_question = questions.get("query_type")
    if isinstance(query_type_question, dict) and query_type_question.get("type") == "choice":
        options = query_type_question.get("options")
        if isinstance(options, list) and "other" not in options:
            warnings.append(
                f"{DECISION_QUESTIONS_KEY}.query_type：choice 建议含 other 兜底选项"
                "（百炼官方建议；软校验不拒收）"
            )
    return warnings


def _check_decision_scalar(key: str, value: Any) -> None:
    """标量键类型/范围闸（越界 raise ValueError → 整版拒收）。

    - confidence_floor：数值（布尔拒收）且 0 < 值 ≤ 1；
    - timeout_ms：整数（布尔拒收）且 100 ≤ 值 ≤ 10000；
    - shadow：严格布尔（SafetySpec.enabled 同款——字符串/数字形态拒收，kill switch
      防 "false" 形态漂移）。
    """
    if key == DECISION_SHADOW_KEY:
        if not isinstance(value, bool):
            raise ValueError(f"{key}：shadow 须为布尔（JSON true/false，字符串/数字形态拒收）")
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key}：须为数值（布尔/字符串形态拒收）")
    if key == DECISION_CONFIDENCE_FLOOR_KEY:
        low, high = _DECISION_CONFIDENCE_BOUNDS
        if not (low < float(value) <= high):
            raise ValueError(f"{key}：须满足 0 < 值 ≤ 1")
        return
    low, high = _DECISION_TIMEOUT_MS_BOUNDS
    if not float(value).is_integer() or not (low <= float(value) <= high):
        raise ValueError(f"{key}：须为整数且 {low} ≤ 值 ≤ {high}")


def safety_token_warnings(prompts_section: Mapping[str, Any]) -> list[str]:
    """fast.rewrite / deep.planner 的 safety 字段 token 软校验（B13；**告警不拒收**）。

    沿 retry.connection 五键软校验先例：返回告警文案列表（空 = 无告警），调用方
    （PromptsPayload 校验钩子）逐条落 ConfigLog WARNING。不拒收的理由：旧提示词若被
    硬拒，生产环境会整版回落 L4 内置默认——运行态提示词被静默替换成内置版本（提示词
    漂移陷阱）；safety 判定缺失时运行层按 fail-open 兜底为 none。
    """
    warnings: list[str] = []
    for key in SAFETY_TOKEN_PROMPT_KEYS:
        value = prompts_section.get(key)
        if isinstance(value, str) and SAFETY_FIELD_TOKEN not in value:
            warnings.append(
                f"{key}：输出 schema 缺 safety 字段 token {SAFETY_FIELD_TOKEN}"
                "（B13 输入安全增量；软校验不拒收，safety 判定缺失按 none 兜底）"
            )
    return warnings


class PromptsPayload(_Envelope):
    """提示词模板：键位即契约（{graph}.{node} / memory.* / home.* / common.*）。

    值为 Jinja2 子集模板字符串；home.suggest_pool 为运营内容数组（§1 v1.11）。
    键缺失不拒收（§1「Nacos 键缺失时回落默认」），键集不锁定（新增键不改结构）。

    v1.20 结构性校验（检索策略增量防热更漂移，§3 校验规则）：键存在即校验——
    ① `fast.rewrite` 模板值的输出 schema 串须含 `search_strategy`（缺则模型按旧契约
    产出，direct/recall 边永不触发）；② `fast.synthesis` 模板值须含四段行前缀（行级
    变体切分的锚点，任一缺失 synthesis 会渲染出错的变体合体）。

    v1.27 扩容（recall 回顾分支 + navigational 题型）：① `fast.rewrite` 枚举串须含
    四值策略串（`"search_strategy":"direct|verify|web|recall"`）与 navigational 尾段
    （`|navigational|other"`）——**精确形态，散文提及不算**（security P3②）；
    ② 四段行前缀校验 +「- 回顾整理路径」；③ `{{history_block}}` 占位符必在
    （缺失则 recall 轮静默丢历史注入——security P3③）。

    B13 增量（输入安全）：① `common.safety_reply` 三段行前缀齐备硬校验（缺段拒收，
    SAFETY_REPLY_LINE_PREFIXES）；② fast.rewrite / deep.planner 的 safety 字段 token
    软校验（safety_token_warnings——**告警不拒收**，防旧提示词被硬拒后生产回落 L4
    内置默认造成提示词漂移的部署陷阱）。
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
            if key == DECISION_QUESTIONS_KEY:
                # §1 v1.33：题面载体 = JSON 字符串，逐题过方言硬校验（违规拒收——发上去必 400）
                if not isinstance(item, str):
                    raise ValueError(f"{key}：题面载体必须是 JSON 字符串")
                violations = decision_questions_violations(item)
                if violations:
                    raise ValueError("；".join(violations))
                continue
            if key in DECISION_SCALAR_KEYS:
                _check_decision_scalar(key, item)
                continue
            if not isinstance(item, str):
                raise ValueError(f"{key}：模板值必须是字符串")

        # 【v1.33 退役】§3 v1.20 条 1 + v1.27 扩容的 fast.rewrite schema 枚举串硬校验：
        # 减面后 fast.rewrite 不再承载分类 schema（判据唯一事实源 = fast.decision.
        # questions_json，方言硬校验见 decision_questions_violations；策略枚举防漂移
        # 软告警见 decision_prompts_warnings）——slim 形态合法通过，旧全量形态亦容忍
        # （发布过渡窗：Nacos 旧值 + 新代码，判据双份冗余无害）。
        # §3 v1.20 条 2 + v1.27 扩容：fast.synthesis 须含四段行前缀与 history_block 占位符
        synthesis = value.get("fast.synthesis")
        if isinstance(synthesis, str):
            lines = synthesis.splitlines()
            for prefix in SYNTHESIS_VARIANT_PREFIXES:
                if not any(line.startswith(prefix) for line in lines):
                    raise ValueError(
                        f"fast.synthesis：缺变体行前缀 {prefix}（v1.27 四段模板）"
                    )
            # security P3③：缺占位符时 recall 轮静默丢历史注入（render 不报错）——拒收
            for placeholder in SYNTHESIS_REQUIRED_PLACEHOLDERS:
                if placeholder not in synthesis:
                    raise ValueError(
                        f"fast.synthesis：缺历史注入占位符 {placeholder}"
                        "（v1.27 recall 变体契约）"
                    )
        # §1 B13：common.safety_reply 三段行前缀齐备（拒答话术行级类别段锚点，沿
        # fast.synthesis 变体前缀先例——缺段拒收）；键缺失不拒收（回落内置默认）。
        safety_reply = value.get("common.safety_reply")
        if isinstance(safety_reply, str):
            lines = safety_reply.splitlines()
            for prefix in SAFETY_REPLY_LINE_PREFIXES:
                if not any(line.startswith(prefix) for line in lines):
                    raise ValueError(
                        f"common.safety_reply：缺行前缀 {prefix}（B13 拒答话术三段模板）"
                    )
        return value

    @model_validator(mode="after")
    def _warn_missing_safety_token(self) -> "PromptsPayload":
        """§1 B13：fast.rewrite / deep.planner 的 safety 字段 token 软校验
        （safety_token_warnings——告警不拒收；接既有 ConfigLog WARNING 通道）。"""
        from app.core.logger import ConfigLog  # 局部导入：校验层不拖重启动链

        for message in safety_token_warnings(self.prompts):
            ConfigLog(message=message, data_id="ai_search.agent.prompts.json").warning()
        return self

    @model_validator(mode="after")
    def _warn_decision_prompts(self) -> "PromptsPayload":
        """§1 v1.33：fast.decision.questions_json 四约定题键齐备与 query_type other
        兜底软校验（decision_prompts_warnings——告警不拒收；键缺失零告警，运行面回落
        内置默认）。"""
        from app.core.logger import ConfigLog  # 局部导入：校验层不拖重启动链

        for message in decision_prompts_warnings(self.prompts):
            ConfigLog(message=message, data_id="ai_search.agent.prompts.json").warning()
        return self


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

QUERY_TYPES = (
    "factoid",
    "list",
    "comparison",
    "multi_facet",
    "temporal",
    "how_to",
    "navigational",  # §3 v1.27：导航查询第八类（fast.rewrite 输出；deep.planner 判 factoid）
    "other",
)


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
    """§3 v1.3：initial_interval_s × backoff_factor + jitter 退避写法。

    v1.24 起嵌套 `connection` 节由 `connection_retry_warnings` 软校验（extra="allow"
    收下原样，非法不拒收——运行期整节回落主节，灰度安全）。
    """

    model_config = ConfigDict(extra="allow")
    max_attempts: int = Field(ge=1)
    initial_interval_s: float = Field(gt=0)
    backoff_factor: float = Field(ge=1)
    jitter: bool = True


#: 连接类长窗重试节五键（§3 v1.24，nacos-config.md）
CONNECTION_RETRY_KEYS: tuple[str, ...] = (
    "max_attempts",
    "initial_interval_s",
    "backoff_factor",
    "max_interval_s",
    "total_window_s",
)
#: 联动约束软校验的固定量（决策 4 预算推导：前置预估 10s + 单次成功调用 8s）
CONNECTION_RETRY_PREFIX_S = 10.0
CONNECTION_RETRY_SINGLE_CALL_S = 8.0


def _connection_key_valid(key: str, value: Any) -> bool:
    """单键边界（§3 v1.24）：max_attempts 为 ≥1 整数、backoff_factor ≥1、其余三键 >0。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if key == "max_attempts":
        return float(value).is_integer() and value >= 1
    if key == "backoff_factor":
        return value >= 1
    return value > 0


def connection_retry_warnings(fast_section: Mapping[str, Any]) -> list[str]:
    """`retry.connection` 节软校验（B11 P1a；**告警不拒收**——灰度安全）。

    返回告警文案列表（空 = 无告警）；调用方（FastSpec 校验钩子）逐条落 ConfigLog
    WARNING。四类告警：
    ① 节点形态非法（非对象）→ 运行期整节回落 retry 主节；
    ② 任一键缺失/类型或越界非法 → 同上（整节回落，不做半新半旧混合）；
    ③ 联动约束 `前置预估 10s + total_window_s + 单次成功调用 8s > turn_timeout_s`
       → 提示同步评估 turn_timeout_s（用户裁决「总窗 ~30s」量级意图由
       total_window_s 承载，放宽时须复核预算）；
    ④【v1.32（B14 增量 A）】**`total_window_s < fast.llm_timeout_s` → 重试总窗小于
       单调用包络**（trace 89043f23 事故形态：窗 20 < 包络 30 → 包络超时类失败后首个
       重试决策点即被窗闸饿死，max_attempts=5 名实不符仅 2 次物理尝试）；建议对齐
       `total_window_s ≥ llm_timeout_s`。**软校验不拒收**（运行值可单键热更在线调窗）。
    """
    retry = fast_section.get("retry")
    if not isinstance(retry, Mapping):
        return []
    node = retry.get("connection")
    if node is None:
        return []  # 节缺失 = 现状行为（零告警零拒收）
    if not isinstance(node, Mapping):
        return ["retry.connection：节点形态非法（非对象）——运行期整节回落 retry 主节"]
    warnings: list[str] = []
    invalid = [
        key
        for key in CONNECTION_RETRY_KEYS
        if key not in node or not _connection_key_valid(key, node.get(key))
    ]
    if invalid:
        warnings.append(
            f"retry.connection：键非法 {invalid}——运行期整节回落 retry 主节（=现状行为）"
        )
        return warnings  # 键层面校验失败时联动约束无意义
    total_window = node.get("total_window_s")
    turn_timeout = fast_section.get("turn_timeout_s")
    if isinstance(total_window, (int, float)) and isinstance(turn_timeout, (int, float)):
        required = (
            CONNECTION_RETRY_PREFIX_S
            + float(total_window)
            + CONNECTION_RETRY_SINGLE_CALL_S
        )
        if required > float(turn_timeout):
            warnings.append(
                "retry.connection：联动约束告警（软校验不拒收）——"
                f"前置预估 {CONNECTION_RETRY_PREFIX_S:g}s + total_window_s "
                f"{float(total_window):g}s + 单次成功调用 "
                f"{CONNECTION_RETRY_SINGLE_CALL_S:g}s = {required:g}s > "
                f"fast.turn_timeout_s {float(turn_timeout):g}s；"
                "建议同步评估 turn_timeout_s"
            )
    # ④ v1.32（B14 增量 A）：窗-包络对齐——窗小于单调用包络时包络超时类失败的首个
    # 重试决策点即被窗闸饿死（trace 89043f23：窗 20 < 包络 30 仅 2 次物理尝试）。
    llm_timeout = fast_section.get("llm_timeout_s")
    if isinstance(total_window, (int, float)) and isinstance(llm_timeout, (int, float)):
        if float(total_window) < float(llm_timeout):
            warnings.append(
                "retry.connection：窗-包络对齐告警（软校验不拒收）——"
                f"total_window_s {float(total_window):g}s < fast.llm_timeout_s "
                f"{float(llm_timeout):g}s；包络超时类失败后首个重试决策点即被窗闸"
                "放弃（max_attempts 名实不符），建议对齐 total_window_s ≥ llm_timeout_s"
            )
    return warnings


class FastSpec(BaseModel):
    model_config = ConfigDict(extra="allow")
    type_query_map: dict[
        Literal[
            "factoid",
            "list",
            "comparison",
            "multi_facet",
            "temporal",
            "how_to",
            "navigational",
            "other",
        ],
        TypeQueryPair,
    ]
    fuse_top_n: int = Field(ge=1)
    max_answer_chars: int = Field(gt=0)  # §3 v1.14：正整数（3000 为硬上限兜底值）
    llm_timeout_s: int = Field(gt=0)
    tool_timeout_s: int = Field(gt=0)
    turn_timeout_s: int = Field(gt=0)
    #: §3 v1.32（B14 增量 C）：模式级思考开关——**键缺失合法**（默认值承载裁决值
    #: fast=off / deep=on），非法值拒收。
    thinking: Literal["on", "off"] = "off"
    recursion_limit: int = Field(gt=0)
    retry: RetrySpec

    @field_validator("type_query_map")
    @classmethod
    def _check_eight_types_covered(cls, value: dict) -> dict:
        missing = [t for t in QUERY_TYPES if t not in value]
        if missing:
            raise ValueError(f"type_query_map 须覆盖 8 类枚举，缺 {missing}")
        return value

    @model_validator(mode="after")
    def _warn_connection_retry(self) -> "FastSpec":
        """§3 v1.24：`retry.connection` 软校验（告警不拒收；运行期整节回落主节）。"""
        from app.core.logger import ConfigLog  # 局部导入：校验层不拖重启动链

        for message in connection_retry_warnings(self.model_dump()):
            ConfigLog(message=message, data_id="ai_search.agent.spec.json").warning()
        return self


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
    #: §3 v1.32（B14 增量 C）：deep 档思考开关（裁决值 default=on）
    thinking: Literal["on", "off"] = "on"
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


# ---- §3 B13 safety：ReDoS 灾难性回溯形态静态检测（grey_rules 兜底第一层）----

#: 外层无限量词（跟在 `)` 后）：`+` / `*` / `{n,}`（无上界）——有限量词（`?`、
#: `{n,m}`）不迭代不指数，不标记
_OUTER_QUANT_RE = re.compile(r"\)\s*(?:[+*]|\{\d+,\s*\})")

#: 分组体内可疑形态：任一量词（含 `?`——`(ab?)+` 每轮两歧义仍指数）或交替
#: （`(a|aa)+` 前缀重叠族）
_INNER_SUSPECT_RE = re.compile(r"(?:[+*?]|\{\d+(?:\s*,\s*\d*)?\}|\|)")


def _is_escaped(rule: str, index: int) -> bool:
    """rule[index] 是否被反斜杠转义（连续反斜杠计数定奇偶）。"""
    slashes = 0
    i = index - 1
    while i >= 0 and rule[i] == "\\":
        slashes += 1
        i -= 1
    return slashes % 2 == 1


def _has_catastrophic_form(rule: str) -> bool:
    """grey_rules 灾难性回溯（ReDoS）形态启发式：无限量词修饰的分组，其分组体内
    又含量词或交替 → 拒收。

    覆盖经典签名 `(a+)+` / `^(([a-z])+.)+[A-Z]…` / `(a|aa)+` / `(ab?)+`。启发式
    面向词表场景的运营规则（≤120 字符）：
    - 假阳（拒掉 `(foo|bar)+` 这类罕见写法）是安全方向——运营改写规则即可；
    - 假阴由运行面第二道防线兜底（safety_check 节点把词表/grey 扫描放线程 + 超时
      包络，超预算 fail-open 直通，事件循环不挂死）。
    """
    for match in _OUTER_QUANT_RE.finditer(rule):
        close = match.start()
        depth = 0
        open_idx = -1
        for i in range(close, -1, -1):
            ch = rule[i]
            if _is_escaped(rule, i):
                continue
            if ch == ")":
                depth += 1
            elif ch == "(":
                depth -= 1
                if depth == 0:
                    open_idx = i
                    break
        if open_idx < 0:
            continue  # 未闭合分组由 re.compile 拒收，本检测不重复报
        if _INNER_SUSPECT_RE.search(rule[open_idx + 1 : close]):
            return True
    return False


def check_regex_rule(rule: str, *, label: str, max_len: int) -> None:
    """运营正则单条硬校验（B15 抽取公共函数：grey_rules / injection_filter.
    patterns / url_guard.blocked_host_patterns 共用同一判据）：

    - 长度上限（超限拒收）；
    - re.compile 可编译（编译失败=拒收 violation，消息只含定位与原因不含原文）；
    - 灾难性回溯（ReDoS）形态静态拒收（`_has_catastrophic_form`）——量词分组内嵌
      量词/交替（如 `(a+)+`、`(a|aa)+`）对短 payload 即指数级回溯，运行面扫描在
      async 节点内执行、可挂死事件循环，必须在发布口拦截。
    """
    if len(rule) > max_len:
        raise ValueError(f"{label} 单条 ≤{max_len} 字符")
    try:
        re.compile(rule)
    except re.error as exc:
        raise ValueError(f"{label} 正则编译失败（violation）：{exc}") from exc
    if _has_catastrophic_form(rule):
        raise ValueError(
            f"{label} 疑似灾难性回溯形态（量词分组内嵌量词/交替，ReDoS "
            "violation）：请改写为有界量词形态"
        )


def _strict_bool_or_raise(value: Any) -> Any:
    """enabled 严格布尔共用判定（SafetySpec / UrlGuardSpec / InjectionFilterSpec
    ——非 bool 一律拒收：pydantic 默认会把 "false"/0 等形态 coerce 成布尔，导致
    validator 通过而运行面读到原始字符串（bool("false")=True，kill switch 反向
    失效）。validator 拒收 + 运行面严格判定的双防线之一。"""
    if not isinstance(value, bool):
        raise ValueError(
            "enabled 须为布尔（JSON true/false；字符串/数字形态拒收——"
            "kill switch 单键热更防形态漂移）"
        )
    return value


class UrlGuardSpec(BaseModel):
    """§3 B15（v1.37）safety.url_guard 子节：SSRF 防护黑名单加强面。

    代码硬底线（scheme ∈ {http,https} / 私网与元数据判定 / localhost 与短主机名）
    在 app/agent/safety/url_guard.py——本节黑名单是运营加强面，只能加强不能削弱。
    校验：enabled 严格布尔；blocked_hosts ≤200 条、每条非空 ≤253 字符（DNS 主机名
    上限）；blocked_host_patterns ≤50 条、逐条 check_regex_rule（compile + ReDoS
    静态拒收）；max_url_length 64~8192（含端点）。子节缺失=None 合法（运行面
    loader 回落 defaults 种子=部署即启用）。键名严禁含 token/key/secret/password
    （撞密钥嗅探，§2 安全条款）。
    """

    model_config = ConfigDict(extra="allow")
    enabled: bool = True
    blocked_hosts: list[str] = Field(default_factory=list, max_length=200)
    blocked_host_patterns: list[str] = Field(default_factory=list, max_length=50)
    max_url_length: int = Field(default=2048, ge=64, le=8192)

    @field_validator("enabled", mode="before")
    @classmethod
    def _check_enabled_strict_bool(cls, value: Any) -> Any:
        return _strict_bool_or_raise(value)

    @field_validator("blocked_hosts")
    @classmethod
    def _check_blocked_hosts(cls, value: list[str]) -> list[str]:
        for host in value:
            if not host:
                raise ValueError("blocked_hosts 条目不得为空串")
            if len(host) > 253:
                raise ValueError("blocked_hosts 单条 ≤253 字符（DNS 主机名上限）")
        return value

    @field_validator("blocked_host_patterns")
    @classmethod
    def _check_blocked_patterns_compilable(cls, value: list[str]) -> list[str]:
        for rule in value:
            check_regex_rule(rule, label="url_guard.blocked_host_patterns", max_len=120)
        return value


class InjectionFilterSpec(BaseModel):
    """§3 B15（v1.37）safety.injection_filter 子节：网页内容注入特征过滤配置。

    运行面匹配集 = 内置种子 ∪ 本表 patterns（Nacos 削不掉种子——loader 层并集
    去重）；on_hit=flag 观察先行（命中仅告警保留，跑出误判基线后单键热切 drop）。
    校验：enabled 严格布尔；patterns ≤50 条、逐条 check_regex_rule（compile +
    ReDoS 静态拒收，同 grey_rules——恶意正则可挂死事件循环，发布口拦截）；
    on_hit Literal["flag","drop"]；max_scan_chars 1000~20000（含端点）。键名严禁
    含 token/key/secret/password（§2）。
    """

    model_config = ConfigDict(extra="allow")
    enabled: bool = True
    patterns: list[str] = Field(default_factory=list, max_length=50)
    on_hit: Literal["flag", "drop"] = "flag"
    max_scan_chars: int = Field(default=16000, ge=1000, le=20000)

    @field_validator("enabled", mode="before")
    @classmethod
    def _check_enabled_strict_bool(cls, value: Any) -> Any:
        return _strict_bool_or_raise(value)

    @field_validator("patterns")
    @classmethod
    def _check_patterns_compilable(cls, value: list[str]) -> list[str]:
        for rule in value:
            check_regex_rule(rule, label="injection_filter.patterns", max_len=120)
        return value


class SafetySpec(BaseModel):
    """§3 B13 输入安全节（fail-open 语义——节缺失=合法 None，安全检查不挡检索）。

    词表纪律（§3 校验规则 v1.31→v1.32 四表口径）：danger_terms/profanity_terms/
    privacy_terms/politics_terms ≤200 条、每条非空 ≤64 字符且条目去重；enabled=true
    时四表至少其一非空；grey_rules ≤50 条、每条
    ≤120 字符且逐条 re.compile 校验（编译失败=拒收 violation），并做灾难性回溯
    （ReDoS）形态静态拒收——量词分组内嵌量词/交替（如 `(a+)+`、`(a|aa)+`）对短
    payload 即指数级回溯，运行面扫描在 async 节点内执行、可挂死事件循环，必须在
    发布口拦截。enabled 严格布尔：JSON 字符串/数字形态一律拒收（kill switch 单键
    热更防 `"false"` 字符串漂移——validator 拒收 + 运行面严格判定的双防线之一）。
    字段命名严禁含 token/key/secret/password（撞密钥嗅探，§2 安全条款）。

    §3 B15（v1.37）增：url_guard / injection_filter 两嵌套子节（内容侧护栏——
    与 safety.enabled 解耦，缺失=None 合法，运行面回落 defaults 种子）。
    """

    model_config = ConfigDict(extra="allow")
    enabled: bool = False
    mode: Literal["lexicon", "hybrid"] = "hybrid"
    danger_terms: list[str] = Field(default_factory=list, max_length=200)
    profanity_terms: list[str] = Field(default_factory=list, max_length=200)
    #: B13 增量一（v1.31）：隐私侵害意图词表（规则同 danger_terms）
    privacy_terms: list[str] = Field(default_factory=list, max_length=200)
    #: v1.32（B14 增量 B）：涉政敏感话题词表——**初值空数组**（内容用户 Nacos 自填，
    #: AI 零生成）；结构规则镜像 danger_terms（≤200 / 单条非空 ≤64 / 去重）
    politics_terms: list[str] = Field(default_factory=list, max_length=200)
    grey_rules: list[str] = Field(default_factory=list, max_length=50)
    classify_timeout_ms: int = Field(default=2000, ge=200, le=10000)
    reply_timeout_ms: int = Field(default=4000, ge=200, le=10000)
    reply_max_chars: int = Field(default=500, ge=100, le=1000)
    #: §3 B15（v1.37）：内容侧护栏两子节——与 safety.enabled 解耦（输入安全链关闸
    #: 不打灭 SSRF/注入护栏）；缺失=None 合法，运行面 loader 回落 defaults 种子
    url_guard: UrlGuardSpec | None = None
    injection_filter: InjectionFilterSpec | None = None

    @field_validator("enabled", mode="before")
    @classmethod
    def _check_enabled_strict_bool(cls, value: Any) -> Any:
        """enabled 严格布尔（§3 校验规则「enabled 布尔」）——判定逻辑抽公共函数
        `_strict_bool_or_raise`（B15：三个安全节共用同一判据与文案）。"""
        return _strict_bool_or_raise(value)

    @field_validator(
        "danger_terms", "profanity_terms", "privacy_terms", "politics_terms"
    )
    @classmethod
    def _check_term_entries(cls, value: list[str]) -> list[str]:
        for term in value:
            if not term:
                raise ValueError("词表条目不得为空串")
            if len(term) > 64:
                raise ValueError("词表单条 ≤64 字符")
        if len(set(value)) != len(value):
            raise ValueError("词表条目须去重（重复条目拒收）")
        return value

    @model_validator(mode="after")
    def _check_enabled_requires_terms(self) -> "SafetySpec":
        """§3 校验规则 v1.32：enabled=true 时**四词表**（danger/profanity/privacy/
        politics）至少其一非空（空词表开启=配置矛盾；politics_terms 空初值不影响
        该约束——politics 拦截面在词表自填前由 Layer 2 初判 + grey 复核兜）。"""
        if (
            self.enabled
            and not self.danger_terms
            and not self.profanity_terms
            and not self.privacy_terms
            and not self.politics_terms
        ):
            raise ValueError(
                "enabled=true 时 danger_terms/profanity_terms/privacy_terms/"
                "politics_terms 至少其一非空"
            )
        return self

    @field_validator("grey_rules")
    @classmethod
    def _check_grey_rules_compilable(cls, value: list[str]) -> list[str]:
        # B15：长度/编译/ReDoS 三判据抽公共函数（blocked_host_patterns 与
        # injection_filter.patterns 复用同一实现——消息文案保持既有形态）
        for rule in value:
            check_regex_rule(rule, label="grey_rules", max_len=120)
        return value


# ---- §3 kb（v1.43 M3 混合检索扩容，ADR-017；校验规则条款 = nacos-config.md §3「校验
# ---- 规则（v1.43 增）」）----

#: 漏斗键建议范围（契约「已批准建议范围」条款钉定）：per_query_top_k 1~100 /
#: kb_fuse_top_k 1~50；rrf_k 契约仅钉 ≥1（上界为实现侧合理范围补齐——挡垃圾热更值）；
#: max_rerank_candidates 契约钉「≥ kb_fuse_top_k」交叉约束（两口径显式区分不混用——
#: KB 份额 vs rerank 调用基数），上界为实现侧合理范围；dual_channel_timeout_s 契约
#: 未钉范围（建议值 3 = PRD 5.1「双通道合计 ≤3s」口径），1~60 为实现侧合理范围。
KB_RRF_K_MAX = 10000
KB_MAX_RERANK_CANDIDATES_MAX = 1000
KB_DUAL_CHANNEL_TIMEOUT_MAX = 60

#: chunk 参数上界（契约钉 chunk_size ≥ chunk_overlap ≥ 0——chunk_overlap=0 合法；
#: 上界为实现侧合理范围）。.env 侧 KB_ 组（core/config.py KbSettings）「overlap < size」
#: 为代码内置默认来源的更严实现面，两平面各自钉定互不牵连。
KB_CHUNK_SIZE_MAX = 100000

#: chunking 逐级切分上限（契约仅钉「各值 >0」——正整数 + 实现侧上界为合理范围授权
#: 下的补齐）：页面 ≥ 段落 ≥ 句子的量级关系由建议值（1200/400/200）承载，逐级
#: 不做跨键强制（大文档场景存在非严格递减的合法配置）。
KB_CHUNKING_MAX: dict[str, int] = {
    "page_max_chars": 100000,
    "para_max_chars": 50000,
    "sentence_max_chars": 20000,
    "table_agg_rows": 10000,
}

#: 归一化引擎枚举（合法域以下方 KbNormalizerSpec 的 Literal 内联钉定，与全文件
#: McpRole/SafetySpec.mode 同惯例；此处仅注释锚点）：pdf_engine ∈ {markitdown,
#: docling}（契约钉定，枚举外拒收——MinerU/UnstructuredIO 除名理由在案 OQ-25）；
#: ocr_engine ∈ {paddleocr, rapidocr}：**初值钉 paddleocr = PRD v1.34 用户裁决**
#: （OQ-25 二选一落地），rapidocr 保留合法 = M3+ 重场景复核线（契约 §3 示例行现写
#: rapidocr 属确认点②建议值、探针后钉一属实施序——文档侧钉正注由文档巡检线落，
#: 本文件只管枚举合法域）。

#: KbSpec 整数键清单（形态闸面——显式 null 与布尔形态拒收：「缺省请删键」，防热更
#: null 语义歧义与 pydantic lax bool→int coerce）
_KB_INT_KEYS: tuple[str, ...] = (
    "top_k_per_query",
    "rerank_timeout_s",
    "per_query_top_k",
    "rrf_k",
    "kb_fuse_top_k",
    "max_rerank_candidates",
    "dual_channel_timeout_s",
    "chunk_size",
    "chunk_overlap",
)


def _reject_non_int_forms(value: Any, keys: tuple[str, ...], label: str) -> Any:
    """整数键形态闸（mode="before" 共用）：显式 null 与布尔形态拒收——JSON null 与
    「键缺失」语义分离（缺失=合法回落代码缺省，在场 null=形态违规拒收）；pydantic
    v2 lax 会把 True coerce 成 1（实测），布尔须在发布口显式排除（严格布尔纪律的
    整数侧同款）。"""
    if isinstance(value, Mapping):
        for key in keys:
            if key in value:
                gate_value = value[key]
                if gate_value is None:
                    raise ValueError(
                        f"{label}{key}：不得为 null（缺省请删键——回落代码内置默认）"
                    )
                if isinstance(gate_value, bool):
                    raise ValueError(f"{label}{key}：须为整数（布尔形态拒收）")
    return value


class KbChunkingSpec(BaseModel):
    """§3 kb.chunking 逐级切分上限（页面→段落→句子逐级降级 + 表格行聚合阈值）。

    子节缺失=合法（None）；部分键在场合法（缺键回落代码缺省）；各值正整数 + 上界
    （KB_CHUNKING_MAX，实现侧合理范围）；显式 null 拒收。
    """

    model_config = ConfigDict(extra="allow")
    page_max_chars: int | None = Field(default=None, gt=0, le=KB_CHUNKING_MAX["page_max_chars"])
    para_max_chars: int | None = Field(default=None, gt=0, le=KB_CHUNKING_MAX["para_max_chars"])
    sentence_max_chars: int | None = Field(default=None, gt=0, le=KB_CHUNKING_MAX["sentence_max_chars"])
    table_agg_rows: int | None = Field(default=None, gt=0, le=KB_CHUNKING_MAX["table_agg_rows"])

    @model_validator(mode="before")
    @classmethod
    def _reject_null_values(cls, value: Any) -> Any:
        return _reject_non_int_forms(value, tuple(KB_CHUNKING_MAX), "kb.chunking.")


class KbNormalizerSpec(BaseModel):
    """§3 kb.normalizer 归一化组件路由（格式路由 + OCR 开关）。

    pdf_engine Literal 枚举（KB_PDF_ENGINES）；ocr_enabled 严格布尔（kill switch 防
    "true"/1 形态漂移，safety 子节同款双防线之一）；ocr_engine Literal 枚举
    （KB_OCR_ENGINES——paddleocr 初值 / rapidocr 复核线保留合法）。子节缺失=合法。
    """

    model_config = ConfigDict(extra="allow")
    pdf_engine: Literal["markitdown", "docling"] | None = None
    ocr_enabled: bool | None = None
    ocr_engine: Literal["paddleocr", "rapidocr"] | None = None

    @field_validator("ocr_enabled", mode="before")
    @classmethod
    def _check_ocr_enabled_strict_bool(cls, value: Any) -> Any:
        return _strict_bool_or_raise(value)


class KbSpec(BaseModel):
    """§3 kb 节（v1.43 M3 混合检索扩容，ADR-017）。

    13 键：现行两键保留（top_k_per_query 弃用迁移沿 type_query_map 先例——告警 +
    批次 4 切 per_query_top_k 后可删）+ 11 新键全部 Optional（**键缺失合法回落代码
    缺省并告警，extra=allow 灰度安全**——契约校验规则条款；存量 Nacos 运行值 = 旧
    2 键形态必须继续通过校验）。键在场时范围/形态越界拒收：
    - 漏斗键正整数 + 范围（契约「已批准建议范围」+ 实现侧上界，见 KB_* 常量注）；
    - max_rerank_candidates ≥ kb_fuse_top_k（契约交叉约束）；
    - es_enabled / ocr_enabled 严格布尔（kill switch 防形态漂移）；
    - pdf_engine / ocr_engine 枚举（枚举外拒收）；
    - type_weights 8 值键全覆盖（QUERY_TYPES 同源）且各 [bm25_w, vec_w] 恰两数字 ≥0；
    - chunk_size ≥ chunk_overlap ≥ 0（契约）；chunking 各值正整数 + 上界。
    键名严禁含 key/secret/token/password（撞 §2 密钥嗅探）。
    """

    model_config = ConfigDict(extra="allow")
    # 现行两键（M1/M2 口径保留不动）
    top_k_per_query: int = Field(ge=1)
    rerank_timeout_s: int = Field(ge=1)
    # 检索漏斗键组（§3 v1.43）
    per_query_top_k: int | None = Field(default=None, ge=1, le=100)
    rrf_k: int | None = Field(default=None, ge=1, le=KB_RRF_K_MAX)
    kb_fuse_top_k: int | None = Field(default=None, ge=1, le=50)
    max_rerank_candidates: int | None = Field(default=None, ge=1, le=KB_MAX_RERANK_CANDIDATES_MAX)
    dual_channel_timeout_s: int | None = Field(default=None, ge=1, le=KB_DUAL_CHANNEL_TIMEOUT_MAX)
    #: ES 通道总开关/回退门（false=pgvector 单通道降级，灰度安全缺省）
    es_enabled: bool | None = None
    # chunk 参数（热更面收敛本节，.env KB_ 值降为代码内置默认来源）
    chunk_size: int | None = Field(default=None, ge=1, le=KB_CHUNK_SIZE_MAX)
    chunk_overlap: int | None = Field(default=None, ge=0, le=KB_CHUNK_SIZE_MAX)
    # 题型权重映射 / chunking 上限 / 归一化路由
    type_weights: dict[str, list[float]] | None = None
    chunking: KbChunkingSpec | None = None
    normalizer: KbNormalizerSpec | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_non_int_forms_gate(cls, value: Any) -> Any:
        return _reject_non_int_forms(value, _KB_INT_KEYS, "kb.")

    @field_validator("es_enabled", mode="before")
    @classmethod
    def _check_es_enabled_strict_bool(cls, value: Any) -> Any:
        return _strict_bool_or_raise(value)

    @field_validator("type_weights", mode="before")
    @classmethod
    def _check_type_weights(cls, value: Any) -> Any:
        """题型权重映射（§3 v1.43：8 值键全覆盖 + 各 [bm25_w, vec_w] 恰两数字 ≥0）。

        键域 = QUERY_TYPES 8 值枚举同源（type_query_map 覆盖校验同一常量——单一
        事实源）；缺任一键/未知键/非两元/含非数字（布尔显式排除——bool 是 int 子类）/
        负数 → 拒收。整键缺失合法（消费面按兜底行兼容，灰度安全）。
        """
        if value is None:
            return value
        if not isinstance(value, dict):
            raise ValueError(
                "kb.type_weights 须为 JSON 对象（query_type → [bm25_w, vec_w]）"
            )
        keys = set(value.keys())
        missing = [t for t in QUERY_TYPES if t not in keys]
        if missing:
            raise ValueError(f"kb.type_weights 须覆盖 8 值枚举，缺 {missing}")
        unknown = sorted(keys - set(QUERY_TYPES))
        if unknown:
            raise ValueError(f"kb.type_weights 键域为 8 值枚举，未知键 {unknown}")
        for weight_key, pair in value.items():
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ValueError(
                    f"kb.type_weights.{weight_key}：须恰为两个数字 [bm25_w, vec_w]"
                )
            for weight in pair:
                if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                    raise ValueError(
                        f"kb.type_weights.{weight_key}：权重须为数字（字符串/null/布尔形态拒收）"
                    )
                if weight < 0:
                    raise ValueError(f"kb.type_weights.{weight_key}：权重须 ≥ 0")
        return value

    @model_validator(mode="after")
    def _check_kb_cross_keys(self) -> "KbSpec":
        """契约交叉约束（两键均在场才判——缺失侧回落代码缺省，无交叉可言）。"""
        if (
            self.max_rerank_candidates is not None
            and self.kb_fuse_top_k is not None
            and self.max_rerank_candidates < self.kb_fuse_top_k
        ):
            raise ValueError(
                "kb 要求 max_rerank_candidates ≥ kb_fuse_top_k"
                "（rerank 调用基数上界不得小于 KB 份额上界）"
            )
        if (
            self.chunk_size is not None
            and self.chunk_overlap is not None
            and self.chunk_overlap > self.chunk_size
        ):
            raise ValueError("kb 要求 chunk_size ≥ chunk_overlap ≥ 0")
        return self


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
    """Agent Spec：fast/deep 必带；turn/followups/safety/routing/memory/kb/history 缺失时
    组装层按契约回落内置默认并告警（§3 各节注），校验层不拒收缺节（safety 节缺失=
    合法 fail-open 语义，B13）。"""

    model_config = ConfigDict(extra="allow")
    fast: FastSpec
    deep: DeepSpec
    turn: TurnSpec | None = None
    routing: RoutingSpec | None = None
    followups: FollowupsSpec | None = None
    safety: SafetySpec | None = None  # §3 B13：节缺失=合法（fail-open，安全检查不挡检索）
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
    """§5：模型映射（v1.33 +`decision`）。

    `decision` 为**映射键唯一允许空串者**——缺省空串 = kill switch 关闭（decision
    不启用、意图识别全量走 light 路径 = 现状行为，存量轮零变化沿 search_strategy
    v1.20 灰度先例）；非空 = litellm 别名，**不做别名白名单校验**（别名注册面在
    litellm config.yaml 网关静态层、Nacos 不感知——未知别名在调用时网关报错 →
    decision 失败兜底回 light，灰度安全）。其余五键 min_length=1 惯例不变。
    """

    model_config = ConfigDict(extra="allow")
    chat: ChatMapping
    vision: str = Field(min_length=1)
    embedding: str = Field(min_length=1)
    rerank: str = Field(min_length=1)
    light: str = Field(min_length=1)
    memory: str = Field(min_length=1)
    decision: str = ""


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

