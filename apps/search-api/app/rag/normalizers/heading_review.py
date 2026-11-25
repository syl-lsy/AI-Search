"""docling 输出多信号复核层（工单 B：标签与排版事实矛盾者在源头纠正）。

**问题**（plan-pdf-root-cause-remedies-20261124.md 工单 B）：docling 版面模型三类
误判未被消除——①正文短语判标题（假阳性「修改为：」×5）；②标题误判 formula
（「关于本书」类）；③标题误判 code 未审计。复核层在装配**前**对每个类型标签过
排版事实门，只降级不升级（升级仍归 Fix 2 提升通道）。

**信号口径（2026-11-24 标定 + 活体验收③修复轮修订，.zcode/tmp/calibrate_ecma_*.py /
audit_gate_decisions.py）**：
- 「字号」主信号 = bbox 内非空白字符的**字符盒高中位数**，基线 = 全文非空白字符
  盒高众数（`CharFontSampler.heights_in_bbox` / `heights_mode`）——2026-11-24
  渲染版本更换后的标本上 FPDFText_GetFontSize 恒 1.0 / GetFontWeight 恒 0 /
  GetFontName 不存在（死亡信号实测），字符盒高与字号线性同构、跨渲染器存活；
  阈值一律相对众数比值（绝对 pt 不可移植纪律）。
- 字号信号**存活**的健康 PDF 上，(a) 门与 Fix 2 提升通道维持 GetFontSize 口径
  （回归等价；`font_size_alive` 检测 + `size_stat_for` 统一回落）。

**① section_header 门（活体验收③定稿）**：非书签锚点 heading **降级当且仅当**
——(a) 盒高 <正文众数×1.05 且 (b) 非加粗 且 (c) **句末标点矛盾**（标题以句末
标点结尾=正文句式，与提升通道同判据）**或同名家族继承**（剥离句末标点后同名
的带标点成员已判假，无标点变体继承矛盾——ECMA p325「修改为」无冒号变体）。
真标题零误降实证（验收员活体验收③取证）：38 条假标题全部句末带标点、18 条真
节标题（盒高比值 0.63~1.22）全部无句末标点——句末标点为完全分隔信号。计划原
列第三信号「独立短行+上下留白」**实测废弃**：假阳性留白 15~17pt 与真标题留白
部分交叠（「码：」留白 33/26pt、「修改为：」16~17pt vs 真标题 18.8+），几何门
无法同时满足「假标题降级」与「真标题零误降」（首版实现 18 条真节标题被几何门
误降——活体验收③ FAIL 修复）；无矛盾证据时尊重版面模型标签（defer-to-label）。

**② formula 复核**：候选盒高=正文众数 ±5% 且行宽近满行 → label 降为 text（文本
段，走 orig/提升通道）。**③ code 复核**：候选盒高=正文众数 ±5% 且字体名可得且
无一命中等宽名册 → 降级文本段（`FPDFText_GetFontInfo` + ctypes.c_int(0)）。

**关闭态**：`KB_PDF_HEADING_REVIEW=false` → 复核层整体旁路（零降级、零回落、
parse_meta 无 heading_review 键），完全等价旧管线——回退门。
"""

import re
from typing import Any

#: 句末标点正则——提升通道（Fix 2）与复核门 (c) 共用；④ P3-1 补英文句点「.」
#: （句末「.」结尾大字行不得提升；探针伤情单「脱漏」判据与此正则 parity 锁死）。
SENTENCE_END_PATTERN = re.compile(r"[。：:，,；;.]$")

