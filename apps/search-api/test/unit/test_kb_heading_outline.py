"""PDF 书签大纲校正纯逻辑单测（TDD 红先；合成数据零 PDF 文件依赖）。

覆盖面（工单 B/A 段）：
- 全顶级书签 + docling 乱层级 → 锚点全部钉回 level=1；
- 多节点同名按页码消歧；文档开头无锚点外推；锚点间 delta 重校准与夹取；
- 书签可用性护栏（条目数 / 命中率）不满足 → 序列一致性清洗；
- 无书签 → 序列清洗（跳级夹取 + 重复标题只报数不删）；
- `**` 包裹 / 全半角 / 空白差异的归一化标题匹配；
- 大纲读取缝（pypdfium2 假体：零真实 PDF、零模型）。
"""

from types import SimpleNamespace
from typing import Any

import pytest

from app.rag.normalizers.heading_outline import (
    CORRECTION_BOOKMARK,
    CORRECTION_RAW,
    CORRECTION_SEQUENCE_CLEAN,
    HeadingItem,
    OutlineEntry,
    SkeletonSlot,
    correct_heading_levels,
    count_level_jumps,
    duplicate_title_report,
    normalize_title,
    plan_skeleton_slots,
    read_pdf_outline,
    resolve_anchors,
)


def _h(title: str, level: int, page: int | None = None) -> HeadingItem:
    return HeadingItem(title=title, level=level, page=page)


def _o(title: str, level: int = 0, page0: int | None = None) -> OutlineEntry:
    return OutlineEntry(title=title, level=level, page=page0)


# ---------------------------------------------------------------------------
# 标题归一化
# ---------------------------------------------------------------------------


def test_normalize_title_strips_emphasis_fullwidth_and_whitespace() -> None:
    """`**` 包裹 + 全半角 + 空白差异一律归一（书签标题与树内标题的比对口径）。"""
    assert normalize_title("**第一章　块级绑定**") == normalize_title("第一章 块级绑定")
    assert normalize_title("**第一章**  块级\n绑定") == normalize_title("第一章块级绑定")
    assert normalize_title("附录B：理解ES7（ES2016）") == normalize_title(
        "附录B：理解ES7(ES2016)"
    )
    assert normalize_title("第七章 Set与Map") == normalize_title("第七章 SET与map")
    assert normalize_title("") == ""
    # 只剥成对 ** —— 单个 * 是标题内容（如 `*args`、`A*B`），不得被吞
    assert normalize_title("A*B") == "a*b"
    assert normalize_title("**粗体**与*星号*") == "粗体与*星号*"


# ---------------------------------------------------------------------------
# 主路径：书签锚点钉正
# ---------------------------------------------------------------------------


def test_all_top_level_bookmarks_pin_anchors_to_level_one() -> None:
    """全顶级书签（pdfium 实测 level=0 为顶层）+ docling 乱层级 → 锚点 level=1。

    未命中 heading 按最近前置锚点的相对偏移重校准：delta = 原始 level − 锚点原始
    level；下界钉为锚点 level+1（锚点后的非锚点标题必是其后代）。
    """
    headings = [
        _h("关于", 1, 3),
        _h("引言", 2, 5),
        _h("通往 ES6 之路", 4, 5),
        _h("第一章 块级绑定", 2, 10),
        _h("块级声明", 3, 11),
        _h("第二章 字符串与正则表达式", 4, 22),
        _h("更好的 Unicode 支持", 4, 23),
    ]
    outline = [
        _o("关于", 0, 2),
        _o("引言", 0, 4),
        _o("第一章 块级绑定", 0, 9),
        _o("第二章 字符串与正则表达式", 0, 21),
    ]
    result = correct_heading_levels(headings, outline)

    assert result.path == CORRECTION_BOOKMARK
    assert result.usable is True
    assert [a.index for a in result.anchors] == [0, 1, 3, 5]
    assert [a.target_level for a in result.anchors] == [1, 1, 1, 1]
    # 锚点全 1；未命中按 delta（引言 raw2 为基准：4-2=+2 → 3；第一章 raw2 基准：3-2=+1 → 2；
    # 第二章 raw4 基准：4-4=0 → 下界 1+1=2）
    assert result.levels == [1, 1, 3, 1, 2, 1, 2]
    assert result.unmatched == []


