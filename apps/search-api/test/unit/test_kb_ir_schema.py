"""unit 层：统一树 IR schema（M3 批次 1 / B16——data-model.md §1.18 v1.19、调研
feishu-kb-import-research-20261122.md §13.3~13.9、ARCHITECTURE ADR-018 决策①②；
TDD 红绿，纯函数零网络零 DB，合成数据）。

钉住的语义（冻结契约，逐条对应测试）：
- **ID 前缀体系**（data-model :541——**2026-11-23 用户裁决四前缀定案·契约胜出**，
  冲突优先级 contract > ARCHITECTURE）：`fs:node:` / `fs:blk:` / `loc:doc:` /
  `loc:blk:` 四前缀 + 非空尾段（URL 文档/块亦用 loc:doc:/loc:blk:——来源语义由
  loc.origin 三源 union 与 document 节点 doc_kind 承载、ID 不重复编码来源）；
  非法前缀一律拒绝（含收窄掉的 `url:doc:`）；`virtual:root` 为
  Forest 虚拟单根保留哨兵（装配器内部，ir_schema 与 tree_assembler 共认）；
- **双层 type**（§13.4/§13.8#4）：统一枚举九值；未知类型兜底 paragraph 且原始类型
  进 `meta.raw_type`（全量留痕——不只未知类型）；`meta.raw_type` 必带；
- **loc 三源 discriminated union**（§13.5）：feishu{space_id,node_token,block_id,
  edit_time} / local{page,char_range,file_object_key} / url{source_url,char_range}，
  以 `origin` 判别；
- **类型专属字段**：heading `level` 1~9；text 载荷节点 `text`；
- **序列化无 prev/next 兄弟指针**（data-model :544 钉注 / 调研 §13.8#2）：兄弟关系
  仅内存形态，`serialize_ir` 产物零 `"prev"`/`"next"` 键；**装载须由 children 数组序
  `rebuild_siblings` 重建 prev/next**（下钻链路 storage.get_ir_json 已内置）；
- **document-ref 占位符契约**（§1.18 / §13.8#3）：`<document-ref id=... kind=...>标签
  </document-ref>` 形态 + PUA U+F0000 私有区单字符替换保护（防切分切碎）+ 完整性
  校验（残留片段拒绝）+ 还原（切碎拼接后还原仍逐字节一致）。
"""

from typing import Any

import pytest
from pydantic import ValidationError

from app.rag.ir_schema import (
    PUA_BASE,
    VIRTUAL_ROOT_ID,
    DocumentRef,
    IrNode,
    IrNodeType,
    deserialize_ir,
    find_document_refs,
    make_document_ref,
    protect_document_refs,
    rebuild_siblings,
    restore_document_refs,
    serialize_ir,
)


# ---------------------------------------------------------------------------
# 构造辅助（合成数据，无真实 token）
# ---------------------------------------------------------------------------


def _meta(raw: Any) -> dict[str, Any]:
    return {"raw_type": raw}


def _doc_node(**kw: Any) -> IrNode:
    base: dict[str, Any] = {
        "id": "fs:node:WTp2dAqBposqlFxMmmhcnLsCnmf",
        "type": "document",
        "title": "旅行记录",
        "doc_kind": "docx",
        "meta": _meta("docx"),
    }
    base.update(kw)
    return IrNode(**base)


def _block_node(**kw: Any) -> IrNode:
    base: dict[str, Any] = {
        "id": "fs:blk:b0001",
        "parent_id": "fs:node:WTp2dAqBposqlFxMmmhcnLsCnmf",
        "type": "paragraph",
        "text": "到达：10月2日 停留时间：2 天",
        "loc": {"origin": "feishu", "space_id": "7708408008821771292", "node_token": "WTp2", "block_id": "b0001"},
        "meta": _meta(2),
    }
    base.update(kw)
    return IrNode(**base)


