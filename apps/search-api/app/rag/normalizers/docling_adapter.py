"""docling 适配器：pdf 结构化（标题层级/表格逐格/代码块/页码溯源）→ 树 IR。

选型锚点（ARCH §2 S7 / ADR-018③；探针 m3-probe-normalization-20261122.md §2.2
pdf 暖跑 3.9s/页）：Docling MIT/LF AI&Data，TableFormer 表格保真（逐格精确 +
num_rows/num_cols 编程取矩阵），代码块独立 code 条目，条目级页码溯源
item.prov[0].page_no。

**已知短板的启发式兜底（不伪造层级）**：探针 §2.2 实测 pdf 标题层级全判 level=1
（20/16/13pt 三级样式未分级）+ 1 处正文误判——适配器以字号/加粗启发式重排层级：
- 字号可得 → **bbox 内字符采样**（`CharFontSampler.stats_in_bbox`；取代「文本首现
  搜索」——同页同文多处会采错首现）→ 字号降序 5% 容差聚类分级
  （`meta.level_source="heuristic_font_size"`，parse_meta.heading_heuristic.inferred 留痕）；
- 字体数据不可得 → level=1 兜底（`level_source="fallback_flat"`，
  heading_heuristic.fallback_flat 留痕——探针 §3 兜底协议：不伪造分级来源）。
- **字号提升**（docling 版面误判补偿，2026-11-24 根因报告）：item.text 为空回落
  `item.orig`（formula 条目的原文只在 orig）；非标题的 text/formula 短行若 bbox
  字号命中已有标题字号簇（≥1.25×正文众数 ±5%，**相对口径**——该 PDF 有 0.72 页
  缩放，绝对 pt 不可用）→ 提升为 heading（`level_source="font_promoted"` 由后续
  分级/校正覆写；parse_meta.heading_heuristic.promoted 计数留痕）。提升在 pending
  阶段完成，故参与书签大纲校正的锚点/偏移重校准。
  采样器与自建 PdfDocument 在采样完成后即关闭（pdfium 句柄零滞留）。

**OCR 叠加（ARCH「Docling+PaddleOCR 进程内」形态）**：文本层稀疏/缺失判定后叠加——
pdf_textlayer 判定文本层不足 32 字符的页（真扫描页 0 字符、稀疏页 9~22 字符）进
OCR 候选，交 PaddleOCR（paddle_engine 惰性单例）逐页识别；识别行**先与该页文本层
原文逐行去重**（剔除空白 + casefold 后**单行相等**才丢弃：页眉/页码/章名等重复
行剔除，正文保留；不做子串判定以免误杀正文短行），再**按页码插回 IR 对应位置**
（该页页边界的活动标题子树内，heading_path 正确继承；不追加树尾），节点落
paragraph（meta.raw_type="ocr_line" + loc.page）；
OCR 运行时 parse_meta.parser 记末段实际执行组件（data-model §1.7「多组件流水记
末段」）；`ocr_enabled=false` 或文本层充足 → OCR 通道零触发（回退门语义）。

**书签大纲校正 + 骨架重建（确定性层级钉正，heading_outline 模块）**：版面/字号推断的层级在
「章」的语义上系统性不可靠（标本 17/17 可命中书签章标题判错）。装配**前**拿 PDF
书签（pypdfium2 get_toc）与 pending heading 序列做归一化标题 + 页码消歧匹配，锚点
level = 书签层级+1、未命中项按最近锚点相对偏移重校准；书签不可用（条目 <3 或
双层命中判据不过：整体命中率 <0.6 且非「顶级命中率 ≥0.6 且顶级命中 ≥3」）回落序列
一致性清洗。**骨架重建（工单 A，推翻 T1-b「校正不造节点」，用户 2026-11-24 立项
批准）**：护栏通过的 PDF，未命中的**顶级**书签条目（章级）按「页码区间归界」在
pending 序列造 heading 节点（raw_type=outline_bookmark、level_source=outline_created，
插入位=首个页码 ≥ dest+1 的块之前），再走既有锚点钉正——造节点与自身条目精确
匹配成锚点（进复核门豁免面），正文按相邻书签页码区间归属其子树；残损书签
（空标题/缺页码/超界）不造、保持 unmatched 上报。护栏不过/无书签/开关关闭 →
零造节点，回落现行路径逐字节等价。**介入点必须在装配前**：层级在 `BlockTreeBuilder.add`
的 heading 栈里即时物化 children 的，装配后改 level 得重挂父子（重复实现嵌套语义）。
书签读取失败（坏字节/pdfium 不可用）不阻塞解析，按「无书签」处理并留痕错误类型名；
parse_meta.heading_outline 记 correction_path 与锚点/未命中/跳级/造节点诊断。

**多信号复核层（工单 B，heading_review 模块；KB_PDF_HEADING_REVIEW 总开关）**：
docling 类型标签进 IR 前过排版事实门（校正后、装配前、只降级不升级）——①非书签
锚点 heading **降级当且仅当**：盒高 <正文众数×1.05 且 非加粗 且 句末标点矛盾
（标题以句末标点结尾=正文句式，含同名家族继承——ECMA p325「修改为」无冒号变体
随「修改为：」×5 族降）；无矛盾证据时尊重版面模型标签保留（真标题零误降实证：
38 假全带标点 / 18 真全无标点，几何留白信号与假阳性分布重叠已废弃）。
②formula 候选盒高=正文众数且行宽近满行降为 text（走 orig/提升通道）；③code
候选盒高=正文众数且字体名无等宽证据降级。「字号」口径：信号存活 → GetFontSize
（回归等价）；死亡（2026-11-24 新渲染版 ECMA 实测恒 1.0/权重恒 0/GetFontName
缺失）→ 字符盒高回落（线性同构、跨渲染器存活），Fix 2 提升通道同批恢复（旧
基线「关于本书 = heading raw_type=formula」形态）。关闭态 = 完全等价旧管线
（零降级、零回落、parse_meta 无 heading_review 键）。

异常归一：组件失败 → NormalizationError（original_exception 链入，零内容回显）。
单例 DocumentConverter（模型权重进程内一份；do_ocr=False——OCR 通道钉 PaddleOCR，
不经 docling 内置 OCR）。
"""

import importlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from app.core.config import kb_settings
from app.rag.ir_schema import IrNode, IrNodeType
from app.rag.normalizers.errors import (
    NormalizationError,
    UnsupportedTypeError,
)
from app.rag.normalizers.heading_outline import (
    CORRECTION_BOOKMARK,
    HeadingItem,
    OutlineCorrection,
    OutlineEntry,
    SkeletonSlot,
    correct_heading_levels,
    plan_skeleton_slots,
    read_pdf_outline,
)
from app.rag.normalizers.heading_review import (
    SENTENCE_END_PATTERN,
    body_baseline_for,
    review_blocks,
    size_stat_for,
)
from app.rag.normalizers.markdown_mapper import (
    BlockTreeBuilder,
    make_document_node,
    make_loc,
)
from app.rag.normalizers.paddle_engine import get_engine, recognize_lines
from app.rag.normalizers.pdf_textlayer import (
    OCR_RENDER_SCALE,
    CharFontSampler,
    open_pdf_bytes,
    page_text,
    page_text_flags,
    render_page,
)
from app.rag.normalizers.result import NormalizationResult
from app.rag.normalizers.settings import NormalizerSettings

#: OCR 识别回调形态：页索引（0 基）→ 文本行流
OcrRecognizer = Callable[[int], list[str]]

#: 字号聚类同簇相对容差（20pt 与 19.5pt 同簇、14pt 与 13pt 异簇——测试锁死）
_FONT_CLUSTER_TOLERANCE = 0.95
#: heading level 上限（ir_schema level ge=1 le=9）
_MAX_HEADING_LEVEL = 9

_CONVERTER_SINGLETON: Any | None = None


def clear_docling_cache() -> None:
    """清空单例（测试隔离面；生产进程内不调用）。"""
    global _CONVERTER_SINGLETON
    _CONVERTER_SINGLETON = None


def _get_converter() -> Any:
    """DocumentConverter 惰性单例：pdf 专用管线、do_ocr=False（OCR 通道钉
    PaddleOCR 进程内叠加，不经 docling 内置 OCR——ARCH 单栈不分裂决策面）。"""
    global _CONVERTER_SINGLETON
    if _CONVERTER_SINGLETON is None:
        document_converter = importlib.import_module("docling.document_converter")
        base_models = importlib.import_module("docling.datamodel.base_models")
        pipeline_options = importlib.import_module("docling.datamodel.pipeline_options")
        po = pipeline_options.PdfPipelineOptions()
        po.do_ocr = False
        po.do_table_structure = True
        _CONVERTER_SINGLETON = document_converter.DocumentConverter(
            format_options={
                base_models.InputFormat.PDF: document_converter.PdfFormatOption(
                    pipeline_options=po
                )
            }
        )
    return _CONVERTER_SINGLETON


# ---------------------------------------------------------------------------
# 采集与映射
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _PendingBlock:
    """iterate_items 顺序采集的块原料（heading 先分级、树装配后统一进行）。"""

    kind: str  # heading/paragraph/list_item/table/code/picture
    text: str | None = None
    title: str | None = None
    label: str = ""
    page: int | None = None
    rows: list[list[str]] | None = None
    col_count: int | None = None
    level: int | None = None
    level_source: str | None = None
    #: docling prov bbox（top-left 原点、PDF 点；`to_top_left_origin` 转换后）——
    #: 字号采样/提升的定位面；无 prov/无页码 → None（回落旧文本搜索路径）
    bbox: tuple[float, float, float, float] | None = None
    #: 是否由字号提升产生（traceability：parse_meta.heading_heuristic.promoted）
    promoted: bool = False


def _label_value(item: Any) -> str:
    label = getattr(item, "label", None)
    if label is None:
        return ""
    return str(getattr(label, "value", label))


def _page_no(item: Any) -> int | None:
    prov = getattr(item, "prov", None)
    if prov:
        first = prov[0]
        page = getattr(first, "page_no", None)
        if page:
            return int(page)
    return None


def _item_bbox(ddoc: Any, item: Any) -> tuple[float, float, float, float] | None:
    """docling 条目 prov bbox → top-left 原点 `(l, t, r, b)`（PDF 点）；缺省 → None。

    与 `CharFontSampler.page_chars` 同坐标系（pdfium 字符盒 y 轴已转 top-left）。
    """
    prov = getattr(item, "prov", None)
    if not prov:
        return None
    first = prov[0]
    try:
        page = ddoc.pages[first.page_no]
        bbox = first.bbox.to_top_left_origin(page.size.height)
        left, top, right, bottom = bbox.as_tuple()
        return (float(left), float(top), float(right), float(bottom))
    except Exception:  # noqa: BLE001 —— bbox 不可得按无 bbox 处理（回落旧路径）
        return None


def _item_text(item: Any) -> str:
    """条目文本：`item.text` 为空时回落 `item.orig`。

    根因（2026-11-24 debugger 实证）：docling 版面模型把「关于本书」「本书读者对象」
    判成 formula（formula 0.61 压过 section_header 0.45），formula 条目 `text`
    恒为空、原文只在 `orig`；旧采集只读 text → 空节点 → 切片跳过（文字静默丢失）。
    安全面实测（ECMA 全树空文本条目=3 表 + 2 图 + 2 公式）：Table/PictureItem 的
    `orig` 均为空串、且两者走各自分支不读本函数 → 通用回落只命中 formula/未知类。
    """
    text = getattr(item, "text", None)
    if isinstance(text, str) and text.strip():
        return text
    orig = getattr(item, "orig", None)
    if isinstance(orig, str) and orig.strip():
        return orig
    return text if isinstance(text, str) else ""


