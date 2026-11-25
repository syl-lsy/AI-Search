"""B17 主线一：docling 适配器单元测试（pdf 结构化 → 树 IR；真模型路径全 mock）。

pdf 通道的 docling 转换是模型路径（TableFormer/版面模型，探针 §2.2 暖跑 3.9s/页）
——单测全 mock：DocumentConverter 假体注入；DoclingDocument 用 docling_core 内存
构造（零模型，同款 API 形态于本会话实测钉定：iterate_items→(item, level)、
TableItem.data.grid/num_rows/num_cols、TableCell.column_header、SectionHeaderItem）。

消费探针发现（m3-probe-normalization-20261122.md §2.2/§3）：
- pdf 标题层级全判 level1（20/16/13pt 三级样式未分级）+ 1 处正文误判——适配器须
  启发式兜底（字号/加粗）并在 parse_meta 留痕，不伪造层级；
- 表格逐格精确 + num_rows/num_cols 编程取矩阵；代码块独立 code 条目；
- 条目级页码溯源 item.prov[0].page_no。
OCR 通道（ADR-018③ PaddleOCR 钉定 + ARCH「Docling+PaddleOCR 进程内」）：文本层
缺失判定后叠加；OCR 真模型绝不在单测加载（假体识别器注入）。
"""

from types import SimpleNamespace
from typing import Any

import pytest
from docling_core.types.doc import DocItemLabel, DoclingDocument
from docling_core.types.doc.document import TableData, TableCell

from app.rag.ir_schema import IrNode, IrNodeType, serialize_ir
from app.rag.normalizers import docling_adapter, paddle_engine
from app.rag.normalizers.docling_adapter import (
    assign_heading_levels,
    normalize_with_docling,
)
from app.rag.normalizers.heading_outline import OutlineEntry
from app.rag.normalizers.registry import NormalizerSettings


def _settings(**overrides: Any) -> NormalizerSettings:
    base: dict[str, Any] = {
        "pdf_engine": "docling",
        "ocr_enabled": True,
        "ocr_engine": "paddleocr",
    }
    base.update(overrides)
    return NormalizerSettings(**base)


def _make_docling_document() -> DoclingDocument:
    """内存构造 DoclingDocument（零模型）：两级标题误判全 level1 的真实形态 +
    表格 + 代码 + 列表 + 逐条页码 prov。"""
    doc = DoclingDocument(name="sample")

    def _prov(text_item: Any, page: int) -> None:
        # add_text 返回值 prov 列表在内存构造下为空——手工补（API 形态：
        # item.prov[0].page_no，探针 §2.2 实测读取面）
        from docling_core.types.doc.document import ProvenanceItem
        from docling_core.types.doc.base import BoundingBox, Size
        text_item.prov.append(
            ProvenanceItem(
                page_no=page,
                bbox=BoundingBox(l=0, t=0, r=100, b=20, page_size=Size(width=612, height=792)),
                charspan=(0, 5),
            )
        )

    h1 = doc.add_text(label=DocItemLabel.SECTION_HEADER, text="行程安排")
    _prov(h1, 1)
    h2 = doc.add_text(label=DocItemLabel.SECTION_HEADER, text="行前准备")
    _prov(h2, 1)
    p1 = doc.add_text(label=DocItemLabel.TEXT, text="到达：10月2日 停留2天")
    _prov(p1, 1)
    li = doc.add_list_item(text="必玩打卡：栈桥、八大关")
    _prov(li, 1)
    td = TableData(
        num_rows=2,
        num_cols=3,
        table_cells=[
            TableCell(text=col, start_row_offset_idx=0, end_row_offset_idx=1, start_col_offset_idx=j, end_col_offset_idx=j + 1, column_header=True)
            for j, col in enumerate(["模型", "参数量", "上下文"])
        ]
        + [
            TableCell(text=col, start_row_offset_idx=1, end_row_offset_idx=2, start_col_offset_idx=j, end_col_offset_idx=j + 1)
            for j, col in enumerate(["Qwen3", "4B", "32k"])
        ],
    )
    tbl = doc.add_table(data=td)
    _prov(tbl, 2)
    code = doc.add_code(text="def plan():\n    pass")
    _prov(code, 2)
    return doc


class _FakeFontSampler:
    """字号/加粗采样假体：按文本关键词返回设定字号（heading 20/16 vs body 11）。"""

    def __init__(self, sizes_by_keyword: dict[str, tuple[float, bool]]) -> None:
        self._sizes = sizes_by_keyword
        self.body_font_size = 11.0
        self.calls = 0

    def text_font_stats(self, page_index: int, text: str) -> tuple[float, bool] | None:
        self.calls += 1
        for keyword, stats in self._sizes.items():
            if keyword in text:
                return stats
        return None


def _fake_converter(doc: DoclingDocument) -> Any:
    return SimpleNamespace(convert=lambda *a, **k: SimpleNamespace(status="SUCCESS", document=doc))


# ---------------------------------------------------------------------------
# 基础映射
# ---------------------------------------------------------------------------


def _run(doc: DoclingDocument, sampler: Any, **kwargs: Any) -> Any:
    return normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=sampler,
        text_layer_pages=[True],
        ocr_recognizer=None,
        **kwargs,
    )


def test_mapping_headings_paragraphs_lists_tables_code() -> None:
    doc = _make_docling_document()
    sampler = _FakeFontSampler({"行程安排": (20.0, True), "行前准备": (16.0, False)})
    result = _run(doc, sampler)

    assert result.parse_meta["pages"] == 2
    assert result.parse_meta["tables"] == 1
    assert result.parse_meta["code_blocks"] == 1
    assert result.parse_meta["parser"] == "docling"

    root = result.document
    assert root.type is IrNodeType.DOCUMENT
    h1 = root.children[0]
    assert h1.type is IrNodeType.HEADING and h1.title == "行程安排"
    # 二级标题嵌套在一级之下（heading 栈同 markdown 形态）
    h2 = next(c for c in h1.children if c.type is IrNodeType.HEADING)
    assert h2.title == "行前准备"
    # 表格矩阵逐格精确（grid 取值、表头列由 column_header 标记）
    tables: list[IrNode] = []
    stack = [*root.children]
    while stack:
        cur = stack.pop()
        if cur.type is IrNodeType.TABLE:
            tables.append(cur)
        stack.extend(cur.children)
    assert tables[0].meta["rows"] == [["模型", "参数量", "上下文"], ["Qwen3", "4B", "32k"]]
    assert tables[0].meta["col_count"] == 3
    # 代码块独立节点整块保留
    codes: list[IrNode] = []
    stack = [*root.children]
    while stack:
        cur = stack.pop()
        if cur.type is IrNodeType.CODE:
            codes.append(cur)
        stack.extend(cur.children)
    assert codes[0].text == "def plan():\n    pass"
    # 列表单节点
    lists = []
    stack = [*root.children]
    while stack:
        cur = stack.pop()
        if cur.type is IrNodeType.LIST:
            lists.append(cur)
        stack.extend(cur.children)
    assert lists and "栈桥" in (lists[0].text or "")


def test_block_loc_pages_from_prov() -> None:
    """条目级页码溯源：loc.page 来自 item.prov[0].page_no（探针 §2.2 判定面）。"""
    doc = _make_docling_document()
    sampler = _FakeFontSampler({})
    result = _run(doc, sampler)
    flat: list[IrNode] = []
    stack = [*result.document.children]
    while stack:
        cur = stack.pop()
        flat.append(cur)
        stack.extend(cur.children)
    pages = {n.loc.page for n in flat if n.loc is not None}
    assert pages == {1, 2}


# ---------------------------------------------------------------------------
# heading 启发式兜底（不伪造层级）
# ---------------------------------------------------------------------------


def test_heading_levels_from_font_heuristic() -> None:
    """字号聚类分级：20pt→1 / 16pt→2；留痕 parse_meta + node.meta.level_source。"""
    doc = _make_docling_document()
    sampler = _FakeFontSampler({"行程安排": (20.0, True), "行前准备": (16.0, False)})
    result = _run(doc, sampler)
    headings: list[IrNode] = []

    def _collect(node: IrNode) -> None:
        if node.type is IrNodeType.HEADING:
            headings.append(node)
        for c in node.children:
            _collect(c)

    _collect(result.document)
    by_title = {h.title: h for h in headings}
    assert by_title["行程安排"].level == 1
    assert by_title["行前准备"].level == 2
    assert by_title["行程安排"].meta["level_source"] == "heuristic_font_size"
    hh = result.parse_meta["heading_heuristic"]
    assert hh["inferred"] == 2
    assert hh["fallback_flat"] == 0
    assert hh["body_font_size"] == 11.0


def test_heading_fallback_flat_when_no_font_data() -> None:
    """字体数据不可得 → level=1 兜底 + fallback_flat 留痕（不伪造层级，探针 §3 兜底协议）。"""
    doc = _make_docling_document()
    sampler = _FakeFontSampler({})  # 全部返回 None
    result = _run(doc, sampler)
    headings: list[IrNode] = []

    def _collect(node: IrNode) -> None:
        if node.type is IrNodeType.HEADING:
            headings.append(node)
        for c in node.children:
            _collect(c)

    _collect(result.document)
    assert all(h.level == 1 for h in headings)
    assert all(h.meta["level_source"] == "fallback_flat" for h in headings)
    assert result.parse_meta["heading_heuristic"]["fallback_flat"] == len(headings)


def test_assign_heading_levels_pure_clustering() -> None:
    """纯函数面：字号降序聚类（同字号同级、加粗优先、上限 9 级）。"""
    stats = [
        ("甲", (20.0, True)),
        ("乙", (19.5, False)),   # 与 20 同簇（容差内）
        ("丙", (14.0, False)),
        ("丁", (13.0, False)),
        ("戊", (11.0, True)),    # ≈正文字号且加粗——仍按字号簇末级
    ]
    levels = assign_heading_levels(stats, body_font_size=11.0)
    assert levels["甲"] == (1, "heuristic_font_size")
    assert levels["乙"] == (1, "heuristic_font_size")
    assert levels["丙"] == (2, "heuristic_font_size")
    assert levels["丁"] == (3, "heuristic_font_size")
    assert levels["戊"][0] >= 3


# ---------------------------------------------------------------------------
# OCR 叠加（文本层缺失判定后叠加；真模型绝不加载）
# ---------------------------------------------------------------------------


