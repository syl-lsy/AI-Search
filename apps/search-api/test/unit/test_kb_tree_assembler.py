"""unit 层：飞书两层树装配器（M3 批次 1 / B16——ARCHITECTURE ADR-018 决策⑤、调研
feishu-kb-import-research-20261122.md §11 树装配层与防御四件、§12 探针量级；
TDD 红绿，纯函数零网络零 DB，程序化合成样本不含真实数据）。

钉住的语义（冻结契约，逐条对应测试）：
- **两层装配**（§11.1）：目录层 wiki node 平铺 → 完全节点树（`assemble_catalog_tree`）；
  内容层 docx block 平铺 → 块树挂 document 节点（`attach_block_tree`，children 数组序
  为显示序权威）；两层以 parent_id 边关联；
- **目录层根判定**（§11.2）：parent 空（None/空串）= 根；虚拟单根 `virtual:root` 承载
  森林（真根 + 孤儿 + 环切节点），子树 catalog_ids/catalog_path 沿树物化（§13.7
  「构造即一致」）；
- **防御四件**（§11.3）：①visited 集合按 id 防环（环边切断、节点留树、report 留痕）；
  ②孤儿节点（parent 引用缺失）兜底挂虚拟根/文档根留痕不崩；③截断安全阀（模块常量
  `MAX_NODES`，超限截断留痕——§12：生产不设探针式硬上限，931+ 实测量级 ×10 余量）；
  ④`node_type=shortcut` 计为引用边、不展开实体（节点保留、子树整支切断 + report 留痕）；
- **装配即双写**（§13.7/§13.8#2）：parent_id 与 children 由装配器同一次遍历双写；
  prev/next 兄弟指针装配期末重建（内存形态，序列化仍省略——ir_schema 钉注）；
- **931 量级合成样本**（§12 实测形态：depth1=4/depth2=38/depth3=343/depth4=425/
  depth5=121，docx=794+file=137）：装配正确性 + 全树兄弟一致性 + roundtrip。
"""

from typing import Any

import pytest

from app.rag.ir_schema import (
    VIRTUAL_ROOT_ID,
    IrNode,
    rebuild_siblings,
    serialize_ir,
)
from app.rag.ir_schema import deserialize_ir
from app.rag.tree_assembler import (
    MAX_NODES,
    BlockRef,
    assemble_catalog_tree,
    attach_block_tree,
)


# ---------------------------------------------------------------------------
# 构造辅助（合成数据）
# ---------------------------------------------------------------------------

_SPACE = "7708408008821771292"


def _doc(nid: str, parent: str | None = None, kind: str = "docx", title: str | None = None) -> IrNode:
    return IrNode(
        id=f"fs:node:{nid}",
        parent_id=parent,
        type="document",
        title=title or f"文档 {nid}",
        doc_kind=kind,
        meta={"raw_type": kind, "obj_edit_time": "2026-11-21 10:19:06"},
    )


def _shortcut_doc(nid: str, parent: str | None = None) -> IrNode:
    return IrNode(
        id=f"fs:node:{nid}",
        parent_id=parent,
        type="document",
        title=f"快捷方式 {nid}",
        doc_kind="docx",
        meta={"raw_type": "shortcut", "node_type": "shortcut"},
    )


def _blk(bid: str, parent: str, type_: str = "paragraph", **kw: Any) -> IrNode:
    payload: dict[str, Any] = {
        "id": f"fs:blk:{bid}",
        "parent_id": parent,
        "type": type_,
        "meta": {"raw_type": 2},
        "text": f"块 {bid}",
    }
    payload.update(kw)
    return IrNode(**payload)


def _ref(node: IrNode, children_ids: list[str] | None = None) -> BlockRef:
    return BlockRef(node=node, children_ids=children_ids or [])


def _walk(root: IrNode):
    """先序遍历产出全部节点（children 数组序）。"""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


# ---------------------------------------------------------------------------
# 目录层：根判定 / BFS 建完全树 / catalog 物化
# ---------------------------------------------------------------------------


def test_parent_empty_becomes_root_and_full_tree_built() -> None:
    a, b, c, d = _doc("a"), _doc("b", "fs:node:a"), _doc("c", "fs:node:a"), _doc("d", "fs:node:b")
    root, report = assemble_catalog_tree([a, b, c, d])
    assert root.id == VIRTUAL_ROOT_ID
    assert root.children == [a]
    assert a.children == [b, c]
    assert b.children == [d]
    # 装配即一致：真根 parent 重接虚拟根（虚拟单根形态）
    assert a.parent_id == VIRTUAL_ROOT_ID
    assert report.total_input == 4
    assert report.attached == 4
    assert report.root_ids == ["fs:node:a"]
    assert not report.orphan_ids and not report.cycle_ids and not report.truncated


