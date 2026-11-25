"""unit 层：飞书 SDK blocks → 统一树 IR 映射器（M3 批次 3 / B20 A 线）。

契约锚点：data-model.md §1.18「SDK blocks → IR 映射细则」（2026-11-25 B20 契约批钉定，
确认点②收尾；通用规则 + 42 行映射表）/ §1.7 parse_meta.feishu 四键存储侧附加键 /
api.md §4.7~4.9（skip 留痕面归 worker 线，本套件只锁映射器面）。合成 fixture 零网络零 DB。

钉住的语义（逐条对应测试）：
- **BLOCK_FIELD 字面表**（notebook `feishu_kb_tree_probe.ipynb` 一手材料 + 锚点断言：
  29=mindnote/31=table/27=image/19=callout，本批补 33=view、34=quote_container）；
- **五断言（工单 E 语义保真面）**：code(14) 换行缩进逐字保留不 strip / table(31) 不降级 /
  callout(19) 透明且内嵌 code 独立成节点 / 枚举外块留痕递归不丢正文 /
  mention_doc 树内占位符、树外留标题；
- **容器透明展开**：callout/grid/grid_column/view/okr 自身不产节点、子块按序挂容器父级；
- **合并容器**（bullet/ordered/todo/quote_container）：子块文本换行拼接为一个节点；
  内嵌 code(14)/table(31)（及 image(27)/file(23) 叶子形态）按不降级规则提升为兄弟节点；
- **枚举外降级（C3=(a)）**：自身降 paragraph（无可提取文本则不产节点）、children 递归
  不丢、degraded_blocks["block_<编号>"] 留痕；
- **paragraph 占位**：IR paragraph + 方括号占位字面 + raw_type=feishu.<块名> +
  degraded_blocks 计数；
- **parse_meta.feishu 四键**（space_id/node_token/obj_type/obj_edit_time）+ 可选
  degraded_blocks + parser=feishu_blocks 常量（data-model §1.7 存储侧附加键，不进投影）；
- **待核项处置**：31 merge_info 按「文本落左上格、余格空串」实现 + meta.merged_cells
  留痕；33 view 按「不丢正文」先展开 + transparent_counts 留痕（实现面诊断）；code
  meta.lang 取 style.language **原值**（SDK 1.8.0 为 int 枚举——契约钉「原值」）；
  language_alias 本版 SDK 无该字段，防御性 getattr 读取（有则另存 meta.lang_alias）。
"""

from typing import Any

import pytest

from app.rag.feishu_mapper import (
    BLOCK_FIELD,
    PARSER_FEISHU_BLOCKS,
    FeishuMappingResult,
    build_document_node,
    build_feishu_parse_meta,
    map_blocks,
)
from app.rag.ir_schema import (
    FeishuLoc,
    IrNodeType,
    find_document_refs,
)

SPACE_ID = "7708408008821771292"
NODE_TOKEN = "WTp2dAqBposqlFxMmmhcnLsCnmf"
DOC_ID = f"fs:node:{NODE_TOKEN}"


# ---------------------------------------------------------------------------
# SDK 形态假体（对齐 lark_oapi 1.8.0 Block/Text/TextElement 实测字段——见模块内注释）
# ---------------------------------------------------------------------------


def _style(**kw: Any) -> Any:
    """TextStyle 假体：language(int 枚举)/done(bool)/language_alias(防御面)。"""
    return type("S", (), {"language": kw.get("language"), "done": kw.get("done"), "language_alias": kw.get("language_alias")})()


def _run(content: str, link_url: str | None = None) -> Any:
    style = type("E", (), {"link": type("L", (), {"url": link_url})()})() if link_url else None
    return type("R", (), {"content": content, "text_element_style": style})()


def _elem(**kw: Any) -> Any:
    """TextElement 假体：text_run/mention_doc/mention_user/equation/file/inline_block/link_preview/reminder。"""
    base: dict[str, Any] = {
        "text_run": None,
        "mention_doc": None,
        "mention_user": None,
        "equation": None,
        "file": None,
        "inline_block": None,
        "link_preview": None,
        "reminder": None,
    }
    base.update(kw)
    return type("El", (), base)()


def _mention_doc(token: str, title: str) -> Any:
    return type("MD", (), {"token": token, "obj_type": 22, "url": None, "title": title})()


def _payload(elements: list[Any] | None = None, style: Any = None) -> Any:
    return type("T", (), {"elements": elements or [], "style": style})()


def _blk(bid: str, btype: int, parent: str, children: list[str] | None = None, **fields: Any) -> Any:
    """SDK Block 假体：block_id/parent_id/children/block_type + 类型载荷字段。"""
    base: dict[str, Any] = {
        "block_id": bid,
        "parent_id": parent,
        "children": children if children is not None else [],
        "block_type": btype,
    }
    base.update(fields)
    return type("B", (), base)()


def _text_blk(bid: str, content: str, parent: str, children: list[str] | None = None) -> Any:
    return _blk(bid, 2, parent, children, text=_payload([_elem(text_run=_run(content))]))


def _simple_table(
    table_bid: str,
    parent: str,
    cell_bids: list[str],
    cell_texts: list[str],
    *,
    row_size: int | None = None,
    column_size: int | None = None,
    merge_info: list[Any] | None = None,
    header_row: Any = None,
    header_column: Any = None,
) -> tuple[Any, list[Any]]:
    """构造 table(31) + table_cell(32)×n 假体（cells 行主序；merge_info 为 TableMergeInfo 形态）。"""
    cells = [
        _blk(cid, 32, table_bid, [f"{cid}_p"], text=_payload([_elem(text_run=_run(txt))]))
        for cid, txt in zip(cell_bids, cell_texts, strict=True)
    ]
    inner_blocks = []
    for cid, txt in zip(cell_bids, cell_texts, strict=True):
        inner_blocks.append(_text_blk(f"{cid}_p", txt, cid))
    prop = type(
        "P",
        (),
        {
            "row_size": row_size,
            "column_size": column_size,
            "merge_info": merge_info,
            "header_row": header_row,
            "header_column": header_column,
        },
    )()
    table = _blk(table_bid, 31, parent, list(cell_bids), table=type("Tb", (), {"cells": list(cell_bids), "property": prop})())
    return table, [*cells, *inner_blocks]


# ---------------------------------------------------------------------------
# BLOCK_FIELD 字面表 + 锚点（notebook 一手材料；33/34 本批补锚）
# ---------------------------------------------------------------------------