def test_ocr_overlay_inserts_paragraphs_for_textless_pages() -> None:
    doc = _make_docling_document()
    sampler = _FakeFontSampler({})
    calls: list[int] = []

    def _recognizer(page_index: int) -> list[str]:
        calls.append(page_index)
        return ["第三页扫描行甲", "第三页扫描行乙"]

    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=sampler,
        text_layer_pages=[True, True, False],
        ocr_recognizer=_recognizer,
        page_count=3,
        settings=_settings(ocr_enabled=True),
    )
    assert calls == [2]  # 只对无文本层页触发
    ocr_paras = []
    stack = [*result.document.children]
    while stack:
        cur = stack.pop()
        if cur.meta.get("raw_type") == "ocr_line":
            ocr_paras.append(cur)
        stack.extend(cur.children)
    assert len(ocr_paras) == 2
    assert all(p.loc is not None and p.loc.page == 3 for p in ocr_paras)
    # 多组件流水 parse_meta.parser 记末段实际执行组件（data-model §1.7）
    assert result.parse_meta["parser"] == "paddleocr"
    assert result.parse_meta["ocr"]["engine"] == "paddleocr"
    assert result.parse_meta["ocr"]["pages"] == [3]


# ---------------------------------------------------------------------------
# OCR 行按页插入（不追加树尾——按 loc.page 定拼接点、继承当页标题栈）
# ---------------------------------------------------------------------------


def _preorder(document: IrNode) -> list[tuple[IrNode, IrNode]]:
    """文档序 pre-order 列表（node, parent）；根节点不入选。"""
    rows: list[tuple[IrNode, IrNode]] = []

    def walk(node: IrNode, parent: IrNode) -> None:
        rows.append((node, parent))
        for child in node.children:
            walk(child, node)

    for child in document.children:
        walk(child, document)
    return rows


def _make_chaptered_document() -> DoclingDocument:
    """两章跨页形态：第一章（page1）+ 第二章（page4），page2/3 无文本块——OCR 插入定位面。"""
    from docling_core.types.doc.base import BoundingBox, Size
    from docling_core.types.doc.document import ProvenanceItem

    doc = DoclingDocument(name="chaptered")

    def _add(text: str, page: int, *, header: bool) -> None:
        if header:
            item = doc.add_text(label=DocItemLabel.SECTION_HEADER, text=text)
        else:
            item = doc.add_text(label=DocItemLabel.TEXT, text=text)
        item.prov.append(
            ProvenanceItem(
                page_no=page,
                bbox=BoundingBox(l=0, t=0, r=100, b=20, page_size=Size(width=612, height=792)),
                charspan=(0, len(text)),
            )
        )

    _add("第一章 起点", 1, header=True)
    _add("第一章正文", 1, header=False)
    _add("第二章 后段", 4, header=True)
    _add("第二章正文", 4, header=False)
    return doc


def test_ocr_lines_inserted_at_page_position_under_active_heading() -> None:
    """OCR 行按页码插回该页位置（第一章内容之后、第二章标题之前），父节点=当页标题。"""
    doc = _make_chaptered_document()
    sampler = _FakeFontSampler({"第一章": (20.0, True), "第二章": (20.0, False)})
    calls: list[int] = []

    def _recognizer(page_index: int) -> list[str]:
        calls.append(page_index)
        return [f"p{page_index + 1} OCR 行"]

    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=sampler,
        text_layer_pages=[True, False, False, True],
        ocr_recognizer=_recognizer,
        page_count=4,
        settings=_settings(ocr_enabled=True),
    )
    assert calls == [1, 2]
    rows = _preorder(result.document)
    assert [n.text or n.title for n, _p in rows] == [
        "第一章 起点",
        "第一章正文",
        "p2 OCR 行",
        "p3 OCR 行",
        "第二章 后段",
        "第二章正文",
    ]
    parents = {n.text: (n, p) for n, p in rows if n.text}
    for line, page in (("p2 OCR 行", 2), ("p3 OCR 行", 3)):
        node, parent = parents[line]
        assert node.meta["raw_type"] == "ocr_line"
        assert getattr(node.loc, "page", None) == page
        assert node.parent_id == parent.id
        assert parent.type is IrNodeType.HEADING and parent.title == "第一章 起点"
    # 非树尾：OCR 行之后仍有内容节点
    assert rows[-1][0].text == "第二章正文"


def test_ocr_lines_page_order_for_fully_scanned_document() -> None:
    """全页扫描件（docling 零文本块）：OCR 行按页序就位、全部挂文档根。"""
    doc = DoclingDocument(name="fully-scanned")

    def _recognizer(page_index: int) -> list[str]:
        return [f"scan-p{page_index + 1}-甲", f"scan-p{page_index + 1}-乙"]

    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=_FakeFontSampler({}),
        text_layer_pages=[False, False, False],
        ocr_recognizer=_recognizer,
        page_count=3,
        settings=_settings(ocr_enabled=True),
    )
    rows = _preorder(result.document)
    assert [n.text for n, _p in rows] == [
        "scan-p1-甲",
        "scan-p1-乙",
        "scan-p2-甲",
        "scan-p2-乙",
        "scan-p3-甲",
        "scan-p3-乙",
    ]
    assert all(parent is result.document for _n, parent in rows)
    assert all(n.parent_id == parent.id for n, parent in rows)


def test_ocr_tail_page_inserted_under_last_heading() -> None:
    """末页 OCR（无后续页锚点）：落在最后活动标题子树末尾，不跨出标题。"""
    doc = _make_chaptered_document()

    def _recognizer(page_index: int) -> list[str]:
        assert page_index == 4
        return ["末页扫描行"]

    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=_FakeFontSampler({"第一章": (20.0, True), "第二章": (20.0, False)}),
        text_layer_pages=[True, True, True, True, False],
        ocr_recognizer=_recognizer,
        page_count=5,
        settings=_settings(ocr_enabled=True),
    )
    rows = _preorder(result.document)
    node, parent = rows[-1]
    assert node.text == "末页扫描行" and node.meta["raw_type"] == "ocr_line"
    assert node.parent_id == parent.id
    assert parent.type is IrNodeType.HEADING and parent.title == "第二章 后段"


# ---------------------------------------------------------------------------
# OCR 行与文本层去重（P1-A：稀疏页重新进 OCR，噪声改在合并环节治理）
# ---------------------------------------------------------------------------


def test_ocr_lines_duplicating_text_layer_are_dropped() -> None:
    """三页「图像正文 + 页码戳」：正文行全部找回、页码戳不重复出现。"""
    doc = DoclingDocument(name="sparse-pages")

    def _recognizer(page_index: int) -> list[str]:
        return [
            f"图像正文第{page_index + 1}页甲",
            str(page_index + 1),  # 页码戳（文本层已有）
            f"图像正文第{page_index + 1}页乙",
        ]

    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=_FakeFontSampler({}),
        text_layer_pages=[False, False, False],
        page_texts=["1", "2", "3"],
        ocr_recognizer=_recognizer,
        page_count=3,
        settings=_settings(ocr_enabled=True),
    )
    rows = _preorder(result.document)
    texts = [n.text for n, _p in rows]
    assert texts == [
        "图像正文第1页甲",
        "图像正文第1页乙",
        "图像正文第2页甲",
        "图像正文第2页乙",
        "图像正文第3页甲",
        "图像正文第3页乙",
    ]
    assert all(n.parent_id == parent.id for n, parent in rows)


def test_ocr_merged_text_layer_lines_are_kept() -> None:
    """两行并一行不再去重（口径=单行相等）：OCR「252 第十一章…」vs 文本层两行 → 保留。

    取舍：丢字不可逆优先——合并行形态的重复噪声可见、可清洗，接受。
    """
    doc = DoclingDocument(name="sparse-merged")

    def _recognizer(page_index: int) -> list[str]:
        return ["252 第十一章 Promise与异步编程"]  # OCR 把文本层两行并成一行

    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=_FakeFontSampler({}),
        text_layer_pages=[False],
        page_texts=["252\n第十一章 Promise与异步编程"],
        ocr_recognizer=_recognizer,
        page_count=1,
        settings=_settings(ocr_enabled=True),
    )
    texts = [n.text for n, _p in _preorder(result.document)]
    assert texts == ["252 第十一章 Promise与异步编程"]


def test_ocr_body_short_lines_not_falsely_deduplicated() -> None:
    """正文短行不被误杀：子串口径会丢「异步编程」/「252」，单位相等口径保留。"""
    doc = DoclingDocument(name="sparse-short")

    def _recognizer(page_index: int) -> list[str]:
        return ["异步编程"] if page_index == 0 else ["252"]

    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=_FakeFontSampler({}),
        text_layer_pages=[False, False],
        page_texts=["第十一章 Promise与异步编程", "共 252 条记录"],
        ocr_recognizer=_recognizer,
        page_count=2,
        settings=_settings(ocr_enabled=True),
    )
    rows = _preorder(result.document)
    assert [n.text for n, _p in rows] == ["异步编程", "252"]
    assert all(n.parent_id == parent.id for n, parent in rows)


# ---------------------------------------------------------------------------
# 页边界标题形态（P1-B：prev 与标题 F 同父时紧贴 prev 插入，不落父级末尾）
# ---------------------------------------------------------------------------


def _make_nested_boundary_document() -> DoclingDocument:
    """H1(L1,p1) > [p1(p1), H2(L2,p2) > [p2(p2)]]，H3(L1,p4)——深层子树页边界。"""
    from docling_core.types.doc.base import BoundingBox, Size
    from docling_core.types.doc.document import ProvenanceItem

    doc = DoclingDocument(name="nested-boundary")

    def _add(text: str, page: int, *, header: bool) -> None:
        item = doc.add_text(
            label=DocItemLabel.SECTION_HEADER if header else DocItemLabel.TEXT, text=text
        )
        item.prov.append(
            ProvenanceItem(
                page_no=page,
                bbox=BoundingBox(l=0, t=0, r=100, b=20, page_size=Size(width=612, height=792)),
                charspan=(0, len(text)),
            )
        )

    _add("第一章 总纲", 1, header=True)
    _add("第一章正文", 1, header=False)
    _add("第一节 细节", 2, header=True)
    _add("第一节正文", 2, header=False)
    _add("第二章 后章", 4, header=True)
    _add("第二章正文", 4, header=False)
    return doc


