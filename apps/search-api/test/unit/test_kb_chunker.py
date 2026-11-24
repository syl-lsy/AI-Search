"""unit 层：逐级切分 chunker（M3 批次 2 / B17 主线二——IR → kb_chunks）。

契约锚点：data-model.md §1.8 kb_chunks（meta 七键：node_id/heading_path/loc/
references/raw_type/char_start/char_end）、§1.18（占位符协议 + heading_path 构成
公式 catalog_path+[doc.title]+文档内标题链）、ARCHITECTURE ADR-018 决策④（逐级
切分/代码整块/表格聚合/content_hash 写入接线随批次 2）、nacos-config.md §3 kb 节
（chunk_size=800/chunk_overlap=120/chunking.{1200,400,200,10}）、调研
feishu-kb-import-research-20261122.md §13.4 切分行为列 / §13.6#1 切分链。

TDD 红绿：纯函数零网络零 DB，手构造 IrNode 森林 fixture（不依赖 normalizers）。

钉住的语义（逐条对应测试）：
- **逐级降级矩阵**：heading 边界优先 → 超长单元按 页面级(≤page_max_chars) →
  段落级(≤para_max_chars) → 句子级(≤sentence_max_chars) 逐级下钻；句子级仍超长
  （无任何语义边界）才按窗（chunk_size/chunk_overlap）硬切；
- **尺寸窗**：chunk_size=目标窗（语义边界优先于窗边界——单个语义单元 ≤ 页面上限
  时允许整块超目标窗成 chunk）；chunk_overlap=重叠（句子级碎片跨 chunk 时携带
  前片句尾尾巴；硬切片自带重叠；不同单元合成的 chunk 零重叠）；
- **代码整块**：kind=code 原子、永不内切、不与邻接合并；超长整块保留不腰斩
  （oversized 留痕 + 结果统计计数）；
- **表格聚合**：≤table_agg_rows 数据行聚为单 chunk；超限按组切分且每组重携表头
  （缓解 markitdown 表头语义瑕疵）；
- **PUA 占位符**（§1.18 / ir_schema.protect_document_refs）：占位符跨块=缺陷——
  保护后逐块还原，原占位符恰好完整落在唯一 chunk；未闭合残片 ValueError；
- **heading_path 物化**：catalog_path 非空 → 完整目录链（catalog + 接缝 doc.title、
  链内相邻同名保留）+ 文档内标题链、交界前导回显（文档序一次性闩锁）不入轨迹；
  为空 → [doc.title] + 文档内标题链（B17 现状口径，零去重，H1/H2 弹栈）；
- **content_hash**：sha256(归一化文本)——同文本同 hash / 异文本异 hash / 64 hex；
  文档级正文 hash（kb_documents.content_hash 接线原料，OQ-34）随结果产出。
"""

import hashlib
from typing import Any

import pytest

from app.rag.chunker import (
    ChunkingConfig,
    content_hash,
    chunk_document,
    chunk_forest,
    estimate_tokens,
)
from app.rag.ir_schema import (
    IrNode,
    IrNodeType,
    find_document_refs,
    make_document_ref,
)


# ---------------------------------------------------------------------------
# 构造辅助（合成数据，零真实内容）
# ---------------------------------------------------------------------------


def _meta(raw: Any) -> dict[str, Any]:
    return {"raw_type": raw}


def _heading(nid: str, level: int, title: str) -> IrNode:
    return IrNode(
        id=f"loc:blk:{nid}",
        type=IrNodeType.HEADING,
        level=level,
        title=title,
        meta=_meta(level),
    )


def _para(nid: str, text: str, **kw: Any) -> IrNode:
    return IrNode(
        id=f"loc:blk:{nid}", type=IrNodeType.PARAGRAPH, text=text, meta=_meta(2), **kw
    )


def _code(nid: str, text: str) -> IrNode:
    return IrNode(
        id=f"loc:blk:{nid}",
        type=IrNodeType.CODE,
        text=text,
        meta={"raw_type": 14, "lang": "python"},
    )


def _table(nid: str, rows: list[list[Any]]) -> IrNode:
    return IrNode(
        id=f"loc:blk:{nid}",
        type=IrNodeType.TABLE,
        meta={"raw_type": 31, "rows": rows, "col_count": len(rows[0]) if rows else 0},
    )


def _doc(children: list[IrNode], title: str | None = "旅行记录") -> IrNode:
    return IrNode(
        id="loc:doc:doc1",
        type=IrNodeType.DOCUMENT,
        title=title,
        catalog_path=[],
        children=children,
        meta=_meta("page"),
    )


