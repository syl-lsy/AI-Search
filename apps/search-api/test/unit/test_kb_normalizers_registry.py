"""B17 主线一：归一化格式路由 registry 单元测试（全 mock 零网络零真模型）。

契约锚点：ARCHITECTURE §2 S7 / ADR-018 决策③（格式路由 markitdown 主线 + Docling
pdf + OCR 通道）；nacos-config.md §3 `kb.normalizer` 三键（pdf_engine/ocr_enabled/
ocr_engine，validator 枚举 pdf_engine ∈ {markitdown, docling} / ocr_engine ∈
{paddleocr, rapidocr}）；.env KB_ALLOWED_TYPES 白名单域（config.py KbSettings）；
data-model.md §1.7 parse_meta 行 / §1.18 IR 前缀（loc:doc:/loc:blk: 四前缀裁决）。
"""

from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import kb_settings
from app.rag.ir_schema import ID_PREFIXES, LocalLoc, UrlLoc
from app.rag.normalizers.errors import (
    NormalizationError,
    UnsupportedTypeError,
)
from app.rag.normalizers.registry import (
    MIME_BY_EXT,
    SUPPORTED_EXTS,
    NormalizerSettings,
    load_normalizer_settings,
    normalize_document,
    read_normalizer_settings,
    resolve_route,
)


def _settings(**overrides: Any) -> NormalizerSettings:
    base: dict[str, Any] = {
        "pdf_engine": "docling",
        "ocr_enabled": True,
        "ocr_engine": "paddleocr",
    }
    base.update(overrides)
    return NormalizerSettings(**base)


# ---------------------------------------------------------------------------
# 路由表对白名单类型穷举断言（ask 钉死面）
# ---------------------------------------------------------------------------


def test_supported_exts_cover_current_whitelist() -> None:
    """现行 .env 白名单（pdf/md/txt/docx）必须全部可路由。"""
    assert set(kb_settings.allowed_types) <= SUPPORTED_EXTS


def test_route_table_exhaustive_over_extended_whitelist() -> None:
    """M3 白名单扩容形态（+html/xlsx/pptx/csv/htm）逐类型穷举可路由。"""
    extended = ["pdf", "md", "txt", "docx", "html", "htm", "xlsx", "pptx", "csv"]
    for ext in extended:
        route = resolve_route(ext, None, _settings(), allowed_types=extended)
        assert route.ext == ext
        assert route.component in ("markitdown", "docling")


def test_pdf_routes_by_spec_pdf_engine() -> None:
    assert resolve_route("pdf", None, _settings(pdf_engine="docling"), allowed_types=None).component == "docling"
    assert (
        resolve_route("pdf", None, _settings(pdf_engine="markitdown"), allowed_types=None).component
        == "markitdown"
    )


def test_document_class_formats_route_to_markitdown() -> None:
    """md/html/txt/docx/xlsx/pptx/csv 主线 = markitdown（S7 行；pdf 除外）——
    M3 白名单扩容形态下逐类型断言。"""
    extended = ["md", "txt", "html", "htm", "docx", "xlsx", "pptx", "csv"]
    for ext in extended:
        route = resolve_route(ext, None, _settings(), allowed_types=extended)
        assert route.component == "markitdown", ext


def test_ext_normalization_case_and_dot() -> None:
    assert resolve_route(".PDF", None, _settings(), allowed_types=None).ext == "pdf"
    assert resolve_route("DOCX", None, _settings(), allowed_types=None).ext == "docx"


def test_whitelist_domain_enforced() -> None:
    """白名单外类型 → UnsupportedTypeError（KB_ALLOWED_TYPES 白名单域）。"""
    with pytest.raises(UnsupportedTypeError):
        resolve_route("exe", None, _settings(), allowed_types=["pdf", "md", "txt", "docx"])


def test_whitelisted_but_unroutable_rejected() -> None:
    """白名单内但路由表不认识（如 zip）→ UnsupportedTypeError 不静默。"""
    with pytest.raises(UnsupportedTypeError):
        resolve_route("zip", None, _settings(), allowed_types=["pdf", "zip"])


def test_mime_mismatch_rejected_known_mime() -> None:
    """已知 MIME 与扩展名矛盾（pdf 扩名 + html MIME）→ 拒绝不猜。"""
    with pytest.raises(UnsupportedTypeError):
        resolve_route("pdf", "text/html", _settings(), allowed_types=None)


def test_mime_unknown_or_empty_ext_wins() -> None:
    """未知/空 MIME 不拒——扩展名权威（浏览器上传 MIME 常缺失）。"""
    route = resolve_route("pdf", "application/octet-stream", _settings(), allowed_types=None)
    assert route.component == "docling"
    route2 = resolve_route("md", None, _settings(), allowed_types=None)
    assert route2.component == "markitdown"


def test_mime_matches_for_every_supported_ext() -> None:
    """SUPPORTED_EXTS 每项都有 MIME 映射且自洽（同类型 MIME 自查通过）。"""
    for ext in SUPPORTED_EXTS:
        assert ext in MIME_BY_EXT, ext
        assert MIME_BY_EXT[ext], ext


