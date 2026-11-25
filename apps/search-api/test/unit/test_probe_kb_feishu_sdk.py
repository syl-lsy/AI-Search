"""探针 `--feishu-sdk` 通道的单测（SDK blocks 直读；假体 client，零网络零 DB）。

验收口径（M3 批次 3 / B20 验收工具补强）：生产导入已走 SDK blocks 通道
（`app/rag/feishu_client.py` + `feishu_mapper.py`），探针须能对同一文档以**同一映射
形态**出树 + 切片视图，与既有 `--feishu-token`（export docx → markitdown）通道并列。

覆盖四条：
① 四选一互斥（`--feishu-sdk` 与 path / `--sample` / `--feishu-token` 冲突 → exit 2）；
② 端到端出 `PipelineOutput`（code 节点 kind=code、table 计数、parse_meta.parser 通道
   标识、summary 按块计数注记、非 PDF 自检节「不适用」、obj_token 掩码 / channel 显式字段）；
③ `truncated=True` 的告警形态：stdout 即时 print + summary 行（text/html 落盘可见）；
④ catalog_path 空白段滤除（与 `--catalog-path` 同口径，含自身标题的链语义）。

假体形态对齐 `test_kb_feishu_mapper.py` 的 SDK Block 假体（lark_oapi 1.8.0 字段面）；
凭据失败面走 `FeishuClientError`（构造即抛，零回显凭据值）→ ProbeError 非 0 退出。
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any

from app.rag.feishu_client import BlockPageResult, FeishuClientError, SheetValuesData
from app.rag.feishu_mapper import PARSER_FEISHU_BLOCKS
from app.rag.ir_schema import IrNode, IrNodeType

_REPO_ROOT = Path(__file__).resolve().parents[4]


def _load_probe() -> Any:
    """按路径装载探针脚本（scripts/ 非包；模块级只做 import，无副作用）。"""
    path = _REPO_ROOT / "scripts" / "probe_kb_pipeline.py"
    spec = importlib.util.spec_from_file_location("probe_kb_pipeline_feishu_sdk_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass 解析 __module__ 需已注册（exec 前）
    spec.loader.exec_module(module)
    return module


probe = _load_probe()

SPACE_ID = "7708408008821771292"
NODE_TOKEN = "WTp2dAqBposqlFxMmmhcnLsCnmf"
OBJ_TOKEN = "doccnABCDEFGHIJKLMN"


# ---------------------------------------------------------------------------
# SDK Block 假体（对齐 lark_oapi 1.8.0：block_id/parent_id/children/block_type + 载荷）
# ---------------------------------------------------------------------------


def _run(content: str) -> Any:
    return type("R", (), {"content": content})()


def _elem(**kw: Any) -> Any:
    base: dict[str, Any] = {
        "text_run": None,
        "mention_doc": None,
        "mention_user": None,
        "equation": None,
        "file": None,
        "inline_block": None,
        "link_preview": None,
        "reminder": None,
    }
    base.update(kw)
    return type("El", (), base)()


def _payload(elements: list[Any] | None = None, style: Any = None) -> Any:
    return type("T", (), {"elements": elements or [], "style": style})()


def _blk(
    bid: str, btype: int, parent: str, children: list[str] | None = None, **fields: Any
) -> Any:
    base: dict[str, Any] = {
        "block_id": bid,
        "parent_id": parent,
        "children": children if children is not None else [],
        "block_type": btype,
    }
    base.update(fields)
    return type("B", (), base)()


def _text_blk(bid: str, content: str, parent: str, children: list[str] | None = None) -> Any:
    return _blk(bid, 2, parent, children, text=_payload([_elem(text_run=_run(content))]))


def _heading_blk(bid: str, content: str, parent: str) -> Any:
    return _blk(bid, 3, parent, heading1=_payload([_elem(text_run=_run(content))]))


def _code_blk(bid: str, content: str, parent: str) -> Any:
    return _blk(bid, 14, parent, code=_payload([_elem(text_run=_run(content))], style=None))


def _table_blk(table_bid: str, parent: str, cells: list[tuple[str, str]]) -> list[Any]:
    """构造 table(31) + table_cell(32) + 格内 paragraph(2) 假体（cells 行主序）。"""
    cell_ids = [cid for cid, _ in cells]
    prop = type("P", (), {"row_size": 1, "column_size": len(cell_ids), "merge_info": None})()
    table = _blk(
        table_bid,
        31,
        parent,
        cell_ids,
        table=type("Tb", (), {"cells": cell_ids, "property": prop})(),
    )
    out: list[Any] = [table]
    for cid, text in cells:
        out.append(_blk(cid, 32, table_bid, [f"{cid}_p"]))
        out.append(_text_blk(f"{cid}_p", text, cid))
    return out


def _blocks() -> list[Any]:
    """page(1) → 标题 / 正文 / 代码 / 2 列表格（显示序权威 = page.children）。"""
    page = _blk(
        "page0001",
        1,
        "",
        ["head0001", "text0001", "code0001", "tab00001"],
    )
    return [
        page,
        _heading_blk("head0001", "第一章 直读", "page0001"),
        _text_blk("text0001", "SDK blocks 直读正文。", "page0001"),
        _code_blk("code0001", "def f():\n    return 1\n", "page0001"),
        *_table_blk("tab00001", "page0001", [("cell0001", "字段"), ("cell0002", "说明")]),
    ]


def _sheet_blk(bid: str, parent: str, token: str) -> Any:
    """sheet(30) 内嵌表格占位块假体（SDK 1.8.0 载荷 Sheet{token, …} 内省形态）。"""
    return _blk(bid, 30, parent, sheet=type("S", (), {"token": token})())


def _blocks_with_sheet(token: str = "shT0kEnABCDEFGHIJ") -> list[Any]:
    """page(1) → 标题 + 正文 + sheet(30) 占位（独立 drive 对象——回填段扫描面；
    正文块保证占位被 chunker 跳过时文档仍有可切分内容——活体文档同形态）。"""
    page = _blk("page0001", 1, "", ["head0001", "text0001", "sheet0001"])
    return [
        page,
        _heading_blk("head0001", "第一章 直读", "page0001"),
        _text_blk("text0001", "SDK blocks 直读正文。", "page0001"),
        _sheet_blk("sheet0001", "page0001", token),
    ]


class _FakeClient:
    """假体 client：list_document_blocks / fetch_sheet_values 返回固定结果（零网络）。"""

    def __init__(
        self,
        items: list[Any],
        *,
        truncated: bool = False,
        sheet_values: SheetValuesData | None = None,
        sheet_error: Exception | None = None,
    ) -> None:
        self.items = items
        self.truncated = truncated
        self.calls: list[str] = []
        self.sheet_values = sheet_values
        self.sheet_error = sheet_error
        self.sheet_calls: list[str] = []

    async def list_document_blocks(self, document_id: str) -> BlockPageResult:
        self.calls.append(document_id)
        return BlockPageResult(items=self.items, truncated=self.truncated)

    async def fetch_sheet_values(self, sheet_token: str) -> SheetValuesData:
        """sheet(30) 回填段桥接假体（与生产 client 同名协程面）。"""
        self.sheet_calls.append(sheet_token)
        if self.sheet_error is not None:
            raise self.sheet_error
        return self.sheet_values if self.sheet_values is not None else SheetValuesData()


def _patch_client(monkeypatch: Any, fake: _FakeClient) -> _FakeClient:
    monkeypatch.setattr(probe, "get_default_feishu_client", lambda: fake)
    return fake


def _walk(node: IrNode) -> list[IrNode]:
    out = [node]
    for child in node.children:
        out.extend(_walk(child))
    return out


# ---------------------------------------------------------------------------
# ① 四选一互斥（与 path / --sample / --feishu-token 冲突 → exit 2）
# ---------------------------------------------------------------------------


def test_sdk_conflicts_with_path_sample_and_export_token(
    tmp_path: Path, capsys: Any
) -> None:
    src = tmp_path / "doc.md"
    src.write_text("# 标题甲\n\n正文。\n", encoding="utf-8")
    conflicts = [
        [str(src), "--feishu-sdk", OBJ_TOKEN],
        ["--sample", "--feishu-sdk", OBJ_TOKEN],
        ["--feishu-token", OBJ_TOKEN, "--feishu-sdk", OBJ_TOKEN],
    ]
    for argv in conflicts:
        capsys.readouterr()  # 清空上一条输出
        assert probe.main(argv) == 2
        err = capsys.readouterr().err
        assert "输入须且只须一种" in err
        assert "--feishu-sdk" in err


def test_sdk_without_token_still_rejected(capsys: Any) -> None:
    """零输入（仅附加旗标）仍拒绝——`--feishu-sdk-node` 等不凑数。"""
    assert probe.main(["--feishu-sdk-node", NODE_TOKEN]) == 2
    assert "输入须且只须一种" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# ② 端到端：SDK blocks → PipelineOutput（生产同映射形态）
# ---------------------------------------------------------------------------


def test_sdk_channel_builds_pipeline_output(monkeypatch: Any) -> None:
    fake = _patch_client(monkeypatch, _FakeClient(_blocks()))
    output = probe.run_feishu_sdk_pipeline(
        OBJ_TOKEN,
        node_token=NODE_TOKEN,
        space_id=SPACE_ID,
        title="标题甲",
        obj_edit_time=1700000000,
    )

    assert fake.calls == [OBJ_TOKEN]
    # obj_token 掩码（对齐 --feishu-token 通道惯例：前 6 位 + ***）
    assert output.source_label == f"feishu-sdk:obj_token={OBJ_TOKEN[:6]}***"
    assert OBJ_TOKEN not in output.source_label
    assert output.channel == "feishu-sdk"
    assert output.truncated is False
    assert output.ext == "docx"
    assert output.byte_size == len(fake.items)  # SDK 通道按块计数
    assert output.outline is None
    assert output.data == b""

    # parse_meta：生产同键面（通道标识 + 计数 + feishu 四键）
    assert output.parse_meta["parser"] == PARSER_FEISHU_BLOCKS
    assert output.parse_meta["pages"] is None
    assert output.parse_meta["tables"] == 1
    assert output.parse_meta["code_blocks"] == 1
    assert output.parse_meta["feishu"] == {
        "space_id": SPACE_ID,
        "node_token": NODE_TOKEN,
        "obj_type": "docx",
        "obj_edit_time": 1700000000,
    }

    # 树：document 根 + heading / paragraph / code / table
    assert output.document.id == f"fs:node:{NODE_TOKEN}"
    nodes = _walk(output.document)
    codes = [node for node in nodes if node.type is IrNodeType.CODE]
    assert len(codes) == 1
    assert codes[0].text == "def f():\n    return 1\n"  # 逐字（不 strip）
    tables = [node for node in nodes if node.type is IrNodeType.TABLE]
    assert len(tables) == 1
    headings = [node for node in nodes if node.type is IrNodeType.HEADING]
    assert [node.title for node in headings] == ["第一章 直读"]

    # 切片视图出片（切分面与生产同一 chunker）
    assert output.chunking.chunks
    assert output.chunking.document_text


def test_sdk_channel_reuses_render_and_main_flow(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """渲染/落盘零新造：main 走既有 render_text + _emit。"""
    _patch_client(monkeypatch, _FakeClient(_blocks()))
    out = tmp_path / "probe.txt"
    code = probe.main(
        [
            "--feishu-sdk",
            OBJ_TOKEN,
            "--feishu-sdk-node",
            NODE_TOKEN,
            "--feishu-sdk-space",
            SPACE_ID,
            "--feishu-sdk-title",
            "标题甲",
            "--feishu-sdk-edit-time",
            "1700000000",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    text = out.read_text(encoding="utf-8")
    assert f"源              : feishu-sdk:obj_token={OBJ_TOKEN[:6]}***" in text
    assert OBJ_TOKEN not in text  # 完整 obj_token 不得落盘
    assert "（SDK 通道按块计数）" in text
    assert f"归一化通道      : {PARSER_FEISHU_BLOCKS}" in text
    assert "不适用（非 PDF 通道）" in text  # 自检节沿非 PDF 口径
    assert f"fs:node:{NODE_TOKEN}" in text


def test_sdk_credentials_error_exits_nonzero(monkeypatch: Any, capsys: Any) -> None:
    """凭据缺失（client 构造即抛 FeishuClientError）→ 非 0 退出 + 中文消息 + 降级提示。"""

    def _boom() -> Any:
        raise FeishuClientError("feishu_settings 缺少 FEISHU_APP_ID / FEISHU_APP_SECRET")

    monkeypatch.setattr(probe, "get_default_feishu_client", _boom)
    assert probe.main(["--feishu-sdk", OBJ_TOKEN]) == 1
    err = capsys.readouterr().err
    assert "飞书 SDK blocks 直读失败" in err
    assert "手动导出" in err  # 飞书失败面降级提示（feishu=True）


# ---------------------------------------------------------------------------
# ③ truncated=True → stdout 显著告警（翻页防御截断）
# ---------------------------------------------------------------------------


def test_sdk_truncated_warns_on_stdout(monkeypatch: Any, capsys: Any) -> None:
    _patch_client(monkeypatch, _FakeClient(_blocks(), truncated=True))
    output = probe.run_feishu_sdk_pipeline(OBJ_TOKEN)
    stdout = capsys.readouterr().out
    assert "truncated=True" in stdout
    assert "截断" in stdout
    # 截断不阻断出树/切片（尾块缺失如实留痕，判定由使用者承担）
    assert output.chunking.chunks
    assert output.truncated is True


def test_sdk_truncated_warning_visible_in_summary_render(
    monkeypatch: Any, capsys: Any
) -> None:
    """截断告警进 summary 块（text 与 html 落盘可见，不只 stdout）。"""
    _patch_client(monkeypatch, _FakeClient(_blocks(), truncated=True))
    output = probe.run_feishu_sdk_pipeline(OBJ_TOKEN)
    capsys.readouterr()  # 丢弃 stdout 即时告警，只验渲染面
    for rendered in (probe.render_text(output), probe.render_html(output)):
        assert "[!] SDK blocks 翻页截断" in rendered
        assert "truncated=True" in rendered
        assert "尾块缺失" in rendered


def test_sdk_not_truncated_has_no_warning(monkeypatch: Any, capsys: Any) -> None:
    _patch_client(monkeypatch, _FakeClient(_blocks(), truncated=False))
    output = probe.run_feishu_sdk_pipeline(OBJ_TOKEN)
    assert "truncated" not in capsys.readouterr().out
    rendered = probe.render_text(output)
    assert "[!] SDK blocks 翻页截断" not in rendered
    assert "truncated=True" not in rendered


# ---------------------------------------------------------------------------
# ④ catalog_path 空白段滤除（与 --catalog-path 同口径，含自身标题）
# ---------------------------------------------------------------------------


def test_sdk_catalog_path_drops_blank_segments(monkeypatch: Any) -> None:
    _patch_client(monkeypatch, _FakeClient(_blocks()))
    # 链语义=从根到本文档、含本文档标题（末位与 title 同名 → chunker 接缝去重不重复）
    output = probe.run_feishu_sdk_pipeline(
        OBJ_TOKEN,
        title="目录乙",
        catalog_path=["空间甲", "   ", "", "目录乙"],
    )
    assert output.document.catalog_path == ["空间甲", "目录乙"]
    # 渲染面：heading_path 轨迹含目录链（空白段不落链）
    text = probe.render_text(output)
    assert "heading_path: 空间甲 > 目录乙 > 第一章 直读" in text


def test_sdk_catalog_path_via_cli_same_parsing(monkeypatch: Any, capsys: Any) -> None:
    """CLI `--catalog-path` 解析与应用面同口径（`_parse_catalog_path` + 空白段滤除）。"""
    assert probe._parse_catalog_path(" 甲 >   > 乙 ") == ["甲", "乙"]
    _patch_client(monkeypatch, _FakeClient(_blocks()))
    assert (
        probe.main(
            [
                "--feishu-sdk",
                OBJ_TOKEN,
                "--feishu-sdk-title",
                "目录乙",
                "--catalog-path",
                " 空间甲 >   > 目录乙",
            ]
        )
        == 0
    )
    assert "heading_path: 空间甲 > 目录乙 > 第一章 直读" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# ⑤ sheet(30) 装配后回填段（生产 `_ingest_docx` 同组合——2026-11-25 活体验收钉正：
#    探针此前缺回填段，sheet 占位不翻转，无法演示生产形态）
# ---------------------------------------------------------------------------


def test_sdk_channel_backfills_sheet_placeholder(monkeypatch: Any) -> None:
    """回填成功：占位翻 false + meta.rows/col_count 物化；回填截断并入
    degraded_blocks（sheet_backfill_truncated）+ parse_meta.feishu.sheet_backfill 留痕。"""
    fake = _patch_client(
        monkeypatch,
        _FakeClient(
            _blocks_with_sheet(),
            sheet_values=SheetValuesData(
                rows=[["表头甲", "表头乙"], ["值甲", "值乙"]],
                truncated_rows=1,
            ),
        ),
    )
    output = probe.run_feishu_sdk_pipeline(OBJ_TOKEN, title="标题甲")

    assert fake.sheet_calls == ["shT0kEnABCDEFGHIJ"]
    tables = [n for n in _walk(output.document) if n.type is IrNodeType.TABLE]
    assert len(tables) == 1
    node = tables[0]
    assert node.meta["placeholder"] is False  # 占位翻转（修复前恒 True=红）
    assert node.meta["rows"] == [["表头甲", "表头乙"], ["值甲", "值乙"]]
    assert node.meta["col_count"] == 2
    assert node.text == "[sheet]"  # 占位字面恒保留（纯文本消费面兜底）
    # 超限截断双留痕：degraded_blocks 增量 + sheet_backfill 计数（0 值省略键口径）
    assert output.parse_meta["feishu"]["degraded_blocks"] == {"sheet_backfill_truncated": 1}
    assert output.parse_meta["feishu"]["sheet_backfill"] == {"truncated_rows": 1}
    assert output.chunking.chunks  # 回填不破坏切分段


def test_sdk_channel_backfill_failure_keeps_placeholder(monkeypatch: Any) -> None:
    """回填失败（FeishuSheetValuesError）：占位保留、不算文档失败、failed_categories
    留痕（活体 category=permission 同形态——探针须能演示该失败分类）。"""
    from app.rag.feishu_client import FeishuSheetValuesError

    fake = _patch_client(
        monkeypatch,
        _FakeClient(
            _blocks_with_sheet(),
            sheet_error=FeishuSheetValuesError("forbidden", category="permission"),
        ),
    )
    output = probe.run_feishu_sdk_pipeline(OBJ_TOKEN, title="标题甲")

    assert fake.sheet_calls == ["shT0kEnABCDEFGHIJ"]
    tables = [n for n in _walk(output.document) if n.type is IrNodeType.TABLE]
    assert len(tables) == 1
    node = tables[0]
    assert node.meta["placeholder"] is True  # 占位保留
    assert "rows" not in node.meta
    assert output.parse_meta["feishu"]["sheet_backfill"] == {
        "failed": 1,
        "failed_categories": {"permission": 1},
    }
    assert "degraded_blocks" not in output.parse_meta["feishu"]  # 失败非降级
    assert output.chunking.chunks  # 文档照常出片