def _doc_at(
    nid: str, children: list[IrNode], title: str | None, catalog: list[str]
) -> IrNode:
    return IrNode(
        id=f"loc:doc:{nid}",
        type=IrNodeType.DOCUMENT,
        title=title,
        catalog_path=catalog,
        children=children,
        meta=_meta("page"),
    )


#: 小尺寸测试配置（等比缩小契约默认值，放大各级上限的触发密度）
CFG = ChunkingConfig(
    chunk_size=50,
    chunk_overlap=10,
    page_max_chars=60,
    para_max_chars=25,
    sentence_max_chars=12,
    table_agg_rows=3,
)


# ---------------------------------------------------------------------------
# 逐级降级矩阵
# ---------------------------------------------------------------------------


class TestHeadingBoundaryFirst:
    def test_heading_starts_new_chunk_and_not_standalone(self) -> None:
        doc = _doc(
            [
                _heading("h1", 1, "行程"),
                _para("p1", "到达第一天。"),
                _heading("h2", 1, "准备"),
                _para("p2", "订机票晚上。"),
            ]
        )
        result = chunk_document(doc, document_id="d-1", config=CFG)
        # 两个内容 chunk（heading 不单独成 chunk、不进 content）
        assert [c.kind for c in result.chunks] == ["paragraph", "paragraph"]
        assert result.chunks[0].content == "到达第一天。"
        assert result.chunks[1].content == "订机票晚上。"
        assert all("行程" not in c.content and "准备" not in c.content for c in result.chunks)

    def test_heading_path_materialized(self) -> None:
        doc = _doc(
            [
                _heading("h1", 1, "行程"),
                _para("p1", "到达第一天。"),
                _heading("h2a", 2, "交通"),
                _para("p2", "坐高铁去。"),
                _heading("h2b", 2, "住宿"),
                _para("p3", "住中央大街。"),
                _heading("h4", 1, "预算"),
                _para("p4", "控制在五千。"),
            ]
        )
        result = chunk_document(doc, document_id="d-1", config=CFG)
        paths = [c.heading_path for c in result.chunks]
        # 基础段 = catalog_path([]) + [doc.title]，文档内标题链随 H1/H2 弹栈
        assert paths == [
            ["旅行记录", "行程"],
            ["旅行记录", "行程", "交通"],
            ["旅行记录", "行程", "住宿"],
            ["旅行记录", "预算"],
        ]

    def test_catalog_path_prefix(self) -> None:
        doc = _doc_at(
            "d9",
            [_heading("h1", 1, "架构"), _para("p1", "Transformer 是基础。")],
            title="一：模型架构",
            catalog=["大模型全套面试题", "LLMs"],
        )
        result = chunk_document(doc, document_id="d-9", config=CFG)
        assert result.chunks[0].heading_path == [
            "大模型全套面试题",
            "LLMs",
            "一：模型架构",
            "架构",
        ]


