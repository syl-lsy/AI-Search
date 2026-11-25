"""KB 管线验证探针（开发工具，不进生产链）——归一化 → 树 IR → 逐级切分 实跑核对。

用途：把任意本地文件（或内置小样本 / 飞书导出文档）喂进 `app/rag` 管线的前两段，
进程内直跑、结果落两视图供肉眼验收：
  ① **树 IR 渲染**：四前缀 ID（`fs:node:`/`fs:blk:`/`loc:doc:`/`loc:blk:`）、kind、
     heading level、层级缩进；`<document-ref>` 占位符渲染为可见标记 `◆[document-ref: node_id]`
     （便于核对占位符位置是否被切碎/丢失）；
  ② **chunk 视图**：每片的 chunk_index / heading_path / kind / 字符范围 / content_hash
     前 12 位 / tokens / references 数；相邻片之间画边界线并标注重叠窗字符数与区间
     连续性（overlap 可视化）；
  ③ **标题结构自检（只读验收视图）**：PDF 通道才适用——书签可用性（条数/全顶级）与
     `correction_path`、锚点命中与未命中清单、重复标题分组（top5）、层级跳级（清洗前/
     后、锚点间、非锚点）、被压深条数、章级挂错数、锚点树深偏差。指标由「树 + PDF
     书签」独立重算（不采信管线自报），**只读不改树**；docx/md 等非 PDF 通道显示
     「不适用（非 PDF 通道）」。
  ③.b **小节级伤情单（工单 C v2，只读观察）**：PDF 通道且书签校正启用时在自检节后
     追加五类检测——①书签召回缺失逐条列出（零命中条目标题/层级/目标页）；②标题
     密度分布（heading/千字、heading/页）与超阈值嫌疑清单（单页密集 + 连续簇）；
     ③Fix 2 提升行形态矩阵（已提升 + 未提升候选，句末标点/行数/字数/字号比值，
     句末正则缺口「脱漏」量化；管线留痕 promoted 对照 + 指纹缺口自诊断）；
     ④formula 空 text 候选计数与标题占比（Fix 1 通道活跃度）；⑤无书签显著警示
     （版面推断主导时伤情单第一行）。**非 PDF 通道与关闭态（KB_PDF_OUTLINE_CORRECTION
     =false）零输出变化**（伤情单整节不出现）。

零外部依赖面：不碰 DB / MinIO / Nacos / 上传流水线编排——直接调 normalizers registry
与 chunker；配置取 `config_center.defaults.SPEC_CONFIG` 的 kb 节缺省（等价「键缺失
回落代码缺省」的灰度安全语义），不读运行时资产中心快照。

用法（全部在仓库根执行；命令统一 `uv run --package search-api`）：

    # 1) 本地文件 → 树 + chunks（text，stdout）
    uv run --package search-api python scripts/probe_kb_pipeline.py README.md

    # 2) 本地文件（HTML 输入亦可；输出走 html 双栏对照）
    uv run --package search-api python scripts/probe_kb_pipeline.py docs/design/tokens.css.html \\
        --format html --out out/kb_probe.html

    # 3) 内置小样本（两个标题层级 + 代码块 + 3 行表格 + 图片 document-ref 占位符）
    uv run --package search-api python scripts/probe_kb_pipeline.py --sample

    # 4) 内置样本 → 自包含 HTML（无外部依赖，浏览器直接打开；缺省落 out/kb_probe.html）
    uv run --package search-api python scripts/probe_kb_pipeline.py --sample --format html

    # 5) 飞书文档（obj_token）→ export task 通道取 docx → 同一管线
    uv run --package search-api python scripts/probe_kb_pipeline.py --feishu-token <obj_token>

    # 5.b) 飞书 docx（obj_token）→ SDK blocks 直读通道（生产导入同映射形态：
    #      list_document_blocks → map_blocks → 树 IR → 同一 chunker）
    uv run --package search-api python scripts/probe_kb_pipeline.py --feishu-sdk <obj_token>

    # 6) 演示完整目录链轨迹（可选附加旗标，可与上面任一输入方式共存）
    uv run --package search-api python scripts/probe_kb_pipeline.py out/x.docx \\
        --catalog-path "RAG核心面（精选） > 一、基础认知与对比"

退出码：0=成功；1=管线/飞书失败；2=参数或凭据缺失。

凭据纪律：飞书凭据只从仓库根 `.env`（`FEISHU_APP_ID`/`FEISHU_APP_SECRET`，可选
`FEISHU_API_BASE`）读取；tenant_access_token 与密钥**绝不打印**；飞书失败时如实报错
并提示降级用法「先在飞书手动导出 docx，再把本地路径传给本脚本」。

飞书通道参考形态：2026-11-23 第二阶段 V5 探针实证（`docs/research/m3-probe-feishu-v5v6-20261122.md`
§2/【复测注】）——创建 export task → 轮询 job_status=0 → 下载产物（即 docx 本体，OOXML zip 容器）。

两条飞书通道并存：`--feishu-token`（export docx → markitdown 归一化，M3 前接线形态）与
`--feishu-sdk`（SDK blocks 直读 → `feishu_mapper` 映射 → sheet(30) 装配后回填段
（`backfill_sheet_placeholders`），**生产导入现行形态**，B20 A 线）。后者与
`app/rag/import_worker.py` `_ingest_docx` 逐字同组合（**含回填段**——2026-11-25 活体
验收钉正：缺回填段时 sheet 占位不翻转、回填失败分类不落 parse_meta，探针无法演示生产
形态），用于对同一文档比对两条通道的树/切片视图差异。
"""

from __future__ import annotations

import argparse
import asyncio
import html
import io
import re
import sys
import time
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config_center.defaults import SPEC_CONFIG
from app.exception.base import BusinessException
from app.rag.chunker import Chunk, ChunkingConfig, ChunkingResult, chunk_forest
from app.rag.feishu_client import (
    FeishuClientError,
    SheetValuesData,
    get_default_feishu_client,
)
from app.rag.feishu_mapper import (
    PARSER_FEISHU_BLOCKS,
    build_document_node,
    build_feishu_parse_meta,
    map_blocks,
)
from app.rag.import_worker import SheetBackfillReport, backfill_sheet_placeholders
from app.rag.ir_schema import (
    DOCUMENT_REF_TEMPLATE,
    IrNode,
    IrNodeType,
    find_document_refs,
)
from app.rag.normalizers.heading_outline import (
    CORRECTION_RAW,
    CORRECTION_SEQUENCE_CLEAN,
    Anchor,
    HeadingItem,
    OutlineEntry,
    count_level_jumps,
    duplicate_title_report,
    read_pdf_outline,
    resolve_anchors,
)
from app.rag.normalizers.pdf_textlayer import CharFontSampler, open_pdf_bytes
from app.rag.normalizers.registry import normalize_document, read_normalizer_settings
from app.rag.tree_assembler import attach_block_tree

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = REPO_ROOT / ".env"
DEFAULT_HTML_OUT = Path("out") / "kb_probe.html"
SAMPLE_EXT = "md"

#: 占位符可见标记前缀（text 视图；html 视图另起 <mark> 高亮）
REF_MARK = "◆[document-ref: {node_id}]"

#: 飞书导出通道参数（沿 V5 探针实测值：2s 轮询、120s 死线）
FEISHU_POLL_INTERVAL_S = 2.0
FEISHU_POLL_DEADLINE_S = 120.0
FEISHU_TIMEOUT_S = 20.0
FEISHU_EXPORT_POLL_TERMINAL_ERR = {
    1: "初始化未完成（继续轮询）",
    2: "处理中（继续轮询）",
    3: "内部错误",
    107: "文档过大",
    108: "导出超时",
    109: "无权限",
    110: "无权限",
    111: "文档已删除",
    6000: "图片过多",
}

#: SDK blocks 直读通道：源标签前缀（summary 行「按块计数」注记的判定依据）
SDK_LABEL_PREFIX = "feishu-sdk:"
#: SDK 通道缺省空间 / 标题（探针无同步目录，取示意值；语义同生产 build_document_node 入参）
SDK_DEFAULT_SPACE = "probe"
SDK_DEFAULT_TITLE = "feishu-sdk"
#: SDK 通道文档类型（build_document_node 要求 ∈ DOC_KINDS；B20 只走 docx 直读）
SDK_OBJ_TYPE = "docx"

#: 内置样本：两个标题层级 + 一段带图片占位符的正文 + 代码块 + 3 行表格。
SAMPLE_MARKDOWN = """# 管线验证样本

本样本用于肉眼核对「归一化 → 树 IR → 逐级切分」三段链路：标题层级、代码块、
表格，以及 document-ref 图片占位符（切分时应保持原子、不跨块）。

## 一、背景

段落文字用于验证段落级切分与 heading_path 物化。下图占位符由切分前的 PUA 保护
协议保护，不得被切碎：<document-ref id="fs:blk:0007" kind="image">图 1 架构示意</document-ref>

## 二、示例

```python
def hello(name: str) -> str:
    return f"hello, {name}"
```

| 字段 | 类型 | 说明 |
|---|---|---|
| id | string | 节点 ID（四前缀之一） |
| kind | string | 统一 type 枚举值 |
| text | string | 文本载荷（占位符原样） |

## 三、收尾

最后一段短正文，用于观察末片边界（无重叠窗）。
"""


class ProbeError(Exception):
    """探针自身可读错误（参数/凭据/环境缺失 → 2；飞书运行期失败 → 1）。"""

    def __init__(
        self, message: str, *, exit_code: int = 2, feishu: bool = False
    ) -> None:
        self.message = message
        self.exit_code = exit_code
        self.feishu = feishu
        super().__init__(message)