#: ① (a) 字号门比值（**盒高口径**）：bbox 内盒高中位数 ≥ 全文盒高众数 × 该值。
#: ECMA 全文门决策审计（.zcode/tmp/audit_gate_decisions.py，2026-11-24，全 372
#: heading 的比值全分布）：假阳性上界 1.034（「修改为：」族/章首目录条/勘误引用
#: 行）；真标题 (a) 带下界 1.053（Array.prototype.includes() 方法——混合脚本行
#: 中位数被拉丁字符拉低后的实测值；纯比值带 1.20~1.93）。**1.05 为实测分界**。
#: 标定轨迹：1.25（误用勘误引用样式）→ 1.15 → 1.05（全量审计定界）；活体验收③
#: 后第三信号改句末标点判据，本阈值仅承担「大字行保留」的正向证据面。
#: 无盒高面（旧版假体）回落 GetFontSize 口径 × 1.25（旧标本校准值，回归等价）。
REVIEW_SIZE_RATIO = 1.05
#: GetFontSize 回落口径比值（仅无盒高面假体路径；旧标本 h2 20pt/body 16pt=1.25）
_REVIEW_SIZE_RATIO_LEGACY = 1.25
#: ②③ 「字号=正文众数」判定容差（±5%，同提升锚相对容差口径）
REVIEW_BODY_TOLERANCE = 0.05
#: ② formula「行宽近满行」：bbox 宽 ≥ 页正文行宽基准 × 该值
REVIEW_FULL_WIDTH_RATIO = 0.85
#: 行带聚类容差（y 中心 3pt 内同行；ECMA 行距 ≥5pt，3pt 为标定脚本同款）
_LINE_CLUSTER_TOLERANCE = 3.0
#: 等宽字体名册 token（casefold 子串命中即判等宽）：pdfium 返回原始名（含子集
#: 前缀 AAAAAD+），ECMA 混淆名（AAAAAD+font000…）不含任何 token → 无等宽证据
MONO_FONT_TOKENS = ("mono", "courier", "consolas", "menlo")


def size_stat_for(
    sampler: Any,
    page: int | None,
    bbox: tuple[float, float, float, float] | None,
    *,
    height_fallback: bool = True,
) -> tuple[float, bool] | None:
    """bbox 字号统一解析：字号存活 → GetFontSize 口径（回归等价）；死亡 → 盒高回落。

    返回 `(字号/盒高中位数, 是否加粗)`；采样失败/缺位 → None。Fix 2 提升通道与
    ① (a) 门共用（同一标本同一口径，两处阈值语义一致）；`height_fallback=False`
    时死亡字号不回落（维持 GetFontSize 原值——提升通道的关闭态回退语义）。
    """
    if sampler is None or bbox is None or not page:
        return None
    try:
        stat = sampler.stats_in_bbox(page - 1, bbox)
    except Exception:  # noqa: BLE001 —— 采样失败按数据不可得处理
        stat = None
    if stat is None:
        return None
    if getattr(sampler, "font_size_alive", True):
        return stat
    if not height_fallback:
        return stat
    height = None
    try:
        height = sampler.heights_in_bbox(page - 1, bbox)
    except Exception:  # noqa: BLE001 —— 盒高面缺失/失败按数据不可得处理
        height = None
    if height is None:
        return None
    return (float(height), bool(stat[1]))


def body_baseline_for(sampler: Any) -> float | None:
    """正文基线：字号存活 → `body_font_size`；死亡 → `heights_mode`（盒高众数）。

    与 `size_stat_for` 必须同口径取值（两处分别取不同口径会造成阈值错位）。
    """
    if sampler is None:
        return None
    if getattr(sampler, "font_size_alive", True):
        return getattr(sampler, "body_font_size", None)
    return getattr(sampler, "heights_mode", None)


