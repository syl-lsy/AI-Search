"""markdown → 统一树 IR 映射器（纯函数，零网络零模型零三方依赖）。

消费面（m3-probe-normalization-20261122.md §2.1/§3）：
- markitdown 表格输出「空表头 + 实际表头行作为首数据行」的表头语义瑕疵——空表头行
  剔除、实际表头回位为矩阵首行；
- markitdown/docx 通道标题带 `**` 加粗包裹残迹 → 标题提取处**成对**剥壳（`_strip_bold_wraps`；
  标题会进 ES `heading_path` 字段）；docling/PDF 通道不经本解析面（直接经
  `BlockTreeBuilder` 装配）→ 零影响是构造上成立；
- html 通道全要素保真 → 直接映射。

形状锚点（调研 feishu-kb-import-research-20261122.md §13.3/§13.4）：table 节点
meta.rows 矩阵内嵌不展开树 + meta.col_count；code 节点 text + meta.lang；list 单
节点 text 聚合；heading level 1~6（ATX）直接映射；全节点 meta.raw_type 必带
（双层 type 契约 §13.8#4，ir_schema._require_raw_type 兜底校验之外显式赋值）。

ID 体系：本地线按导入批次生成（§13.5 稳定性注记）——document=`loc:doc:0`、块=
`loc:blk:%04d`（文档内确定序号，重导=整文档单元替换、ID 不跨批次承诺稳定）。

标题嵌套：heading 栈构建（`BlockTreeBuilder`，markitdown 适配器与 docling 适配器
共用同一嵌套语义——heading 关闭同级/更浅级标题的悬垂段，深标题嵌套于浅标题之下）。

块运行态状态机：段落/列表/引用为「连续行运行态」——同态行聚合为单节点，空行/标题/
围栏/表格边界关闭运行态；表格=连续管道行（首行 | 开头判定 + 分隔行丢弃）。
"""

import re

from app.rag.ir_schema import (
    DOC_KINDS,
    DocKind,
    IrLoc,
    IrNode,
    IrNodeType,
    LocalLoc,
    UrlLoc,
)

#: 本地线 document ID（单文档产出单元；Forest 虚拟单根由装配器/流水线负责）
DOCUMENT_ID = "loc:doc:0"
#: 块节点构造期占位 id（builder.add() 即以序号覆写；占位值本身须过前缀校验）
PENDING_BLOCK_ID = "loc:blk:_pending"

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_LIST_RE = re.compile(r"^\s*[-*+]\s+(.+)$")
_ORDERED_LIST_RE = re.compile(r"^\s*(\d+)[.)]\s+(.+)$")
_IMAGE_LINE_RE = re.compile(r"^!\[[^\]]*\]\([^)]*\)\s*$")
_HR_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
_FENCE_RE = re.compile(r"^\s*```([\w+-]*)\s*$")
#: 表格分隔行（| --- | :---: | ---: |）
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")

#: markitdown 输出的转义回解（仅段落/列表/引用/表格单元格文本，代码块不动）——
#: markdown 转义最小集，保守处理
_UNESCAPE_PAIRS: tuple[tuple[str, str], ...] = (
    (r"\_", "_"),
    (r"\*", "*"),
    (r"\#", "#"),
    (r"\`", "`"),
    (r"\[", "["),
    (r"\]", "]"),
    (r"\(", "("),
    (r"\)", ")"),
    (r"\>", ">"),
    (r"\|", "|"),
    (r"\\", "\\"),
)


def _unescape_text(text: str) -> str:
    for old, new in _UNESCAPE_PAIRS:
        text = text.replace(old, new)
    return text.strip()


#: 成对 `**` 加粗包裹（docx/markitdown 通道的标题残迹；与
#: `heading_outline.normalize_title` 同哲学——成对剥、单 `*` 保留）
_BOLD_WRAP_RE = re.compile(r"\*\*(.+?)\*\*")


def _strip_bold_wraps(text: str) -> str:
    """标题 `**` 残迹归一：剥**成对**加粗包裹、保留内容；单个 `*` 原样（可能是标题
    里的真字符，如 `A*B` / `*斜体*`）。

    docx 通道（markitdown 输出）标题常带 `**…**` 加粗包裹残迹。**docling/PDF 通道
    零影响是构造上成立的**：docling 适配器直接经 `BlockTreeBuilder` 装配、不经本
    解析面（`_Parser` heading 分支），故此处不可能触碰其标题——实测证据为 ECMA
    标本探针输出逐字节不变。order 钉死：**先剥壳后回解转义**——`\\*\\*A\\*\\*`
    表示字面星号文本，不得被当成加粗包裹。
    """
    return _BOLD_WRAP_RE.sub(r"\1", text)


