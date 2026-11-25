"""工单 B：docling 输出多信号复核层单元测试（heading_review 模块；全假体零模型）。

被测面（plan-pdf-root-cause-remedies-20261124.md 工单 B）：
① section_header 门：非书签锚点 heading 须满足（字号 ≥ 正文众数×1.25 或 加粗
   [FPDFText_GetFontWeight≥700] 或 独立短行+上下留白），不满足者降级回正文段；
   只降级不升级；书签锚点豁免；
② formula 复核：bbox 内字号（字符盒高中位数口径）=正文众数 ±5% 且行宽近满行
   → label 降为 text（文本段，走 orig/提升通道）；
③ code 复核：bbox 内字号=正文众数 ±5% 且字体名可得且无一命中等宽名册 → 降级文本段；
④ 负例锁死：真标题（字号/加粗/独立短行三形态）与真代码/真公式零误降。

标定锚点（.zcode/tmp/calibrate_ecma_*.py，2026-11-24 新渲染版 ECMA 实测）：
- FPDFText_GetFontSize 恒 1.0、GetFontWeight 恒 0、GetFontName 不存在——「字号」
  主信号改用 **bbox 内字符盒高中位数**（渲染器无关、与字号线性同构），基线=
  全文非空白字符盒高众数；阈值仍为相对众数比值（绝对 pt 不可移植纪律）。
- 「修改为：」×5 盒高≈0.95×正文、留白 15~17pt 与真标题（≥1.25× 通过字号门）不
  冲突；「码：」等独立短行留白形态靠句末标点否决兜住（共用提升通道正则）。
- 旧基线（旧标本）「关于本书」= heading raw_type=formula（Fix 2 提升指纹）——
  提升通道的高度回落（GetFontSize 死亡检测）在 adapter 测试面覆盖。
"""

from dataclasses import dataclass
from typing import Any

from app.rag.normalizers.heading_review import (
    MONO_FONT_TOKENS,
    PageGeometry,
    review_blocks,
    size_stat_for,
)


# ---------------------------------------------------------------------------
# 假体：复核层全表面采样器 + 几何（按 bbox 高度键控，同 _BboxFontSampler 风格）
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Block:
    """_PendingBlock 同形假体（review_blocks 鸭子类型协议面）。"""

    kind: str
    label: str = ""
    text: str | None = None
    title: str | None = None
    page: int | None = 1
    bbox: tuple[float, float, float, float] | None = None


class _ReviewFontSampler:
    """复核层假体：字号（死亡口径可设）/盒高/加粗/字体名按 bbox 高度键控。"""

    def __init__(
        self,
        *,
        sizes_by_height: dict[float, tuple[float, bool]] | None = None,
        heights_by_height: dict[float, float] | None = None,
        names_by_height: dict[float, list[str]] | None = None,
        body_font_size: float = 1.0,
        body_height: float = 10.0,
        alive: bool = False,
    ) -> None:
        self._sizes = sizes_by_height or {}
        self._heights = heights_by_height or {}
        self._names = names_by_height or {}
        self.body_font_size = body_font_size
        self.heights_mode = body_height
        self.font_size_alive = alive

    def stats_in_bbox(
        self, page_index: int, bbox: tuple[float, float, float, float]
    ) -> tuple[float, bool] | None:
        return self._sizes.get(round(bbox[3] - bbox[1], 1))

    def heights_in_bbox(
        self, page_index: int, bbox: tuple[float, float, float, float]
    ) -> float | None:
        return self._heights.get(round(bbox[3] - bbox[1], 1))

    def font_names_in_bbox(
        self, page_index: int, bbox: tuple[float, float, float, float]
    ) -> list[str] | None:
        return self._names.get(round(bbox[3] - bbox[1], 1))