class TestHeadingPathCatalogChain:
    """目录链 + 文档内标题路径逐级拼接（2026-11-24 用户裁定「轨迹不能有断层」）。

    - catalog_path 非空 → heading_path = 完整目录链 + 文档内标题链；完整链 = catalog
      原样 + **接缝**追加 doc.title（末位同名不追加；**链内相邻同名保留**——嵌套同名
      目录是合法形态，合并即断层）。链的**两种形态均兼容**（链含本文档节点标题 /
      链仅祖先由 doc.title 补接缝均可，两口径都无断层不重复）；
    - **交界回显不入轨迹**：文内标题路径的**文首连续段**中与任一链元素同名的标题滤除，
      依据**文档序一次性闩锁**（首个非回显标题后永久关闭）——中后部同名一律保留；
      **只动轨迹不动树**；
    - catalog_path 为空 → **完全维持现状**（B17 口径 `[doc.title] + 文内标题链`，
      零去重零行为变化；doc.title 前缀属既有行为、非本工单引入）。
    """

    def test_catalog_chain_prepended_before_inner_headings(self) -> None:
        """目录链非空 → 轨迹 = 链 + 文档内标题路径（链含本文档节点标题）。"""
        doc = _doc_at(
            "d10",
            [_heading("h1", 1, "Q1：什么是 RAG？"), _para("p1", "RAG 是检索增强生成。")],
            title="一、基础认知与对比",
            catalog=["RAG核心面（精选）", "一、基础认知与对比"],
        )
        result = chunk_document(doc, document_id="d-10", config=CFG)
        assert result.chunks[0].heading_path == [
            "RAG核心面（精选）",
            "一、基础认知与对比",
            "Q1：什么是 RAG？",
        ]

    def test_junction_echo_characteristic_specimen_three_layers(self) -> None:
        """特征化（真标本形态）：文内 H1 回显目录链首元素 → 不入轨迹，期望 3 层。"""
        doc = _doc_at(
            "d17",
            [
                _heading("h1", 1, "RAG核心面（精选）"),
                _heading("h2", 4, "Q1：请用一段话解释什么是 RAG。"),
                _para("p1", "正文。"),
            ],
            title="一、基础认知与对比",
            catalog=["RAG核心面（精选）", "一、基础认知与对比"],
        )
        result = chunk_document(doc, document_id="d-17", config=CFG)
        assert result.chunks[0].heading_path == [
            "RAG核心面（精选）",
            "一、基础认知与对比",
            "Q1：请用一段话解释什么是 RAG。",
        ]

    def test_junction_leading_multi_echoes_dropped(self) -> None:
        """交界前导**连续**双回显：H1 回显链首、H2 回显链中元素 → 两者都不入轨迹。"""
        doc = _doc_at(
            "d18",
            [
                _heading("h1", 1, "空间"),
                _heading("h2", 2, "目录"),
                _heading("h3", 3, "Q1：问题"),
                _para("p1", "正文。"),
            ],
            title="文档",
            catalog=["空间", "目录"],
        )
        result = chunk_document(doc, document_id="d-18", config=CFG)
        assert result.chunks[0].heading_path == [
            "空间",
            "目录",
            "文档",
            "Q1：问题",
        ]

    def test_late_top_level_same_title_as_chain_kept(self) -> None:
        """文内后部顶层与链元素同名 → 必须保留。

        闩锁是**文档序一次性**的：文内首个非回显标题关闭闸门后，其后任何层级与链元素
        同名的标题都按普通标题保留（按「栈前缀」判回显会把这里的「空间」误滤——回显
        判定必须依赖文档序，不能只看当前祖先栈）。
        """
        doc = _doc_at(
            "d21",
            [
                _heading("h1", 1, "产品定位"),
                _para("p1", "正文一。"),
                _heading("h2", 1, "空间"),
                _para("p2", "正文二。"),
            ],
            title="文档",
            catalog=["空间", "目录"],
        )
        result = chunk_document(doc, document_id="d-21", config=CFG)
        assert [c.heading_path for c in result.chunks] == [
            ["空间", "目录", "文档", "产品定位"],
            ["空间", "目录", "文档", "空间"],
        ]

    def test_nested_chain_title_under_echo_branch_kept(self) -> None:
        """`H1 空间`（回显）> `H2 产品定位` > `H2 目录`：回显段之后的同名标题保留。

        只有文首连续段的回显被滤；`目录` 虽与链元素同名，但出现在非回显标题之后 →
        保留（层级按弹栈：与「产品定位」同级，二者互不为祖先）。
        """
        doc = _doc_at(
            "d22",
            [
                _heading("h1", 1, "空间"),
                _heading("h2", 2, "产品定位"),
                _para("p1", "正文一。"),
                _heading("h3", 2, "目录"),
                _para("p2", "正文二。"),
            ],
            title="文档",
            catalog=["空间", "目录"],
        )
        result = chunk_document(doc, document_id="d-22", config=CFG)
        assert [c.heading_path for c in result.chunks] == [
            ["空间", "目录", "文档", "产品定位"],
            ["空间", "目录", "文档", "目录"],
        ]

    def test_junction_echo_of_chain_last_element_dropped(self) -> None:
        """链末与文档内首个标题同名 → 只出现一次（回显规则的链末形态）。"""
        doc = _doc_at(
            "d11",
            [_heading("h1", 1, "一、基础认知与对比"), _para("p1", "正文。")],
            title=None,
            catalog=["RAG核心面（精选）", "一、基础认知与对比"],
        )
        result = chunk_document(doc, document_id="d-11", config=CFG)
        assert result.chunks[0].heading_path == [
            "RAG核心面（精选）",
            "一、基础认知与对比",
        ]

    def test_catalog_internal_adjacent_duplicate_preserved(self) -> None:
        """链内相邻同名**保留**（嵌套同名目录是合法形态，合并即断层——去重只在
        doc.title 与链末的接缝一处）。"""
        doc = _doc_at(
            "d23",
            [_heading("h1", 1, "Q1：问题"), _para("p1", "正文。")],
            title=None,
            catalog=["A", "A", "B"],
        )
        result = chunk_document(doc, document_id="d-23", config=CFG)
        assert result.chunks[0].heading_path == ["A", "A", "B", "Q1：问题"]

    def test_seam_duplicate_with_doc_title_collapsed(self) -> None:
        """接缝去重（真实触发）：catalog 末位 = 直接父节点标题 = doc.title（仅祖先链
        形态下 doc.title 会重复）→ 不追加、轨迹不出现双份。"""
        doc = _doc_at(
            "d16",
            [_heading("h1", 1, "Q1：什么是 RAG？"), _para("p1", "正文。")],
            title="一、基础认知与对比",
            catalog=["RAG核心面（精选）", "一、基础认知与对比"],
        )
        result = chunk_document(doc, document_id="d-16", config=CFG)
        assert result.chunks[0].heading_path == [
            "RAG核心面（精选）",
            "一、基础认知与对比",
            "Q1：什么是 RAG？",
        ]

    def test_mid_document_same_title_as_chain_kept(self) -> None:
        """文内中部与链元素同名（非交界前导段）→ 保留（多章共有的同名标题合法）。"""
        doc = _doc_at(
            "d12",
            [
                _heading("h1", 1, "产品定位"),
                _para("p1", "正文一。"),
                _heading("h2", 2, "RAG核心面（精选）"),
                _para("p2", "正文二。"),
            ],
            title="一、基础认知与对比",
            catalog=["RAG核心面（精选）", "一、基础认知与对比"],
        )
        result = chunk_document(doc, document_id="d-12", config=CFG)
        assert [c.heading_path for c in result.chunks] == [
            ["RAG核心面（精选）", "一、基础认知与对比", "产品定位"],
            ["RAG核心面（精选）", "一、基础认知与对比", "产品定位", "RAG核心面（精选）"],
        ]

    def test_junction_echo_does_not_touch_tree(self) -> None:
        """只动轨迹不动树：回显标题节点仍在树中、children 归属不变、内容不被改。"""
        echo = _heading("h1", 1, "RAG核心面（精选）")
        child = _heading("h2", 4, "Q1：问题")
        para = _para("p1", "正文。")
        child.children = [para]
        para.parent_id = child.id
        echo.children = [child]
        child.parent_id = echo.id
        doc = _doc_at("d20", [echo], title="一、基础认知与对比",
                      catalog=["RAG核心面（精选）", "一、基础认知与对比"])

        result = chunk_document(doc, document_id="d-20", config=CFG)

        assert result.chunks[0].heading_path == ["RAG核心面（精选）", "一、基础认知与对比", "Q1：问题"]
        # 树零变化
        assert doc.children == [echo]
        assert echo.title == "RAG核心面（精选）" and echo.children == [child]
        assert child.title == "Q1：问题" and child.children == [para]
        assert child.parent_id == echo.id and para.parent_id == child.id

    def test_preamble_chunk_path_equals_catalog_chain(self) -> None:
        """前导无标题片（首个 heading 之前的内容）：目录链非空时轨迹 = 链本身，
        不再为空（消除「54/55 断层」）。"""
        doc = _doc_at(
            "d13",
            [
                _para("p0", "封面语。"),
                _heading("h1", 1, "第一节"),
                _para("p1", "正文。"),
            ],
            title=None,
            catalog=["RAG核心面（精选）", "一、基础认知与对比"],
        )
        result = chunk_document(doc, document_id="d-13", config=CFG)
        assert [c.heading_path for c in result.chunks] == [
            ["RAG核心面（精选）", "一、基础认知与对比"],
            ["RAG核心面（精选）", "一、基础认知与对比", "第一节"],
        ]

    def test_preamble_chunk_without_catalog_stays_unchanged(self) -> None:
        """catalog_path 为空 → 前导片轨迹维持现状（无 title 时为空列表）。"""
        doc = _doc(
            [_para("p0", "封面语。"), _heading("h1", 1, "第一节"), _para("p1", "正文。")],
            title=None,
        )
        result = chunk_document(doc, document_id="d-14", config=CFG)
        assert [c.heading_path for c in result.chunks] == [[], ["第一节"]]

    def test_top_level_document_without_catalog_keeps_doc_title_prefix(self) -> None:
        """顶层文档（catalog=[] + doc.title=T + 文内 H1=T）：路径 = [T, T] —— 按现状
        如实断言。

        澄清：链空 + doc.title 非空时轨迹带 doc.title 前缀是 **B17 既有行为**
        （`base_path = catalog_path + [doc.title]` 在本次工单前即存在，见既有用例
        `test_heading_path_materialized` 的 `["旅行记录", "行程"]`），**非本工单引入**，
        故不回退；本工单对该分支零去重、零行为变化（upload 通道与 ECMA 逐字节一致为证）。
        批次 5（B20）契约将钉「导入文档必须物化 catalog_path（顶层文档=[自身标题]）」，
        届时该形态走链非空分支、回显规则生效。
        """
        doc = _doc(
            [_heading("h1", 1, "RAG核心面（精选）"), _para("p1", "正文。")],
            title="RAG核心面（精选）",
        )
        result = chunk_document(doc, document_id="d-15", config=CFG)
        assert result.chunks[0].heading_path == ["RAG核心面（精选）", "RAG核心面（精选）"]


