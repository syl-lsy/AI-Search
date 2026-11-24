"""B17 主线一：markdown → 统一树 IR 映射器单元测试（纯函数零依赖）。

消费探针发现（m3-probe-normalization-20261122.md §2.1/§3）：
- markitdown 表格输出「空表头 + 实际表头行作为首数据行」——映射器须消费该瑕疵
  （空表头行剔除、实际表头回位为矩阵首行），不把空表头矩阵原样入库；
- html 通道全要素保真 → 直接映射 IR。
形状锚点：调研 feishu-kb-import-research-20261122.md §13.3/§13.4（table meta.rows
矩阵内嵌不展开树 / code meta.lang / list 单节点 / heading level 1~9 / 全节点
meta.raw_type 必带）。
"""


from app.rag.ir_schema import IrNode, IrNodeType
from app.rag.normalizers.markdown_mapper import (
    markdown_to_blocks,
    markdown_to_document,
)


def _find(node: IrNode, node_type: IrNodeType) -> list[IrNode]:
    found: list[IrNode] = []
    stack = [*node.children]
    while stack:
        cur = stack.pop()
        if cur.type is node_type:
            found.append(cur)
        stack.extend(cur.children)
    return found


def _walk(node: IrNode) -> list[IrNode]:
    out: list[IrNode] = []
    stack = [*node.children]
    while stack:
        cur = stack.pop()
        out.append(cur)
        stack.extend(cur.children)
    return out


MD_FULL = """# 一级标题甲

开头段落第一段。

## 二级标题乙

- 列表项一
- 列表项二

> 这是一段引用

```python
def f():
    pass
```

|  |  |  |
| --- | --- | --- |
| 模型 | 参数量 | 上下文 |
| Qwen3 | 4B | 32k |

结尾段落。

![示意图](https://example.com/a.png)
"""


def test_markdown_to_document_shape() -> None:
    doc = markdown_to_document(MD_FULL, doc_kind="upload")
    assert doc.id.startswith("loc:doc:")
    assert doc.type is IrNodeType.DOCUMENT
    assert doc.doc_kind == "upload"
    headings = _find(doc, IrNodeType.HEADING)
    assert [h.title for h in headings] == ["一级标题甲", "二级标题乙"]
    assert [h.level for h in headings] == [1, 2]
    # h2 嵌套在 h1 之下
    h1 = doc.children[0]
    assert h1.title == "一级标题甲"
    assert any(c.title == "二级标题乙" for c in h1.children)


def test_markdown_nodes_all_carry_raw_type() -> None:
    """meta.raw_type 全量必带（双层 type 契约 §13.8#4）——ir_schema 校验兜底之外
    的显式断言（构造即合法）。"""
    doc = markdown_to_document(MD_FULL, doc_kind="upload")
    for node in [doc, *_walk(doc)]:
        assert "raw_type" in node.meta, node.id


def test_markdown_paragraph_list_quote_code_table_image() -> None:
    doc = markdown_to_document(MD_FULL, doc_kind="upload")
    paras = _find(doc, IrNodeType.PARAGRAPH)
    assert any(p.text == "开头段落第一段。" for p in paras)
    lists = _find(doc, IrNodeType.LIST)
    assert len(lists) == 1
    assert lists[0].text is not None and "列表项一" in lists[0].text and "列表项二" in lists[0].text
    quotes = _find(doc, IrNodeType.QUOTE)
    assert len(quotes) == 1 and quotes[0].text == "这是一段引用"
    codes = _find(doc, IrNodeType.CODE)
    assert len(codes) == 1
    assert codes[0].text == "def f():\n    pass"
    assert codes[0].meta.get("lang") == "python"
    tables = _find(doc, IrNodeType.TABLE)
    assert len(tables) == 1
    images = _find(doc, IrNodeType.IMAGE)
    assert len(images) == 1


def test_markdown_table_matrix_embedded_not_expanded() -> None:
    """表格 = 独立节点 + meta.rows 矩阵内嵌（不展开为子树）。"""
    doc = markdown_to_document(MD_FULL, doc_kind="upload")
    table = _find(doc, IrNodeType.TABLE)[0]
    assert table.children == []
    rows = table.meta["rows"]
    assert rows == [["模型", "参数量", "上下文"], ["Qwen3", "4B", "32k"]]
    assert table.meta["col_count"] == 3


def test_markdown_table_header_quirk_consumed() -> None:
    """markitdown「空表头 + 实际表头作首数据行」瑕疵被消费：全空表头行剔除后
    实际表头回位为矩阵首行（m3-probe §2.1 表头语义瑕疵）。"""
    quirk_md = "|  |  |  |\n| --- | --- | --- |\n| 表头A | 表头B | 表头C |\n| a1 | b1 | c1 |\n"
    doc = markdown_to_document(quirk_md, doc_kind="upload")
    table = _find(doc, IrNodeType.TABLE)[0]
    assert table.meta["rows"][0] == ["表头A", "表头B", "表头C"]
    assert table.meta["col_count"] == 3


def test_markdown_table_without_quirk_untouched() -> None:
    """正常表头（非空）不被改动——空表头剔除规则只在全空行触发。"""
    normal_md = "| 表头A | 表头B |\n| --- | --- |\n| a1 | b1 |\n"
    doc = markdown_to_document(normal_md, doc_kind="upload")
    table = _find(doc, IrNodeType.TABLE)[0]
    assert table.meta["rows"] == [["表头A", "表头B"], ["a1", "b1"]]