class BlockTreeBuilder:
    """heading 栈块树构建器：add() 按文档序喂块，标题嵌套由 level 栈驱动。

    两适配器（markitdown/markdown、docling/pdf）共用——统一嵌套语义单点实现。
    块节点构造期 id 用 PENDING_BLOCK_ID 占位、add() 即以 `loc:blk:%04d` 序号覆写。
    """

    def __init__(self, *, document_id: str = DOCUMENT_ID) -> None:
        self._counter = 0
        self._stack: list[IrNode] = []
        self.document = IrNode(
            id=document_id,
            parent_id=None,
            type=IrNodeType.DOCUMENT,
            meta={"raw_type": "normalizer.document"},
        )

    def next_block_id(self) -> str:
        self._counter += 1
        return f"loc:blk:{self._counter:04d}"

    def _attach(self, node: IrNode) -> None:
        node.parent_id = self._stack[-1].id if self._stack else self.document.id
        target = self._stack[-1].children if self._stack else self.document.children
        target.append(node)

    def add(self, node: IrNode) -> IrNode:
        """喂入一个块节点（heading 按 level 入栈/出栈，其余挂当前标题之下）。"""
        node.id = self.next_block_id()
        if node.type is IrNodeType.HEADING and node.level:
            while self._stack and (self._stack[-1].level or 1) >= node.level:
                self._stack.pop()
            self._attach(node)
            self._stack.append(node)
            return node
        self._attach(node)
        return node

    def add_node(
        self,
        node_type: IrNodeType,
        *,
        text: str | None = None,
        level: int | None = None,
        title: str | None = None,
        loc: IrLoc | None = None,
        meta: dict[str, object] | None = None,
    ) -> IrNode:
        """构造 + 喂入一步到位（适配器侧免手拼占位 id）。"""
        node = IrNode(
            id=PENDING_BLOCK_ID,
            parent_id=None,
            type=node_type,
            text=text,
            level=level,
            title=title,
            loc=loc,
            meta=dict(meta or {}),
        )
        return self.add(node)


def make_loc(
    doc_kind: str,
    *,
    page: int | None = None,
    file_object_key: str | None = None,
    source_url: str | None = None,
) -> LocalLoc | UrlLoc:
    """按 doc_kind 造 loc（§13.5 分源 union）：url → UrlLoc，其余本地来源 → LocalLoc。"""
    if doc_kind == "url":
        return UrlLoc(origin="url", source_url=source_url or "", char_range=None)
    if doc_kind not in DOC_KINDS:
        raise ValueError(f"doc_kind 须 ∈ {DOC_KINDS}：{doc_kind!r}")
    return LocalLoc(origin="local", page=page, char_range=None, file_object_key=file_object_key)


def make_document_node(
    doc_kind: DocKind,
    *,
    file_object_key: str | None = None,
    source_url: str | None = None,
    raw_type: str = "normalizer.document",
) -> tuple[IrNode, BlockTreeBuilder]:
    """构造 document 根 + 绑定的块树构建器（两适配器统一入口）。"""
    builder = BlockTreeBuilder()
    builder.document.doc_kind = doc_kind
    builder.document.loc = make_loc(
        doc_kind, file_object_key=file_object_key, source_url=source_url
    )
    builder.document.meta = {"raw_type": raw_type}
    return builder.document, builder


# ---------------------------------------------------------------------------
# markdown 文本解析（行态状态机）
# ---------------------------------------------------------------------------


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [_unescape_text(cell) for cell in stripped.split("|")]


def _is_table_separator(line: str) -> bool:
    return "|" in line and bool(_TABLE_SEP_RE.match(line))