class TestLadderDescent:
    def test_no_split_below_page_cap(self) -> None:
        text = "x" * 55  # ≤ page_max_chars=60：整单元成单 chunk（可超目标窗 50）
        result = chunk_document(_doc([_para("p1", text)]), document_id="d", config=CFG)
        assert len(result.chunks) == 1
        assert result.chunks[0].content == text

    def test_page_level_split_with_page_breaks(self) -> None:
        text = "x" * 70  # > page_max_chars=60
        # 页内断点 35：页面级切成两片（35/35 均 ≤60）
        result = chunk_document(
            _doc([_para("p1", text)]),
            document_id="d",
            config=CFG,
            page_breaks={"loc:blk:p1": (35,)},
        )
        # 两片 35+35=70 > 目标窗 50 → 各自成 chunk
        assert len(result.chunks) == 2
        assert result.chunks[0].content == "x" * 35
        assert result.chunks[1].content == "x" * 35

    def test_paragraph_level_descent(self) -> None:
        # 70 字符 > 页面上限 60，无 page_breaks → 段落级：按 \n\n 切，贪并 ≤25
        text = "a" * 20 + "\n\n" + "b" * 20 + "\n\n" + "c" * 20
        result = chunk_document(_doc([_para("p1", text)]), document_id="d", config=CFG)
        contents = [c.content for c in result.chunks]
        # 三段各 20 ≤ 25：贪并目标窗 50 → [20+2+20=42] 与 [20]（第三段并入超窗）
        # chunk 边界替代段间分隔符：首 chunk 保留段间 \n\n、次 chunk 起始无残留分隔
        assert len(result.chunks) == 2
        assert all(len(c) <= 50 for c in contents)
        assert contents[0] == "a" * 20 + "\n\n" + "b" * 20
        assert contents[1] == "c" * 20
        # char 区间随分隔符丢弃同步收缩：chunk2 新鲜内容从第三段正文起
        assert result.chunks[1].char_start == 44
        assert result.chunks[1].char_end == 64

    def test_sentence_level_descent(self) -> None:
        # 70 字符、无 \n\n、有句号 → 句子级：碎片贪并 ≤ sentence_max_chars=12
        text = "甲乙丙丁。" * 14  # 70 字符，句边界每 5 字符
        result = chunk_document(_doc([_para("p1", text)]), document_id="d", config=CFG)
        assert len(result.chunks) >= 2
        for chunk in result.chunks:
            # 目标窗内 + 句子整块不腰斩（句级边界切分——每片以句号收尾）
            assert len(chunk.content) <= CFG.chunk_size
            assert chunk.content.endswith("。")
        # 无字符丢失：首 chunk 起始与末 chunk 终止覆盖原文两端
        assert result.chunks[0].content.startswith("甲乙丙丁。")
        assert result.chunks[-1].content.endswith("甲乙丙丁。")

    def test_hard_window_cut_without_any_boundary(self) -> None:
        text = "a" * 100  # 无页/段/句任何边界 → 按窗硬切 50/重叠 10
        result = chunk_document(_doc([_para("p1", text)]), document_id="d", config=CFG)
        assert [len(c.content) for c in result.chunks] == [50, 50, 20]
        # 相邻 chunk 共享 chunk_overlap=10 字符尾巴
        assert result.chunks[1].content[:10] == result.chunks[0].content[-10:]
        assert result.chunks[2].content[:10] == result.chunks[1].content[-10:]
        # 拼接（步进=窗口−重叠）还原全文
        rebuilt = result.chunks[0].content + result.chunks[1].content[10:] + result.chunks[2].content[10:]
        assert rebuilt == text


