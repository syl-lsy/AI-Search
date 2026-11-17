"""safety.sanitizer：纯函数 PII 检测与脱敏（B13 sanitize 通道，nacos-config.md §1 v1.30）。

检测四形态（正则边界断言防子串误伤 + 校验算法压误报）：
- **mobile**：CN 手机号 `1[3-9]` 开头 11 位，`(?<!\\d)…(?!\\d)` 防止长数字串内截匹配；
- **id_card**：18 位身份证（含末位 X/x），过 GB11643（ISO 7064 MOD 11-2）校验位——
  订单号等 18 位数字串不误报；
- **bank_card**：13-19 位数字串，过 Luhn 校验——毫秒时间戳/订单号不误报（Luhn 随机
  通过率 ~1/10，边界断言排除 >19 位）；
- **email**：常见邮箱，要求 TLD ≥2（`foo@bar` 不命中），ASCII 边界断言防前缀粘连。

数字三形态另做**归一化第二遍**（分隔符折叠 + 全角数字转 ASCII）——分字符防和谐/
防爬的抄写变体（「138-0013-8000」「１３８００１３８０００」「6222 0212 …」）是
query 出域（→ 第三方搜索源）的主通道，命中后按位置映射回原文区间替换；校验算法
照跑（归一化串纯数字），负例画像不变（日期/版本号折叠后位数不足或校验不过）。

**负例（误报面）与正例同等对待**（安全内核纪律）：自指查询（「手机号正则怎么写」
「身份证校验算法是什么」——无数字串，天然不命中）、短数字串（日期/年份/尾号）、
校验不过的长数字串均有单测锁定。

重叠裁决：`id_card > bank_card > mobile > email`——18 位纯数字同时过 GB11643 与
Luhn 时按 id_card 归类（单条不重复）。mobile/email 与数字长串因边界断言天然不相交。

脱敏占位词：`[手机号] / [证件号] / [卡号] / [邮箱]`（语义占位，sanitize 后覆写
图内 state.query 照常检索，PII 不产生 error 事件——契约 §1 v1.30）。
"""

import re
from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "PiiFinding",
    "PiiKind",
    "detect_pii",
    "sanitize",
]

PiiKind = Literal["mobile", "id_card", "bank_card", "email"]


@dataclass(frozen=True)
class PiiFinding:
    """一处 PII 命中（kind + 原文半开区间 [start, end)，可比较/可哈希）。"""

    kind: PiiKind
    start: int
    end: int


#: CN 手机号：1[3-9] 开头 11 位；前后非数字（防订单号/银行卡内截匹配）
_MOBILE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

#: 18 位身份证：17 数字 + 校验位（数字或 X/x）；尾部断言含 X 防止「X 后黏数字」误配
_ID_CARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?![\dXx])")

#: 银行卡：13-19 位连续数字；Luhn 校验在检测器侧做（正则只管形态）
_BANK_CARD_RE = re.compile(r"(?<!\d)\d{13,19}(?!\d)")

#: 常见邮箱：TLD ≥2；前后 ASCII 词字符断言（防 `abcuser@example.com` 粘连误配）
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*"
    r"\.[A-Za-z]{2,}(?![A-Za-z0-9.-])"
)

#: GB11643（ISO 7064 MOD 11-2）加权因子（i=0..16）
_ID_WEIGHTS: tuple[int, ...] = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)

#: 归一化映射表（数字串抄写变体检测——P11 防出域闸门）：全角数字 → ASCII、
#: 全角连字符/全角句点/各类 dash/间隔点 → ASCII 形态（供折叠判定）
_NORMALIZE_TABLE: dict[int, str] = {
    **{0xFF10 + i: str(i) for i in range(10)},
    0xFF0D: "-",
    0x2013: "-",
    0x2014: "-",
    0x2015: "-",
    0xFF0E: ".",
    0x00B7: ".",
    0x30FB: ".",
}

#: 折叠分隔符集（归一化后仍属分隔形态的字符：数字串内空格/连字符/点——
#: 分字符防和谐/防爬的手机号「138-0013-8000」与全角变体的识别基础）
_FOLD_CHARS = frozenset(" \t\r\n\v\f-.")


def _normalized_digit_view(text: str) -> tuple[str, list[int]]:
    """构造数字检测用归一化视图：全角数字转 ASCII、折叠分隔符；返回视图串与
    `视图位置 → 原文位置` 映射（命中后映射回原文半开区间）。"""
    chars: list[str] = []
    index_map: list[int] = []
    for pos, ch in enumerate(text):
        mapped = _NORMALIZE_TABLE.get(ord(ch), ch)
        if mapped in _FOLD_CHARS:
            continue
        chars.append(mapped)
        index_map.append(pos)
    return "".join(chars), index_map