def test_outline_levels_map_zero_based_to_ir_levels() -> None:
    """书签层级 0-based（顶层=0）→ IR level = 书签 level+1；子章节按书签嵌套。"""
    headings = [_h("第一章 函数", 3, 10), _h("声明与表达式", 3, 11), _h("第二章 类", 3, 40)]
    outline = [_o("第一章 函数", 0, 9), _o("声明与表达式", 1, 10), _o("第二章 类", 0, 39)]
    result = correct_heading_levels(headings, outline)

    assert result.path == CORRECTION_BOOKMARK
    assert result.levels == [1, 2, 1]
    assert count_level_jumps(result.levels) == 0


def test_one_based_outline_levels_normalized_by_minimum() -> None:
    """书签层级按最小层级归一再 +1（防御性归一；pypdfium2 实际按父链给深、顶层恒 0）。"""
    headings = [_h("第一章 函数", 3, 10), _h("声明与表达式", 3, 11), _h("第二章 类", 3, 40)]
    outline = [_o("第一章 函数", 1, 9), _o("声明与表达式", 2, 10), _o("第二章 类", 1, 39)]
    result = correct_heading_levels(headings, outline)

    assert result.path == CORRECTION_BOOKMARK
    assert result.levels == [1, 2, 1]


def test_baseline_escape_regression_stale_deep_entry_does_not_win() -> None:
    """P2-1 基准逃逸回归：锚点 raw=4、P raw=2（被压深）、R raw=5 → R 必须挂 P 之下（L5）。

    旧实现（按 raw 建字典）会把已弹出的锚点深度（raw4）留在映射里，R 取 raw4 为基准
    得出 L2——R 逃出 P 的子树与它平级。stack 口径下 P 之后的栈只剩 (2, P_level)，
    R = P_level + (5−2) = 5。
    """
    headings = [
        _h("章甲", 4, 10),
        _h("P 节", 2, 11),
        _h("R 小节", 5, 12),
        _h("章乙", 4, 20),
    ]
    outline = [_o("章甲", 0, 9), _o("章乙", 0, 19), _o("参考文献", 0, 99)]
    result = correct_heading_levels(headings, outline)

    assert result.path == CORRECTION_BOOKMARK
    assert result.levels == [1, 2, 5, 1]
    assert result.pressed == 1
    assert result.pressed_titles == ["P 节"]


def test_sequence_clean_document_level_starts_at_one() -> None:
    """序列清洗的文档层口径：原始层级从 >1 起步也归 level=1（不保留虚高层级）。"""
    headings = [_h("开篇", 3), _h("小节", 4)]
    result = correct_heading_levels(headings, [])
    assert result.levels == [1, 2]


def test_chart_bookmarks_do_not_disable_correction() -> None:
    """双层护栏：整体命中 58% <0.6，但顶级书签 3/3 全命中且 ≥3 → 仍启用校正。

    教材类 PDF 的图表书签（图 1-1…）常年搜不到对应标题，旧单层口径会连坐禁用整本。
    """
    headings = [
        _h("章一", 2, 10),
        _h("节1", 3, 11),
        _h("章二", 2, 20),
        _h("节2", 3, 21),
        _h("章三", 2, 30),
        _h("节3", 3, 31),
        _h("节4", 3, 32),
    ]
    outline = [
        _o("章一", 0, 9),
        _o("节1", 1, 10),
        _o("章二", 0, 19),
        _o("节2", 1, 20),
        _o("章三", 0, 29),
        _o("节3", 1, 30),
        _o("节4", 1, 31),
    ] + [_o(f"图 {i}-1 示意图", 1, 100 + i) for i in range(5)]
    result = correct_heading_levels(headings, outline)

    assert len(outline) == 12
    assert len(result.anchors) == 7  # 7/12 = 58.3% < 0.6
    assert result.usable is True
    assert result.path == CORRECTION_BOOKMARK
    assert result.levels == [1, 2, 1, 2, 1, 2, 2]
    assert len(result.unmatched) == 5