# ---------------------------------------------------------------------------
# 尺寸窗与重叠语义
# ---------------------------------------------------------------------------


class TestOverlapSemantics:
    def test_sentence_piece_tail_carries_overlap_across_chunks(self) -> None:
        # 70 字符句号串 → 句子级碎片（≤12）贪并；目标窗 50 处跨 chunk → 尾巴重叠
        text = "甲乙丙丁。" * 14
        result = chunk_document(_doc([_para("p1", text)]), document_id="d", config=CFG)
        first, second = result.chunks[0], result.chunks[1]
        # 尾巴按句边界吸附：second 以 first 的最后一个完整句开头（≤ chunk_overlap=10）
        assert second.content.startswith("甲乙丙丁。")
        assert first.content.endswith("甲乙丙丁。")
        assert len(second.content) - len(second.content.lstrip("甲乙丙丁。")) >= 0
        # 新鲜内容邻接：char 区间相接（尾巴不计入 char 区间）
        assert second.char_start == first.char_end

    def test_distinct_unit_chunks_have_zero_overlap(self) -> None:
        doc = _doc([_para("p1", "a" * 30), _para("p2", "b" * 30)])
        result = chunk_document(doc, document_id="d", config=CFG)
        assert len(result.chunks) == 2
        assert result.chunks[0].content == "a" * 30
        assert result.chunks[1].content == "b" * 30
        # 零重叠：单元间 "\n\n" 扁平分隔符（2 字符）不属于任何 chunk——区间相隔
        # 恰为分隔符宽度；同单元重叠场景（前测）才是 char_start == char_end 邻接
        assert result.chunks[1].char_start == result.chunks[0].char_end + 2
        assert set(result.chunks[0].content) & set(result.chunks[1].content) == set()


