"""PDF 书签大纲 → heading 层级确定性校正（docling 版面推断的确定性补丁）。

**缺陷背景（真实标本量化）**：docling 版面分析推断的标题层级系统性不可靠。标本
《Understanding ECMAScript 6》中文全译（328 页，GitBook 导出，自带 18 条全顶级
书签）：树内 373 个 heading，17 个可命中的书签章级标题**全部判错**（引言与多数章
被判 L2 挂在「关于」下、第二/三章被判 L4）、14 处层级跳级、7 个章标题被修改记录区
重复识别。字号启发式（docling_adapter.assign_heading_levels）只能聚类样式，无从
知道「章」的语义——而 PDF 书签（outline/TOC，pypdfium2 `get_toc`）是出版方给出的
**语义层级真相**，属确定性信号，优先于版面推断。

**设计（确定性，零模型零网络）**：
1. **可用性护栏（双层）**：条目 ≥ `OUTLINE_MIN_ENTRIES`，且命中判据二选一——
   **整体命中率 ≥ `OUTLINE_MIN_HIT_RATE`**，或（**顶级书签命中率 ≥ 同一阈值 且顶级
   命中数 ≥ 3**）。命中率 = 命中书签条目数 / 该书签层级的条目数——书签是出版物的
   目录，正常出版的命中率应接近全量（标本 17/18=94%）；顶级层全命中说明书签与正文
   结构仍对应，故教材类 PDF 混入的图表书签（`图 1-1` 之类找不到对应标题）不再连坐
   禁用整本校正；而「3 条书签 1 命中」这类既过不了整体也过不了顶级双层判据的情形
   整份禁用、回退保守的序列一致性清洗。若用「命中节点数 / 全部 heading 数」，会被
   大量未入书签的小节稀释（标本 17/373=4.5%）恒判不可用，属错误口径。
2. **锚点匹配**：归一化标题精确比对（NFKC 全半角、剥成对 `**` 强调包裹、格式化字符
   剥离、去全部空白、casefold）；同名多节点按**页码**消歧（书签 dest 0-based +1
   对 docling prov 1-based；精确优先，其次 ±1 页容差，再次文档序首个未占用节点）；
   每个节点至多被一条书签占用（先到先得，后到按未命中处理）。
3. **层级钉正**：锚点 level = 书签层级（按最小层级归一后 +1）；未命中 heading 按
   「相对偏移重校准」——以**最近更浅的已定级标题**为基准
   （`level_at_raw[base] + (raw − base)`，base 取当前锚点区内 raw 更小的最近一档）。
   书签覆盖全文时，锚点之后的标题若原始层级不深于锚点，语义上仍是其后代：
   **下界钉为锚点 level+1，且该被压深标题自身成为后续更深标题的基准，直到下一个
   锚点**（否则被压深者的子级会与它平级——评审 P1-1 复现的父子反转：原始
   `[1,1,2,2,1]` → 校正 `[1,2,3,3,1]`）。文档开头无前置锚点时以首个锚点为参照外推。
4. **无可用书签 → 序列一致性清洗**（保守，不引入新推断）：按**原始层级栈**压取
   （相对最近更浅祖先压到 +1；`[1,4,3,2]` → `[1,2,2,2]`，不做紧邻式推断——紧邻式
   会把回退的浅标题误当上一标题的父级，产生父子反转）；重复标题**只报数不删节点**
   （内容零丢失）。字号/版面聚类留后续工单。

**介入时机**：校正产物是「标题序列 → 校正 level」的纯映射，调用方在**装配前**应用
（docling_adapter 在 `BlockTreeBuilder.add` 之前写回 pending.level）——层级是在
heading 栈里即时物化 children 的，装配后改 level 须重挂父子，装配前介入让唯一嵌套
实现（markdown_mapper.BlockTreeBuilder）直接产出正确树。兄弟指针（prev/next）本路径
从不物化，装载期由 `storage.get_ir_json` 调 `ir_schema.rebuild_siblings` 重建。
"""

import re
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.rag.normalizers.pdf_textlayer import open_pdf_bytes

__all__ = [
    "CORRECTION_BOOKMARK",
    "CORRECTION_RAW",
    "CORRECTION_SEQUENCE_CLEAN",
    "MIN_LEVEL",
    "MAX_LEVEL",
    "OUTLINE_MIN_ENTRIES",
    "OUTLINE_MIN_HIT_RATE",
    "Anchor",
    "HeadingItem",
    "OutlineCorrection",
    "OutlineEntry",
    "correct_heading_levels",
    "count_level_jumps",
    "duplicate_title_report",
    "normalize_title",
    "read_pdf_outline",
    "resolve_anchors",
    "sequence_clean_levels",
]

