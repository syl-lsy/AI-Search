"""B17 主线一：PaddleOCR 惰性单例引擎单元测试（真模型绝不加载——真引擎冒烟在
`test/integration/test_kb_ocr_engine_smoke.py`，`RUN_PADDLE_OCR_SMOKE=1` 门控）。

OCR 钉定（PRD v1.34 用户裁决 + ARCH §2 S7/ADR-018③）：PaddleOCR PP 系列，进程内
CPU（OQ-32）；HF_HUB_OFFLINE=1 离线加载（模型缓存 ~/.paddlex/official_models 预置）；
重模型懒加载 + 进程内单例；ocr_enabled=false 或文本层完好绝不触发加载（docling
适配器测试锁调用面，本文件锁装载面 + 输入形态契约）。

API 形态 = 探针实证（m3-probe-normalization-20261122.md §2.3，probe_paddleocr.py
脚本留档）：paddleocr 3.x `PaddleOCR(lang="ch", use_doc_orientation_classify=False,
use_doc_unwarping=False, use_textline_orientation=False)` + `predict()` →
`[{"rec_texts": [...], "rec_scores": [...]}]`。
"""

import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.rag.normalizers.errors import NormalizationError
from app.rag.normalizers import paddle_engine


@pytest.fixture(autouse=True)
def _reset_engine() -> None:
    paddle_engine.reset_engine_cache()
    yield
    paddle_engine.reset_engine_cache()


def test_lazy_load_only_once() -> None:
    """懒加载 + 进程内单例：load 恰好一次。"""
    loads = 0

    def _fake_load() -> Any:
        nonlocal loads
        loads += 1
        return object()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(paddle_engine, "_load_engine", _fake_load)
        first = paddle_engine.get_engine()
        second = paddle_engine.get_engine()
        assert first is second
        assert loads == 1