class _FakeGeometry:
    """页几何假体：行距/行宽基准恒定，留白按 bbox 高度键控。"""

    def __init__(
        self,
        *,
        pitch: float = 7.0,
        full_width: float = 400.0,
        gaps_by_height: dict[float, tuple[float, float]] | None = None,
    ) -> None:
        self._pitch = pitch
        self._full = full_width
        self._gaps = gaps_by_height or {}

    def available(self) -> bool:
        return True

    def pitch(self, page_index: int) -> float | None:
        return self._pitch

    def full_width(self, page_index: int) -> float | None:
        return self._full

    def gaps(self, page_index: int, bbox: tuple[float, float, float, float]) -> tuple[float, float] | None:
        return self._gaps.get(round(bbox[3] - bbox[1], 1))


def _header(height: float = 14.0, text: str = "修改为：", page: int = 1) -> _Block:
    return _Block(kind="heading", label="section_header", title=text, page=page,
                  bbox=(62.0, 100.0, 62.0 + 40.0, 100.0 + height))


# ---------------------------------------------------------------------------
# ① section_header 门
# ---------------------------------------------------------------------------


def test_header_gate_demotes_body_size_heading() -> None:
    """① 正门：正文字号、非加粗、宽行 → 降级回正文段（title 带回 text）。"""
    sampler = _ReviewFontSampler(
        sizes_by_height={14.0: (1.0, False)},
        heights_by_height={14.0: 9.5},
        body_height=10.0,
        alive=False,
    )
    blocks = [_header(height=14.0, text="且使用该新目标作为 this 去执行函数体。"),
              _Block(kind="paragraph", label="text", text="正文段落。")]
    counts = review_blocks(blocks, sampler=sampler, geometry=_FakeGeometry())
    demoted = blocks[0]
    assert demoted.kind == "paragraph"
    assert demoted.title is None
    assert demoted.text == "且使用该新目标作为 this 去执行函数体。"
    assert demoted.label == "section_header"  # 留痕：docling 原始标签不篡改
    assert counts["demoted_header"] == 1


def test_header_gate_keeps_large_heading() -> None:
    """① 负例：盒高 ≥1.25× 正文众数（真标题字号形态）→ 不降。"""
    sampler = _ReviewFontSampler(heights_by_height={14.0: 14.2}, body_height=10.0, alive=False)
    blocks = [_header(height=14.0, text="浏览器与 Node.js 的兼容性")]
    counts = review_blocks(blocks, sampler=sampler, geometry=_FakeGeometry())
    assert blocks[0].kind == "heading"
    assert counts["demoted_header"] == 0


def test_header_gate_keeps_h3_section_heading_near_boundary() -> None:
    """① 负例（阈值分界回归锁）：真节标题 (a) 带下界比值 1.053 → 不降。

    ECMA 全量门决策审计（.zcode/tmp/audit_gate_decisions.py，2026-11-24，全 372
    heading 比值全分布）：假阳性上界 1.034（「修改为：」族/章首目录条/勘误引用行）、
    真标题 (a) 带下界 1.053（Array.prototype.includes() 方法——混合脚本行中位数被
    拉低后的实测值）。阈值定标轨迹 1.25→1.15→1.05：1.25/1.15 均实测误降真节标题
    （更好的 Unicode 支持 1.222 / 规范化的 __proto__ 属性 1.128 / 1.053 条），本用例
    卡在实测分界两侧。
    """
    sampler = _ReviewFontSampler(heights_by_height={14.0: 10.53, 15.0: 12.2}, body_height=10.0, alive=False)
    blocks = [
        _header(height=14.0, text="Array.prototype.includes() 方法"),  # 1.053
        _header(height=15.0, text="更好的 Unicode 支持"),  # 1.222
        _header(height=15.0, text="规范化的 __proto__ 属性"),  # 1.222 带内
    ]
    counts = review_blocks(blocks, sampler=sampler, geometry=_FakeGeometry())
    assert all(block.kind == "heading" for block in blocks)
    assert counts["demoted_header"] == 0