def test_ocr_boundary_heading_after_deep_subtree_keeps_heading_path() -> None:
    """形态①：H1 > [p1, H2 > [p2]]，H3 在后页——OCR 行落 p2 之后 H3 之前，轨迹 [H1,H2]。"""
    doc = _make_nested_boundary_document()

    def _recognizer(page_index: int) -> list[str]:
        assert page_index == 2  # 第 3 页（0 基）
        return ["第三页扫描行"]

    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=_FakeFontSampler(
            {"第一章": (20.0, True), "第一节": (18.0, False), "第二章": (20.0, False)}
        ),
        text_layer_pages=[True, True, False, True],
        ocr_recognizer=_recognizer,
        page_count=4,
        settings=_settings(ocr_enabled=True),
    )
    rows = _preorder(result.document)
    assert [n.text or n.title for n, _p in rows] == [
        "第一章 总纲",
        "第一章正文",
        "第一节 细节",
        "第一节正文",
        "第三页扫描行",
        "第二章 后章",
        "第二章正文",
    ]
    node, parent = rows[4]
    assert node.parent_id == parent.id
    assert parent.title == "第一节 细节"
    # 层级轨迹 [H1, H2]：父链 H2 → H1
    assert parent.parent_id == rows[0][0].id


def _make_sibling_boundary_document() -> DoclingDocument:
    """H1(L1,p1) > [p1(p1), 正文段(p2)]，H2(L2,p4) > [p2(p4)]——prev 与标题同父形态。"""
    from docling_core.types.doc.base import BoundingBox, Size
    from docling_core.types.doc.document import ProvenanceItem

    doc = DoclingDocument(name="sibling-boundary")

    def _add(text: str, page: int, *, header: bool) -> None:
        item = doc.add_text(
            label=DocItemLabel.SECTION_HEADER if header else DocItemLabel.TEXT, text=text
        )
        item.prov.append(
            ProvenanceItem(
                page_no=page,
                bbox=BoundingBox(l=0, t=0, r=100, b=20, page_size=Size(width=612, height=792)),
                charspan=(0, len(text)),
            )
        )

    _add("第一章 总纲", 1, header=True)
    _add("第一章正文", 1, header=False)
    _add("第一章尾段", 2, header=False)
    _add("第一节 后节", 4, header=True)
    _add("第一节正文", 4, header=False)
    return doc


def test_ocr_boundary_heading_same_parent_inserts_tightly_before_heading() -> None:
    """形态②：OCR 行紧贴 prev（同父段落）之后、标题之前——不并入标题正文片。"""
    doc = _make_sibling_boundary_document()

    def _recognizer(page_index: int) -> list[str]:
        assert page_index == 2  # 第 3 页（0 基）
        return ["第三页扫描行"]

    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=_FakeFontSampler({"第一章": (20.0, True), "第一节": (18.0, False)}),
        text_layer_pages=[True, True, False, True],
        ocr_recognizer=_recognizer,
        page_count=4,
        settings=_settings(ocr_enabled=True),
    )
    rows = _preorder(result.document)
    assert [n.text or n.title for n, _p in rows] == [
        "第一章 总纲",
        "第一章正文",
        "第一章尾段",
        "第三页扫描行",
        "第一节 后节",
        "第一节正文",
    ]
    node, parent = rows[3]
    assert node.meta["raw_type"] == "ocr_line"
    assert node.parent_id == parent.id
    assert parent.title == "第一章 总纲"
    # 不并入标题正文片：OCR 节点是标题的同级兄弟、位于标题之前
    heading_index = next(i for i, (n, _p) in enumerate(rows) if n.title == "第一节 后节")
    assert rows.index((node, parent)) < heading_index


def test_ocr_disabled_never_invokes_recognizer() -> None:
    """ocr_enabled=false → 识别器绝不触发（回退门单键热更语义）。"""
    doc = _make_docling_document()
    sampler = _FakeFontSampler({})
    calls: list[int] = []

    def _recognizer(page_index: int) -> list[str]:
        calls.append(page_index)
        return []

    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=sampler,
        text_layer_pages=[False, False],
        ocr_recognizer=_recognizer,
        page_count=2,
        settings=_settings(ocr_enabled=False),
    )
    assert calls == []
    assert result.parse_meta["parser"] == "docling"
    assert result.parse_meta["ocr"]["pages"] == []


def test_text_layer_intact_never_invokes_recognizer() -> None:
    """文本层完好 → OCR 通道零触发（ARCH「文本层缺失判定后叠加」）。"""
    doc = _make_docling_document()
    sampler = _FakeFontSampler({})

    def _recognizer(page_index: int) -> list[str]:
        raise AssertionError("文本层完好时不得触发 OCR")

    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=sampler,
        text_layer_pages=[True, True],
        ocr_recognizer=_recognizer,
        page_count=2,
        settings=_settings(ocr_enabled=True),
    )
    assert result.parse_meta["ocr"]["pages"] == []


def test_rapidocr_engine_honestly_unimplemented() -> None:
    """ocr_engine=rapidocr 为 validator 合法值但未实装（PRD v1.34 钉 paddleocr）
    → 显式报错不静默。"""
    doc = _make_docling_document()
    sampler = _FakeFontSampler({})
    with pytest.raises(Exception, match="rapidocr"):
        normalize_with_docling(
            b"%PDF-1.4 fake",
            converter=_fake_converter(doc),
            font_sampler=sampler,
            text_layer_pages=[False],
            ocr_recognizer=None,
            page_count=1,
            settings=_settings(ocr_enabled=True, ocr_engine="rapidocr"),
        )


# ---------------------------------------------------------------------------
# 失败归一
# ---------------------------------------------------------------------------


def test_conversion_failure_normalized() -> None:
    def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("docling pipeline exploded")

    converter = SimpleNamespace(convert=_boom)
    with pytest.raises(NormalizationError) as excinfo:
        normalize_with_docling(
            b"%PDF-1.4 fake",
            converter=converter,
            font_sampler=_FakeFontSampler({}),
            text_layer_pages=[],
            ocr_recognizer=None,
        )
    assert isinstance(excinfo.value.original_exception, RuntimeError)
    assert "docling pipeline exploded" not in excinfo.value.message  # 三方细节不外穿


from app.rag.normalizers.errors import NormalizationError  # noqa: E402  -- 保持文件尾部 import 于用例可读序


# ---------------------------------------------------------------------------
# 书签大纲校正接缝（装配前介入：层级在 BlockTreeBuilder.add 的 heading 栈里即时
# 物化 children——校正写回 pending.level 后由唯一嵌套实现产出正确树）
# ---------------------------------------------------------------------------


def _make_outline_document() -> DoclingDocument:
    """章/节形态 document（docling 版面推断的层级错误形态：章被判 L2、节被判 L3/L4）。"""
    from docling_core.types.doc.base import BoundingBox, Size
    from docling_core.types.doc.document import ProvenanceItem

    doc = DoclingDocument(name="outline")
    layout = [
        ("关于", 1),
        ("引言", 2),
        ("通往 ES6 之路", 2),
        ("第一章 块级绑定", 3),
        ("块级声明", 3),
        ("第二章 字符串与正则表达式", 4),
        ("更好的 Unicode 支持", 4),
    ]
    for text, page in layout:
        item = doc.add_text(label=DocItemLabel.SECTION_HEADER, text=text)
        item.prov.append(
            ProvenanceItem(
                page_no=page,
                bbox=BoundingBox(l=0, t=0, r=100, b=20, page_size=Size(width=612, height=792)),
                charspan=(0, len(text)),
            )
        )
    return doc


_OUTLINE_SAMPLER_SIZES = {
    "关于": (20.0, True),
    "引言": (16.0, False),
    "通往 ES6 之路": (13.0, False),
    "第一章": (16.0, False),
    "块级声明": (13.0, False),
    "第二章": (16.0, False),
    "更好的": (11.0, False),
}


def _all_headings(root: IrNode) -> list[tuple[IrNode, int]]:
    """(heading 节点, 树深) 扁平列表（文档序）。"""
    out: list[tuple[IrNode, int]] = []
    stack: list[tuple[IrNode, int]] = [(c, 1) for c in reversed(root.children)]
    while stack:
        node, depth = stack.pop()
        if node.type is IrNodeType.HEADING:
            out.append((node, depth))
        for child in reversed(node.children):
            stack.append((child, depth + 1))
    return out


def _outline_entries() -> list[OutlineEntry]:
    """全顶级书签（pdfium 实测 level=0 为顶层）+ 0-based dest 页。"""
    return [
        OutlineEntry(title="关于", level=0, page=0),
        OutlineEntry(title="引言", level=0, page=1),
        OutlineEntry(title="第一章 块级绑定", level=0, page=2),
        OutlineEntry(title="第二章 字符串与正则表达式", level=0, page=3),
    ]


def _run_with_outline(
    doc: DoclingDocument, reader: Any, **kwargs: Any
) -> Any:
    return normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=_FakeFontSampler(_OUTLINE_SAMPLER_SIZES),
        text_layer_pages=[True, True, True, True],
        ocr_recognizer=None,
        outline_reader=reader,
        **kwargs,
    )


def test_outline_bookmarks_repin_chapter_levels_and_nesting() -> None:
    """书签可用 → 章级锚点全部钉回 level=1/树深 1，节按其归属章节嵌套、顺序不变。"""
    result = _run_with_outline(_make_outline_document(), lambda _data: _outline_entries())

    headings = _all_headings(result.document)
    chapters = [node.title for node, depth in headings if node.level == 1]
    assert chapters == ["关于", "引言", "第一章 块级绑定", "第二章 字符串与正则表达式"]
    assert all(depth == 1 for node, depth in headings if node.level == 1)
    # 章节下的节：按 delta 重校准（引言 raw2 → 通往 raw3 = +1 → level2 且挂引言之下）
    by_title = {node.title: (node, depth) for node, depth in headings}
    assert by_title["通往 ES6 之路"][0].level == 2
    assert by_title["通往 ES6 之路"][1] == 2
    assert by_title["块级声明"][0].level == 2
    assert by_title["更好的 Unicode 支持"][0].level == 3
    # 父链（children 数组序 = 文档序）
    yinyan = by_title["引言"][0]
    assert [c.title for c in yinyan.children] == ["通往 ES6 之路"]
    first = by_title["第一章 块级绑定"][0]
    assert [c.title for c in first.children] == ["块级声明"]

    meta = result.parse_meta["heading_outline"]
    assert meta["correction_path"] == "bookmark"
    assert meta["bookmarks"] == 4
    assert meta["anchors"] == 4
    assert meta["unmatched"] == []
    # 纯 delta 保序（不重排小节）：章 1 → 首节 3 留一格空档属如实诊断（跳级只报数，
    # 锚点之间零跳级才是验收口径——见下一条断言）
    assert meta["level_jumps_after"] == 1
    chapter_levels = [node.level for node, _d in headings if node.level is not None and node.meta["level_source"] == "bookmark_outline"]
    assert chapter_levels == [1, 1, 1, 1]
    # 来源如实：锚点标 bookmark_outline，非锚点重校准项标 outline_offset
    assert by_title["第一章 块级绑定"][0].meta["level_source"] == "bookmark_outline"
    assert by_title["更好的 Unicode 支持"][0].meta["level_source"] == "outline_offset"


