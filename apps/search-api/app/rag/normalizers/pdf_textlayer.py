"""pdf 文本层探测与字号采样（pypdfium2 轻绑定——docling 传递依赖，非模型）。

职责三件（docling 适配器的数据源）：
1. **文本层稀疏判定**（ARCH「文本层缺失判定后叠加」OCR 触发面）：页 char 数阈值
   （代码常量 `OCR_TEXT_LAYER_MIN_CHARS`——纯逻辑阈值按三分法收敛常量模块；现行
   阈值 32＝B17 现场标定值：真扫描页 0 字符、稀疏页 9~22 字符——**稀疏页仍进 OCR
   候选**，噪声在合并环节按文本层原文去重，不再靠抬高阈值丢弃整页；
2. **字号/加粗采样**（heading 启发式兜底数据源——探针 §2.2「pdf 标题层级全判
   level1」短板的补偿面；raw fpdfium 字号/字体名 API）；
3. **页位图渲染**（OCR 通道输入——PaddleOCR 进程内识别的图像来源）。

pypdfium2 惰性 importlib 解析（与 markitdown/paddleocr 同款——依赖在 venv 内但
import 惰性化，保模块 import 轻量）；本模块保持库形态（异常原样上抛），归一为
NormalizationError 由 docling 适配器统一收口。
"""

import ctypes
import importlib
import io
from collections import Counter
from statistics import median
from typing import Any

#: 文本层稀疏判定阈值（页 char 数下限）——**低于 32 字符视为稀疏页、仍进 OCR 候选**。
#: 依据（B17 现场标定 + 2026-11-24 复核）：
#: - 真扫描页 pdfium 抽取字符数 = 0；ECMA 第 252/328 页 22/9 字符（页眉/页码/「修改记录」）。
#: - **失败方向讨论（为何不抬阈值）**：字符数 > 0 ≠ 文本层可用——「正文是图像、文本层只有
#:   页码戳」的页（评审实测形态）若因稀疏而跳过 OCR，整页正文静默丢失；旧规则只是页码
#:   重复（可见噪声）。故本阈值只负责「是否值得 OCR」，**噪声由合并环节按该页文本层
#:   原文逐行去重**（docling_adapter._insert_ocr_lines）兜底——宁可多跑一次 OCR，
#:   不可整页丢字。抬高阈值只会扩大 OCR 面、增加去重负担，收益无。
OCR_TEXT_LAYER_MIN_CHARS = 32
#: OCR 页渲染缩放（72dpi 基准 ×2 ≈ 144dpi——CPU 识别耗时与清晰度折中）
OCR_RENDER_SCALE = 2.0

#: 加粗判定阈值（FPDFText_GetFontWeight）——ECMA A/B 实测：Arial-BoldMT=800、
#: 常规字体 435；≥700 判粗体。旧实现走 FPDFText_GetFontInfo 且第 5 参传 int 0
#: 在 pdfium 侧抛错被吞 → 全工程恒 False（死信号，2026-11-24 根因报告实证）。
BOLD_FONT_WEIGHT_MIN = 700
#: bbox 字号采样容差（pt）：字符中心落在 bbox 外扩该值内即计入（docling bbox 与
#: pdfium 字符盒存在亚像素偏差，1.5pt 为 debugger 原型实测零误配窗口）
_BBOX_CENTER_TOLERANCE = 1.5

_PDFIUM_MODULE: Any | None = None
_PDFIUM_RAW: Any | None = None


def _pdfium() -> Any:
    """pypdfium2 主模块惰性解析（SDK 运行期 importlib——同 storage 先例）。"""
    global _PDFIUM_MODULE
    if _PDFIUM_MODULE is None:
        _PDFIUM_MODULE = importlib.import_module("pypdfium2")
    return _PDFIUM_MODULE


def _pdfium_raw() -> Any:
    """pypdfium2.raw 字号/字体名 API 惰性解析。"""
    global _PDFIUM_RAW
    if _PDFIUM_RAW is None:
        _PDFIUM_RAW = importlib.import_module("pypdfium2.raw")
    return _PDFIUM_RAW


def open_pdf_bytes(data: bytes) -> Any:
    """bytes → pypdfium2 PdfDocument（坏字节由 pdfium 抛错，调用方归一）。"""
    return _pdfium().PdfDocument(io.BytesIO(data))


def page_char_counts(pdf: Any) -> list[int]:
    """逐页文本 char 数（文本层探测原始量）。"""
    counts: list[int] = []
    for page in pdf:
        textpage = page.get_textpage()
        try:
            counts.append(textpage.count_chars())
        finally:
            textpage.close()
            page.close()
    return counts


