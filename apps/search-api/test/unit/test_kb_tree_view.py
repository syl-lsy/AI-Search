"""unit 层：完全树视图投影（tree_view——heading 层级物化 + 文档/森林视图）。

TDD 红绿，纯函数零网络零 DB，程序化合成样本不含真实数据。钉住的语义：

- **平铺→嵌套物化**：飞书 SDK 通道的内容块平铺在 document 下（heading 与正文同为
  page 直接子块、heading 仅带 `level`）→ 视图按 heading 层栈物化嵌套；零节点丢失
  （投影前后节点计数相等）；
- **嵌套幂等**：本地通道（markitdown/docling）heading 子树天然嵌套 → 视图只做字段
  投影、不二次挪动；二次调用幂等；投影为纯函数（输入树逐字节不变）；
- **视图卫生**：保留 `id/type/parent_id/title/level/text/meta/loc/children`；
  **`parent_id` 回填视图父节点 id**（视图自洽可导航：嵌套块 = 所嵌 heading id、
  标题前根层块 = document 根 id、document 根 = None 树根）——与 IR 本体 parent_id
  （生产事实=指向文档根）语义不同；内容块剥除 `catalog_ids/catalog_path/doc_kind`
  （catalog 面仅在 document 根保留）；`meta`（含 raw_type/rows 等）与 `loc` 原样
  透传；
- **深层保留**：table 格内子块 / list 子块等非 heading 嵌套照旧递归投影；
- **forest 合并**：沿 catalog_ids/catalog_path（C9 物化口径：catalog_path 含自身
  标题）挂目录树——同链同父、链尾=文档自身标题，不重复挂一层；空链挂 roots 顶层。
- **通道按 id 前缀判定**（2026-11-25 契约四前缀）：`fs:node:`/`fs:blk:` = 飞书平铺
  通道（层栈物化）、`loc:doc:`/`loc:blk:` = 本地/url 已嵌套通道（只投影不重排），以
  文档根节点前缀为准——拓扑启发式的残余缺陷（全空标题形态误吸尾块）由此消除；
- **level 缺省 1**：无 level 的 heading 同样入栈物化（对齐 chunker 既有口径）；
- **catalog 面仅 document**：heading 不投影 catalog 键（对齐 tree_assembler 只对
  DOCUMENT 物化的事实）；森林文档节点补 `loc`（溯源下钻 node_token 回跳投影），
  文档级七键根不变；`document_count` 按实际入树文档数去重计。
"""

from typing import Any

from app.rag.ir_schema import FeishuLoc, IrNode, serialize_ir
from app.rag.tree_view import (
    build_document_tree_view,
    build_forest_view,
    build_hierarchy_view,
)


# ---------------------------------------------------------------------------
# 构造辅助（合成数据）
# ---------------------------------------------------------------------------

_FS = "fs:blk:"
_LOC = "loc:blk:"
_FS_DOC = "fs:node:"
_LOC_DOC = "loc:doc:"


def _blk(
    nid: str,
    type_: str,
    *,
    prefix: str = _FS,
    raw: Any = 2,
    **kw: Any,
) -> IrNode:
    payload: dict[str, Any] = {
        "id": f"{prefix}{nid}",
        "type": type_,
        "meta": {"raw_type": raw},
    }
    payload.update(kw)
    return IrNode(**payload)


def _doc(
    nid: str,
    *,
    title: str | None = None,
    kind: str = "docx",
    catalog_ids: list[str] | None = None,
    catalog_path: list[str] | None = None,
    children: list[IrNode] | None = None,
    prefix: str = _FS_DOC,
    loc: FeishuLoc | None = None,
) -> IrNode:
    return IrNode(
        id=f"{prefix}{nid}",
        type="document",
        title=title or f"文档 {nid}",
        doc_kind=kind,
        catalog_ids=catalog_ids or [],
        catalog_path=catalog_path or [],
        loc=loc,
        meta={"raw_type": kind},
        children=children or [],
    )