def test_outline_unusable_falls_back_to_sequence_clean() -> None:
    """书签与树内标题几乎不命中（垃圾大纲）→ 回退序列一致性清洗并如实留痕。"""
    junk = [OutlineEntry(title=f"页码 {i}", level=0, page=i) for i in range(4)]
    result = _run_with_outline(_make_outline_document(), lambda _data: junk)

    meta = result.parse_meta["heading_outline"]
    assert meta["correction_path"] == "sequence-clean"
    assert meta["anchors"] == 0
    assert meta["unmatched"] == [f"页码 {i}" for i in range(4)]
    # 原始序列 [1,2,3,2,3,2,4]（字号聚类）→ 跳级 2→4 压回 3
    headings = _all_headings(result.document)
    assert [node.level for node, _depth in headings] == [1, 2, 3, 2, 3, 2, 3]
    assert meta["level_jumps_before"] == 1
    assert meta["level_jumps_after"] == 0


def test_outline_reader_failure_falls_back_to_sequence_clean() -> None:
    """书签读取失败（pdfium 不可用/坏 dest 集）→ 不阻塞解析，回退序列清洗并留痕类型名。"""
    def _boom(_data: bytes) -> Any:
        raise RuntimeError("pdfium 不可用")

    result = _run_with_outline(_make_outline_document(), _boom)

    meta = result.parse_meta["heading_outline"]
    assert meta["correction_path"] == "sequence-clean"
    assert meta["bookmarks"] == 0
    assert meta["error"] == "RuntimeError"
    assert len(_all_headings(result.document)) == 7


def test_default_outline_reader_is_best_effort_on_unparsable_bytes() -> None:
    """生产缺省读（真 pdfium）遇坏字节 → 异常内归一、解析继续（现有用例零回归的护栏）。"""
    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(_make_outline_document()),
        font_sampler=_FakeFontSampler(_OUTLINE_SAMPLER_SIZES),
        text_layer_pages=[True, True, True, True],
        ocr_recognizer=None,
    )
    meta = result.parse_meta["heading_outline"]
    assert meta["correction_path"] == "sequence-clean"
    assert meta["error"] is not None
    assert len(_all_headings(result.document)) == 7


def test_pressed_heading_keeps_its_children_in_tree() -> None:
    """评审复现的特征化（装配面）：被压深的章仍作其节的父级，不出现父子反转。"""
    from docling_core.types.doc.base import BoundingBox, Size
    from docling_core.types.doc.document import ProvenanceItem

    doc = DoclingDocument(name="repro")
    layout = [
        ("第一章 函数", 1),
        ("第二章 方法", 1),
        ("2.1 声明", 2),
        ("2.2 表达式", 2),
        ("第三章 结果", 3),
    ]
    for text, page in layout:
        item = doc.add_text(label=DocItemLabel.SECTION_HEADER, text=text)
        item.prov.append(
            ProvenanceItem(
                page_no=page,
                bbox=BoundingBox(l=0, t=0, r=100, b=20, page_size=Size(width=612, height=792)),
                charspan=(0, len(text)),
            )
        )
    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        # 原始层级 [1,1,2,2,1]（= 评审复现输入）：章同级、2.1/2.2 深一级
        font_sampler=_FakeFontSampler(
            {
                "第一章": (16.0, False),
                "第二章": (16.0, False),
                "2.1": (13.0, False),
                "2.2": (13.0, False),
                "第三章": (16.0, False),
            }
        ),
        text_layer_pages=[True, True, True],
        ocr_recognizer=None,
        outline_reader=lambda _data: [
            OutlineEntry(title="第一章 函数", level=0, page=0),
            OutlineEntry(title="第三章 结果", level=0, page=2),
            OutlineEntry(title="参考文献", level=0, page=9),
        ],
    )
    by_title = {node.title: depth for node, depth in _all_headings(result.document)}
    assert by_title["第一章 函数"] == 1
    assert by_title["第二章 方法"] == 2  # 被压深（非锚点不冒充章级）
    assert by_title["2.1 声明"] == 3  # 仍挂在「第二章 方法」之下
    assert by_title["2.2 表达式"] == 3
    assert by_title["第三章 结果"] == 1
    meta = result.parse_meta["heading_outline"]
    assert meta["correction_path"] == "bookmark"
    assert meta["unmatched"] == ["参考文献"]
    assert meta["unmatched_count"] == 1
    assert meta["pressed"] == 1
    # 透传链锁死：parse_meta 预览必须带上被压深标题（防退化成「前 5: —」）
    assert meta["pressed_sample"] == ["第二章 方法"]


def test_outline_correction_disabled_is_byte_equivalent_to_legacy_pipeline() -> None:
    """裁决 C：`pdf_outline_correction=false` = 完全等价旧管线（逐字节）。

    旧管线参照面：把校正缝替换成「原始 level 原样返回」的恒等实现（修复前该适配器
    只有字号启发式分级，无任何序列清洗），并确认书签读取缝零调用。
    """
    doc = _make_outline_document()
    sampler = _FakeFontSampler(_OUTLINE_SAMPLER_SIZES)

    def _identity(headings: Any, outline: Any) -> Any:
        return SimpleNamespace(
            path="raw",
            levels=[max(1, min(9, item.level)) for item in headings],
            usable=False,
            outline_count=0,
            anchors=[],
            unmatched=[],
            duplicate_titles=[],
            level_jumps_before=0,
            level_jumps_after=0,
            pressed=0,
            pressed_titles=[],
        )

    original = docling_adapter.correct_heading_levels
    docling_adapter.correct_heading_levels = _identity
    try:
        legacy = normalize_with_docling(
            b"%PDF-1.4 fake",
            converter=_fake_converter(doc),
            font_sampler=sampler,
            text_layer_pages=[True, True, True, True],
            ocr_recognizer=None,
            outline_reader=lambda _data: [],  # 旧管线无书签面
        )
    finally:
        docling_adapter.correct_heading_levels = original

    reads: list[bytes] = []

    def _counting_reader(data: bytes) -> list[OutlineEntry]:
        reads.append(data)
        return _outline_entries()

    disabled = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=sampler,
        text_layer_pages=[True, True, True, True],
        ocr_recognizer=None,
        outline_reader=_counting_reader,
        outline_correction=False,
    )

    assert reads == []  # 计数器守卫（AssertionError 会被 except Exception 吞掉，不能靠 raise）
    assert "heading_outline" not in disabled.parse_meta
    assert serialize_ir(disabled.document) == serialize_ir(legacy.document)
    assert {k: v for k, v in disabled.parse_meta.items()} == {
        k: v for k, v in legacy.parse_meta.items() if k != "heading_outline"
    }


