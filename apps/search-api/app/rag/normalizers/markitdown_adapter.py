"""markitdown 适配器：文档类格式主线（md/txt/html/htm/docx/xlsx/pptx/csv）→ 树 IR。

选型锚点（ARCH §2 S7 / ADR-018③）：markitdown 维持主线（Office/HTML/飞书导出
docx 咬合，探针 §2.1 docx 0.074s / html 0.007s 量级）；已知缺口如实消费——docx
代码段围栏语义丢失（code_blocks 如实计 0）、表格空表头瑕疵（markdown_mapper 回位
规则消费）、Word Title 样式降级为段落（不伪造标题）。

坏文件防线：二进制格式（docx/xlsx/pptx/pdf）转换前置魔数校验——markitdown 对坏
字节回落纯文本转换产出垃圾文本（本会话实测：b"not-a-docx" 命名 .docx 不报错、
原样透出），必须在转换前拦截（BadFileError）。

飞书导出 docx 标题样式缺失修复（本模块 `_repair_docx_heading_styles`，纯函数
bytes→bytes）：飞书「导出为 Word」的 `word/styles.xml` 是空壳（真实标本
out/feishu_export_rag1.docx：121 字节、0 个 `<w:style>`），正文段落却引用
`<w:pStyle w:val="4"/>` 并携带 `<w:outlineLvl w:val="3"/>`。mammoth 按样式表里的
样式名（heading N）判标题，查不到定义 → 全部降级加粗普通段落（树 IR heading 节点 0、
切片 heading_path 全空）。恢复规则（4 篇文档 320 个标题按层级逐一精确相等实证）：
取 document.xml 中每段 pPr 内**同段共存**的 (pStyle val=X, outlineLvl val=N) 对，
标题层级 = N+1，向 styles.xml 合并插入缺失的 `<w:name w:val="heading {N+1}"/>`
样式定义（0-indexed outlineLvl）。实测 mammoth 默认 style map 双写
`Heading N`/`heading N`（options.py :47-58），大小写均命中。

异常归一：组件异常 → NormalizationError（original_exception 链入 + raise from，
消息零内容回显）；单例 MarkItDown（init 注册转换器面只走一次）。
"""

import importlib
import importlib.util
import zipfile
from io import BytesIO
from typing import Any
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from app.rag.ir_schema import IrNodeType
from app.rag.normalizers.errors import BadFileError, NormalizationError
from app.rag.normalizers.markdown_mapper import markdown_to_document
from app.rag.normalizers.result import NormalizationResult

#: 文本类格式（UTF-8 严格解码校验；html/htm/csv 仍交 markitdown 转换归一）
_TEXT_EXTS: frozenset[str] = frozenset({"md", "txt", "html", "htm", "csv"})
#: 恒等透传格式——markitdown 的 md/txt 通道即原文透传（探针 §2.1 实测），本地直读
#: 产出相同文本；规避其 magika 内容嗅探对极小/无尾换行输入的偶发
#: UnsupportedFormatException（本会话实测：b"# 另一篇" 命名 .md 两次失败一次）。
#: parse_meta.parser 仍记 markitdown（通道身份——透传即该组件的文档化行为）。
_TEXT_PASSTHROUGH_EXTS: frozenset[str] = frozenset({"md", "txt"})
#: 二进制格式魔数（转换前置校验——坏文件不进组件）
_MAGIC_BY_EXT: dict[str, bytes] = {
    "docx": b"PK\x03\x04",
    "xlsx": b"PK\x03\x04",
    "pptx": b"PK\x03\x04",
    "pdf": b"%PDF-",
}

_MARKITDOWN_SINGLETON: Any | None = None


def clear_markitdown_cache() -> None:
    """清空单例（测试隔离面；生产进程内不调用）。"""
    global _MARKITDOWN_SINGLETON
    _MARKITDOWN_SINGLETON = None


def _get_markitdown() -> Any:
    """MarkItDown 惰性单例（SDK 运行期 importlib 解析——同 storage 先例）。"""
    global _MARKITDOWN_SINGLETON
    if _MARKITDOWN_SINGLETON is None:
        markitdown = importlib.import_module("markitdown")
        _MARKITDOWN_SINGLETON = markitdown.MarkItDown()
    return _MARKITDOWN_SINGLETON