def _collect_blocks(ddoc: Any) -> list[_PendingBlock]:
    """DoclingDocument.iterate_items → 文档序块原料（探针 §2.2 形态）。"""
    pending: list[_PendingBlock] = []
    for item, _level in ddoc.iterate_items():
        label = _label_value(item)
        page = _page_no(item)
        bbox = _item_bbox(ddoc, item)
        if label == "table" and getattr(item, "data", None) is not None:
            grid = item.data.grid
            rows = [
                [str(getattr(cell, "text", None) or "") for cell in row] for row in grid
            ]
            col_count = getattr(item.data, "num_cols", None) or max(
                (len(r) for r in rows), default=0
            )
            pending.append(
                _PendingBlock(
                    kind="table",
                    label=label,
                    page=page,
                    rows=rows,
                    col_count=int(col_count),
                    bbox=bbox,
                )
            )
            continue
        text = _item_text(item)
        if label in ("section_header", "title"):
            pending.append(
                _PendingBlock(
                    kind="heading", title=str(text or ""), label=label, page=page, bbox=bbox
                )
            )
        elif label == "code":
            pending.append(
                _PendingBlock(kind="code", text=str(text or ""), label=label, page=page, bbox=bbox)
            )
        elif label == "list_item":
            pending.append(
                _PendingBlock(
                    kind="list_item", text=str(text or ""), label=label, page=page, bbox=bbox
                )
            )
        elif label == "picture":
            pending.append(_PendingBlock(kind="picture", label=label, page=page, bbox=bbox))
        else:
            pending.append(
                _PendingBlock(
                    kind="paragraph",
                    text=str(text or "") or None,
                    label=label or "text",
                    page=page,
                    bbox=bbox,
                )
            )
    return pending


def assign_heading_levels(
    entries: Sequence[tuple[str, tuple[float, bool] | None]],
    body_font_size: float | None,
) -> dict[str, tuple[int, str]]:
    """字号/加粗启发式分级（纯函数）：标题文本 → `(level, 来源)`。

    `stat is None`（字体数据不可得）→ `(1, "fallback_flat")`（level=1 兜底，来源
    如实——不伪造层级）；有字号者按字号降序 5% 容差聚类，簇序即 level（上限 9），
    来源 `"heuristic_font_size"`。body_font_size 仅作基线留痕——字号低于正文基线
    的「标题」仍按字号簇末级诚实降级，不冒充分级。同文标题映射同键（后到覆盖）——
    同文异字号属病理形态，级别取末簇并如实留痕 level_source。
    """
    levels: dict[str, tuple[int, str]] = {}
    sized: list[tuple[str, float, bool]] = []
    for title, stat in entries:
        if stat is None:
            levels[title] = (1, "fallback_flat")
        else:
            sized.append((title, float(stat[0]), bool(stat[1])))
    if not sized:
        return levels
    clusters: list[dict[str, Any]] = []
    for title, size, bold in sorted(sized, key=lambda t: (-t[1], not t[2])):
        for cluster in clusters:
            if size / cluster["size"] >= _FONT_CLUSTER_TOLERANCE:
                cluster["members"].append(title)
                break
        else:
            clusters.append({"size": size, "members": [title]})
    for rank, cluster in enumerate(clusters[:_MAX_HEADING_LEVEL], start=1):
        for title in cluster["members"]:
            levels[title] = (rank, "heuristic_font_size")
    return levels


# ---------------------------------------------------------------------------
# 字号提升（docling 把标题样式行判成 text/formula 的补偿面；pending 阶段介入）
# ---------------------------------------------------------------------------

#: 提升候选 label（docling 判成正文/公式的标题样式行）
_PROMOTE_LABELS = frozenset({"text", "formula"})
#: 锚字号下限 = 该比值 × 正文众数。**相对口径硬约束**（A/B 实测）：该 PDF 每页有
#: 0.72 倍整体缩放（cm 矩阵），pdfium/docling 字号是不同的 Tf 口径；绝对 pt 阈值
#: 跨文档不可移植，一律用「相对正文众数」比值（ECMA 正文众数 16 → 锚 ≥20）。
_PROMOTE_HEAD_RATIO = 1.25
#: 与已有标题字号簇的相对容差（±5%：23 与 23.03 同簇）
_PROMOTE_ANCHOR_TOLERANCE = 0.05
#: 提升候选文本长度上限（字）：标题样式短行；超长多行正文不提升
_PROMOTE_MAX_CHARS = 40
#: 单行判定：bbox 高 < 该比值 × 字号（两行以上行距会把 bbox 撑高）
_PROMOTE_LINE_RATIO = 1.75
#: 句末/列表式标点结尾 → 正文句式，不提升（debugger 原型零误报约束的一部分）。
#: 工单 B ④ P3-1 补英文句点「.」（句末「.」结尾大字行不得提升）；与复核门 (c)
#: 同源（heading_review.SENTENCE_END_PATTERN），探针伤情单「脱漏」判据 parity 锁死。
_PROMOTE_SENT_END = SENTENCE_END_PATTERN


def _bbox_font_stat(
    font_sampler: Any, page: int | None, bbox: tuple[float, float, float, float] | None
) -> tuple[float, bool] | None:
    """bbox 字号采样（异常/缺位一律 None——单条失败不废整份解析）。"""
    if font_sampler is None or bbox is None or not page:
        return None
    try:
        return font_sampler.stats_in_bbox(page - 1, bbox)
    except Exception:  # noqa: BLE001 —— 采样失败按数据不可得处理
        return None