def test_outline_correction_flag_reads_env_settings_on_production_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产路径（不传显式参数）读 `.env` 档 kb_settings：False → 不写键；True → 写键。"""
    from types import SimpleNamespace

    doc = _make_outline_document()
    sampler = _FakeFontSampler(_OUTLINE_SAMPLER_SIZES)

    def _run() -> Any:
        return normalize_with_docling(
            b"%PDF-1.4 fake",
            converter=_fake_converter(doc),
            font_sampler=sampler,
            text_layer_pages=[True, True, True, True],
            ocr_recognizer=None,
            outline_reader=lambda _data: _outline_entries(),
        )

    monkeypatch.setattr(
        docling_adapter, "kb_settings", SimpleNamespace(pdf_outline_correction=False)
    )
    assert "heading_outline" not in _run().parse_meta

    monkeypatch.setattr(
        docling_adapter, "kb_settings", SimpleNamespace(pdf_outline_correction=True)
    )
    enabled = _run().parse_meta["heading_outline"]
    assert enabled["correction_path"] == "bookmark"
    assert enabled["pressed"] == 0
    assert enabled["unmatched_count"] == 0


def test_outline_meta_truncates_unmatched_and_counts_total() -> None:
    """unmatched 截前 10 条如实留痕（防脏书签撑爆 parse_meta），全量计数另给。"""
    doc = _make_outline_document()
    sampler = _FakeFontSampler(_OUTLINE_SAMPLER_SIZES)
    outline = _outline_entries() + [
        OutlineEntry(title=f"缺章 {i:02d}", level=0, page=50 + i) for i in range(9)
    ]
    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=sampler,
        text_layer_pages=[True, True, True, True],
        ocr_recognizer=None,
        outline_reader=lambda _data: outline,
    )
    meta = result.parse_meta["heading_outline"]
    assert len(outline) == 13 and meta["anchors"] == 4
    assert meta["unmatched_count"] == 9
    assert len(meta["unmatched"]) == 9  # < 10 全留
    assert "pressed" in meta

    outline_many = _outline_entries() + [
        OutlineEntry(title=f"缺章 {i:02d}", level=0, page=50 + i) for i in range(12)
    ]
    result_many = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=sampler,
        text_layer_pages=[True, True, True, True],
        ocr_recognizer=None,
        outline_reader=lambda _data: outline_many,
    )
    meta_many = result_many.parse_meta["heading_outline"]
    assert meta_many["unmatched_count"] == 12
    assert len(meta_many["unmatched"]) == 10  # 截断生效
    assert meta_many["unmatched"][-1] == "缺章 09"


# ---------------------------------------------------------------------------
# 工单 A：书签骨架重建（护栏过门 → 未命中顶级书签条目造节点；内容按页区间归属；
# 无锚点 heading 存废判据 = 工单 B 复核门）
# ---------------------------------------------------------------------------


def _make_skeleton_document() -> DoclingDocument:
    """「修改记录」形态：p1/p2 两章版面有标题，p3 只有正文（书签在版面零命中）。"""
    from docling_core.types.doc.base import Size

    doc = DoclingDocument(name="skeleton")
    for page_no in (1, 2, 3):
        doc.add_page(page_no=page_no, size=Size(width=612, height=792))
    _add_bbox_item(doc, "关于", 1, header=True, height=28.0)
    _add_bbox_item(doc, "关于章的正文段落", 1, height=14.0)
    _add_bbox_item(doc, "第一章 块级绑定", 2, header=True, height=28.0)
    _add_bbox_item(doc, "第一章的正文段落", 2, height=14.0)
    _add_bbox_item(doc, "修改记录区的正文段落", 3, height=14.0)
    return doc


_SKELETON_OUTLINE = [
    OutlineEntry("关于", 0, 0),
    OutlineEntry("第一章 块级绑定", 0, 1),
    OutlineEntry("修改记录", 0, 2),
]


def _run_skeleton(doc: DoclingDocument, outline: Any, **kwargs: Any) -> Any:
    return normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=_BboxFontSampler({28.0: (23.0, True), 14.0: (9.5, False)}, body=16.0),
        text_layer_pages=[True, True, True],
        ocr_recognizer=None,
        outline_reader=lambda _data: outline,
        outline_correction=True,
        heading_review=False,
        settings=_settings(ocr_enabled=False),
        **kwargs,
    )


def test_skeleton_rebuild_creates_node_and_attributes_content() -> None:
    """书签有而版面识别无 → 造节点（L1、页=书签目标页），页区间正文归属其下，零丢字。"""
    result = _run_skeleton(_make_skeleton_document(), _SKELETON_OUTLINE)

    headings = _all_headings(result.document)
    assert [(node.title, node.level, depth) for node, depth in headings] == [
        ("关于", 1, 1),
        ("第一章 块级绑定", 1, 1),
        ("修改记录", 1, 1),
    ]
    created = headings[2][0]
    assert created.meta["raw_type"] == "outline_bookmark"  # 版面未识别、书签确定性重建
    assert created.meta["level_source"] == "outline_created"
    assert created.loc is not None and created.loc.page == 3
    # 内容归属：目标页（p3）正文直接挂造节点之下
    assert [c.text for c in created.children] == ["修改记录区的正文段落"]
    # 零丢字：三段正文全部在树内（各出现一次）
    texts = [
        n.text for n, _p in _preorder(result.document) if n.type is IrNodeType.PARAGRAPH
    ]
    assert texts == ["关于章的正文段落", "第一章的正文段落", "修改记录区的正文段落"]

    meta = result.parse_meta["heading_outline"]
    assert meta["correction_path"] == "bookmark"
    assert meta["anchors"] == 3  # 造节点与自身书签条目匹配成锚点
    assert meta["unmatched"] == []
    assert meta["created"] == 1
    assert meta["created_sample"] == ["修改记录"]


def test_skeleton_created_node_exempt_from_review_gate() -> None:
    """造节点是确定性书签源 → 与锚点同豁免面，不被工单 B 复核门降级。

    「修正为：」带句末标点 + 造节点无 bbox（零正向证据）——若无豁免会被句末
    标点判据降级；锚点豁免使该形态可观察。
    """
    from docling_core.types.doc.base import Size

    doc = DoclingDocument(name="skeleton-review")
    for page_no in (1, 2, 3):
        doc.add_page(page_no=page_no, size=Size(width=612, height=792))
    _add_bbox_item(doc, "章一", 1, header=True, height=28.0)
    _add_bbox_item(doc, "章二", 2, header=True, height=28.0)
    _add_bbox_item(doc, "末节正文段落", 3, height=14.0)
    sampler = _ReviewE2ESampler({28.0: 20.0, 14.0: 9.5}, body_height=10.0)
    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=sampler,
        text_layer_pages=[True, True, True],
        page_count=3,
        ocr_recognizer=None,
        outline_reader=lambda _data: [
            OutlineEntry("章一", 0, 0),
            OutlineEntry("章二", 0, 1),
            OutlineEntry("修正为：", 0, 2),
        ],
        outline_correction=True,
        heading_review=True,
        settings=_settings(ocr_enabled=False),
    )
    headings = [node.title for node, _p in _preorder(result.document) if node.type is IrNodeType.HEADING]
    assert headings == ["章一", "章二", "修正为："]  # 造节点带句末标点仍存活（豁免）
    assert result.parse_meta["heading_review"]["demoted_header"] == 0
    assert result.parse_meta["heading_review"]["checked"] == 0  # 全部锚点豁免


def test_skeleton_unanchored_heading_fate_by_review_gate() -> None:
    """② 存废判据 = 工单 B 复核门：不过门降级回正文；过门保留挂最近锚点（造节点）下。

    - 「修改为：」未入书签 + 正文字号 + 句末标点 → 降级回正文段（废）；
    - 「真小节」未入书签 + 标题字号（正向证据过门）→ 保留且挂最近前置锚点之下（存）。
    """
    from docling_core.types.doc.base import Size

    doc = DoclingDocument(name="skeleton-fate")
    for page_no in (1, 2, 3, 4):
        doc.add_page(page_no=page_no, size=Size(width=612, height=792))
    _add_bbox_item(doc, "章一", 1, header=True, height=28.0)
    _add_bbox_item(doc, "修改为：", 2, header=True, height=14.0)  # 假阳性：正文字号+标点
    _add_bbox_item(doc, "第二章的正文段落", 2, height=14.0)
    _add_bbox_item(doc, "真小节", 2, header=True, height=28.0)  # 真标题版面漏挂书签
    _add_bbox_item(doc, "小节正文段落", 2, height=14.0)
    _add_bbox_item(doc, "章二", 3, header=True, height=28.0)
    _add_bbox_item(doc, "修改记录区的正文段落", 4, height=14.0)
    sampler = _ReviewE2ESampler({28.0: 20.0, 14.0: 9.5}, body_height=10.0)
    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=sampler,
        text_layer_pages=[True, True, True, True],
        page_count=4,
        ocr_recognizer=None,
        outline_reader=lambda _data: [
            OutlineEntry("章一", 0, 0),
            OutlineEntry("章二", 0, 2),
            OutlineEntry("修改记录", 0, 3),
        ],
        outline_correction=True,
        heading_review=True,
        settings=_settings(ocr_enabled=False),
    )

    by_title = {node.title: node for node, _p in _preorder(result.document)}
    # 废：假阳性降级回正文段（降级后 title 置 None、文本带回 text——按 text 找）
    demoted = next(n for n, _p in _preorder(result.document) if (n.text or "") == "修改为：")
    assert demoted.type is IrNodeType.PARAGRAPH
    assert result.parse_meta["heading_review"]["demoted_header"] == 1
    # 存：真小节过门保留，挂最近前置锚点「章一」之下（不冒充章级）
    kept = next(n for n, _p in _preorder(result.document) if n.title == "真小节")
    assert kept.type is IrNodeType.HEADING
    assert kept.level == 2
    zhang1 = by_title["章一"]
    assert kept.parent_id == zhang1.id and kept in zhang1.children
    # 造节点照常产出，正文归属不变
    created = by_title["修改记录"]
    assert created.meta["raw_type"] == "outline_bookmark"
    assert [c.text for c in created.children] == ["修改记录区的正文段落"]


def test_skeleton_guardrail_fail_falls_back_and_creates_nothing() -> None:
    """护栏不过（3 条书签 1 命中）→ 零造节点，结构树与无书签现行路径逐条等价。

    （meta 级 level_source 溯源差异——锚点槽在 sequence-clean 路径也标
    bookmark_outline——系 T1-b 既有语义，非本单改动面，不纳入对照。）
    """
    doc = _make_skeleton_document()
    junk = [
        OutlineEntry("关于", 0, 0),
        OutlineEntry("缺甲", 0, 1),
        OutlineEntry("缺乙", 0, 2),
    ]
    with_junk = _run_skeleton(doc, junk)
    without_outline = _run_skeleton(_make_skeleton_document(), [])

    def _shape(node: IrNode) -> list[tuple[str, str | None, int | None, int | None, str | None]]:
        rows: list[tuple[str, str | None, int | None, int | None, str | None]] = []
        stack: list[tuple[IrNode, int]] = [(child, 1) for child in reversed(node.children)]
        while stack:
            current, depth = stack.pop()
            loc_page = getattr(current.loc, "page", None) if current.loc else None
            rows.append(
                (current.type.value, current.title, current.level, loc_page, current.text)
            )
            for child in reversed(current.children):
                stack.append((child, depth + 1))
        return rows

    assert _shape(with_junk.document) == _shape(without_outline.document)
    created_nodes = [
        n
        for n, _p in _preorder(with_junk.document)
        if n.meta.get("raw_type") == "outline_bookmark"
    ]
    assert created_nodes == []
    meta = with_junk.parse_meta["heading_outline"]
    assert meta["correction_path"] == "sequence-clean"
    assert meta["created"] == 0
    assert meta["unmatched"] == ["缺甲", "缺乙"]


def test_skeleton_respects_correction_switch() -> None:
    """总开关关闭（KB_PDF_OUTLINE_CORRECTION=false）→ 骨架重建一并旁路（等价旧管线）。"""
    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(_make_skeleton_document()),
        font_sampler=_BboxFontSampler({28.0: (23.0, True), 14.0: (9.5, False)}, body=16.0),
        text_layer_pages=[True, True, True],
        ocr_recognizer=None,
        outline_reader=lambda _data: _SKELETON_OUTLINE,
        outline_correction=False,
        settings=_settings(ocr_enabled=False),
    )
    assert "heading_outline" not in result.parse_meta
    created_nodes = [
        n
        for n, _p in _preorder(result.document)
        if n.meta.get("raw_type") == "outline_bookmark"
    ]
    assert created_nodes == []


# ---------------------------------------------------------------------------
# 生产接线缝（P0 回归）：render_page 的 PIL 必须经归一层才到达 engine.predict
# ---------------------------------------------------------------------------


def test_ocr_production_wiring_feeds_engine_bgr_ndarray(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ocr_recognizer=None` 的生产默认识别器链：PIL 渲染 → 归一 → 假引擎收到 ndarray。

    防「将来绕过 `paddle_engine.recognize_lines` 直调 `engine.predict(PIL)`」原样复发
    （P0：PaddleOCR 静默忽略 PIL → 扫描页 0 行）。假引擎经 `_load_engine` 注入，零真模型。
    测试页用**纯红实心矩形**（白底黑字页 RGB≡BGR 锁不住通道序）：红 RGB(255,0,0)
    经归一后必须是 BGR `[0,0,255]`。
    """
    import numpy as np  # 与真实 paddle 依赖同源，单测环境必然可用

    from test.unit.test_kb_pdf_textlayer import build_red_rect_pdf

    captured: list[Any] = []

    def _predict(image: Any) -> list[dict[str, Any]]:
        captured.append(image)
        return [{"rec_texts": ["接线测试行"], "rec_scores": [0.9]}]

    fake_engine = SimpleNamespace(predict=_predict)
    monkeypatch.setattr(paddle_engine, "_load_engine", lambda: fake_engine)
    paddle_engine.reset_engine_cache()
    try:
        data = build_red_rect_pdf()
        result = normalize_with_docling(
            data,
            converter=_fake_converter(DoclingDocument(name="empty-doc")),
            font_sampler=_FakeFontSampler({}),
            text_layer_pages=[False],
            ocr_recognizer=None,  # 生产默认识别器：pdfium 渲染 + paddle_engine 链
            page_count=1,
            settings=_settings(ocr_enabled=True),
        )
    finally:
        paddle_engine.reset_engine_cache()

    assert len(captured) == 1
    arr = captured[0]
    assert isinstance(arr, np.ndarray)
    assert arr.dtype == np.uint8 and arr.ndim == 3 and arr.shape[2] == 3
    assert arr.flags["C_CONTIGUOUS"] and arr.flags["WRITEABLE"]
    # 通道序断言：红色元素 → BGR 纯红 [0,0,255]；未反转会得到 [255,0,0]
    assert np.any(np.all(arr == np.array([0, 0, 255], dtype=np.uint8), axis=-1))
    assert [n.text for n, _p in _preorder(result.document)] == ["接线测试行"]
    assert result.parse_meta["ocr"]["pages"] == [1]