def test_thread_safe_single_load() -> None:
    """并发首取：锁保证 load 恰一次（worker 多任务并发同进程）。"""
    loads = 0
    lock = threading.Lock()

    def _fake_load() -> Any:
        nonlocal loads
        with lock:
            loads += 1
            return object()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(paddle_engine, "_load_engine", _fake_load)
        threads = [threading.Thread(target=paddle_engine.get_engine) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert loads == 1


def test_load_failure_normalized_with_original() -> None:
    def _boom() -> Any:
        raise ModuleNotFoundError("paddleocr not installed")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(paddle_engine, "_load_engine", _boom)
        with pytest.raises(NormalizationError) as excinfo:
            paddle_engine.get_engine()
    assert isinstance(excinfo.value.original_exception, ModuleNotFoundError)


def _page_array() -> Any:
    """合法引擎输入（numpy ndarray——PaddleOCR 3.7.0 唯二支持的形态之一）。"""
    np = pytest.importorskip("numpy")
    return np.zeros((4, 4, 3), dtype=np.uint8)


def test_recognize_lines_maps_rec_texts() -> None:
    """predict 结果 → 文本行（rec_texts 映射；分数零外泄）。"""
    fake = SimpleNamespace(
        predict=lambda image: [
            {"rec_texts": ["海景酒店打卡", "停留2天"], "rec_scores": [0.99, 0.98]}
        ]
    )
    lines = paddle_engine.recognize_lines(fake, image=_page_array())
    assert lines == ["海景酒店打卡", "停留2天"]


def test_recognize_lines_multi_page_result() -> None:
    fake = SimpleNamespace(
        predict=lambda image: [
            {"rec_texts": ["行甲"], "rec_scores": [0.9]},
            {"rec_texts": ["行乙", "行丙"], "rec_scores": [0.9, 0.9]},
        ]
    )
    assert paddle_engine.recognize_lines(fake, image=_page_array()) == ["行甲", "行乙", "行丙"]


# ---------------------------------------------------------------------------
# 输入形态契约（P0 回归：PIL 直传曾被 PaddleOCR 3.7.0 静默忽略 → 扫描页文字全丢）
#
# paddlex OCR 管线 `ReadImage(format="BGR")`（pipeline.py :148）的 ndarray 分支
# 对 ndarray 原样返回、不做通道转换（image_reader.py :52-55）——即 ndarray 契约
# 是 BGR；路径分支经 cv2.imread 同样产出 BGR。PIL 侧是 RGB，跨界必须反转通道。
# ---------------------------------------------------------------------------


def _recording_engine(captured: list[Any]) -> SimpleNamespace:
    """假引擎：记录 predict 收到的输入对象，返回固定一行。"""

    def _predict(image: Any) -> list[dict[str, Any]]:
        captured.append(image)
        return [{"rec_texts": ["行甲"], "rec_scores": [0.9]}]

    return SimpleNamespace(predict=_predict)


def test_recognize_lines_pil_input_converted_to_bgr_ndarray() -> None:
    """PIL.Image 输入 → uint8 BGR ndarray（通道反转；PaddleOCR 只吃 ndarray/str）。"""
    np = pytest.importorskip("numpy")
    Image = pytest.importorskip("PIL.Image")
    captured: list[Any] = []
    pil = Image.new("RGB", (2, 1))
    pil.putpixel((0, 0), (255, 0, 0))  # RGB 纯红
    pil.putpixel((1, 0), (0, 0, 255))  # RGB 纯蓝

    lines = paddle_engine.recognize_lines(_recording_engine(captured), image=pil)

    assert lines == ["行甲"]
    assert len(captured) == 1
    arr = captured[0]
    assert isinstance(arr, np.ndarray)
    assert arr.dtype == np.uint8
    assert arr.shape == (1, 2, 3)
    # 通道序断言：BGR（纯红 → [0,0,255]；纯蓝 → [255,0,0]）——与路径 str 输入
    # 经 cv2.imread 的 BGR 口径一致
    assert arr[0, 0].tolist() == [0, 0, 255]
    assert arr[0, 1].tolist() == [255, 0, 0]
    # 连续性断言：负步长视图若未收敛为连续可写数组，下游 cv2/paddle 行为不可靠
    assert arr.flags["C_CONTIGUOUS"] and arr.flags["WRITEABLE"]


def test_recognize_lines_non_rgb_pil_mode_normalized_three_channels() -> None:
    """非 RGB 模式（如灰度 L）也归一为三通道 BGR ndarray（引擎侧不带模式语义）。"""
    np = pytest.importorskip("numpy")
    Image = pytest.importorskip("PIL.Image")
    captured: list[Any] = []
    pil = Image.new("L", (3, 2), color=128)

    paddle_engine.recognize_lines(_recording_engine(captured), image=pil)

    arr = captured[0]
    assert isinstance(arr, np.ndarray)
    assert arr.shape == (2, 3, 3)
    assert arr[0, 0].tolist() == [128, 128, 128]


def test_recognize_lines_ndarray_input_passthrough() -> None:
    """ndarray 输入原样透传（不二次转通道——上游已按 BGR 契约交付）。"""
    arr = _page_array()
    captured: list[Any] = []
    paddle_engine.recognize_lines(_recording_engine(captured), image=arr)
    assert captured[0] is arr


def test_recognize_lines_path_str_passthrough() -> None:
    """路径字符串输入原样透传（探针脚本形态；既有行为零变化）。"""
    captured: list[Any] = []
    paddle_engine.recognize_lines(_recording_engine(captured), image="/tmp/page.png")
    assert captured[0] == "/tmp/page.png"


def test_recognize_lines_pathlike_input_converted_to_str() -> None:
    """os.PathLike（pathlib.Path）→ str（引擎只认 str，Path 会被静默忽略）。"""
    captured: list[Any] = []
    paddle_engine.recognize_lines(_recording_engine(captured), image=Path("/tmp/page.png"))
    assert captured[0] == "/tmp/page.png"
    assert isinstance(captured[0], str)
    assert os.fspath(captured[0]) == "/tmp/page.png"


def test_recognize_lines_unsupported_input_type_raises() -> None:
    """不支持的输入类型显式失败（不静默丢弃——PIL 直传静默 0 行是本次 P0 根因）。"""
    captured: list[Any] = []
    with pytest.raises(NormalizationError) as excinfo:
        paddle_engine.recognize_lines(_recording_engine(captured), image=object())
    assert captured == []
    assert "不支持" in excinfo.value.message or "不受支持" in excinfo.value.message


class _BytesPath:
    """PEP 519 PathLike 返回 bytes 的形态（引擎只认 str）。"""

    def __fspath__(self) -> bytes:
        return b"/tmp/page.png"


class _RaisingPath:
    """`__fspath__` 抛错的 PathLike（不得裸穿）。"""

    def __fspath__(self) -> str:
        raise ValueError("broken fspath")


def test_recognize_lines_pathlike_bytes_rejected() -> None:
    """fspath → bytes：显式 NormalizationError（detail 带类型名），不静默放行。"""
    captured: list[Any] = []
    with pytest.raises(NormalizationError) as excinfo:
        paddle_engine.recognize_lines(_recording_engine(captured), image=_BytesPath())
    assert captured == []
    assert "bytes" in excinfo.value.detail


def test_recognize_lines_pathlike_error_normalized() -> None:
    """`__fspath__` 抛错 → NormalizationError（original_exception 链入）。"""
    with pytest.raises(NormalizationError) as excinfo:
        paddle_engine.recognize_lines(_recording_engine([]), image=_RaisingPath())
    assert isinstance(excinfo.value.original_exception, ValueError)


def test_offline_env_setdefault(monkeypatch: pytest.MonkeyPatch) -> None:
    """HF_HUB_OFFLINE 缺省置 1（ARCH 离线口径）；显式 0 不覆盖（用户可破例）。"""
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    paddle_engine.ensure_offline_env()
    import os

    assert os.environ["HF_HUB_OFFLINE"] == "1"
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    paddle_engine.ensure_offline_env()
    assert os.environ["HF_HUB_OFFLINE"] == "0"


def test_predict_failure_normalized() -> None:
    def _boom(image: Any) -> Any:
        raise RuntimeError("paddle core dump")

    fake = SimpleNamespace(predict=_boom)
    with pytest.raises(NormalizationError) as excinfo:
        paddle_engine.recognize_lines(fake, image=_page_array())
    assert isinstance(excinfo.value.original_exception, RuntimeError)