#: 校正路径三值（探针自检门禁的 correction_path 指标；raw=无 heading 可校正）
CORRECTION_BOOKMARK = "bookmark"
CORRECTION_SEQUENCE_CLEAN = "sequence-clean"
CORRECTION_RAW = "raw"

#: 书签可用性护栏：条目下限（一两份条目的自动大纲不足以定章法）
OUTLINE_MIN_ENTRIES = 3
#: 书签可用性护栏：命中率下限（书签是出版物目录，真书签命中率应接近全量——标本
#: 17/18=94%；低命中说明书签与正文结构脱节，整份禁用校正。见模块 docstring 第 1 条）
OUTLINE_MIN_HIT_RATE = 0.6

#: IR heading level 契约域（ir_schema level ge=1 le=9）
MIN_LEVEL = 1
MAX_LEVEL = 9

#: 书签读取深度上限（覆盖多级书签；超深结构 clamp 由 level 契约兜底）
_OUTLINE_MAX_DEPTH = 8

#: 成对 `**` 强调包裹（剥壳保留内容；单个 `*` 不剥——可能是标题里的真字符）
_BOLD_WRAP_RE = re.compile(r"\*\*(.+?)\*\*")


@dataclass(frozen=True, slots=True)
class OutlineEntry:
    """书签条目：标题 + 层级（0-based，pdfium 实测顶层=0）+ 目标页（0-based；缺为 None）。"""

    title: str
    level: int
    page: int | None = None


@dataclass(frozen=True, slots=True)
class HeadingItem:
    """树内 heading 候选：标题 + docling 原始 level + 页码（1-based，同 loc/prov）。"""

    title: str
    level: int
    page: int | None = None


@dataclass(frozen=True, slots=True)
class Anchor:
    """书签锚点（命中项）：下标 + 书签原标题/层级 + 钉正后的 IR level。"""

    index: int
    outline_title: str
    outline_level: int
    target_level: int


@dataclass(frozen=True, slots=True)
class OutlineCorrection:
    """校正结果（纯数据；调用方按 `levels` 写回，`path`/诊断指标进 parse_meta）。"""

    path: str
    levels: list[int]
    usable: bool
    outline_count: int
    anchors: list[Anchor]
    unmatched: list[str]
    duplicate_titles: list[tuple[str, int]]
    level_jumps_before: int
    level_jumps_after: int
    #: 被压深标题（走下界分支的条目：原始层级栈已弹空、落锚点 level+1 的那批）
    pressed_titles: list[str] = field(default_factory=list)

    @property
    def pressed(self) -> int:
        """被压深条数（= 走下界分支的条目数）。"""
        return len(self.pressed_titles)


# ---------------------------------------------------------------------------
# 标题归一化与标题读取
# ---------------------------------------------------------------------------


def normalize_title(text: str) -> str:
    """标题归一化（书签 ↔ 树内标题的比对口径）：NFKC 全半角归一、剥**成对** markdown
    强调符（`**粗体**`；单个 `*` 是标题内容如 `A*B`，不剥）、剥 Unicode 格式化字符
    （Cf，零宽/方向符）、去全部空白、casefold。"""
    flat = unicodedata.normalize("NFKC", text or "")
    flat = _BOLD_WRAP_RE.sub(r"\1", flat)
    flat = "".join(ch for ch in flat if unicodedata.category(ch) != "Cf")
    return re.sub(r"\s+", "", flat).casefold()


def read_pdf_outline(
    data: bytes, *, pdf_factory: Callable[[bytes], Any] | None = None
) -> list[OutlineEntry]:
    """PDF 字节 → 书签条目列表（pypdfium2 读取；失败原样上抛由调用方归一）。

    `pdf_factory` 为单测注入缝（生产零传 → `pdf_textlayer.open_pdf_bytes`）；单条
    get_title/dest 损坏只降级该条（标题置空 → 自然进 unmatched；页码缺 → 消歧退化），
    不废整份大纲。读取完即 close（句柄零滞留）。层级原样返回——pypdfium2 按书签
    父链深度给出层级，顶层恒为 0（生产路径不会出现 1-based；归一逻辑仍按最小层级
    兜底，防其它生成器形态）。
    """
    factory = pdf_factory or open_pdf_bytes
    document = factory(data)
    entries: list[OutlineEntry] = []
    try:
        for entry in document.get_toc(max_depth=_OUTLINE_MAX_DEPTH):
            page: int | None = None
            try:
                title = str(entry.get_title() or "")
            except Exception:  # noqa: BLE001 —— 单条标题损坏按空串（进 unmatched）
                title = ""
            try:
                dest = entry.get_dest()
                if dest is not None:
                    page = int(dest.get_index())
            except Exception:  # noqa: BLE001 —— 单条 dest 损坏不废整份大纲
                page = None
            entries.append(
                OutlineEntry(title=title, level=int(entry.level), page=page)
            )
    finally:
        close = getattr(document, "close", None)
        if callable(close):
            close()
    return entries