# ---------------------------------------------------------------------------
# 1) 管线：bytes → 文档树 → chunks（纯进程内，无 DB/MinIO/Nacos）
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PipelineOutput:
    """一次管线运行的产出（渲染层只读本结构）。"""

    source_label: str
    ext: str
    byte_size: int
    document: IrNode
    parse_meta: dict[str, Any]
    chunking: ChunkingResult
    config: ChunkingConfig
    #: PDF 通道的书签大纲（自检节用；非 PDF / 读取失败 → None）
    outline: list[OutlineEntry] | None = None
    #: 原始输入字节（伤情单 ③ 字号比值重采样用；渲染层只读，None=不重采样）
    data: bytes | None = None
    #: 取数通道标识（`file` / `feishu-sdk`；summary 按通道注记，不做标签前缀匹配）
    channel: str = "file"
    #: SDK blocks 翻页防御截断（True=尾块缺失，树/切片视图不完整）
    truncated: bool = False


def run_pipeline(
    data: bytes,
    ext: str,
    source_label: str,
    *,
    catalog_path: Sequence[str] = (),
) -> PipelineOutput:
    """归一化（registry 路由 + 适配器）→ 单文档树 → chunker 逐级切分。

    `catalog_path`：演示用知识库目录链（从根到本文档节点、含本文档节点标题；
    `--catalog-path "A > B"` 解析而来）。空白段（纯空白）在消费面滤除（不是标题、
    落进链会污染轨迹）。缺省空 = 现状零变化——归一化产物本不带目录链（上传通道
    恒空），此处按需写入 document 根节点再切分，用于在真标本上演示「目录链 +
    文档内标题路径」的完整轨迹。
    """
    settings = read_normalizer_settings(SPEC_CONFIG)  # 配置缺省面（不读资产中心快照）
    normalized = normalize_document(data, ext, settings=settings)
    # 空白段（纯空白）不是标题、落进链会污染轨迹与将来 ES heading_path 字段——
    # 消费面兜底滤除（与「链值不二次清洗」不冲突：只丢空白段，不重写真实标题内容）
    chain = [segment for segment in catalog_path if segment.strip()]
    if chain:
        normalized.document.catalog_path = chain
    config = ChunkingConfig.from_spec(SPEC_CONFIG)
    results = chunk_forest(normalized.document, config=config)
    if len(results) != 1:
        raise ProbeError(
            f"单文档管线期望 1 个切分结果，实得 {len(results)}（归一化产物形态异常）"
        )
    return PipelineOutput(
        source_label=source_label,
        ext=ext,
        byte_size=len(data),
        document=normalized.document,
        parse_meta=normalized.parse_meta,
        chunking=results[0],
        config=config,
        outline=_read_outline(data, ext),
        data=data,
    )


def _parse_catalog_path(value: str | None) -> list[str]:
    """`--catalog-path "A > B > C"` → ["A", "B", "C"]（`>` 分隔；段首尾空白剥除、
    空段跳过；缺省/空白/全空段 → []，等价不设）。"""
    if not value:
        return []
    return [segment.strip() for segment in value.split(">") if segment.strip()]


def _read_outline(data: bytes, ext: str) -> list[OutlineEntry] | None:
    """PDF 输入 → 书签大纲（自检只读面；读取失败降级 None，不阻塞管线实跑）。"""
    if ext.lstrip(".").lower() != "pdf":
        return None
    try:
        return read_pdf_outline(data)
    except Exception:  # noqa: BLE001 —— 自检面降级（坏字节/pdfium 不可用）
        return None


# ---------------------------------------------------------------------------
# 2) 占位符可见化（text / html 两形态；读取面复用 ir_schema 的解析与模板）
# ---------------------------------------------------------------------------


def _iter_placeholders(text: str) -> list[tuple[str, str, str, str]]:
    """返回 ((原占位符文本, node_id, kind, label), ...)——原文本供精确替换。"""
    out: list[tuple[str, str, str, str]] = []
    for ref in find_document_refs(text):
        raw = DOCUMENT_REF_TEMPLATE.format(
            node_id=ref.node_id, kind=ref.kind, label=ref.label
        )
        out.append((raw, ref.node_id, ref.kind, ref.label))
    return out


def placeholder_text(text: str) -> str:
    """text 视图：`<document-ref …>` → `◆[document-ref: node_id]`（其余原样）。"""
    for raw, node_id, _kind, _label in _iter_placeholders(text):
        text = text.replace(raw, REF_MARK.format(node_id=node_id))
    return text


def placeholder_html(text: str) -> str:
    """html 视图：占位符高亮为 `<mark>`（先换哨兵再转义，避免转义吞掉标签）。"""
    sentinels: dict[str, str] = {}
    for index, (raw, node_id, kind, label) in enumerate(_iter_placeholders(text)):
        token = f"\x00__KB_REF_{index}__\x00"
        sentinels[token] = (
            '<mark class="ref" title="kind={kind} · {label}">{mark}</mark>'.format(
                kind=html.escape(kind),
                label=html.escape(label),
                mark=html.escape(REF_MARK.format(node_id=node_id)),
            )
        )
        text = text.replace(raw, token)
    escaped = html.escape(text)
    for token, fragment in sentinels.items():
        escaped = escaped.replace(html.escape(token), fragment)
    return escaped


def _snippet(text: str | None, limit: int = 96) -> str:
    """单行摘要：占位符可见化 + 空白折叠 + 截断。"""
    if not text:
        return ""
    flat = re.sub(r"\s+", " ", placeholder_text(text)).strip()
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# 3) 树渲染
# ---------------------------------------------------------------------------


def _node_line(node: IrNode, depth: int) -> str:
    parts = [f"{'  ' * depth}[{node.id}] {node.type.value}"]
    if node.type is IrNodeType.HEADING and node.level is not None:
        parts.append(f"level={node.level}")
    if node.doc_kind:
        parts.append(f"doc_kind={node.doc_kind}")
    raw_type = node.meta.get("raw_type") if isinstance(node.meta, dict) else None
    if raw_type:
        parts.append(f"raw_type={raw_type}")
    if node.title:
        parts.append(f"title={placeholder_text(node.title)!r}")
    if node.text:
        parts.append(f"text={_snippet(node.text)!r}")
    if node.loc is not None:
        parts.append(f"loc.origin={node.loc.origin}")
    return " ".join(parts)


def render_tree_text(document: IrNode) -> list[str]:
    lines: list[str] = []
    stack: list[tuple[IrNode, int]] = [(document, 0)]
    while stack:
        node, depth = stack.pop()
        lines.append(_node_line(node, depth))
        for child in reversed(node.children):
            stack.append((child, depth + 1))
    return lines


def render_tree_html(document: IrNode) -> str:
    rows: list[str] = []
    stack: list[tuple[IrNode, int]] = [(document, 0)]
    while stack:
        node, depth = stack.pop()
        label = _node_line(node, depth).replace("  " * depth, "", 1)
        kind = html.escape(node.type.value)
        rows.append(
            f'<div class="node" style="--d:{depth}" data-kind="{kind}">'
            f"{placeholder_html(label)}</div>"
        )
        for child in reversed(node.children):
            stack.append((child, depth + 1))
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# 4) chunk 视图（含边界与重叠窗可视化）
# ---------------------------------------------------------------------------


def _overlap_chars(prev: str, nxt: str, *, limit: int = 512) -> int:
    """前片尾 ≡ 后片头 的最长公共长度（重叠窗字符数；无重叠为 0）。"""
    max_len = min(len(prev), len(nxt), limit)
    for size in range(max_len, 0, -1):
        if prev.endswith(nxt[:size]):
            return size
    return 0


def _chunk_header(chunk: Chunk) -> str:
    heading_path = " > ".join(chunk.heading_path) if chunk.heading_path else "（无）"
    oversized = " oversized=yes" if chunk.oversized else ""
    return (
        f"#{chunk.chunk_index} kind={chunk.kind} chars=[{chunk.char_start},{chunk.char_end})"
        f" tokens={chunk.token_count} hash={chunk.content_hash[:12]}"
        f" refs={len(chunk.references)} node_id={chunk.node_id}{oversized}\n"
        f"    heading_path: {heading_path}"
    )


def render_chunks_text(output: PipelineOutput) -> list[str]:
    chunks = output.chunking.chunks
    overlap_target = output.config.chunk_overlap
    lines: list[str] = []
    for index, chunk in enumerate(chunks):
        lines.append(_chunk_header(chunk))
        lines.append(f"    预览: {_snippet(chunk.content, 140)}")
        if chunk.references:
            refs = ", ".join(
                f"{ref.get('node_id', '?')}({ref.get('kind', '?')})"
                for ref in chunk.references
            )
            lines.append(f"    引用占位符: {refs}")
        if index + 1 < len(chunks):
            nxt = chunks[index + 1]
            overlap = _overlap_chars(chunk.content, nxt.content)
            gap = nxt.char_start - chunk.char_end
            continuity = "区间连续" if gap == 0 else f"区间间隙 {gap} 字符"
            lines.append(
                f"  ── 边界 #{index}→#{index + 1}：重叠窗 {overlap} 字符"
                f"（配置 chunk_overlap={overlap_target}） / {continuity} ──"
            )
    return lines


_BOUNDARY_TPL = (
    '<div class="boundary">边界 #{i}→#{j} · 重叠窗 {overlap} 字符'
    '（配置 chunk_overlap={target}） · {continuity}</div>'
)


