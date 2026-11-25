"""探针「③.b 小节级伤情单」（工单 C v2）的单测（合成 PipelineOutput，零真实 PDF）。

验收口径（plan-pdf-root-cause-remedies-20261124 工单 C）：探针自检节升级为小节级
伤情单，五类检测机器先于用户抓异常——
  ① 书签召回缺失：书签条目在树内零命中者**逐条**列出（标题/层级/目标页）；
  ② 标题密度异常：heading/千字 与 heading/页 分布，超阈值（单页密集 / 连续簇）列嫌疑；
  ③ 提升行形态矩阵：Fix 2 提升的每条标题输出句末标点/行数/字数/字号比值（句末正则
    缺口 P3-1 量化暴露——「脱漏」= 句末标点但提升正则不含）；
  ④ 空 text 候选：formula 节点计数与标题占比（Fix 1 通道活跃度监控）；
  ⑤ 无书签显著警示：correction_path 落序列清洗/裸路径时伤情单第一行显著警告。

**硬约束（零变化门禁）**：非 PDF 通道与关闭态（parse_meta 无 heading_outline 键，
等价 KB_PDF_OUTLINE_CORRECTION=false）下 `build_injury_report` 恒 None、渲染输出
与 v1 逐字节一致（不新增任何一行）。探针只读观察，不改管线行为。
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from app.rag.chunker import ChunkingConfig, ChunkingResult
from app.rag.ir_schema import IrNode, IrNodeType
from app.rag.normalizers.heading_outline import OutlineEntry

_REPO_ROOT = Path(__file__).resolve().parents[4]


def _load_probe() -> Any:
    """按路径装载探针脚本（scripts/ 非包；模块级只做 import，无副作用）。"""
    path = _REPO_ROOT / "scripts" / "probe_kb_pipeline.py"
    spec = importlib.util.spec_from_file_location(
        "probe_kb_pipeline_injury_under_test", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass 解析 __module__ 需已注册（exec 前）
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


# ---------------------------------------------------------------------------
# 合成树/产物工厂（零 PDF；loc 走 LocalLoc dict 形态）
# ---------------------------------------------------------------------------


def _loc(page: int | None) -> dict[str, Any]:
    return {
        "origin": "local",
        "page": page,
        "char_range": None,
        "file_object_key": None,
    }


def _heading(
    block_id: str,
    title: str,
    level: int,
    page: int | None = None,
    *,
    raw_type: str = "section_header",
) -> IrNode:
    node = IrNode(
        id=f"loc:blk:{block_id}",
        type=IrNodeType.HEADING,
        title=title,
        level=level,
        meta={"raw_type": raw_type},
    )
    node.loc = _loc(page)
    return node


def _para(
    block_id: str,
    text: str | None,
    page: int | None = None,
    *,
    raw_type: str = "text",
) -> IrNode:
    node = IrNode(
        id=f"loc:blk:{block_id}",
        type=IrNodeType.PARAGRAPH,
        text=text,
        meta={"raw_type": raw_type},
    )
    node.loc = _loc(page)
    return node


def _document(children: list[IrNode]) -> IrNode:
    return IrNode(
        id="loc:doc:0",
        type=IrNodeType.DOCUMENT,
        doc_kind="upload",
        meta={"raw_type": "docling.document"},
        children=children,
    )


def _make_output(
    document: IrNode,
    *,
    ext: str = "pdf",
    correction_path: str | None = "bookmark",
    outline: list[OutlineEntry] | None = None,
    pages: int = 6,
    body_chars: int = 3000,
    body_font_size: float | None = 16.0,
    promoted_pipeline: int = 0,
) -> Any:
    """合成 PipelineOutput。`correction_path=None` 模拟关闭态（heading_outline 键整缺）。"""
    parse_meta: dict[str, Any] = {
        "pages": pages,
        "tables": 0,
        "code_blocks": 0,
        "parser": "docling",
        "heading_heuristic": {
            "inferred": 0,
            "fallback_flat": 0,
            "body_font_size": body_font_size,
            "promoted": promoted_pipeline,
        },
    }
    if correction_path is not None:
        parse_meta["heading_outline"] = {
            "correction_path": correction_path,
            "bookmarks": 0,
            "anchors": 0,
            "unmatched": [],
            "unmatched_count": 0,
            "pressed": 0,
            "pressed_sample": [],
            "level_jumps_before": 0,
            "level_jumps_after": 0,
        }
    return probe.PipelineOutput(
        source_label="结构自检样本",
        ext=ext,
        byte_size=1024,
        document=document,
        parse_meta=parse_meta,
        chunking=ChunkingResult(
            document_id=None,
            chunks=[],
            document_text="x" * body_chars,
            document_content_hash="",
            skipped_units=0,
            oversized_units=0,
        ),
        config=ChunkingConfig.from_spec({}),
        outline=outline,
    )


class _FakeSampler:
    """字号采样假体：标题原文 → (字号, 非加粗)；缺字 → None（真实面形态一致）。"""

    def __init__(self, size_by_text: dict[str, float]) -> None:
        self._size_by_text = size_by_text

    def text_font_stats(self, page_index: int, text: str) -> tuple[float, bool] | None:
        size = self._size_by_text.get(text)
        return None if size is None else (size, False)


# 良构标本：三章各带 600 字正文（间隔 > 簇阈值）、书签全命中、零提升零 formula。
_CHAPTER_TITLES = ["第一章 块级绑定", "第二章 字符串", "第三章 异步"]
_CLEAN_BODY = "正文段落。" * 100  # 600 字


def _clean_document() -> IrNode:
    children: list[IrNode] = []
    for index, title in enumerate(_CHAPTER_TITLES):
        children.append(_heading(f"{index:04d}", title, 1, index * 2 + 1))
        children.append(_para(f"{index:04d}b", _CLEAN_BODY, index * 2 + 1))
    return _document(children)


def _clean_outline() -> list[OutlineEntry]:
    return [
        OutlineEntry(title=title, level=0, page=index * 2)
        for index, title in enumerate(_CHAPTER_TITLES)
    ]


def _clean_output(**kwargs: Any) -> Any:
    return _make_output(
        _clean_document(), outline=_clean_outline(), pages=6, **kwargs
    )


# ---------------------------------------------------------------------------
# 门禁：非 PDF 与关闭态恒 None、渲染零变化（硬约束）
# ---------------------------------------------------------------------------


def test_injury_report_absent_for_non_pdf() -> None:
    """docx/md 通道 → 恒 None（伤情单不臆造 PDF 面）。"""
    output = _make_output(_clean_document(), ext="docx", outline=None)
    assert probe.build_injury_report(output) is None


def test_injury_report_absent_for_non_pdf_even_with_outline_meta() -> None:
    """门禁键在**通道**上：非 PDF 即便残留 heading_outline 键也不出伤情单。"""
    output = _make_output(_clean_document(), ext="md", correction_path="bookmark")
    assert probe.build_injury_report(output) is None


def test_injury_report_absent_when_correction_disabled() -> None:
    """关闭态（heading_outline 键整缺 = KB_PDF_OUTLINE_CORRECTION=false）→ 恒 None。"""
    output = _make_output(
        _clean_document(), correction_path=None, outline=_clean_outline()
    )
    assert probe.build_injury_report(output) is None


def _assert_v1_only_render(text: str, output: Any) -> None:
    """零变化锁：渲染不含任何伤情单新面，且恰好终止于 v1 结构自检块。"""
    v1_block = "\n".join(probe.render_structure_check_text(probe.build_structure_check(output)))
    assert text.endswith(v1_block)
    for marker in ("伤情单", "⚠️", "书签召回缺失", "标题密度", "提升行形态", "空 text 候选"):
        assert marker not in text


def test_render_text_byte_identical_for_non_pdf() -> None:
    """非 PDF：render_text 与 v1 完全一致（无新增行）。"""
    output = _make_output(_clean_document(), ext="docx", outline=None)
    _assert_v1_only_render(probe.render_text(output), output)


def test_render_text_byte_identical_when_correction_disabled() -> None:
    """关闭态：render_text 与 v1 完全一致（无新增行）——硬约束的关闭态面。"""
    output = _make_output(
        _clean_document(), correction_path=None, outline=_clean_outline()
    )
    _assert_v1_only_render(probe.render_text(output), output)


def test_render_html_omits_injury_for_non_pdf() -> None:
    """非 PDF html：无伤情单节，且自检节 → 主区交界处零新增行（逐字节锁）。"""
    payload = probe.render_html(_make_output(_clean_document(), ext="docx", outline=None))
    assert "伤情单" not in payload
    assert '</section>\n<div class="wrap">' in payload  # 关闭态同款交界（零空行插入）


def test_render_html_omits_injury_when_correction_disabled() -> None:
    """关闭态 html：伤情单整节不出现，交界处零新增行。"""
    output = _make_output(
        _clean_document(), correction_path=None, outline=_clean_outline()
    )
    payload = probe.render_html(output)
    assert "伤情单" not in payload
    assert '</section>\n<div class="wrap">' in payload


# ---------------------------------------------------------------------------
# ① 书签召回缺失（零命中逐条列出）
# ---------------------------------------------------------------------------


def test_missing_bookmarks_listed_individually() -> None:
    """命中 2/3：零命中的「修改记录」逐条列出（标题+层级+目标页）。"""
    outline = [*_clean_outline(), OutlineEntry(title="修改记录", level=0, page=321)]
    output = _make_output(_clean_document(), outline=outline)
    report = probe.build_injury_report(output)

    assert report is not None
    assert len(report.missing) == 1
    entry = report.missing[0]
    assert entry.title == "修改记录"
    assert entry.level == 0
    assert entry.page == 321

    rendered = "\n".join(probe.render_injury_report_text(report))
    assert "书签召回缺失    : 1 条" in rendered
    assert "「修改记录」" in rendered
    assert "目标页 322" in rendered  # 书签 dest 0 基 → 阅读页 1 基
    assert "树内零命中" in rendered


def test_missing_bookmarks_empty_when_all_hit() -> None:
    """不命中面：书签全命中 → 0 条（全部命中），不误报。"""
    report = probe.build_injury_report(_clean_output())
    assert report is not None
    assert report.missing == []

    rendered = "\n".join(probe.render_injury_report_text(report))
    assert "书签召回缺失    : 0 条（全部命中）" in rendered


def test_missing_bookmarks_unavailable_without_outline() -> None:
    """书签面不可读（outline=None）→ 如实标「无法核对」，不臆造清单。"""
    output = _make_output(_clean_document(), outline=None)
    report = probe.build_injury_report(output)
    assert report is not None
    assert report.missing_available is False
    rendered = "\n".join(probe.render_injury_report_text(report))
    assert "书签召回缺失    : 书签不可读，无法核对" in rendered


# ---------------------------------------------------------------------------
# ② 标题密度异常（单页密集 + 连续簇嫌疑清单）
# ---------------------------------------------------------------------------


def _clustered_document() -> IrNode:
    """「修改为：」×5 假阳性形态：连续 5 条标题、间隔 20 字短正文、3 条挤同页。"""
    children: list[IrNode] = []
    for index in range(5):
        page = 24 if index < 3 else 25
        children.append(_heading(f"{index:04d}", "修改为：", 2, page))
        children.append(_para(f"{index:04d}b", "短句。" * 5, page))  # 20 字
    return _document(children)


def test_density_suspects_for_clustered_fake_headings() -> None:
    """命中面：连续簇（char 轴）与单页密集（heading/页 轴）都入嫌疑清单。"""
    output = _make_output(_clustered_document(), outline=None, pages=30, body_chars=400)
    report = probe.build_injury_report(output)

    assert report is not None
    kinds = {suspect.kind for suspect in report.density_suspects}
    assert "cluster" in kinds
    assert "page" in kinds
    # 分布指标：5 heading / 400 字 = 12.5 条/千字；30 页 = 0.17 条/页
    assert report.headings == 5
    assert report.per_1k == pytest.approx(12.5)
    assert report.per_page == pytest.approx(5 / 30)

    rendered = "\n".join(probe.render_injury_report_text(report))
    assert "[连续簇]" in rendered
    assert "[单页密集]" in rendered
    assert "嫌疑 2 处" in rendered


def test_density_no_suspects_for_spread_headings() -> None:
    """不命中面：标题稀疏分布（间隔>阈值、单页 ≤2 条）→ 嫌疑 0 处。"""
    report = probe.build_injury_report(_clean_output())
    assert report is not None
    assert report.density_suspects == []

    rendered = "\n".join(probe.render_injury_report_text(report))
    assert "嫌疑 0 处" in rendered


# ---------------------------------------------------------------------------
# ③ 提升行形态矩阵（Fix 2 提升通道）
# ---------------------------------------------------------------------------


def test_promoted_rows_report_morphology_and_regex_gap() -> None:
    """命中面：raw_type=text/formula 的 heading 逐条输出形态；「？」脱漏、含正则标点不脱漏。"""
    document = _document([
        _heading("0300", "第一章 总则", 1, 1),
        _heading("0301", "关于本书", 2, 2, raw_type="text"),
        _heading("0302", "为什么选择 ES？", 2, 3, raw_type="formula"),
        _heading("0303", "修改说明：", 2, 4, raw_type="text"),
    ])
    output = _make_output(
        document,
        outline=[OutlineEntry(title="第一章 总则", level=0, page=0)],
    )
    report = probe.build_injury_report(
        output, font_sampler=_FakeSampler({"关于本书": 23.0, "为什么选择 ES？": 23.0})
    )

    assert report is not None
    rows = report.promoted
    assert [row.title for row in rows] == ["关于本书", "为什么选择 ES？", "修改说明："]
    # 形态列：字数 / 行数 / 句末标点 / 脱漏标记 / 字号比值
    assert rows[0].chars == 4 and rows[0].lines == 1
    assert rows[0].ending is None and rows[0].gap is False
    assert rows[0].ratio == pytest.approx(23.0 / 16.0)
    assert rows[1].ending == "？" and rows[1].gap is True  # P3-1 缺口：提升正则不含 ？
    assert rows[2].ending == "：" and rows[2].gap is False  # 正则面内的句末标点不算缺口

    rendered = "\n".join(probe.render_injury_report_text(report))
    assert (
        "提升行形态      : Fix 2 提升通道已提升 3 条 · 未提升候选 0 条"
        "（管线留痕 promoted=0）" in rendered
    )
    assert "「关于本书」 4 字 · 1 行 · 句末：无 · 字号比值 1.44" in rendered
    assert "句末：「？」（脱漏：提升正则不含）" in rendered
    assert "句末：「：」" in rendered


def test_promoted_rows_empty_when_no_promotion() -> None:
    """不命中面：全部 heading 都是 docling 原生 section_header → 已提升 0 条。"""
    report = probe.build_injury_report(_clean_output())
    assert report is not None
    assert report.promoted == []
    rendered = "\n".join(probe.render_injury_report_text(report))
    assert (
        "提升行形态      : Fix 2 提升通道已提升 0 条 · 未提升候选 0 条"
        "（管线留痕 promoted=0）" in rendered
    )


def test_promoted_ratio_none_without_sampler() -> None:
    """字号采样不可得（无 data / 无 sampler）→ 比值 —，其余形态列照常。"""
    document = _document([_heading("0310", "关于本书", 2, 2, raw_type="text")])
    output = _make_output(document, outline=None)
    report = probe.build_injury_report(output)  # output.data 未设、无注入 sampler

    assert report is not None
    assert len(report.promoted) == 1
    assert report.promoted[0].ratio is None
    rendered = "\n".join(probe.render_injury_report_text(report))
    assert "字号比值 —" in rendered


def test_promotion_channel_reports_unpromoted_candidates() -> None:
    """未提升候选面（验收第 1 轮 ③ FAIL 修复）：text/formula 短行段入矩阵——
    形态四字段照常输出 + 「未提升」归因标注；超长/多行段与原生标题不入。"""
    document = _document([
        _heading("0500", "第一章 总则", 1, 1),
        _para("0501", "关于本书", 2, raw_type="formula"),  # 候选：4 字单行
        _para("0502", "这是一段很长的正文文字。" * 6, 3),  # 60+ 字：非候选
        _para("0503", "第一行\n第二行", 4),  # 多行：非候选
    ])
    output = _make_output(
        document, outline=[OutlineEntry(title="第一章 总则", level=0, page=0)]
    )
    report = probe.build_injury_report(output)

    assert report is not None
    assert [(row.title, row.promoted) for row in report.promoted] == [
        ("关于本书", False)
    ]
    row = report.promoted[0]
    assert row.chars == 4 and row.lines == 1 and row.ending is None

    rendered = "\n".join(probe.render_injury_report_text(report))
    assert (
        "提升行形态      : Fix 2 提升通道已提升 0 条 · 未提升候选 1 条"
        "（管线留痕 promoted=0）" in rendered
    )
    assert "「关于本书」 4 字 · 1 行 · 句末：无" in rendered
    assert "· 未提升（字号未命中标题簇或句末标点拦截）" in rendered
    assert "这是一段很长的正文文字" not in rendered
    assert "第一行" not in rendered


def test_promotion_candidate_ratio_resampled_by_payload() -> None:
    """候选行字号比值按**段内文本**重采样（已提升行按标题采样；注入缝验证）。"""
    document = _document([_para("0510", "关于本书", 2, raw_type="formula")])
    output = _make_output(document, outline=None)
    report = probe.build_injury_report(
        output, font_sampler=_FakeSampler({"关于本书": 18.0})
    )

    assert report is not None
    assert report.promoted[0].promoted is False
    assert report.promoted[0].ratio == pytest.approx(18.0 / 16.0)


def test_promotion_fingerprint_gap_warning_when_pipeline_loses_rows() -> None:
    """指纹缺口自诊断（命中面）：管线留痕 promoted=8 > 树内指纹 0 → 显著缺口行。

    复现验收第 1 轮 ③ 的失败形态（输入被替换后提升通道空转/留痕与树面矛盾）——
    伤情单必须把矛盾显式说出来，而不是静默渲染 0 条。
    """
    document = _document([_heading("0520", "第一章 总则", 1, 1)])
    output = _make_output(
        document,
        outline=[OutlineEntry(title="第一章 总则", level=0, page=0)],
        promoted_pipeline=8,
    )
    report = probe.build_injury_report(output)

    assert report is not None
    assert report.promoted_pipeline == 8
    assert report.promotion_gap_warning is not None

    rendered = "\n".join(probe.render_injury_report_text(report))
    assert "（管线留痕 promoted=8）" in rendered
    assert (
        "指纹缺口：管线留痕 promoted=8 条，"
        "树内 raw_type=text/formula 的 heading 仅 0 条" in rendered
    )
    assert "工单 B 复核面" in rendered


def test_promotion_no_gap_warning_when_counts_agree() -> None:
    """不命中面：留痕与指纹一致（0/0 与 1/1 两形态）→ 无缺口行。"""
    report = probe.build_injury_report(_clean_output())
    assert report is not None
    assert report.promoted_pipeline == 0
    assert report.promotion_gap_warning is None
    rendered = "\n".join(probe.render_injury_report_text(report))
    assert "指纹缺口" not in rendered

    document = _document([
        _heading("0530", "第一章", 1, 1),
        _heading("0531", "关于本书", 2, 2, raw_type="text"),
    ])
    output = _make_output(
        document, outline=[OutlineEntry(title="第一章", level=0, page=0)],
        promoted_pipeline=1,
    )
    report_agree = probe.build_injury_report(output)
    assert report_agree is not None
    assert report_agree.promotion_gap_warning is None
    rendered_agree = "\n".join(probe.render_injury_report_text(report_agree))
    assert "提升通道已提升 1 条" in rendered_agree
    assert "指纹缺口" not in rendered_agree


# ---------------------------------------------------------------------------
# ④ 空 text 候选（formula 通道活跃度）
# ---------------------------------------------------------------------------


def test_formula_candidates_counted_with_heading_share() -> None:
    """命中面：formula 节点计数 + 标题占比 + 空载荷 + 样例（Fix 1 通道活跃度）。"""
    document = _document([
        _heading("0400", "第一章 总则", 1, 1),
        _para("0401", "E = mc^2", 2, raw_type="formula"),
        _heading("0402", "本书读者对象", 2, 3, raw_type="formula"),
        _para("0403", None, 4, raw_type="formula"),  # 空载荷（text 与 title 皆空）
    ])
    output = _make_output(document, outline=[OutlineEntry(title="第一章 总则", level=0, page=0)])
    report = probe.build_injury_report(output)

    assert report is not None
    assert report.formula_total == 3
    assert report.formula_headings == 1
    assert report.formula_empty == 1
    # 样例标题类（误判嫌疑面）优先，正文样例殿后
    assert report.formula_samples[0] == "本书读者对象"
    assert "E = mc^2" in report.formula_samples

    rendered = "\n".join(probe.render_injury_report_text(report))
    assert "空 text 候选    : formula 3 条（标题 1 · 正文 1 · 空载荷 1）" in rendered
    assert "标题占比 33%" in rendered
    assert "样例: 本书读者对象" in rendered


def test_formula_zero_when_absent() -> None:
    """不命中面：零 formula 节点 → 0 条（通道静默也是事实）。"""
    report = probe.build_injury_report(_clean_output())
    assert report is not None
    assert report.formula_total == 0
    rendered = "\n".join(probe.render_injury_report_text(report))
    assert "空 text 候选    : formula 0 条（标题 0 · 正文 0 · 空载荷 0）" in rendered
    assert "样例: —" in rendered


# ---------------------------------------------------------------------------
# ⑤ 无书签显著警示（伤情单第一行）
# ---------------------------------------------------------------------------


def test_no_bookmark_warning_first_line_for_sequence_clean() -> None:
    """correction_path=sequence-clean → 第一行显著警告「版面推断主导」。"""
    output = _make_output(_clean_document(), correction_path="sequence-clean",
                          outline=_clean_outline())
    report = probe.build_injury_report(output)
    assert report is not None
    assert report.no_bookmark_warning is not None

    lines = probe.render_injury_report_text(report)
    assert lines[1] == (
        "⚠️ 无书签锚定：版面推断主导，层级可信度降级（correction_path=sequence-clean）"
    )


def test_no_bookmark_warning_for_raw_path() -> None:
    """correction_path=raw（零 heading 可校正）同样触发显著警告。"""
    output = _make_output(_clean_document(), correction_path="raw", outline=_clean_outline())
    report = probe.build_injury_report(output)
    assert report is not None
    assert report.no_bookmark_warning is not None
    assert "correction_path=raw" in report.no_bookmark_warning


def test_no_bookmark_warning_absent_for_bookmark_path() -> None:
    """不命中面：bookmark 主导 → 无警告；伤情单头部后直接进指标行。"""
    report = probe.build_injury_report(_clean_output())
    assert report is not None
    assert report.no_bookmark_warning is None
    lines = probe.render_injury_report_text(report)
    assert "⚠️" not in "\n".join(lines)
    assert lines[1] == ""  # 头部与首指标行之间的常规空行


# ---------------------------------------------------------------------------
# 渲染面：text / html 同源、节序在 ③ 自检之后、句末正则奇偶锁
# ---------------------------------------------------------------------------


def test_injury_renders_after_structure_check_in_text() -> None:
    output = _make_output(_clean_document(), correction_path="sequence-clean",
                          outline=_clean_outline())
    text = probe.render_text(output)
    assert text.index("③ 标题结构自检") < text.index("③.b 小节级伤情单")
    assert "版面推断主导，层级可信度降级" in text


def test_injury_renders_in_html_view() -> None:
    outline = [*_clean_outline(), OutlineEntry(title="修改记录", level=0, page=321)]
    output = _make_output(_clean_document(), outline=outline)
    payload = probe.render_html(output)
    assert "③.b 小节级伤情单" in payload
    assert "书签召回缺失" in payload
    assert "「修改记录」" in payload


def test_sentence_ending_superset_locked_to_promote_regex() -> None:
    """奇偶锁：伤情单的「脱漏」判据必须与 docling_adapter._PROMOTE_SENT_END 同源——
    提升正则面内的字符不得被判脱漏；超集字符（脱漏面）= 正则面 ∪ 中英补集。"""
    import re as _re

    from app.rag.normalizers.docling_adapter import _PROMOTE_SENT_END

    pattern_chars = set(_re.match(r"\[(.+)\]\$", _PROMOTE_SENT_END.pattern).group(1))
    assert pattern_chars == set(probe._PROMOTE_SENT_END_CHARS)  # 正则面对齐（防漂移）
    assert set(probe._SENTENCE_ENDING_ALL) > pattern_chars  # 超集才可能暴露缺口
    assert set("！!？?…．.") & set(probe._SENTENCE_ENDING_ALL) == set("！!？?…．.")