def test_block_field_literal_table_full() -> None:
    expected: dict[int, str] = {
        1: "page", 2: "text",
        3: "heading1", 4: "heading2", 5: "heading3", 6: "heading4",
        7: "heading5", 8: "heading6", 9: "heading7", 10: "heading8", 11: "heading9",
        12: "bullet", 13: "ordered", 14: "code", 15: "quote",
        17: "todo", 18: "bitable", 19: "callout", 20: "chat_card", 21: "diagram",
        22: "divider", 23: "file", 24: "grid", 25: "grid_column", 26: "iframe",
        27: "image", 28: "isv", 29: "mindnote", 30: "sheet", 31: "table",
        32: "table_cell", 33: "view", 34: "quote_container", 35: "task",
        36: "okr", 37: "okr_objective", 38: "okr_key_result", 39: "okr_progress",
        40: "add_ons", 41: "jira_issue", 42: "wiki_catalog",
    }
    assert BLOCK_FIELD == expected


@pytest.mark.parametrize(
    ("btype", "name"),
    [(29, "mindnote"), (31, "table"), (27, "image"), (19, "callout"), (33, "view"), (34, "quote_container"), (14, "code"), (30, "sheet"), (18, "bitable")],
)
def test_block_field_anchors(btype: int, name: str) -> None:
    """锚点断言：错标回归门（29/31 沿 notebook，33/34 契约批补锚——C1/R-b）。"""
    assert BLOCK_FIELD[btype] == name


# ---------------------------------------------------------------------------
# 五断言 ①：code(14) 换行缩进逐字保留不 strip + meta.lang 原值
# ---------------------------------------------------------------------------


def test_code_block_text_preserved_verbatim_with_lang() -> None:
    code_text = "def f():\n    return 1\n\n\ttab_indent = 2\n"
    code = _blk(
        "c1",
        14,
        DOC_ID,
        code=_payload(
            [_elem(text_run=_run("def f():\n")), _elem(text_run=_run("    return 1\n\n")), _elem(text_run=_run("\ttab_indent = 2\n"))],
            style=_style(language=72),
        ),
    )
    result = map_blocks([code], space_id=SPACE_ID, node_token=NODE_TOKEN)
    refs = {r.node.id: r for r in result.refs}
    node = refs["fs:blk:c1"].node
    assert node.type is IrNodeType.CODE
    assert node.text == code_text  # 逐字：换行、缩进、尾换行、tab 全保留（不 strip）
    assert node.meta["raw_type"] == "feishu.code"
    assert node.meta["lang"] == 72  # style.language 原值（SDK 1.8.0 为 int 枚举）
    assert "lang_alias" not in node.meta  # 本版 SDK 无 language_alias 字段 → 不写


def test_code_block_empty_text_still_node_with_lang_alias_defensive() -> None:
    """有 language_alias（防御性 getattr 面）→ meta.lang_alias 另存；空文本 code 仍产节点。"""
    code = _blk("c2", 14, DOC_ID, code=_payload([], style=_style(language=1, language_alias="py")))
    result = map_blocks([code], space_id=SPACE_ID, node_token=NODE_TOKEN)
    node = result.refs[0].node
    assert node.type is IrNodeType.CODE
    assert node.meta["lang"] == 1
    assert node.meta["lang_alias"] == "py"


# ---------------------------------------------------------------------------
# 五断言 ②：table(31) 不降级 + 吸收 32
# ---------------------------------------------------------------------------


def test_table_absorbs_cells_and_survives_inside_callout() -> None:
    table, extra = _simple_table(
        "t1", "co1", ["cell_a", "cell_b"], ["甲", "乙"], row_size=1, column_size=2
    )
    callout = _blk("co1", 19, DOC_ID, ["t1"], callout=_payload())
    result = map_blocks([callout, table, *extra], space_id=SPACE_ID, node_token=NODE_TOKEN)
    by_id = {r.node.id: r for r in result.refs}
    node = by_id["fs:blk:t1"].node
    # 31 恒独立 table 节点（不降级规则——嵌在 callout 内仍为 table）
    assert node.type is IrNodeType.TABLE
    assert node.meta["raw_type"] == "feishu.table"
    assert node.meta["rows"] == [["甲", "乙"]]
    assert node.meta["col_count"] == 2
    # 32 被吸收：无独立 cell 节点
    assert "fs:blk:cell_a" not in by_id and "fs:blk:cell_b" not in by_id
    # callout 透明：table 直接挂文档根层，且带 meta.callout=true（callout 直接子节点）
    assert result.root_order == ["fs:blk:t1"]
    assert node.meta["callout"] is True


def test_table_rows_from_cell_children_multiline() -> None:
    """cell 内多子块文本换行拼接；rows×col_count 由 property 定形。"""
    table, extra = _simple_table(
        "t2", DOC_ID, ["c1", "c2"], ["r1c1", "r1c2"], row_size=1, column_size=2
    )
    # 给 c1 再叠一行
    extra.append(_text_blk("c1_p2", "次行", "c1"))
    extra[0].children.append("c1_p2")
    result = map_blocks([table, *extra], space_id=SPACE_ID, node_token=NODE_TOKEN)
    node = next(r.node for r in result.refs if r.node.id == "fs:blk:t2")
    assert node.meta["rows"] == [["r1c1\n次行", "r1c2"]]
    assert node.meta["col_count"] == 2


# ---------------------------------------------------------------------------
# 五断言 ③：callout(19) 透明容器 + 内嵌 code 独立成节点（C2=(a)）
# ---------------------------------------------------------------------------


def test_callout_transparent_with_embedded_code_independent() -> None:
    code = _blk("cc1", 14, "co1", code=_payload([_elem(text_run=_run("x=1"))], style=_style(language=72)))
    para = _text_blk("cp1", "高亮正文", "co1")
    callout = _blk("co1", 19, DOC_ID, ["cc1", "cp1"], callout=_payload())
    result = map_blocks([callout, code, para], space_id=SPACE_ID, node_token=NODE_TOKEN)
    by_id = {r.node.id: r for r in result.refs}
    assert "fs:blk:co1" not in by_id  # callout 自身不产节点（透明容器）
    code_node = by_id["fs:blk:cc1"].node
    para_node = by_id["fs:blk:cp1"].node
    # E① 结构性保证：callout 内 code 恒独立 code 节点，不被压成引用/段落文本
    assert code_node.type is IrNodeType.CODE
    assert code_node.text == "x=1"
    assert para_node.type is IrNodeType.PARAGRAPH
    for node in (code_node, para_node):  # 直接子节点各加 meta.callout=true
        assert node.meta["callout"] is True
    assert result.root_order == ["fs:blk:cc1", "fs:blk:cp1"]  # 按 children 序展开挂容器父级


def test_empty_callout_produces_nothing() -> None:
    callout = _blk("co2", 19, DOC_ID, [], callout=_payload())
    result = map_blocks([callout], space_id=SPACE_ID, node_token=NODE_TOKEN)
    assert result.refs == []
    assert result.root_order == []