def _ids(node_view: dict[str, Any]) -> list[str]:
    return [child["id"] for child in node_view["children"]]


def _count_ir(node: IrNode) -> int:
    return 1 + sum(_count_ir(child) for child in node.children)


def _count_view(view: dict[str, Any]) -> int:
    return 1 + sum(_count_view(child) for child in view["children"])


def _flat_feishu_document() -> IrNode:
    """飞书形态：内容块平铺在 document 下，heading 仅带 level（含交错正文/代码/表格）。"""
    return _doc(
        "flatdoc",
        title="史普文档",
        catalog_ids=["fs:node:space-root"],
        catalog_path=["空间根目录", "史普文档"],
        children=[
            _blk("h1", "heading", raw=3, level=1, title="第一章"),
            _blk("p1", "paragraph", text="第一章前言"),
            _blk("h2", "heading", raw=4, level=2, title="第一节"),
            _blk("p2", "paragraph", text="第一节正文"),
            _blk("c1", "code", raw=14, text="print(1)\n"),
            _blk("h2b", "heading", raw=4, level=2, title="第二节"),
            _blk(
                "t1",
                "table",
                raw=31,
                rows=2,
                col_count=2,
                children=[_blk("cellp", "paragraph", text="格内段")],
            ),
            _blk("h1b", "heading", raw=3, level=1, title="第二章"),
            _blk("p3", "paragraph", text="第二章前言"),
            _blk("h3", "heading", raw=5, level=3, title="1.1.1 深层"),
            _blk("p4", "paragraph", text="深层正文"),
        ],
    )


def _nested_local_document() -> IrNode:
    """本地形态：heading 子树天然嵌套（markdown 章节拓扑），文档与块同走 loc: 通道。"""
    return _doc(
        "nestdoc",
        title="本地文档",
        kind="upload",
        prefix=_LOC_DOC,
        children=[
            _blk("p0", "paragraph", prefix=_LOC, text="开篇"),
            _blk(
                "h1",
                "heading",
                prefix=_LOC,
                raw=3,
                level=1,
                title="第一章",
                children=[
                    _blk("p1", "paragraph", prefix=_LOC, text="第一章前言"),
                    _blk(
                        "h2",
                        "heading",
                        prefix=_LOC,
                        raw=4,
                        level=2,
                        title="第一节",
                        children=[
                            _blk("p2", "paragraph", prefix=_LOC, text="第一节正文"),
                            _blk(
                                "h3",
                                "heading",
                                prefix=_LOC,
                                raw=5,
                                level=3,
                                title="1.1.1",
                                children=[
                                    _blk("p3", "paragraph", prefix=_LOC, text="深层正文"),
                                ],
                            ),
                        ],
                    ),
                    _blk(
                        "h2b",
                        "heading",
                        prefix=_LOC,
                        raw=4,
                        level=2,
                        title="第二节",
                        children=[
                            _blk("p4", "paragraph", prefix=_LOC, text="第二节正文"),
                        ],
                    ),
                ],
            ),
            _blk("p5", "paragraph", prefix=_LOC, text="收尾"),
        ],
    )


# ---------------------------------------------------------------------------
# 1) 平铺 → 嵌套物化
# ---------------------------------------------------------------------------