def test_dual_gate_requires_three_top_level_hits() -> None:
    """双层护栏边界①：「顶级命中 ≥3」不可省——顶级 2 条全中 + 10 条未命中 → 禁用。"""
    headings = [_h("章一", 2, 10), _h("章二", 2, 20)]
    outline = [_o("章一", 0, 9), _o("章二", 0, 19)] + [
        _o(f"图 {i}-1 示意图", 1, 100 + i) for i in range(10)
    ]
    result = correct_heading_levels(headings, outline)

    assert len(result.anchors) == 2  # 顶级 2/2 全中（命中率 100%）
    assert result.usable is False  # 但顶级命中数 2 < 3
    assert result.path == CORRECTION_SEQUENCE_CLEAN


def test_dual_gate_requires_top_level_hit_rate() -> None:
    """双层护栏边界②：「顶级命中率 ≥0.6」不可省——顶级 3 命中 / 6 条（50%）→ 禁用。"""
    headings = [_h("章一", 2, 10), _h("章二", 2, 20), _h("章三", 2, 30)]
    outline = [_o("章一", 0, 9), _o("章二", 0, 19), _o("章三", 0, 29)] + [
        _o(f"附{i}", 0, 50 + i) for i in range(3)
    ]
    result = correct_heading_levels(headings, outline)

    assert len(result.anchors) == 3  # 顶级命中数 3 达标
    assert result.usable is False  # 但顶级命中率 3/6 = 50% < 0.6
    assert result.path == CORRECTION_SEQUENCE_CLEAN


def test_multi_candidate_resolved_by_page() -> None:
    """同名多节点 → 书签 dest 页（0-based→+1）与节点 loc.page 精确匹配者胜出。"""
    headings = [
        _h("前言", 4, 4),
        _h("第二章 字符串", 4, 22),
        _h("第二章 字符串", 4, 323),  # 修改记录区把章节名当粗体条目的重复识别
        _h("小结", 4, 24),
    ]
    outline = [_o("前言", 0, 3), _o("第二章 字符串", 0, 21), _o("小结", 0, 23)]
    result = correct_heading_levels(headings, outline)

    assert [a.index for a in result.anchors] == [0, 1, 3]
    assert result.levels[2] == 2  # 重复识别项按 delta 重校准并钉在锚点之下（不冒充章级）
    assert result.levels[1] == 1


def test_page_disambiguation_prefers_later_page_over_document_order() -> None:
    """变异敏感：同名相邻两页、书签指后页 → 必须选后页节点（文档序首位是错误答案）。"""
    headings = [_h("第二章 方法", 4, 22), _h("第二章 方法", 4, 23)]
    outline = [_o("第二章 方法", 0, 22)]  # dest 22 → 目标页 23
    anchors, _unmatched = resolve_anchors(headings, outline)
    assert [a.index for a in anchors] == [1]


def test_page_disambiguation_tolerance_beats_document_order() -> None:
    """变异敏感：±1 页容差命中的候选非文档序首位 → 仍须选它（不是首个候选兜底）。"""
    headings = [_h("第三章 函数", 4, 40), _h("第三章 函数", 4, 30)]
    outline = [_o("第三章 函数", 0, 30)]  # dest 30 → 目标页 31；|30-31|=1 容差命中后者
    anchors, _unmatched = resolve_anchors(headings, outline)
    assert [a.index for a in anchors] == [1]


def test_page_disambiguation_prefers_exact_over_tolerance() -> None:
    """精确页优先于 ±1 容差（两者同时存在时不吃容差档）。"""
    headings = [_h("第四章 对象", 4, 31), _h("第四章 对象", 4, 32)]
    outline = [_o("第四章 对象", 0, 31)]  # 目标页 32 → 精确命中第二个（31 也是容差候选）
    anchors, _unmatched = resolve_anchors(headings, outline)
    assert [a.index for a in anchors] == [1]


def test_page_disambiguation_tolerance_width_is_one_page() -> None:
    """容差宽度锁死为 1 页：差 2 页的候选不参与（否则会选到文档序首位的错节点）。"""
    headings = [_h("第五章 模块", 4, 30), _h("第五章 模块", 4, 33)]
    outline = [_o("第五章 模块", 0, 31)]  # 目标页 32：|30-32|=2 排除，|33-32|=1 命中
    anchors, _unmatched = resolve_anchors(headings, outline)
    assert [a.index for a in anchors] == [1]