def test_duplicate_parent_reference_produces_node_once() -> None:
    """畸形双父引用（块既在某容器 children 又以父缺失形态出现）→ 只产一次节点。"""
    inner = _text_blk("dp_p", "容器内正文", "dp1")
    holder = _blk("dp1", 19, DOC_ID, ["dp_p"], callout=_payload())
    result = map_blocks([holder, inner], space_id=SPACE_ID, node_token=NODE_TOKEN)
    ids = [r.node.id for r in result.refs]
    assert len(ids) == len(set(ids)) == 1  # 无重复节点
    assert result.root_order == ["fs:blk:dp_p"]


# ---------------------------------------------------------------------------
# 五断言 ④：枚举外块（16/0/>42）降级留痕 + children 递归不丢正文（C3=(a)）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("btype", [0, 16, 99])
def test_unknown_block_traced_and_children_recursive_not_lost(btype: int) -> None:
    inner = _text_blk("u_p", "未知块里的正文", "u1")
    unknown = _blk("u1", btype, DOC_ID, ["u_p"])
    result = map_blocks([unknown, inner], space_id=SPACE_ID, node_token=NODE_TOKEN)
    by_id = {r.node.id: r for r in result.refs}
    assert "fs:blk:u1" not in by_id  # 无可提取文本 → 自身不产节点
    # 正文零丢失：children 递归产出、挂未知块的父级
    assert result.root_order == ["fs:blk:u_p"]
    para = by_id["fs:blk:u_p"].node
    assert para.type is IrNodeType.PARAGRAPH
    assert para.text == "未知块里的正文"
    assert para.meta["raw_type"] == "feishu.text"
    # 留痕不静默：degraded_blocks["block_<编号>"] 计数
    assert result.degraded_blocks == {f"block_{btype}": 1}


def test_unknown_block_with_known_field_text_degrades_to_paragraph() -> None:
    """有可提取文本（未知编号但命中已知载荷字段形态的防御面）→ 降 paragraph 产节点。"""
    unknown = _blk("u2", 16, DOC_ID, text=_payload([_elem(text_run=_run("残文"))]))
    result = map_blocks([unknown], space_id=SPACE_ID, node_token=NODE_TOKEN)
    assert result.degraded_blocks == {"block_16": 1}
    node = result.refs[0].node
    assert node.type is IrNodeType.PARAGRAPH
    assert node.text == "残文"
    assert node.meta["raw_type"] == "feishu.block_16"  # 枚举外 raw_type=feishu.block_<编号>


# ---------------------------------------------------------------------------
# 五断言 ⑤：mention_doc 树内占位符 / 树外留标题
# ---------------------------------------------------------------------------


def test_mention_doc_in_tree_becomes_document_ref_placeholder() -> None:
    elem = _elem(mention_doc=_mention_doc("siblingToken1", "兄弟文档"))
    para = _blk("m1", 2, DOC_ID, text=_payload([_elem(text_run=_run("见 ")), elem]))
    result = map_blocks(
        [para], space_id=SPACE_ID, node_token=NODE_TOKEN, known_node_tokens={"siblingToken1"}
    )
    text = result.refs[0].node.text
    assert "<document-ref" in (text or "")
    refs = find_document_refs(text or "")
    assert refs[0].node_id == "fs:node:siblingToken1"  # id=fs:node:{目标 node_token}
    assert refs[0].kind == "document"
    assert refs[0].label == "兄弟文档"
    assert "见 " in (text or "")


def test_mention_doc_out_tree_keeps_title_only() -> None:
    elem = _elem(mention_doc=_mention_doc("elsewhereTok", "外部文档"))
    para = _blk("m2", 2, DOC_ID, text=_payload([elem, _elem(text_run=_run(" 尾"))]))
    result = map_blocks([para], space_id=SPACE_ID, node_token=NODE_TOKEN, known_node_tokens=set())
    text = result.refs[0].node.text
    assert text == "外部文档 尾"  # 仅保留标题文字
    assert "<document-ref" not in (text or "")


def test_mention_doc_resolved_via_obj_token_map() -> None:
    """mention token 为 obj_token 形态 → 经 obj_token 映射解析为树内 node_token。"""
    elem = _elem(mention_doc=_mention_doc("objTok9999", "映射文档"))
    para = _blk("m3", 2, DOC_ID, text=_payload([elem]))
    result = map_blocks(
        [para],
        space_id=SPACE_ID,
        node_token=NODE_TOKEN,
        known_node_tokens={"inTreeToken"},
        node_token_by_obj_token={"objTok9999": "inTreeToken"},
    )
    refs = find_document_refs(result.refs[0].node.text or "")
    assert refs[0].node_id == "fs:node:inTreeToken"


# ---------------------------------------------------------------------------
# 标题 / 列表 / 引用容器 / 占位符
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("btype", "level", "name"),
    [(b, b - 2, f"heading{b - 2}") for b in range(3, 12)],  # 3~11 全行：heading1~heading9
)
def test_heading_level_mapping(btype: int, level: int, name: str) -> None:
    h = _blk(f"h{btype}", btype, DOC_ID, **{name: _payload([_elem(text_run=_run("标题"))])})
    result = map_blocks([h], space_id=SPACE_ID, node_token=NODE_TOKEN)
    node = result.refs[0].node
    assert node.type is IrNodeType.HEADING
    assert node.level == level  # level = 编号 − 2（1~9）
    assert node.title == "标题"
    assert node.meta["raw_type"] == f"feishu.{name}"


def test_bullet_and_ordered_and_todo_merge_children_text() -> None:
    b_child1 = _text_blk("b_c1", "首项正文", "b1")
    b_child2 = _text_blk("b_c2", "续行", "b1")
    bullet = _blk("b1", 12, DOC_ID, ["b_c1", "b_c2"], bullet=_payload([_elem(text_run=_run("要点"))]))
    ordered = _blk("o1", 13, DOC_ID, [], ordered=_payload([_elem(text_run=_run("步骤"))]))
    todo = _blk("td1", 17, DOC_ID, [], todo=_payload([_elem(text_run=_run("待办"))], style=_style(done=True)))
    todo_open = _blk("td2", 17, DOC_ID, [], todo=_payload([_elem(text_run=_run("未完"))], style=_style(done=False)))
    result = map_blocks([bullet, b_child1, b_child2, ordered, todo, todo_open], space_id=SPACE_ID, node_token=NODE_TOKEN)
    by_id = {r.node.id: r.node for r in result.refs}
    b = by_id["fs:blk:b1"]
    assert b.type is IrNodeType.LIST and b.meta["list_kind"] == "bullet"
    assert b.text == "要点\n首项正文\n续行"  # 子块文本换行拼接为一个节点；子块不单独产节点
    assert "fs:blk:b_c1" not in by_id
    o = by_id["fs:blk:o1"]
    assert o.meta["list_kind"] == "ordered"
    td = by_id["fs:blk:td1"]
    assert td.meta["list_kind"] == "todo"
    assert td.meta["done"] is True  # done 原值
    assert (td.text or "").startswith("[x] ")  # 文本前缀 [x] / [ ]
    assert (by_id["fs:blk:td2"].text or "").startswith("[ ] ")