def test_flat_blocks_are_materialized_into_heading_hierarchy() -> None:
    doc = _flat_feishu_document()

    view = build_hierarchy_view(doc)

    # 顶层只剩两个 L1 标题；所有非 heading 块归入栈顶最近 heading
    assert _ids(view) == ["fs:blk:h1", "fs:blk:h1b"]
    h1, h1b = view["children"]
    assert h1["type"] == "heading"
    assert h1["level"] == 1
    assert h1["title"] == "第一章"
    assert _ids(h1) == ["fs:blk:p1", "fs:blk:h2", "fs:blk:h2b"]
    assert _ids(h1["children"][1]) == ["fs:blk:p2", "fs:blk:c1"]
    assert _ids(h1["children"][2]) == ["fs:blk:t1"]
    # table 仍是 table 节点，格内子块嵌在 table 下
    assert _ids(h1["children"][2]["children"][0]) == ["fs:blk:cellp"]
    # 跳级 L3 挂在更浅的最近 heading（第二章）下，不再独立成章
    assert _ids(h1b) == ["fs:blk:p3", "fs:blk:h3"]
    assert _ids(h1b["children"][1]) == ["fs:blk:p4"]

    # parent_id 回填视图父 id（视图自洽：child.parent_id == 父节点.id）
    assert view["parent_id"] is None  # document 根=树根
    assert h1["parent_id"] == doc.id
    assert h1b["parent_id"] == doc.id
    assert all(child["parent_id"] == h1["id"] for child in h1["children"])
    assert h1b["children"][1]["parent_id"] == h1b["id"]
    table_view = h1["children"][2]["children"][0]
    assert table_view["children"][0]["parent_id"] == table_view["id"]

    # 零节点丢失：投影前后节点计数相等
    assert _count_view(view) == _count_ir(doc) == 13


def test_heading_without_level_defaults_to_level_one() -> None:
    """P1：level 缺省的 heading 仍入栈物化（对齐 chunker「缺省 1」口径）。"""
    doc = _doc(
        "nolevel",
        title="无 level 标题文档",
        children=[
            # level=2 的标题先占栈；其后无 level 标题按 1 处理 → 弹栈回到根层
            _blk("h2", "heading", raw=4, level=2, title="二级"),
            _blk("hx", "heading", raw=3, title="缺省层级标题"),
            _blk("p", "paragraph", text="缺省标题下正文"),
        ],
    )

    view = build_hierarchy_view(doc)

    assert _ids(view) == ["fs:blk:h2", "fs:blk:hx"]
    h2_view, hx_view = view["children"]
    # 无 level 的 heading 自身视图不出 level 键（可选键省略口径）
    assert "level" not in hx_view
    # 后续块挂其下，parent_id 回填该 heading 的视图 id
    assert _ids(hx_view) == ["fs:blk:p"]
    assert hx_view["children"][0]["parent_id"] == hx_view["id"]
    assert hx_view["parent_id"] == doc.id
    # 零节点丢失
    assert _count_view(view) == _count_ir(doc) == 4


def test_heading_without_level_first_block_group_nests_under_it() -> None:
    """P1：无 level 的 heading 可作首个标题入栈（无前序标题也照常成父）。"""
    doc = _doc(
        "nolevel-first",
        title="首标题无 level",
        children=[
            _blk("hx", "heading", raw=3, title="首个标题"),
            _blk("p1", "paragraph", text="一"),
            _blk("p2", "paragraph", text="二"),
        ],
    )

    view = build_hierarchy_view(doc)

    assert _ids(view) == ["fs:blk:hx"]
    hx_view = view["children"][0]
    assert _ids(hx_view) == ["fs:blk:p1", "fs:blk:p2"]
    assert all(child["parent_id"] == hx_view["id"] for child in hx_view["children"])
    assert _count_view(view) == _count_ir(doc) == 4


def test_document_tree_view_root_shape_and_children() -> None:
    doc = _flat_feishu_document()

    tree = build_document_tree_view(doc)

    assert set(tree) == {
        "id",
        "type",
        "title",
        "doc_kind",
        "catalog_ids",
        "catalog_path",
        "children",
    }
    assert tree["id"] == doc.id
    assert tree["type"] == "document"
    assert tree["title"] == "史普文档"
    assert tree["doc_kind"] == "docx"
    assert tree["catalog_ids"] == ["fs:node:space-root"]
    assert tree["catalog_path"] == ["空间根目录", "史普文档"]
    # children 与层级物化视图一致（同一投影，不重复一份逻辑）
    assert tree["children"] == build_hierarchy_view(doc)["children"]


# ---------------------------------------------------------------------------
# 2) 嵌套拓扑幂等
# ---------------------------------------------------------------------------