def _promote_font_headings(
    pending: list[_PendingBlock],
    font_sampler: Any,
    body_font_size: float | None,
    *,
    allow_height_fallback: bool = False,
) -> int:
    """非标题短行（text/formula）按字号命中已有标题字号簇 → 提升为 heading。

    返回提升条数。条件（全满足，debugger 原型 ECMA 8 条零误报的约束）：
    单行（bbox 高 < 1.75×字号）、≤40 字、不以句末/列表式标点结尾（工单 B ④ 补
    英文句点「.」）、bbox 采样的字号落在**已有标题字号簇**（≥1.25×正文众数者）
    ±5% 内。
    提升在 pending 阶段完成 → 参与后续字号分级与书签大纲校正（与 T1-b 的交互面）。

    **字号口径**（工单 B ④ 高度回落）：字号信号存活（`font_size_alive`）→ GetFontSize
    口径（回归等价）；死亡（2026-11-24 新渲染版 ECMA 实测恒 1.0）且
    `allow_height_fallback`（随 KB_PDF_HEADING_REVIEW 开关）→ 字符盒高回落
    （`heading_review.size_stat_for`，正文基线同步回落 `heights_mode`）——恢复旧
    基线「关于本书 = heading raw_type=formula」的既有已验收行为。回落关闭时死亡
    字号通道保持旧行为（零提升，等价旧管线）。
    """
    if font_sampler is None or body_font_size is None:
        return 0
    if not hasattr(font_sampler, "stats_in_bbox"):
        return 0  # 注入假体无 bbox 采样面 → 旧行为（不提升）
    baseline = body_font_size
    if allow_height_fallback and not getattr(font_sampler, "font_size_alive", True):
        baseline = body_baseline_for(font_sampler)
        if baseline is None:
            return 0
    anchors: list[float] = []
    for block in pending:
        if block.kind != "heading":
            continue
        stat = size_stat_for(font_sampler, block.page, block.bbox, height_fallback=allow_height_fallback)
        if stat is not None and stat[0] >= _PROMOTE_HEAD_RATIO * baseline:
            anchors.append(stat[0])
    if not anchors:
        return 0
    promoted = 0
    for block in pending:
        if block.kind != "paragraph" or block.label not in _PROMOTE_LABELS:
            continue
        text = (block.text or "").strip()
        if not text or "\n" in text or len(text) > _PROMOTE_MAX_CHARS:
            continue
        if _PROMOTE_SENT_END.search(text):
            continue
        stat = size_stat_for(font_sampler, block.page, block.bbox, height_fallback=allow_height_fallback)
        if stat is None:
            continue
        size = stat[0]
        if not any(abs(size / anchor - 1) <= _PROMOTE_ANCHOR_TOLERANCE for anchor in anchors):
            continue
        if block.bbox is not None and (block.bbox[3] - block.bbox[1]) >= _PROMOTE_LINE_RATIO * size:
            continue
        block.kind = "heading"
        block.title = text
        block.text = None
        block.level_source = "font_promoted"
        block.promoted = True
        promoted += 1
    return promoted


# ---------------------------------------------------------------------------
# 书签大纲校正 + 骨架重建（确定性层级钉正；装配前写回 pending.level）
# ---------------------------------------------------------------------------

#: 造节点 raw_type（工单 A：docling 版面未识别、由书签大纲确定性重建的 heading）
_OUTLINE_BOOKMARK_LABEL = "outline_bookmark"
#: 造节点 level_source（来源如实：与命中锚点的 bookmark_outline 区分）
_OUTLINE_CREATED_SOURCE = "outline_created"


def _correct_outline_levels(
    pending: list[_PendingBlock],
    heading_indexes: Sequence[int],
    data: bytes,
    outline_reader: Callable[[bytes], Sequence[OutlineEntry]] | None,
) -> tuple[OutlineCorrection, str | None, set[int], int, list[str]]:
    """pending heading 序列 → 书签校正并写回 level/level_source（含骨架重建造节点）。

    返回 `(结果, 读取错误名, 锚点 pending 下标集合, 造节点数, 造节点标题样例)`——
    锚点下标供工单 B 复核门豁免面使用（书签锚点=确定性层级源，不让位于启发式门；
    骨架重建造节点与自身书签条目匹配成锚点，天然进豁免面）。

    **骨架重建（工单 A，推翻 T1-b「校正不造节点」，用户 2026-11-24 立项批准）**：
    书签可读且护栏（plan_skeleton_slots 内按原始匹配）通过 → 未命中顶级书签条目
    按「页码区间归界」插入新 pending heading 块，再走既有锚点钉正——造出来的节点
    标题=书签标题、页=dest+1，与自身条目精确匹配成锚点，后续内容由 heading 栈
    自然归属。护栏不过/无书签 → 计划恒空，本函数与旧路径逐字节等价。

    书签读取失败按「无书签」处理（不阻塞解析）——错误只留类型名，零内容回显。
    level_source 如实标注：锚点 `bookmark_outline`、造节点 `outline_created`、
    非锚点重校准 `outline_offset`、序列清洗改动项 `sequence_clean`；未改动项保留
    原有启发式来源（不伪造来源）。
    """
    items = [
        HeadingItem(
            title=pending[index].title or "",
            level=pending[index].level or 1,
            page=pending[index].page,
        )
        for index in heading_indexes
    ]
    outline: list[OutlineEntry] = []
    error: str | None = None
    if items:
        reader = outline_reader if outline_reader is not None else read_pdf_outline
        try:
            outline = list(reader(data))
        except Exception as exc:  # noqa: BLE001 —— 书签不可读不废解析（回退序列清洗）
            error = type(exc).__name__

    # 骨架重建（工单 A）：护栏过门才造节点；造块预置 level=书签归一层
    # （= 锚点 target_level，写回零漂移），插入后重取 heading 序列参与钉正
    created_count = 0
    created_sample: list[str] = []
    created_blocks: list[_PendingBlock] = []
    if outline:
        slots: list[SkeletonSlot] = plan_skeleton_slots(
            items, outline, [block.page for block in pending]
        )
        # 逆序插入：前位下标不漂移；同位插入按逆大纲序执行 → 序内保持大纲序
        for slot in reversed(slots):
            block = _PendingBlock(
                kind="heading",
                title=slot.title,
                label=_OUTLINE_BOOKMARK_LABEL,
                page=slot.page,
                level=slot.level,
                level_source=_OUTLINE_CREATED_SOURCE,
            )
            pending.insert(slot.insert_index, block)
            created_blocks.append(block)
        if created_blocks:
            heading_indexes = [i for i, block in enumerate(pending) if block.kind == "heading"]
            items = [
                HeadingItem(
                    title=pending[index].title or "",
                    level=pending[index].level or 1,
                    page=pending[index].page,
                )
                for index in heading_indexes
            ]
            created_count = len(created_blocks)
            created_sample = [slot.title for slot in slots[:5]]

    correction = correct_heading_levels(items, outline)
    anchor_slots = {anchor.index for anchor in correction.anchors}
    for slot, index in enumerate(heading_indexes):
        node = pending[index]
        level = correction.levels[slot]
        if level != (node.level or 1):
            node.level = level
            if slot in anchor_slots:
                node.level_source = "bookmark_outline"
            elif correction.path == CORRECTION_BOOKMARK:
                node.level_source = "outline_offset"
            else:
                node.level_source = "sequence_clean"
        elif slot in anchor_slots:
            node.level_source = "bookmark_outline"
    # 造节点来源如实回写（上方锚点分支会盖成 bookmark_outline；此处改判）
    if created_blocks:
        created_ids = {id(block) for block in created_blocks}
        for index, block in enumerate(pending):
            if id(block) in created_ids:
                block.level_source = _OUTLINE_CREATED_SOURCE
    anchor_indexes = {heading_indexes[slot] for slot in anchor_slots}
    return correction, error, anchor_indexes, created_count, created_sample


