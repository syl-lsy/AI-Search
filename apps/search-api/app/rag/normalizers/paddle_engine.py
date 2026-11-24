"""PaddleOCR 惰性单例引擎（扫描件 OCR 通道——PRD v1.34 用户裁决钉定）。

锚点：ARCH §2 S7 / ADR-018③（PaddleOCR PP 系列，未采纳 RapidOCR 建议、留 M3+
重场景复核线）；OQ-32（本地 CPU 进程内）；.env HF_HUB_OFFLINE=1 离线加载（模型
缓存 ~/.paddlex/official_models 预置——首跑下载属部署动作非运行时，
m3-probe-normalization-20261122.md §1 已验证离线复跑可用）。

加载纪律：**重模型懒加载 + 进程内单例 + 线程安全**（入库 worker 多任务并发面）——
`ocr_enabled=false` 或文本层完好 pdf 的调用方绝不触达本模块（触发面由 docling
适配器收口并测试锁死），引擎构造只发生在首个真实扫描页识别前。

API 形态 = 探针实证（m3-probe-normalization §2.3；probe_paddleocr.py 脚本留档）：
paddleocr 3.x `PaddleOCR(lang="ch", use_doc_orientation_classify=False,
use_doc_unwarping=False, use_textline_orientation=False)` + `predict(image)` →
`[{"rec_texts": [...], "rec_scores": [...]}]`。

异常归一：装载/识别失败 → NormalizationError（original_exception 链入；消息零
内容回显——识别文本绝不进异常消息/日志）。

**输入形态契约（P0 修复 2026-11-24）**：PaddleOCR 3.7.0 的 `predict` 只接受
`numpy.ndarray` / 路径 `str`——其他类型（含 PIL.Image）被管线**静默忽略**（打
warning 后返回 0 结果，不抛错）。上游 `pdf_textlayer.render_page` 产出 PIL 位图，
跨界前必须在 `_normalize_engine_input` 归一：PIL → **BGR** uint8 ndarray。
通道序依据：paddlex OCR 管线 `ReadImage(format="BGR")`
（`.venv/.../paddlex/inference/pipelines/ocr/pipeline.py`）的 ndarray 分支原样返回、
不做通道转换（`.../common/reader/image_reader.py`），路径分支经 `cv2.imread` 亦产
BGR——故 ndarray 契约即 BGR；PIL 侧为 RGB，必须反转通道。`ndarray`/`str` 原样
透传（既有行为零变化），`os.PathLike` 转 `str`，其余类型显式 NormalizationError
（不静默丢弃）。
"""

import importlib
import os
import threading
from typing import Any

from app.rag.normalizers.errors import NormalizationError

_ENGINE: Any | None = None
_LOCK = threading.Lock()


def ensure_offline_env() -> None:
    """HF_HUB_OFFLINE 缺省置 1（ARCH 离线口径）；进程已有显式值不覆盖（可破例）。"""
    os.environ.setdefault("HF_HUB_OFFLINE", "1")


def _load_engine() -> Any:
    """真装载（仅单例 miss 时触达；单测以 monkeypatch 替换本函数，真模型绝不加载）。"""
    ensure_offline_env()
    paddleocr = importlib.import_module("paddleocr")
    return paddleocr.PaddleOCR(
        lang="ch",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
    )


def get_engine() -> Any:
    """进程内单例引擎（double-checked locking；装载失败归一 NormalizationError）。"""
    global _ENGINE
    if _ENGINE is None:
        with _LOCK:
            if _ENGINE is None:
                try:
                    _ENGINE = _load_engine()
                except Exception as exc:
                    raise NormalizationError(
                        "OCR 引擎装载失败",
                        detail=f"{type(exc).__name__}: {exc}",
                        original_exception=exc,
                    ) from exc
    return _ENGINE


def reset_engine_cache() -> None:
    """清空单例（测试隔离面；生产进程内不调用）。"""
    global _ENGINE
    _ENGINE = None


def _normalize_engine_input(image: Any) -> Any:
    """引擎输入形态归一（调用前最后一层：PIL 与 ndarray 双上游都能走通）。

    契约（见模块 docstring）：`ndarray`/`str` 原样透传；`os.PathLike` → `str`
    （`__fspath__` 抛错或返回 bytes 一律显式拒绝——引擎只认 str，bytes 会被静默
    忽略）；`PIL.Image` → **BGR** uint8 `ndarray`（PIL 是 RGB，按 paddlex
    `ReadImage(format="BGR")` 的 ndarray 契约反转通道）；其余类型显式拒绝——
    PaddleOCR 对未知类型只 warning 后返回空结果，静默丢弃正是 P0 缺陷形态，这里
    必须响亮失败。
    """
    numpy = importlib.import_module("numpy")
    if isinstance(image, numpy.ndarray):
        return image
    if isinstance(image, str):
        return image
    if isinstance(image, os.PathLike):
        try:
            path = os.fspath(image)
        except Exception as exc:  # noqa: BLE001 —— __fspath__ 抛错归一为契约错误
            raise NormalizationError(
                "OCR 输入路径不可解析",
                detail=f"type={type(image).__name__}",
                original_exception=exc,
            ) from exc
        if not isinstance(path, str):
            # fspath 可返回 bytes（PEP 519）——引擎只认 str，bytes 会被静默忽略
            raise NormalizationError(
                "OCR 输入路径类型不受支持",
                detail=f"os.fspath→{type(path).__name__}（引擎只接受 str 路径）",
            )
        return path
    pil_image = importlib.import_module("PIL.Image")
    if isinstance(image, pil_image.Image):
        rgb = image if image.mode == "RGB" else image.convert("RGB")
        # RGB → BGR（页渲染图多为灰度扫描件，通道序不影响识别率，但契约必须与
        # 路径 str 输入的 cv2.imread BGR 口径一致——否则彩色页会 R/B 互换）
        return numpy.ascontiguousarray(numpy.asarray(rgb)[:, :, ::-1])
    raise NormalizationError(
        "OCR 输入形态不受支持",
        detail=(
            f"type={type(image).__name__}（PaddleOCR 3.7.0 仅接受 numpy.ndarray / "
            "路径 str；PIL 等对象须经本函数归一）"
        ),
    )


def recognize_lines(engine: Any, *, image: Any) -> list[str]:
    """单页位图 → 文本行流（rec_texts 映射；分数不外泄——OCR 行流无结构语义，
    表格/标题结构重建不在 OCR 通道职责内，探针 §2.3 缺口如实由上层消费）。

    输入先过 `_normalize_engine_input`：PIL 位图转 BGR ndarray（引擎只认
    ndarray/路径，PIL 直传会被静默忽略产 0 行——P0 根因）。
    """
    try:
        engine_input = _normalize_engine_input(image)
        results = engine.predict(engine_input)
    except NormalizationError:
        raise
    except Exception as exc:
        raise NormalizationError(
            "OCR 识别失败",
            detail=f"{type(exc).__name__}: {exc}",
            original_exception=exc,
        ) from exc
    lines: list[str] = []
    for page_result in results or []:
        try:
            texts = page_result["rec_texts"]
        except (KeyError, TypeError, IndexError) as exc:
            raise NormalizationError(
                "OCR 结果形态异常",
                detail=f"{type(exc).__name__}: {exc}",
                original_exception=exc,
            ) from exc
        lines.extend(str(t) for t in texts or [])
    return lines
