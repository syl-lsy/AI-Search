"""safety.config：Agent Spec `safety` 节 → SafetyConfig（B13 输入安全，nacos-config.md §3 v1.30）。

fail-open 语义（契约口径：安全检查不挡检索）：
- spec 缺 safety 节 / 节非对象 / mode 非枚举 → 返回 None + AgentLog 告警一次——调用方
  把 None 视为「本轮安全检查关闭」直通（validator 侧 safety 节缺失=合法不拒收，本层
  是图运行面的第二道防线，同 guards.py 定位）；
- enabled=false → None 且不告警（回退门合法形态：单键热更即全链直通，§1 v1.30）；
- grey_rules 单条编译失败跳过该条 + 汇总告警一次，其余保留（不整体失败）；
- 词表混入非字符串条目丢弃 + 汇总告警一次；timeout/上限非法值静默回落契约缺省
  （guard 不阻塞主链路，沿 guards.llm_timeout_s 先例）。

B15 内容侧护栏（v1.37）：`load_content_guard_config` 装载 safety.url_guard /
injection_filter 两子节 → ContentGuardConfig——**与 safety.enabled 解耦**（输入
安全链关闸不打灭 SSRF/注入护栏）；缺子节回落 defaults.py SPEC_CONFIG 种子（=部署
即启用，单一事实源）；子节畸形 → None + 告警一次（单机制 fail-open，互不牵连）；
enabled 非严格 true → 关该机制且不告警（kill switch 纪律同上）。

单位换算：spec 进 `_ms`（classify_timeout_ms / reply_timeout_ms，validator 契约键），
SafetyConfig 出 `_s`（秒）——图运行面统一秒制（guards 分级超时口径一致）。
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.config_center.defaults import SPEC_CONFIG
from app.core.logger import AgentLog

__all__ = [
    "SAFETY_MODES",
    "ContentGuardConfig",
    "InjectionFilterConfig",
    "SafetyConfig",
    "UrlGuardConfig",
    "load_content_guard_config",
    "load_safety_config",
]

#: mode 枚举（validator.SafetySpec Literal 同源：lexicon=仅词表 / hybrid=词表+grey LLM 复核）
SAFETY_MODES: tuple[str, ...] = ("lexicon", "hybrid")

#: 契约缺省（nacos-config.md §3 v1.30 / defaults.py SPEC_CONFIG safety 节）
_DEFAULT_CLASSIFY_TIMEOUT_S = 2.0
_DEFAULT_REPLY_TIMEOUT_S = 4.0
_DEFAULT_REPLY_MAX_CHARS = 500

#: B15 契约缺省（nacos-config.md §3 v1.37 / defaults.py SPEC_CONFIG safety 两子节；
#: validator 同口径范围闸 64~8192 / 1000~20000——越界静默回落种子值）
_URL_GUARD_MAX_URL_LENGTH_DEFAULT = 2048
_URL_GUARD_MAX_URL_LENGTH_BOUNDS = (64, 8192)
_INJECTION_MAX_SCAN_CHARS_DEFAULT = 16000
_INJECTION_MAX_SCAN_CHARS_BOUNDS = (1000, 20000)

#: on_hit 动作枚举（validator.InjectionFilterSpec Literal 同源）；非法值回落 flag
#: （观察姿态=更少拦截面，fail-open 方向）
_INJECTION_ON_HIT_ACTIONS: tuple[str, ...] = ("flag", "drop")
_INJECTION_ON_HIT_DEFAULT = "flag"


@dataclass(frozen=True)
class SafetyConfig:
    """轮内冻结的输入安全配置（集合用 tuple——冻结语义 + 可哈希传递）。

    grey_rules 为编译好的 `re.Pattern` 列表（原样编译不加 flags——种子规则自带
    大小写处理如 `[Bb]`，运行层不做二次改写）。
    """

    enabled: bool
    mode: str
    danger_terms: tuple[str, ...]
    profanity_terms: tuple[str, ...]
    #: B13 增量一（nacos-config.md §3 v1.31）：隐私侵害意图词表——code 层直拦后一律
    #: reject（隐私侵害是意图不是可脱敏内容，与 pii 的 sanitize 语义正交）
    privacy_terms: tuple[str, ...]
    #: B14 增量 B（v1.32）：涉政敏感话题词表——初值空数组（内容用户 Nacos 自填），
    #: 命中即 reject 固定话术直答（全程不搜索不综合），与 privacy 同为 reject-only
    politics_terms: tuple[str, ...]
    grey_rules: tuple[re.Pattern[str], ...]
    classify_timeout_s: float
    reply_timeout_s: float
    reply_max_chars: int


def _warn(message: str) -> None:
    """safety 域告警（AgentLog.graph="safety"，告警一次 = 每个失败面恰好一条）。"""
    AgentLog(message=message, graph="safety").warning()


def _coerce_terms(value: Any, name: str) -> tuple[str, ...]:
    """词表收集：保留字符串条目，非字符串丢弃并汇总告警一次。"""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        _warn(f"safety.{name} 非列表结构，按空词表处理（fail-open）")
        return ()
    kept = tuple(item for item in value if isinstance(item, str) and item)
    dropped = len(value) - len(kept)
    if dropped:
        _warn(f"safety.{name} 有 {dropped} 条非字符串条目被丢弃（fail-open 不整体失败）")
    return kept


def _compile_patterns(
    value: Any, name: str, flags: int = 0
) -> tuple[re.Pattern[str], ...]:
    """正则列表逐条编译：单条失败跳过 + 汇总告警一次（不整体失败）。

    B15 抽通用化（原 _compile_grey_rules）：grey_rules 与 url_guard.blocked_host_
    patterns 共用同一跳过-告警口径；规则条目跳过对 deny 侧只可能削弱——validator
    已在发布口拒收坏正则，此处是运行面第二道防线（快照/L4 残余形态）。
    `flags`：blocked_host_patterns 传 re.IGNORECASE（复核残留修正——host 归一化恒
    小写，运营手写大写 deny 正则不得因大小写失灵；deny 侧匹配面只加宽不收窄）；
    grey_rules 维持缺省 0（输入安全种子自带 [Bb] 大小写处理，沿袭不动）。
    """
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        _warn(f"safety.{name} 非列表结构，按空规则集处理（fail-open）")
        return ()
    patterns: list[re.Pattern[str]] = []
    failed = 0
    for rule in value:
        if not isinstance(rule, str) or not rule:
            failed += 1
            continue
        try:
            patterns.append(re.compile(rule, flags))
        except re.error:
            failed += 1
    if failed:
        _warn(f"safety.{name} 有 {failed} 条正则编译失败被跳过（fail-open 不整体失败）")
    return tuple(patterns)


def _timeout_s(value: Any, default_s: float) -> float:
    """ms → s 换算：非数/≤0 静默回落契约缺省（guard 不阻塞主链路）。"""
    try:
        ms = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default_s
    return ms / 1000.0 if ms > 0 else default_s


def _max_chars(value: Any) -> int:
    """reply_max_chars：非数/≤0 回落契约缺省。"""
    try:
        chars = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _DEFAULT_REPLY_MAX_CHARS
    return chars if chars > 0 else _DEFAULT_REPLY_MAX_CHARS


def load_safety_config(spec: Mapping[str, Any] | None) -> SafetyConfig | None:
    """spec.safety 节 → SafetyConfig；不可用返回 None（fail-open，调用方直通）。

    返回 None 的三种形态（前两种告警一次、enabled 门静默）：
    ① spec 非 Mapping / safety 节缺失 / 节非对象；
    ② mode 非 lexicon|hybrid（枚举漂移，validator 拒收之外的第二道防线）；
    ③ enabled 非严格 true（false/缺失/字符串 "false"/数字等一律关闸直通，不告警
    ——回退门合法形态或形态漂移，热更开关不该刷 WARNING 噪音）。
    """
    section = spec.get("safety") if isinstance(spec, Mapping) else None
    if not isinstance(section, Mapping):
        _warn("Agent Spec 缺 safety 节（或结构非法），输入安全检查 fail-open 直通")
        return None
    if section.get("enabled") is not True:
        # 回退门：safety.enabled 单键热更即全链直通（§1 v1.30）。严格布尔判定：
        # 仅 JSON true 开闸——字符串 "false"/"0"、数字 1 等形态漂移一律按关闸直通
        # （bool("false")=True 的宽松判定会让 kill switch 反向失效，validator
        # 严格拒收之外的第二道防线，fail-open 方向）。
        return None
    mode = section.get("mode", "hybrid")
    if mode not in SAFETY_MODES:
        _warn(f"safety.mode 非法：{mode!r}，输入安全检查 fail-open 直通")
        return None
    return SafetyConfig(
        enabled=True,
        mode=str(mode),
        danger_terms=_coerce_terms(section.get("danger_terms"), "danger_terms"),
        profanity_terms=_coerce_terms(section.get("profanity_terms"), "profanity_terms"),
        privacy_terms=_coerce_terms(section.get("privacy_terms"), "privacy_terms"),
        politics_terms=_coerce_terms(section.get("politics_terms"), "politics_terms"),
        grey_rules=_compile_patterns(section.get("grey_rules"), "grey_rules"),
        classify_timeout_s=_timeout_s(
            section.get("classify_timeout_ms"), _DEFAULT_CLASSIFY_TIMEOUT_S
        ),
        reply_timeout_s=_timeout_s(
            section.get("reply_timeout_ms"), _DEFAULT_REPLY_TIMEOUT_S
        ),
        reply_max_chars=_max_chars(section.get("reply_max_chars")),
    )


# ---- B15 内容侧护栏（v1.37：url_guard / injection_filter——与 safety.enabled 解耦） ----


@dataclass(frozen=True)
class UrlGuardConfig:
    """SSRF 防护 URL 校验运行配置（blocked_host_patterns 已编译——原样编译不加
    flags，deny 侧运营正则自带锚定；集合用 tuple——冻结语义与 SafetyConfig 同口径）。

    代码硬底线（scheme/私网/元数据/短主机名判定）在 url_guard.py——本配置只承载
    运营加强面（黑名单）与长度上限；enabled 恒 True（非 true 已被 loader 关为 None）。
    """

    enabled: bool
    blocked_hosts: tuple[str, ...]
    blocked_host_patterns: tuple[re.Pattern[str], ...]
    max_url_length: int


@dataclass(frozen=True)
class InjectionFilterConfig:
    """注入特征过滤运行配置：patterns 为 (rule_id, 编译正则) 对——命中告警只报
    rule_id（零 payload 原文、零规则原文）；匹配集 = 内置种子 ∪ Nacos patterns
    （loader 层并集、按正则源串去重——**Nacos 削不掉种子**）。on_hit ∈ flag|drop
    （flag=命中仅告警保留，drop=丢弃内容），语义由接线层消费、本配置只承载。
    """

    enabled: bool
    patterns: tuple[tuple[str, re.Pattern[str]], ...]
    on_hit: str
    max_scan_chars: int


@dataclass(frozen=True)
class ContentGuardConfig:
    """B15 内容侧护栏总配置：两机制各自独立装载（None=该机制关/不可用，互不牵连；
    与 SafetyConfig 输入安全链完全解耦——safety.enabled=false 不影响两机制在場）。"""

    url_guard: UrlGuardConfig | None
    injection_filter: InjectionFilterConfig | None


def _seed_section(name: str) -> dict[str, Any]:
    """defaults.py SPEC_CONFIG safety 子节种子（运行时回落源——种子单一事实源在
    defaults，缺子节即回落此处 = 部署即启用）。"""
    safety = SPEC_CONFIG.get("safety")
    section = safety.get(name) if isinstance(safety, Mapping) else None
    return dict(section) if isinstance(section, Mapping) else {}


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    """范围闸：非数/越界静默回落契约缺省（guard 不阻塞主链路，同 timeout 先例；
    布尔按 int(True)=1 处理——越界自然回落）。"""
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if low <= number <= high else default


def _compile_rule_patterns(
    value: Any, name: str, id_prefix: str
) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """运营正则逐条编译为 (rule_id, Pattern) 对（injection_filter 专用——命中告警
    只报 rule_id）：非字符串/编译失败跳过该条 + 汇总告警一次（不整体失败，沿
    grey_rules 口径）；rule_id = `{id_prefix}[{索引}]`（seed[i]=内置种子 /
    nacos[i]=Nacos 扩充，索引随源表位置稳定）。"""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        _warn(f"safety.{name} 非列表结构，按空规则集处理（fail-open）")
        return ()
    compiled: list[tuple[str, re.Pattern[str]]] = []
    failed = 0
    for index, rule in enumerate(value):
        if not isinstance(rule, str) or not rule:
            failed += 1
            continue
        try:
            # IGNORECASE（评审 P1）：文本侧归一化含 casefold（恒小写），种子与 Nacos
            # patterns 不分大小写命中——大写形态（历史种子/运营手写）不构成匹配死区，
            # 匹配面只加宽不收窄（检测器方向安全；grey_rules 不吃本 flag——输入安全
            # 种子自带 [Bb] 大小写处理，沿袭不动）
            compiled.append((f"{id_prefix}[{index}]", re.compile(rule, re.IGNORECASE)))
        except re.error:
            failed += 1
    if failed:
        _warn(f"safety.{name} 有 {failed} 条正则编译失败被跳过（fail-open 不整体失败）")
    return tuple(compiled)


def _load_url_guard(node: Any) -> UrlGuardConfig | None:
    """safety.url_guard 子节 → UrlGuardConfig；不可用返回 None（单机制 fail-open）。

    None 三形态（中间告警一次、enabled 门静默）：
    ① 子节缺失 → 回落 defaults 种子（=部署即启用，**不告警**——旧 payload 正常形态）；
    ② 子节非对象（结构漂移）→ None + 告警一次；
    ③ enabled 非严格 true（false/缺失/字符串/数字）→ None 且不告警（kill switch
    合法直通形态，bool("false")=True 宽松判定防漂移同 load_safety_config）。
    """
    if node is None:
        node = _seed_section("url_guard")
    if not isinstance(node, Mapping):
        _warn("Agent Spec safety.url_guard 子节结构非法，URL 护栏该机制 fail-open 直通")
        return None
    if node.get("enabled") is not True:
        return None
    seed_max = _bounded_int(
        _seed_section("url_guard").get("max_url_length"),
        _URL_GUARD_MAX_URL_LENGTH_DEFAULT,
        *_URL_GUARD_MAX_URL_LENGTH_BOUNDS,
    )
    return UrlGuardConfig(
        enabled=True,
        blocked_hosts=_coerce_terms(node.get("blocked_hosts"), "url_guard.blocked_hosts"),
        blocked_host_patterns=_compile_patterns(
            node.get("blocked_host_patterns"),
            "url_guard.blocked_host_patterns",
            # IGNORECASE（评审复核残留）：host 归一化恒小写，deny 侧运营正则不分
            # 大小写命中——黑名单不因手写大写而静默失效
            flags=re.IGNORECASE,
        ),
        max_url_length=_bounded_int(
            node.get("max_url_length"), seed_max, *_URL_GUARD_MAX_URL_LENGTH_BOUNDS
        ),
    )


def _load_injection_filter(node: Any) -> InjectionFilterConfig | None:
    """safety.injection_filter 子节 → InjectionFilterConfig；不可用返回 None
    （单机制 fail-open，三形态与 _load_url_guard 同判据）。

    匹配集 = 内置种子（defaults）∪ Nacos patterns——按正则源串去重（Nacos 重复
    种子条目不重复计）；on_hit/max_scan_chars 非法静默回落种子值。
    """
    if node is None:
        node = _seed_section("injection_filter")
    if not isinstance(node, Mapping):
        _warn(
            "Agent Spec safety.injection_filter 子节结构非法，注入过滤该机制 fail-open 直通"
        )
        return None
    if node.get("enabled") is not True:
        return None
    seeds = _compile_rule_patterns(
        _seed_section("injection_filter").get("patterns"),
        "injection_filter.seeds",
        "seed",
    )
    extras = _compile_rule_patterns(
        node.get("patterns"), "injection_filter.patterns", "nacos"
    )
    seen = {pattern.pattern for _, pattern in seeds}
    merged = seeds + tuple(
        (rule_id, pattern) for rule_id, pattern in extras if pattern.pattern not in seen
    )
    on_hit = node.get("on_hit", _INJECTION_ON_HIT_DEFAULT)
    seed_scan_chars = _bounded_int(
        _seed_section("injection_filter").get("max_scan_chars"),
        _INJECTION_MAX_SCAN_CHARS_DEFAULT,
        *_INJECTION_MAX_SCAN_CHARS_BOUNDS,
    )
    return InjectionFilterConfig(
        enabled=True,
        patterns=merged,
        on_hit=(
            on_hit if on_hit in _INJECTION_ON_HIT_ACTIONS else _INJECTION_ON_HIT_DEFAULT
        ),
        max_scan_chars=_bounded_int(
            node.get("max_scan_chars"), seed_scan_chars, *_INJECTION_MAX_SCAN_CHARS_BOUNDS
        ),
    )


def load_content_guard_config(spec: Mapping[str, Any] | None) -> ContentGuardConfig:
    """spec.safety 两内容子节 → ContentGuardConfig（B15 内容侧护栏装载入口）。

    **与 safety.enabled 解耦**：输入安全链关闸（enabled=false）不打灭内容护栏；
    缺子节回落 defaults 种子（=部署即启用）；单机制 fail-open——子节畸形该机制
    None + 告警一次（另一机制不受牵连）；enabled 非严格 true → 该机制 None 且
    不告警（kill switch 纪律镜像 load_safety_config）。
    """
    section = spec.get("safety") if isinstance(spec, Mapping) else None
    url_node = section.get("url_guard") if isinstance(section, Mapping) else None
    injection_node = (
        section.get("injection_filter") if isinstance(section, Mapping) else None
    )
    return ContentGuardConfig(
        url_guard=_load_url_guard(url_node),
        injection_filter=_load_injection_filter(injection_node),
    )