def _sample_tree() -> IrNode:
    """覆盖全部九类 type + 三源 loc + 类型专属字段的合成文档子树。"""
    doc = _doc_node(catalog_ids=["fs:node:anc0"], catalog_path=["大模型笔记"])
    doc.children = [
        _block_node(id="fs:blk:h1", type="heading", level=1, title="行程安排", meta=_meta(3), text=None),
        _block_node(id="fs:blk:p1"),
        _block_node(
            id="fs:blk:code1",
            type="code",
            text="def hello(): ...",
            meta={**_meta(14), "lang": "python"},
            loc={"origin": "feishu", "space_id": "7708", "node_token": "WTp2", "block_id": "code1"},
        ),
        _block_node(
            id="fs:blk:tbl1",
            type="table",
            meta={**_meta(31), "rows": [["模型", "参数量"], ["A", "7B"]], "col_count": 2},
        ),
        _block_node(
            id="fs:blk:img1",
            type="image",
            text="![行李清单](占位符)",
            meta={**_meta(15), "minio_key": "kb/u1/d1/img/b12.png"},
        ),
        _block_node(
            id="fs:blk:q1",
            type="quote",
            text="引用一段",
            loc={"origin": "local", "page": 12, "char_range": [3, 9], "file_object_key": "kb/u1/d1/original.pdf"},
            meta=_meta(10),
        ),
        _block_node(
            id="fs:blk:l1",
            type="list",
            text="必玩打卡：中央大街",
            loc={"origin": "url", "source_url": "https://example.com/a", "char_range": [0, 20]},
            meta=_meta(12),
        ),
        _block_node(id="fs:blk:att1", type="file_attachment", title="某资料.pdf", meta={**_meta("file"), "parse_status": "pending"}, children=[]),
    ]
    return doc


# ---------------------------------------------------------------------------
# ID 前缀体系（§13.9④）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["fs:node:", "fs:blk:", "loc:doc:", "loc:blk:"])
def test_valid_id_prefixes_accepted(prefix: str) -> None:
    node = IrNode(id=f"{prefix}abc123", type="paragraph", meta=_meta(2))
    assert node.id == f"{prefix}abc123"