# ---------------------------------------------------------------------------
# spec kb.normalizer 三键读取（经 config_center 现行 settings 形态）
# ---------------------------------------------------------------------------


def test_read_settings_from_spec_payload() -> None:
    spec: dict[str, Any] = {"kb": {"normalizer": {"pdf_engine": "markitdown", "ocr_enabled": False}}}
    settings = read_normalizer_settings(spec)
    assert settings.pdf_engine == "markitdown"
    assert settings.ocr_enabled is False
    assert settings.ocr_engine == "paddleocr"  # 缺键回落代码缺省（键缺失合法回落语义）


def test_read_settings_missing_section_falls_back_to_defaults() -> None:
    settings = read_normalizer_settings({})
    assert settings == _settings()


def test_read_settings_rejects_enum_violation() -> None:
    """pdf_engine 枚举外（validator 已拒，防御面二次校验不静默 clamp）。"""
    spec: dict[str, Any] = {"kb": {"normalizer": {"pdf_engine": "mineru"}}}
    with pytest.raises(UnsupportedTypeError):
        read_normalizer_settings(spec)


def test_load_settings_via_asset_center_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """现行 settings 形态：get_asset_center().snapshot(SPEC_DATA_ID).payload。"""
    payload = {"kb": {"normalizer": {"pdf_engine": "markitdown", "ocr_enabled": True}}}

    fake_snapshot = SimpleNamespace(payload=payload)
    fake_center = SimpleNamespace(snapshot=lambda data_id: fake_snapshot)

    import app.rag.normalizers.registry as registry

    monkeypatch.setattr(registry, "get_asset_center", lambda: fake_center)
    assert load_normalizer_settings().pdf_engine == "markitdown"


def test_load_settings_snapshot_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.rag.normalizers.registry as registry

    fake_center = SimpleNamespace(snapshot=lambda data_id: None)

    monkeypatch.setattr(registry, "get_asset_center", lambda: fake_center)
    with pytest.raises(RuntimeError):
        load_normalizer_settings()


# ---------------------------------------------------------------------------
# normalize_document 分发与输出契约
# ---------------------------------------------------------------------------


def test_normalize_document_dispatches_by_route(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.rag.normalizers.registry as registry

    calls: list[str] = []

    def _fake_markitdown(data: bytes, ext: str, **kwargs: Any) -> Any:
        calls.append(f"markitdown:{ext}")
        raise NormalizationError("stop-here")

    def _fake_docling(data: bytes, **kwargs: Any) -> Any:
        calls.append("docling")
        raise NormalizationError("stop-here")

    monkeypatch.setattr(registry, "normalize_with_markitdown", _fake_markitdown)
    monkeypatch.setattr(registry, "normalize_with_docling", _fake_docling)

    with pytest.raises(NormalizationError):
        normalize_document(b"x", "docx", settings=_settings())
    with pytest.raises(NormalizationError):
        normalize_document(b"x", "pdf", settings=_settings(pdf_engine="docling"))
    with pytest.raises(NormalizationError):
        normalize_document(b"x", "pdf", settings=_settings(pdf_engine="markitdown"))
    assert calls == ["markitdown:docx", "docling", "markitdown:pdf"]


def test_normalize_document_unsupported_ext_raises() -> None:
    with pytest.raises(UnsupportedTypeError):
        normalize_document(b"x", "exe", settings=_settings(), allowed_types=["pdf"])


def test_document_loc_by_doc_kind() -> None:
    """upload → LocalLoc（file_object_key）；url → UrlLoc（source_url）——loc 前缀体系。"""
    # 经 markitdown 真通道（纯字符串 md，零网络零模型）
    result = normalize_document(
        "# 甲\n\n正文。".encode(),
        "md",
        doc_kind="upload",
        file_object_key="kb/u1/d1/original.md",
        settings=_settings(),
    )
    assert result.document.id.startswith("loc:doc:")
    assert isinstance(result.document.loc, LocalLoc)
    assert result.document.loc.file_object_key == "kb/u1/d1/original.md"

    result2 = normalize_document(
        "# 甲".encode(),
        "md",
        doc_kind="url",
        source_url="https://example.com/a",
        settings=_settings(),
    )
    assert isinstance(result2.document.loc, UrlLoc)
    assert result2.document.loc.source_url == "https://example.com/a"


def test_document_node_id_prefix_system() -> None:
    """全节点 id ∈ 四前缀体系（2026-11-23 用户裁决四前缀）。"""
    result = normalize_document(
        "# 标\n\n段。".encode(), "md", settings=_settings()
    )
    ids = [result.document.id]
    stack = [result.document]
    while stack:
        node = stack.pop()
        ids.append(node.id)
        stack.extend(node.children)
    for node_id in ids:
        assert any(node_id.startswith(p) for p in ID_PREFIXES), node_id