# ---------------------------------------------------------------------------
# 飞书导出 docx 标题样式缺失修复（模块内纯函数：bytes → bytes）
#
# 面：只处理「pStyle 与 outlineLvl 同段共存」的段落。只有 outlineLvl 没有 pStyle
# 的段落本修复**不覆盖**（无 styleId 可补，mammoth 亦无样式可查）——该形态需在
# document.xml 侧改写段落本身，属另一类修法，不在本函数职责内。
# ---------------------------------------------------------------------------

#: WordprocessingML 主命名空间（styles.xml / document.xml 同源）
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_STYLES_ENTRY = "word/styles.xml"
_DOCUMENT_ENTRY = "word/document.xml"
#: markdown ATX 层级上限（mammoth 默认 style map 只映射 heading 1~6）
_HEADING_MAX_LEVEL = 6
#: XML 部件解析上限（防病态输入撑爆内存）：实测量级锚点——离线飞书标本正文 2.7
#: 万字符、document.xml 220KB；长文档再放大一两个数量级仍在数 MB 内，64MB 留足余量
_XML_PARSE_MAX_BYTES = 64 * 1024 * 1024
#: DTD 声明标记：OOXML（ECMA-376）明确规定部件**不含 DTD**（无 DOCTYPE/ENTITY），
#: 出现即为不可信构造。stdlib ElementTree 会展开内部实体（billion laughs 形态），
#: 故解析前按字节软拒绝——docx 属 KB 上传通道的不可信输入。
_XML_DTD_MARKERS: tuple[bytes, ...] = (b"<!DOCTYPE", b"<!ENTITY")


def _safe_parse_xml(data: bytes) -> ET.Element | None:
    """不可信 XML 部件的防御式解析：超限/含 DTD 声明 → None（同 ParseError 软失败）。

    返回 None 的语义对调用方一致：document.xml → 空映射（不恢复标题，正文照常走
    markitdown/mammoth 转换）；styles.xml → 视为无可用样式表（原样返回不重写）。
    """
    if len(data) > _XML_PARSE_MAX_BYTES:
        return None
    if any(marker in data for marker in _XML_DTD_MARKERS):
        return None
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        return None


def _attr(value: str) -> str:
    """XML 属性值转义（styleId 来自文档，不假设字符集）。"""
    return escape(value, {'"': "&quot;"})


def _read_heading_style_levels(document_xml: bytes) -> list[tuple[str, int]]:
    """document.xml → [(styleId, 标题层级)]，按文档序去重（同 id 首见为准）。

    层级 = outlineLvl + 1（0-indexed）；outlineLvl=9 等超出 1~6 的值（OOXML 正文级
    语义）与不可解析值直接跳过——不造 markdown 无法表达的层级。
    不可信 XML 防线见 `_safe_parse_xml`（DTD/超限 → 空映射软失败）。
    """
    root = _safe_parse_xml(document_xml)
    if root is None:
        return []
    seen: dict[str, int] = {}
    for paragraph in root.iter(f"{{{_W_NS}}}p"):
        ppr = paragraph.find(f"{{{_W_NS}}}pPr")
        if ppr is None:
            continue
        p_style = ppr.find(f"{{{_W_NS}}}pStyle")
        outline = ppr.find(f"{{{_W_NS}}}outlineLvl")
        if p_style is None or outline is None:
            continue  # 只认同段共存的 (pStyle, outlineLvl) 对
        style_id = p_style.get(f"{{{_W_NS}}}val")
        raw_level = outline.get(f"{{{_W_NS}}}val")
        if not style_id or raw_level is None:
            continue
        try:
            level = int(raw_level) + 1
        except ValueError:
            continue
        if not 1 <= level <= _HEADING_MAX_LEVEL:
            continue
        seen.setdefault(style_id, level)
    return list(seen.items())