def render_chunks_html(output: PipelineOutput) -> str:
    chunks = output.chunking.chunks
    target = output.config.chunk_overlap
    parts: list[str] = []
    for index, chunk in enumerate(chunks):
        parts.append('<section class="chunk">')
        parts.append(f"<h3>{placeholder_html(_chunk_header(chunk))}</h3>")
        if chunk.references:
            refs = ", ".join(
                f"{ref.get('node_id', '?')}({ref.get('kind', '?')})"
                for ref in chunk.references
            )
            parts.append(f'<p class="refs">引用占位符: {html.escape(refs)}</p>')
        parts.append(f"<pre>{placeholder_html(chunk.content)}</pre>")
        parts.append("</section>")
        if index + 1 < len(chunks):
            nxt = chunks[index + 1]
            overlap = _overlap_chars(chunk.content, nxt.content)
            gap = nxt.char_start - chunk.char_end
            parts.append(
                _BOUNDARY_TPL.format(
                    i=index,
                    j=index + 1,
                    overlap=overlap,
                    target=target,
                    continuity="区间连续" if gap == 0 else f"区间间隙 {gap} 字符",
                )
            )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 5) 标题结构自检（只读验收视图——校正发生在管线内，此处只重算指标）
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StructureSelfCheck:
    """标题结构自检指标块（PDF 书签面；非 PDF 通道显式不适用）。"""

    applicable: bool
    note: str | None = None
    correction_path: str | None = None
    bookmarks: int = 0
    all_top_level: bool = False
    anchor_hits: int = 0
    unmatched: list[str] = field(default_factory=list)
    duplicate_groups: int = 0
    duplicate_extra: int = 0
    duplicate_top: list[tuple[str, int]] = field(default_factory=list)
    level_jumps: int = 0
    level_jumps_before: int | None = None
    anchor_level_jumps: int = 0
    non_anchor_jumps: int = 0
    pressed: int | None = None
    pressed_titles: list[str] = field(default_factory=list)
    mis_mounted: int = 0
    anchor_depth_mismatch: int = 0
    max_anchor_depth: int | None = None


def _node_page(node: IrNode) -> int | None:
    loc = node.loc
    page = getattr(loc, "page", None) if loc is not None else None
    return page if isinstance(page, int) else None


def _collect_headings(
    document: IrNode,
) -> tuple[list[HeadingItem], list[int], list[list[int]]]:
    """树 → (heading 序列, 各自树深, 各自祖先 heading 下标)；文档序 pre-order。"""
    headings: list[HeadingItem] = []
    depths: list[int] = []
    ancestors: list[list[int]] = []
    open_chain: list[tuple[int, int]] = []  # (depth, heading 下标)
    stack: list[tuple[IrNode, int]] = [(child, 1) for child in reversed(document.children)]
    while stack:
        node, depth = stack.pop()
        while open_chain and open_chain[-1][0] >= depth:
            open_chain.pop()
        if node.type is IrNodeType.HEADING:
            ancestors.append([index for _d, index in open_chain])
            depths.append(depth)
            headings.append(
                HeadingItem(
                    title=node.title or "",
                    level=node.level or 1,
                    page=_node_page(node),
                )
            )
            open_chain.append((depth, len(headings) - 1))
        for child in reversed(node.children):
            stack.append((child, depth + 1))
    return headings, depths, ancestors


def build_structure_check(output: PipelineOutput) -> StructureSelfCheck:
    """管线产物 + PDF 书签 → 结构自检指标（只读；不碰树、不改 parse_meta）。

    书签面口径（多级书签适用）：
    - 章级挂错 = 锚点的**最近祖先锚点** outline_level ≥ 本锚点 outline_level（同级/更浅
      的章被塞进另一个章子树里才是错；合法的父子嵌套不算）；
    - 锚点树深 = 逐锚点比对 `outline_level − 顶层 + 1`（顶层由书签最小层级归一）。
    """
    meta = output.parse_meta.get("heading_outline")
    path = meta.get("correction_path") if isinstance(meta, dict) else None
    jumps_before = meta.get("level_jumps_before") if isinstance(meta, dict) else None
    pressed = meta.get("pressed") if isinstance(meta, dict) else None
    pressed_sample = meta.get("pressed_sample") if isinstance(meta, dict) else None
    if output.ext.lstrip(".").lower() != "pdf":
        return StructureSelfCheck(
            applicable=False, note="不适用（非 PDF 通道）", correction_path=None
        )

    headings, depths, ancestors = _collect_headings(output.document)
    levels = [item.level for item in headings]
    duplicates = duplicate_title_report(headings)
    level_jumps = count_level_jumps(levels)
    if not output.outline:
        return StructureSelfCheck(
            applicable=False,
            note="书签不可读或为空（PDF 无书签 / 解析失败 / pdfium 不可用）",
            correction_path=path if isinstance(path, str) else None,
            level_jumps=level_jumps,
            level_jumps_before=jumps_before if isinstance(jumps_before, int) else None,
            duplicate_groups=len(duplicates),
            duplicate_extra=sum(count - 1 for _title, count in duplicates),
            duplicate_top=duplicates[:5],
        )

    anchors, unmatched = resolve_anchors(headings, output.outline)
    anchor_by_index = {anchor.index: anchor for anchor in anchors}
    top_level = min(entry.level for entry in output.outline)
    mis_mounted = 0
    depth_mismatch = 0
    for anchor in anchors:
        nearest: Anchor | None = None
        for ancestor in reversed(ancestors[anchor.index]):
            nearest = anchor_by_index.get(ancestor)
            if nearest is not None:
                break
        if nearest is not None and nearest.outline_level >= anchor.outline_level:
            mis_mounted += 1
        if depths[anchor.index] != anchor.outline_level + 1:
            depth_mismatch += 1
    non_anchor_jumps = sum(
        1
        for index in range(len(headings) - 1)
        if levels[index + 1] - levels[index] > 1 and (index + 1) not in anchor_by_index
    )
    return StructureSelfCheck(
        applicable=True,
        correction_path=path if isinstance(path, str) else None,
        bookmarks=len(output.outline),
        all_top_level=all(entry.level == top_level for entry in output.outline),
        anchor_hits=len(anchors),
        unmatched=list(unmatched),
        duplicate_groups=len(duplicates),
        duplicate_extra=sum(count - 1 for _title, count in duplicates),
        duplicate_top=duplicates[:5],
        level_jumps=level_jumps,
        level_jumps_before=jumps_before if isinstance(jumps_before, int) else None,
        anchor_level_jumps=count_level_jumps(
            [headings[index].level for index in sorted(anchor_by_index)]
        ),
        non_anchor_jumps=non_anchor_jumps,
        pressed=pressed if isinstance(pressed, int) else None,
        pressed_titles=[
            str(title) for title in pressed_sample
        ] if isinstance(pressed_sample, list) else [],
        mis_mounted=mis_mounted,
        anchor_depth_mismatch=depth_mismatch,
        max_anchor_depth=max(
            (depths[index] for index in anchor_by_index), default=None
        ),
    )


def render_structure_check_text(check: StructureSelfCheck) -> list[str]:
    """text 视图指标块（③ 节）。"""
    header = "--- ③ 标题结构自检（只读验收视图；层级校正在管线内完成）---"
    if not check.applicable:
        lines = [header, "", f"        : {check.note}"]
        if check.correction_path is not None or check.level_jumps_before is not None:
            before = "—" if check.level_jumps_before is None else f"{check.level_jumps_before} 处"
            lines.append(
                f"        : correction_path={check.correction_path or '未启用'}"
                f" · 清洗前跳级 {before} / 清洗后 {check.level_jumps} 处"
            )
        return lines
    top = "、".join(f"{title}×{count}" for title, count in check.duplicate_top) or "—"
    unmatched = "、".join(check.unmatched) or "—"
    before = "—" if check.level_jumps_before is None else f"{check.level_jumps_before} 处"
    pressed = "—" if check.pressed is None else f"{check.pressed} 条"
    sample = "、".join(check.pressed_titles[:5]) or "—"
    depth = "—" if check.max_anchor_depth is None else str(check.max_anchor_depth)
    return [
        header,
        "",
        f"书签可用性    : {check.bookmarks} 条（全顶级={'是' if check.all_top_level else '否'}）"
        f" · correction_path={check.correction_path or '未启用'}",
        f"锚点匹配      : {check.anchor_hits} / {check.bookmarks} 书签命中；未命中：{unmatched}",
        f"标题重复      : {check.duplicate_groups} 组（多出 {check.duplicate_extra} 个节点）"
        f"；top5: {top}",
        f"层级跳级      : {check.level_jumps} 处（清洗前 {before}；"
        f"锚点间跳级 {check.anchor_level_jumps} 处；非锚点跳级 {check.non_anchor_jumps} 处）",
        f"被压深        : {pressed}（管线留痕）；前 5: {sample}",
        f"章级挂错      : {check.mis_mounted} 处（最近祖先锚点层级 ≥ 本锚点）",
        f"锚点树深      : 偏差 {check.anchor_depth_mismatch} 处"
        f"（期望=outline_level−顶层+1；实测最大 {depth}）",
    ]


def render_structure_check_html(check: StructureSelfCheck) -> str:
    """html 视图指标块（与 text 视图同源同文案）。"""
    body = "\n".join(
        html.escape(line) for line in render_structure_check_text(check)
    )
    return f'<section class="selfcheck"><pre>{body}</pre></section>'


# ---------------------------------------------------------------------------
# 5b) 小节级伤情单（工单 C v2；只读观察——五类检测机器先于用户抓异常）
# ---------------------------------------------------------------------------

