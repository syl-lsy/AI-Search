"""safety.config：Agent Spec `safety` 节 → SafetyConfig（B13 输入安全，nacos-config.md §3 v1.30）。

fail-open 语义（契约口径：安全检查不挡检索）：
- spec 缺 safety 节 / 节非对象 / mode 非枚举 → 返回 None + AgentLog 告警一次——调用方
  把 None 视为「本轮安全检查关闭」直通（validator 侧 safety 节缺失=合法不拒收，本层
  是图运行面的第二道防线，同 guards.py 定位）；
- enabled=false → None 且不告警（回退门合法形态：单键热更即全链直通，§1 v1.30）；
- grey_rules 单条编译失败跳过该条 + 汇总告警一次，其余保留（不整体失败）；
- 词表混入非字符串条目丢弃 + 汇总告警一次；timeout/上限非法值静默回落契约缺省
  （guard 不阻塞主链路，沿 guards.llm_timeout_s 先例）。

单位换算：spec 进 `_ms`（classify_timeout_ms / reply_timeout_ms，validator 契约键），
SafetyConfig 出 `_s`（秒）——图运行面统一秒制（guards 分级超时口径一致）。
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.core.logger import AgentLog

__all__ = [
    "SAFETY_MODES",
    "SafetyConfig",
    "load_safety_config",
]

#: mode 枚举（validator.SafetySpec Literal 同源：lexicon=仅词表 / hybrid=词表+grey LLM 复核）
SAFETY_MODES: tuple[str, ...] = ("lexicon", "hybrid")

#: 契约缺省（nacos-config.md §3 v1.30 / defaults.py SPEC_CONFIG safety 节）
_DEFAULT_CLASSIFY_TIMEOUT_S = 2.0
_DEFAULT_REPLY_TIMEOUT_S = 4.0
_DEFAULT_REPLY_MAX_CHARS = 500


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


def _compile_grey_rules(value: Any) -> tuple[re.Pattern[str], ...]:
    """grey 正则逐条编译：单条失败跳过 + 汇总告警一次（不整体失败）。"""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        _warn("safety.grey_rules 非列表结构，按空规则集处理（fail-open）")
        return ()
    patterns: list[re.Pattern[str]] = []
    failed = 0
    for rule in value:
        if not isinstance(rule, str) or not rule:
            failed += 1
            continue
        try:
            patterns.append(re.compile(rule))
        except re.error:
            failed += 1
    if failed:
        _warn(f"safety.grey_rules 有 {failed} 条正则编译失败被跳过（fail-open 不整体失败）")
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
        grey_rules=_compile_grey_rules(section.get("grey_rules")),
        classify_timeout_s=_timeout_s(
            section.get("classify_timeout_ms"), _DEFAULT_CLASSIFY_TIMEOUT_S
        ),
        reply_timeout_s=_timeout_s(
            section.get("reply_timeout_ms"), _DEFAULT_REPLY_TIMEOUT_S
        ),
        reply_max_chars=_max_chars(section.get("reply_max_chars")),
    )