def _outline_correction_enabled(explicit: bool | None) -> bool:
    """校正总开关：显式注入优先（测试缝），否则读 `.env` 档 kb_settings（重启生效）。"""
    if explicit is not None:
        return explicit
    return kb_settings.pdf_outline_correction


def _heading_review_enabled(explicit: bool | None) -> bool:
    """复核层总开关（工单 B）：显式注入优先（测试缝），否则读 `.env` 档 kb_settings。

    false = 复核层整体旁路（零降级、Fix 2 高度回落不生效、不写 parse_meta 键）——
    完全等价旧管线的回退门。getattr 兜底：测试面 SimpleNamespace 假体可能缺属性。
    """
    if explicit is not None:
        return explicit
    return bool(getattr(kb_settings, "pdf_heading_review", True))


def _outline_meta(
    correction: OutlineCorrection,
    error: str | None,
    created_count: int = 0,
    created_sample: Sequence[str] | None = None,
) -> dict[str, Any]:
    """parse_meta.heading_outline（探针自检节读 correction_path / pressed 等留痕）。

    `unmatched` 截前 10 条、全量计数另给 `unmatched_count`（脏书签不撑爆 parse_meta）；
    `created`/`created_sample` = 工单 A 骨架重建造节点数与前 5 标题样例；
    `unmatched`/`duplicates`/`pressed_sample`/`created_sample` 里是书签与标题原文
    （不是敏感面），其余诊断字段零正文回显。
    """
    return {
        "correction_path": correction.path,
        "bookmarks": correction.outline_count,
        "anchors": len(correction.anchors),
        "unmatched": list(correction.unmatched[:10]),
        "unmatched_count": len(correction.unmatched),
        "duplicates": [[title, count] for title, count in correction.duplicate_titles[:5]],
        "pressed": correction.pressed,
        "pressed_sample": list(correction.pressed_titles[:5]),
        "level_jumps_before": correction.level_jumps_before,
        "level_jumps_after": correction.level_jumps_after,
        "created": created_count,
        "created_sample": list(created_sample or []),
        "error": error,
    }


# ---------------------------------------------------------------------------
# OCR 叠加
# ---------------------------------------------------------------------------


def _default_recognizer(pdf: Any) -> OcrRecognizer:
    """生产形态识别回调：paddle_engine 惰性单例 + pdfium 页渲染（重模型只在
    真实无文本层页出现时装载——惰性链的最后一环）。"""

    def _recognize(page_index: int) -> list[str]:
        engine = get_engine()
        image = render_page(pdf, page_index, scale=OCR_RENDER_SCALE)
        return recognize_lines(engine, image=image)

    return _recognize


def _node_page(node: IrNode) -> int | None:
    """节点页码（loc.page；url 源/未知页 → None）。"""
    loc = node.loc
    page = getattr(loc, "page", None) if loc is not None else None
    return page if isinstance(page, int) else None


def _ocr_compare_text(text: str) -> str:
    """OCR 去重比对归一：剔除全部空白 + casefold。

    理由：pdfium 文本层与 OCR 的行切分/空白不一致（ECMA 252 页文本层
    `第十一章 Promise与异步编程\\r\\n252` 对 OCR 逐行输出），归一后按**单行相等**
    判定重复。**口径取舍**：只用相等、不用子串——子串会误杀正文短行（文本层
    「第十一章 Promise与异步编程」时正文行「异步编程」、文本层「共 252 条」时正文
    「252」会被错丢，丢字不可逆）；代价是 OCR 把文本层两行并成一行时不再去重
    （可见重复行噪声，可接受）。
    """
    return "".join(text.split()).casefold()


def _page_text_line_set(text: str) -> set[str]:
    """页文本层原文 → 归一单行集合（OCR 去重比对面；空行剔除）。"""
    lines: set[str] = set()
    for raw_line in text.splitlines():
        normalized = _ocr_compare_text(raw_line)
        if normalized:
            lines.add(normalized)
    return lines