#: ② 单页密集阈值：同页 heading 数 ≥ 此值 → 嫌疑页（heading/页 轴）。
#: Nacos 式可调常量——探针零运行时资产中心依赖，按三分法收敛模块常量；
#: `build_injury_report` 的同名 kwarg 供会话级/测试覆写。
INJURY_DENSITY_PAGE_SUSPECT = 3
#: ② 连续簇阈值：文档序连续 heading 条数 ≥ 此值 → 检查簇内正文间隔合计
#: （heading/千字 轴的局部形态；治「修改为：」×5 类假阳性的主要抓手）。
INJURY_DENSITY_CLUSTER_MIN = 4
#: ② 连续簇阈值：相邻 heading 之间正文量合计 ≤ 此值（字）→ 判「标题扎堆」。
INJURY_DENSITY_CLUSTER_MAX_CHARS = 300

#: ③ Fix 2 提升通道的树内指纹：提升块在装配时保留 docling 原始 label 进
#: meta.raw_type（`font_promoted` 来源会被后续分级/校正覆写，raw_type 不会）——
#: 原生标题是 section_header/title、OCR 行是 ocr_line，互不重叠。
_PROMOTED_RAW_TYPES = frozenset({"text", "formula"})
#: ③ 句末标点正则面（与 docling_adapter._PROMOTE_SENT_END `[。：:，,；;.]$` 逐字符
#: 对齐——parity 由 test_probe_kb_injury_report 锁死，防两处漂移；工单 B ④ 补
#: 英文句点「.」——句末「.」结尾大字行不得提升，两处同批）。
_PROMOTE_SENT_END_CHARS = "。：:，,；;."
#: ③ 句末标点超集 = 正则面 ∪ 中英文缺口字符（！?…等）——落在超集而正则面不含
#: 即「脱漏」，把 P3-1 句末正则缺口量化暴露成伤情单行。
_SENTENCE_ENDING_ALL = "。．.！!？?；;，,：:…"
#: ④ Fix 1 通道树内指纹：docling formula 条目 text 恒空、原文只在 orig
#: （_item_text 回落补写）——树内 raw_type=formula 的节点即该通道的全部活动面。
_FORMULA_RAW_TYPE = "formula"
#: ③ 未提升候选的形态筛选（与 docling_adapter 提升候选形状对齐：单行、≤40 字；
#: 句末标点**不筛**——「因句末标点被拒」正是要暴露的形态信息）。
_INJURY_CANDIDATE_MAX_CHARS = 40
#: ③ 形态矩阵行数展示上限（超过截断计数；防病态树撑爆伤情单）。
_INJURY_MATRIX_ROW_LIMIT = 50
#: 伤情单清单/样例展示截断（①缺失逐条不截、②③④的标题预览与样例截前 N 条）
_INJURY_SAMPLE_LIMIT = 5

#: ⑤ 无书签显著警示触发路径：校正启用但书签未主导（序列清洗/裸路径）——
#: 版面推断主导、层级可信度降级。关闭态（键整缺）不触发（零变化门禁）。
_INJURY_LAYOUT_ONLY_PATHS = (CORRECTION_SEQUENCE_CLEAN, CORRECTION_RAW)


@dataclass(slots=True)
class MissingBookmark:
    """① 书签召回缺失条目：零命中的书签原文 + 原始层级 + 目标页（0 基）。"""

    title: str
    level: int
    page: int | None


@dataclass(slots=True)
class DensitySuspect:
    """② 密度嫌疑：kind=page（单页密集）| cluster（连续簇）。"""

    kind: str
    page: int | None
    count: int
    span_chars: int | None
    pages: tuple[int, int] | None
    titles: list[str]


@dataclass(slots=True)
class PromotedRow:
    """③ 提升行形态：句末标点/行数/字数/字号比值（比值不可采样 → None）。

    `promoted=False` 为未提升候选（text/formula 短行段——Fix 2 候选形状但未被
    提升；其形态列同样输出，用于归因「为何未提升」：句末标点拦截 / 字号未命中）。
    """

    title: str
    chars: int
    lines: int
    ending: str | None
    gap: bool
    ratio: float | None
    promoted: bool = True


@dataclass(slots=True)
class InjuryReport:
    """小节级伤情单指标块（五类检测；渲染与指标分离）。"""

    correction_path: str
    no_bookmark_warning: str | None
    missing_available: bool
    missing: list[MissingBookmark]
    headings: int
    body_chars: int
    per_1k: float | None
    pages: int | None
    per_page: float | None
    density_suspects: list[DensitySuspect]
    promoted: list[PromotedRow]
    #: 管线自报的提升条数（parse_meta.heading_heuristic.promoted；缺失 → None）
    promoted_pipeline: int | None
    #: 指纹缺口警示：管线留痕 promoted > 树内指纹已提升行数时非空（留痕与树面矛盾，
    ##  提升行形态在树内不可归因——观测面自诊断，工单 B 复核面）
    promotion_gap_warning: str | None
    formula_total: int
    formula_headings: int
    formula_empty: int
    formula_samples: list[str]


def _iter_nodes(document: IrNode) -> Any:
    """文档序 pre-order 迭代（含根下全部后代）。"""
    stack = list(reversed(document.children))
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def _node_body_volume(node: IrNode) -> int:
    """非 heading 节点的正文量（字）：text + 表格逐格字符（簇间隔体积的近似）。"""
    volume = len(node.text or "")
    rows = node.meta.get("rows")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, list):
                volume += sum(len(str(cell)) for cell in row)
    return volume


def _walk_headings(document: IrNode) -> tuple[list[IrNode], list[int]]:
    """文档序 heading 节点 + 各 heading 之前的正文量（gaps[i]=heading i 前的间隔；
    相邻 heading 间隔 = sum(gaps[i+1:j])）。"""
    headings: list[IrNode] = []
    gaps: list[int] = []
    acc = 0
    for node in _iter_nodes(document):
        if node.type is IrNodeType.HEADING:
            headings.append(node)
            gaps.append(acc)
            acc = 0
        else:
            acc += _node_body_volume(node)
    return headings, gaps


def _missing_bookmark_entries(
    outline: Sequence[OutlineEntry], unmatched: Sequence[str]
) -> list[MissingBookmark]:
    """未命中书签标题 → 逐条缺失条目（标题 + 原始层级 + 目标页 0 基）。"""
    remaining = list(unmatched)
    entries: list[MissingBookmark] = []
    for entry in outline:
        if entry.title in remaining:
            entries.append(
                MissingBookmark(title=entry.title, level=entry.level, page=entry.page)
            )
            remaining.remove(entry.title)
            if not remaining:
                break
    return entries


def _density_suspects(
    headings: Sequence[IrNode],
    gaps: Sequence[int],
    *,
    page_suspect: int,
    cluster_min: int,
    cluster_max_chars: int,
) -> list[DensitySuspect]:
    """② 密度嫌疑清单：单页密集（heading/页 轴）+ 连续簇（char 轴局部密度）。"""
    suspects: list[DensitySuspect] = []
    by_page: dict[int, list[IrNode]] = {}
    for node in headings:
        page = _node_page(node)
        if page is not None:
            by_page.setdefault(page, []).append(node)
    for page in sorted(by_page):
        nodes = by_page[page]
        if len(nodes) >= page_suspect:
            suspects.append(
                DensitySuspect(
                    kind="page",
                    page=page,
                    count=len(nodes),
                    span_chars=None,
                    pages=(page, page),
                    titles=[node.title or "" for node in nodes[:_INJURY_SAMPLE_LIMIT]],
                )
            )
    total = len(headings)
    if total >= cluster_min:
        start = 0
        while start + cluster_min <= total:
            span = sum(gaps[start + 1 : start + cluster_min])
            if span > cluster_max_chars:
                start += 1
                continue
            end = start + cluster_min
            while (
                end < total
                and span + gaps[end] <= cluster_max_chars
            ):
                span += gaps[end]
                end += 1
            first_page = _node_page(headings[start])
            last_page = _node_page(headings[end - 1])
            pages = (
                (first_page, last_page)
                if first_page is not None and last_page is not None
                else None
            )
            suspects.append(
                DensitySuspect(
                    kind="cluster",
                    page=None,
                    count=end - start,
                    span_chars=span,
                    pages=pages,
                    titles=[
                        node.title or ""
                        for node in headings[start:end][: _INJURY_SAMPLE_LIMIT]
                    ],
                )
            )
            start = end
    return suspects


def _promotion_channel(document: IrNode) -> list[tuple[IrNode, bool]]:
    """③ 提升通道全集（文档序）：`(节点, 是否已提升)`。

    已提升 = heading 且 raw_type∈{text,formula}（提升块装配时保留 docling 原始
    label——`font_promoted` 来源会被分级/校正覆写，raw_type 不会，故为唯一树内指纹）；
    未提升候选 = 同指纹的 paragraph 且单行 ≤ `_INJURY_CANDIDATE_MAX_CHARS` 字
    （Fix 2 候选形状，与 docling_adapter 对齐；句末标点不筛——「因句末标点被拒」
    正是要暴露的形态）。
    """
    channel: list[tuple[IrNode, bool]] = []
    for node in _iter_nodes(document):
        if node.meta.get("raw_type") not in _PROMOTED_RAW_TYPES:
            continue
        if node.type is IrNodeType.HEADING:
            channel.append((node, True))
            continue
        payload = (node.text or "") or (node.title or "")
        if payload and "\n" not in payload and len(payload) <= _INJURY_CANDIDATE_MAX_CHARS:
            channel.append((node, False))
    return channel


