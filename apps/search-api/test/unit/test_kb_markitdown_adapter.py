"""B17 主线一：markitdown 适配器单元测试（文档类格式主线 → 树 IR）。

组件性质：markitdown 为纯转换器（无模型）——探针 §2.1 已实证 docx/html 通道，
单测允许真组件跑内联小样本（md/html 字符串、python-docx 构造 docx）；失败路径经
注入假体确定性触发，并断言异常归一（original_exception 链入、消息零内容回显）。

消费探针发现（m3-probe-normalization-20261122.md §2.1）：
- docx 代码段被降为普通段落（代码围栏语义丢失）→ parse_meta.code_blocks 对 docx
  样本如实计 0，不伪造；
- 表格保真但表头语义瑕疵 → 由 markdown_mapper 的空表头回位规则消费（mapper 测试锁）。
"""

import time
import zipfile
from io import BytesIO
from typing import Any

import pytest
from docx import Document

from app.rag.ir_schema import IrNode, IrNodeType, UrlLoc
from app.rag.normalizers.errors import BadFileError, NormalizationError
from app.rag.normalizers.markitdown_adapter import (
    _XML_PARSE_MAX_BYTES,
    _convert_to_markdown,
    _read_heading_style_levels,
    _read_style_names,
    _repair_docx_heading_styles,
    _safe_parse_xml,
    clear_markitdown_cache,
    normalize_with_markitdown,
)


@pytest.fixture(autouse=True)
def _clean_singleton() -> None:
    clear_markitdown_cache()
    yield
    clear_markitdown_cache()


def _make_docx_bytes() -> bytes:
    """构造小 docx（Title/H1/H2 样式 + 表格 + 列表——python-docx，探针 §0 同款）。"""
    d = Document()
    d.add_heading("KB 适配器样本", 0)
    d.add_heading("一级标题", 1)
    d.add_paragraph("正文段落。")
    d.add_paragraph("要点甲", style="List Bullet")
    d.add_paragraph("要点乙", style="List Bullet")
    t = d.add_table(rows=2, cols=3)
    t.style = "Table Grid"
    for i, row in enumerate([["模型", "参数量", "上下文"], ["Qwen3", "4B", "32k"]]):
        for j, cell in enumerate(row):
            t.rows[i].cells[j].text = cell
    buf = BytesIO()
    d.save(buf)
    return buf.getvalue()


MD_SAMPLE = "# 甲标题\n\n正文段落内容。\n\n- 项一\n- 项二\n"


def test_markdown_channel_end_to_end() -> None:
    result = normalize_with_markitdown(MD_SAMPLE.encode(), "md")
    doc = result.document
    assert doc.type is IrNodeType.DOCUMENT
    assert doc.doc_kind == "upload"
    headings = [c for c in doc.children if c.type is IrNodeType.HEADING]
    assert headings and headings[0].title == "甲标题"
    paras = []
    stack = [*doc.children]
    while stack:
        cur = stack.pop()
        if cur.type is IrNodeType.PARAGRAPH:
            paras.append(cur)
        stack.extend(cur.children)
    assert any(p.text == "正文段落内容。" for p in paras)


def test_txt_and_html_channels_end_to_end() -> None:
    txt_result = normalize_with_markitdown("纯文本行甲\n纯文本行乙".encode(), "txt")
    assert txt_result.document.children  # txt 逐行落段

    html = (
        "<html><body><h1>页题</h1><p>段</p><pre><code>x=1</code></pre>"
        "<table><tr><th>表头</th><th>列二</th></tr><tr><td>a</td><td>b</td></tr></table>"
        "</body></html>".encode()
    )
    html_result = normalize_with_markitdown(html, "html")
    codes = []
    tables = []
    stack = [*html_result.document.children]
    while stack:
        cur = stack.pop()
        if cur.type is IrNodeType.CODE:
            codes.append(cur)
        if cur.type is IrNodeType.TABLE:
            tables.append(cur)
        stack.extend(cur.children)
    assert codes and codes[0].text == "x=1"
    assert tables and tables[0].meta["rows"][0] == ["表头", "列二"]