def _read_style_names(styles_xml: bytes) -> dict[str, str | None] | None:
    """styles.xml → {styleId: 样式名}；不可解析/根元素不符 → None。

    同 id 多定义取首见（与 mammoth `read_styles_xml_element` 的「首实例保号、
    后续重编号」语义对齐——后续定义对转换无效）。
    不可信 XML 防线见 `_safe_parse_xml`（DTD/超限 → 视为无样式表，原样返回不重写）。
    """
    root = _safe_parse_xml(styles_xml)
    if root is None:
        return None
    if root.tag != f"{{{_W_NS}}}styles":
        return None
    names: dict[str, str | None] = {}
    for style in root.findall(f"{{{_W_NS}}}style"):
        style_id = style.get(f"{{{_W_NS}}}styleId")
        if style_id is None or style_id in names:
            continue
        name_element = style.find(f"{{{_W_NS}}}name")
        names[style_id] = (
            None if name_element is None else name_element.get(f"{{{_W_NS}}}val")
        )
    return names


def _find_tag_end(text: str, start: int) -> int | None:
    """从 `start` 处的标签起点找收尾 '>'（跳过引号内的 '>'）。"""
    quote: str | None = None
    for index in range(start + 1, len(text)):
        char = text[index]
        if quote is not None:
            if char == quote:
                quote = None
        elif char in {'"', "'"}:
            quote = char
        elif char == ">":
            return index
    return None


def _insert_style_definitions(
    styles_text: str, inserts: list[tuple[str, int]]
) -> str | None:
    """向既有 styles.xml 文本里追加缺失的标题样式定义（保留原有内容，不整体重写）。

    只在根元素内插入 `<w:style>`，其余字节不动。空壳根元素（飞书导出实证形态
    `<w:styles .../>`）先展开成配对标签。根元素定位失败 → None（调用方原样返回）。
    """
    start = styles_text.find("<w:styles")
    if start < 0:
        return None
    boundary = styles_text[start + len("<w:styles") : start + len("<w:styles") + 1]
    if boundary and boundary not in (" ", "\t", "\r", "\n", ">", "/"):
        return None  # 命中 <w:stylesFoo> 之类同前缀异名标签，不猜
    end = _find_tag_end(styles_text, start)
    if end is None:
        return None
    snippets = "".join(
        f'<w:style w:type="paragraph" w:styleId="{_attr(style_id)}">'
        f'<w:name w:val="heading {level}"/></w:style>'
        for style_id, level in inserts
    )
    root_tag = styles_text[start : end + 1]
    if root_tag.endswith("/>"):
        return (
            styles_text[:start]
            + root_tag[:-2]
            + ">"
            + snippets
            + "</w:styles>"
            + styles_text[end + 1 :]
        )
    closing = styles_text.rfind("</w:styles>")
    if closing < end:
        return None
    return styles_text[:closing] + snippets + styles_text[closing:]


def _repair_docx_heading_styles(data: bytes) -> bytes:
    """飞书导出 docx 标题样式恢复（纯函数 bytes → bytes，无副作用、零日志）。

    三步：① 检测——styles.xml 里缺失的标题样式定义（对 document.xml 里
    (pStyle, outlineLvl) 同段共存的 styleId 逐个判缺，既有定义/既有非标题名一律
    视为「已有」不覆盖）；② 恢复——向 styles.xml 合并插入 `<w:name w:val="heading N"/>`
    定义；③ 无缺失即原样返回（正常 Word 文件零字节改动，content_hash 不受影响）。

    docx zip 结构异常（坏 zip/截断/加密/缺关键条目）→ 原样返回，由下游转换按既有
    异常路径失败（NormalizationError），不静默吞掉也不劣化现状。
    """
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            names = set(archive.namelist())
            if _STYLES_ENTRY not in names or _DOCUMENT_ENTRY not in names:
                return data
            styles_xml = archive.read(_STYLES_ENTRY)
            existing = _read_style_names(styles_xml)
            if existing is None:
                return data
            candidates = _read_heading_style_levels(archive.read(_DOCUMENT_ENTRY))
            # 既有同 id 定义一律不覆盖（含名为非标题的冲突定义——既有优先）
            inserts = [
                (style_id, level)
                for style_id, level in candidates
                if style_id not in existing
            ]
            if not inserts:
                return data
            try:
                styles_text = styles_xml.decode("utf-8")
            except UnicodeDecodeError:
                return data  # 非 UTF-8 编码的 styles.xml 不猜编码，交下游
            repaired_text = _insert_style_definitions(styles_text, inserts)
            if repaired_text is None:
                return data
            repaired_styles = repaired_text.encode("utf-8")
            buffer = BytesIO()
            with zipfile.ZipFile(buffer, "w") as output:
                output.comment = archive.comment
                for info in archive.infolist():
                    payload = (
                        repaired_styles
                        if info.filename == _STYLES_ENTRY
                        else archive.read(info.filename)
                    )
                    output.writestr(info, payload)
            return buffer.getvalue()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, RuntimeError, EOFError,
            NotImplementedError, ValueError):
        return data