def _make_page_text_provider(
    data: bytes, page_texts: Sequence[str] | None
) -> tuple[Callable[[int], str], Any | None]:
    """OCR 去重用的页文本层来源 → `(provider, 需调用方关闭的 pdf 或 None)`。

    注入优先（单测缝，假字节 PDF 无法抽取）；否则 pdfium 抽取（句柄由调用方收尾）；
    抽取失败/非 PDF → 空串 = 去重降级为不去重（噪声治理面不得阻塞 OCR 叠加主链）。
    """
    if page_texts is not None:

        def _injected(page_index: int) -> str:
            return page_texts[page_index] if 0 <= page_index < len(page_texts) else ""

        return _injected, None
    try:
        pdf = open_pdf_bytes(data)
    except Exception:  # noqa: BLE001 —— 文本层不可读 → 去重降级（不阻塞叠加）
        return (lambda page_index: ""), None

    def _from_pdf(page_index: int) -> str:
        try:
            return page_text(pdf, page_index)
        except Exception:  # noqa: BLE001 —— 单页文本层读取失败按空处理
            return ""

    return _from_pdf, pdf


def _preorder_with_parents(document: IrNode) -> list[tuple[IrNode, IrNode, int]]:
    """文档序 pre-order 列表 `(node, parent, parent.children 下标)`（根不入选）。"""
    rows: list[tuple[IrNode, IrNode, int]] = []

    def walk(container: IrNode) -> None:
        for index, child in enumerate(container.children):
            rows.append((child, container, index))
            walk(child)

    walk(document)
    return rows


def _resolve_ocr_insertion(document: IrNode, page: int) -> tuple[IrNode, int]:
    """该页 OCR 行的插入位置 → `(父节点, children 下标)`（不追加树尾）。

    以「文档序第一个 `loc.page > page` 的节点 F」为页边界：
    - F 非标题 → 插 F 之前、同父级（F 所在标题子树内 → 继承该页 heading_path）；
    - F 是标题、prev 恰为 F 的父标题 P（F=P 首个子节点）→ 插 P.children[F 下标]
      之前（落到 P 子树末尾会越过 F 子树，本页文字排到后页之后）；
    - F 是标题、prev 与 F 同父（非标题）→ 紧贴 prev 之后、F 之前（不落父级末尾）；
    - F 是标题、prev 在更深标题子树 → 落 prev 所在子树末尾（继承上一标题栈）；
    - 无后续页节点（末页/首末页号不可比）→ 文档序末节点的活动标题栈末尾。
    页号未知（None）的节点不参与页边界比较（不改变现有文档序关系）。
    """
    rows = _preorder_with_parents(document)
    for position, (node, parent, index) in enumerate(rows):
        node_page = _node_page(node)
        if node_page is None or node_page <= page:
            continue
        if node.type is not IrNodeType.HEADING:
            return parent, index
        if position == 0:
            return document, 0
        prev, prev_parent, _prev_index = rows[position - 1]
        if prev is parent:
            # F 是父标题 P 的首个子节点：紧贴 P 之后、F 之前（P.children[index]）
            return parent, index
        if prev.type is IrNodeType.HEADING:
            return prev, len(prev.children)
        if prev_parent is parent:
            # prev 与 F 同父：紧贴 prev 之后、F 之前插入。落到「父 children 末尾」
            # 会越过 F 及其子树（后页内容），本页文字排错页。
            return parent, index
        return prev_parent, len(prev_parent.children)
    if not rows:
        return document, 0
    last, last_parent, _last_index = rows[-1]
    if last.type is IrNodeType.HEADING:
        return last, len(last.children)
    return last_parent, len(last_parent.children)


def _insert_ocr_lines(
    builder: BlockTreeBuilder,
    lines: Sequence[str],
    *,
    page: int,
    doc_kind: str,
    file_object_key: str | None,
    source_url: str | None,
) -> None:
    """一页 OCR 行按页码插回 IR 对应位置（行序保持；node_id 走 builder 序号）。

    语义：OCR 行是该页正文的内容节点——挂在当页活动标题子树内（heading_path 正确
    继承），而不是文档树尾（旧形态：顺序错、标题归属错）。
    """
    parent, index = _resolve_ocr_insertion(builder.document, page)
    nodes: list[IrNode] = []
    for line in lines:
        if not line.strip():
            continue
        nodes.append(
            IrNode(
                id=builder.next_block_id(),
                parent_id=parent.id,
                type=IrNodeType.PARAGRAPH,
                text=line,
                loc=make_loc(
                    doc_kind,
                    page=page,
                    file_object_key=file_object_key,
                    source_url=source_url,
                ),
                meta={"raw_type": "ocr_line", "ocr_page": page},
            )
        )
    parent.children[index:index] = nodes