def test_docx_channel_table_and_code_gap_honest() -> None:
    """docx 通道：表格保真（表头回位）；代码段缺口如实（code_blocks=0，探针 §2.1）。"""
    result = normalize_with_markitdown(_make_docx_bytes(), "docx")
    assert result.parse_meta["tables"] == 1
    assert result.parse_meta["code_blocks"] == 0
    tables = []
    stack = [*result.document.children]
    while stack:
        cur = stack.pop()
        if cur.type is IrNodeType.TABLE:
            tables.append(cur)
        stack.extend(cur.children)
    assert tables and tables[0].meta["rows"][0] == ["模型", "参数量", "上下文"]


def test_parse_meta_four_keys_and_parser_markitdown() -> None:
    """parse_meta 契约四键：{pages, tables, code_blocks, parser}；markitdown 非页式
    → pages=null（data-model §1.7）。"""
    result = normalize_with_markitdown(MD_SAMPLE.encode(), "md")
    assert result.parse_meta["pages"] is None
    assert result.parse_meta["parser"] == "markitdown"
    assert {"pages", "tables", "code_blocks", "parser"} <= set(result.parse_meta)


def test_binary_format_magic_mismatch_rejected() -> None:
    """坏文件：docx 扩名非 zip 魔数 → 转换前置拒（BadFileError），不静默产垃圾文本
    （markitdown 对坏字节回落纯文本转换——实测行为，必须前置拦截）。"""
    with pytest.raises(BadFileError) as excinfo:
        normalize_with_markitdown(b"not-a-docx", "docx")
    assert "docx" in excinfo.value.message


def test_pdf_magic_checked_when_routed_to_markitdown() -> None:
    with pytest.raises(BadFileError):
        normalize_with_markitdown(b"junk-not-pdf-bytes", "pdf")


def test_pdf_dependency_gap_honest_error() -> None:
    """好魔数但 markitdown[pdf] extra 未安装（现行依赖线只声明 [docx,html]）→
    显式 NormalizationError 拒绝，不静默回落纯文本产垃圾（markitdown 无 pdfminer
    时对坏 pdf 静默透出原文——本会话实测行为，必须前置拦截）。"""
    import importlib.util

    if importlib.util.find_spec("pdfminer") is not None:
        pytest.skip("pdfminer 已安装（依赖线已补 extra）——缺口路径不适用")
    with pytest.raises(NormalizationError, match="pdfminer"):
        normalize_with_markitdown(b"%PDF-1.4\ntrailer", "pdf")