def _promoted_rows(
    channel: Sequence[tuple[IrNode, bool]],
    *,
    sampler: Any,
    body_font_size: float | None,
) -> list[PromotedRow]:
    """③ 提升行形态矩阵：字数/行数/句末标点/脱漏标记/字号比值（重采样）。

    已提升行采样标题原文；未提升候选采样段内文本（比值 < 提升锚比值即「字号
    未命中」的直接成因可见）。
    """
    rows: list[PromotedRow] = []
    for node, promoted in channel:
        payload = (node.title or "") if promoted else ((node.text or "") or (node.title or ""))
        ending = payload[-1] if payload and payload[-1] in _SENTENCE_ENDING_ALL else None
        gap = ending is not None and ending not in _PROMOTE_SENT_END_CHARS
        ratio: float | None = None
        page = _node_page(node)
        if sampler is not None and payload and page and body_font_size:
            try:
                stat = sampler.text_font_stats(page - 1, payload)
            except Exception:  # noqa: BLE001 —— 重采样失败按数据不可得（比值 —）
                stat = None
            if stat is not None:
                ratio = float(stat[0]) / float(body_font_size)
        rows.append(
            PromotedRow(
                title=payload,
                chars=len(payload),
                lines=len(payload.splitlines()),
                ending=ending,
                gap=gap,
                ratio=ratio,
                promoted=promoted,
            )
        )
    return rows


def _formula_stats(document: IrNode) -> tuple[int, int, int, list[str]]:
    """④ formula 通道活跃度：→ (总数, 其中 heading 数, 空载荷数, 样例)。

    树内 raw_type=formula 的节点 = 「text 空且有 orig」的 docling 条目全量（Fix 1
    回落写回 orig 后 text/title 二者必有其一；皆空 = 空载荷残留，切分时被跳过）。
    样例标题类优先（误判嫌疑面），无标题类时回落正文样例。
    """
    total = heading_count = empty = 0
    heading_samples: list[str] = []
    text_samples: list[str] = []
    for node in _iter_nodes(document):
        if node.meta.get("raw_type") != _FORMULA_RAW_TYPE:
            continue
        total += 1
        payload = (node.title or "") or (node.text or "")
        if node.type is IrNodeType.HEADING:
            heading_count += 1
            if payload and len(heading_samples) < _INJURY_SAMPLE_LIMIT:
                heading_samples.append(payload)
        elif payload and len(text_samples) < _INJURY_SAMPLE_LIMIT:
            text_samples.append(payload)
        if not payload:
            empty += 1
    return total, heading_count, empty, (heading_samples + text_samples)


def build_injury_report(
    output: PipelineOutput,
    *,
    font_sampler: Any | None = None,
    page_suspect: int = INJURY_DENSITY_PAGE_SUSPECT,
    cluster_min: int = INJURY_DENSITY_CLUSTER_MIN,
    cluster_max_chars: int = INJURY_DENSITY_CLUSTER_MAX_CHARS,
) -> InjuryReport | None:
    """小节级伤情单（工单 C v2；只读观察，不碰树、不碰管线、不写 parse_meta）。

    **门禁（零变化硬约束）**：仅 PDF 通道且 parse_meta 带 heading_outline 键
    （= 书签校正启用）时产出；非 PDF 与关闭态（KB_PDF_OUTLINE_CORRECTION=false）
    恒 None——text/html 渲染与 v1 逐字节一致。
    """
    meta = output.parse_meta.get("heading_outline")
    path = meta.get("correction_path") if isinstance(meta, dict) else None
    if output.ext.lstrip(".").lower() != "pdf" or not isinstance(path, str):
        return None

    headings, gaps = _walk_headings(output.document)
    heading_count = len(headings)
    body_chars = len(output.chunking.document_text)
    per_1k = heading_count / body_chars * 1000 if body_chars else None
    pages_raw = output.parse_meta.get("pages")
    pages = pages_raw if isinstance(pages_raw, int) and pages_raw > 0 else None
    per_page = heading_count / pages if pages else None

    # ⑤ 无书签显著警示（校正启用但书签未主导）
    warning = None
    if path in _INJURY_LAYOUT_ONLY_PATHS:
        warning = f"⚠️ 无书签锚定：版面推断主导，层级可信度降级（correction_path={path}）"

    # ① 书签召回缺失（需要可读书签面；零命中逐条列出）
    outline = output.outline
    missing_available = bool(outline)
    missing: list[MissingBookmark] = []
    if outline:
        items = [
            HeadingItem(
                title=node.title or "",
                level=node.level or 1,
                page=_node_page(node),
            )
            for node in headings
        ]
        _anchors, unmatched = resolve_anchors(items, outline)
        missing = _missing_bookmark_entries(outline, unmatched)

    # ② 密度嫌疑（阈值相对量：页/簇/间隔均为分布形态，不写绝对 pt/绝对密度上限）
    suspects = _density_suspects(
        headings,
        gaps,
        page_suspect=page_suspect,
        cluster_min=cluster_min,
        cluster_max_chars=cluster_max_chars,
    )

    # ③ 提升行形态矩阵（字号比值需重采样；sampler 缺失 → 比值 —）
    sampler = font_sampler
    owned_pdf: Any | None = None
    if sampler is None and output.data:
        try:
            owned_pdf = open_pdf_bytes(output.data)
            sampler = CharFontSampler(owned_pdf)
        except Exception:  # noqa: BLE001 —— 字体数据不可得 → 比值列 —（不阻塞伤情单）
            if owned_pdf is not None:
                owned_pdf.close()
                owned_pdf = None
            sampler = None
    heuristic = output.parse_meta.get("heading_heuristic")
    base_raw = heuristic.get("body_font_size") if isinstance(heuristic, dict) else None
    if not isinstance(base_raw, (int, float)) or not base_raw:
        base_raw = getattr(sampler, "body_font_size", None)
    try:
        channel = _promotion_channel(output.document)
        promoted = _promoted_rows(
            channel,
            sampler=sampler,
            body_font_size=float(base_raw) if base_raw else None,
        )
    finally:
        if sampler is not None and sampler is not font_sampler:
            sampler.close()
        if owned_pdf is not None:
            owned_pdf.close()
    # ③ 自诊断：管线留痕 promoted（parse_meta.heading_heuristic.promoted）与树内
    # 指纹（已提升行数）矛盾 = 提升行形态在树内不可归因——缺口警示显式输出
    fingerprint_promoted = sum(1 for _node, flag in channel if flag)
    pipeline_promoted_raw = (
        heuristic.get("promoted") if isinstance(heuristic, dict) else None
    )
    pipeline_promoted = (
        pipeline_promoted_raw if isinstance(pipeline_promoted_raw, int) else None
    )
    promotion_gap_warning: str | None = None
    if pipeline_promoted is not None and pipeline_promoted > fingerprint_promoted:
        promotion_gap_warning = (
            f"  · ⚠️ 指纹缺口：管线留痕 promoted={pipeline_promoted} 条，"
            f"树内 raw_type=text/formula 的 heading 仅 {fingerprint_promoted} 条"
            "——提升行形态指纹不可归因（工单 B 复核面）"
        )

    # ④ formula 通道活跃度
    f_total, f_headings, f_empty, f_samples = _formula_stats(output.document)

    return InjuryReport(
        correction_path=path,
        no_bookmark_warning=warning,
        missing_available=missing_available,
        missing=missing,
        headings=heading_count,
        body_chars=body_chars,
        per_1k=per_1k,
        pages=pages,
        per_page=per_page,
        density_suspects=suspects,
        promoted=promoted,
        promoted_pipeline=pipeline_promoted,
        promotion_gap_warning=promotion_gap_warning,
        formula_total=f_total,
        formula_headings=f_headings,
        formula_empty=f_empty,
        formula_samples=f_samples,
    )


def _render_density_suspect(suspect: DensitySuspect) -> str:
    titles = "、".join(suspect.titles[:_INJURY_SAMPLE_LIMIT]) or "—"
    if suspect.kind == "page":
        return f"  · [单页密集] p{suspect.page} 共 {suspect.count} 个 heading：{titles}"
    pages = (
        "—"
        if suspect.pages is None
        else f"p{suspect.pages[0]}~p{suspect.pages[1]}"
    )
    span = "—" if suspect.span_chars is None else str(suspect.span_chars)
    return (
        f"  · [连续簇] {suspect.count} 条 heading · 正文间隔 {span} 字"
        f"（{pages}）：{titles}"
    )


def _render_promoted_row(row: PromotedRow) -> str:
    ratio = "—" if row.ratio is None else f"{row.ratio:.2f}"
    if row.ending is None:
        ending = "无"
    elif row.gap:
        ending = f"「{row.ending}」（脱漏：提升正则不含）"
    else:
        ending = f"「{row.ending}」"
    status = "" if row.promoted else " · 未提升（字号未命中标题簇或句末标点拦截）"
    return (
        f"  · 「{row.title}」 {row.chars} 字 · {row.lines} 行"
        f" · 句末：{ending} · 字号比值 {ratio}{status}"
    )