# ---------------------------------------------------------------------------
# 页边界标题=F 且 F 是父标题首子节点（P1：prev 恰为父标题时不得落父子树末尾）
# ---------------------------------------------------------------------------


def _make_first_child_boundary_document() -> DoclingDocument:
    """H1(L1,p1) 无段落；H2(L2,p3)=H1 首子节点；p3 在 H2 下。"""
    from docling_core.types.doc.base import BoundingBox, Size
    from docling_core.types.doc.document import ProvenanceItem

    doc = DoclingDocument(name="first-child-boundary")

    def _add(text: str, page: int, *, header: bool) -> None:
        item = doc.add_text(
            label=DocItemLabel.SECTION_HEADER if header else DocItemLabel.TEXT, text=text
        )
        item.prov.append(
            ProvenanceItem(
                page_no=page,
                bbox=BoundingBox(l=0, t=0, r=100, b=20, page_size=Size(width=612, height=792)),
                charspan=(0, len(text)),
            )
        )

    _add("第一章 总纲", 1, header=True)
    _add("第一节 首子节", 3, header=True)
    _add("第一节正文", 3, header=False)
    return doc


def test_ocr_boundary_heading_first_child_of_parent() -> None:
    """形态③：H1 无段落、H2 是 H1 首子节点——OCR 行落 H1 后、H2 前（轨迹 ['H1']）。"""
    doc = _make_first_child_boundary_document()

    def _recognizer(page_index: int) -> list[str]:
        assert page_index == 1  # 第 2 页（0 基）
        return ["第二页扫描行"]

    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=_FakeFontSampler({"第一章": (20.0, True), "第一节": (18.0, False)}),
        text_layer_pages=[True, False, True],
        ocr_recognizer=_recognizer,
        page_count=3,
        settings=_settings(ocr_enabled=True),
    )
    rows = _preorder(result.document)
    assert [n.text or n.title for n, _p in rows] == [
        "第一章 总纲",
        "第二页扫描行",
        "第一节 首子节",
        "第一节正文",
    ]
    node, parent = rows[1]
    assert node.meta["raw_type"] == "ocr_line"
    assert node.parent_id == parent.id
    # 轨迹 ['H1']：父标题是 H1，且 H1 挂文档根
    assert parent.title == "第一章 总纲"
    assert parent.parent_id == result.document.id


def _make_trailing_paragraph_boundary_document() -> DoclingDocument:
    """H1(L1,p1) > [H2(L2,p3) > [正文(p3), 后置段(p3)]]——H2 是 H1 首子节点。"""
    from docling_core.types.doc.base import BoundingBox, Size
    from docling_core.types.doc.document import ProvenanceItem

    doc = DoclingDocument(name="trailing-paragraph-boundary")

    def _add(text: str, page: int, *, header: bool) -> None:
        item = doc.add_text(
            label=DocItemLabel.SECTION_HEADER if header else DocItemLabel.TEXT, text=text
        )
        item.prov.append(
            ProvenanceItem(
                page_no=page,
                bbox=BoundingBox(l=0, t=0, r=100, b=20, page_size=Size(width=612, height=792)),
                charspan=(0, len(text)),
            )
        )

    _add("第一章 总纲", 1, header=True)
    _add("第一节 后节", 3, header=True)
    _add("第一节正文", 3, header=False)
    _add("第一节后置段", 3, header=False)
    return doc


def test_ocr_boundary_heading_with_trailing_paragraph_not_misplaced() -> None:
    """形态④：H1 首子节点即 H2（真压首子节点分支）——OCR 行落 H1 后、H2 前，H2 子树不动。"""
    doc = _make_trailing_paragraph_boundary_document()

    def _recognizer(page_index: int) -> list[str]:
        assert page_index == 1
        return ["第二页扫描行"]

    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=_FakeFontSampler({"第一章": (20.0, True), "第一节": (18.0, False)}),
        text_layer_pages=[True, False, True],
        ocr_recognizer=_recognizer,
        page_count=3,
        settings=_settings(ocr_enabled=True),
    )
    rows = _preorder(result.document)
    assert [n.text or n.title for n, _p in rows] == [
        "第一章 总纲",
        "第二页扫描行",
        "第一节 后节",
        "第一节正文",
        "第一节后置段",
    ]
    node, parent = rows[1]
    assert node.parent_id == parent.id
    assert parent.title == "第一章 总纲"


# ---------------------------------------------------------------------------
# 标题丢失修复：orig 回落 / 字号提升 / bbox 分级 / 与书签校正交互
# ---------------------------------------------------------------------------


def _add_bbox_item(
    doc: DoclingDocument,
    text: str,
    page: int,
    *,
    header: bool = False,
    height: float = 20.0,
    label: Any = None,
) -> Any:
    """加一个带 prov bbox 的条目（top-left 原点；高度=字号样式的判定面）。"""
    from docling_core.types.doc.base import BoundingBox, Size
    from docling_core.types.doc.document import ProvenanceItem

    item = doc.add_text(
        label=DocItemLabel.SECTION_HEADER if header else (label or DocItemLabel.TEXT),
        text=text,
    )
    item.prov.append(
        ProvenanceItem(
            page_no=page,
            bbox=BoundingBox(
                l=10, t=100, r=310, b=100 + height, page_size=Size(width=612, height=792)
            ),
            charspan=(0, len(text)),
        )
    )
    return item


class _BboxFontSampler:
    """bbox 字号采样假体：按 bbox 高度映射 `(字号, 加粗)`（top-left 转换不变高度）。"""

    def __init__(self, sizes_by_height: dict[float, tuple[float, bool]], body: float = 16.0) -> None:
        self._sizes = {round(height, 1): value for height, value in sizes_by_height.items()}
        self.body_font_size = body
        self.bbox_calls = 0
        self.text_calls = 0

    def stats_in_bbox(
        self, page_index: int, bbox: tuple[float, float, float, float]
    ) -> tuple[float, bool] | None:
        self.bbox_calls += 1
        return self._sizes.get(round(bbox[3] - bbox[1], 1))

    def text_font_stats(self, page_index: int, text: str) -> tuple[float, bool] | None:
        self.text_calls += 1
        return None


def test_collect_blocks_uses_orig_when_text_empty() -> None:
    """Fix 1：formula 条目 text 为空、原文在 orig → 采集回落 orig（文字不丢）。"""
    from docling_core.types.doc.base import BoundingBox, Size
    from docling_core.types.doc.document import ProvenanceItem

    doc = DoclingDocument(name="orig-fallback")
    doc.add_page(page_no=1, size=Size(width=612, height=792))
    item = doc.add_text(label=DocItemLabel.FORMULA, text="")
    item.orig = "关于本书"
    item.prov.append(
        ProvenanceItem(
            page_no=1,
            bbox=BoundingBox(l=62, t=364, r=128, b=381, page_size=Size(width=612, height=792)),
            charspan=(0, 4),
        )
    )
    pending = docling_adapter._collect_blocks(doc)
    assert [(p.kind, p.label, p.text) for p in pending] == [("paragraph", "formula", "关于本书")]


def _make_bbox_font_document() -> DoclingDocument:
    from docling_core.types.doc.base import Size

    doc = DoclingDocument(name="bbox-font")
    doc.add_page(page_no=1, size=Size(width=612, height=792))
    doc.add_page(page_no=2, size=Size(width=612, height=792))
    _add_bbox_item(doc, "第一章 总纲", 1, header=True, height=28.0)
    _add_bbox_item(doc, "候选标题行", 2, height=28.0)
    _add_bbox_item(doc, "这是正文段落内容", 2, height=14.0)
    return doc


def test_promotes_short_single_line_text_with_heading_font_size() -> None:
    """Fix 2：正文大小的单行短文本命中标题字号簇 → 提升 heading（父标题正确）。"""
    doc = _make_bbox_font_document()
    sampler = _BboxFontSampler({28.0: (23.0, False), 14.0: (16.0, False)}, body=16.0)

    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=sampler,
        text_layer_pages=[True, True],
        page_count=2,
        settings=_settings(ocr_enabled=False),
    )

    assert sampler.text_calls == 0  # bbox 采样优先，零回退文本首现搜索
    assert result.parse_meta["heading_heuristic"]["promoted"] == 1
    assert result.parse_meta["heading_heuristic"]["body_font_size"] == 16.0
    rows = _preorder(result.document)
    headings = [(n.title, n.level) for n, _p in rows if n.type is IrNodeType.HEADING]
    assert headings == [("第一章 总纲", 1), ("候选标题行", 1)]
    promoted = next(n for n, _p in rows if n.title == "候选标题行")
    paragraph = next(n for n, _p in rows if (n.text or "") == "这是正文段落内容")
    assert paragraph.parent_id == promoted.id