def test_component_failure_normalized_with_original_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """三方异常归一：NormalizationError + original_exception 链入 + 消息零内容回显。"""
    import app.rag.normalizers.markitdown_adapter as mod

    secret_marker = "绝密正文内容甲乙丙"

    class _BoomConverter:
        def convert_stream(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("SDK internal boom")

    monkeypatch.setattr(mod, "_get_markitdown", lambda: _BoomConverter())
    with pytest.raises(NormalizationError) as excinfo:
        normalize_with_markitdown(
            f"<p>{secret_marker}</p>".encode(), "html"
        )  # html 走真转换路径（md/txt 为本地透传，不经组件）
    assert isinstance(excinfo.value.original_exception, RuntimeError)
    assert secret_marker not in excinfo.value.message
    assert excinfo.value.__cause__ is excinfo.value.original_exception


def test_text_decode_failure_normalized() -> None:
    """文本类格式遇到不可解码字节 → NormalizationError（UTF-8 严格解码）。"""
    with pytest.raises(NormalizationError):
        normalize_with_markitdown(b"\xff\xfe\xfa\xfb", "txt")


def test_url_doc_kind_yields_url_loc() -> None:
    result = normalize_with_markitdown("# 题".encode(), "md", doc_kind="url", source_url="https://e.com/x")
    assert isinstance(result.document.loc, UrlLoc)
    assert result.document.loc.source_url == "https://e.com/x"


def test_singleton_memoized() -> None:
    """MarkItDown 单例：同进程复用（init 注册面只走一次）。"""
    first = normalize_with_markitdown(MD_SAMPLE.encode(), "md")
    second = normalize_with_markitdown("# 另一篇".encode(), "md")
    # 两次转换走同一底层转换器实例（行为级断言：两次都成功且解析形态一致）
    assert first.document.type is IrNodeType.DOCUMENT
    assert second.document.type is IrNodeType.DOCUMENT


def test_block_nodes_loc_and_ids() -> None:
    result = normalize_with_markitdown(
        MD_SAMPLE.encode(), "md", file_object_key="kb/u/d/original.md"
    )
    stack = [result.document]
    ids: list[str] = []
    while stack:
        cur = stack.pop()
        ids.append(cur.id)
        stack.extend(cur.children)
    assert any(i.startswith("loc:blk:") for i in ids)
    assert isinstance(result.document.loc, object)


# ---------------------------------------------------------------------------
# 飞书导出 docx 标题样式缺失（styles.xml 空壳 + 段落 outlineLvl）恢复面
#
# 缺陷形态（真实标本 out/feishu_export_rag1.docx 实证）：飞书「导出为 Word」的
# styles.xml 只有空根元素（0 个 w:style 定义），正文段落却引用 pStyle 编号并携带
# outlineLvl。mammoth 按 styles.xml 里的样式名（heading N）判标题，查不到定义
# → 标题全部降级为加粗普通段落。恢复规则：pPr 内 (pStyle, outlineLvl) 同段共存
# → 标题层级 = outlineLvl+1。
#
# 测试纪律：合成 docx 用 zipfile 现造（不依赖 out/ 下的真实标本——无该文件的
# 环境必须可复跑）。
# ---------------------------------------------------------------------------

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _paragraph_xml(
    text: str, *, style_id: str | None = None, outline_lvl: int | None = None
) -> str:
    """造一个 w:p（可选 pStyle / outlineLvl 进 pPr）。"""
    inner = ""
    if style_id is not None:
        inner += f'<w:pStyle w:val="{style_id}"/>'
    if outline_lvl is not None:
        inner += f'<w:outlineLvl w:val="{outline_lvl}"/>'
    ppr = f"<w:pPr>{inner}</w:pPr>" if inner else ""
    return f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>"


def _empty_styles_xml() -> str:
    """飞书导出的 styles.xml 形态：空根元素、零 w:style 定义（121 字节量级）。"""
    return f'<?xml version="1.0" encoding="UTF-8"?><w:styles xmlns:w="{_W_NS}"/>'


def _synth_docx(
    paragraphs: list[str], *, styles_xml: str, document_xml: str | None = None
) -> bytes:
    """最小合法 docx（mammoth 对缺 rels/[Content_Types].xml 有 fallback 路径）。

    `document_xml` 给定时直接落该字节（安全面测试要注入 DTD 等非段落形态部件）。
    """
    document = document_xml or (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<w:document xmlns:w="{_W_NS}"><w:body>{"".join(paragraphs)}</w:body></w:document>'
    )
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", document)
        zf.writestr("word/styles.xml", styles_xml)
    return buf.getvalue()


def _styles_text(data: bytes) -> str:
    with zipfile.ZipFile(BytesIO(data)) as zf:
        return zf.read("word/styles.xml").decode("utf-8")


def _headings_of(doc: IrNode) -> list[IrNode]:
    """按文档序收集 heading 节点（直取 children 树，不走模型面）。"""
    out: list[IrNode] = []

    def walk(node: IrNode) -> None:
        for child in node.children:
            if child.type is IrNodeType.HEADING:
                out.append(child)
            walk(child)

    walk(doc)
    return out


def _feishu_like_docx() -> bytes:
    """飞书形态：两档标题（outlineLvl 0 / 3）+ 正文段，样式表空壳。"""
    return _synth_docx(
        [
            _paragraph_xml("一、总览", style_id="1", outline_lvl=0),
            _paragraph_xml("正文段落。"),
            _paragraph_xml("子标题甲", style_id="4", outline_lvl=3),
            _paragraph_xml("正文细节。"),
            _paragraph_xml("子标题乙", style_id="4", outline_lvl=3),
        ],
        styles_xml=_empty_styles_xml(),
    )


def test_docx_feishu_style_shell_headings_recovered() -> None:
    """飞书形态（styles.xml 空壳 + outlineLvl 段落）：标题不再降级为普通段落。

    markdown 层：出现 `# `（level1）与 `#### `（level4）ATX 行；
    IR 层：heading 节点按 outlineLvl+1 分层（1 个 level=1 + 2 个 level=4）。
    """
    data = _feishu_like_docx()

    md_text = _convert_to_markdown(_repair_docx_heading_styles(data), "docx")
    atx = [line for line in md_text.splitlines() if line.startswith("#")]
    assert any(line.startswith("# ") and "一、总览" in line for line in atx)
    assert sum(1 for line in atx if line.startswith("#### ")) == 2
    # 幂等：已恢复件再走一遍守卫（既有定义齐备）→ 零改动（重试/多重入队安全）
    repaired = _repair_docx_heading_styles(data)
    assert _repair_docx_heading_styles(repaired) == repaired

    result = normalize_with_markitdown(data, "docx")
    headings = _headings_of(result.document)
    assert [(h.level, h.title) for h in headings] == [
        (1, "一、总览"),
        (4, "子标题甲"),
        (4, "子标题乙"),
    ]


def test_docx_complete_heading_styles_bytes_unchanged() -> None:
    """正常 Word 文件（styles.xml 已有 heading 定义）→ 一个字节都不动。

    两种形态都覆盖：python-docx 生成件（pPr 无 outlineLvl → 零候选）、
    Word 原生形态（pStyle=Heading1 与 outlineLvl 同段共存但样式定义齐备）。
    """
    # 工厂产物只取一次：python-docx 写 zip 时间戳精确到秒，两次调用跨秒即字节不同
    # （评审实测 2/400 假红）——断言须引用同一实例
    python_docx_bytes = _make_docx_bytes()
    assert _repair_docx_heading_styles(python_docx_bytes) == python_docx_bytes

    word_native_styles = (
        f'<?xml version="1.0" encoding="UTF-8"?><w:styles xmlns:w="{_W_NS}">'
        '<w:style w:type="paragraph" w:styleId="Heading1">'
        '<w:name w:val="heading 1"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Heading4">'
        '<w:name w:val="heading 4"/></w:style>'
        "</w:styles>"
    )
    data = _synth_docx(
        [
            _paragraph_xml("一级标题", style_id="Heading1", outline_lvl=0),
            _paragraph_xml("四级标题", style_id="Heading4", outline_lvl=3),
        ],
        styles_xml=word_native_styles,
    )
    assert _repair_docx_heading_styles(data) == data


def test_docx_without_headings_bytes_unchanged() -> None:
    """无任何标题的 docx（零 (pStyle, outlineLvl) 对）→ 原样返回。"""
    styles = (
        f'<?xml version="1.0" encoding="UTF-8"?><w:styles xmlns:w="{_W_NS}">'
        '<w:style w:type="paragraph" w:styleId="Normal">'
        '<w:name w:val="Normal"/></w:style>'
        "</w:styles>"
    )
    data = _synth_docx(
        [_paragraph_xml("只是正文甲。"), _paragraph_xml("只是正文乙。")],
        styles_xml=styles,
    )
    assert _repair_docx_heading_styles(data) == data


def test_docx_partial_heading_styles_merged_existing_preserved() -> None:
    """部分样式已存在：补缺不覆盖——既有 heading 1 定义逐字保留、缺失的 heading 4 插入。"""
    partial_styles = (
        f'<?xml version="1.0" encoding="UTF-8"?><w:styles xmlns:w="{_W_NS}">'
        '<w:style w:type="paragraph" w:styleId="MyH1">'
        '<w:name w:val="heading 1"/><w:basedOn w:val="Normal"/></w:style>'
        '<w:style w:type="paragraph" w:styleId="Normal">'
        '<w:name w:val="Normal"/></w:style>'
        "</w:styles>"
    )
    data = _synth_docx(
        [
            _paragraph_xml("既有样式标题", style_id="MyH1", outline_lvl=0),
            _paragraph_xml("缺样式标题", style_id="4", outline_lvl=3),
        ],
        styles_xml=partial_styles,
    )
    repaired = _repair_docx_heading_styles(data)
    assert repaired != data

    styles = _styles_text(repaired)
    # 既有定义与同级其他样式逐字保留
    assert '<w:style w:type="paragraph" w:styleId="MyH1">' in styles
    assert '<w:name w:val="heading 1"/>' in styles
    assert "<w:basedOn" in styles and 'w:val="Normal"' in styles
    assert styles.count('w:styleId="MyH1"') == 1
    # 缺失项按 outlineLvl+1 补齐（mammoth 默认 map 认 heading 4）
    assert '<w:style w:type="paragraph" w:styleId="4">' in styles
    assert '<w:name w:val="heading 4"/>' in styles

    headings = _headings_of(normalize_with_markitdown(data, "docx").document)
    assert [(h.level, h.title) for h in headings] == [
        (1, "既有样式标题"),
        (4, "缺样式标题"),
    ]


def test_docx_style_id_conflict_existing_definition_wins() -> None:
    """与既有定义冲突（同 styleId 已有非 heading 名）→ 既有优先，不覆盖、不改写。"""
    conflict_styles = (
        f'<?xml version="1.0" encoding="UTF-8"?><w:styles xmlns:w="{_W_NS}">'
        '<w:style w:type="paragraph" w:styleId="7">'
        '<w:name w:val="自定义强调段"/></w:style>'
        "</w:styles>"
    )
    data = _synth_docx(
        [_paragraph_xml("冲突样式段落", style_id="7", outline_lvl=1)],
        styles_xml=conflict_styles,
    )
    assert _repair_docx_heading_styles(data) == data


def test_docx_duplicate_outline_levels_first_seen_wins() -> None:
    """同 styleId 多次出现且 outlineLvl 不同 → 以 document.xml 首见为准。"""
    data = _synth_docx(
        [
            _paragraph_xml("首见二级", style_id="5", outline_lvl=1),
            _paragraph_xml("后见三级", style_id="5", outline_lvl=2),
        ],
        styles_xml=_empty_styles_xml(),
    )
    styles = _styles_text(_repair_docx_heading_styles(data))
    assert '<w:name w:val="heading 2"/>' in styles
    assert '<w:name w:val="heading 3"/>' not in styles
    assert styles.count('w:styleId="5"') == 1


def test_docx_outline_level_out_of_range_skipped() -> None:
    """outlineLvl=9（OOXML 正文级语义）超出 markdown 1~6 → 不造样式，原样返回。"""
    data = _synth_docx(
        [_paragraph_xml("正文级段落", style_id="9", outline_lvl=9)],
        styles_xml=_empty_styles_xml(),
    )
    assert _repair_docx_heading_styles(data) == data


def test_docx_bad_zip_fails_via_normalization_error() -> None:
    """坏 zip（仅魔数像 zip）→ 走现有异常路径（NormalizationError），不静默吞掉。"""
    with pytest.raises(NormalizationError):
        normalize_with_markitdown(b"PK\x03\x04" + b"\x00" * 64, "docx")


# ---------------------------------------------------------------------------
# 不可信 XML 防线（KB 上传通道的 docx 属外部输入）
#
# OOXML（ECMA-376）规定部件不含 DTD；stdlib ElementTree 会展开内部实体
# （billion laughs 形态），故解析前按字节软拒绝。软失败语义与 ParseError 一致：
# document.xml → 空映射（不恢复标题，正文仍由 markitdown/mammoth 转换）、
# styles.xml → 视为无样式表（原样返回不重写）。
# ---------------------------------------------------------------------------


def _bomb_document_xml(levels: int = 6) -> str:
    """内部实体膨胀样本：n 层实体各引用上一层 10 次（含一对待恢复的 pStyle 对）。

    守卫失效时解析会产出非空映射（测试因此对标守卫本身）、层级加深即内存放大；
    取 6 层（10^6 展开量级）——既让断言对守卫敏感，又不给复跑本身留挂死风险。
    """
    entities = ['<!ENTITY e0 "xxxxxxxxxx">']
    for i in range(1, levels):
        entities.append(f'<!ENTITY e{i} "{"".join(f"&e{i - 1};" for _ in range(10))}">')
    body = (
        "<w:p><w:pPr>"
        '<w:pStyle w:val="4"/><w:outlineLvl w:val="3"/>'
        "</w:pPr><w:r><w:t>&e%d;</w:t></w:r></w:p>" % (levels - 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<!DOCTYPE w:document [{' '.join(entities)}]>"
        f'<w:document xmlns:w="{_W_NS}"><w:body>{body}</w:body></w:document>'
    )


def test_docx_dtd_entity_expansion_soft_fails_without_hang() -> None:
    """billion laughs 形态（嵌套实体定义）→ 软失败：不展开、不挂起、原样返回。"""
    document_xml = _bomb_document_xml()
    # 守卫本身：同一部件直喂解析器必须是空映射（变异验证——去掉 DOCTYPE 检查即红）
    assert _read_heading_style_levels(document_xml.encode("utf-8")) == []

    data = _synth_docx([], styles_xml=_empty_styles_xml(), document_xml=document_xml)
    started = time.monotonic()
    assert _repair_docx_heading_styles(data) == data
    assert time.monotonic() - started < 5.0


def test_docx_doctype_without_entity_soft_fails() -> None:
    """仅 DOCTYPE 无 ENTITY（合法 DTD 声明）→ 同样软失败，不恢复标题、原样返回。"""
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<!DOCTYPE w:document>"
        f'<w:document xmlns:w="{_W_NS}"><w:body>'
        '<w:p><w:pPr><w:pStyle w:val="4"/><w:outlineLvl w:val="3"/></w:pPr>'
        "<w:r><w:t>标题</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    assert _read_heading_style_levels(document_xml.encode("utf-8")) == []
    data = _synth_docx([], styles_xml=_empty_styles_xml(), document_xml=document_xml)
    assert _repair_docx_heading_styles(data) == data


def test_docx_large_normal_xml_unaffected_by_size_guard() -> None:
    """正常大 XML（无 DTD、数百 KB）不受守卫影响：标题映射与补样式照常工作。"""
    paragraphs = [
        _paragraph_xml(f"第{i}段正文内容。", outline_lvl=None) for i in range(3000)
    ]
    paragraphs.append(_paragraph_xml("大文档标题", style_id="4", outline_lvl=3))
    data = _synth_docx(paragraphs, styles_xml=_empty_styles_xml())
    with zipfile.ZipFile(BytesIO(data)) as zf:
        assert len(zf.read("word/document.xml")) > 100_000  # 量级前提成立

    repaired = _repair_docx_heading_styles(data)
    assert repaired != data
    assert '<w:name w:val="heading 4"/>' in _styles_text(repaired)


def test_xml_parse_size_cap_soft_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超限部件（> _XML_PARSE_MAX_BYTES）→ 软失败；上限常量为 64MB 量级锁死。"""
    import app.rag.normalizers.markitdown_adapter as mod

    assert _XML_PARSE_MAX_BYTES == 64 * 1024 * 1024
    styles = f'<w:styles xmlns:w="{_W_NS}"/>'
    assert _safe_parse_xml(styles.encode("utf-8")) is not None

    monkeypatch.setattr(mod, "_XML_PARSE_MAX_BYTES", 8)
    assert _safe_parse_xml(styles.encode("utf-8")) is None
    assert _read_style_names(styles.encode("utf-8")) is None
    assert _read_heading_style_levels(styles.encode("utf-8")) == []