def test_list_promotes_embedded_code_and_table_as_siblings() -> None:
    """合并容器内嵌 14/31：按不降级规则提升为兄弟节点（文本不并入列表）。"""
    code = _blk("bl_c", 14, "b2", code=_payload([_elem(text_run=_run("Y=2"))], style=_style(language=30)))
    bullet = _blk("b2", 12, DOC_ID, ["bl_c"], bullet=_payload([_elem(text_run=_run("带码项"))]))
    result = map_blocks([bullet, code], space_id=SPACE_ID, node_token=NODE_TOKEN)
    by_id = {r.node.id: r.node for r in result.refs}
    assert by_id["fs:blk:b2"].text == "带码项"  # code 文本不并入
    code_node = by_id["fs:blk:bl_c"]
    assert code_node.type is IrNodeType.CODE
    assert result.root_order == ["fs:blk:b2", "fs:blk:bl_c"]  # 兄弟位、随后


def test_quote_container_merges_text_promotes_code() -> None:
    inner = _text_blk("qc_p", "引用续行", "qc1")
    code = _blk("qc_c", 14, "qc1", code=_payload([_elem(text_run=_run("code_in_quote"))], style=_style(language=1)))
    qc = _blk("qc1", 34, DOC_ID, ["qc_p", "qc_c"], quote_container=_payload([_elem(text_run=_run("引用首行"))]))
    result = map_blocks([qc, inner, code], space_id=SPACE_ID, node_token=NODE_TOKEN)
    by_id = {r.node.id: r.node for r in result.refs}
    q = by_id["fs:blk:qc1"]
    assert q.type is IrNodeType.QUOTE
    assert q.meta["raw_type"] == "feishu.quote_container"
    assert q.text == "引用首行\n引用续行"  # 子块文本换行拼接为一个 quote 节点
    code_node = by_id["fs:blk:qc_c"]
    assert code_node.type is IrNodeType.CODE  # 内含 14 按不降级规则提升为兄弟节点
    assert result.root_order == ["fs:blk:qc1", "fs:blk:qc_c"]


def test_quote_container_promotes_table_as_sibling() -> None:
    """quote_container 内含 31（映射表 34 行「内含 14/31 提升为兄弟」的 table 半边）：
    table 恒独立节点、格文本不并入 quote 文本。"""
    table, extra = _simple_table(
        "qct1", "qc3", ["qcell1", "qcell2"], ["甲", "乙"], row_size=1, column_size=2
    )
    qc = _blk("qc3", 34, DOC_ID, ["qct1"], quote_container=_payload([_elem(text_run=_run("引用文字"))]))
    result = map_blocks([qc, table, *extra], space_id=SPACE_ID, node_token=NODE_TOKEN)
    by_id = {r.node.id: r.node for r in result.refs}
    t = by_id["fs:blk:qct1"]
    assert t.type is IrNodeType.TABLE  # 31 不降级规则在 quote_container 内同样成立
    assert t.meta["rows"] == [["甲", "乙"]]
    assert by_id["fs:blk:qc3"].text == "引用文字"  # 格文本零并入
    assert result.root_order == ["fs:blk:qc3", "fs:blk:qct1"]  # 提升为兄弟节点、顺次随后


def test_single_quote_block() -> None:
    q = _blk("q1", 15, DOC_ID, quote=_payload([_elem(text_run=_run("单块引用"))]))
    result = map_blocks([q], space_id=SPACE_ID, node_token=NODE_TOKEN)
    node = result.refs[0].node
    assert node.type is IrNodeType.QUOTE
    assert node.text == "单块引用"
    assert node.meta["raw_type"] == "feishu.quote"


@pytest.mark.parametrize(("btype", "literal"), [(20, "chat_card"), (21, "diagram"), (26, "iframe"), (28, "isv"), (29, "mindnote"), (35, "task"), (37, "okr_objective"), (38, "okr_key_result"), (39, "okr_progress"), (40, "add_ons"), (41, "jira_issue"), (42, "wiki_catalog")])
def test_placeholder_blocks_paragraph_literal_and_degraded_trace(btype: int, literal: str) -> None:
    blk = _blk(f"p{btype}", btype, DOC_ID)
    result = map_blocks([blk], space_id=SPACE_ID, node_token=NODE_TOKEN)
    node = result.refs[0].node
    assert node.type is IrNodeType.PARAGRAPH
    assert node.text == f"[{literal}]"  # 方括号占位字面
    assert node.meta["raw_type"] == f"feishu.{literal}"
    assert result.degraded_blocks == {literal: 1}  # paragraph 占位计入 degraded_blocks（按块名）


# ---------------------------------------------------------------------------
# 30 sheet / 18 bitable 占位升格（data-model §1.18 v1.20 表格双工单契约批 2026-11-25）
# ---------------------------------------------------------------------------


def _sheet_payload(token: str | None = "shT0kEn") -> Any:
    """Sheet 载荷假体（lark-oapi 1.8.0 SDK 内省实测 Sheet{token, row_size, column_size}）。"""
    return type("Sheet", (), {"token": token, "row_size": 3, "column_size": 2})()


def _bitable_payload(token: str | None = "biT0kEn") -> Any:
    """Bitable 载荷假体（SDK 内省实测 Bitable{token, view_type}）。"""
    return type("Bitable", (), {"token": token, "view_type": 0})()


def test_sheet_block_upgrade_to_placeholder_table() -> None:
    """30 行升格协议（契约 30 行逐键）：type=table + text=`[sheet]` 占位字面恒保留
    （纯文本消费面兜底）+ raw_type=feishu.sheet + meta.placeholder=true +
    meta.sheet_token=payload Sheet.token 原值；升格不计入 degraded_blocks（非降级，
    回填由 worker 按 sheets API 通道承接）。"""
    blk = _blk("sh1", 30, DOC_ID, sheet=_sheet_payload("shT0kEn"))
    result = map_blocks([blk], space_id=SPACE_ID, node_token=NODE_TOKEN)
    node = result.refs[0].node
    assert node.type is IrNodeType.TABLE
    assert node.text == "[sheet]"
    assert node.meta["raw_type"] == "feishu.sheet"
    assert node.meta["placeholder"] is True
    assert node.meta["sheet_token"] == "shT0kEn"
    assert result.degraded_blocks == {}  # 升格 ≠ 降级（契约 30 行「不计入 degraded_blocks」钉注）