def test_markdown_unknown_block_falls_back_paragraph_with_raw_type() -> None:
    """未知行降级 paragraph（§13.9③；raw_type 留原始形态）。"""
    md = "<div>html直通行</div>\n"
    blocks = markdown_to_blocks(md)
    assert len(blocks) == 1
    assert blocks[0].type is IrNodeType.PARAGRAPH
    assert blocks[0].meta["raw_type"]


def test_markdown_empty_text_yields_empty_document() -> None:
    doc = markdown_to_document("", doc_kind="upload")
    assert doc.children == []


def test_markdown_loc_prefix_ids_sequential() -> None:
    """本地线 ID 按导入批次生成（loc:blk: 前缀 + 序号稳定）。"""
    doc = markdown_to_document("段一。\n\n段二。", doc_kind="upload")
    block_ids = [c.id for c in doc.children]
    assert all(b.startswith("loc:blk:") for b in block_ids)
    assert len(set(block_ids)) == 2


def test_markdown_deep_heading_nesting() -> None:
    md = "# H1甲\n\n甲段。\n\n## H2乙\n\n乙段。\n\n### H3丙\n\n丙段。\n\n## H2丁\n\n丁段。\n"
    doc = markdown_to_document(md, doc_kind="upload")
    h1 = doc.children[0]
    assert h1.level == 1
    h2s = [c for c in h1.children if c.type is IrNodeType.HEADING]
    assert [h.title for h in h2s] == ["H2乙", "H2丁"]
    h3s = [c for c in h2s[0].children if c.type is IrNodeType.HEADING]
    assert len(h3s) == 1
    h3 = h3s[0]
    assert h3.title == "H3丙" and h3.level == 3
    # 嵌套父子指针一致（装配即双写语义）
    assert h3.parent_id == h2s[0].id
    assert h2s[1].parent_id == h1.id  # 同级 H2丁 不嵌套于 H2乙


# ---------------------------------------------------------------------------
# 标题 `**` 残迹清理（markdown 通道标题分支；docx 通道 markitdown 输出的加粗包裹
# 即标题残迹——标题会进 ES heading_path 字段，与 heading_outline.normalize_title
# 同哲学：成对剥、单 `*` 保留。docling 通道零影响是构造上成立的：其适配器直接经
# BlockTreeBuilder 装配、不经本解析面）
# ---------------------------------------------------------------------------


def test_heading_paired_bold_markers_stripped() -> None:
    """成对 `**` 剥离（docx 标本：`## **Q1：…**` → 标题不带残迹）。"""
    md = "# **一级标题甲**\n\n## **二级标题乙**\n\n正文。\n"
    doc = markdown_to_document(md, doc_kind="upload")
    headings = _find(doc, IrNodeType.HEADING)
    assert [h.title for h in headings] == ["一级标题甲", "二级标题乙"]


def test_heading_single_asterisk_preserved() -> None:
    """单 `*` 保留（可能是标题里的真字符，如 A*B / *斜体*）——只剥成对 `**`。"""
    md = "# A*B 与 *斜体*\n\n## 2 * 3 = 6\n"
    doc = markdown_to_document(md, doc_kind="upload")
    headings = _find(doc, IrNodeType.HEADING)
    assert [h.title for h in headings] == ["A*B 与 *斜体*", "2 * 3 = 6"]


def test_heading_bold_wrap_inside_title_removed_but_text_kept() -> None:
    """标题内嵌成对包裹同样剥壳保留内容（与 normalize_title 同口径）。"""
    doc = markdown_to_document("# 前言 **重点** 说明\n", doc_kind="upload")
    assert _find(doc, IrNodeType.HEADING)[0].title == "前言 重点 说明"


def test_heading_escaped_bold_markers_preserved() -> None:
    """转义 `\\*` 表示字面星号 → 不进成对剥壳（先剥壳后回解转义——转义保留）。"""
    doc = markdown_to_document("# A\\*\\*B\\*\\*\n", doc_kind="upload")
    assert _find(doc, IrNodeType.HEADING)[0].title == "A**B**"


def test_heading_consecutive_bold_wraps_stripped() -> None:
    """连续形态：两个包裹背靠背（`**A****B**`）→ 各自剥壳、内容相连。"""
    doc = markdown_to_document("# **甲****乙**\n", doc_kind="upload")
    assert _find(doc, IrNodeType.HEADING)[0].title == "甲乙"


def test_heading_odd_asterisk_count_keeps_outer_single_asterisks() -> None:
    """奇数个记号（`***A***`）：剥最内一对成对记号、外侧单 `*` 保留。"""
    doc = markdown_to_document("# ***甲***\n", doc_kind="upload")
    assert _find(doc, IrNodeType.HEADING)[0].title == "*甲*"


def test_plain_title_unchanged_by_strip() -> None:
    """无 `**` 残迹的标题零改动（markdown 通道面）。

    docling/PDF 通道不受本归一影响是**构造上成立**的（docling 适配器直接经
    `BlockTreeBuilder` 装配、不经 `_Parser`），因此本用例只锁 markdown 通道「普通
    标题不被误剥」——不是 docling 通道的覆盖证据。
    """
    md = "# 第一章 块级绑定\n\n## 1.1 块级声明 [修订: 2024-01-01]\n"
    doc = markdown_to_document(md, doc_kind="upload")
    headings = _find(doc, IrNodeType.HEADING)
    assert [h.title for h in headings] == ["第一章 块级绑定", "1.1 块级声明 [修订: 2024-01-01]"]