def render_injury_report_text(report: InjuryReport) -> list[str]:
    """text 视图伤情单块（③.b 节；⑤ 警告恒为第一行）。"""
    lines = ["--- ③.b 小节级伤情单（工单 C v2 · 只读观察）---"]
    if report.no_bookmark_warning:
        lines.append(report.no_bookmark_warning)
    lines.append("")
    # ① 书签召回缺失（零命中逐条）
    if report.missing_available:
        if report.missing:
            lines.append(f"书签召回缺失    : {len(report.missing)} 条——")
            for entry in report.missing:
                page = "—" if entry.page is None else str(entry.page + 1)
                lines.append(
                    f"  · 「{entry.title}」 书签层级 L{entry.level + 1}"
                    f" · 目标页 {page} · 树内零命中"
                )
        else:
            lines.append("书签召回缺失    : 0 条（全部命中）")
    else:
        lines.append("书签召回缺失    : 书签不可读，无法核对")
    # ② 标题密度（分布 + 嫌疑清单）
    volume = (
        f"{report.body_chars / 1000:.1f} 千字"
        if report.body_chars >= 1000
        else f"{report.body_chars} 字"
    )
    density = f"标题密度        : {report.headings} heading / {volume}"
    if report.per_1k is not None:
        density += f" = {report.per_1k:.2f} 条/千字"
    if report.pages is not None:
        density += f" · {report.pages} 页"
        if report.per_page is not None:
            density += f" = {report.per_page:.2f} 条/页"
    if report.density_suspects:
        density += f"；嫌疑 {len(report.density_suspects)} 处——"
        lines.append(density)
        lines.extend(_render_density_suspect(s) for s in report.density_suspects)
    else:
        lines.append(f"{density}；嫌疑 0 处")
    # ③ 提升行形态矩阵（已提升 + 未提升候选；留痕对照与缺口自诊断）
    promoted_count = sum(1 for row in report.promoted if row.promoted)
    candidate_count = len(report.promoted) - promoted_count
    pipeline_mark = (
        str(report.promoted_pipeline)
        if report.promoted_pipeline is not None
        else "—"
    )
    lines.append(
        f"提升行形态      : Fix 2 提升通道已提升 {promoted_count} 条"
        f" · 未提升候选 {candidate_count} 条"
        f"（管线留痕 promoted={pipeline_mark}）"
        + ("——" if report.promoted else "")
    )
    if report.promotion_gap_warning:
        lines.append(report.promotion_gap_warning)
    shown = report.promoted[:_INJURY_MATRIX_ROW_LIMIT]
    lines.extend(_render_promoted_row(row) for row in shown)
    if len(report.promoted) > _INJURY_MATRIX_ROW_LIMIT:
        hidden = len(report.promoted) - _INJURY_MATRIX_ROW_LIMIT
        lines.append(f"  · …其余 {hidden} 条略（上限 {_INJURY_MATRIX_ROW_LIMIT} 条）")
    # ④ 空 text 候选（formula 活跃度）
    share = (
        round(report.formula_headings / report.formula_total * 100)
        if report.formula_total
        else 0
    )
    body_count = (
        report.formula_total - report.formula_headings - report.formula_empty
    )
    samples = "、".join(report.formula_samples) if report.formula_samples else "—"
    lines.append(
        f"空 text 候选    : formula {report.formula_total} 条"
        f"（标题 {report.formula_headings} · 正文 {body_count}"
        f" · 空载荷 {report.formula_empty}）；标题占比 {share}%；样例: {samples}"
    )
    return lines


def render_injury_report_html(report: InjuryReport) -> str:
    """html 视图伤情单块（与 text 同源同文案；复用 selfcheck 类，零新增 CSS）。"""
    body = "\n".join(
        html.escape(line) for line in render_injury_report_text(report)
    )
    return f'<section class="selfcheck"><pre>{body}</pre></section>'


# ---------------------------------------------------------------------------
# 6) 整体渲染
# ---------------------------------------------------------------------------


def _summary_lines(output: PipelineOutput) -> list[str]:
    meta = output.parse_meta
    # SDK blocks 通道无原始字节面：byte_size 记块数，行尾注记口径（见 run_feishu_sdk_pipeline）
    size_note = "（SDK 通道按块计数）" if output.channel == "feishu-sdk" else ""
    lines = [
        f"源              : {output.source_label}",
        f"字节 / 扩展名   : {output.byte_size} B / .{output.ext}{size_note}",
        f"归一化通道      : {meta.get('parser', '?')}"
        f"（pages={meta.get('pages')} tables={meta.get('tables')}"
        f" code_blocks={meta.get('code_blocks')}）",
        f"文档根          : {output.document.id}（doc_kind={output.document.doc_kind}）",
        f"正文扁平字符数  : {len(output.chunking.document_text)}",
        f"切片数          : {len(output.chunking.chunks)}"
        f"（oversized_units={output.chunking.oversized_units}"
        f" skipped_units={output.chunking.skipped_units}）",
        f"文档 content_hash: {output.chunking.document_content_hash[:12]}…",
        f"切分配置        : chunk_size={output.config.chunk_size}"
        f" chunk_overlap={output.config.chunk_overlap}"
        f" page/para/sentence={output.config.page_max_chars}/"
        f"{output.config.para_max_chars}/{output.config.sentence_max_chars}"
        f" table_agg_rows={output.config.table_agg_rows}",
    ]
    if output.truncated:
        # 翻页防御截断：summary 与落盘文件里也必须显眼可见（不只 stdout 即时 print）
        lines.append(
            "[!] SDK blocks 翻页截断（truncated=True）：视图不完整，尾块缺失"
        )
    return lines


def render_text(output: PipelineOutput) -> str:
    lines = ["=== KB 管线验证探针（归一化 → 树 IR → 逐级切分） ===", ""]
    lines.extend(_summary_lines(output))
    lines.extend(
        [
            "",
            "--- ① 树 IR 渲染（◆ = document-ref 占位符可见标记）---",
            "",
        ]
    )
    lines.extend(render_tree_text(output.document))
    lines.extend(["", "--- ② Chunk 视图 ---", ""])
    lines.extend(render_chunks_text(output))
    lines.extend([""])
    lines.extend(render_structure_check_text(build_structure_check(output)))
    # ③.b 小节级伤情单（工单 C v2）：仅 PDF + 校正启用时产出（非 PDF / 关闭态零变化）
    injury = build_injury_report(output)
    if injury is not None:
        lines.extend([""])
        lines.extend(render_injury_report_text(injury))
    return "\n".join(lines)


_HTML_STYLE = """
:root { color-scheme: light dark; }
body { font: 14px/1.5 ui-sans-serif, system-ui, "PingFang SC", sans-serif;
       margin: 1.5rem; max-width: 1600px; }
h1 { font-size: 1.15rem; margin: 0 0 .75rem; }
h2 { font-size: 1rem; margin: 1.25rem 0 .5rem; }
.meta { font-size: 12px; color: #555; border: 1px solid #ddd; border-radius: 6px;
        padding: .6rem .8rem; margin-bottom: 1rem; white-space: pre-wrap; }
.wrap { display: flex; gap: 1.5rem; align-items: flex-start; }
.col { flex: 1 1 0; min-width: 0; }
.col.tree { flex: 0 0 42%; }
.node { font: 12px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace;
        white-space: pre-wrap; word-break: break-all; padding-left: calc(var(--d) * 1.1rem);
        border-left: 1px solid #eee; }
.node[data-kind="document"] { font-weight: 600; }
.node[data-kind="heading"] { color: #0b5cad; }
.node[data-kind="code"] { color: #7a3d00; }
.node[data-kind="table"] { color: #096b3f; }
.chunk { border: 1px solid #ddd; border-radius: 6px; padding: .5rem .7rem; margin: 0 0 .5rem; }
.chunk h3 { font: 12px/1.5 ui-monospace, Menlo, monospace; margin: 0 0 .35rem;
            white-space: pre-wrap; word-break: break-all; }
.chunk pre { white-space: pre-wrap; word-break: break-all; margin: 0; font-size: 12px; }
.refs { font-size: 12px; color: #555; margin: 0 0 .35rem; }
.selfcheck { border: 1px solid #cfe0d6; background: #f3faf6; border-radius: 6px;
             padding: .5rem .7rem; margin: 0 0 1rem; }
.selfcheck pre { margin: 0; font: 12px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace;
                 white-space: pre-wrap; }
.boundary { text-align: center; font-size: 12px; color: #8a5a00; background: #fff7e0;
            border: 1px dashed #e0b44c; border-radius: 6px; padding: .25rem;
            margin: 0 0 .5rem; }
mark.ref { background: #ffe08a; padding: 0 .12rem; border-radius: 3px; }
footer { margin-top: 1.5rem; font-size: 12px; color: #777; }
"""


def render_html(output: PipelineOutput) -> str:
    meta = "\n".join(html.escape(line) for line in _summary_lines(output))
    injury = build_injury_report(output)
    injury_html = (
        render_injury_report_html(injury) if injury is not None else ""
    )
    return f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KB 管线探针 · {html.escape(output.source_label)}</title>
<style>{_HTML_STYLE}</style>
</head>
<body>
<h1>KB 管线验证探针 · {html.escape(output.source_label)}</h1>
<div class="meta">{meta}</div>
{render_structure_check_html(build_structure_check(output))}{injury_html}
<div class="wrap">
  <div class="col tree">
    <h2>① 树 IR（◆ = document-ref 占位符）</h2>
    {render_tree_html(output.document)}
  </div>
  <div class="col">
    <h2>② Chunk 视图（相邻片边界线标注重叠窗）</h2>
    {render_chunks_html(output)}
  </div>
</div>
<footer>归一化 → 树 IR → 逐级切分 · 配置取 config_center 缺省 · 零 DB/MinIO/Nacos</footer>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 6) 飞书通道（export task：创建 → 轮询 → 下载 docx 本体；凭据只读 .env）
# ---------------------------------------------------------------------------


def load_env(path: Path) -> dict[str, str]:
    """极简 .env 解析（KEY=VALUE；容忍引号与行内 ` #` 注释；跳过注释/空行）。"""
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
        env[key.strip()] = value
    return env