def test_bitable_block_upgrade_to_placeholder_table() -> None:
    """18 行升格协议：同 sheet 形态 + meta.bitable_token 原值留痕（Base API 域、
    非 sheets 通道——本批零回填、回填协议不钉）；升格不计入 degraded_blocks。"""
    blk = _blk("bi1", 18, DOC_ID, bitable=_bitable_payload("biT0kEn"))
    result = map_blocks([blk], space_id=SPACE_ID, node_token=NODE_TOKEN)
    node = result.refs[0].node
    assert node.type is IrNodeType.TABLE
    assert node.text == "[bitable]"
    assert node.meta["raw_type"] == "feishu.bitable"
    assert node.meta["placeholder"] is True
    assert node.meta["bitable_token"] == "biT0kEn"
    assert result.degraded_blocks == {}


def test_sheet_upgrade_missing_payload_token_empty_trace() -> None:
    """防御：载荷或 token 缺失 → sheet_token 空串留痕（worker 回填扫描按空 token
    跳过——无法寻址 sheets 通道的占位表保留占位形态）。"""
    blk = _blk("sh2", 30, DOC_ID, sheet=_sheet_payload(None))
    result = map_blocks([blk], space_id=SPACE_ID, node_token=NODE_TOKEN)
    node = result.refs[0].node
    assert node.type is IrNodeType.TABLE
    assert node.meta["placeholder"] is True
    assert node.meta["sheet_token"] == ""


def test_no_sheet_document_mapping_output_unchanged() -> None:
    """负例锁：不含 sheet(30)/bitable(18) 块的真实文档映射输出零变化——本批唯一
    动面 = 18/30 两分支；其余映射面（含 UNKNOWN(16) 兜底降级、容器、合并容器、
    31 真表格）逐节点锁死，防升格改造波及无关分支。"""
    blocks: list[Any] = [
        _text_blk("n1", "第一段", DOC_ID),
        _blk("n2", 5, DOC_ID, heading3=_payload([_elem(text_run=_run("一级标题"))])),  # heading3 → level=3
        _blk("n3", 15, DOC_ID, quote=_payload([_elem(text_run=_run("引用句"))])),
        _blk("n4", 12, DOC_ID, ["n4a", "n4b"], bullet=_payload([_elem(text_run=_run("列表项"))])),
        _text_blk("n4a", "条目一", "n4"),
        _text_blk("n4b", "条目二", "n4"),
        _blk("n5", 16, DOC_ID),  # UNKNOWN 兜底（枚举外降级留痕）
        _blk("pg", 1, DOC_ID, ["n1", "n2", "n3", "n4", "n5"], page=_payload()),
    ]
    result = map_blocks(blocks, space_id=SPACE_ID, node_token=NODE_TOKEN)
    assert result.root_order == [
        "fs:blk:n1", "fs:blk:n2", "fs:blk:n3", "fs:blk:n4",
    ]  # n5 无可提取文本不产节点（C3=(a)）、divider/page 亦不产
    by_id = {r.node.id: r.node for r in result.refs}
    assert by_id["fs:blk:n1"].type is IrNodeType.PARAGRAPH
    assert by_id["fs:blk:n2"].type is IrNodeType.HEADING and by_id["fs:blk:n2"].level == 3
    assert by_id["fs:blk:n3"].type is IrNodeType.QUOTE
    assert by_id["fs:blk:n4"].type is IrNodeType.LIST
    assert by_id["fs:blk:n4"].text == "列表项\n条目一\n条目二"  # 合并容器拼接形态不变
    assert result.degraded_blocks == {"block_16": 1}  # 仅 UNKNOWN 留痕，零 sheet/bitable 键
    for node in by_id.values():
        assert "placeholder" not in node.meta  # 无任何占位表（image 亦不在本 fixture）
        assert "sheet_token" not in node.meta and "bitable_token" not in node.meta


def test_divider_and_page_root_order() -> None:
    p1 = _text_blk("r1", "第一段", DOC_ID)
    div = _blk("d1", 22, DOC_ID, divider=_payload())
    p2 = _text_blk("r2", "第二段", DOC_ID)
    page = _blk("pg", 1, DOC_ID, ["r1", "d1", "r2"], page=_payload())
    result = map_blocks([page, p1, div, p2], space_id=SPACE_ID, node_token=NODE_TOKEN)
    by_id = {r.node.id for r in result.refs}
    assert "fs:blk:pg" not in by_id  # page 不产节点（文档根）
    assert "fs:blk:d1" not in by_id  # divider 不产节点
    assert result.root_order == ["fs:blk:r1", "fs:blk:r2"]  # 显示序权威 = page children 序


def test_image_placeholder_shape_and_file_attachment_shape() -> None:
    image = _blk("img1", 27, DOC_ID, image=type("I", (), {"token": "img_tok_v3"})())
    file = _blk("f1", 23, DOC_ID, file=type("F", (), {"token": "boxcnFileTok", "name": "年度报告.pdf"})())
    result = map_blocks([image, file], space_id=SPACE_ID, node_token=NODE_TOKEN)
    by_id = {r.node.id: r.node for r in result.refs}
    img = by_id["fs:blk:img1"]
    assert img.type is IrNodeType.IMAGE
    assert img.meta["placeholder"] is True  # 先占位；素材下载后 worker 回填 meta.minio_key
    assert img.meta["image_token"] == "img_tok_v3"
    att = by_id["fs:blk:f1"]
    assert att.type is IrNodeType.FILE_ATTACHMENT
    assert att.children == []
    assert att.meta["parse_status"] == "pending"  # §13.9⑥ 既有字面
    assert att.meta["file_token"] == "boxcnFileTok"
    assert att.meta["file_name"] == "年度报告.pdf"
    assert att.title == "年度报告.pdf"


class _OkMinio:
    """put 成功假体（消费流内容即弃——本层只验键面与回填语义）。"""

    def bucket_exists(self, bucket: str) -> bool:
        return True

    def put_object(self, bucket: str, key: str, stream: Any, length: int, content_type: str = "") -> None:
        stream.read()