def test_does_not_promote_sentence_like_or_body_size_lines() -> None:
    """Fix 2 负向：句末标点/超长/多行/正文字号一律不提升（零误报约束）。"""
    from docling_core.types.doc.base import Size

    doc = DoclingDocument(name="no-promote")
    doc.add_page(page_no=1, size=Size(width=612, height=792))
    _add_bbox_item(doc, "第一章 总纲", 1, header=True, height=28.0)
    _add_bbox_item(doc, "以句号结尾的短行。", 1, height=28.0)
    _add_bbox_item(doc, "以冒号结尾的短行：", 1, height=28.0)
    _add_bbox_item(doc, "长" * 41, 1, height=28.0)
    _add_bbox_item(doc, "第一行\n第二行", 1, height=28.0)
    _add_bbox_item(doc, "正文字号短行", 1, height=14.0)
    sampler = _BboxFontSampler({28.0: (23.0, False), 14.0: (16.0, False)}, body=16.0)

    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=sampler,
        text_layer_pages=[True],
        page_count=1,
        settings=_settings(ocr_enabled=False),
    )

    assert result.parse_meta["heading_heuristic"]["promoted"] == 0
    headings = [n for n, _p in _preorder(result.document) if n.type is IrNodeType.HEADING]
    assert [n.title for n in headings] == ["第一章 总纲"]


def test_heading_levels_sampled_from_bbox_not_text_search() -> None:
    """Fix 3：标题分级走 bbox 字号采样（同页多字号可分，零文本搜索回退）。"""
    from docling_core.types.doc.base import Size

    doc = DoclingDocument(name="bbox-levels")
    doc.add_page(page_no=1, size=Size(width=612, height=792))
    _add_bbox_item(doc, "大字标题", 1, header=True, height=28.0)
    _add_bbox_item(doc, "小字标题", 1, header=True, height=16.0)
    sampler = _BboxFontSampler({28.0: (23.0, False), 16.0: (16.0, False)}, body=16.0)

    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=sampler,
        text_layer_pages=[True],
        page_count=1,
        heading_review=False,  # 本测面=分级聚类（小字标题为正文字号样式）；复核门另一测面
        settings=_settings(ocr_enabled=False),
    )

    assert sampler.text_calls == 0
    levels = {
        n.title: n.level for n, _p in _preorder(result.document) if n.type is IrNodeType.HEADING
    }
    assert levels == {"大字标题": 1, "小字标题": 2}


def test_promoted_heading_participates_in_outline_correction() -> None:
    """交互面：提升产生的新 heading 参与书签校正锚点匹配（level_source=bookmark_outline）。"""
    from docling_core.types.doc.base import Size

    doc = DoclingDocument(name="promote-outline")
    for page_no in (1, 2, 4):
        doc.add_page(page_no=page_no, size=Size(width=612, height=792))
    _add_bbox_item(doc, "引言", 1, header=True, height=28.0)
    _add_bbox_item(doc, "新提升节", 2, height=28.0)
    _add_bbox_item(doc, "后续章", 4, header=True, height=28.0)
    sampler = _BboxFontSampler({28.0: (23.0, False)}, body=16.0)
    outline = [
        OutlineEntry("引言", 0, 0),
        OutlineEntry("新提升节", 0, 1),
        OutlineEntry("后续章", 0, 3),
    ]

    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=sampler,
        text_layer_pages=[True, True, True, True],
        page_count=4,
        outline_reader=lambda _data: outline,
        outline_correction=True,
        settings=_settings(ocr_enabled=False),
    )

    assert result.parse_meta["heading_outline"]["anchors"] == 3
    promoted = next(
        n for n, _p in _preorder(result.document) if n.type is IrNodeType.HEADING and n.title == "新提升节"
    )
    assert promoted.level == 1
    assert promoted.meta["level_source"] == "bookmark_outline"


# ---------------------------------------------------------------------------
# 工单 B：多信号复核层（section_header 门 / formula 复核 / code 复核 / Fix 2 高度回落）
# ---------------------------------------------------------------------------


class _ReviewE2ESampler:
    """复核层 e2e 假体：死亡字号口径（恒 1.0）+ 盒高回落表面（ECMA 新渲染形态）。

    page_chars 提供单条全宽行带（x 62→532，宽 470）——PageGeometry 的行宽基准面；
    盒高/字号/加粗按 bbox 高度键控（同 _BboxFontSampler 风格）。
    """

    def __init__(
        self,
        heights_by_height: dict[float, float],
        *,
        body_height: float = 10.0,
        bold_by_height: dict[float, bool] | None = None,
    ) -> None:
        self._heights = heights_by_height
        self._bold = bold_by_height or {}
        self.body_font_size = 1.0  # GetFontSize 死亡口径（ECMA 2026-11-24 新渲染版实测恒 1.0）
        self.heights_mode = body_height
        self.font_size_alive = False

    def page_chars(self, page_index: int) -> list[tuple[float, float, float, float, float, str, int]]:
        chars: list[tuple[float, float, float, float, float, str, int]] = []
        step = (532.0 - 62.0) / 20
        for i in range(20):
            chars.append((62.0 + i * step, 60.0, 62.0 + (i + 1) * step, 70.0, 1.0, "字", i))
        return chars

    def stats_in_bbox(
        self, page_index: int, bbox: tuple[float, float, float, float]
    ) -> tuple[float, bool] | None:
        height = round(bbox[3] - bbox[1], 1)
        if height not in self._heights:
            return None
        return (1.0, self._bold.get(height, False))

    def heights_in_bbox(
        self, page_index: int, bbox: tuple[float, float, float, float]
    ) -> float | None:
        return self._heights.get(round(bbox[3] - bbox[1], 1))


def _make_review_document() -> DoclingDocument:
    """复核层 e2e 形态：真标题（大字号）+ 假阳性标题（正文字号短行）+ 正文。"""
    from docling_core.types.doc.base import Size

    doc = DoclingDocument(name="review-gate")
    doc.add_page(page_no=1, size=Size(width=612, height=792))
    _add_bbox_item(doc, "第一章 块级绑定", 1, header=True, height=28.0)
    _add_bbox_item(doc, "修改为：", 1, header=True, height=14.0)
    _add_bbox_item(doc, "这是正文段落内容", 1, height=14.0)
    return doc


def test_review_gate_demotes_false_header_end_to_end() -> None:
    """① e2e：正文字号假阳性 heading 降级回正文段，真标题（字号门）保留。"""
    sampler = _ReviewE2ESampler({28.0: 20.0, 14.0: 9.5}, body_height=10.0)
    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(_make_review_document()),
        font_sampler=sampler,
        text_layer_pages=[True],
        page_count=1,
        outline_correction=False,
        heading_review=True,
        settings=_settings(ocr_enabled=False),
    )
    headings = [n.title for n, _p in _preorder(result.document) if n.type is IrNodeType.HEADING]
    assert headings == ["第一章 块级绑定"]
    false_header = next(n for n, _p in _preorder(result.document) if (n.text or "") == "修改为：")
    assert false_header.type is IrNodeType.PARAGRAPH
    assert false_header.meta["raw_type"] == "section_header"  # 留痕：docling 原始标签不篡改
    assert result.parse_meta["heading_review"]["demoted_header"] == 1
    assert result.parse_meta["heading_review"]["checked"] == 2


def test_review_gate_spares_bookmark_anchor_end_to_end() -> None:
    """① e2e：书签锚点 heading 零信号也豁免（校正开启时锚点不被门误伤）。

    锚题带句末标点使豁免可观察（非标点锚题在句末标点判据下本就保留）。
    """
    from docling_core.types.doc.base import Size

    doc = DoclingDocument(name="review-anchor")
    doc.add_page(page_no=1, size=Size(width=612, height=792))
    doc.add_page(page_no=2, size=Size(width=612, height=792))
    _add_bbox_item(doc, "引言：", 1, header=True, height=14.0)  # 正文字号 + 句末标点形态
    _add_bbox_item(doc, "正文内容", 2, height=14.0)
    sampler = _ReviewE2ESampler({14.0: 9.5}, body_height=10.0)
    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=sampler,
        text_layer_pages=[True, True],
        page_count=2,
        outline_reader=lambda _data: [OutlineEntry("引言：", 0, 0)],
        outline_correction=True,
        heading_review=True,
        settings=_settings(ocr_enabled=False),
    )
    headings = [n.title for n, _p in _preorder(result.document) if n.type is IrNodeType.HEADING]
    assert headings == ["引言："]  # 锚点豁免（无锚点时该形态会被句末标点判据降级）
    assert result.parse_meta["heading_review"]["demoted_header"] == 0


def test_review_disabled_switch_is_noop() -> None:
    """关闭态回退门：heading_review=False → 零降级、不写 parse_meta 键（等价旧管线）。"""
    sampler = _ReviewE2ESampler({28.0: 20.0, 14.0: 9.5}, body_height=10.0)
    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(_make_review_document()),
        font_sampler=sampler,
        text_layer_pages=[True],
        page_count=1,
        outline_correction=False,
        heading_review=False,
        settings=_settings(ocr_enabled=False),
    )
    headings = [n.title for n, _p in _preorder(result.document) if n.type is IrNodeType.HEADING]
    assert headings == ["第一章 块级绑定", "修改为："]  # 假阳性保留（旧管线形态）
    assert "heading_review" not in result.parse_meta