def test_nested_topology_is_projected_idempotently() -> None:
    doc = _nested_local_document()
    snapshot = serialize_ir(doc)

    view = build_hierarchy_view(doc)

    # 嵌套关系原样保留（不二次挪动）
    assert _ids(view) == ["loc:blk:p0", "loc:blk:h1", "loc:blk:p5"]
    h1 = view["children"][1]
    assert _ids(h1) == ["loc:blk:p1", "loc:blk:h2", "loc:blk:h2b"]
    h2 = h1["children"][1]
    assert _ids(h2) == ["loc:blk:p2", "loc:blk:h3"]
    assert _ids(h2["children"][1]) == ["loc:blk:p3"]
    assert _ids(h1["children"][2]) == ["loc:blk:p4"]
    # 嵌套拓扑的 parent_id 同样回填视图父 id（根层块=document 根）
    assert all(child["parent_id"] == doc.id for child in view["children"])
    assert all(child["parent_id"] == h1["id"] for child in h1["children"])
    assert h2["children"][1]["parent_id"] == h2["id"]
    # 视图节点即文档视图根（同一子树投影）
    assert build_document_tree_view(doc)["children"] == view["children"]

    # 二次调用幂等；投影是纯函数（输入树逐字节不变）
    assert build_hierarchy_view(doc) == view
    assert serialize_ir(doc) == snapshot


# ---------------------------------------------------------------------------
# 2.5) 通道按 id 前缀判定（替代拓扑启发式）
# ---------------------------------------------------------------------------


def test_channel_is_decided_by_id_prefix_not_topology() -> None:
    """终审 P2-3 锁死用例：全空标题平铺 `[h1{}, h2{}, p]` 的两种通道形态。

    loc: 通道（本地/url 已嵌套）只投影不重排 → p 留在根层不被吸进标题；
    fs: 通道（飞书平铺）正常层栈物化 → p 挂最近标题 h2 下。
    """

    def _blocks(prefix: str) -> list[IrNode]:
        return [
            _blk("h1", "heading", prefix=prefix, raw=3, level=1, title="一"),
            _blk("h2", "heading", prefix=prefix, raw=4, level=2, title="1.1"),
            _blk("p", "paragraph", prefix=prefix, text="尾块"),
        ]

    nested_doc = _doc(
        "locchan", title="本地文档", kind="upload", prefix=_LOC_DOC, children=_blocks(_LOC)
    )
    nested_view = build_hierarchy_view(nested_doc)

    # 嵌套通道：三个块全部留在根层（不被层栈重排），parent 均回填文档根
    assert _ids(nested_view) == ["loc:blk:h1", "loc:blk:h2", "loc:blk:p"]
    assert all(child["parent_id"] == nested_doc.id for child in nested_view["children"])
    assert all(child["children"] == [] for child in nested_view["children"])

    flat_doc = _doc("flatchan", title="飞书文档", children=_blocks(_FS))
    flat_view = build_hierarchy_view(flat_doc)

    # 平铺通道：层栈物化 h1 > h2 > 尾块
    assert _ids(flat_view) == ["fs:blk:h1"]
    h1_view = flat_view["children"][0]
    assert _ids(h1_view) == ["fs:blk:h2"]
    h2_view = h1_view["children"][0]
    assert _ids(h2_view) == ["fs:blk:p"]
    assert h2_view["children"][0]["parent_id"] == h2_view["id"]
    assert h1_view["parent_id"] == flat_doc.id


# ---------------------------------------------------------------------------
# 3) 视图卫生：parent_id / catalog 面剥除，meta / loc 透传
# ---------------------------------------------------------------------------