def test_header_gate_keeps_bold_heading() -> None:
    """① 负例：正文字号但加粗（FPDFText_GetFontWeight≥700 形态）→ 不降。

    题尾带句末标点使 (b) 可观察——无标点标题在句末标点判据下本来保留。
    """
    sampler = _ReviewFontSampler(
        sizes_by_height={14.0: (1.0, True)},  # 死亡字号 + 加粗
        heights_by_height={14.0: 9.5},
        body_height=10.0,
        alive=False,
    )
    blocks = [_header(height=14.0, text="加粗标题行：")]
    counts = review_blocks(blocks, sampler=sampler, geometry=_FakeGeometry())
    assert blocks[0].kind == "heading"
    assert counts["demoted_header"] == 0


def test_header_gate_keeps_short_line_with_margins() -> None:
    """① 负例：正文字号、非加粗，但独立短行+上下留白 → 不降。"""
    sampler = _ReviewFontSampler(heights_by_height={14.0: 9.5}, body_height=10.0, alive=False)
    geometry = _FakeGeometry(pitch=7.0, full_width=400.0, gaps_by_height={14.0: (26.0, 24.0)})
    blocks = [_header(height=14.0, text="循环中的块级绑定")]
    counts = review_blocks(blocks, sampler=sampler, geometry=geometry)
    assert blocks[0].kind == "heading"
    assert counts["demoted_header"] == 0


def test_header_gate_spares_bookmark_anchor() -> None:
    """① 锚点豁免：书签锚点 heading 即使句末标点矛盾也不降（书签=确定性层级源）。

    锚题带句末标点（矛盾信号在身）才能让豁免可观察——非标点锚题在句末标点判据
    下本来就保留，豁免不可分辨。
    """
    sampler = _ReviewFontSampler(heights_by_height={14.0: 9.5}, body_height=10.0, alive=False)
    geometry = _FakeGeometry(pitch=7.0, full_width=400.0, gaps_by_height={14.0: (6.0, 6.0)})
    blocks = [_header(height=14.0, text="修改记录：")]
    counts = review_blocks(blocks, sampler=sampler, geometry=geometry, anchors={0})
    assert blocks[0].kind == "heading"
    assert counts["demoted_header"] == 0


def test_header_gate_large_punct_title_kept_via_size() -> None:
    """① (a) 门优先于句末标点：比值带内（1.05~1.25）的带标点大字行 → 保留。

    句末标点只在 (a)(b) 均无证据时判矛盾；字号正向证据优先（阈值变异 1.05→1.25
    时本用例红——(a) 失守后标点判据接管降级）。
    """
    sampler = _ReviewFontSampler(heights_by_height={14.0: 11.0}, body_height=10.0, alive=False)
    blocks = [_header(height=14.0, text="修改为：")]
    counts = review_blocks(blocks, sampler=sampler, geometry=_FakeGeometry())
    assert blocks[0].kind == "heading"
    assert counts["demoted_header"] == 0


def test_header_gate_demotes_sentence_ended_even_with_margins() -> None:
    """① 句末标点矛盾：句末标点结尾的 (a)(b) 无证据 heading → 降级。

    ECMA 全量实证（验收员③取证）：38 条假标题（修改为：族/引导句族）全部句末带
    标点、18 条真节标题全部无句末标点——句末标点是完全分隔信号（几何留白与假阳性
    分布重叠 15~17pt vs 真标题 18.8+ 部分交叠，无法两全，原「独立短行+上下留白」
    由本判据替代，与提升通道「句末标点=正文句式」同源）。
    """
    sampler = _ReviewFontSampler(heights_by_height={14.0: 9.5}, body_height=10.0, alive=False)
    geometry = _FakeGeometry(pitch=7.0, full_width=400.0, gaps_by_height={14.0: (29.6, 25.7)})
    blocks = [
        _header(height=14.0, text="修改为："),
        _header(height=14.0, text="Set the flag."),
        _header(height=14.0, text="这里有个示例："),
    ]
    counts = review_blocks(blocks, sampler=sampler, geometry=geometry)
    assert all(block.kind == "paragraph" for block in blocks)
    assert counts["demoted_header"] == 3


