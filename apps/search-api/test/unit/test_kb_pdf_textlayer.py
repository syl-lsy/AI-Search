"""B17 主线一：pdf 文本层探测与字号采样单元测试（pypdfium2 轻绑定，真构造小 PDF）。

pypdfium2 非模型（docling 传递依赖的轻绑定）——「能构造的小 pdf 则构造」口径下
真跑：手写最小 PDF（程序化 xref 偏移），两页两字号（24pt 标题 / 12pt 正文）。
文本层缺失判定 = 页 char 数阈值（代码常量）；字号/加粗采样为 docling heading
启发式的数据源（探针 §2.2 短板消费）。
"""

import io
from typing import Any

import pytest

from app.rag.normalizers import pdf_textlayer
from app.rag.normalizers.pdf_textlayer import (
    OCR_TEXT_LAYER_MIN_CHARS,
    CharFontSampler,
    open_pdf_bytes,
    page_text,
    page_text_flags,
    render_page,
)


def pdf_textlayer_raw():
    '''pdfium raw 模块（加粗权重 monkeypatch 面；惰性取，避免测试导入期加载 pdfium）。'''
    return pdf_textlayer._pdfium_raw()


def _build_pdf(pages: list[list[tuple[float, str]]]) -> bytes:
    """程序化构造最小合法 PDF：每页若干 (字号, 文本) 行（Helvetica 单字体）。"""
    objects: dict[int, bytes] = {}
    page_ids: list[int] = []
    n_pages = len(pages)
    # 对象布局：1=Catalog 2=Pages 3..(3+n-1)=Page (3+n)..(3+2n-1)=Contents 3+2n=Font
    for i, lines in enumerate(pages):
        page_id = 3 + i
        content_id = 3 + n_pages + i
        page_ids.append(page_id)
        stream_parts = []
        y = 750.0
        for size, text in lines:
            escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            stream_parts.append(f"BT /F1 {size:.0f} Tf 72 {y:.0f} Td ({escaped}) Tj ET")
            y -= size * 1.6
        stream = "\n".join(stream_parts).encode("latin-1", "replace")
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {3 + 2 * n_pages} 0 R >> >> "
            f"/Contents {content_id} 0 R >>".encode()
        )
        objects[content_id] = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)
    objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode()
    objects[3 + 2 * n_pages] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for obj_id in sorted(objects):
        offsets[obj_id] = out.tell()
        out.write(f"{obj_id} 0 obj\n".encode())
        out.write(objects[obj_id])
        out.write(b"\nendobj\n")
    xref_pos = out.tell()
    max_id = max(objects)
    out.write(f"xref\n0 {max_id + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for obj_id in range(1, max_id + 1):
        out.write(f"{offsets[obj_id]:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {max_id + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF".encode()
    )
    return out.getvalue()


TWO_PAGE_PDF = _build_pdf(
    [
        [(24, "First Heading Line"), (12, "body text line one"), (12, "body text line two")],
        [(16, "Second Heading"), (12, "second page body")],
    ]
)


def test_open_and_page_count() -> None:
    pdf = open_pdf_bytes(TWO_PAGE_PDF)
    assert len(pdf) == 2


def test_text_flags_full_text_layer() -> None:
    pdf = open_pdf_bytes(TWO_PAGE_PDF)
    flags = page_text_flags(pdf)
    assert flags == [True, True]


def test_text_flags_threshold_boundary() -> None:
    """纯函数面：char 数 < 32 → False（稀疏/缺失页进 OCR 候选）。

    恢复 B17 现场标定阈值 32：稀疏页整页跳过会丢图像正文（评审实测「正文是图像 +
    文本层只有页码戳」形态），噪声改由合并环节按页文本层原文去重。
    """
    assert page_text_flags(None, char_counts=[0, 3, 50]) == [False, False, True]
    assert page_text_flags(None, char_counts=[31, 32]) == [False, True]
    assert OCR_TEXT_LAYER_MIN_CHARS == 32


def test_sparse_text_layer_page_remains_ocr_candidate() -> None:
    """稀疏文本层页（1/9/22 字符）仍进 OCR 候选——不因稀疏整页丢字。"""
    assert page_text_flags(None, char_counts=[0, 1, 9, 22, 31]) == [
        False,
        False,
        False,
        False,
        False,
    ]


def test_page_text_extracts_text_layer_raw() -> None:
    """`page_text` 抽取页文本层原文（OCR 去重比对面；逐页开合句柄）。"""
    pdf = open_pdf_bytes(TWO_PAGE_PDF)
    assert "First Heading Line" in page_text(pdf, 0)
    assert "second page body" in page_text(pdf, 1)


def build_red_rect_pdf() -> bytes:
    """单页最小 PDF：纯红实心矩形（无文本层）——OCR 接线缝通道序断言用。

    红（RGB 1 0 0）经 pdfium 渲染为 RGB 位图；PIL→BGR 归一后对应像素必须为
    `[0, 0, 255]`，以此锁死通道反转（白底黑字页 RGB≡BGR，无法区分通道序）。
    """
    content = b"1 0 0 rg 20 20 120 120 re f"
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R >>",
        4: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
    }
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for obj_id in sorted(objects):
        offsets[obj_id] = out.tell()
        out.write(f"{obj_id} 0 obj\n".encode())
        out.write(objects[obj_id])
        out.write(b"\nendobj\n")
    xref_pos = out.tell()
    max_id = max(objects)
    out.write(f"xref\n0 {max_id + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for obj_id in range(1, max_id + 1):
        out.write(f"{offsets[obj_id]:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {max_id + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF".encode()
    )
    return out.getvalue()


def test_render_page_returns_image() -> None:
    """渲染产物是 PIL.Image（RGB）——OCR 跨界由 paddle_engine 归一为 BGR ndarray，
    本层不产出 ndarray（P0 回归：PIL 直传引擎会被静默忽略产 0 行）。"""
    Image = pytest.importorskip("PIL.Image")
    pdf = open_pdf_bytes(TWO_PAGE_PDF)
    img = render_page(pdf, 0)
    assert img.size[0] > 0 and img.size[1] > 0
    assert isinstance(img, Image.Image)
    assert img.mode == "RGB"


def test_char_font_sampler_sizes_and_search() -> None:
    pdf = open_pdf_bytes(TWO_PAGE_PDF)
    sampler = CharFontSampler(pdf)
    body = sampler.page_char_sizes(0)
    assert body and max(body) == pytest.approx(24.0, abs=0.5)
    # 按文本定位字号：标题行 24pt，正文行 12pt
    heading_stats = sampler.text_font_stats(0, "First Heading Line")
    assert heading_stats is not None
    size, bold = heading_stats
    assert size == pytest.approx(24.0, abs=0.5)
    assert isinstance(bold, bool)
    body_stats = sampler.text_font_stats(0, "body text line one")
    assert body_stats is not None and body_stats[0] == pytest.approx(12.0, abs=0.5)
    assert sampler.text_font_stats(1, "不存在的文本") is None


def test_sampler_missing_text_returns_none() -> None:
    pdf = open_pdf_bytes(TWO_PAGE_PDF)
    sampler = CharFontSampler(pdf)
    assert sampler.text_font_stats(0, "") is None


def test_broken_pdf_raises_value_error_shape() -> None:
    """坏字节 → 上抛（normalize_document 统一归一 NormalizationError，本层保持
    库形态异常由调用方归一）。"""
    with pytest.raises(Exception):
        open_pdf_bytes(b"not a pdf at all")


# ---------------------------------------------------------------------------
# 字号采样口径修复（body 众数 / bbox 采样 / 字体权重加粗）
# ---------------------------------------------------------------------------


def test_body_font_size_is_non_space_char_mode() -> None:
    """正文基线 = 非空白字符字号众数（旧「全字符中位数」被空白拉偏）。

    面构造：10 个 12pt 正文字符 + 30 个 24pt 空白字符——旧中位数=24，新口径
    （剔除空白后取众数）=12。ECMA 实测同型：43,030 个 size=1 空白把全字符中位数
    压到 12，而非空白众数是 16（正文真实基线）。
    """

    class _StubPdf:
        def __len__(self) -> int:
            return 1

    sampler = CharFontSampler(_StubPdf())
    body_chars = [(0.0, 0.0, 1.0, 1.0, 12.0, "a", index) for index in range(10)]
    space_chars = [(0.0, 0.0, 1.0, 1.0, 24.0, " ", 10 + index) for index in range(30)]
    sampler.page_chars = lambda page_index: body_chars + space_chars  # type: ignore[method-assign]
    assert sampler.body_font_size == 12.0


def test_stats_in_bbox_samples_only_chars_inside_box() -> None:
    """bbox 内字符采样：只落在盒内的字符参与字号中位数（同页多字号可分）。"""
    data = _build_pdf([[(24, "BIGHEADING"), (12, "small body line")]])
    sampler = CharFontSampler(open_pdf_bytes(data))
    try:
        chars = sampler.page_chars(0)
        big = next(c for c in chars if c[4] == 24.0 and not c[5].isspace())
        small = next(c for c in chars if c[4] == 12.0 and not c[5].isspace())

        def _box(char: tuple[float, float, float, float, float, str, int]) -> tuple[float, float, float, float]:
            return (char[0] - 2, char[1] - 2, char[2] + 2, char[3] + 2)

        big_stat = sampler.stats_in_bbox(0, _box(big))
        small_stat = sampler.stats_in_bbox(0, _box(small))
        assert big_stat is not None and big_stat[0] == pytest.approx(24.0)
        assert small_stat is not None and small_stat[0] == pytest.approx(12.0)
        assert sampler.stats_in_bbox(0, (5000, 5000, 5100, 5100)) is None
    finally:
        sampler.close()


def test_bold_judged_by_font_weight_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """加粗 = FPDFText_GetFontWeight ≥ 700（旧 FontInfo 死信号替换）。"""

    def _weight(value: int):
        def _impl(tp: Any, index: int) -> int:
            return value

        return _impl

    sampler = CharFontSampler(open_pdf_bytes(TWO_PAGE_PDF))
    try:
        tp = sampler._textpage(0)
        monkeypatch.setattr(pdf_textlayer_raw(), "FPDFText_GetFontWeight", _weight(800))
        assert sampler._is_bold(tp, 0) is True
        monkeypatch.setattr(pdf_textlayer_raw(), "FPDFText_GetFontWeight", _weight(435))
        assert sampler._is_bold(tp, 0) is False
        monkeypatch.setattr(pdf_textlayer_raw(), "FPDFText_GetFontWeight", _weight(700))
        assert sampler._is_bold(tp, 0) is True
    finally:
        sampler.close()


def test_stats_in_bbox_bold_takes_median_char_weight(monkeypatch: pytest.MonkeyPatch) -> None:
    """④ 负例：bbox 加粗判定取**中位字符**权重——首字符加粗不代表整行加粗。

    「B」首字符 800、其余 435 → bbox 加粗=False（取中位字符）；反向（仅中位字符
    800）→ True。取首字符的变异形态会被本用例打红。
    """
    data = _build_pdf([[(12, "Bold headline text here")]])
    sampler = CharFontSampler(open_pdf_bytes(data))
    try:
        chars = [c for c in sampler.page_chars(0) if not c[5].isspace()]
        first = chars[0]
        box = (
            min(c[0] for c in chars) - 2,
            min(c[1] for c in chars) - 2,
            max(c[2] for c in chars) + 2,
            max(c[3] for c in chars) + 2,
        )
        mid_index = len(chars) // 2

        def _weight_for(bold_indexes: set[int]):
            def _impl(tp: Any, index: int) -> int:
                return 800 if index in bold_indexes else 435

            return _impl

        # 首字符加粗、其余常规 → 整行不加粗（中位字符权重决定）
        monkeypatch.setattr(pdf_textlayer_raw(), "FPDFText_GetFontWeight", _weight_for({first[6]}))
        stat = sampler.stats_in_bbox(0, box)
        assert stat is not None and stat[1] is False
        # 仅中位字符加粗 → 整行加粗
        monkeypatch.setattr(
            pdf_textlayer_raw(), "FPDFText_GetFontWeight", _weight_for({chars[mid_index][6]})
        )
        stat = sampler.stats_in_bbox(0, box)
        assert stat is not None and stat[1] is True
    finally:
        sampler.close()


# ---------------------------------------------------------------------------
# 工单 B：多信号复核层数据源（盒高采样 / 字体名 / 字号存活检测 / 盒高众数）
# ---------------------------------------------------------------------------


def test_heights_in_bbox_returns_median_charbox_height() -> None:
    """盒高采样：bbox 内非空白字符的字符盒高中位数（字号死亡信号的可移植回落面）。"""
    data = _build_pdf([[(24, "BIGHEADING"), (12, "small body line")]])
    sampler = CharFontSampler(open_pdf_bytes(data))
    try:
        chars = sampler.page_chars(0)
        big = next(c for c in chars if c[4] == 24.0 and not c[5].isspace())
        small = next(c for c in chars if c[4] == 12.0 and not c[5].isspace())

        def _box(char: tuple[float, float, float, float, float, str, int]) -> tuple[float, float, float, float]:
            return (char[0] - 2, char[1] - 2, char[2] + 2, char[3] + 2)

        big_h = sampler.heights_in_bbox(0, _box(big))
        small_h = sampler.heights_in_bbox(0, _box(small))
        assert big_h is not None and big_h > small_h  # 24pt 行盒高 > 12pt 行盒高
        assert sampler.heights_in_bbox(0, (5000, 5000, 5100, 5100)) is None
        # 真字符盒高与字符自身盒高一致（非空白字符）
        assert big_h == pytest.approx(big[3] - big[1], abs=0.01)
    finally:
        sampler.close()


def test_heights_in_bbox_excludes_whitespace_chars() -> None:
    """盒高采样剔空白：空白字符的盒高不得进入中位数（与 stats_in_bbox 同口径）。"""
    import statistics

    data = _build_pdf([[(12, "a b")]])
    sampler = CharFontSampler(open_pdf_bytes(data))
    try:
        chars = sampler.page_chars(0)
        target = next(c for c in chars if c[5] == "a")
        box = (target[0] - 5, target[1] - 5, target[2] + 30, target[3] + 5)
        heights = [
            c[3] - c[1]
            for c in chars
            if not c[5].isspace()
            and box[0] - 1.5 <= (c[0] + c[2]) / 2 <= box[2] + 1.5
            and box[1] - 1.5 <= (c[1] + c[3]) / 2 <= box[3] + 1.5
        ]
        expected = statistics.median(heights)
        assert sampler.heights_in_bbox(0, box) == pytest.approx(expected, abs=0.01)
        # 剔空白自证：含空白的全字符中位数不同（空白盒高参与会稀释）
        all_heights = [
            c[3] - c[1]
            for c in chars
            if box[0] - 1.5 <= (c[0] + c[2]) / 2 <= box[2] + 1.5
            and box[1] - 1.5 <= (c[1] + c[3]) / 2 <= box[3] + 1.5
        ]
        assert all_heights != heights  # 面上确有空白字符被剔除
    finally:
        sampler.close()


def test_font_names_in_bbox_reads_font_info_with_c_int_flags() -> None:
    """字体名面：FPDFText_GetFontInfo 第 5 参须传 ctypes.c_int（裸 int 会 TypeError——
    旧实现死信号根因）；真构造 PDF 返回 Helvetica。"""
    import ctypes

    data = _build_pdf([[(12, "body text line")]])
    sampler = CharFontSampler(open_pdf_bytes(data))
    try:
        chars = sampler.page_chars(0)
        target = next(c for c in chars if not c[5].isspace())
        box = (target[0] - 2, target[1] - 2, target[2] + 2, target[3] + 2)
        names = sampler.font_names_in_bbox(0, box)
        assert names is not None and names
        assert any("helvetica" in name.casefold() for name in names)
        assert sampler.font_names_in_bbox(0, (5000, 5000, 5100, 5100)) is None
        # 绑定形态自证：裸 int 传参复现旧死亡形态（TypeError——第 5 参须 LP_c_int）
        buf = ctypes.create_string_buffer(128)
        with pytest.raises(ctypes.ArgumentError):
            pdf_textlayer._pdfium_raw().FPDFText_GetFontInfo(
                sampler._textpage(0), target[6], buf, 128, 0
            )
    finally:
        sampler.close()


def test_font_size_alive_detects_uniform_dead_signal() -> None:
    """字号存活检测：全文档字号恒定（渲染器死亡形态）→ False；多字号 → True。"""
    sampler = CharFontSampler(open_pdf_bytes(TWO_PAGE_PDF))
    try:
        assert sampler.font_size_alive is True  # 24/12 两档
    finally:
        sampler.close()
    uniform = _build_pdf([[(12, "only one size here"), (12, "second line")]])
    sampler2 = CharFontSampler(open_pdf_bytes(uniform))
    try:
        assert sampler2.font_size_alive is False
    finally:
        sampler2.close()


def test_heights_mode_is_non_space_charbox_mode() -> None:
    """全文盒高众数：非空白字符盒高（取整）众数（复核层正文基线）。"""
    sampler = CharFontSampler(open_pdf_bytes(TWO_PAGE_PDF))
    try:
        from collections import Counter

        histogram: Counter[int] = Counter()
        for i in range(2):
            for char in sampler.page_chars(i):
                if char[4] > 0 and not char[5].isspace():
                    histogram[round(char[3] - char[1])] += 1
        expected = float(histogram.most_common(1)[0][0])
        assert sampler.heights_mode == expected
        assert sampler.heights_mode > 0
    finally:
        sampler.close()