def test_sibling_order_follows_input_order() -> None:
    nodes = [_doc("r"), _doc("c2", "fs:node:r"), _doc("c1", "fs:node:r")]
    root, _ = assemble_catalog_tree(nodes)
    assert [n.id for n in root.children[0].children] == ["fs:node:c2", "fs:node:c1"]


def test_orphans_attached_to_virtual_root_with_trace() -> None:
    a = _doc("a")
    x = _doc("x", "fs:node:missing-parent")
    root, report = assemble_catalog_tree([a, x])
    assert [n.id for n in root.children] == ["fs:node:a", "fs:node:x"]
    assert x.parent_id == VIRTUAL_ROOT_ID
    assert report.orphan_ids == ["fs:node:x"]
    assert report.attached == 2


def test_catalog_ids_and_path_materialized_down_tree() -> None:
    r = _doc("r", title="根文档")
    m1 = _doc("m1", "fs:node:r", title="中间层")
    m2 = _doc("m2", "fs:node:m1", title="叶文档")
    root, _ = assemble_catalog_tree([r, m1, m2])
    assert r.catalog_ids == [] and r.catalog_path == []  # 顶层无祖先
    assert m1.catalog_ids == ["fs:node:r"]
    assert m1.catalog_path == ["根文档"]
    assert m2.catalog_ids == ["fs:node:r", "fs:node:m1"]
    assert m2.catalog_path == ["根文档", "中间层"]


def test_assembly_wires_prev_next_siblings() -> None:
    nodes = [_doc("r"), _doc("c1", "fs:node:r"), _doc("c2", "fs:node:r")]
    root, _ = assemble_catalog_tree(nodes)
    r = root.children[0]
    assert r.children[0].next is r.children[1]
    assert r.children[1].prev is r.children[0]
    assert r.prev is None and r.next is None


# ---------------------------------------------------------------------------
# 防御①：防环（visited 集合，环边切断留痕）
# ---------------------------------------------------------------------------


def test_cycle_cut_entry_traced_and_content_kept() -> None:
    a = _doc("a")
    x, y = _doc("x", "fs:node:y"), _doc("y", "fs:node:x")  # 互指环
    root, report = assemble_catalog_tree([a, x, y])
    # 环入口（输入序首个未达节点）切边成根；另一节点随其入树——内容零丢失
    assert report.cycle_ids == ["fs:node:x"]
    assert y.parent_id == "fs:node:x"
    assert x.parent_id == VIRTUAL_ROOT_ID
    ids = {n.id for n in _walk(root)}
    assert {"fs:node:a", "fs:node:x", "fs:node:y"} <= ids
    assert report.attached == 3


def test_self_loop_cut_traced() -> None:
    z = _doc("z", "fs:node:z")  # 自环
    a = _doc("a")
    root, report = assemble_catalog_tree([a, z])
    assert report.cycle_ids == ["fs:node:z"]
    assert z.parent_id == VIRTUAL_ROOT_ID


# ---------------------------------------------------------------------------
# 防御③：截断安全阀（模块常量 MAX_NODES）
# ---------------------------------------------------------------------------


def test_truncation_safety_valve(monkeypatch: pytest.MonkeyPatch) -> None:
    assert MAX_NODES == 10_000  # 931 实测量级 ×10 余量（§12：生产不设探针式硬上限）
    monkeypatch.setattr("app.rag.tree_assembler.MAX_NODES", 3)
    nodes = [_doc("r"), _doc("c1", "fs:node:r"), _doc("c2", "fs:node:r"), _doc("c3", "fs:node:r"), _doc("c4", "fs:node:r")]
    root, report = assemble_catalog_tree(nodes)
    assert report.truncated is True
    assert report.truncated_dropped == 2
    assert report.attached == 3
    kept_ids = {n.id for n in _walk(root)}
    assert kept_ids == {"virtual:root", "fs:node:r", "fs:node:c1", "fs:node:c2"}


