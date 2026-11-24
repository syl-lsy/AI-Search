"""归一化组件路由设置（spec kb.normalizer 三键内存投影——registry 与适配器共用，
独立小模块避免 registry↔适配器循环 import）。

枚举与 validator 同源：pdf_engine ∈ {markitdown, docling} / ocr_engine ∈
{paddleocr, rapidocr}（config_center/validator.py KbNormalizerSpec）；缺省值 =
config_center/defaults.py spec kb.normalizer 内置默认（docling / True / paddleocr
——ocr_engine 初值钉 paddleocr = PRD v1.34 用户裁决）。
"""
from dataclasses import dataclass

PDF_ENGINES: tuple[str, ...] = ("markitdown", "docling")
OCR_ENGINES: tuple[str, ...] = ("paddleocr", "rapidocr")

_DEFAULT_PDF_ENGINE = "docling"
_DEFAULT_OCR_ENABLED = True
_DEFAULT_OCR_ENGINE = "paddleocr"


@dataclass(frozen=True, slots=True)
class NormalizerSettings:
    """spec kb.normalizer 三键的内存投影（键缺失合法回落代码缺省）。"""

    pdf_engine: str = _DEFAULT_PDF_ENGINE
    ocr_enabled: bool = _DEFAULT_OCR_ENABLED
    ocr_engine: str = _DEFAULT_OCR_ENGINE