def _run_ocr_overlay(
    *,
    data: bytes,
    builder: BlockTreeBuilder,
    settings: NormalizerSettings,
    text_layer_pages: Sequence[bool] | None,
    page_texts: Sequence[str] | None,
    ocr_recognizer: OcrRecognizer | None,
    page_count: int | None,
    file_object_key: str | None,
    doc_kind: str,
    source_url: str | None,
) -> tuple[list[int], int | None]:
    """无文本层/稀疏页 OCR 叠加 → (实际识别页号 1 基列表, 校正后页数)。

    `ocr_enabled=false` → 零触发（回退门）；文本层充足 → 零触发；引擎枚举外
    （rapidocr 未实装）→ 显式拒绝不静默。OCR 行先按该页**文本层原文去重**（页眉/
    页码/章名等重复行丢弃、图像正文保留），再**按页插回树内对应位置**（见
    `_insert_ocr_lines`），不追加树尾。
    """
    if not settings.ocr_enabled:
        return [], page_count
    if settings.ocr_engine != "paddleocr":
        raise UnsupportedTypeError(
            f"ocr_engine={settings.ocr_engine} 未实装（PRD v1.34 钉 paddleocr；"
            "rapidocr 留 M3+ 重场景复核线）"
        )
    pdf_for_flags: Any | None = None
    if text_layer_pages is None:
        probe = open_pdf_bytes(data)
        pdf_for_flags = probe
        text_layer_pages = page_text_flags(probe)
        page_count = page_count or len(probe)
    missing = [i for i, ok in enumerate(text_layer_pages) if not ok]
    if page_count is not None:
        missing = [i for i in missing if i < page_count]
    if not missing:
        if pdf_for_flags is not None:
            pdf_for_flags.close()
        return [], page_count
    pdf_for_recognizer: Any | None = None
    if ocr_recognizer is None:
        pdf_for_recognizer = open_pdf_bytes(data)
        recognizer = _default_recognizer(pdf_for_recognizer)
    else:
        recognizer = ocr_recognizer
    page_text_provider, pdf_for_text = _make_page_text_provider(data, page_texts)
    try:
        for page_index in missing:
            layer_lines = _page_text_line_set(page_text_provider(page_index))
            lines = []
            for raw_line in recognizer(page_index):
                line = raw_line.strip()
                if not line:
                    continue
                if _ocr_compare_text(line) in layer_lines:
                    continue  # 与文本层单行相同（页眉/页码/章名）——OCR 重复行丢弃
                lines.append(line)
            if not lines:
                continue
            _insert_ocr_lines(
                builder,
                lines,
                page=page_index + 1,
                doc_kind=doc_kind,
                file_object_key=file_object_key,
                source_url=source_url,
            )
    finally:
        for pdf in (pdf_for_flags, pdf_for_recognizer, pdf_for_text):
            if pdf is not None:
                pdf.close()
    return [i + 1 for i in missing], page_count


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def normalize_with_docling(
    data: bytes,
    *,
    doc_kind: str = "upload",
    file_object_key: str | None = None,
    source_url: str | None = None,
    settings: NormalizerSettings | None = None,
    converter: Any | None = None,
    font_sampler: Any | None = None,
    text_layer_pages: Sequence[bool] | None = None,
    page_texts: Sequence[str] | None = None,
    ocr_recognizer: OcrRecognizer | None = None,
    page_count: int | None = None,
    outline_reader: Callable[[bytes], Sequence[OutlineEntry]] | None = None,
    outline_correction: bool | None = None,
    heading_review: bool | None = None,
) -> NormalizationResult:
    """docling pdf 归一化：bytes → document 树 + parse_meta（启发式/OCR/大纲留痕）。

    注入面（单测全 mock 的缝，生产调用方零传）：converter（DocumentConverter 假体）、
    font_sampler（字号采样假体）、text_layer_pages（文本层判定结果直给——免
    pypdfium2）、page_texts（OCR 去重的页文本层原文——免 pdfium 抽页；缺省按
    `data` 逐页抽取、抽取失败去重降级）、ocr_recognizer（识别假体——真模型绝不
    加载）、outline_reader（书签读取假体——免真实 PDF）、outline_correction（校正
    总开关：None=读 .env 档 `KB_PDF_OUTLINE_CORRECTION`，false=完全等价旧管线）、
    heading_review（复核层总开关，工单 B：None=读 .env 档
    `KB_PDF_HEADING_REVIEW`；false=复核门/降级/高度回落全部旁路，等价旧管线）。
    """
    settings = settings or NormalizerSettings()
    review_enabled = _heading_review_enabled(heading_review)
    conv = converter if converter is not None else _get_converter()
    try:
        from docling_core.types.io import DocumentStream

        stream = DocumentStream(stream=BytesIO(data), name="doc.pdf")
        result = conv.convert(stream)
    except NormalizationError:
        raise
    except Exception as exc:
        raise NormalizationError(
            "docling pdf 解析失败",
            detail=f"{type(exc).__name__}: {exc}",
            original_exception=exc,
        ) from exc
    status_name = str(getattr(result.status, "name", None) or result.status)
    if "SUCCESS" not in status_name:
        raise NormalizationError("docling 转换未成功", detail=f"status={status_name}")
    ddoc = result.document

    pending = _collect_blocks(ddoc)
    _doc, builder = make_document_node(
        "url" if doc_kind == "url" else "upload",
        file_object_key=file_object_key,
        source_url=source_url,
        raw_type="docling.document",
    )

    # heading 启发式（先分级后装配——层级驱动嵌套；不伪造层级，留痕见 meta）
    # 顺序硬约束：字号提升 → 重新取 heading_indexes → 分级 → 书签校正。提升必须在
    # 书签校正之前进入 pending 序列，否则新 heading 不参与锚点/偏移重校准。
    inferred = 0
    fallback_flat = 0
    promoted = 0
    body_font_size: float | None = None
    font_sampler_owned = False
    font_pdf: Any | None = None
    heading_indexes = [i for i, p in enumerate(pending) if p.kind == "heading"]
    if heading_indexes:
        if font_sampler is None:
            try:
                font_pdf = open_pdf_bytes(data)
                font_sampler = CharFontSampler(font_pdf)
                font_sampler_owned = True
            except Exception:  # noqa: BLE001 —— 字体数据不可得走 fallback_flat（不阻塞解析）
                font_sampler = None
        if font_sampler is not None:
            body_font_size = getattr(font_sampler, "body_font_size", None)
            promoted = _promote_font_headings(
                pending, font_sampler, body_font_size, allow_height_fallback=review_enabled
            )
            if promoted:
                heading_indexes = [i for i, p in enumerate(pending) if p.kind == "heading"]
        entries: list[tuple[str, tuple[float, bool] | None]] = []
        for i in heading_indexes:
            entry = pending[i]
            # 生产路径优先 bbox 采样（同页同文多处时「首现搜索」会采错）；无 bbox
            # 或注入假体无 bbox 面 → 旧文本搜索兜底
            stat = _bbox_font_stat(font_sampler, entry.page, entry.bbox)
            if stat is None and font_sampler is not None:
                page_idx = (entry.page or 1) - 1
                try:
                    stat = font_sampler.text_font_stats(page_idx, entry.title or "")
                except Exception:  # noqa: BLE001 —— 单条采样失败按数据不可得处理
                    stat = None
            entries.append((entry.title or "", stat))
        level_map = assign_heading_levels(entries, body_font_size)
        for title, (level, source) in level_map.items():
            if source == "heuristic_font_size":
                inferred += 1
            else:
                fallback_flat += 1
            for i in heading_indexes:
                if (pending[i].title or "") == title:
                    pending[i].level = level
                    pending[i].level_source = source

    # 书签大纲校正（确定性层级钉正；**装配前介入**——层级在 BlockTreeBuilder.add
    # 的 heading 栈里即时物化 children，装配后改 level 须重挂父子）。总开关关闭时
    # 完全跳过（等价旧管线：原始启发式 level、不写 parse_meta.heading_outline）
    correction: OutlineCorrection | None = None
    outline_error: str | None = None
    anchor_indexes: set[int] = set()
    created_count = 0
    created_sample: list[str] = []
    if _outline_correction_enabled(outline_correction):
        correction, outline_error, anchor_indexes, created_count, created_sample = (
            _correct_outline_levels(pending, heading_indexes, data, outline_reader)
        )

    # 多信号复核层（工单 B；**校正后**——锚点豁免面依赖校正产出；**装配前**——
    # 降级即时改变 kind/label，装配段零特判）。关闭态整体旁路（等价旧管线）
    review_counts: dict[str, int] | None = None
    if review_enabled and font_sampler is not None:
        review_counts = review_blocks(pending, sampler=font_sampler, anchors=anchor_indexes)

    # 采样完成即释放（pdfium 句柄零滞留——每次归一化泄漏的旧缺陷；复核门之后）
    if font_sampler_owned and font_sampler is not None:
        try:
            font_sampler.close()
        finally:
            if font_pdf is not None:
                font_pdf.close()

    # 统一装配（文档序；列表连续项聚合单节点；heading 栈嵌套经 builder）
    i = 0
    while i < len(pending):
        entry = pending[i]
        loc = make_loc(
            "url" if doc_kind == "url" else "upload",
            page=entry.page,
            file_object_key=file_object_key,
            source_url=source_url,
        )
        if entry.kind == "heading":
            builder.add_node(
                IrNodeType.HEADING,
                level=entry.level or 1,
                title=entry.title,
                loc=loc,
                meta={
                    "raw_type": entry.label,
                    "level_source": entry.level_source or "fallback_flat",
                },
            )
        elif entry.kind == "table":
            builder.add_node(
                IrNodeType.TABLE,
                loc=loc,
                meta={
                    "raw_type": entry.label,
                    "rows": entry.rows,
                    "col_count": entry.col_count,
                },
            )
        elif entry.kind == "code":
            builder.add_node(
                IrNodeType.CODE, text=entry.text, loc=loc, meta={"raw_type": entry.label}
            )
        elif entry.kind == "picture":
            builder.add_node(
                IrNodeType.IMAGE,
                loc=loc,
                meta={"raw_type": entry.label, "placeholder": True},
            )
        elif entry.kind == "list_item":
            texts = [str(entry.text or "")]
            j = i + 1
            while j < len(pending) and pending[j].kind == "list_item":
                texts.append(str(pending[j].text or ""))
                j += 1
            builder.add_node(
                IrNodeType.LIST,
                text="\n".join(texts),
                loc=loc,
                meta={"raw_type": entry.label},
            )
            i = j
            continue
        else:
            builder.add_node(
                IrNodeType.PARAGRAPH,
                text=entry.text,
                loc=loc,
                meta={"raw_type": entry.label},
            )
        i += 1

    # OCR 叠加（文本层缺失判定后；parser 记末段实际执行组件）
    ocr_pages, page_count_eff = _run_ocr_overlay(
        data=data,
        builder=builder,
        settings=settings,
        text_layer_pages=text_layer_pages,
        page_texts=page_texts,
        ocr_recognizer=ocr_recognizer,
        page_count=page_count,
        file_object_key=file_object_key,
        doc_kind="url" if doc_kind == "url" else "upload",
        source_url=source_url,
    )

    tables, code_blocks = _count_types(builder.document)
    if page_count_eff is None:
        page_count_eff = max((p.page or 0 for p in pending), default=0) or None
    parse_meta: dict[str, Any] = {
        "pages": page_count_eff,
        "tables": tables,
        "code_blocks": code_blocks,
        "parser": "paddleocr" if ocr_pages else "docling",
        # 校正前计数（字号启发式判定面）；与节点 meta.level_source（校正后来源）
        # 口径不同——书签校正可能改级并覆写来源，两处数字不可直接互校
        "heading_heuristic": {
            "inferred": inferred,
            "fallback_flat": fallback_flat,
            "body_font_size": body_font_size,
            # 字号提升条数（docling 判成 text/formula 的标题样式行补偿面）
            "promoted": promoted,
        },
    }
    if correction is not None:
        parse_meta["heading_outline"] = _outline_meta(
            correction, outline_error, created_count, created_sample
        )
    if review_counts is not None:
        parse_meta["heading_review"] = review_counts
    parse_meta["ocr"] = {
        "engine": settings.ocr_engine if settings.ocr_enabled else None,
        "pages": ocr_pages,
    }
    return NormalizationResult(document=builder.document, parse_meta=parse_meta)


def _count_types(root: Any) -> tuple[int, int]:
    tables = 0
    code_blocks = 0
    stack: list[Any] = [*root.children]
    while stack:
        node = stack.pop()
        if node.type is IrNodeType.TABLE:
            tables += 1
        elif node.type is IrNodeType.CODE:
            code_blocks += 1
        stack.extend(node.children)
    return tables, code_blocks
