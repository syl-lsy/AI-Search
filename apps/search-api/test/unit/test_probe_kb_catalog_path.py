"""探针 `--catalog-path` 旗标的单测（合成小样本跑管线，零网络零 DB）。

验收口径（2026-11-24 切片标题轨迹工单）：探针要能在真标本上演示「目录链 + 文档内
标题路径」的完整三层轨迹——`--catalog-path "A > B"` 把链拆进 document 根节点
`catalog_path` 后再跑同一管线；**缺省不设 = 现状零变化**（链恒空）。
旗标属附加面：与三种输入方式（文件路径 / --sample / --feishu-token）共存，
三选一互斥纪律不变。
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]


def _load_probe() -> Any:
    """按路径装载探针脚本（scripts/ 非包；模块级只做 import，无副作用）。"""
    path = _REPO_ROOT / "scripts" / "probe_kb_pipeline.py"
    spec = importlib.util.spec_from_file_location("probe_kb_pipeline_catalog_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass 解析 __module__ 需已注册（exec 前）
    spec.loader.exec_module(module)
    return module


probe = _load_probe()

_SAMPLE_MD = "# 管线验证样本\n\n正文。\n".encode("utf-8")


# ---------------------------------------------------------------------------
# 链字符串解析
# ---------------------------------------------------------------------------


def test_parse_catalog_path_splits_on_gt() -> None:
    assert probe._parse_catalog_path("RAG核心面（精选） > 一、基础认知与对比") == [
        "RAG核心面（精选）",
        "一、基础认知与对比",
    ]


def test_parse_catalog_path_strips_segments_and_skips_empty() -> None:
    assert probe._parse_catalog_path("  A  >  > B >  ") == ["A", "B"]


def test_parse_catalog_path_none_and_blank_yield_empty() -> None:
    assert probe._parse_catalog_path(None) == []
    assert probe._parse_catalog_path("") == []
    assert probe._parse_catalog_path("   >  ") == []


# ---------------------------------------------------------------------------
# 管线：链写入 document 根 → 轨迹逐级拼接
# ---------------------------------------------------------------------------


def test_run_pipeline_sets_root_catalog_path_and_full_trace() -> None:
    output = probe.run_pipeline(
        _SAMPLE_MD, "md", "样本", catalog_path=["甲", "乙"]
    )
    assert output.document.catalog_path == ["甲", "乙"]
    assert output.chunking.chunks[0].heading_path == ["甲", "乙", "管线验证样本"]


def test_run_pipeline_default_keeps_catalog_empty() -> None:
    """缺省不设 → 根 catalog_path 恒空、轨迹同现状（零变化）。"""
    output = probe.run_pipeline(_SAMPLE_MD, "md", "样本")
    assert output.document.catalog_path == []
    assert output.chunking.chunks[0].heading_path == ["管线验证样本"]


def test_run_pipeline_blank_catalog_path_is_noop() -> None:
    """空白链不产生空串层（解析层已滤），管线侧等价缺省。"""
    output = probe.run_pipeline(_SAMPLE_MD, "md", "样本", catalog_path=[])
    assert output.document.catalog_path == []
    assert output.chunking.chunks[0].heading_path == ["管线验证样本"]


def test_run_pipeline_filters_blank_segments_from_direct_call() -> None:
    """直接调 run_pipeline 传空白段 → 消费面滤除（纯空白不是标题，落进链会污染
    轨迹与将来 ES heading_path 字段；只丢空白段、不重写真实标题）。"""
    output = probe.run_pipeline(
        _SAMPLE_MD, "md", "样本", catalog_path=["", "  ", "甲"]
    )
    assert output.document.catalog_path == ["甲"]
    assert output.chunking.chunks[0].heading_path == ["甲", "管线验证样本"]


# ---------------------------------------------------------------------------
# CLI：旗标与三种输入方式共存、渲染出完整轨迹
# ---------------------------------------------------------------------------


def test_main_with_catalog_path_renders_full_trace(tmp_path: Path) -> None:
    out = tmp_path / "probe.txt"
    code = probe.main(
        [
            "--sample",
            "--catalog-path",
            "RAG核心面（精选） > 一、基础认知与对比",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    text = out.read_text(encoding="utf-8")
    assert (
        "heading_path: RAG核心面（精选） > 一、基础认知与对比 > 管线验证样本" in text
    )


def test_main_without_catalog_path_outputs_no_chain(tmp_path: Path) -> None:
    """缺省渲染：轨迹不含任何目录链层（现状文本）。"""
    out = tmp_path / "probe.txt"
    assert probe.main(["--sample", "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "heading_path: 管线验证样本 > 一、背景" in text
    assert " > 管线验证样本 > 一、背景" not in text


def test_catalog_path_coexists_with_file_input(tmp_path: Path) -> None:
    src = tmp_path / "doc.md"
    src.write_text("# 标题甲\n\n正文。\n", encoding="utf-8")
    out = tmp_path / "probe.txt"
    code = probe.main(
        [str(src), "--catalog-path", "空间 > 目录", "--out", str(out)]
    )
    assert code == 0
    assert "heading_path: 空间 > 目录 > 标题甲" in out.read_text(encoding="utf-8")


def test_input_triplet_discipline_still_enforced(tmp_path: Path) -> None:
    """**两种输入同时给出** → `_resolve_input` 抛「输入须且只须一种」（拒收方向之一；
    零输入 + 仅旗标方向由 `test_no_input_with_catalog_path_exits_2` 承载）。
    `--catalog-path` 旗标不参与输入选择。
    """
    src = tmp_path / "doc.md"
    src.write_text("# 标题甲\n", encoding="utf-8")
    with pytest.raises(probe.ProbeError, match="输入须且只须一种"):
        probe._resolve_input(
            probe._build_parser().parse_args([str(src), "--sample"])
        )


def test_no_input_with_catalog_path_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    """零输入 + 仅有旗标 → 仍按「输入须且只须一种」拒绝（exit 2），旗标不凑数。"""
    assert probe.main(["--catalog-path", "空间 > 目录"]) == 2
    err = capsys.readouterr().err
    assert "输入须且只须一种" in err