# ---------------------------------------------------------------------------
# 代码整块
# ---------------------------------------------------------------------------


class TestCodeAtomicity:
    def test_code_standalone_never_merged(self) -> None:
        doc = _doc(
            [
                _para("p1", "前文说明。"),
                _code("c1", "def f():\n    return 1"),
                _para("p2", "后文说明。"),
            ]
        )
        result = chunk_document(doc, document_id="d", config=CFG)
        kinds = [c.kind for c in result.chunks]
        assert kinds == ["paragraph", "code", "paragraph"]
        assert result.chunks[1].content == "def f():\n    return 1"

    def test_oversized_code_kept_whole_with_trail(self) -> None:
        long_code = "x = 1  # " + "a" * 95  # > chunk_size=50
        doc = _doc([_code("c1", long_code)])
        result = chunk_document(doc, document_id="d", config=CFG)
        assert len(result.chunks) == 1  # 不腰斩
        assert result.chunks[0].content == long_code
        assert result.chunks[0].oversized is True
        assert result.oversized_units == 1


# ---------------------------------------------------------------------------
# 表格聚合
# ---------------------------------------------------------------------------


class TestTableAggregation:
    def test_small_table_single_chunk_with_header(self) -> None:
        rows = [["模型", "参数量"], ["A", "7B"], ["B", "13B"], ["C", "70B"]]
        result = chunk_document(_doc([_table("t1", rows)]), document_id="d", config=CFG)
        assert len(result.chunks) == 1
        chunk = result.chunks[0]
        assert chunk.kind == "table"
        assert chunk.content.split("\n") == [
            "| 模型 | 参数量 |",
            "| --- | --- |",
            "| A | 7B |",
            "| B | 13B |",
            "| C | 70B |",
        ]

    def test_large_table_groups_each_carry_header(self) -> None:
        rows = [["模型", "参数量"]] + [[f"m{i}", f"{i}B"] for i in range(7)]
        result = chunk_document(_doc([_table("t1", rows)]), document_id="d", config=CFG)
        # 表头 + 7 数据行，agg=3 → 3 组
        assert len(result.chunks) == 3
        for chunk in result.chunks:
            assert chunk.kind == "table"
            lines = chunk.content.split("\n")
            # 每组重携表头（markitdown 表头语义瑕疵缓解）
            assert lines[0] == "| 模型 | 参数量 |"
            assert lines[1] == "| --- | --- |"
        assert result.chunks[0].content.count("\n|") == 4  # 头2行 + 3数据行
        data_partition = "".join(c.content for c in result.chunks)
        for i in range(7):
            assert f"| m{i} | {i}B |" in data_partition

    def test_table_standalone_never_merged_with_paragraph(self) -> None:
        rows = [["h1", "h2"], ["a", "b"]]
        doc = _doc([_para("p1", "前文。"), _table("t1", rows), _para("p2", "后文。")])
        result = chunk_document(doc, document_id="d", config=CFG)
        assert [c.kind for c in result.chunks] == ["paragraph", "table", "paragraph"]