def _check_magic(data: bytes, ext: str) -> None:
    magic = _MAGIC_BY_EXT.get(ext)
    if magic is not None and not data.startswith(magic):
        raise BadFileError(
            f"文件字节形态与 .{ext} 不符（魔数校验失败）",
            detail=f"expected_magic={magic!r} actual_head={data[:4]!r}",
        )


def _convert_to_markdown(data: bytes, ext: str) -> str:
    """bytes → markdown 文本（统一走 markitdown——html→md 归一、md/txt 透传；
    文本类先做严格 UTF-8 预校验，坏字节在进组件前显式拒绝）。"""
    if ext in _TEXT_EXTS:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise NormalizationError(
                "文本解码失败（非 UTF-8 字节）",
                detail=f"ext={ext} {exc}",
                original_exception=exc,
            ) from exc
    if ext in _TEXT_PASSTHROUGH_EXTS:
        return data.decode("utf-8")
    if ext == "pdf" and importlib.util.find_spec("pdfminer") is None:
        # 依赖缺口诚实面：markitdown 对无 pdfminer 的 pdf 会静默回落纯文本转换
        # 产出垃圾文本（本会话实测）——pdf_engine=markitdown 热更前必须补
        # markitdown[pdf] extra（pyproject 现行只声明 [docx,html]，探针 §1 口径），
        # 缺依赖显式拒绝，不静默产垃圾。
        raise NormalizationError(
            "markitdown pdf 通道依赖缺失（需 markitdown[pdf] extra：pdfminer.six）",
            detail="pdf_engine=markitdown 为 spec 合法值但依赖未声明；"
            "现行依赖线仅 markitdown[docx,html]（pyproject B17 依赖批）",
        )
    try:
        result = _get_markitdown().convert_stream(BytesIO(data), file_extension=ext)
    except NormalizationError:
        raise
    except Exception as exc:
        raise NormalizationError(
            f"markitdown 转换失败（.{ext}）",
            detail=f"{type(exc).__name__}: {exc}",
            original_exception=exc,
        ) from exc
    return result.text_content or ""


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


def normalize_with_markitdown(
    data: bytes,
    ext: str,
    *,
    doc_kind: str = "upload",
    file_object_key: str | None = None,
    source_url: str | None = None,
) -> NormalizationResult:
    """markitdown 主线归一化：bytes（ext 格式）→ document 树 + parse_meta。

    异常契约：魔数不符 → BadFileError；解码/组件失败 → NormalizationError
    （original_exception 链入）；白名单/路由校验属 registry 上游职责。
    docx 路径在魔数校验之后、转换之前做飞书导出标题样式恢复（幂等；无缺失即零改动）。
    """
    normalized_ext = ext.lstrip(".").lower()
    _check_magic(data, normalized_ext)
    if normalized_ext == "docx":
        data = _repair_docx_heading_styles(data)
    md_text = _convert_to_markdown(data, normalized_ext)
    document = markdown_to_document(
        md_text,
        "url" if doc_kind == "url" else "upload",
        file_object_key=file_object_key,
        source_url=source_url,
    )
    tables, code_blocks = _count_types(document)
    parse_meta: dict[str, Any] = {
        "pages": None,  # markitdown 无页码概念（探针 §3）——非页式来源 pages=null
        "tables": tables,
        "code_blocks": code_blocks,
        "parser": "markitdown",
    }
    return NormalizationResult(document=document, parse_meta=parse_meta)