def test_pre_anchor_headings_extrapolated_from_first_anchor() -> None:
    """文档开头无前置锚点 → 以首个锚点（raw2→level1）为参照外推，层级逐条不同。

    外推口径 `首锚点 level + (本节点 raw − 首锚点 raw)`：raw1 → 1（夹取下界）、
    raw3 → 2、raw4 → 3——与「恒等保留原始 raw」可区分（该用例锁死外推逻辑本身）。
    """
    headings = [
        _h("扉页", 1, 1),
        _h("目錄", 3, 2),
        _h("版权页", 4, 3),
        _h("关于", 2, 6),
        _h("引言", 3, 8),
        _h("第一章", 3, 10),
    ]
    outline = [_o("关于", 0, 5), _o("引言", 0, 7), _o("第一章", 0, 9)]
    result = correct_heading_levels(headings, outline)

    assert result.path == CORRECTION_BOOKMARK
    assert result.levels == [1, 2, 3, 1, 1, 1]


def test_pressed_heading_becomes_baseline_for_deeper_headings() -> None:
    """评审复现（P1-1）：被压深的标题自身成为后续更深标题的偏移基准，直到下一个锚点。

    原始 [1,1,2,2,1] + 首尾两条顶级书签 → 校正 [1,2,3,3,1]：章边界语义保持（2.1/2.2
    仍挂在被压深的「第二章 方法」之下，而非与它平级或被抬回章级）。
    """
    headings = [
        _h("第一章 函数", 1, 10),
        _h("第二章 方法", 1, 20),
        _h("2.1 声明", 2, 21),
        _h("2.2 表达式", 2, 22),
        _h("第三章 结果", 1, 30),
    ]
    outline = [
        _o("第一章 函数", 0, 9),
        _o("第三章 结果", 0, 29),
        _o("参考文献", 0, 99),
    ]
    result = correct_heading_levels(headings, outline)

    assert result.path == CORRECTION_BOOKMARK
    assert result.levels == [1, 2, 3, 3, 1]
    assert result.pressed == 1  # 仅「第二章 方法」被下界压深
    assert result.unmatched == ["参考文献"]
    assert result.level_jumps_after == 0


def test_pressed_siblings_share_anchor_baseline() -> None:
    """被压深的同级兄弟各自回到锚点 level+1（不随压深链条层递加深）。"""
    headings = [
        _h("章甲", 1, 10),
        _h("同名重复甲", 1, 100),
        _h("同名重复乙", 1, 200),
        _h("章乙", 1, 300),
    ]
    outline = [_o("章甲", 0, 9), _o("章乙", 0, 299), _o("附录", 0, 400)]
    result = correct_heading_levels(headings, outline)
    assert result.levels == [1, 2, 2, 1]
    assert result.pressed == 2


def test_offset_recalibration_clamped_to_nine() -> None:
    """重校准夹取上限 9（IR level 契约 1~9，越界不下发非法 level）。"""
    headings = [_h("深章", 1, 3), _h("更深小节", 9, 4), _h("另一章", 1, 40)]
    outline = [_o("深章", 4, 2), _o("另一章", 0, 39), _o("缺章", 0, 50)]  # 书签 L4 → target 5
    result = correct_heading_levels(headings, outline)

    assert result.levels[0] == 5
    assert result.levels[1] == 9  # 5 + (9-1) = 13 → 夹取 9


def test_outline_title_unmatched_reported() -> None:
    """书签有、树内无（识别召回缺口）→ 计入 unmatched 清单。

    工单 A 修订（2026-11-24，推翻 T1-b「校正不造节点」）：造节点改由
    `plan_skeleton_slots` 出计划、docling_adapter 装配序执行——本函数仍是纯层级
    映射器，对输入 items 只定级不增删（造节点语义见 plan_skeleton_slots 用例）。
    """
    headings = [_h("关于", 1, 3), _h("引言", 1, 5), _h("附录A", 1, 300)]
    outline = [_o("关于", 0, 2), _o("引言", 0, 4), _o("附录A", 0, 299), _o("修改记录", 0, 321)]
    result = correct_heading_levels(headings, outline)

    assert result.unmatched == ["修改记录"]
    assert len(result.levels) == 3


