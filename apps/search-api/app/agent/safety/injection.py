"""safety.injection：网页内容提示注入特征过滤（B15 内容侧安全护栏——纯函数零 IO）。

背景与定位（plan-b15 §2）：现有 safety 包只作用在用户输入侧（B13/B14）；搜索摘要、
deep 读页正文、子代理报告回传协调器均无注入扫描——外部内容经 `<external_content>`
包裹是隔离硬边界，本模块是包裹之外的**第二道内容侧防线**（命中处置 on_hit=flag
观察先行：仅告警保留，跑出误判基线后 Nacos 单键热切 drop——PRD REQ-066 裁决）。

匹配口径（nacos-config.md §3 v1.37）：
- 匹配集 = 内置种子 ∪ Nacos patterns（loader 层并集去重，config.patterns 已是
  并集——**Nacos 削不掉种子**，运营 patterns 只能加强不能削弱，与 url_guard 同一
  「代码硬底线」原则）；种子 ~12 条高精度：结构标记（`<|system|>` / `<|im_start|>` /
  `[inst]`（小写源串——匹配经 IGNORECASE 编译，大写形态同样命中〔评审 P1〕）/
  原始 `</external_content>` 闭合标记）+ EN 语义（ignore/disregard
  previous instructions、reveal system prompt、you are now）+ ZH 语义（忽略以上
  指令/规则、无视、忘记、输出系统提示词、你现在扮演）；
- **归一化匹配**：NFKC（全角→ASCII）→ 剥离 Unicode Cf 类字符（zero-width
  U+200B~200F / format U+202A~202E / U+FEFF / 软连字符 U+00AD）→ casefold——
  大小写/空白/零宽/全角绕过全部失效；正则按归一化文本 search（种子自带 \\b 与
  \\s+ 形态）；
- **截断**：归一化文本超 max_scan_chars 只扫前段 + truncated=True（长文头部注入
  面优先；缺省 16000 与 deep 读窗 head 4000 token（ASCII 4:1）预算对齐——交付窗
  全量可扫〔评审 P2：原 8000 对英文大页系统性只扫前半〕；超限残余（如显式 limit
  放大窗）truncated 标记可观测，随 R21 观察面留档）；
- **v1 形态**：无 redact、无 span 映射——Scan 只携带 hits（rule_id 列表，配置序）
  与 truncated 标记，**零 payload 原文回显**（告警纪律：只记 rule_id，接线层消费）；
- **fail-open**：扫描面异常返回零命中 Scan（扫描器故障不挡内容链路——「安全检查
  不挡检索」同构语义；坏正则已在 loader 层跳过 + 汇总告警）。

纯函数零 IO（同 lexicon.py 定位）：LTM 接线点（fast 搜索摘要 / deep 读页 / 子代理
报告）在后续接线批落地，本模块只提供判定原语。
"""

import unicodedata
from dataclasses import dataclass
from typing import Any

from app.agent.safety.config import InjectionFilterConfig

__all__ = [
    "InjectionScan",
    "scan_injection",
]


@dataclass(frozen=True)
class InjectionScan:
    """注入特征扫描结果：hits 为命中的 rule_id 列表（配置序——seed[i] 在前
    nacos[i] 在后；零 payload 原文）；truncated=归一化文本超 max_scan_chars 已
    截断（只扫前段）。"""

    hits: tuple[str, ...] = ()
    truncated: bool = False


def _normalize(text: str) -> str:
    """归一化：NFKC（全角→ASCII）→ 剥离 Cf 类字符（零宽/format/软连字符——多形态
    插入绕过失效）→ casefold（大小写绕过失效；对中文恒等）。"""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Cf")
    return normalized.casefold()


def scan_injection(text: Any, config: InjectionFilterConfig) -> InjectionScan:
    """对 text 做注入特征扫描（纯函数，零 IO）。

    非字符串输入安全收口（None→空串、其余 str() 化，同 scan_lexicon 口径）；
    扫描面异常 fail-open 返回零命中 Scan（不抛——调用点在 async 节点内，扫描器
    故障不得中断内容链路）。on_hit 语义（flag 保留 / drop 丢弃）由接线层消费，
    本模块只判定。
    """
    try:
        return _scan(text, config)
    except Exception:  # noqa: BLE001 —— 扫描器故障不挡内容链路（fail-open 直通）
        return InjectionScan()


def _scan(text: Any, config: InjectionFilterConfig) -> InjectionScan:
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    normalized = _normalize(text)
    truncated = len(normalized) > config.max_scan_chars
    if truncated:
        normalized = normalized[: config.max_scan_chars]
    # patterns 为 loader 层 IGNORECASE 编译（评审 P1）——文本已 casefold，大写形态
    # 种子/运营 patterns 同样命中（匹配面只加宽不收窄，检测器方向安全）
    hits = tuple(
        rule_id for rule_id, pattern in config.patterns if pattern.search(normalized)
    )
    return InjectionScan(hits=hits, truncated=truncated)
