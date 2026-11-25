"""探针标题结构自检节的渲染单测（合成 PipelineOutput，零真实 PDF）。

验收口径（工单 B 段）：自检节是**只读验收视图**——只从归一化产物（树 + parse_meta）
与 PDF 书签重算指标，不回写树；text 与 html 两视图都渲染同一指标块；非 PDF 通道
（docx/md）显式标注「不适用」。
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any

from app.rag.chunker import ChunkingConfig, ChunkingResult
from app.rag.ir_schema import IrNode, IrNodeType
from app.rag.normalizers.heading_outline import OutlineEntry

_REPO_ROOT = Path(__file__).resolve().parents[4]


def _load_probe() -> Any:
    """按路径装载探针脚本（scripts/ 非包；模块级只做 import，无副作用）。"""
    path = _REPO_ROOT / "scripts" / "probe_kb_pipeline.py"
    spec = importlib.util.spec_from_file_location("probe_kb_pipeline_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass 解析 __module__ 需已注册（exec 前）
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


def _heading(block_id: str, title: str, level: int, page: int | None = None,
             children: list[IrNode] | None = None) -> IrNode:
    node = IrNode(
        id=f"loc:blk:{block_id}",
        type=IrNodeType.HEADING,
        title=title,
        level=level,
        meta={"raw_type": "section_header"},
        children=children or [],
    )
    node.loc = {"origin": "local", "page": page, "char_range": None, "file_object_key": None}
    return node


def _document() -> IrNode:
    """两章 + 各一节的已校正树（章 level=1 深度 1；节 level=2 深度 2）。"""
    return IrNode(
        id="loc:doc:0",
        type=IrNodeType.DOCUMENT,
        doc_kind="upload",
        meta={"raw_type": "docling.document"},
        children=[
            _heading(
                "0001",
                "第一章 块级绑定",
                1,
                10,
                [_heading("0002", "块级声明", 2, 11)],
            ),
            _heading(
                "0003",
                "第二章 字符串",
                1,
                22,
                [_heading("0004", "更好的 Unicode 支持", 3, 23)],
            ),
            _heading("0005", "修改为：", 2, 24),
            _heading("0006", "修改为：", 2, 25),
        ],
    )


def _output(*, ext: str = "pdf", outline: list[OutlineEntry] | None = None) -> Any:
    return probe.PipelineOutput(
        source_label="单元测试样本",
        ext=ext,
        byte_size=1024,
        document=_document(),
        parse_meta={
            "pages": 2,
            "tables": 0,
            "code_blocks": 0,
            "parser": "docling",
            "heading_outline": {
                "correction_path": "bookmark",
                "bookmarks": 3,
                "anchors": 2,
                "unmatched": ["修改记录"],
                "unmatched_count": 1,
                "pressed": 2,
                "pressed_sample": ["章乙", "章丙"],
                "level_jumps_before": 3,
                "level_jumps_after": 1,
            },
        },
        chunking=ChunkingResult(
            document_id=None,
            chunks=[],
            document_text="",
            document_content_hash="",
            skipped_units=0,
            oversized_units=0,
        ),
        config=ChunkingConfig.from_spec({}),
        outline=outline,
    )


_OUTLINE = [
    OutlineEntry(title="第一章 块级绑定", level=0, page=9),
    OutlineEntry(title="第二章 字符串", level=0, page=21),
    OutlineEntry(title="修改记录", level=0, page=321),
]


def test_structure_check_counts_metrics_from_tree() -> None:
    check = probe.build_structure_check(_output(outline=_OUTLINE))

    assert check.applicable is True
    assert check.correction_path == "bookmark"
    assert check.bookmarks == 3
    assert check.all_top_level is True
    assert check.anchor_hits == 2
    assert check.unmatched == ["修改记录"]
    assert check.max_anchor_depth == 1
    # 全顶级书签 → 每个锚点期望树深 = outline_level − top_level + 1 = 1
    assert check.anchor_depth_mismatch == 0
    assert check.mis_mounted == 0
    assert check.anchor_level_jumps == 0
    # 章(1) → 节(3) 留一格空档 = 1 处跳级（两层都是非锚点链路）；「修改为：」×2 一组
    assert check.level_jumps == 1
    assert check.non_anchor_jumps == 1
    assert check.pressed == 2  # 管线留痕透传
    assert check.level_jumps_before == 3
    assert check.duplicate_groups == 1
    assert check.duplicate_extra == 1
    assert check.duplicate_top[0] == ("修改为：", 2)


def test_structure_check_top_level_judgement_is_level_offset_agnostic() -> None:
    """书签层级 1-based（非本机标本形态）→ 「全顶级」判定按最小层级归一，不误判。"""
    outline = [
        OutlineEntry(title="第一章 块级绑定", level=1, page=9),
        OutlineEntry(title="第二章 字符串", level=1, page=21),
        OutlineEntry(title="修改记录", level=1, page=321),
    ]
    check = probe.build_structure_check(_output(outline=outline))
    assert check.all_top_level is True
    assert check.anchor_hits == 2
    assert check.max_anchor_depth == 1
    assert check.anchor_depth_mismatch == 0


def test_structure_check_detects_mis_mounted_anchor() -> None:
    """章级挂错判定：同级锚点标题挂在另一个锚点子树内 → 计数 1；树深亦偏差 1。"""
    doc = _document()
    nested = IrNode(
        id="loc:doc:1",
        type=IrNodeType.DOCUMENT,
        doc_kind="upload",
        meta={"raw_type": "docling.document"},
        children=[
            _heading(
                "0001",
                "第一章 块级绑定",
                1,
                10,
                [_heading("0002", "第二章 字符串", 2, 22)],
            )
        ],
    )
    doc.children = nested.children
    output = _output(outline=_OUTLINE)
    output.document = doc

    check = probe.build_structure_check(output)
    assert check.mis_mounted == 1
    assert check.max_anchor_depth == 2
    assert check.anchor_depth_mismatch == 1  # 第二章期望深 1，实际 2


def test_structure_check_multi_level_bookmarks_pass() -> None:
    """多级书签正确树：子锚点挂在父锚点下 → 挂错 0、树深偏差 0。"""
    doc = IrNode(
        id="loc:doc:2",
        type=IrNodeType.DOCUMENT,
        doc_kind="upload",
        meta={"raw_type": "docling.document"},
        children=[
            _heading(
                "0101",
                "第一章 块级绑定",
                1,
                10,
                [
                    _heading("0102", "1.1 块级声明", 2, 11),
                    _heading("0103", "1.2 暂时性死区", 2, 12),
                ],
            ),
            _heading("0104", "第二章 字符串", 1, 22),
        ],
    )
    output = _output(outline=[
        OutlineEntry(title="第一章 块级绑定", level=0, page=9),
        OutlineEntry(title="1.1 块级声明", level=1, page=10),
        OutlineEntry(title="1.2 暂时性死区", level=1, page=11),
        OutlineEntry(title="第二章 字符串", level=0, page=21),
    ])
    output.document = doc

    check = probe.build_structure_check(output)
    assert check.anchor_hits == 4
    assert check.mis_mounted == 0
    assert check.anchor_depth_mismatch == 0
    assert check.max_anchor_depth == 2
    assert len(check.unmatched) == 0  # 书签面与树面全对齐（4/4）


def test_structure_check_multi_level_outline_detects_deep_mis_mount() -> None:
    """多级书签错父：L0 章被塞进 L1 节子树 → 最近祖先锚点 outline_level(1) ≥ 0 判挂错。

    对照：L1 节挂在 L0 章下是合法嵌套（上一条用例），不误判。
    """
    doc = IrNode(
        id="loc:doc:3",
        type=IrNodeType.DOCUMENT,
        doc_kind="upload",
        meta={"raw_type": "docling.document"},
        children=[
            _heading(
                "0201",
                "第一章 块级绑定",
                1,
                10,
                [
                    _heading(
                        "0202",
                        "1.1 块级声明",
                        2,
                        11,
                        [_heading("0203", "第二章 字符串", 3, 22)],
                    )
                ],
            )
        ],
    )
    output = _output(outline=[
        OutlineEntry(title="第一章 块级绑定", level=0, page=9),
        OutlineEntry(title="1.1 块级声明", level=1, page=10),
        OutlineEntry(title="第二章 字符串", level=0, page=21),
    ])
    output.document = doc

    check = probe.build_structure_check(output)
    assert check.mis_mounted == 1  # 第二章（L0）最近祖先锚点 1.1 是 L1 → 错父
    assert check.anchor_depth_mismatch == 1  # 第二章期望深 1，实际深 3
    assert check.anchor_hits == 3


def test_structure_check_renders_in_text_view() -> None:
    check = probe.build_structure_check(_output(outline=_OUTLINE))
    rendered = "\n".join(probe.render_structure_check_text(check))

    assert "标题结构自检" in rendered
    assert "correction_path=bookmark" in rendered
    assert "锚点匹配      : 2 / 3 书签命中；未命中：修改记录" in rendered
    assert "层级跳级      : 1 处（清洗前 3 处；锚点间跳级 0 处；非锚点跳级 1 处）" in rendered
    assert "章级挂错      : 0 处（最近祖先锚点层级 ≥ 本锚点）" in rendered
    assert "锚点树深      : 偏差 0 处（期望=outline_level−顶层+1；实测最大 1）" in rendered
    assert "被压深        : 2 条（管线留痕）；前 5: 章乙、章丙" in rendered


def test_structure_check_renders_pressed_sample_preview() -> None:
    """被压深前 5 条预览：55 条里若混入真顶级条目，计数看不出来——得能肉眼过一眼。"""
    check = probe.build_structure_check(_output(outline=_OUTLINE))
    assert check.pressed_titles == ["章乙", "章丙"]


def test_structure_check_renders_in_html_view() -> None:
    payload = probe.render_html(_output(outline=_OUTLINE))

    assert "标题结构自检" in payload
    assert "correction_path=bookmark" in payload
    assert "锚点匹配      : 2 / 3 书签命中；未命中：修改记录" in payload
    assert "被压深        : 2 条（管线留痕）" in payload


def test_structure_check_disabled_correction_renders_not_enabled() -> None:
    """关闭态（无 heading_outline 键）→ 渲染「未启用」、被压深显示 —（不冒充数据）。"""
    output = _output(outline=_OUTLINE)
    output.parse_meta = {
        key: value
        for key, value in output.parse_meta.items()
        if key != "heading_outline"
    }
    check = probe.build_structure_check(output)
    assert check.correction_path is None
    assert check.pressed is None
    assert check.pressed_titles == []
    rendered = "\n".join(probe.render_structure_check_text(check))
    assert "correction_path=未启用" in rendered
    assert "被压深        : —（管线留痕）；前 5: —" in rendered
    # 树权重算面不受闭态影响（自检仍能报出结构事实）
    assert "章级挂错      : 0 处" in rendered


def test_structure_check_not_applicable_for_non_pdf() -> None:
    """docx/md 等非 PDF 通道 → 显式「不适用」（该节不臆造书签面）。"""
    check = probe.build_structure_check(_output(ext="docx", outline=None))
    assert check.applicable is False
    text = "\n".join(probe.render_structure_check_text(check))
    assert "        : 不适用（非 PDF 通道）" in text
    html_payload = probe.render_html(_output(ext="docx", outline=None))
    assert "不适用（非 PDF 通道）" in html_payload


def test_structure_check_pdf_without_outline_notes_degradation() -> None:
    """PDF 但书签读不到 → 不适用 + 降级说明 + 管线留痕的 correction_path/跳级前后。"""
    check = probe.build_structure_check(_output(outline=None))
    assert check.applicable is False
    text = "\n".join(probe.render_structure_check_text(check))
    assert "        : 书签不可读或为空（PDF 无书签 / 解析失败 / pdfium 不可用）" in text
    assert "correction_path=bookmark" in text
    assert "清洗前跳级 3 处 / 清洗后 1 处" in text


def test_render_text_includes_self_check_section() -> None:
    """整份 text 渲染含自检节（探针实跑输出即可验收）。"""
    payload = probe.render_text(_output(outline=_OUTLINE))
    assert "--- ③ 标题结构自检" in payload
    assert "correction_path=bookmark" in payload


def test_render_text_appends_no_injury_section_for_non_pdf() -> None:
    """伤情单 v2 零变化门禁（非 PDF 面）：docx 渲染恰止于 v1 自检块、零新增行。"""
    output = _output(ext="docx", outline=None)
    text = probe.render_text(output)
    v1_block = "\n".join(probe.render_structure_check_text(probe.build_structure_check(output)))
    assert text.endswith(v1_block)
    for marker in ("伤情单", "书签召回缺失", "标题密度", "提升行形态", "空 text 候选"):
        assert marker not in text


def test_render_text_appends_no_injury_section_when_correction_disabled() -> None:
    """伤情单 v2 零变化门禁（关闭态面）：heading_outline 键整缺时渲染零新增行。"""
    output = _output(outline=_OUTLINE)
    output.parse_meta = {
        key: value
        for key, value in output.parse_meta.items()
        if key != "heading_outline"
    }
    text = probe.render_text(output)
    v1_block = "\n".join(probe.render_structure_check_text(probe.build_structure_check(output)))
    assert text.endswith(v1_block)
    for marker in ("伤情单", "书签召回缺失", "标题密度", "提升行形态", "空 text 候选"):
        assert marker not in text