# ---------------------------------------------------------------------------
# 骨架重建计划（工单 A）：未命中顶级书签条目 → 造节点规格（纯函数）
# ---------------------------------------------------------------------------


def test_plan_skeleton_slots_creates_nodes_for_unmatched_bookmarks() -> None:
    """ECMA「修改记录」形态：护栏过门 → 未命中顶级书签条目各得一个造节点规格。

    插入位 = pending 块序列中首个页码 ≥ 目标页（dest 0-based +1）的块之前
    （内容按相邻书签页码区间归属，页首即文本边界）；规格按插入位升序输出。
    """
    headings = [
        _h("关于", 1, 3),
        _h("第一章 块级绑定", 2, 10),
        _h("第三章 结果", 2, 20),
    ]
    outline = [
        _o("关于", 0, 2),
        _o("第一章 块级绑定", 0, 9),
        _o("第三章 结果", 0, 19),
        _o("引言", 0, 0),
        _o("修改记录", 0, 321),
    ]
    block_pages = [1, 1, 2, 3, 3, 10, 322, 322, 323]
    slots = plan_skeleton_slots(headings, outline, block_pages)

    assert slots == [
        SkeletonSlot(title="引言", level=1, page=1, insert_index=0),
        SkeletonSlot(title="修改记录", level=1, page=322, insert_index=6),
    ]


def test_plan_skeleton_slots_guardrail_fail_blocks_creation() -> None:
    """双层护栏不过（3 条书签 1 命中）→ 零造节点，骨架重建整体不启用。

    缺书签的目标页刻意落在 block_pages 范围内（可插入）——若护栏被去掉，
    缺一/缺二会拿到造节点规格（变异敏感；页不可达的形态由残损书签用例单独锁）。
    """
    headings = [_h("甲", 1, 3)]
    outline = [_o("甲", 0, 2), _o("缺一", 0, 3), _o("缺二", 0, 4)]
    assert plan_skeleton_slots(headings, outline, [1, 2, 3, 4, 5]) == []


def test_plan_skeleton_slots_dual_gate_pass_chart_bookmarks_not_fabricated() -> None:
    """双层护栏顶级支通过（图表书签豁免形态）→ 造节点不反转豁免面。

    顶级书签 3/3 全命中启用重建；未命中的**非顶级**图表书签（图 1-1…）不得被
    造节点——护栏豁免它们正是为了不连坐整本，造出来会把图表页内容划进伪章。
    """
    headings = [
        _h("章一", 2, 10),
        _h("节1", 3, 11),
        _h("章二", 2, 20),
        _h("节2", 3, 21),
        _h("章三", 2, 30),
        _h("节3", 3, 31),
        _h("节4", 3, 32),
    ]
    outline = [
        _o("章一", 0, 9),
        _o("节1", 1, 10),
        _o("章二", 0, 19),
        _o("节2", 1, 20),
        _o("章三", 0, 29),
        _o("节3", 1, 30),
        _o("节4", 1, 31),
    ] + [_o(f"图 {i}-1 示意图", 1, 100 + i) for i in range(5)]
    block_pages = [10, 11, 20, 21, 30, 31, 32]
    assert plan_skeleton_slots(headings, outline, block_pages) == []


def test_plan_skeleton_slots_creates_only_top_level_entries() -> None:
    """整体命中率达标时，未命中的非顶级条目（小节/图表书签）同样不造节点。

    造节点只收顶级（章级）条目——书签大纲对「章」的确定性最强；小节粒度版面
    已有候选，缺小节多属版式变体，造节点风险大于收益。
    """
    headings = [
        _h("章一", 2, 10),
        _h("节1", 3, 11),
        _h("章二", 2, 20),
        _h("节2", 3, 21),
        _h("章三", 2, 30),
        _h("节3", 3, 31),
        _h("节4", 3, 32),
    ]
    outline = [
        _o("章一", 0, 9),
        _o("节1", 1, 10),
        _o("章二", 0, 19),
        _o("节2", 1, 20),
        _o("章三", 0, 29),
        _o("节3", 1, 30),
        _o("节4", 1, 31),
        _o("节5", 1, 32),
        _o("图 2-1", 1, 40),
    ]
    block_pages = [10, 11, 20, 21, 30, 31, 32, 33, 41]
    assert plan_skeleton_slots(headings, outline, block_pages) == []