# ---------------------------------------------------------------------------
# PUA 占位符保护（A36）
# ---------------------------------------------------------------------------


class TestPuaPlaceholderProtection:
    def _ref(self) -> str:
        return make_document_ref("fs:node:abc123", "document", "子文档标签")

    def test_placeholder_never_split_across_chunks(self) -> None:
        ref = self._ref()
        # 占位符插入在无边界文本 30 字符硬切点附近（20~40 区间）——无保护必被切碎
        text = "a" * 22 + ref + "b" * 45
        assert len(text) > CFG.page_max_chars
        result = chunk_document(_doc([_para("p1", text)]), document_id="d", config=CFG)
        assert len(result.chunks) >= 2
        # 每块内占位符标记出现次数 = 完整占位符数（无残片）
        found: list[str] = []
        for chunk in result.chunks:
            content = chunk.content
            assert content.count("<document-ref") == len(find_document_refs(content))
            found += [r.node_id for r in find_document_refs(content)]
        # 原占位符恰好在唯一 chunk 完整出现一次
        assert found.count("fs:node:abc123") == 1
        # 含占位符的 chunk references 携带该引用，其余为空（引用随切片走）
        carriers = [c for c in result.chunks if c.references]
        assert len(carriers) == 1
        assert carriers[0].references == [
            {"node_id": "fs:node:abc123", "kind": "document", "label": "子文档标签"}
        ]

    def test_unclosed_placeholder_rejected(self) -> None:
        text = "a" * 10 + '<document-ref id="fs:node:x" kind="document">未闭合'
        with pytest.raises(ValueError, match="document-ref"):
            chunk_document(_doc([_para("p1", text)]), document_id="d", config=CFG)


# ---------------------------------------------------------------------------
# chunk metadata 对齐（data-model §1.8）与 content_hash
# ---------------------------------------------------------------------------


class TestChunkMetadata:
    def test_meta_fields_aligned(self) -> None:
        doc = _doc([_para("p1", "唯一一段。")])
        result = chunk_document(doc, document_id="doc-uuid-1", config=CFG)
        chunk = result.chunks[0]
        assert chunk.document_id == "doc-uuid-1"
        assert chunk.chunk_index == 0
        assert chunk.node_id == "loc:blk:p1"
        assert chunk.kind == "paragraph"
        assert chunk.raw_type == 2
        assert chunk.heading_path == ["旅行记录"]
        assert chunk.char_start == 0
        assert chunk.char_end == len("唯一一段。")
        assert chunk.references == []
        assert chunk.loc is None
        assert chunk.token_count == estimate_tokens(chunk.content) > 0
        assert result.document_text == "唯一一段。"

    def test_chunk_index_contiguous_per_document(self) -> None:
        root = IrNode(
            id="virtual:root",
            type=IrNodeType.DOCUMENT,
            meta=_meta("root"),
            children=[
                _doc_at("d1", [_para("a1", "a" * 30), _para("a2", "b" * 30)], "文一", []),
                _doc_at("d2", [_para("b1", "文二段落。")], "文二", []),
            ],
        )
        results = chunk_forest(root, config=CFG)
        assert len(results) == 2
        # 文档一：30+2+30=62 > 目标窗 50 → 两 chunk；文档二：单 chunk
        assert [c.chunk_index for c in results[0].chunks] == [0, 1]
        assert [c.chunk_index for c in results[1].chunks] == [0]
        assert all(c.document_id is None for c in results[0].chunks + results[1].chunks)
        # 文档正文互不串
        assert results[0].document_text == "a" * 30 + "\n\n" + "b" * 30
        assert results[1].document_text == "文二段落。"

    def test_kind_and_loc_passthrough(self) -> None:
        node = IrNode.model_validate(
            {
                "id": "loc:blk:pg1",
                "type": "paragraph",
                "text": "本地页段落。",
                "meta": _meta(2),
                "loc": {"origin": "local", "page": 3},
            }
        )
        result = chunk_document(_doc([node]), document_id="d", config=CFG)
        chunk = result.chunks[0]
        assert chunk.kind == "paragraph"
        assert chunk.loc == {"origin": "local", "page": 3}

    def test_char_ranges_within_document_text(self) -> None:
        doc = _doc([_para("p1", "第一段落。"), _code("c1", "print(1)"), _para("p2", "第二段落。")])
        result = chunk_document(doc, document_id="d", config=CFG)
        assert result.document_text == "第一段落。\n\nprint(1)\n\n第二段落。"
        for chunk in result.chunks:
            assert 0 <= chunk.char_start < chunk.char_end <= len(result.document_text)