def page_text_flags(
    pdf: Any | None = None,
    *,
    char_counts: list[int] | None = None,
    threshold: int = OCR_TEXT_LAYER_MIN_CHARS,
) -> list[bool]:
    """逐页文本层判定：char 数 ≥ 阈值 → True（文本层充足，OCR 零触发）。

    低于阈值（含 0）→ False（**稀疏/缺失页进 OCR 候选**）——稀疏页是否重复由合并
    环节按页文本层原文去重（见 `paddle_engine`/`docling_adapter` 的 OCR 叠加面）。
    """
    if char_counts is None:
        if pdf is None:
            raise ValueError("pdf 与 char_counts 至少给一个")
        char_counts = page_char_counts(pdf)
    return [count >= threshold for count in char_counts]


def page_text(pdf: Any, page_index: int) -> str:
    """页 → 文本层原文（FPDFText_GetText；OCR 去重比对面）。

    与 `page_char_counts` 同一抽取面、逐页独立开合（句柄零滞留）；坏页异常原样
    上抛，由调用方按「去重不可用」降级（不得阻塞 OCR 叠加主链）。
    """
    page = pdf[page_index]
    textpage = None
    try:
        textpage = page.get_textpage()
        return str(textpage.get_text_range())
    finally:
        if textpage is not None:
            textpage.close()
        page.close()


def render_page(pdf: Any, page_index: int, *, scale: float = OCR_RENDER_SCALE) -> Any:
    """页 → PIL 位图（OCR 输入；页用完即关，连接句柄零滞留）。

    返回形态是 **PIL.Image（RGB）**，不是 numpy——PaddleOCR 只接受 ndarray/路径，
    PIL 直传会被静默忽略（P0 缺陷形态）。跨界归一由 `paddle_engine.recognize_lines`
    的 `_normalize_engine_input` 收口（PIL→BGR ndarray），调用方不要绕过该层。
    """
    page = pdf[page_index]
    try:
        bitmap = page.render(scale=scale)
        return bitmap.to_pil()
    finally:
        page.close()


def _font_info_name(tp: Any, char_index: int) -> str | None:
    """单字符字体名（FPDFText_GetFontInfo）；不可得 → None（调用方按无证据处理）。

    第 5 参 flags 必须传 `ctypes.c_int(0)`：绑定层签名为 LP_c_int，裸 int 抛
    TypeError（2026-11-24 实测复现）——旧实现「FontInfo 死信号」的根因即此形态。
    """
    try:
        buf = ctypes.create_string_buffer(128)
        length = _pdfium_raw().FPDFText_GetFontInfo(tp.raw, char_index, buf, 128, ctypes.c_int(0))
    except Exception:  # noqa: BLE001 —— 字体名面异常一律按不可得处理（零降级方向）
        return None
    if not length or length <= 0:
        return None
    return buf.value.decode("utf-8", "replace")