def test_review_flag_reads_env_settings_on_production_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产路径（不传显式参数）读 `.env` 档 kb_settings.pdf_heading_review。"""
    from types import SimpleNamespace

    sampler = _ReviewE2ESampler({28.0: 20.0, 14.0: 9.5}, body_height=10.0)

    def _run() -> Any:
        return normalize_with_docling(
            b"%PDF-1.4 fake",
            converter=_fake_converter(_make_review_document()),
            font_sampler=sampler,
            text_layer_pages=[True],
            page_count=1,
            outline_correction=False,
            settings=_settings(ocr_enabled=False),
        )

    monkeypatch.setattr(
        docling_adapter,
        "kb_settings",
        SimpleNamespace(pdf_outline_correction=False, pdf_heading_review=False),
    )
    assert "heading_review" not in _run().parse_meta

    monkeypatch.setattr(
        docling_adapter,
        "kb_settings",
        SimpleNamespace(pdf_outline_correction=False, pdf_heading_review=True),
    )
    enabled = _run().parse_meta["heading_review"]
    assert enabled["demoted_header"] == 1


def test_formula_review_demotes_to_text_end_to_end() -> None:
    """② e2e：盒高=正文众数且近满行的 formula 候选 → raw_type 降为 text（文本段）。"""
    from docling_core.types.doc.base import BoundingBox, Size
    from docling_core.types.doc.document import ProvenanceItem

    doc = DoclingDocument(name="review-formula")
    doc.add_page(page_no=1, size=Size(width=612, height=792))
    _add_bbox_item(doc, "第一章 块级绑定", 1, header=True, height=28.0)
    item = doc.add_text(label=DocItemLabel.FORMULA, text="")
    item.orig = "这一行其实是被误判成公式的正文内容"
    item.prov.append(
        ProvenanceItem(
            page_no=1,
            bbox=BoundingBox(l=62, t=100, r=532, b=111, page_size=Size(width=612, height=792)),
            charspan=(0, 4),
        )
    )
    sampler = _ReviewE2ESampler({28.0: 20.0, 11.0: 10.1}, body_height=10.0)
    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=sampler,
        text_layer_pages=[True],
        page_count=1,
        outline_correction=False,
        heading_review=True,
        settings=_settings(ocr_enabled=False),
    )
    formula_node = next(
        n for n, _p in _preorder(result.document) if (n.text or "").startswith("这一行其实")
    )
    assert formula_node.meta["raw_type"] == "text"
    assert result.parse_meta["heading_review"]["demoted_formula"] == 1


def test_code_review_demotes_to_text_end_to_end() -> None:
    """③ e2e：盒高=正文众数 + 字体名可得且非等宽 → code 降级为文本段。"""
    from docling_core.types.doc.base import BoundingBox, Size
    from docling_core.types.doc.document import ProvenanceItem

    doc = DoclingDocument(name="review-code")
    doc.add_page(page_no=1, size=Size(width=612, height=792))
    _add_bbox_item(doc, "第一章 块级绑定", 1, header=True, height=28.0)
    code = doc.add_code(text="x = 1  # 这其实不是代码")
    code.prov.append(
        ProvenanceItem(
            page_no=1,
            bbox=BoundingBox(l=62, t=100, r=532, b=111, page_size=Size(width=612, height=792)),
            charspan=(0, 5),
        )
    )
    sampler = _ReviewE2ESampler({28.0: 20.0, 11.0: 9.9}, body_height=10.0)
    names_by_height = {11.0: ["AAAAAD+ArialMT"]}

    def _names(page_index: int, bbox: tuple[float, float, float, float]) -> list[str] | None:
        return names_by_height.get(round(bbox[3] - bbox[1], 1))

    sampler.font_names_in_bbox = _names  # type: ignore[method-assign]
    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=sampler,
        text_layer_pages=[True],
        page_count=1,
        outline_correction=False,
        heading_review=True,
        settings=_settings(ocr_enabled=False),
    )
    code_node = next(n for n, _p in _preorder(result.document) if (n.text or "").startswith("x = 1"))
    assert code_node.type is IrNodeType.PARAGRAPH
    assert result.parse_meta["heading_review"]["demoted_code"] == 1


# ---------------------------------------------------------------------------
# 工单 B ④：Fix 2 提升通道——句末正则补英文句点 + 约束负例 + 高度回落复活
# ---------------------------------------------------------------------------


def test_does_not_promote_line_ending_with_english_period() -> None:
    """④ P3-1：句末英文句点「.」结尾的大字行不得提升（脱漏缺口修复，工单 B 随单清偿）。"""
    from docling_core.types.doc.base import Size

    doc = DoclingDocument(name="promote-period")
    doc.add_page(page_no=1, size=Size(width=612, height=792))
    _add_bbox_item(doc, "第一章 总纲", 1, header=True, height=28.0)
    _add_bbox_item(doc, "Set the flag.", 1, height=28.0)
    _add_bbox_item(doc, "参见附录 A.", 1, height=28.0)
    sampler = _BboxFontSampler({28.0: (23.0, False), 14.0: (16.0, False)}, body=16.0)
    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=sampler,
        text_layer_pages=[True],
        page_count=1,
        settings=_settings(ocr_enabled=False),
    )
    assert result.parse_meta["heading_heuristic"]["promoted"] == 0
    headings = [n.title for n, _p in _preorder(result.document) if n.type is IrNodeType.HEADING]
    assert headings == ["第一章 总纲"]


def test_does_not_promote_line_ending_with_comma_or_semicolon() -> None:
    """④ 负例：逗号/分号结尾不提升（正则面已有，负例锁死防回退——变异敏感性见交付说明）。"""
    from docling_core.types.doc.base import Size

    doc = DoclingDocument(name="promote-comma")
    doc.add_page(page_no=1, size=Size(width=612, height=792))
    _add_bbox_item(doc, "第一章 总纲", 1, header=True, height=28.0)
    _add_bbox_item(doc, "第一部分，", 1, height=28.0)
    _add_bbox_item(doc, "流程如下;", 1, height=28.0)
    sampler = _BboxFontSampler({28.0: (23.0, False), 14.0: (16.0, False)}, body=16.0)
    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=sampler,
        text_layer_pages=[True],
        page_count=1,
        settings=_settings(ocr_enabled=False),
    )
    assert result.parse_meta["heading_heuristic"]["promoted"] == 0


def test_does_not_promote_tall_bbox_single_line() -> None:
    """④ 负例：单行高度比约束——bbox 高 ≥1.75×字号（多行行距撑高形态）不提升。"""
    from docling_core.types.doc.base import Size

    doc = DoclingDocument(name="promote-tall")
    doc.add_page(page_no=1, size=Size(width=612, height=792))
    _add_bbox_item(doc, "第一章 总纲", 1, header=True, height=28.0)
    _add_bbox_item(doc, "占位两行的标题样式行", 1, height=50.0)  # 50 ≥ 1.75×23
    sampler = _BboxFontSampler({28.0: (23.0, False), 50.0: (23.0, False)}, body=16.0)
    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=sampler,
        text_layer_pages=[True],
        page_count=1,
        settings=_settings(ocr_enabled=False),
    )
    assert result.parse_meta["heading_heuristic"]["promoted"] == 0
    headings = [n.title for n, _p in _preorder(result.document) if n.type is IrNodeType.HEADING]
    assert headings == ["第一章 总纲"]


def _make_height_fallback_document() -> DoclingDocument:
    """高度回落形态：死亡字号 + 标题样式 formula 候选（旧基线「关于本书」指纹形态）。"""
    from docling_core.types.doc.base import BoundingBox, Size
    from docling_core.types.doc.document import ProvenanceItem

    doc = DoclingDocument(name="height-fallback")
    doc.add_page(page_no=1, size=Size(width=612, height=792))
    doc.add_page(page_no=2, size=Size(width=612, height=792))
    _add_bbox_item(doc, "浏览器与 Node.js 的兼容性", 1, header=True, height=28.0)
    item = doc.add_text(label=DocItemLabel.FORMULA, text="")
    item.orig = "关于本书"
    item.prov.append(
        ProvenanceItem(
            page_no=2,
            bbox=BoundingBox(l=62, t=100, r=128, b=124, page_size=Size(width=612, height=792)),
            charspan=(0, 4),
        )
    )
    return doc


def test_promote_falls_back_to_heights_when_font_size_dead() -> None:
    """④ Fix 2 高度回落：字号死亡（恒 1.0）标本上按盒高命中标题簇 → formula 候选提升。

    旧基线（旧标本）验收形态：关于本书 = heading raw_type=formula（Fix 2 提升指纹，
    out/kb_probe_ecma_final.txt:50）——新渲染版 GetFontSize 死亡后通道失效，本面=
    恢复既有已验收行为，使「关于本书」类在关闭书签校正场景也能归位。
    """
    sampler = _ReviewE2ESampler({28.0: 14.2, 24.0: 14.2}, body_height=10.0)
    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(_make_height_fallback_document()),
        font_sampler=sampler,
        text_layer_pages=[True, True],
        page_count=2,
        outline_correction=False,
        heading_review=True,
        settings=_settings(ocr_enabled=False),
    )
    assert result.parse_meta["heading_heuristic"]["promoted"] == 1
    promoted = next(
        n for n, _p in _preorder(result.document)
        if n.type is IrNodeType.HEADING and n.title == "关于本书"
    )
    assert promoted.meta["raw_type"] == "formula"  # 旧基线同款提升指纹
    # 死亡字号标本上分级本就扁平（GetFontSize 口径，级别归一非本单范围）→
    # 提升标题与锚点标题同为 level=1 兄弟节点，父=文档根
    anchor = next(
        n for n, _p in _preorder(result.document) if n.title == "浏览器与 Node.js 的兼容性"
    )
    assert anchor.type is IrNodeType.HEADING
    assert promoted.level == 1 and anchor.level == 1
    assert promoted.parent_id == "loc:doc:0"


def test_promote_height_fallback_gated_by_review_switch() -> None:
    """④ 回落门控：review 关闭 → 回落不生效（关闭态等价旧管线=死亡通道零提升）。"""
    sampler = _ReviewE2ESampler({28.0: 14.2}, body_height=10.0)
    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(_make_height_fallback_document()),
        font_sampler=sampler,
        text_layer_pages=[True, True],
        page_count=2,
        outline_correction=False,
        heading_review=False,
        settings=_settings(ocr_enabled=False),
    )
    assert result.parse_meta["heading_heuristic"]["promoted"] == 0


def test_height_fallback_does_not_promote_body_height_line() -> None:
    """④ 回落负例：正文件高的 text 行（修正记录句形态）不提升（零误报约束保持）。"""
    from docling_core.types.doc.base import Size

    doc = DoclingDocument(name="height-fallback-neg")
    doc.add_page(page_no=1, size=Size(width=612, height=792))
    doc.add_page(page_no=2, size=Size(width=612, height=792))
    _add_bbox_item(doc, "第一章 总纲", 1, header=True, height=28.0)
    _add_bbox_item(doc, "相应的，本书也存在一些问题。", 2, height=14.0)
    sampler = _ReviewE2ESampler({28.0: 20.0, 14.0: 9.5}, body_height=10.0)
    result = normalize_with_docling(
        b"%PDF-1.4 fake",
        converter=_fake_converter(doc),
        font_sampler=sampler,
        text_layer_pages=[True, True],
        page_count=2,
        outline_correction=False,
        heading_review=True,
        settings=_settings(ocr_enabled=False),
    )
    assert result.parse_meta["heading_heuristic"]["promoted"] == 0
    headings = [n.title for n, _p in _preorder(result.document) if n.type is IrNodeType.HEADING]
    assert headings == ["第一章 总纲"]