def test_view_parent_id_points_to_view_parent_and_catalog_only_on_root() -> None:
    loc = FeishuLoc(
        origin="feishu",
        space_id="7708408008821771292",
        node_token="Wtp2node",
        block_id="Blk0001",
        obj_edit_time=1_700_000_000,
    )
    pua_text = "占位符 \U000f0001 原样透传"
    doc = _doc(
        "stripdoc",
        title="文档",
        catalog_ids=["fs:node:parent"],
        catalog_path=["父目录", "文档"],
        children=[
            # IR 本体 parent_id 陈旧（指向非文档根）：视图必须按位置回填 doc.id
            _blk(
                "bp",
                "paragraph",
                parent_id="fs:node:stale",
                meta={"raw_type": 2, "lang": "zh"},
                loc=loc,
                text=pua_text,
            ),
            _blk(
                "bh",
                "heading",
                raw=3,
                level=1,
                title="标题",
                children=[
                    # 平铺通道 IR parent_id=文档根；视图中它是 heading 的子块
                    _blk(
                        "bhc",
                        "paragraph",
                        parent_id="fs:node:stripdoc",
                        text="标题下正文",
                    ),
                ],
            ),
        ],
    )

    view = build_hierarchy_view(doc)

    # document 根=视图树根（parent_id 键在、值为 None）且保留 catalog 链；
    # 视图卫生剥除 doc_kind（文档视图根另有它）
    assert view["parent_id"] is None
    assert view["catalog_ids"] == ["fs:node:parent"]
    assert view["catalog_path"] == ["父目录", "文档"]
    assert "doc_kind" not in view

    p_view, h_view = view["children"]
    # parent_id 回填视图父 id——非 IR 本体值（陈旧值被视图位置覆盖）
    assert p_view["parent_id"] == doc.id
    assert h_view["parent_id"] == doc.id
    assert h_view["children"][0]["id"] == "fs:blk:bhc"
    assert h_view["children"][0]["parent_id"] == h_view["id"]  # 嵌套块=heading id

    # catalog 面只在 document 根：内容块/标题节点都不出这些键
    for node_view in (p_view, h_view):
        assert "catalog_ids" not in node_view
        assert "catalog_path" not in node_view
        assert "doc_kind" not in node_view
    # meta 原样透传（含扩展键）；loc 转纯 dict（JSON 模式）
    assert p_view["meta"] == {"raw_type": 2, "lang": "zh"}
    assert p_view["loc"] == {
        "origin": "feishu",
        "space_id": "7708408008821771292",
        "node_token": "Wtp2node",
        "block_id": "Blk0001",
        "obj_edit_time": 1_700_000_000,
    }
    # PUA document-ref 占位符原样透传（不解释不展开）
    assert p_view["text"] == pua_text
    # meta 为副本：改视图不污染 IR
    p_view["meta"]["lang"] = "en"
    assert doc.children[0].meta["lang"] == "zh"


def test_heading_never_emits_catalog_keys_even_if_ir_carries_them() -> None:
    """P2-1：catalog 面只在 document 节点物化（对齐 tree_assembler 事实）。

    heading 即便被防御性塞入 catalog 值（非契约语义），视图也不出这些键。
    """
    heading = _blk(
        "cath",
        "heading",
        raw=3,
        level=1,
        title="带目录标题",
        catalog_ids=["fs:node:x"],
        catalog_path=["目录", "带目录标题"],
    )
    doc = _doc("catalogdoc", title="文档", children=[heading])

    view = build_hierarchy_view(doc)

    assert view["catalog_ids"] == []
    assert view["catalog_path"] == []
    h_view = view["children"][0]
    assert h_view["id"] == "fs:blk:cath"
    assert "catalog_ids" not in h_view
    assert "catalog_path" not in h_view


# ---------------------------------------------------------------------------
# 4) 深层非 heading 嵌套保留
# ---------------------------------------------------------------------------