def test_plan_skeleton_slots_skips_damaged_bookmark_entries() -> None:
    """残损书签三形态不造节点：标题空（归一化零串）/缺 dest 页码/目标页超出正文。

    超界条目保持 unmatched 上报（伤情单可见），不给文档尾伪造空节（否则末节
    内容会被错划进它——`test_pressed_heading_keeps_its_children_in_tree` 的
    「参考文献」形态即依赖此防御）。
    """
    headings = [
        _h("关于", 1, 3),
        _h("章二", 2, 10),
        _h("章三", 2, 20),
        _h("章四", 2, 30),
        _h("章五", 2, 40),
    ]
    outline = [
        _o("关于", 0, 2),
        _o("章二", 0, 9),
        _o("章三", 0, 19),
        _o("章四", 0, 29),
        _o("章五", 0, 39),
        _o("", 0, 50),
        _o("无 dest 条目", 0, None),
        _o("超界条目", 0, 999),
    ]
    block_pages = [1, 3, 10, 20, 30, 40]
    assert plan_skeleton_slots(headings, outline, block_pages) == []


def test_plan_skeleton_slots_insert_index_is_page_boundary() -> None:
    """插入位是页码区间边界：目标页之前的块全部留在前一书签区间（不被吞入）。"""
    headings = [_h("关于", 1, 3), _h("章二", 2, 10)]
    outline = [_o("关于", 0, 2), _o("章二", 0, 9), _o("附录", 0, 5)]
    block_pages = [1, 2, 3, 3, 10, 10]
    slots = plan_skeleton_slots(headings, outline, block_pages)

    assert slots == [SkeletonSlot(title="附录", level=1, page=6, insert_index=4)]
    assert all(page < 6 for page in block_pages[:4])  # 前区间内容不被吞


def test_plan_skeleton_slots_multi_level_outline_top_level_mapping() -> None:
    """多级书签形态：未命中顶级条目 level = 书签层级按最小层级归一 + 1。"""
    headings = [
        _h("章一", 2, 10),
        _h("节1", 3, 11),
        _h("章二", 2, 20),
        _h("节2", 3, 21),
        _h("章三", 2, 30),
        _h("节3", 3, 31),
    ]
    outline = [
        _o("章一", 0, 9),
        _o("节1", 1, 10),
        _o("章二", 0, 19),
        _o("节2", 1, 20),
        _o("章三", 0, 29),
        _o("节3", 1, 30),
        _o("附录A", 0, 39),
    ]
    block_pages = [10, 11, 20, 21, 30, 31, 40]
    slots = plan_skeleton_slots(headings, outline, block_pages)

    assert slots == [SkeletonSlot(title="附录A", level=1, page=40, insert_index=6)]


def test_plan_skeleton_slots_empty_outline_no_slots() -> None:
    """无书签（sample_structured 形态）→ 零造节点（现行路径逐字节等价的前提）。"""
    headings = [_h("甲", 1, 3), _h("乙", 2, 5)]
    assert plan_skeleton_slots(headings, [], [1, 3, 5]) == []


# ---------------------------------------------------------------------------
# 可用性护栏（垃圾书签 / 命中率不足）
# ---------------------------------------------------------------------------


def test_too_few_outline_entries_not_usable() -> None:
    """书签条目 < 3 → 不启用校正（两份条目的自动大纲不足以定章法）。"""
    headings = [_h("第一章", 1, 3), _h("小节", 1, 4)]
    outline = [_o("第一章", 0, 2), _o("小节", 0, 3)]
    result = correct_heading_levels(headings, outline)

    assert result.usable is False
    assert result.path == CORRECTION_SEQUENCE_CLEAN


