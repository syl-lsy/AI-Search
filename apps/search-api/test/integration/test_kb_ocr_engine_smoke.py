"""integration：PaddleOCR 真引擎冒烟（显式门控，默认 skip）。

口径（沿 `RUN_STRATEGY_PROBE` 先例）：真模型加载 ~5s 且依赖部署期权重预置
（`~/.paddlex/official_models`，`HF_HUB_OFFLINE=1`），不进 unit 确定性循环——
`RUN_PADDLE_OCR_SMOKE=1 make test-integration` 显式启用。

覆盖 P0 全链：PIL 合成图（PIL 画字）→ `recognize_lines`（PIL→BGR ndarray 归一）
→ 真引擎 → 行数 > 0。归一层本身的确定性契约在
`test/unit/test_kb_paddle_engine.py` + `test_kb_docling_adapter.py`（生产接线缝）
锁死，本用例只做真模型端到端佐证。
"""

import os

import pytest

from app.rag.normalizers import paddle_engine

#: 真引擎冒烟显式启用开关（默认零执行）
SMOKE_ENV = "RUN_PADDLE_OCR_SMOKE"

pytestmark = pytest.mark.skipif(
    os.environ.get(SMOKE_ENV) != "1",
    reason=f"真 PaddleOCR 模型加载（约 5s + 部署期权重预置）：设 {SMOKE_ENV}=1 启用",
)


def test_recognize_lines_real_engine_pil_synthetic_scan() -> None:
    """合成扫描图（PIL 画文字）→ 真引擎识别行数 > 0。"""
    Image = pytest.importorskip("PIL.Image")
    ImageDraw = pytest.importorskip("PIL.ImageDraw")
    ImageFont = pytest.importorskip("PIL.ImageFont")
    pytest.importorskip("paddleocr")

    image = Image.new("RGB", (640, 200), color="white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=64)
    draw.text((30, 60), "HELLO OCR 123", fill="black", font=font)

    engine = paddle_engine.get_engine()
    lines = paddle_engine.recognize_lines(engine, image=image)

    assert lines, f"PIL 合成图识别 0 行（输入类型归一回归）：{lines!r}"