def test_deep_non_heading_nesting_is_preserved() -> None:
    lst = _blk(
        "l1",
        "list",
        raw=12,
        children=[_blk("li1", "paragraph", text="列表项")],
    )
    table = _blk(
        "tb",
        "table",
        raw=31,
        children=[_blk("cell", "paragraph", text="单元格"), lst],
    )
    doc = _doc("deepdoc", title="表格文档", children=[table])

    view = build_hierarchy_view(doc)

    assert _ids(view) == ["fs:blk:tb"]
    tb_view = view["children"][0]
    assert tb_view["type"] == "table"
    assert _ids(tb_view) == ["fs:blk:cell", "fs:blk:l1"]
    assert _ids(tb_view["children"][1]) == ["fs:blk:li1"]
    # 深层链上 parent_id 逐级指向视图父
    assert tb_view["parent_id"] == doc.id
    assert all(child["parent_id"] == tb_view["id"] for child in tb_view["children"])
    assert tb_view["children"][1]["children"][0]["parent_id"] == "fs:blk:l1"
    assert _count_view(view) == _count_ir(doc) == 5


# ---------------------------------------------------------------------------
# 5) forest 合并（catalog 链挂目录树）
# ---------------------------------------------------------------------------


def test_forest_view_merges_documents_along_catalog_chain() -> None:
    doc_a = _doc("A", title="根文档", children=[_blk("pa", "paragraph", text="A")])
    doc_b = _doc(
        "B",
        title="子文档B",
        catalog_ids=["fs:node:A"],
        catalog_path=["根文档", "子文档B"],
        children=[_blk("pb", "paragraph", text="B")],
    )
    doc_c = _doc(
        "C",
        title="文档C",
        catalog_ids=["fs:node:X", "fs:node:Y"],
        catalog_path=["目录X", "子目录Y", "文档C"],
        children=[_blk("pc", "paragraph", text="C")],
    )
    doc_d = _doc(
        "D",
        title="文档D",
        catalog_ids=["fs:node:X"],
        catalog_path=["目录X", "文档D"],
        children=[],
    )
    doc_e = _doc("E", title="散文档", children=[_blk("pe", "paragraph", text="E")])

    forest = build_forest_view([doc_a, doc_b, doc_c, doc_d, doc_e])

    assert forest["document_count"] == 5
    roots = forest["roots"]
    assert [r["title"] for r in roots] == ["根文档", "目录X", "散文档"]

    # 根文档同时是子文档 B 的父：同一节点承载自身文档视图 + 子节点（不重复挂叶）
    root_a = roots[0]
    assert root_a["type"] == "document"
    assert root_a["id"] == "fs:node:A"
    assert root_a["document"] is not None
    assert root_a["document"]["title"] == "根文档"
    assert root_a["document"]["children"][0]["parent_id"] == "fs:node:A"
    assert [c["id"] for c in root_a["children"]] == ["fs:node:B"]
    child_b = root_a["children"][0]
    assert child_b["type"] == "document"
    assert child_b["document"]["catalog_path"] == ["根文档", "子文档B"]

    # X 是合成目录（非文档）：自身 document 为 None，两个不同链的文档同父
    root_x = roots[1]
    assert root_x["type"] == "catalog"
    assert root_x["id"] == "fs:node:X"
    assert root_x["document"] is None
    # catalog_path 含自身标题（C9）→ 挂接不多挂一层：X 下恰两个子（Y、D）
    assert [c["title"] for c in root_x["children"]] == ["子目录Y", "文档D"]
    dir_y, leaf_d = root_x["children"]
    assert dir_y["type"] == "catalog"
    assert [c["id"] for c in dir_y["children"]] == ["fs:node:C"]
    assert leaf_d["document"]["catalog_path"] == ["目录X", "文档D"]

    # 空链文档挂 roots 顶层，标题取自身标题（不产生空目录层）
    root_e = roots[2]
    assert root_e["type"] == "document"
    assert root_e["document"]["catalog_path"] == []