def test_header_gate_keeps_non_punct_body_size_heading() -> None:
    """① 负例（验收员③修复面）：无句末标点的正文比值 heading → 保留。

    18 条真实章节节标题（使用 const 声明对象/默认值/创建 Weak Set 等）盒高比值
    0.9~1.0 且留白不足，几何信号无法救——无句末标点=无矛盾证据 → 尊重版面模型
    标签保留。删本判据（句末标点保留支）本用例红。
    """
    sampler = _ReviewFontSampler(heights_by_height={14.0: 10.34, 15.0: 9.15}, body_height=10.0, alive=False)
    geometry = _FakeGeometry(pitch=7.0, full_width=400.0, gaps_by_height={14.0: (7.3, 7.8), 15.0: (7.3, 7.8)})
    blocks = [
        _header(height=14.0, text="块级声明"),
        _header(height=15.0, text="使用 const 声明对象"),
    ]
    counts = review_blocks(blocks, sampler=sampler, geometry=geometry)
    assert all(block.kind == "heading" for block in blocks)
    assert counts["demoted_header"] == 0


def test_header_gate_no_punct_variant_inherits_family_demotion() -> None:
    """① 家族继承：同名剥离句末标点后，带标点成员被判假 → 无标点变体继承降级。

    ECMA p325 实测「修改为」（无冒号变体，验收员① grep 模式命中）：与「修改为：」
    ×5 同为引导句排版变体——同名族内有句末标点成员即继承矛盾证据。删家族继承
    红本用例（无标点变体会被保留）。
    """
    sampler = _ReviewFontSampler(heights_by_height={14.0: 9.5, 16.0: 9.9}, body_height=10.0, alive=False)
    geometry = _FakeGeometry(pitch=7.0, full_width=400.0, gaps_by_height={16.0: (29.6, 25.7)})
    blocks = [
        _header(height=14.0, text="修改为："),
        _header(height=16.0, text="修改为"),  # 旧几何门下留白达标会被保留——家族继承后必须降
        _header(height=14.0, text="添加文字，改为："),
    ]
    counts = review_blocks(blocks, sampler=sampler, geometry=geometry)
    assert all(block.kind == "paragraph" for block in blocks)
    assert counts["demoted_header"] == 3


def test_header_gate_family_rule_spares_unrelated_real_title() -> None:
    """① 家族继承负例：无关真标题不因他族降级而被误伤。"""
    sampler = _ReviewFontSampler(heights_by_height={14.0: 9.5}, body_height=10.0, alive=False)
    blocks = [
        _header(height=14.0, text="修改为："),
        _header(height=14.0, text="默认值"),
    ]
    counts = review_blocks(blocks, sampler=sampler, geometry=_FakeGeometry())
    assert blocks[0].kind == "paragraph"
    assert blocks[1].kind == "heading"
    assert counts["demoted_header"] == 1


def test_header_gate_noop_without_font_surface() -> None:
    """零数据零降级：采样器无字号/盒高表面（旧版假体形态）→ 门整体空转。"""

    class _BareSampler:
        body_font_size = None
        heights_mode = None
        font_size_alive = True

    blocks = [_header()]
    counts = review_blocks(blocks, sampler=_BareSampler(), geometry=None)
    assert blocks[0].kind == "heading"
    assert counts["checked"] == 0


# ---------------------------------------------------------------------------
# ② formula 复核
# ---------------------------------------------------------------------------