class _Parser:
    """行态状态机：paragraph/list/quote/table/code 五态 + heading 即时节点。"""

    def __init__(self, builder: BlockTreeBuilder) -> None:
        self.builder = builder
        self.paragraph: list[str] = []
        self.list_items: list[str] = []
        self.list_kind = "unordered"
        self.quote: list[str] = []
        self.table_rows: list[list[str]] = []
        self.fence_lang: str | None = None
        self.fence_lines: list[str] = []

    # ---- 各运行态收口 ----

    def _emit_paragraph(self) -> None:
        if not self.paragraph:
            return
        text = _unescape_text(" ".join(self.paragraph))
        self.paragraph = []
        if text:
            self.builder.add_node(
                IrNodeType.PARAGRAPH,
                text=text,
                meta={"raw_type": "markdown.paragraph"},
            )

    def _emit_list(self) -> None:
        if not self.list_items:
            return
        items = self.list_items
        kind = self.list_kind
        self.list_items = []
        self.builder.add_node(
            IrNodeType.LIST,
            text="\n".join(_unescape_text(i) for i in items),
            meta={"raw_type": "markdown.list", "list_kind": kind},
        )

    def _emit_quote(self) -> None:
        if not self.quote:
            return
        lines = self.quote
        self.quote = []
        self.builder.add_node(
            IrNodeType.QUOTE,
            text=_unescape_text("\n".join(lines)),
            meta={"raw_type": "markdown.quote"},
        )

    def _emit_table(self) -> None:
        if not self.table_rows:
            return
        rows = self.table_rows
        self.table_rows = []
        # 表头语义瑕疵消费（探针 §2.1）：全空表头行剔除、实际表头回位首行
        if len(rows) >= 2 and all(not cell for cell in rows[0]):
            rows = rows[1:]
        col_count = max((len(r) for r in rows), default=0)
        self.builder.add_node(
            IrNodeType.TABLE,
            meta={"raw_type": "markdown.table", "rows": rows, "col_count": col_count},
        )

    def close_runs(self) -> None:
        """关闭全部运行态（空行/标题/围栏/表格边界/文末统一出口）。"""
        self._emit_paragraph()
        self._emit_list()
        self._emit_quote()
        self._emit_table()

    def feed(self, md_text: str) -> None:
        for line in md_text.splitlines():
            # 围栏态优先：代码整块保真不回解（§13.4 code 整块保留）
            if self.fence_lang is not None:
                if line.lstrip().startswith("```"):
                    text = "\n".join(self.fence_lines)
                    lang = self.fence_lang or None
                    self.fence_lines = []
                    self.fence_lang = None
                    self.builder.add_node(
                        IrNodeType.CODE,
                        text=text,
                        meta={"raw_type": "markdown.code", "lang": lang},
                    )
                else:
                    self.fence_lines.append(line)
                continue

            stripped = line.strip()

            # 空行/分隔线 → 冲刷运行态
            if not stripped or _HR_RE.match(stripped):
                self.close_runs()
                continue

            fence_match = _FENCE_RE.match(line)
            if fence_match:
                self.close_runs()
                self.fence_lang = fence_match.group(1) or ""
                continue

            heading_match = _HEADING_RE.match(line)
            if heading_match:
                self.close_runs()
                self.builder.add_node(
                    IrNodeType.HEADING,
                    level=len(heading_match.group(1)),
                    # 标题 `**` 残迹归一（标题进 ES heading_path 字段——见 _strip_bold_wraps）
                    title=_unescape_text(_strip_bold_wraps(heading_match.group(2))),
                    meta={"raw_type": "markdown.heading"},
                )
                continue

            # 表格：管道行收集；分隔行丢弃（markdown 表头分隔语义）
            if stripped.startswith("|"):
                self._emit_paragraph()
                if self._is_in_list_or_quote():
                    self._emit_list()
                    self._emit_quote()
                if _is_table_separator(line):
                    continue
                self.table_rows.append(_split_table_row(line))
                continue
            if self.table_rows:
                self._emit_table()

            list_match = _LIST_RE.match(line)
            ordered_match = _ORDERED_LIST_RE.match(line)
            if list_match or ordered_match:
                self._emit_paragraph()
                if self.list_items and self.list_kind == "ordered" and not ordered_match:
                    self._emit_list()
                    self.list_kind = "unordered"
                if ordered_match and not self.list_items:
                    self.list_kind = "ordered"
                elif not ordered_match and not self.list_items:
                    self.list_kind = "unordered"
                # 上方守卫已确保 list_match/ordered_match 至少其一命中（二者皆 None
                # 不可达——else continue 为窄化兜底，不改变行为）
                if list_match is not None:
                    item = list_match.group(1)
                elif ordered_match is not None:
                    item = ordered_match.group(2)
                else:
                    continue
                self.list_items.append(item)
                continue

            if stripped.startswith(">"):
                self._emit_paragraph()
                self._emit_list()
                self.quote.append(stripped.lstrip(">").strip())
                continue

            if _IMAGE_LINE_RE.match(stripped):
                self.close_runs()
                self.builder.add_node(
                    IrNodeType.IMAGE,
                    text=stripped,
                    meta={"raw_type": "markdown.image", "placeholder": True},
                )
                continue

            # 普通文本行：并入段落（软换行合并）；跨态先收口
            if self.list_items:
                self._emit_list()
            if self.quote:
                self._emit_quote()
            self.paragraph.append(stripped)

        # 文末收口；围栏未闭合兜底按代码块收（不丢内容）
        if self.fence_lang is not None and self.fence_lines:
            text = "\n".join(self.fence_lines)
            lang = self.fence_lang or None
            self.fence_lines = []
            self.fence_lang = None
            self.builder.add_node(
                IrNodeType.CODE,
                text=text,
                meta={"raw_type": "markdown.code", "lang": lang},
            )
        self.fence_lang = None
        self.close_runs()

    def _is_in_list_or_quote(self) -> bool:
        return bool(self.list_items) or bool(self.quote)


def markdown_to_blocks(md_text: str) -> list[IrNode]:
    """markdown 文本 → 块树（返回 document 根的 children，含 heading 嵌套）。"""
    _doc, builder = make_document_node("upload")
    _Parser(builder).feed(md_text)
    return builder.document.children


def markdown_to_document(
    md_text: str,
    doc_kind: DocKind,
    *,
    file_object_key: str | None = None,
    source_url: str | None = None,
) -> IrNode:
    """markdown 文本 → document 节点（loc 按 doc_kind；children=块树）。"""
    doc, builder = make_document_node(
        doc_kind, file_object_key=file_object_key, source_url=source_url
    )
    _Parser(builder).feed(md_text)
    return doc


__all__ = [
    "BlockTreeBuilder",
    "DOCUMENT_ID",
    "make_document_node",
    "make_loc",
    "markdown_to_blocks",
    "markdown_to_document",
]