def _require_httpx() -> Any:
    """惰性取 httpx（仅飞书通道需要；本地文件路径零网络依赖）。"""
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover —— 依赖在 search-api venv 内
        raise ProbeError("飞书通道需要 httpx（search-api 依赖内已声明）", feishu=True) from exc
    return httpx


def _feishu_json(resp: Any) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError as exc:
        raise ProbeError(
            f"飞书接口返回非 JSON（HTTP {resp.status_code}）", exit_code=1, feishu=True
        ) from exc
    if not isinstance(body, dict):
        raise ProbeError(
            f"飞书接口返回非对象（HTTP {resp.status_code}）", exit_code=1, feishu=True
        )
    return body


def _feishu_ok(body: dict[str, Any], step: str) -> dict[str, Any]:
    """校验飞书信封 code==0 并取 data（错误只回显 code/msg，不回显凭据）。

    查询导出任务接口把 job_status/file_token 包在 data.result 里（创建接口直接在 data
    下），两种形态归一——漏解 result 层时 job_status 恒 None、飞书通道必失败（2026-11-24
    首次活体运行实证）。
    """
    if body.get("code") != 0:
        raise ProbeError(
            f"{step}失败：code={body.get('code')} msg={body.get('msg')}",
            exit_code=1,
            feishu=True,
        )
    data = body.get("data")
    if not isinstance(data, dict):
        return {}
    result = data.get("result")
    return result if isinstance(result, dict) else data


def fetch_feishu_docx(obj_token: str) -> bytes:
    """obj_token → export task（docx）→ 下载 docx 本体（凭据只读 .env）。"""
    httpx = _require_httpx()
    env = load_env(ENV_PATH)
    app_id = env.get("FEISHU_APP_ID", "").strip()
    app_secret = env.get("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise ProbeError(
            "仓库根 .env 缺少 FEISHU_APP_ID / FEISHU_APP_SECRET（凭据只从 .env 读）",
            feishu=True,
        )
    api_base = env.get("FEISHU_API_BASE", "").strip() or "https://open.feishu.cn"

    with httpx.Client(
        base_url=api_base, timeout=FEISHU_TIMEOUT_S, follow_redirects=True
    ) as client:
        started = time.perf_counter()
        body = _feishu_json(
            client.post(
                "/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": app_id, "app_secret": app_secret},
            )
        )
        token = str(body.get("tenant_access_token") or "")
        if not token:
            raise ProbeError(
                f"获取 tenant_access_token 失败：code={body.get('code')}"
                f" msg={body.get('msg')}",
                feishu=True,
            )
        print(
            f"[feishu] tenant token 已获取（{(time.perf_counter() - started) * 1000:.0f}ms，值不打印）"
        )
        headers = {"Authorization": f"Bearer {token}"}

        ticket = str(
            _feishu_ok(
                _feishu_json(
                    client.post(
                        "/open-apis/drive/v1/export_tasks",
                        headers=headers,
                        json={
                            "file_extension": "docx",
                            "token": obj_token,
                            "type": "docx",
                        },
                    )
                ),
                "创建导出任务",
            ).get("ticket")
            or ""
        )
        if not ticket:
            raise ProbeError("创建导出任务失败：响应缺 ticket", exit_code=1, feishu=True)
        print("[feishu] 导出任务已创建，开始轮询 job_status")

        deadline = time.monotonic() + FEISHU_POLL_DEADLINE_S
        file_token = ""
        while time.monotonic() < deadline:
            data = _feishu_ok(
                _feishu_json(
                    client.get(
                        f"/open-apis/drive/v1/export_tasks/{ticket}",
                        headers=headers,
                        params={"token": obj_token},
                    )
                ),
                "查询导出任务",
            )
            status = data.get("job_status")
            if status == 0:
                file_token = str(data.get("file_token") or "")
                break
            if status not in (1, 2):
                reason = FEISHU_EXPORT_POLL_TERMINAL_ERR.get(
                    status if isinstance(status, int) else -1, "未知状态"
                )
                raise ProbeError(
                    f"导出任务失败：job_status={status}（{reason}）",
                    exit_code=1,
                    feishu=True,
                )
            time.sleep(FEISHU_POLL_INTERVAL_S)
        if not file_token:
            raise ProbeError(
                f"导出任务轮询超时（{FEISHU_POLL_DEADLINE_S:.0f}s 内未达 job_status=0）",
                exit_code=1,
                feishu=True,
            )
        print("[feishu] 导出完成，下载产物")

        resp = client.get(
            f"/open-apis/drive/v1/export_tasks/file/{file_token}/download",
            headers=headers,
        )
        if resp.status_code != 200:
            raise ProbeError(
                f"下载导出产物失败：HTTP {resp.status_code}（10 分钟时效内须即时下载）",
                exit_code=1,
                feishu=True,
            )
        payload = resp.content
    return _validate_docx(payload)


def _validate_docx(payload: bytes) -> bytes:
    """校验导出产物是 docx 本体后原样返回。

    file_extension=docx 的导出下载产物**就是 docx 文件本身**（OOXML zip 容器：PK 魔数 +
    `[Content_Types].xml` + `word/document.xml` + `word/media/*` 内嵌图片），外面没有再包一层
    zip——误当「zip 里取首个成员」会取到 `[Content_Types].xml`（2026-11-24 首次活体运行实证）。
    """
    if not payload.startswith(b"PK\x03\x04"):
        raise ProbeError(
            "导出产物非 docx（缺 PK 魔数；收到字节数与形态不符的响应）",
            exit_code=1,
            feishu=True,
        )
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            has_document = "word/document.xml" in set(zf.namelist())
    except zipfile.BadZipFile as exc:
        raise ProbeError(
            "导出产物 docx 结构损坏（BadZipFile）", exit_code=1, feishu=True
        ) from exc
    if not has_document:
        raise ProbeError(
            "导出产物不是 Word 文档（容器内无 word/document.xml）",
            exit_code=1,
            feishu=True,
        )
    return payload


# ---------------------------------------------------------------------------
# 6.b) 飞书 SDK blocks 直读通道（生产导入同映射形态；凭据走 feishu_settings 单例）
# ---------------------------------------------------------------------------


def _fetch_sdk_blocks(obj_token: str) -> tuple[list[Any], bool]:
    """同步桥接：异步 client 分页翻全 → (items, truncated)。

    凭据缺失时 `get_default_feishu_client()` 在**构造即抛** FeishuClientError（消息经
    redact 脱敏、零回显凭据值）——此处归一 ProbeError（飞书失败面 → 退出码 1 + 降级
    提示）；取回后全程同步，不再进事件循环。
    """
    async def _run() -> Any:
        return await get_default_feishu_client().list_document_blocks(obj_token)

    try:
        page = asyncio.run(_run())
    except FeishuClientError as exc:
        raise ProbeError(
            f"飞书 SDK blocks 直读失败：{exc}", exit_code=1, feishu=True
        ) from exc
    except ImportError as exc:  # lark-oapi 未安装（环境面）
        raise ProbeError(
            f"飞书 SDK 不可用（lark-oapi 未安装）：{exc}", exit_code=2, feishu=True
        ) from exc
    return list(page.items), bool(page.truncated)