class _BoomMinio:
    def bucket_exists(self, bucket: str) -> bool:
        return True

    def put_object(self, *a: Any, **k: Any) -> None:
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_image_placeholder_backfill_minio_key_and_failure_keeps_placeholder() -> None:
    """映射表 27 行回填面：占位节点 → 素材落 MinIO 成功后回填 meta.minio_key（占位标记
    保留）；素材通道失败（IrStorageError）→ 节点保持占位原样、无 minio_key——图片素材
    失败不算文档失败（§1.18 映射表 27 行；下载/落桶异常由 worker 捕获留痕，本层锁
    「失败不回填、占位不动」的映射器/存储接缝语义）。"""
    from app.rag.feishu_client import store_image_material
    from app.rag.storage import IrStorageError

    user_id = "00000000-0000-4000-8000-000000000001"
    doc_uuid = "11111111-1111-4111-8111-111111111111"

    image = _blk("img2", 27, DOC_ID, image=type("I", (), {"token": "img_tok_v3"})())
    node = map_blocks([image], space_id=SPACE_ID, node_token=NODE_TOKEN).refs[0].node
    assert node.meta["placeholder"] is True  # 先占位
    assert "minio_key" not in node.meta

    key = await store_image_material(user_id, doc_uuid, "img2", "png", b"png-bytes", minio_client=_OkMinio())
    assert key.endswith("/img/img2.png")  # 实现面键布局（kb/{u}/{d}/img/{block_id}.{ext}）
    node.meta["minio_key"] = key  # worker 回填（meta dict 直写，validate_assignment 照常过）
    assert node.meta["minio_key"] == key
    assert node.meta["placeholder"] is True  # 回填不抹占位标记（image_token 溯源面保留）

    # 失败路径（独立映射）：素材落桶异常 → 不回填，节点占位原样（不算文档失败）
    image2 = _blk("img3", 27, DOC_ID, image=type("I", (), {"token": "tok"})())
    node2 = map_blocks([image2], space_id=SPACE_ID, node_token=NODE_TOKEN).refs[0].node
    with pytest.raises(IrStorageError):
        await store_image_material(user_id, doc_uuid, "img3", "png", b"x", minio_client=_BoomMinio())
    assert node2.meta["placeholder"] is True
    assert "minio_key" not in node2.meta


def test_view_transparent_expand_with_trace() -> None:
    """33 view 子块形态待核——按「不丢正文」先展开 + transparent_counts 留痕（R-b/待核项）。"""
    inner1 = _text_blk("v_p1", "视图正文一", "v1")
    inner2 = _text_blk("v_p2", "视图正文二", "v1")
    view = _blk("v1", 33, DOC_ID, ["v_p1", "v_p2"], view=_payload())
    result = map_blocks([view, inner1, inner2], space_id=SPACE_ID, node_token=NODE_TOKEN)
    by_id = {r.node.id for r in result.refs}
    assert "fs:blk:v1" not in by_id  # 透明：view 自身不产节点
    assert result.root_order == ["fs:blk:v_p1", "fs:blk:v_p2"]  # children 序展开，正文零丢失
    assert result.transparent_counts == {"view": 1}  # 留痕（实现面诊断）


@pytest.mark.parametrize("btype", [24, 25, 36])
def test_other_transparent_containers_expand(btype: int) -> None:
    inner = _text_blk("g_p", "容器正文", f"g{btype}")
    holder = _blk(f"g{btype}", btype, DOC_ID, ["g_p"])
    result = map_blocks([holder, inner], space_id=SPACE_ID, node_token=NODE_TOKEN)
    assert result.root_order == ["fs:blk:g_p"]
    assert "callout" not in result.refs[0].node.meta  # 非 callout 透明容器不叠 callout 标记


# ---------------------------------------------------------------------------
# 行内元素（通用规则）
# ---------------------------------------------------------------------------


def test_inline_elements_text_run_link_and_placeholders() -> None:
    elements = [
        _elem(text_run=_run("plain")),
        _elem(text_run=_run("链接", link_url="https://example.com/a")),
        _elem(mention_user=type("MU", (), {"user_id": "u1"})()),
        _elem(file=type("IF", (), {"file_token": "ft"})()),
        _elem(inline_block=type("IB", (), {"block_id": "ib1"})()),
        _elem(link_preview=type("LP", (), {"url": "https://example.com/b"})()),
        _elem(reminder=type("RM", (), {"expire_time": 1})()),  # reminder 丢弃
        _elem(equation=type("EQ", (), {"content": "E=mc^2"})()),
    ]
    para = _blk("inl1", 2, DOC_ID, text=_payload(elements))
    result = map_blocks([para], space_id=SPACE_ID, node_token=NODE_TOKEN)
    text = result.refs[0].node.text or ""
    assert text == "plain[链接](https://example.com/a)[mention_user][file][inline_block][link_preview]E=mc^2"


# ---------------------------------------------------------------------------
# merge_info 合并单元格（待核项实现：文本落左上格、余格空串 + 留痕）
# ---------------------------------------------------------------------------


def test_merge_info_top_left_keeps_text_rest_empty_with_trace() -> None:
    cells = ["r0c0", "r0c1", "r1c0", "r1c1"]
    # 2×2：首格 row_span=2 col_span=2（行主序 4 项，被吸收格 span 记 1——形态按实测兜底）
    merge = [
        type("M", (), {"row_span": 2, "col_span": 2})(),
        type("M", (), {"row_span": 1, "col_span": 1})(),
        type("M", (), {"row_span": 1, "col_span": 1})(),
        type("M", (), {"row_span": 1, "col_span": 1})(),
    ]
    table, extra = _simple_table("tm1", DOC_ID, [f"c{i}" for i in range(4)], cells, row_size=2, column_size=2, merge_info=merge)
    result = map_blocks([table, *extra], space_id=SPACE_ID, node_token=NODE_TOKEN)
    node = next(r.node for r in result.refs if r.node.id == "fs:blk:tm1")
    rows = node.meta["rows"]
    assert rows == [["r0c0", ""], ["", ""]]  # 文本落合并区左上格、其余格空串
    assert node.meta["merged_cells"] == 3  # 留痕：被吸收格计数
    assert node.meta["col_count"] == 2
    # v1.20 扩容：稠密跨度矩阵随合并区写入（锚点格承载真实跨度）
    assert node.meta["cell_spans"] == [[[2, 2], [1, 1]], [[1, 1], [1, 1]]]


def test_merge_info_mismatch_is_ignored_defensively() -> None:
    """merge_info 长度与 cells 不对齐 → 整体忽略（形态待核的防御面），正文不丢。"""
    cells = ["x1", "x2"]
    merge = [type("M", (), {"row_span": 5, "col_span": 5})()]  # 数量不符
    table, extra = _simple_table("tm2", DOC_ID, ["m1", "m2"], cells, row_size=1, column_size=2, merge_info=merge)
    result = map_blocks([table, *extra], space_id=SPACE_ID, node_token=NODE_TOKEN)
    node = next(r.node for r in result.refs if r.node.id == "fs:blk:tm2")
    assert node.meta["rows"] == [["x1", "x2"]]
    assert "merged_cells" not in node.meta
    assert "cell_spans" not in node.meta


# ---------------------------------------------------------------------------
# meta.cell_spans 稠密跨度矩阵 + header_row/header_column 透传
# （2026-11-25 表格双工单契约批 v1.20——data-model §1.18 映射表 31 行 meta 扩容）
# ---------------------------------------------------------------------------


def _merge(row_span: int, col_span: int) -> Any:
    return type("M", (), {"row_span": row_span, "col_span": col_span})()


def _table_node(bid: str, result: Any) -> Any:
    return next(r.node for r in result.refs if r.node.id == f"fs:blk:{bid}")