def test_hit_rate_below_threshold_falls_back_to_sequence_clean() -> None:
    """命中率（命中书签数/书签总数）< 0.6 → 书签与正文结构脱节，整份禁用校正。

    评审复现口径：3 条书签仅 1 命中（33%）→ 走 sequence-clean，不让 1 条锚点改写全文。
    """
    headings = [_h("甲", 1, 3), _h("另一节", 4, 5), _h("第三节", 3, 6)]
    outline = [_o("甲", 0, 2), _o("缺书签一", 0, 3), _o("缺书签二", 0, 4)]
    result = correct_heading_levels(headings, outline)

    assert result.usable is False
    assert result.path == CORRECTION_SEQUENCE_CLEAN
    # 序列清洗产物（原始层级栈压取：1→1、4 压 2、3 与 4 平级 2）：书签不参与定级
    assert result.levels == [1, 2, 2]


def test_hit_rate_at_threshold_is_usable() -> None:
    """命中率恰为 0.6 边界 → 启用（`>=` 语义）。"""
    headings = [_h(name, 1, 3 + i) for i, name in enumerate("甲乙丙丁戊己")]
    outline = [_o(name, 0, 2 + i) for i, name in enumerate("甲乙丙丁戊己")] + [
        _o(f"缺{i}", 0, 50 + i) for i in range(4)
    ]
    result = correct_heading_levels(headings, outline)
    assert len(result.anchors) == 6 and len(outline) == 10
    assert result.path == CORRECTION_BOOKMARK
    assert result.levels == [1, 1, 1, 1, 1, 1]


def test_thirty_three_percent_hit_rate_disables_correction() -> None:
    """特征化：33% 命中（评审复现的「3 条书签 1 命中改写全文」）必须走禁用路径。"""
    headings = [_h("第一章 函数", 1, 10), _h("第二章 方法", 2, 20), _h("第三章 结果", 1, 30)]
    outline = [
        _o("第一章 函数", 0, 9),
        _o("参考文献", 0, 90),
        _o("索引", 0, 95),
    ]
    result = correct_heading_levels(headings, outline)
    assert result.usable is False
    assert result.path == CORRECTION_SEQUENCE_CLEAN


# ---------------------------------------------------------------------------
# 无书签兜底：序列一致性清洗
# ---------------------------------------------------------------------------


def test_no_outline_sequence_clean_clamps_level_jumps() -> None:
    """无书签 → 序列清洗：按原始层级栈压取（相对最近更浅祖先压到 +1）。"""
    headings = [_h("甲", 1), _h("乙", 4), _h("丙", 3), _h("丁", 5), _h("戊", 2)]
    result = correct_heading_levels(headings, [])

    assert result.path == CORRECTION_SEQUENCE_CLEAN
    # 1→1；4 相对祖先 1 压 2；3 与 4 平级（栈弹出 4）→ 2；5 相对 3 压 3；2 弹回 1 之下 → 2
    assert result.levels == [1, 2, 2, 3, 2]
    assert result.level_jumps_before == 2
    assert result.level_jumps_after == 0


def test_sequence_clean_uses_raw_level_stack_not_previous_sibling() -> None:
    """父子反转修复：浅标题回退时按「最近更浅祖先」压取，不按紧邻前一个标题。

    `[1,4,3,2]` 的紧邻式算会得出 `[1,2,3,2]`（3 被当成 2 的父级 → 父子反转）；
    层级栈口径得出 `[1,2,2,2]`（4/3/2 同属 1 的下一层）。
    """
    headings = [_h("甲", 1), _h("乙", 4), _h("丙", 3), _h("丁", 2)]
    result = correct_heading_levels(headings, [])
    assert result.levels == [1, 2, 2, 2]


def test_sequence_clean_marks_duplicate_titles_without_dropping_nodes() -> None:
    """重复连续标题只报数不删节点（内容零丢失；重复检测归自检门禁）。"""
    headings = [_h("修改为：", 2), _h("修改为：", 2), _h("正文", 3), _h("**修改为：**", 2)]
    result = correct_heading_levels(headings, [])

    assert len(result.levels) == len(headings)
    assert result.duplicate_titles[0][0] == "修改为："
    assert result.duplicate_titles[0][1] == 3


def test_no_headings_reports_raw_path() -> None:
    """零 heading → path=raw（无可校正对象，不伪造）。"""
    result = correct_heading_levels([], [])
    assert result.path == CORRECTION_RAW
    assert result.levels == []