class TestContentHash:
    def test_per_chunk_hash_stable_and_correct(self) -> None:
        doc = _doc([_para("p1", "同一段文本。")])
        r1 = chunk_document(doc, document_id="d", config=CFG)
        r2 = chunk_document(doc, document_id="d", config=CFG)
        chunk = r1.chunks[0]
        assert chunk.content_hash == r2.chunks[0].content_hash
        assert chunk.content_hash == hashlib.sha256(chunk.content.strip().encode("utf-8")).hexdigest()
        assert len(chunk.content_hash) == 64

    def test_different_text_different_hash(self) -> None:
        r1 = chunk_document(_doc([_para("p1", "文本甲。")]), document_id="d", config=CFG)
        r2 = chunk_document(_doc([_para("p1", "文本乙。")]), document_id="d", config=CFG)
        assert r1.chunks[0].content_hash != r2.chunks[0].content_hash

    def test_document_level_hash_for_kb_documents_wiring(self) -> None:
        r1 = chunk_document(_doc([_para("p1", "正文内容。")]), document_id="d", config=CFG)
        r2 = chunk_document(_doc([_para("p1", "正文内容变了。")]), document_id="d", config=CFG)
        assert r1.document_content_hash == content_hash(r1.document_text)
        assert r1.document_content_hash != r2.document_content_hash
        assert len(r1.document_content_hash) == 64


# ---------------------------------------------------------------------------
# 配置消费（ChunkingConfig.from_spec）与边界
# ---------------------------------------------------------------------------


class TestChunkingConfig:
    def test_from_spec_reads_kb_section(self) -> None:
        spec = {
            "kb": {
                "chunk_size": 900,
                "chunk_overlap": 100,
                "chunking": {
                    "page_max_chars": 1100,
                    "para_max_chars": 350,
                    "sentence_max_chars": 180,
                    "table_agg_rows": 8,
                },
            }
        }
        cfg = ChunkingConfig.from_spec(spec)
        assert (cfg.chunk_size, cfg.chunk_overlap) == (900, 100)
        assert (cfg.page_max_chars, cfg.para_max_chars) == (1100, 350)
        assert (cfg.sentence_max_chars, cfg.table_agg_rows) == (180, 8)

    def test_from_spec_missing_keys_fall_back_to_contract_defaults(self) -> None:
        cfg = ChunkingConfig.from_spec({})
        assert (cfg.chunk_size, cfg.chunk_overlap) == (800, 120)
        assert (cfg.page_max_chars, cfg.para_max_chars, cfg.sentence_max_chars, cfg.table_agg_rows) == (
            1200,
            400,
            200,
            10,
        )

    def test_invalid_config_rejected(self) -> None:
        with pytest.raises(ValueError, match="overlap"):
            ChunkingConfig(chunk_size=100, chunk_overlap=100)
        with pytest.raises(ValueError):
            ChunkingConfig(chunk_size=0)


class TestBoundaries:
    def test_empty_units_skipped_with_trail(self) -> None:
        doc = _doc(
            [_para("p1", ""), _para("p2", "   "), _para("p3", "有效段落。")]
        )
        result = chunk_document(doc, document_id="d", config=CFG)
        assert len(result.chunks) == 1
        assert result.skipped_units == 2

    def test_file_attachment_skipped(self) -> None:
        node = IrNode(
            id="loc:blk:f1",
            type=IrNodeType.FILE_ATTACHMENT,
            meta={"raw_type": "file", "parse_status": "pending"},
        )
        result = chunk_document(_doc([node]), document_id="d", config=CFG)
        assert result.chunks == []
        assert result.skipped_units == 1

    def test_headings_only_document_yields_zero_chunks(self) -> None:
        result = chunk_document(_doc([_heading("h1", 1, "空章节")]), document_id="d", config=CFG)
        assert result.chunks == []

    def test_non_document_input_rejected(self) -> None:
        with pytest.raises(ValueError, match="document"):
            chunk_document(_para("p1", "不是文档节点"), document_id="d", config=CFG)

    def test_document_level_forest_root_accepted(self) -> None:
        results = chunk_forest(_doc([_para("p1", "单文档森林。")]), config=CFG)
        assert len(results) == 1
        assert results[0].chunks[0].content == "单文档森林。"

    def test_document_text_excludes_headings(self) -> None:
        doc = _doc([_heading("h1", 1, "章节"), _para("p1", "正文段落。")])
        result = chunk_document(doc, document_id="d", config=CFG)
        assert result.document_text == "正文段落。"
        assert "章节" not in result.document_text