class CharFontSampler:
    """页字符级字号/加粗采样（heading 启发式数据源；textpage 惰性缓存按页）。

    API 形态：
    - `stats_in_bbox(page_index, bbox) -> (字号中位数, 是否加粗) | None`：**bbox 内
      字符采样**（heading 分级与提升的唯一生产入口）——按字符盒中心落在 bbox（外扩
      1.5pt）内筛选非空白字符，字号取中位数；加粗取中位字符的
      `FPDFText_GetFontWeight ≥ 700`。bbox 为该页 top-left 原点、PDF 点单位。
    - `text_font_stats(page_index, text)`：旧「文本首现搜索」采样（无 bbox 的注入
      假体/兜底路径保留；已知缺陷=同页同文多处时采错首现，生产路径改用 bbox 面）。
    - `body_font_size`：**非空白字符字号的众数**（旧口径=包含空白的全字符中位数，
      ECMA 实测 12 vs 16——空白字符把中位数拉偏；众数才代表正文密度）。
    - `page_char_sizes` / `page_chars`：逐页字符字号/盒（测试与诊断面）。

    句柄纪律：`close()` 释放本采样器缓存的 page/textpage（PdfDocument 生命周期由
    创建方负责；docling 适配器在采样完成后 close + 关闭自己打开的文档）。
    """

    def __init__(self, pdf: Any) -> None:
        self._pdf = pdf
        self._pages: dict[int, Any] = {}
        self._textpages: dict[int, Any] = {}
        self._sizes_cache: dict[int, list[float]] = {}
        self._chars_cache: dict[
            int, list[tuple[float, float, float, float, float, str, int]]
        ] = {}
        self._body_cache: float | None = None
        self._heights_mode_cache: float | None = None
        self._alive_cache: bool | None = None

    def close(self) -> None:
        for tp in self._textpages.values():
            tp.close()
        for page in self._pages.values():
            page.close()
        self._textpages.clear()
        self._pages.clear()
        self._sizes_cache.clear()
        self._chars_cache.clear()

    def _textpage(self, page_index: int) -> Any:
        if page_index not in self._textpages:
            page = self._pdf[page_index]
            tp = page.get_textpage()
            # page 引用必须保活：关闭 page 会使 textpage 底层句柄失效
            # （close 后 tp.raw 为 None——本会话实测），采样面全废
            self._pages[page_index] = page
            self._textpages[page_index] = tp
        return self._textpages[page_index]

    def page_char_sizes(self, page_index: int) -> list[float]:
        """页内逐字符字号（空白/零值剔除；按页缓存）。"""
        if page_index in self._sizes_cache:
            return self._sizes_cache[page_index]
        tp = self._textpage(page_index)
        count = tp.count_chars()
        get_size = _pdfium_raw().FPDFText_GetFontSize
        sizes: list[float] = []
        for i in range(count):
            size = get_size(tp.raw, i)
            if size > 0:
                sizes.append(float(size))
        self._sizes_cache[page_index] = sizes
        return sizes

    def page_chars(
        self, page_index: int
    ) -> list[tuple[float, float, float, float, float, str, int]]:
        """逐字符 `(x0, y0, x1, y1, 字号, 字符, 页内字符下标)`（top-left 原点、点；按页缓存）。

        y 轴由 pdfium 的 bottom-left 转为 top-left（`page_h - t` / `page_h - b`），
        与 docling `bbox.to_top_left_origin(...)` 同坐标系——bbox 采样可直接比较。
        """
        if page_index in self._chars_cache:
            return self._chars_cache[page_index]
        tp = self._textpage(page_index)
        page_height = float(self._pages[page_index].get_size()[1])
        get_size = _pdfium_raw().FPDFText_GetFontSize
        chars: list[tuple[float, float, float, float, float, str, int]] = []
        for i in range(tp.count_chars()):
            try:
                left, bottom, right, top = tp.get_charbox(i)
            except Exception:  # noqa: BLE001 —— 单字符盒不可得不废整页采样
                continue
            size = float(get_size(tp.raw, i))
            chars.append(
                (float(left), page_height - float(top), float(right), page_height - float(bottom), size,
                 tp.get_text_range(i, 1), i)
            )
        self._chars_cache[page_index] = chars
        return chars

    @property
    def body_font_size(self) -> float | None:
        """全文正文基线 = **非空白字符字号的众数**（整数化后取最高频；无数据 → None）。

        与旧「全字符中位数」口径的差别（ECMA 实测）：空白字符量大且常随行大小分布，
        把中位数拉向小字号（12）而正文实际众数 16；heading 阈值以本值为基线。
        """
        if self._body_cache is None:
            histogram: Counter[int] = Counter()
            for i in range(len(self._pdf)):
                for char in self.page_chars(i):
                    if char[4] > 0 and not char[5].isspace():
                        histogram[round(char[4])] += 1
            if not histogram:
                return None
            self._body_cache = float(histogram.most_common(1)[0][0])
        return self._body_cache

    @property
    def heights_mode(self) -> float | None:
        """全文非空白字符**字符盒高众数**（复核层的正文盒高基线；无数据 → None）。

        字号（GetFontSize）死亡标本上的正文基线：盒高取整后取最高频——CJK 正文
        盒高聚集（ECMA 实测正文 ≈9.5~11.1 → 众数 10~11），与标题盒高（≥13.8）
        分离度 ≥1.25×。
        """
        if self._heights_mode_cache is None:
            histogram: Counter[int] = Counter()
            for i in range(len(self._pdf)):
                for char in self.page_chars(i):
                    if char[4] > 0 and not char[5].isspace():
                        height = char[3] - char[1]
                        if height > 0:
                            histogram[round(height)] += 1
            if not histogram:
                return None
            self._heights_mode_cache = float(histogram.most_common(1)[0][0])
        return self._heights_mode_cache

    @property
    def font_size_alive(self) -> bool:
        """字号信号存活判定：**非空白字符**非零字号取整后 ≥2 档 → True。

        剔空白口径与 body_font_size/heights_mode 一致——空白字符的字号在健康
        PDF 上也可能报退化值（构造 PDF 实测空白=1.0），不剔会把健康文档误判为
        死亡。渲染器死亡形态（2026-11-24 ECMA 新渲染版实测：全字符恒 1.0）→
        False，提升通道/复核门回落字符盒高口径（heading_review.size_stat_for）。
        """
        if self._alive_cache is None:
            distinct: set[int] = set()
            for i in range(len(self._pdf)):
                for char in self.page_chars(i):
                    if char[4] > 0 and not char[5].isspace():
                        distinct.add(round(char[4]))
                        if len(distinct) >= 2:
                            self._alive_cache = True
                            return True
            self._alive_cache = False
        return self._alive_cache

    def stats_in_bbox(
        self, page_index: int, bbox: tuple[float, float, float, float]
    ) -> tuple[float, bool] | None:
        """bbox（top-left、点）内字符字号中位数 + 加粗；无候选 → None。

        生产 heading 分级/提升的采样面（取代「文本首现搜索」——同页同文多处时首现
        会采错，ECMA 实测 30/373 条偏样）。
        """
        candidates = self._chars_in_bbox(page_index, bbox)
        if not candidates:
            return None
        sizes = [char[4] for char in candidates]
        # 加粗面取候选序列中位字符（页内字符下标随 tuple 携带，免二次定位）
        mid_index = candidates[len(candidates) // 2][6]
        return float(median(sizes)), self._is_bold(self._textpage(page_index), mid_index)

    def _chars_in_bbox(
        self, page_index: int, bbox: tuple[float, float, float, float]
    ) -> list[tuple[float, float, float, float, float, str, int]]:
        """bbox 内候选字符（剔空白 + 字符盒中心外扩容差过滤；无候选 → 空列表）。

        stats_in_bbox / heights_in_bbox / font_names_in_bbox 共用的过滤面——空白
        字符盒会稀释中位数（负例锁死面，工单 B ④「bbox 采样剔空白」）。
        """
        left, top, right, bottom = bbox
        return [
            char
            for char in self.page_chars(page_index)
            if char[4] > 0
            and not char[5].isspace()
            and left - _BBOX_CENTER_TOLERANCE <= (char[0] + char[2]) / 2 <= right + _BBOX_CENTER_TOLERANCE
            and top - _BBOX_CENTER_TOLERANCE <= (char[1] + char[3]) / 2 <= bottom + _BBOX_CENTER_TOLERANCE
        ]

    def heights_in_bbox(
        self, page_index: int, bbox: tuple[float, float, float, float]
    ) -> float | None:
        """bbox 内非空白字符的**字符盒高中位数**；无候选 → None。

        工单 B 复核层的「字号」主信号：2026-11-24 渲染版本更换后的标本上
        FPDFText_GetFontSize 恒 1.0（死亡信号，全文档实测），字符盒高与字号线性
        同构、跨渲染器存活；相对比值阈值（≥正文众数×1.25）两口径通用。
        """
        candidates = self._chars_in_bbox(page_index, bbox)
        if not candidates:
            return None
        return float(median(char[3] - char[1] for char in candidates))

    def font_names_in_bbox(
        self, page_index: int, bbox: tuple[float, float, float, float]
    ) -> list[str] | None:
        """bbox 内非空白字符的字体名（去重保序）；无候选或全部不可得 → None。

        `FPDFText_GetFontInfo` 第 5 参必须传 `ctypes.c_int`（裸 int 在绑定层
        TypeError——旧实现死信号根因）；名称是 pdfium 侧原始串（子集前缀/混淆名
        原样返回，等宽判定由调用方做）。
        """
        candidates = self._chars_in_bbox(page_index, bbox)
        if not candidates:
            return None
        tp = self._textpage(page_index)
        names: list[str] = []
        seen: set[str] = set()
        for char in candidates:
            name = _font_info_name(tp, char[6])
            if name is not None and name not in seen:
                seen.add(name)
                names.append(name)
        return names or None

    def text_font_stats(self, page_index: int, text: str) -> tuple[float, bool] | None:
        """按文本首现位置定位字符区间 → (字号中位数, 是否加粗)；定位失败 → None。"""
        if not text:
            return None
        tp = self._textpage(page_index)
        searcher = tp.search(text)
        try:
            found = searcher.get_next()
        finally:
            close = getattr(searcher, "close", None)
            if callable(close):
                close()
        if not found:
            return None
        char_index, char_count = found
        get_size = _pdfium_raw().FPDFText_GetFontSize
        sizes = []
        total = tp.count_chars()
        for i in range(char_index, min(char_index + char_count, total)):
            size = float(get_size(tp.raw, i))
            if size > 0:
                sizes.append(size)
        if not sizes:
            return None
        mid = char_index + char_count // 2
        return float(median(sizes)), self._is_bold(tp, mid)

    def _is_bold(self, tp: Any, char_index: int) -> bool:
        """`FPDFText_GetFontWeight ≥ 700` 判粗体（取不到 → False 如实不猜）。"""
        try:
            weight = _pdfium_raw().FPDFText_GetFontWeight(tp.raw, char_index)
        except Exception:  # noqa: BLE001 —— 权重 API 面异常一律按非加粗处理
            return False
        return bool(weight >= BOLD_FONT_WEIGHT_MIN)