#: 脱敏语义占位词（kind → 占位）
_PLACEHOLDERS: dict[str, str] = {
    "mobile": "[手机号]",
    "id_card": "[证件号]",
    "bank_card": "[卡号]",
    "email": "[邮箱]",
}

#: 检测器优先级（小者先保留；仅 id_card 与 bank_card 可能重叠——18 位纯数字双合法）
_PRIORITY_ORDER: tuple[PiiKind, ...] = ("id_card", "bank_card", "mobile", "email")


def _id_card_valid(value: str) -> bool:
    """GB11643 校验位：加权求和 → (12 − sum mod 11) mod 11 → 校验字符（10 → X）。"""
    body, check = value[:17], value[17].upper()
    total = sum(int(d) * w for d, w in zip(body, _ID_WEIGHTS))
    expected = (12 - total % 11) % 11
    return check == ("X" if expected == 10 else str(expected))


def _luhn_ok(value: str) -> bool:
    """Luhn 校验（银行卡压误报关键：时间戳/订单号随机通过率 ~1/10）。"""
    total = 0
    for i, ch in enumerate(reversed(value)):
        digit = int(ch)
        if i % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def detect_pii(text: str) -> list[PiiFinding]:
    """检测文本中的 PII：返回按 start 升序、互不重叠的 PiiFinding 列表。

    两遍扫描：第一遍原文（email 等分隔即断形态 + 连续数字形态）；第二遍归一化视图
    （全角数字转 ASCII + 折叠分隔符，仅三类数字 kind——手机号/证件号/卡号的分字符
    抄写变体「138-0013-8000」「１３８…」），命中后按位置映射回原文区间。两遍候选
    合并走同一重叠裁决（优先级高者保留）。校验算法（Luhn/GB11643）在归一化串上
    运行（纯数字）。

    纯函数（零 IO 零状态）；非字符串/空文本返回 []。校验失败的候选（订单号/
    时间戳/日期串）不产出 finding——负例纪律见模块 docstring。
    """
    if not isinstance(text, str) or not text:
        return []
    detectors: dict[str, tuple[re.Pattern[str], Any]] = {
        "id_card": (_ID_CARD_RE, _id_card_valid),
        "bank_card": (_BANK_CARD_RE, _luhn_ok),
        "mobile": (_MOBILE_RE, None),
        "email": (_EMAIL_RE, None),
    }
    candidates: list[tuple[int, PiiFinding]] = []
    for kind in _PRIORITY_ORDER:
        pattern, validator = detectors[kind]
        for match in pattern.finditer(text):
            if validator is not None and not validator(match.group(0)):
                continue
            candidates.append(
                (_PRIORITY_ORDER.index(kind), PiiFinding(kind, match.start(), match.end()))  # type: ignore[arg-type]
            )
    # 第二遍：归一化数字视图（分隔符/全角变体；email 不参与——分隔即非邮箱形态）
    normalized, index_map = _normalized_digit_view(text)
    if normalized != text:
        for kind in ("id_card", "bank_card", "mobile"):
            pattern, validator = detectors[kind]
            for match in pattern.finditer(normalized):
                if validator is not None and not validator(match.group(0)):
                    continue
                start = index_map[match.start()]
                end = index_map[match.end() - 1] + 1  # 视图末位 → 原文闭区间右端
                candidates.append(
                    (_PRIORITY_ORDER.index(kind), PiiFinding(kind, start, end))  # type: ignore[arg-type]
                )
    candidates.sort(key=lambda item: (item[0], item[1].start, item[1].end))
    kept: list[PiiFinding] = []
    for _, finding in candidates:
        if any(finding.start < f.end and f.start < finding.end for f in kept):
            continue  # 与高优先级已保留区间重叠 → 丢弃
        kept.append(finding)
    kept.sort(key=lambda f: (f.start, f.end))
    return kept


def sanitize(text: str, findings: list[PiiFinding]) -> str:
    """按 findings（detect_pii 输出）把命中片段替换为语义占位词，其余原样保留。

    纯函数；findings 为空返回原文；防御式跳过与游标重叠的条目（正常输入不会出现）。
    """
    if not isinstance(text, str) or not findings:
        return text
    parts: list[str] = []
    cursor = 0
    for finding in sorted(findings, key=lambda f: (f.start, f.end)):
        if finding.start < cursor:
            continue  # 防御：重叠区间跳过（detect_pii 输出本不重叠）
        parts.append(text[cursor : finding.start])
        parts.append(_PLACEHOLDERS[finding.kind])
        cursor = finding.end
    parts.append(text[cursor:])
    return "".join(parts)