def test_duplicate_title_report_and_level_jumps_helpers() -> None:
    """诊断指标纯函数：重复标题分组计数（按出现次数降序）+ 跳级计数。"""
    headings = [_h("总结", 2), _h("甲", 3), _h("总结", 2), _h("乙", 5)]
    dups = duplicate_title_report(headings)
    assert dups[0] == ("总结", 2)
    assert count_level_jumps([1, 2, 4, 3]) == 1
    assert count_level_jumps([1, 2, 3]) == 0
    assert count_level_jumps([]) == 0


# ---------------------------------------------------------------------------
# 大纲读取缝（pypdfium2 假体）
# ---------------------------------------------------------------------------


class _FakeDest:
    def __init__(self, index: int) -> None:
        self._index = index

    def get_index(self) -> int:
        return self._index


class _FakeEntry:
    def __init__(self, title: str, level: int, page: int | None = None, boom: bool = False,
                 boom_title: bool = False) -> None:
        self._title = title
        self.level = level
        self._page = page
        self._boom = boom
        self._boom_title = boom_title

    def get_title(self) -> str:
        if self._boom_title:
            raise RuntimeError("title 损坏")
        return self._title

    def get_dest(self) -> Any:
        if self._boom:
            raise RuntimeError("dest 损坏")
        return None if self._page is None else _FakeDest(self._page)


class _FakeDoc:
    def __init__(self, entries: list[_FakeEntry]) -> None:
        self._entries = entries
        self.closed = False
        self.max_depth: int | None = None

    def get_toc(self, max_depth: int = 6) -> Any:
        self.max_depth = max_depth
        return iter(self._entries)

    def close(self) -> None:
        self.closed = True


def test_read_pdf_outline_seam_reads_title_level_and_page() -> None:
    """读取缝：get_title / 0-based level / dest.get_index（0-based 页）→ OutlineEntry。

    单条 get_title 或 dest 损坏只降级该条（标题置空→自然进 unmatched / 页码缺→
    消歧退化），不废整份大纲。
    """
    doc = _FakeDoc(
        [
            _FakeEntry("关于", 0, 2),
            _FakeEntry("第一章 块级绑定", 0, 9),
            _FakeEntry("块级声明", 1, 10),
            _FakeEntry("坏 dest", 1, boom=True),
            _FakeEntry("坏 title", 1, 11, boom_title=True),
        ]
    )
    entries = read_pdf_outline(b"%PDF-1.4 fake", pdf_factory=lambda _data: doc)

    assert [(e.title, e.level, e.page) for e in entries] == [
        ("关于", 0, 2),
        ("第一章 块级绑定", 0, 9),
        ("块级声明", 1, 10),
        ("坏 dest", 1, None),
        ("", 1, 11),
    ]
    assert doc.max_depth == 8  # 深度上限覆盖多级书签
    assert doc.closed is True


def test_read_pdf_outline_propagates_reader_failure_to_caller() -> None:
    """读取失败原样上抛（适配器侧重归一为「书签不可用 → 序列清洗」，此处不吞）。"""

    def _boom(_data: bytes) -> Any:
        raise RuntimeError("pdfium 不可用")

    with pytest.raises(RuntimeError, match="pdfium"):
        read_pdf_outline(b"x", pdf_factory=_boom)


def test_resolve_anchors_claims_each_node_once() -> None:
    """同一节点不被两条书签重复占用（后到条目按 unmatched 处理）。"""
    headings = [_h("同名", 1, 3)]
    outline = [_o("同名", 0, 2), _o("同名", 0, 2)]
    anchors, unmatched = resolve_anchors(headings, outline)
    assert len(anchors) == 1
    assert unmatched == ["同名"]


def test_simple_namespace_factory_form_supported() -> None:
    """读取缝兼容轻量对象（生产调用方零传；测试假体可为 SimpleNamespace）。"""
    doc = SimpleNamespace(
        get_toc=lambda max_depth=8: iter([_FakeEntry("关于", 0, 2)]),
        close=lambda: None,
    )
    entries = read_pdf_outline(b"x", pdf_factory=lambda _d: doc)
    assert entries == [OutlineEntry(title="关于", level=0, page=2)]