def test_cell_spans_dense_matrix_written_on_merge() -> None:
    """merge_info 有跨度 → meta.cell_spans 稠密 R×C 矩阵（锚点承载真实跨度、其余 [1,1]）。"""
    cells = ["a", "b", "c", "d", "e", "f"]
    # 2×3：首格 row_span=2 col_span=2（覆盖 (0,0)(0,1)(1,0)(1,1)），余格 span 记 1
    merge = [_merge(2, 2), *(_merge(1, 1) for _ in range(5))]
    table, extra = _simple_table(
        "tc1", DOC_ID, [f"k{i}" for i in range(6)], cells,
        row_size=2, column_size=3, merge_info=merge,
    )
    result = map_blocks([table, *extra], space_id=SPACE_ID, node_token=NODE_TOKEN)
    node = _table_node("tc1", result)
    assert node.meta["rows"] == [["a", "", "c"], ["", "", "f"]]  # 锚点单写形态不变
    assert node.meta["merged_cells"] == 3
    assert node.meta["cell_spans"] == [
        [[2, 2], [1, 1], [1, 1]],
        [[1, 1], [1, 1], [1, 1]],
    ]


def test_cell_spans_omitted_without_merge_info() -> None:
    """无合并区 → cell_spans 整键省略（缺省=全 [1,1]，meta 字节最小化）。"""
    table, extra = _simple_table(
        "tc2", DOC_ID, ["p1", "p2", "p3", "p4"], ["a", "b", "c", "d"],
        row_size=2, column_size=2,
    )
    result = map_blocks([table, *extra], space_id=SPACE_ID, node_token=NODE_TOKEN)
    node = _table_node("tc2", result)
    assert "cell_spans" not in node.meta
    assert "header_row" not in node.meta
    assert "header_column" not in node.meta


def test_header_flags_passthrough_truthy_only() -> None:
    """TableProperty.header_row/header_column 原值透传（真值写入；false/缺省 →
    键省略，消费侧缺省 false 同语义——meta 字节最小化沿 cell_spans 缺省省略先例）。"""
    table, extra = _simple_table(
        "tc3", DOC_ID, ["p1", "p2"], ["a", "b"],
        row_size=1, column_size=2, header_row=True, header_column=False,
    )
    result = map_blocks([table, *extra], space_id=SPACE_ID, node_token=NODE_TOKEN)
    node = _table_node("tc3", result)
    assert node.meta["header_row"] is True
    assert "header_column" not in node.meta  # false → 省略（缺省 false 同语义）


def test_header_flags_and_cell_spans_coexist() -> None:
    """表头标志与合并区并存：三键同表齐写（chunker 表头重携判定原料）。"""
    cells = ["h1", "h2", "d1", ""]
    merge = [_merge(1, 2), _merge(1, 1), _merge(1, 1), _merge(1, 1)]
    table, extra = _simple_table(
        "tc4", DOC_ID, [f"k{i}" for i in range(4)], cells,
        row_size=2, column_size=2, merge_info=merge,
        header_row=True, header_column=True,
    )
    result = map_blocks([table, *extra], space_id=SPACE_ID, node_token=NODE_TOKEN)
    node = _table_node("tc4", result)
    assert node.meta["header_row"] is True
    assert node.meta["header_column"] is True
    assert node.meta["cell_spans"] == [
        [[1, 2], [1, 1]],
        [[1, 1], [1, 1]],
    ]


def test_cell_spans_area_overflow_self_check_degrades_with_trace() -> None:
    """自检②面积溢出（末格 row_span=2 出 2 行矩阵）→ 整表降级：键省略 + degraded_blocks
    留痕；正文零变化（降级只动 meta 键，行主序余格 span=1 的兜底形态不变）。"""
    cells = ["a", "b", "c", "d"]
    merge = [_merge(1, 1), _merge(1, 1), _merge(1, 1), _merge(2, 1)]  # idx3→(1,1) 出界
    table, extra = _simple_table(
        "tc5", DOC_ID, [f"k{i}" for i in range(4)], cells,
        row_size=2, column_size=2, merge_info=merge,
    )
    result = map_blocks([table, *extra], space_id=SPACE_ID, node_token=NODE_TOKEN)
    node = _table_node("tc5", result)
    assert "cell_spans" not in node.meta
    assert result.degraded_blocks == {"table_cell_spans": 1}
    assert node.meta["rows"] == [["a", "b"], ["c", "d"]]  # 正文零变化


def test_cell_spans_overlap_self_check_degrades_with_trace() -> None:
    """自检③重叠覆盖（第二锚点落第一锚点已声明区）→ 键省略 + 留痕；
    行文本仍沿既有锚点单写口径（降级只省 cell_spans 键）。"""
    cells = ["a", "b", "c", "d", "e", "f", "g", "h", "i"]
    merge = [
        _merge(2, 2),  # (0,0) 覆盖 rows0-1 × cols0-1
        _merge(1, 1), _merge(1, 1), _merge(1, 1),
        _merge(1, 2),  # (1,1) 与首锚点重叠
        _merge(1, 1), _merge(1, 1), _merge(1, 1), _merge(1, 1),
    ]
    table, extra = _simple_table(
        "tc6", DOC_ID, [f"k{i}" for i in range(9)], cells,
        row_size=3, column_size=3, merge_info=merge,
    )
    result = map_blocks([table, *extra], space_id=SPACE_ID, node_token=NODE_TOKEN)
    node = _table_node("tc6", result)
    assert "cell_spans" not in node.meta
    assert result.degraded_blocks == {"table_cell_spans": 1}
    # 行文本仍沿既有锚点单写口径（降级只省 cell_spans 键，正文零变化）
    assert node.meta["rows"] == [["a", "", "c"], ["", "", ""], ["g", "h", "i"]]


def test_cell_spans_incomplete_matrix_self_check_degrades_with_trace() -> None:
    """自检「矩阵不完整」（声明 2×2=4 格但只取回 3 格 → R×C 有未覆盖格残留）
    → 键省略 + degraded_blocks 留痕。"""
    cells = ["a", "b", "c"]
    merge = [_merge(1, 1), _merge(2, 1), _merge(1, 1)]
    table, extra = _simple_table(
        "tc7", DOC_ID, [f"k{i}" for i in range(3)], cells,
        row_size=2, column_size=2, merge_info=merge,
    )
    result = map_blocks([table, *extra], space_id=SPACE_ID, node_token=NODE_TOKEN)
    node = _table_node("tc7", result)
    assert "cell_spans" not in node.meta
    assert result.degraded_blocks == {"table_cell_spans": 1}


# ---------------------------------------------------------------------------
# 文档节点 + parse_meta.feishu（data-model §1.7 / §1.18）
# ---------------------------------------------------------------------------