def test_formula_review_demotes_body_height_full_width() -> None:
    """② formula 候选盒高=正文众数 ±5% 且行宽近满行 → label 降为 text。"""
    sampler = _ReviewFontSampler(heights_by_height={11.0: 10.2}, body_height=10.0, alive=False)
    geometry = _FakeGeometry(pitch=7.0, full_width=400.0)
    blocks = [_Block(kind="paragraph", label="formula", text="E = mc^2 该行其实占满整行",
                     bbox=(62.0, 100.0, 62.0 + 370.0, 111.0))]
    counts = review_blocks(blocks, sampler=sampler, geometry=geometry)
    assert blocks[0].label == "text"
    assert blocks[0].kind == "paragraph"
    assert counts["demoted_formula"] == 1


def test_formula_review_spares_title_like_formula() -> None:
    """② 负例（「关于本书」形态）：标题样式的 formula（盒高 1.4×、窄行）不降。"""
    sampler = _ReviewFontSampler(heights_by_height={17.0: 14.2}, body_height=10.0, alive=False)
    geometry = _FakeGeometry(pitch=7.0, full_width=400.0)
    blocks = [_Block(kind="paragraph", label="formula", text="关于本书",
                     bbox=(62.0, 100.0, 62.0 + 66.0, 117.0))]
    counts = review_blocks(blocks, sampler=sampler, geometry=geometry)
    assert blocks[0].label == "formula"
    assert counts["demoted_formula"] == 0


def test_formula_review_spares_narrow_real_formula() -> None:
    """② 负例（真公式）：盒高=正文众数但行宽不满行（居中公式形态）→ 不降。"""
    sampler = _ReviewFontSampler(heights_by_height={11.0: 10.1}, body_height=10.0, alive=False)
    geometry = _FakeGeometry(pitch=7.0, full_width=400.0)
    blocks = [_Block(kind="paragraph", label="formula", text="f(x) = ax + b",
                     bbox=(200.0, 100.0, 240.0, 111.0))]
    counts = review_blocks(blocks, sampler=sampler, geometry=geometry)
    assert blocks[0].label == "formula"
    assert counts["demoted_formula"] == 0


# ---------------------------------------------------------------------------
# ③ code 复核
# ---------------------------------------------------------------------------


def test_code_review_demotes_body_height_non_mono() -> None:
    """③ code 候选盒高=正文众数且字体名可得、无一命中等宽名册 → 降级文本段。"""
    sampler = _ReviewFontSampler(
        heights_by_height={11.0: 9.9},
        names_by_height={11.0: ["AAAAAD+ArialMT"]},
        body_height=10.0,
        alive=False,
    )
    blocks = [_Block(kind="code", label="code", text="x = 1  # 伪代码正文",
                     bbox=(62.0, 100.0, 420.0, 111.0))]
    counts = review_blocks(blocks, sampler=sampler, geometry=_FakeGeometry(full_width=400.0))
    assert blocks[0].kind == "paragraph"
    assert blocks[0].label == "text"
    assert blocks[0].text == "x = 1  # 伪代码正文"
    assert counts["demoted_code"] == 1


def test_code_review_spares_monospace() -> None:
    """③ 负例（真代码）：等宽字体名命中 → 不降（名册大小写不敏感）。"""
    sampler = _ReviewFontSampler(
        heights_by_height={11.0: 9.8},
        names_by_height={11.0: ["AAAAAB+CourierNewPSMT"]},
        body_height=10.0,
        alive=False,
    )
    blocks = [_Block(kind="code", label="code", text="let x = 1;",
                     bbox=(62.0, 100.0, 420.0, 111.0))]
    counts = review_blocks(blocks, sampler=sampler, geometry=_FakeGeometry(full_width=400.0))
    assert blocks[0].kind == "code"
    assert counts["demoted_code"] == 0
    assert "mono" in MONO_FONT_TOKENS and "courier" in MONO_FONT_TOKENS