# ---------------------------------------------------------------------------
# 锚点解析
# ---------------------------------------------------------------------------


def resolve_anchors(
    headings: Sequence[HeadingItem], outline: Sequence[OutlineEntry]
) -> tuple[list[Anchor], list[str]]:
    """书签 → 树内 heading 锚点解析：返回 (按文档序的锚点列表, 未命中书签标题)。

    同名多节点用页码消歧（见模块 docstring 第 2 条）；每个节点至多被占用一次。
    层级按**最小层级归一**（书签 level − min(level) + 1）——pypdfium2 按书签父链深度
    给层级、顶层恒为 0（生产路径不会出现 1-based），此处归一为兜底形态（其它生成器
    若给 1-based 也不至于让全文档锚点统一抬高一级）。
    """
    level_offset = min((entry.level for entry in outline), default=0)
    index_by_title: dict[str, list[int]] = {}
    for index, item in enumerate(headings):
        key = normalize_title(item.title)
        if key:
            index_by_title.setdefault(key, []).append(index)

    claimed: set[int] = set()
    anchors: list[Anchor] = []
    unmatched: list[str] = []
    for entry in outline:
        key = normalize_title(entry.title)
        candidates = (
            [i for i in index_by_title.get(key, []) if i not in claimed] if key else []
        )
        picked = _pick_candidate(headings, candidates, entry.page)
        if picked is None:
            unmatched.append(entry.title)
            continue
        claimed.add(picked)
        normalized_level = entry.level - level_offset
        anchors.append(
            Anchor(
                index=picked,
                outline_title=entry.title,
                outline_level=normalized_level,
                target_level=_clamp(normalized_level + 1),
            )
        )
    anchors.sort(key=lambda anchor: anchor.index)
    return anchors, unmatched


def _pick_candidate(
    headings: Sequence[HeadingItem], candidates: Sequence[int], page0: int | None
) -> int | None:
    """同名候选 → 页码消歧：精确页 → ±1 页容差 → 文档序首个（末档兜底）。"""
    if not candidates:
        return None
    if page0 is None:
        return candidates[0]
    target = page0 + 1  # 书签 dest 0-based → docling prov 1-based
    for index in candidates:
        if headings[index].page == target:
            return index
    for index in candidates:
        page = headings[index].page
        if page is not None and abs(page - target) <= 1:
            return index
    return candidates[0]


def _outline_usable(
    outline: Sequence[OutlineEntry], anchors: Sequence[Anchor]
) -> bool:
    """可用性护栏（双层口径，见模块 docstring 第 1 条）。

    ① 条目 ≥ `OUTLINE_MIN_ENTRIES`；
    ② 命中判据二选一：**整体命中率 ≥ 0.6**，或（**顶级书签命中率 ≥ 0.6 且顶级命中
       数 ≥ 3**）——教材类 PDF 的图表书签（图 1-1…）常年搜不到对应标题，单看整体
       命中率会连坐禁用整本书；顶级目录全命中说明书签与正文结构仍然对应。
    """
    if len(outline) < OUTLINE_MIN_ENTRIES:
        return False
    if len(anchors) / len(outline) >= OUTLINE_MIN_HIT_RATE:
        return True
    top_level = min(entry.level for entry in outline)
    top_entries = sum(1 for entry in outline if entry.level == top_level)
    top_hits = sum(1 for anchor in anchors if anchor.outline_level == 0)
    return top_hits >= OUTLINE_MIN_ENTRIES and top_hits / top_entries >= OUTLINE_MIN_HIT_RATE


# ---------------------------------------------------------------------------
# 层级校正
# ---------------------------------------------------------------------------


def _clamp(level: int) -> int:
    return max(MIN_LEVEL, min(MAX_LEVEL, int(level)))


def sequence_clean_levels(levels: Sequence[int]) -> list[int]:
    """序列一致性清洗（无可用书签时的保守兜底）：按**原始层级栈**压取。

    逐条压栈：弹出 raw ≥ 当前 raw 的祖先，最近更浅祖先生效——子级 = 祖先 level+1
    （跳级被压平）；无更浅祖先（文档层）→ level=1。与「紧邻前一个标题」的差式口径
    不同：浅标题回退时不会把它误当上一标题的父级（`[1,4,3,2]` → `[1,2,2,2]`）。
    """
    cleaned: list[int] = []
    stack: list[tuple[int, int]] = []  # (raw level, corrected level)
    for raw in levels:
        level = _clamp(raw)
        while stack and stack[-1][0] >= level:
            stack.pop()
        corrected = _clamp(stack[-1][1] + 1) if stack else MIN_LEVEL
        cleaned.append(corrected)
        stack.append((level, corrected))
    return cleaned