def run_feishu_sdk_pipeline(
    obj_token: str,
    *,
    node_token: str | None = None,
    space_id: str = SDK_DEFAULT_SPACE,
    title: str = SDK_DEFAULT_TITLE,
    obj_edit_time: int | None = None,
    catalog_path: Sequence[str] = (),
) -> PipelineOutput:
    """SDK blocks 直读 → 树 IR → sheet(30) 回填 → 逐级切分（与生产 `_ingest_docx` 逐字同组合）。

    生产组合（`apps/search-api/app/rag/import_worker.py` `_ingest_docx`）：
    `map_blocks`（探针无同步目录 → `known_node_tokens`/`node_token_by_obj_token` 空）
    → `build_document_node` → catalog_path → `attach_block_tree` → **sheet(30) 装配后
    回填段**（`backfill_sheet_placeholders`：placeholder 翻 false + meta.rows/col_count
    物化；失败保留占位不算文档失败，FeishuSheetValuesError.category 进
    failed_categories 留痕；探针零 DB 语义不变，client 即取即用）→ parse_meta →
    `chunk_forest`。产出面（`PipelineOutput`）与本地文件通道同构，后续渲染/落盘
    全部复用既有 main 流程。

    `catalog_path` 与 `run_pipeline` 同口径：从根到本文档、含本文档标题；空白段
    （纯空白）在此滤除（不是标题，落进链会污染 heading_path 轨迹）。
    """
    items, truncated = _fetch_sdk_blocks(obj_token)
    if truncated:
        # 翻页防御上限截断：尾块缺失 → 树与切片视图都不完整，判定不可全信
        print(
            "[!] SDK blocks 翻页防御截断（truncated=True）：尾块缺失，树/切片视图不完整"
        )

    node = node_token or obj_token
    mapping = map_blocks(
        items,
        space_id=space_id,
        node_token=node,
        known_node_tokens=frozenset(),  # 探针场景无同步目录（生产由同步 diff 供给）
        node_token_by_obj_token={},  # 同上：无 obj_token → node_token 反查表
    )
    doc_node = build_document_node(
        node_token=node,
        title=title,
        obj_type=SDK_OBJ_TYPE,
        obj_edit_time=obj_edit_time,
        space_id=space_id,
    )
    # 空白段滤除（与 run_pipeline 注释同语义：只丢空白段，不重写真实标题内容）
    doc_node.catalog_path = [segment for segment in catalog_path if segment.strip()]
    attach_block_tree(doc_node, mapping.refs, root_order=mapping.root_order)

    # sheet(30) 装配后回填段（生产 `_ingest_docx` 同组合——同位：装配后、切分前）：
    # 占位翻 false + meta.rows/col_count 物化；失败保留占位不算文档失败（27 行 image
    # 先例同款）；超限截断计数留痕 parse_meta.feishu.sheet_backfill。同步→异步单桥：
    # backfill 整体一个 asyncio.run，fetch 闭包保持生产同款 async 形态（嵌套 run 非法）。
    async def _backfill() -> SheetBackfillReport:
        async def _fetch_sheet_values(sheet_token: str) -> SheetValuesData:
            return await get_default_feishu_client().fetch_sheet_values(sheet_token)

        return await backfill_sheet_placeholders(doc_node, _fetch_sheet_values)

    backfill = asyncio.run(_backfill())
    degraded = dict(mapping.degraded_blocks)
    for key, count in backfill.degraded_delta().items():
        degraded[key] = degraded.get(key, 0) + count
    parse_meta = {
        "pages": None,
        "tables": sum(1 for ref in mapping.refs if ref.node.type.value == "table"),
        "code_blocks": sum(1 for ref in mapping.refs if ref.node.type.value == "code"),
        "parser": PARSER_FEISHU_BLOCKS,  # 通道标识（api.md §4.7；常量取自 feishu_mapper）
        "feishu": build_feishu_parse_meta(
            space_id=space_id,
            node_token=node,
            obj_type=SDK_OBJ_TYPE,
            obj_edit_time=obj_edit_time,
            degraded_blocks=degraded,
        ),
    }
    sheet_backfill_block = backfill.parse_meta_block()
    if sheet_backfill_block:
        parse_meta["feishu"]["sheet_backfill"] = sheet_backfill_block
    config = ChunkingConfig.from_spec(SPEC_CONFIG)
    results = chunk_forest(doc_node, config=config)
    if len(results) != 1:
        raise ProbeError(
            f"单文档管线期望 1 个切分结果，实得 {len(results)}（SDK blocks 产物形态异常）"
        )
    return PipelineOutput(
        source_label=f"{SDK_LABEL_PREFIX}obj_token={obj_token[:6]}***",
        ext=SDK_OBJ_TYPE,
        byte_size=len(items),  # 无字节面：按块计数（summary 行注记口径）
        document=doc_node,
        parse_meta=parse_meta,
        chunking=results[0],
        config=config,
        outline=None,  # 非 PDF 通道（自检节「不适用」）
        data=b"",  # 无原始字节（伤情单等 PDF 专属面不触达）
        channel="feishu-sdk",  # summary 按通道注记（不靠标签前缀判定）
        truncated=truncated,  # 截断留痕进 summary（text/html 落盘可见）
    )


# ---------------------------------------------------------------------------
# 7) 入口
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="KB 管线验证探针（归一化 → 树 IR → 逐级切分；开发工具，不进生产链）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python scripts/probe_kb_pipeline.py README.md\n"
            "  python scripts/probe_kb_pipeline.py README.md --format html --out out/kb_probe.html\n"
            "  python scripts/probe_kb_pipeline.py --sample\n"
            "  python scripts/probe_kb_pipeline.py --feishu-token <obj_token>\n"
            "  python scripts/probe_kb_pipeline.py --feishu-sdk <obj_token> --feishu-sdk-space <space_id>\n"
            "  python scripts/probe_kb_pipeline.py out/x.docx --catalog-path \"A > B\"\n"
        ),
    )
    parser.add_argument("path", nargs="?", help="输入文件路径（md/txt/html/docx/pdf…）")
    parser.add_argument(
        "--sample", action="store_true", help="使用内置小样本（标题/代码/表格/占位符）"
    )
    parser.add_argument(
        "--feishu-token",
        metavar="OBJ_TOKEN",
        help="飞书文档 obj_token：走 export task 通道取 docx 后喂同一管线",
    )
    parser.add_argument(
        "--feishu-sdk",
        metavar="OBJ_TOKEN",
        help="飞书 docx obj_token：走 SDK blocks 直读通道（生产导入同映射形态）",
    )
    parser.add_argument(
        "--feishu-sdk-node",
        metavar="NODE_TOKEN",
        help="SDK 通道的 wiki 节点 token（缺省=obj_token；无同步目录时任意值）",
    )
    parser.add_argument(
        "--feishu-sdk-space",
        metavar="SPACE_ID",
        default=SDK_DEFAULT_SPACE,
        help=f"SDK 通道的知识空间 ID（缺省 {SDK_DEFAULT_SPACE}；只进 parse_meta.feishu）",
    )
    parser.add_argument(
        "--feishu-sdk-title",
        metavar="TITLE",
        default=SDK_DEFAULT_TITLE,
        help=f"SDK 通道的文档标题（缺省 {SDK_DEFAULT_TITLE}）",
    )
    parser.add_argument(
        "--feishu-sdk-edit-time",
        metavar="INT",
        type=int,
        default=None,
        help="SDK 通道的 obj_edit_time（秒级整数，缺省 None）",
    )
    parser.add_argument(
        "--catalog-path",
        metavar="CHAIN",
        help=(
            "演示用知识库目录链（`>` 分隔，从根到本文档节点、含本文档节点标题），"
            "写入 document 根 catalog_path 后跑同一管线；缺省不设=现状零变化"
        ),
    )
    parser.add_argument(
        "--format", choices=("text", "html"), default="text", help="输出格式（默认 text）"
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        help=f"输出文件（text 缺省 stdout；html 缺省 {DEFAULT_HTML_OUT}）",
    )
    return parser


#: 输入方式四选一（path / --sample / --feishu-token / --feishu-sdk）——互斥校验与文案单一来源
_INPUT_MODES: tuple[tuple[str, str], ...] = (
    ("<文件路径>", "path"),
    ("--sample", "sample"),
    ("--feishu-token", "feishu_token"),
    ("--feishu-sdk", "feishu_sdk"),
)


def _check_single_input(args: argparse.Namespace) -> None:
    """输入须且只须一种（四选一互斥；`--catalog-path` 等附加旗标不参与选择）。"""
    picked = [name for name, attr in _INPUT_MODES if getattr(args, attr, None)]
    if len(picked) != 1:
        raise ProbeError("输入须且只须一种：" + " / ".join(name for name, _ in _INPUT_MODES))


def _resolve_input(args: argparse.Namespace) -> tuple[bytes, str, str]:
    """参数 → (bytes, ext, 源标签)；四选一互斥校验（SDK blocks 通道由 main 先分流）。"""
    _check_single_input(args)
    if args.sample:
        return SAMPLE_MARKDOWN.encode("utf-8"), SAMPLE_EXT, "内置样本 --sample"
    if args.feishu_token:
        token = args.feishu_token
        label = f"飞书文档 obj_token={token[:6]}***（export task）"
        return fetch_feishu_docx(token), "docx", label
    if args.feishu_sdk:
        # 防御：main 对 --feishu-sdk 先分流（无字节面），走到这里说明调用方绕过了 main
        raise ProbeError("--feishu-sdk 走 SDK blocks 直读通道，不经本地字节管线")
    assert args.path is not None  # 互斥校验已保证
    path = Path(args.path)
    if not path.is_file():
        raise ProbeError(f"输入文件不存在：{path}")
    ext = path.suffix.lstrip(".").lower()
    if not ext:
        raise ProbeError(f"无法从文件名判断扩展名：{path.name}（请带 .md/.txt/.html/… 后缀）")
    return path.read_bytes(), ext, str(path)


def _emit(payload: str, out: str | None, *, default_out: Path | None) -> None:
    """写出结果：显式 --out → 文件；否则 html 走 default_out、text 走 stdout。"""
    if out is None and default_out is None:
        print(payload)
        return
    target = Path(out) if out else (default_out or DEFAULT_HTML_OUT)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload, encoding="utf-8")
    print(f"[probe] 输出已写入 {target}")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        _check_single_input(args)  # 四选一互斥先于任何取数动作（含 SDK 通道）
        if args.feishu_sdk:
            # SDK blocks 直读通道：产出面与本地文件通道同构，渲染/落盘全复用下方流程
            output = run_feishu_sdk_pipeline(
                args.feishu_sdk,
                node_token=args.feishu_sdk_node,
                space_id=args.feishu_sdk_space,
                title=args.feishu_sdk_title,
                obj_edit_time=args.feishu_sdk_edit_time,
                catalog_path=_parse_catalog_path(args.catalog_path),
            )
        else:
            data, ext, label = _resolve_input(args)
            output = run_pipeline(
                data, ext, label, catalog_path=_parse_catalog_path(args.catalog_path)
            )
        if args.format == "html":
            _emit(render_html(output), args.out, default_out=DEFAULT_HTML_OUT)
        else:
            _emit(render_text(output), args.out, default_out=None)
    except ProbeError as exc:
        print(f"[FAIL] {exc.message}", file=sys.stderr)
        if exc.feishu:
            print(
                "[hint] 飞书通道不可用时的降级用法：先在飞书手动导出 docx，"
                "再把本地路径传给本脚本（python scripts/probe_kb_pipeline.py <本地.docx>）",
                file=sys.stderr,
            )
        return exc.exit_code
    except BusinessException as exc:
        # 领域异常（BadFileError/UnsupportedTypeError/NormalizationError）如实报错
        print(f"[FAIL] {exc.message}", file=sys.stderr)
        if exc.detail:
            print(f"[detail] {exc.detail}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"[FAIL] 文件读取失败：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