def test_build_document_node_meta_shape() -> None:
    doc = build_document_node(
        node_token=NODE_TOKEN, title="手册", obj_type="docx", obj_edit_time=1737100000, space_id=SPACE_ID
    )
    assert doc.id == DOC_ID  # fs:node:{node_token}
    assert doc.type is IrNodeType.DOCUMENT
    assert doc.title == "手册"
    assert doc.doc_kind == "docx"
    assert doc.meta["raw_type"] == "docx"  # wiki 节点 obj_type 原值
    assert doc.meta["obj_edit_time"] == 1737100000  # 契约键名 obj_edit_time（命名统一待核项处置）
    assert doc.loc is not None and doc.loc.origin == "feishu"
    assert doc.loc.space_id == SPACE_ID and doc.loc.node_token == NODE_TOKEN


def test_build_document_node_rejects_non_document_obj_type() -> None:
    with pytest.raises(ValueError):
        build_document_node(node_token=NODE_TOKEN, title="x", obj_type="mindnote", obj_edit_time=0, space_id=SPACE_ID)


def test_feishu_loc_obj_edit_time_key_name() -> None:
    """命名统一待核项：loc 源定位键以契约 obj_edit_time 为准（原 edit_time 已收窄统一）。"""
    loc = FeishuLoc(origin="feishu", space_id=SPACE_ID, node_token="tok", obj_edit_time=42)
    assert loc.obj_edit_time == 42


def test_build_feishu_parse_meta_four_keys_and_optional_degraded() -> None:
    meta = build_feishu_parse_meta(
        space_id=SPACE_ID, node_token=NODE_TOKEN, obj_type="docx", obj_edit_time=1737100000, degraded_blocks={"bitable": 2, "block_16": 1}
    )
    assert meta == {
        "space_id": SPACE_ID,
        "node_token": NODE_TOKEN,
        "obj_type": "docx",
        "obj_edit_time": 1737100000,
        "degraded_blocks": {"bitable": 2, "block_16": 1},
    }
    no_degraded = build_feishu_parse_meta(
        space_id=SPACE_ID, node_token=NODE_TOKEN, obj_type="docx", obj_edit_time=1, degraded_blocks={}
    )
    assert "degraded_blocks" not in no_degraded  # 无降级块时省略键
    assert set(no_degraded) == {"space_id", "node_token", "obj_type", "obj_edit_time"}  # 四键必带
    assert PARSER_FEISHU_BLOCKS == "feishu_blocks"  # A34 GWT#1 通道标识


# ---------------------------------------------------------------------------
# 结构完整性：refs 可直接喂 attach_block_tree（parent 链闭合、ID 前缀、双写序一致）
# ---------------------------------------------------------------------------


def test_refs_parent_chain_closed_and_children_ids_consistent() -> None:
    inner = _text_blk("s_p", "容器内正文", "s1")
    callout = _blk("s1", 19, DOC_ID, ["s_p"], callout=_payload())
    para = _text_blk("s2", "根层段落", DOC_ID)
    page = _blk("pg", 1, DOC_ID, ["s1", "s2"], page=_payload())
    result: FeishuMappingResult = map_blocks([page, callout, inner, para], space_id=SPACE_ID, node_token=NODE_TOKEN)
    ref_ids = {r.node.id for r in result.refs}
    assert result.root_order == ["fs:blk:s_p", "fs:blk:s2"]
    for ref in result.refs:
        assert ref.node.id.startswith("fs:blk:")
        assert ref.node.parent_id == DOC_ID  # 本例全部挂文档根（透明展开后）
        # 双写一致：children_ids ⊆ ref ids（本例叶层为空数组）
        assert set(ref.children_ids) <= ref_ids


# ---------------------------------------------------------------------------
# 退化 cell_spans 省键（评审 P3：merge_info 在场但全 [1,1] 时不再写全 [1,1] 矩阵
# ——契约「无合并区整键省略〔缺省=全 [1,1]〕」的退化形态兑现，白增 ir.json 体量消除）
# ---------------------------------------------------------------------------


def test_cell_spans_degenerate_all_one_matrix_omitted() -> None:
    """merge_info 在场但全 [1,1]（无真实合并区）→ cell_spans 整键省略且零 degraded
    留痕（非自检失败——省键≠降级）；merged_cells 同态省略（0 值既有口径）。"""
    cells = ["a", "b", "c", "d"]
    merge = [_merge(1, 1) for _ in range(4)]
    table, extra = _simple_table(
        "td1", DOC_ID, [f"g{i}" for i in range(4)], cells,
        row_size=2, column_size=2, merge_info=merge,
    )
    result = map_blocks([table, *extra], space_id=SPACE_ID, node_token=NODE_TOKEN)
    node = _table_node("td1", result)
    assert "cell_spans" not in node.meta
    assert "merged_cells" not in node.meta
    assert "table_cell_spans" not in result.degraded_blocks


# ---------------------------------------------------------------------------
# 块遍历防环（评审 P3：畸形循环 children 引用无限递归——tree_assembler 防环先例同款）
# ---------------------------------------------------------------------------


def test_map_children_self_loop_terminates() -> None:
    """块自环（children 指向自身）→ 映射终止不 RecursionError，块自身仍产节点。"""
    looping = _blk("lp1", 2, DOC_ID, ["lp1"], text=_payload([_elem(text_run=_run("自环正文"))]))
    result = map_blocks([looping], space_id=SPACE_ID, node_token=NODE_TOKEN)
    ids = [r.node.id for r in result.refs]
    assert "fs:blk:lp1" in ids  # 节点仍产出（环边跳过、正文不丢）


def test_map_children_mutual_loop_terminates() -> None:
    """双块互环（A→B→A）→ 映射有限终止。"""
    a = _blk("la1", 2, DOC_ID, ["lb1"], text=_payload([_elem(text_run=_run("甲"))]))
    b = _blk("lb1", 2, DOC_ID, ["la1"], text=_payload([_elem(text_run=_run("乙"))]))
    result = map_blocks([a, b], space_id=SPACE_ID, node_token=NODE_TOKEN)
    ids = {r.node.id for r in result.refs}
    assert "fs:blk:la1" in ids and "fs:blk:lb1" in ids


def test_cell_text_cycle_terminates() -> None:
    """格内文本递归环（cell → paragraph → 自身 children 回指）→ 有限终止。"""
    loop_p = _blk("cp1", 2, "cell9", ["cp1"], text=_payload([_elem(text_run=_run("格内环"))]))
    cell = _blk("cell9", 32, "tc9", ["cp1"])
    table = _blk("tc9", 31, DOC_ID, ["cell9"], table=type(
        "Tb", (), {"cells": ["cell9"], "property": type("P", (), {"row_size": 1, "column_size": 1, "merge_info": None, "header_row": None, "header_column": None})()}
    )())
    result = map_blocks([table, cell, loop_p], space_id=SPACE_ID, node_token=NODE_TOKEN)
    node = _table_node("tc9", result)
    assert node.meta["rows"][0][0] == "格内环"  # 环块文本仍被提取一次