def test_truncation_orphans_dropped_parents_descendants(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.rag.tree_assembler.MAX_NODES", 2)
    # 子节点排在被截父节点之前：leaf 留、其 parent m 被截 → parent 引用缺失 → 孤儿兜底
    nodes = [_doc("r"), _doc("leaf", "fs:node:m"), _doc("m", "fs:node:r")]
    root, report = assemble_catalog_tree(nodes)
    assert report.truncated is True
    assert report.truncated_dropped == 1
    assert report.orphan_ids == ["fs:node:leaf"]
    ids = {n.id for n in _walk(root)}
    assert "fs:node:leaf" in ids
    assert report.attached == 2


# ---------------------------------------------------------------------------
# 防御④：shortcut 计为引用边、不展开实体
# ---------------------------------------------------------------------------


def test_shortcut_node_kept_but_subtree_not_expanded() -> None:
    s = _shortcut_doc("s")
    c1 = _doc("c1", "fs:node:s")
    g = _doc("g", "fs:node:c1")  # 被切子树深层
    plain = _doc("p")
    root, report = assemble_catalog_tree([s, c1, g, plain])
    ids = {n.id for n in _walk(root)}
    assert "fs:node:s" in ids and "fs:node:p" in ids  # shortcut 节点本身保留（引用边）
    assert s.children == []  # 不展开实体
    assert "fs:node:c1" not in ids and "fs:node:g" not in ids
    assert report.shortcut_ids == ["fs:node:s"]
    assert report.shortcut_cut_ids == ["fs:node:c1", "fs:node:g"]  # 传递切断全量留痕
    assert report.attached == 2  # s + plain


# ---------------------------------------------------------------------------
# 防御②：重复 id / 去重留痕
# ---------------------------------------------------------------------------


def test_duplicate_id_first_wins_traced() -> None:
    a1 = _doc("a")
    a2 = _doc("a", "fs:node:missing")  # 同 id 第二出现：丢弃留痕
    root, report = assemble_catalog_tree([a1, a2])
    assert report.duplicate_ids == ["fs:node:a"]
    assert report.attached == 1


# ---------------------------------------------------------------------------
# 内容层：children 数组序 → 块树挂 document 节点
# ---------------------------------------------------------------------------


def test_blocks_attach_by_children_array_order() -> None:
    doc = _doc("d")
    r1 = _blk("r1", "fs:node:d")
    r2 = _blk("r2", "fs:node:d")
    c1 = _blk("c1", "fs:blk:r1")
    c2 = _blk("c2", "fs:blk:r1")
    out, report = attach_block_tree(
        doc,
        [_ref(r1, ["fs:blk:c2", "fs:blk:c1"]), _ref(r2), _ref(c1), _ref(c2)],
        root_order=["fs:blk:r2", "fs:blk:r1"],
    )
    assert out is doc
    assert [n.id for n in doc.children] == ["fs:blk:r2", "fs:blk:r1"]  # root_order 权威
    assert [n.id for n in r1.children] == ["fs:blk:c2", "fs:blk:c1"]  # children 数组序权威
    assert r1.parent_id == "fs:node:d" and c2.parent_id == "fs:blk:r1"  # 构造即一致
    assert r2.next is r1 and r1.prev is r2  # 兄弟指针随装配重建（children 序=[r2, r1]）
    assert report.attached == 4 and not report.orphan_ids


def test_block_children_fallback_input_order_without_children_ids() -> None:
    doc = _doc("d")
    r1 = _blk("r1", "fs:node:d")
    c1 = _blk("c1", "fs:blk:r1")
    c2 = _blk("c2", "fs:blk:r1")
    attach_block_tree(doc, [_ref(r1, ["fs:blk:c2"]), _ref(c1), _ref(c2)])  # c1 未列入数组
    assert [n.id for n in r1.children] == ["fs:blk:c2", "fs:blk:c1"]  # 未列入者按输入序补尾


def test_unlisted_root_blocks_append_after_root_order() -> None:
    doc = _doc("d")
    r1 = _blk("r1", "fs:node:d")
    r2 = _blk("r2", "fs:node:d")
    attach_block_tree(doc, [_ref(r1), _ref(r2)], root_order=["fs:blk:r2"])
    assert [n.id for n in doc.children] == ["fs:blk:r2", "fs:blk:r1"]


def test_orphan_block_reattached_to_doc_root_with_trace() -> None:
    doc = _doc("d")
    r1 = _blk("r1", "fs:node:d")
    x = _blk("x", "fs:blk:missing-parent")  # parent 引用缺失
    y = IrNode(id="fs:blk:y", parent_id=None, type="paragraph", text="无父块", meta={"raw_type": 2})
    _, report = attach_block_tree(doc, [_ref(r1), _ref(x), _ref(y)])
    assert report.orphan_ids == ["fs:blk:x", "fs:blk:y"]
    assert x.parent_id == "fs:node:d" and y.parent_id == "fs:node:d"
    ids = [n.id for n in doc.children]
    assert {"fs:blk:x", "fs:blk:y"} <= set(ids)


def test_block_cycle_cut_and_reattached_with_trace() -> None:
    doc = _doc("d")
    r1 = _blk("r1", "fs:node:d")
    c1 = _blk("c1", "fs:blk:c2")  # 互指环
    c2 = _blk("c2", "fs:blk:c1")
    _, report = attach_block_tree(doc, [_ref(r1), _ref(c1), _ref(c2)])
    assert report.cycle_ids == ["fs:blk:c1"]
    assert c1.parent_id == "fs:node:d"  # 环入口切边 → 挂文档根
    assert c2.parent_id == "fs:blk:c1"  # 随入口入树，内容零丢失
    assert report.attached == 3


def test_shortcut_doc_refuses_content_expansion() -> None:
    doc = _shortcut_doc("d")
    r1 = _blk("r1", "fs:node:d")
    out, report = attach_block_tree(doc, [_ref(r1)])
    assert out is doc
    assert doc.children == []  # shortcut 不展开实体（内容层守卫）
    assert report.attached == 0
    assert report.shortcut_ids == ["fs:node:d"]


def test_block_truncation_valve(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.rag.tree_assembler.MAX_NODES", 2)
    doc = _doc("d")
    r1, r2, r3 = (_blk("r1", "fs:node:d"), _blk("r2", "fs:node:d"), _blk("r3", "fs:node:d"))
    _, report = attach_block_tree(doc, [_ref(r1), _ref(r2), _ref(r3)])
    assert report.truncated is True
    assert report.truncated_dropped == 1
    assert report.attached == 2
    assert [n.id for n in doc.children] == ["fs:blk:r1", "fs:blk:r2"]


def test_content_layer_never_touches_catalog_fields() -> None:
    """catalog_ids/catalog_path 为 document 专属目录语义——内容层装配不触碰块节点。"""
    doc = _doc("d")
    doc.catalog_ids = ["fs:node:anc"]
    doc.catalog_path = ["祖先"]
    b = _blk("b", "fs:node:d")
    attach_block_tree(doc, [_ref(b)])
    assert b.catalog_ids == [] and b.catalog_path == []


# ---------------------------------------------------------------------------
# 931 量级合成样本（调研 §12 实测形态——程序化生成，不含真实数据）
# ---------------------------------------------------------------------------


def _build_probe_shaped_forest() -> tuple[list[IrNode], dict[str, list[BlockRef]]]:
    """合成 931 文档单元目录森林（depth1=4/depth2=38/depth3=343/depth4=425/depth5=121；
    docx=794+file=137 按全序前 794 为 docx）+ 3 篇 docx 的内容块树。

    父节点按轮转（round-robin）确定性选取——合成样本不含真实数据、形态可复现。
    """
    layer_counts = {1: 4, 2: 38, 3: 343, 4: 425, 5: 121}
    flat: list[IrNode] = []
    prev_layer: list[IrNode] = []
    counter = 0
    for depth in (1, 2, 3, 4, 5):
        layer: list[IrNode] = []
        for _ in range(layer_counts[depth]):
            parent = None
            if depth > 1:
                parent = prev_layer[counter % len(prev_layer)].id
            kind = "docx" if counter < 794 else "file"
            node = _doc(f"n{counter:05d}", parent, kind=kind, title=f"合成文档 {counter:05d}")
            node.loc = {
                "origin": "feishu",
                "space_id": _SPACE,
                "node_token": f"tok{counter:05d}",
                "block_id": None,
                "edit_time": 1732166400 + counter,
            }
            layer.append(node)
            flat.append(node)
            counter += 1
        prev_layer = layer

    blocks_by_doc: dict[str, list[BlockRef]] = {}
    docx_docs = [n for n in flat if n.doc_kind == "docx"]
    for doc in (docx_docs[0], docx_docs[100], docx_docs[500]):
        refs: list[BlockRef] = []
        root_block_ids: list[str] = []
        for j in range(30):
            bid = f"{doc.id.split(':')[-1]}-b{j:03d}"
            if j % 10 == 0:
                node = _blk(bid, doc.id, "heading", level=1, title=f"标题 {j}", text=None)
                node.meta = {"raw_type": 3}
            elif j % 10 == 1:
                node = _blk(bid, doc.id, "code", text="def f(): ...")
                node.meta = {"raw_type": 14, "lang": "python"}
            elif j % 10 == 2:
                node = _blk(bid, doc.id, "table", text=None)
                node.meta = {"raw_type": 31, "rows": [["a", "b"], ["1", "2"]], "col_count": 2}
            elif j % 10 == 3:
                node = _blk(bid, doc.id, "image", text="![图](占位符)")
                node.meta = {"raw_type": 15, "minio_key": f"kb/u/d/img/{bid}.png"}
            else:
                node = _blk(bid, doc.id)
            node.loc = {
                "origin": "feishu",
                "space_id": _SPACE,
                "node_token": doc.id.split(":")[-1].replace("n", "tok"),
                "block_id": bid,
            }
            refs.append(_ref(node))
            root_block_ids.append(f"fs:blk:{bid}")
        blocks_by_doc[doc.id] = refs
        doc.meta["children_ids"] = root_block_ids  # 页块 children 数组（worker 透传形态）
    return flat, blocks_by_doc


def test_probe_scale_forest_assembles_correctly() -> None:
    flat, blocks_by_doc = _build_probe_shaped_forest()
    assert len(flat) == 931
    assert sum(1 for n in flat if n.doc_kind == "docx") == 794
    assert sum(1 for n in flat if n.doc_kind == "file") == 137

    root, report = assemble_catalog_tree(flat)
    assert report.total_input == 931
    assert report.attached == 931
    assert len(report.root_ids) == 4
    assert not report.orphan_ids and not report.cycle_ids and not report.shortcut_ids
    assert report.truncated is False

    walked = list(_walk(root))
    assert len(walked) == 932  # 931 文档 + 虚拟根
    depth5_samples = [n for n in walked if len(n.catalog_ids) == 4]
    assert len(depth5_samples) == 121  # 第 5 层节点：祖先链长 = 4（catalog_ids 物化）
    deepest = depth5_samples[0]
    # catalog_ids=祖先 ID 链物化（边真相）；catalog_path=其标题解析派生（§13.7）——沿 parent 链回溯逐级核对
    by_id = {n.id: n for n in flat}
    assert deepest.catalog_ids == _ancestor_ids(by_id, deepest)
    assert deepest.catalog_path == [by_id[i].title for i in _ancestor_ids(by_id, deepest)]


def _ancestor_ids(by_id: dict[str, IrNode], node: IrNode) -> list[str]:
    ids: list[str] = []
    cursor: IrNode | None = node
    while cursor is not None and cursor.parent_id not in (None, VIRTUAL_ROOT_ID):
        cursor = by_id[cursor.parent_id]
        ids.append(cursor.id)
    ids.reverse()
    return ids


def test_probe_scale_sibling_consistency_and_roundtrip() -> None:
    flat, blocks_by_doc = _build_probe_shaped_forest()
    root, _ = assemble_catalog_tree(flat)

    # 挂内容层（3 篇 docx）
    for doc_id, refs in blocks_by_doc.items():
        target = next(n for n in _walk(root) if n.id == doc_id)
        root_order = target.meta.get("children_ids")
        attach_block_tree(target, refs, root_order=root_order)
        # 混合 children 契约形态（§13.7）：目录子文档（fs:node，目录父语义）在前，
        # 内容根块（fs:blk，内容父语义）追加在后——两者 parent_id 均指本文档
        blk_children = [n for n in target.children if n.id.startswith("fs:blk:")]
        assert len(blk_children) == 30
        assert [n.id for n in blk_children] == root_order

    # 全树兄弟一致性（prev/next 双向 + parent_id 双写一致）
    for node in _walk(root):
        kids = node.children
        for i, child in enumerate(kids):
            assert child.parent_id == node.id
            assert child.prev is (kids[i - 1] if i > 0 else None)
            assert child.next is (kids[i + 1] if i + 1 < len(kids) else None)

    # roundtrip：序列化无兄弟指针 → 反序列化结构相等 → 重建后兄弟指针一致
    payload = serialize_ir(root)
    assert '"prev"' not in payload and '"next"' not in payload
    back = deserialize_ir(payload)
    assert back.model_dump() == root.model_dump()
    rebuild_siblings(back)
    for a, b in zip(_walk(root), _walk(back)):
        assert a.id == b.id
        assert a.model_dump() == b.model_dump()
        kids_a, kids_b = a.children, b.children
        for i in range(len(kids_a)):
            assert kids_b[i].prev is (kids_b[i - 1] if i > 0 else None)
            assert kids_b[i].next is (kids_b[i + 1] if i + 1 < len(kids_b) else None)