class PageGeometry:
    """页几何服务：从采样器 `page_chars` 聚类行带 → 页正文行宽基准。

    仅依赖字符盒几何（不依赖字号 API）；采样器无 `page_chars` 面（旧版假体）时
    `available()=False`，② 复核的行宽判据如实不可用（零数据零降级方向）。
    活体验收③后 ① 门改句末标点判据，行距/留白面已随几何门废弃移除。
    """

    def __init__(self, sampler: Any) -> None:
        self._sampler = sampler
        self._bands_cache: dict[int, list[tuple[float, float, float, float]]] = {}
        self._full_cache: dict[int, float | None] = {}

    def available(self) -> bool:
        return hasattr(self._sampler, "page_chars")

    def _bands(self, page_index: int) -> list[tuple[float, float, float, float]]:
        """页字符 → 行带 `(x0, y0, x1, y1)`（y 中心 3pt 聚类、文档序）。"""
        if page_index in self._bands_cache:
            return self._bands_cache[page_index]
        chars = self._sampler.page_chars(page_index)
        ordered = sorted(chars, key=lambda c: ((c[1] + c[3]) / 2, c[0]))
        bands: list[list[float]] = []
        for char in ordered:
            yc = (char[1] + char[3]) / 2
            if bands and abs(yc - (bands[-1][1] + bands[-1][3]) / 2) <= _LINE_CLUSTER_TOLERANCE:
                band = bands[-1]
                band[0] = min(band[0], char[0])
                band[1] = min(band[1], char[1])
                band[2] = max(band[2], char[2])
                band[3] = max(band[3], char[3])
            else:
                bands.append([char[0], char[1], char[2], char[3]])
        out = [(b[0], b[1], b[2], b[3]) for b in bands]
        self._bands_cache[page_index] = out
        return out

    def full_width(self, page_index: int) -> float | None:
        """页正文行宽基准 = 最宽行带宽（无行带 → None）。"""
        if page_index in self._full_cache:
            return self._full_cache[page_index]
        bands = self._bands(page_index)
        if not bands:
            self._full_cache[page_index] = None
            return None
        value = max(band[2] - band[0] for band in bands)
        self._full_cache[page_index] = float(value)
        return self._full_cache[page_index]


def _header_gate(
    block: Any,
    sampler: Any,
    body_height: float | None,
    *,
    has_stats: bool,
    has_heights: bool,
) -> bool:
    """① 正向证据面：(a) 盒高/字号 或 (b) 加粗 → `True`=保留。

    `False` 仅为「无正向证据」；最终降级由 review_blocks 的句末标点矛盾判据
    （含同名家族继承）决定——无矛盾证据时尊重版面模型标签（defer-to-label）。
    """
    page = block.page
    bbox = block.bbox
    height = None  # (a) 主值：盒高中位数（跨渲染器口径）
    bold = False   # (b)
    ratio = REVIEW_SIZE_RATIO
    if bbox is not None and page:
        if has_stats:
            try:
                stat = sampler.stats_in_bbox(page - 1, bbox)
            except Exception:  # noqa: BLE001 —— 采样失败按数据不可得
                stat = None
            if stat is not None:
                bold = bool(stat[1])
        if has_heights:
            try:
                height = sampler.heights_in_bbox(page - 1, bbox)
            except Exception:  # noqa: BLE001 —— 盒高面缺失/失败按数据不可得
                height = None
        elif stat is not None and getattr(sampler, "font_size_alive", True):
            # 无盒高面（旧版假体）→ GetFontSize 回落口径 × 1.25（回归等价）
            height = float(stat[0])
            body_height = getattr(sampler, "body_font_size", None)
            ratio = _REVIEW_SIZE_RATIO_LEGACY
    # (a) 字号门：盒高 ≥ 正文众数 × 1.05（盒高口径）；无盒高面回落 GetFontSize × 1.25
    if height is not None and body_height is not None and height >= ratio * body_height:
        return True
    # (b) 加粗门：FPDFText_GetFontWeight ≥700（由采样器中位字符判定）
    return bold


def _demote_header(block: Any) -> None:
    """heading → 正文段：title 带回 text（零丢字），label 留痕不篡改。"""
    block.kind = "paragraph"
    block.text = block.title
    block.title = None


def _formula_review(
    block: Any, sampler: Any, body_height: float | None, geometry: PageGeometry | None
) -> bool:
    """② formula 复核：盒高=正文众数 ±5% 且行宽近满行 → `True`=降为 text。"""
    if body_height is None or block.bbox is None or not block.page:
        return False
    try:
        height = sampler.heights_in_bbox(block.page - 1, block.bbox)
    except Exception:  # noqa: BLE001 —— 盒高面缺失/失败按数据不可得
        height = None
    if height is None:
        return False
    if abs(height / body_height - 1) > REVIEW_BODY_TOLERANCE:
        return False
    if geometry is None or not geometry.available():
        return False
    full = geometry.full_width(block.page - 1)
    if full is None:
        return False
    return (block.bbox[2] - block.bbox[0]) >= REVIEW_FULL_WIDTH_RATIO * full