def _apply_anchor_levels(
    items: Sequence[HeadingItem], raw_levels: Sequence[int], anchors: Sequence[Anchor]
) -> tuple[list[int], list[str]]:
    """锚点钉正 + 相对偏移重校准 → (校正 level 序列, 被压深标题清单)。

    锚点区内维护**按位置的原始层级栈**（锚点作栈底）：非锚点先弹掉栈内 raw ≥ 自身
    的条目（兄弟/更浅分支不留陈旧基准），取栈顶为基准做偏移
     （`基准 level + (raw − 基准 raw)`）；弹空 = 原始层级栈无更浅基准 → 按下界钉为
     锚点 level+1（**被压深**），并压栈成为更深标题的基准——否则被压深者的子级会与
     它平级。栈式口径消除字典式映射的「基准逃逸」：已出栈的深层条目不会给后文更深
     的标题当基准（P 被压深在 L2、R raw5 必须落在 P 之下而非与 P 平级）。
    文档开头无前置锚点时以首个锚点为参照外推。
    """
    levels = list(raw_levels)
    first = anchors[0]
    first_raw = raw_levels[first.index]
    for index in range(first.index):
        levels[index] = _clamp(first.target_level + raw_levels[index] - first_raw)

    by_index = {anchor.index: anchor for anchor in anchors}
    anchor = first
    levels[first.index] = first.target_level
    stack: list[tuple[int, int]] = [(raw_levels[first.index], first.target_level)]
    pressed_titles: list[str] = []
    for index in range(first.index + 1, len(items)):
        candidate = by_index.get(index)
        if candidate is not None:
            anchor = candidate
            levels[index] = anchor.target_level
            stack = [(raw_levels[index], anchor.target_level)]
            continue
        level = _clamp(raw_levels[index])
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            base_raw, base_level = stack[-1]
            corrected = base_level + (level - base_raw)
        else:
            corrected = anchor.target_level + 1
            pressed_titles.append(items[index].title)
        levels[index] = _clamp(corrected)
        stack.append((level, levels[index]))
    return levels, pressed_titles


def duplicate_title_report(headings: Sequence[HeadingItem]) -> list[tuple[str, int]]:
    """重复标题分组（归一化标题 → 出现次数降序；只报数不改树）。"""
    counts: dict[str, tuple[str, int]] = {}
    for item in headings:
        key = normalize_title(item.title)
        if not key:
            continue
        title, count = counts.get(key, (item.title, 0))
        counts[key] = (title, count + 1)
    duplicates = [(title, count) for title, count in counts.values() if count > 1]
    duplicates.sort(key=lambda pair: (-pair[1], pair[0]))
    return duplicates


def count_level_jumps(levels: Sequence[int]) -> int:
    """层级跳级计数：相邻 level 上升 >1 处数（诊断指标）。"""
    jumps = 0
    for previous, current in zip(levels, levels[1:]):
        if current - previous > 1:
            jumps += 1
    return jumps


def correct_heading_levels(
    headings: Sequence[HeadingItem], outline: Sequence[OutlineEntry]
) -> OutlineCorrection:
    """heading 序列 + 书签大纲 → 校正结果（纯函数；决策全在此处，调用方零推断）。

    路径判定：零 heading → raw；书签不可用（条目数/命中率不过护栏）→ sequence-clean；
    否则 bookmark。
    """
    items = list(headings)
    entries = list(outline)
    if not items:
        return OutlineCorrection(
            path=CORRECTION_RAW,
            levels=[],
            usable=False,
            outline_count=len(entries),
            anchors=[],
            unmatched=[entry.title for entry in entries],
            duplicate_titles=[],
            level_jumps_before=0,
            level_jumps_after=0,
        )

    raw_levels = [_clamp(item.level) for item in items]
    anchors, unmatched = resolve_anchors(items, entries)
    usable = _outline_usable(entries, anchors)
    pressed_titles: list[str] = []
    if usable:
        levels, pressed_titles = _apply_anchor_levels(items, raw_levels, anchors)
        path = CORRECTION_BOOKMARK
    else:
        levels = sequence_clean_levels(raw_levels)
        path = CORRECTION_SEQUENCE_CLEAN

    return OutlineCorrection(
        path=path,
        levels=levels,
        usable=usable,
        outline_count=len(entries),
        anchors=anchors,
        unmatched=unmatched,
        duplicate_titles=duplicate_title_report(items),
        level_jumps_before=count_level_jumps(raw_levels),
        level_jumps_after=count_level_jumps(levels),
        pressed_titles=pressed_titles,
    )
