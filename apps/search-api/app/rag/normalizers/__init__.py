"""KB 归一化管线（B17 主线一 / ARCH ADR-018③）：格式路由 → 统一树 IR。

管线形态：`normalize_document(bytes, ext)` 单一入口——白名单域类型路由（pdf 按
spec `kb.normalizer.pdf_engine` 走 docling/markitdown，文档类格式 markitdown 主线）
→ 适配器产出单文档树（loc:doc: 根 + loc:blk: 块树）+ parse_meta（data-model §1.7
四键 + 存储侧留痕）。组件三件：markitdown（Office/HTML 主线，探针 §2.1）、docling
（pdf 结构化 + 字号启发式层级兜底 + OCR 叠加协作面，探针 §2.2）、paddleocr（扫描
页识别惰性单例，PRD v1.34 钉定）。

pdf 标题层级另有确定性校正面（heading_outline）：装配前用 PDF 书签（pypdfium2
get_toc）钉正 heading level，未命中项按相对偏移重校准；书签不可用回落序列一致性
清洗；总开关 `KB_PDF_OUTLINE_CORRECTION`（false = 等价旧管线）。指标验收视图见
`scripts/probe_kb_pipeline.py` 的 ③ 标题结构自检节。

异常面：NormalizationError（BusinessException 子类）/ UnsupportedTypeError /
BadFileError——三方 SDK 异常一律归一（original_exception 链入），零裸穿零内容回显。
"""

from app.rag.normalizers.errors import (
    BadFileError,
    NormalizationError,
    UnsupportedTypeError,
)
from app.rag.normalizers.registry import (
    MIME_BY_EXT,
    SPEC_DATA_ID,
    SUPPORTED_EXTS,
    NormalizerRoute,
    NormalizerSettings,
    load_normalizer_settings,
    normalize_document,
    read_normalizer_settings,
    resolve_route,
)
from app.rag.normalizers.result import NormalizationResult

__all__ = [
    "BadFileError",
    "MIME_BY_EXT",
    "SPEC_DATA_ID",
    "SUPPORTED_EXTS",
    "NormalizationError",
    "NormalizationResult",
    "NormalizerRoute",
    "NormalizerSettings",
    "UnsupportedTypeError",
    "load_normalizer_settings",
    "normalize_document",
    "read_normalizer_settings",
    "resolve_route",
]