@pytest.mark.parametrize(
    "bad_id",
    ["doc:abc", "fs:blk:", "url:doc:abc", "url:docx:abc", "plain-id", "", "FS:node:abc"],
)
def test_invalid_id_prefix_rejected(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        IrNode(id=bad_id, type="paragraph", meta=_meta(2))


def test_virtual_root_sentinel_accepted() -> None:
    """Forest 虚拟单根保留哨兵：仅 `virtual:root` 一个值豁免前缀校验（装配器内部）。"""
    node = IrNode(id=VIRTUAL_ROOT_ID, type="document", title="虚拟根", meta=_meta("virtual_root"))
    assert node.id == VIRTUAL_ROOT_ID


def test_virtual_root_sentinel_is_reserved_value_not_prefix() -> None:
    with pytest.raises(ValidationError):
        IrNode(id="virtual:other", type="paragraph", meta=_meta(2))


# ---------------------------------------------------------------------------
# 双层 type（§13.4/§13.8#4）
# ---------------------------------------------------------------------------


def test_type_enum_nine_values() -> None:
    assert {t.value for t in IrNodeType} == {
        "document",
        "heading",
        "paragraph",
        "list",
        "quote",
        "code",
        "table",
        "image",
        "file_attachment",
    }


def test_unknown_type_falls_back_to_paragraph_with_raw_type_trace() -> None:
    node = IrNode(id="fs:blk:u1", type="mindnote", meta={})
    assert node.type is IrNodeType.PARAGRAPH
    assert node.meta["raw_type"] == "mindnote"  # 原始类型留痕，零内容丢失


def test_unknown_type_does_not_clobber_existing_raw_type() -> None:
    node = IrNode(id="fs:blk:u2", type="widget", meta=_meta(99))
    assert node.type is IrNodeType.PARAGRAPH
    assert node.meta["raw_type"] == 99


def test_raw_type_required_on_every_node() -> None:
    """`meta.raw_type` 全量必带（双层类型契约——原始类型永远可查）。"""
    with pytest.raises(ValidationError):
        IrNode(id="fs:blk:r1", type="paragraph", meta={})


def test_doc_kind_literal_set() -> None:
    for kind in ("docx", "file", "sheet", "bitable", "upload", "url"):
        assert _doc_node(doc_kind=kind).doc_kind == kind
    with pytest.raises(ValidationError):
        _doc_node(doc_kind="mindnote")


# ---------------------------------------------------------------------------
# loc 三源 discriminated union（§13.5）
# ---------------------------------------------------------------------------


def test_feishu_loc_discriminated() -> None:
    node = _block_node()
    assert node.loc is not None
    assert node.loc.origin == "feishu"
    assert node.loc.space_id == "7708408008821771292"
    assert node.loc.node_token == "WTp2"


def test_local_loc_with_page_and_char_range() -> None:
    node = IrNode(
        id="loc:blk:lp1",
        parent_id="loc:doc:ld1",
        type="paragraph",
        text="本地段落",
        loc={"origin": "local", "page": 3, "char_range": [10, 40], "file_object_key": "kb/u1/d1/original.pdf"},
        meta=_meta("p"),
    )
    assert node.loc is not None
    assert node.loc.page == 3
    assert node.loc.char_range == (10, 40)


def test_url_loc_with_source_url() -> None:
    """URL 来源文档 id=loc:doc: 形态（2026-11-23 四前缀裁决——来源语义由
    loc.origin=url 承载，ID 不重复编码来源；url:doc: 前缀已收窄废除）。"""
    node = IrNode(
        id="loc:doc:ud1",
        type="paragraph",
        text="网页正文",
        loc={"origin": "url", "source_url": "https://example.com/a", "char_range": [0, 5]},
        meta=_meta("html"),
    )
    assert node.id == "loc:doc:ud1"  # URL 来源文档 id=loc:doc: 形态断言（四前缀定案）
    assert node.loc is not None
    assert node.loc.origin == "url"
    assert str(node.loc.source_url).startswith("https://")


def test_loc_missing_required_field_rejected() -> None:
    with pytest.raises(ValidationError):
        _block_node(loc={"origin": "feishu", "space_id": "7708"})  # 缺 node_token
    with pytest.raises(ValidationError):
        _block_node(loc={"origin": "url"})  # 缺 source_url


def test_loc_char_range_must_be_pair() -> None:
    with pytest.raises(ValidationError):
        _block_node(
            loc={"origin": "local", "char_range": [1, 2, 3], "file_object_key": "kb/u/d/original.pdf"},
        )


# ---------------------------------------------------------------------------
# 类型专属字段
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level", [1, 5, 9])
def test_heading_level_in_range(level: int) -> None:
    node = _block_node(id="fs:blk:h9", type="heading", level=level, title="H", meta=_meta(3), text=None)
    assert node.level == level


@pytest.mark.parametrize("level", [0, 10, -1])
def test_heading_level_out_of_range_rejected(level: int) -> None:
    with pytest.raises(ValidationError):
        _block_node(id="fs:blk:h0", type="heading", level=level, title="H", meta=_meta(3), text=None)


def test_file_attachment_placeholder_shape() -> None:
    """file 附件未解析占位形态（§13.9⑥）：children=[] + meta.parse_status。"""
    node = _sample_tree().children[-1]
    assert node.type is IrNodeType.FILE_ATTACHMENT
    assert node.children == []
    assert node.meta["parse_status"] == "pending"


# ---------------------------------------------------------------------------
# 序列化 / 反序列化与 prev-next 重建（data-model :544 钉注）
# ---------------------------------------------------------------------------


def test_serialize_drops_prev_and_next_keys() -> None:
    tree = _sample_tree()
    rebuild_siblings(tree)
    payload = serialize_ir(tree)
    assert '"prev"' not in payload
    assert '"next"' not in payload


def test_roundtrip_structure_equal() -> None:
    tree = _sample_tree()
    rebuild_siblings(tree)
    back = deserialize_ir(serialize_ir(tree).encode("utf-8"))
    assert back.model_dump() == tree.model_dump()


def test_deserialize_does_not_resurrect_siblings() -> None:
    back = deserialize_ir(serialize_ir(_sample_tree()))
    first = back.children[0]
    assert first.next is None  # 装载态无兄弟指针——须 rebuild_siblings


def test_rebuild_siblings_wires_children_order_bidirectionally() -> None:
    back = deserialize_ir(serialize_ir(_sample_tree()))
    rebuild_siblings(back)
    for parent in (back, *back.children):
        kids = parent.children
        for i, child in enumerate(kids):
            expected_prev = kids[i - 1] if i > 0 else None
            expected_next = kids[i + 1] if i + 1 < len(kids) else None
            assert child.prev is expected_prev
            assert child.next is expected_next
    # 深层节点同样成立（code/table/image 挂在 heading 之下——sample 为扁平结构，此处锁根层即可）
    assert back.children[0].prev is None
    assert back.children[-1].next is None


def test_rebuild_siblings_deep_nesting() -> None:
    tree = _doc_node()
    h = _block_node(id="fs:blk:h", type="heading", level=1, title="H", meta=_meta(3), text=None)
    a = _block_node(id="fs:blk:a")
    b = _block_node(id="fs:blk:b")
    h.children = [a, b]
    tree.children = [h]
    rebuild_siblings(tree)
    assert a.next is b
    assert b.prev is a
    assert a.prev is None and b.next is None
    assert h.prev is None and h.next is None


def test_rebuild_siblings_returns_root_and_keeps_root_pointers() -> None:
    """重建范围 = children 各层；root 自身指针不在范围（由父层装配/装载负责）——
    装配器组合场景（attach_block_tree）不抹掉目录层已接好的同级指针。"""
    tree = _doc_node()
    tree.children = [_block_node(id="fs:blk:x")]
    dirty = _block_node(id="fs:blk:dirty")
    tree.prev = dirty  # 模拟父层已接好的既有同级指针（非自环示例；模型无禁自环校验——如需拒绝 prev 指向自身须另行加 model_validator，validate_assignment 仅做字段类型校验）
    out = rebuild_siblings(tree)
    assert out is tree
    assert tree.prev is dirty  # 不被触碰
    assert tree.children[0].prev is None and tree.children[0].next is None


# ---------------------------------------------------------------------------
# document-ref 占位符工具（§1.18 / §13.8#3）
# ---------------------------------------------------------------------------


def test_make_document_ref_canonical_form() -> None:
    ref = make_document_ref("fs:node:abc", "document", "参见 文档甲")
    assert ref == '<document-ref id="fs:node:abc" kind="document">参见 文档甲</document-ref>'


def test_find_document_refs_extracts_triple() -> None:
    ref = make_document_ref("fs:blk:b9", "code", "工具函数")
    found = find_document_refs(f"前文 {ref} 后文")
    assert found == [DocumentRef(node_id="fs:blk:b9", kind="code", label="工具函数")]


def test_find_document_refs_ignores_unclosed_fragment() -> None:
    assert find_document_refs('<document-ref id="a" kind="b">未闭合') == []


def test_protect_replaces_each_ref_with_single_pua_char() -> None:
    r1 = make_document_ref("fs:node:a", "document", "甲")
    r2 = make_document_ref("fs:blk:b", "code", "乙")
    text = f"A{r1}B{r2}C"
    protected, mapping = protect_document_refs(text)
    pua_chars = [c for c in protected if ord(c) >= PUA_BASE]
    assert len(pua_chars) == 2
    assert len(protected) == len("ABC") + 2  # 每个占位符 → 恰 1 个 PUA 字符
    assert set(mapping.values()) == {r1, r2}
    assert set(mapping.keys()) == set(pua_chars)


def test_restore_after_split_is_byte_identical() -> None:
    """切碎模拟：保护后按任意粒度切开再拼接 → 还原后与原文逐字节一致（A36 协议面）。"""
    text = f"开头 {make_document_ref('fs:node:n1', 'document', '标签一')} 中间 {make_document_ref('fs:blk:b1', 'paragraph', '标签二')} 结尾"
    protected, mapping = protect_document_refs(text)
    pieces = [protected[i : i + 3] for i in range(0, len(protected), 3)]
    assert restore_document_refs("".join(pieces), mapping) == text


def test_protect_rejects_unclosed_placeholder() -> None:
    with pytest.raises(ValueError, match="document-ref"):
        protect_document_refs('正文 <document-ref id="a" kind="b">未闭合收尾')


def test_protect_avoids_collision_with_preexisting_pua_chars() -> None:
    pre = chr(0xF0000)
    text = f"已有{pre}字符 {make_document_ref('fs:node:c1', 'document', '标签')}"
    protected, mapping = protect_document_refs(text)
    new_chars = [c for c in protected if ord(c) >= PUA_BASE and c != pre]
    assert len(new_chars) == 1
    assert ord(new_chars[0]) > 0xF0000  # 偏移上移避碰撞——既有 PUA 字符不被误还原
    assert ord(mapping[new_chars[0]][0]) == ord("<")  # mapping 值为原占位符文本
    assert restore_document_refs(protected, mapping) == text