def test_forest_view_upgrades_ancestor_node_when_document_arrives_later() -> None:
    # 子文档先入参：祖先 P 先成合成目录；随后 P 自身入参 → 同节点升级为文档节点
    child = _doc(
        "K",
        title="子K",
        catalog_ids=["fs:node:P"],
        catalog_path=["旧标题", "子K"],
        children=[_blk("pk", "paragraph", text="K")],
    )
    parent = _doc("P", title="父P", children=[_blk("pp", "paragraph", text="P")])

    forest = build_forest_view([child, parent])

    assert forest["document_count"] == 2
    assert len(forest["roots"]) == 1  # 不重复挂叶
    root = forest["roots"][0]
    assert root["id"] == "fs:node:P"
    assert root["type"] == "document"
    assert root["title"] == "父P"  # 自身标题覆盖目录层首见标题
    assert root["document"]["children"][0]["parent_id"] == "fs:node:P"
    assert [c["id"] for c in root["children"]] == ["fs:node:K"]


def test_forest_document_nodes_carry_loc_for_traceability_drilldown() -> None:
    """P2-2：森林文档节点补 `loc`（node_token 回跳投影）；文档级七键根不变。"""
    loc = FeishuLoc(
        origin="feishu",
        space_id="7708408008821771292",
        node_token="Wtp2forest",
        block_id="Blk0002",
        obj_edit_time=1_700_000_001,
    )
    doc_a = _doc("FA", title="森林根", loc=loc, children=[_blk("pfa", "paragraph", text="A")])
    doc_b = _doc(
        "FB",
        title="子文档FB",
        catalog_ids=["fs:node:FA"],
        catalog_path=["森林根", "子文档FB"],
        children=[],
    )
    doc_c = _doc(
        "FC",
        title="目录C文档",
        catalog_ids=["fs:node:DIR"],
        catalog_path=["合成目录", "目录C文档"],
        children=[],
    )

    forest = build_forest_view([doc_a, doc_b, doc_c])

    root_a = forest["roots"][0]
    assert root_a["type"] == "document"
    assert root_a["loc"] == loc.model_dump(mode="json")
    # 森林节点的文档视图仍是七键根（loc 只在森林层出现，不进文档视图根）
    assert set(root_a["document"]) == {
        "id",
        "type",
        "title",
        "doc_kind",
        "catalog_ids",
        "catalog_path",
        "children",
    }
    assert "loc" not in root_a["document"]
    # 无 loc 的文档叶 / 合成目录层：loc 键在、值为 None（键恒在口径）
    child_b = root_a["children"][0]
    assert child_b["loc"] is None
    catalog_dir = forest["roots"][1]
    assert catalog_dir["type"] == "catalog"
    assert catalog_dir["loc"] is None
    assert catalog_dir["children"][0]["loc"] is None


def test_forest_document_count_counts_distinct_documents() -> None:
    """P2-4：document_count 按实际入树文档数计（重复入参归并后只算一次）。"""
    doc = _doc("DUP", title="重复文档", children=[_blk("pd", "paragraph", text="x")])

    forest = build_forest_view([doc, doc])

    assert forest["document_count"] == 1
    assert len(forest["roots"]) == 1
    root = forest["roots"][0]
    assert root["type"] == "document"
    assert root["children"] == []


# ---------------------------------------------------------------------------
# 6) 空文档 / 无 heading 平铺
# ---------------------------------------------------------------------------


def test_document_without_headings_keeps_all_blocks_at_root() -> None:
    doc = _doc(
        "noheading",
        title="无标题文档",
        children=[
            _blk("np1", "paragraph", text="一"),
            _blk("np2", "paragraph", text="二"),
            _blk("nc1", "code", raw=14, text="x = 1"),
        ],
    )

    view = build_hierarchy_view(doc)

    assert _ids(view) == ["fs:blk:np1", "fs:blk:np2", "fs:blk:nc1"]
    assert all(child["parent_id"] == doc.id for child in view["children"])
    assert all(child["children"] == [] for child in view["children"])


def test_empty_document_view_has_empty_children() -> None:
    doc = _doc("emptydoc", title="空文档")

    view = build_hierarchy_view(doc)

    assert view["children"] == []
    assert build_document_tree_view(doc)["children"] == []
    assert build_forest_view([doc])["roots"][0]["document"]["children"] == []