def _code_review(
    block: Any, sampler: Any, body_height: float | None, geometry: PageGeometry | None
) -> bool:
    """③ code 复核：盒高=正文众数 ±5% 且字体名可得且无一命中等宽名册 → `True`=降级。

    「等宽缺失」按**正证据缺失不降级**方向执行：字体名不可得（None）时保持 code
    （零误降方向）；ECMA 混淆字体名（无 mono token）+ 代码盒高 ≈0.5×正文众数 →
    AND 条件使真代码零误降。
    """
    if body_height is None or block.bbox is None or not block.page:
        return False
    if not hasattr(sampler, "font_names_in_bbox"):
        return False
    try:
        height = sampler.heights_in_bbox(block.page - 1, block.bbox)
    except Exception:  # noqa: BLE001
        height = None
    if height is None:
        return False
    if abs(height / body_height - 1) > REVIEW_BODY_TOLERANCE:
        return False
    try:
        names = sampler.font_names_in_bbox(block.page - 1, block.bbox)
    except Exception:  # noqa: BLE001
        names = None
    if not names:
        return False
    return not any(
        any(token in name.casefold() for token in MONO_FONT_TOKENS) for name in names
    )


def review_blocks(
    blocks: list[Any],
    *,
    sampler: Any,
    geometry: PageGeometry | None = None,
    anchors: frozenset[int] | set[int] = frozenset(),
) -> dict[str, int]:
    """复核 pending 块序列（就地降级），返回计数（parse_meta.heading_review 面）。

    `anchors` = 书签锚点的 pending 下标集合（书签校正产出；校正关闭 → 空集 →
    全部 heading 过门）。采样器无字体表面（旧版假体/零字体数据）→ 整体旁路
    （零数据零降级）。① 门判定：盒高/加粗正向证据过门保留；无正向证据时以
    句末标点矛盾（含同名家族继承）定降级，无矛盾则尊重版面模型标签保留。
    formula 候选降级只改 label（text/title 原样，走 orig/提升通道）；code 降级
    kind→paragraph、label→text。
    """
    counts: dict[str, int] = {
        "checked": 0,
        "demoted_header": 0,
        "demoted_formula": 0,
        "demoted_code": 0,
    }
    if sampler is None:
        return counts
    has_stats = hasattr(sampler, "stats_in_bbox")
    has_heights = hasattr(sampler, "heights_in_bbox")
    if not has_stats and not has_heights:
        return counts
    if geometry is None and hasattr(sampler, "page_chars"):
        geometry = PageGeometry(sampler)
    body_height = getattr(sampler, "heights_mode", None)
    # pass1：非锚点 heading 分槽 + 句末标点家族登记（剥离句末标点后同名 = 同族）
    slots: list[tuple[int, Any, str, bool]] = []
    punct_norms: set[str] = set()
    for index, block in enumerate(blocks):
        if block.kind == "heading" and index not in anchors:
            counts["checked"] += 1
            title = (block.title or "").strip()
            has_punct = bool(SENTENCE_END_PATTERN.search(title))
            norm = SENTENCE_END_PATTERN.sub("", title, count=1).strip()
            slots.append((index, block, norm, has_punct))
            if has_punct:
                punct_norms.add(norm)
    # pass2：判定——(a)/(b) 正向证据过门保留；否则句末标点矛盾（含家族继承）降级；
    # 无矛盾证据 → 尊重版面模型标签保留（defer-to-label，真标题零误降方向）
    for _index, block, norm, has_punct in slots:
        if _header_gate(block, sampler, body_height, has_stats=has_stats, has_heights=has_heights):
            continue
        if has_punct or norm in punct_norms:
            _demote_header(block)
            counts["demoted_header"] += 1
    for block in blocks:
        if block.kind == "paragraph" and block.label == "formula":
            if _formula_review(block, sampler, body_height, geometry):
                block.label = "text"
                counts["demoted_formula"] += 1
        elif block.kind == "code":
            if _code_review(block, sampler, body_height, geometry):
                block.kind = "paragraph"
                block.label = "text"
                counts["demoted_code"] += 1
    return counts
