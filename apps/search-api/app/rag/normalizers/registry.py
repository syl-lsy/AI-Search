"""归一化格式路由 registry（B17 主线一入口）。

职责（ARCH §2 S7 / ADR-018③；nacos-config.md §3 `kb.normalizer` 三键）：
1. **类型路由表**：KB_ALLOWED_TYPES 白名单域内按扩展名路由——pdf → spec
   `kb.normalizer.pdf_engine`（docling 主线 / markitdown 热更备选），文档类格式
   （md/txt/html/htm/docx/xlsx/pptx/csv）→ markitdown 主线；白名单外/路由表外
   → UnsupportedTypeError（显式拒绝不猜）；
2. **组件选择配置读取**：spec kb.normalizer 三键（pdf_engine/ocr_enabled/
   ocr_engine）经 config_center 现行 settings 形态拿值（`get_asset_center()
   .snapshot(SPEC_DATA_ID).payload`，OQ-11 同款快照捕获语义）——`read_` 纯函数
   面吃 payload（键缺失合法回落代码缺省，灰度安全），`load_` 面吃资产中心；
3. **normalize_document 分发**：路由 → 适配器 → NormalizationResult。

枚举与 validator 同源：pdf_engine ∈ {markitdown, docling} / ocr_engine ∈
{paddleocr, rapidocr}（config_center/validator.py KbNormalizerSpec；本表为消费侧
防御面——枚举外值显式拒绝不 clamp）。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.config_center import get_asset_center
from app.core.config import kb_settings
from app.rag.normalizers.docling_adapter import normalize_with_docling
from app.rag.normalizers.errors import UnsupportedTypeError
from app.rag.normalizers.markitdown_adapter import normalize_with_markitdown
from app.rag.normalizers.result import NormalizationResult
from app.rag.normalizers.settings import (
    OCR_ENGINES,
    PDF_ENGINES,
    NormalizerSettings,
)

__all__ = [
    "MIME_BY_EXT",
    "SUPPORTED_EXTS",
    "SPEC_DATA_ID",
    "NormalizerRoute",
    "NormalizerSettings",
    "load_normalizer_settings",
    "normalize_document",
    "read_normalizer_settings",
    "resolve_route",
]

#: Agent Spec dataId（与 turn_service._SPEC_DATA_ID 同值——单点各自常量，避免
#: services 层反向 import）
SPEC_DATA_ID = "ai_search.agent.spec.json"

#: 路由表覆盖的格式域（白名单域超集——现行 .env 白名单 pdf/md/txt/docx ⊂ 本表；
#: 文档类格式 = markitdown 主线，探针 §2.1）
SUPPORTED_EXTS: frozenset[str] = frozenset(
    {"pdf", "md", "txt", "docx", "html", "htm", "xlsx", "pptx", "csv"}
)

#: 扩展名 → MIME（上传入口 MIME 归一面；矛盾 MIME 拒绝、未知 MIME 以扩展名权威）
MIME_BY_EXT: dict[str, tuple[str, ...]] = {
    "pdf": ("application/pdf",),
    "md": ("text/markdown", "text/x-markdown"),
    "txt": ("text/plain",),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",),
    "html": ("text/html",),
    "htm": ("text/html",),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",),
    "pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation",),
    "csv": ("text/csv",),
}


@dataclass(frozen=True, slots=True)
class NormalizerRoute:
    """一次归一化的路由判定（组件 + OCR 计划）。"""

    ext: str
    component: str  # markitdown | docling
    ocr_enabled: bool
    ocr_engine: str


def read_normalizer_settings(spec_payload: Mapping[str, Any]) -> NormalizerSettings:
    """spec payload → NormalizerSettings（纯函数；键缺失合法回落代码缺省——
    nacos-config §3「键缺失合法回落代码缺省并告警，灰度安全」语义）。"""
    kb = spec_payload.get("kb")
    normalizer = kb.get("normalizer") if isinstance(kb, Mapping) else None
    if not isinstance(normalizer, Mapping):
        return NormalizerSettings()
    defaults = NormalizerSettings()
    pdf_engine = normalizer.get("pdf_engine", defaults.pdf_engine)
    ocr_enabled = normalizer.get("ocr_enabled", defaults.ocr_enabled)
    ocr_engine = normalizer.get("ocr_engine", defaults.ocr_engine)
    if pdf_engine not in PDF_ENGINES:
        raise UnsupportedTypeError(
            f"kb.normalizer.pdf_engine 枚举外：{pdf_engine}（合法值 {'/'.join(PDF_ENGINES)}）"
        )
    if ocr_engine not in OCR_ENGINES:
        raise UnsupportedTypeError(
            f"kb.normalizer.ocr_engine 枚举外：{ocr_engine}（合法值 {'/'.join(OCR_ENGINES)}）"
        )
    return NormalizerSettings(
        pdf_engine=str(pdf_engine),
        ocr_enabled=bool(ocr_enabled),
        ocr_engine=str(ocr_engine),
    )


def load_normalizer_settings() -> NormalizerSettings:
    """经 config_center 现行 settings 形态拿值（组装层未启动 → RuntimeError——
    与 turn_service._capture_spec_payload 同语义；消费方在入库 worker，lifespan
    启动钩子保证快照已装载）。"""
    snap = get_asset_center().snapshot(SPEC_DATA_ID)
    if snap is None:
        raise RuntimeError(f"Agent Spec 快照不可用（{SPEC_DATA_ID} 未装载）")
    return read_normalizer_settings(snap.payload)


def resolve_route(
    ext: str,
    mime: str | None,
    settings: NormalizerSettings,
    *,
    allowed_types: Sequence[str] | None = None,
) -> NormalizerRoute:
    """扩展名（+已知 MIME 矛盾校验）→ 路由判定。

    白名单域 = KB_ALLOWED_TYPES（缺省读 kb_settings.allowed_types 单例）；MIME
    仅作矛盾校验（已知 MIME 且与扩展名分属不同类型 → 拒绝；未知/空 MIME 不拒——
    浏览器上传 MIME 常缺失，扩展名权威）。
    """
    normalized = ext.lstrip(".").lower()
    whitelist = frozenset(t.lower() for t in (allowed_types or kb_settings.allowed_types))
    if normalized not in whitelist:
        raise UnsupportedTypeError(f"文件类型 .{normalized} 不在知识库白名单内")
    if normalized not in SUPPORTED_EXTS:
        raise UnsupportedTypeError(f"文件类型 .{normalized} 暂无归一化组件")
    if mime:
        mime_l = mime.split(";")[0].strip().lower()
        known_mimes = {m for ext_mimes in MIME_BY_EXT.values() for m in ext_mimes}
        own_mimes = MIME_BY_EXT.get(normalized, ())
        if mime_l in known_mimes and mime_l not in own_mimes:
            raise UnsupportedTypeError(
                f"MIME {mime_l} 与扩展名 .{normalized} 矛盾（拒绝不猜）"
            )
    if normalized == "pdf":
        component = settings.pdf_engine
    else:
        component = "markitdown"
    return NormalizerRoute(
        ext=normalized,
        component=component,
        ocr_enabled=settings.ocr_enabled,
        ocr_engine=settings.ocr_engine,
    )


def normalize_document(
    data: bytes,
    ext: str,
    *,
    mime: str | None = None,
    doc_kind: str = "upload",
    settings: NormalizerSettings | None = None,
    allowed_types: Sequence[str] | None = None,
    file_object_key: str | None = None,
    source_url: str | None = None,
    **adapter_kwargs: Any,
) -> NormalizationResult:
    """格式路由 → 适配器分发（归一化管线单一入口；同步 CPU 面，worker 侧
    to_thread 下放）。

    settings 缺省时经 config_center 快照现读（load_normalizer_settings）；单测/
    显式调用方传 NormalizerSettings 直用（零资产中心依赖）。adapter_kwargs 直通
    适配器注入面（converter/font_sampler/text_layer_pages/ocr_recognizer/
    page_count——单测 mock 缝，生产调用方零传）。
    """
    settings = settings or load_normalizer_settings()
    route = resolve_route(ext, mime, settings, allowed_types=allowed_types)
    if route.component == "docling":
        return normalize_with_docling(
            data,
            doc_kind=doc_kind,
            file_object_key=file_object_key,
            source_url=source_url,
            settings=settings,
            **adapter_kwargs,
        )
    return normalize_with_markitdown(
        data,
        route.ext,
        doc_kind=doc_kind,
        file_object_key=file_object_key,
        source_url=source_url,
    )