def test_code_review_spares_when_names_unavailable() -> None:
    """③ 负例：字体名不可得（GetFontInfo 全失败 → None）→ 无正证据不降。"""
    sampler = _ReviewFontSampler(heights_by_height={11.0: 9.8}, names_by_height={}, body_height=10.0)
    blocks = [_Block(kind="code", label="code", text="let x = 1;",
                     bbox=(62.0, 100.0, 420.0, 111.0))]
    counts = review_blocks(blocks, sampler=sampler, geometry=_FakeGeometry(full_width=400.0))
    assert blocks[0].kind == "code"
    assert counts["demoted_code"] == 0


# ---------------------------------------------------------------------------
# 信号解析：死亡字号回落盒高（与 Fix 2 提升通道共用）
# ---------------------------------------------------------------------------


def test_size_stat_falls_back_to_height_when_dead() -> None:
    """字号死亡（font_size_alive=False）→ size_stat_for 回落盒高中位数。"""
    sampler = _ReviewFontSampler(
        sizes_by_height={14.0: (1.0, True)},   # 死亡值 1.0 + 加粗
        heights_by_height={14.0: 14.2},
        alive=False,
    )
    stat = size_stat_for(sampler, 1, (0.0, 0.0, 10.0, 14.0))
    assert stat == (14.2, True)


def test_size_stat_uses_font_size_when_alive() -> None:
    """字号存活（健康 PDF）→ 维持 GetFontSize 口径（回归等价）。"""
    sampler = _ReviewFontSampler(
        sizes_by_height={14.0: (23.0, False)},
        heights_by_height={14.0: 16.0},
        alive=True,
    )
    stat = size_stat_for(sampler, 1, (0.0, 0.0, 10.0, 14.0))
    assert stat == (23.0, False)


# ---------------------------------------------------------------------------
# PageGeometry（行带/行距/行宽基准；真采样器 page_chars 面）
# ---------------------------------------------------------------------------


class _CharsSampler:
    """page_chars 假体：两行字符带（正文 + 短行），供 full_width/available 面。"""

    def __init__(self) -> None:
        self.body_font_size = None
        self.heights_mode = None
        self.font_size_alive = True

    def page_chars(self, page_index: int) -> list[tuple[Any, ...]]:
        chars: list[tuple[Any, ...]] = []
        if page_index != 0:
            return chars  # 页 1+ 为空页（full_width None 面）

        def _line(y: float, x0: float, x1: float, h: float, count: int = 10) -> None:
            step = (x1 - x0) / count
            for i in range(count):
                chars.append((x0 + i * step, y, x0 + (i + 1) * step, y + h, 12.0, "字", len(chars)))

        _line(100.0, 62.0, 530.0, 10.0)   # 正文行（宽 468）
        _line(136.0, 62.0, 140.0, 10.0)   # 短行
        return chars


def test_page_geometry_full_width_and_available() -> None:
    geometry = PageGeometry(_CharsSampler())
    assert geometry.available() is True
    assert geometry.full_width(0) == 468.0
    missing = PageGeometry(_ReviewFontSampler())
    assert missing.available() is False
    empty = PageGeometry(_CharsSampler())
    assert empty.full_width(99) is None  # 空页 → None


def test_review_blocks_counts_shapes() -> None:
    """计数面：checked 只计被门检的 heading；formula/code 计数独立。"""
    sampler = _ReviewFontSampler(heights_by_height={14.0: 9.5}, body_height=10.0, alive=False)
    blocks = [
        _header(height=14.0),
        _Block(kind="paragraph", label="text", text="正文"),
        _Block(kind="paragraph", label="formula", text="公式",
               bbox=(62.0, 100.0, 432.0, 111.0)),
        _Block(kind="table", label="table"),
    ]
    counts = review_blocks(blocks, sampler=sampler, geometry=_FakeGeometry(full_width=400.0))
    assert counts["checked"] == 1
    assert counts["demoted_header"] == 1
    assert counts["demoted_formula"] == 0  # formula 无盒高样本（heights 键 11.0 缺）→ 不降
