"""逐级切分（IR → kb_chunks）（M3 批次 2 / B17 主线二）。

契约锚点：
- data-model.md §1.8 kb_chunks（meta 七键 `{node_id, heading_path, loc, references,
  raw_type, char_start, char_end}`）、§1.18（document-ref 占位符协议 + heading_path
  构成公式 `catalog_path + [doc.title] + 文档内标题链`——**2026-11-24 用户裁定增补**：
  catalog_path 两种形态均兼容（链含本文档节点标题 / 链仅祖先均可），物化时接缝去重
  + 交界回显不入轨迹，见模块内第 6 条）；
- ARCHITECTURE.md ADR-018 决策④（逐级切分 OQ-27：页面→段落→句子逐级降级、代码整块
  不内切、表格行/分组聚合、chunk 参数与各级上限 Nacos `kb.chunking` 热更）；
- nacos-config.md §3 kb 节（`chunk_size`=800 / `chunk_overlap`=120 /
  `chunking.{page_max_chars=1200, para_max_chars=400, sentence_max_chars=200,
  table_agg_rows=10}`——经 `ChunkingConfig.from_spec` 只消费 Agent Spec 快照）；
- 调研 feishu-kb-import-research-20261122.md §13.4 切分行为列 / §13.6#1 切分链
  （DFS 展开线性节点流 → 逐级降级 chunk；占位符完整性校验 → PUA 保护 → 还原 →
  按切片筛选 references）。

**钉住的切分语义**（本模块为实现事实源，fixture 测试锁死）：
1. **逐级降级**：heading 边界优先切（不单独成 chunk、不进 content——heading_path
   元数据承载）；超长文本单元按 页面级(≤page_max_chars) → 段落级(≤para_max_chars)
   → 句子级(≤sentence_max_chars) 逐级下钻；句子级仍超长（无任何语义边界）才按窗
   硬切（chunk_size/chunk_overlap）。页面级边界来自 `page_breaks` 显式入参（页式
   来源由归一化/流水线线供给；缺省无页界则直接下钻段落级）。
2. **尺寸窗**：`chunk_size`=目标窗（语义边界优先于窗边界——单个语义单元 ≤ 页面上限
   时允许整块超目标窗成 chunk，不强行内切）；`chunk_overlap`=重叠——句子级碎片跨
   chunk 时携带前片句尾尾巴（按句边界吸附、≤ chunk_overlap）；硬切片自带重叠不再
   叠加；不同单元合成的 chunk 零重叠。各级上限（1200/400/200）决定下钻触发与切片
   粒度，目标窗（800）决定碎片贪并终点。
3. **代码整块**：kind=code 原子——永不内切、不与邻接合并；超长整块保留不腰斩
   （ADR-018④/调研 §13.4），`Chunk.oversized` 留痕 + `ChunkingResult.oversized_units`
   计数（日志形态由流水线层承担：只记组件名/字符数，零内容）。
4. **表格聚合**：≤table_agg_rows 数据行聚为单 chunk；超限按组切分且**每组重携表头**
   （缓解 markitdown 表头语义瑕疵）；行为组原子切（单元格/占位符永不跨块）。
5. **PUA 占位符保护**（§1.18 / ir_schema 协议）：切分前 `protect_document_refs`
   完整性校验（残片 ValueError）+ PUA U+F0000 单字符替换 → 在保护空间选切点
   （占位符=单字符，结构性永不跨块）→ 逐片还原 → `find_document_refs` 按切片筛选
   `references`。偏移换算（保护空间 ↔ 原文空间）经占位符 span 收缩量精确互转；
   `char_start/char_end` 恒为**原文（document_text 扁平正文）坐标**。硬切窗在保护
   空间计量（含占位符文本的硬切窗长偏差 ≤ 占位符收缩字符数，罕见路径如实注记）。
6. **heading_path 物化**：每 chunk 携带完整路径（B19 溯源 O(1) 消费面）——
   **catalog_path 非空**时 = 知识库目录链 + 文档内标题路径逐级拼接：
   ① 完整链 = `catalog_path` 原样 + 接缝追加 `doc.title`（末位同名则不追加；**链内
   相邻同名保留**——嵌套同名目录合法，合并即断层）；② **交界回显不入轨迹**：
   文内标题路径的**文首连续段**中与任一链元素同名的标题滤除（文档序一次性闩锁：
   首个非回显标题后永久关闭——中后部同名一律保留），只动轨迹不动树。
   链的**两种形态均兼容**（链含本文档节点标题 / 链仅祖先由 doc.title 补接缝均可，
   拼接 + 接缝去重对两口径都无断层不重复）。链非空时**前导无标题片**轨迹 = 目录链
   本身（不再为空，消除轨迹断层）。
   **catalog_path 为空**（upload / 单文件语境）时完全维持旧口径
   `[doc.title] + 文档内标题链`（零去重、零行为变化——B17 既有行为）。
7. **content_hash**：`content_hash()` = sha256(text.strip()) hex 64——per-chunk
   （`Chunk.content_hash`，同文本同 hash）+ 文档级正文（`ChunkingResult.
   document_content_hash` = 全文扁平正文 hash——**kb_documents.content_hash 接线
   原料**（data-model §1.7 :272 / ADR-018④ / R26：正文 sha256 由入库流水线写入，
   OQ-34 同步 diff 兜底判据）。★注：kb_chunks 表契约（§1.8）**无** content_hash
   列、meta 七键亦不含——per-chunk hash 是否持久化由流水线/契约线裁决，本模块只
   产出计算面。
8. **字符范围**：char_start/char_end = chunk 新鲜内容（不含重叠尾巴）在
   `document_text`（`= "\n\n".join(全部叶子单元文本)`，heading 不入正文）中的
   扁平偏移；重叠尾巴为前片真实文本的前缀重复，不计入本 chunk 区间。
9. **kind/raw_type/node_id/loc**：取 chunk 首个来源单元（切分源粒度=§13.9② 建议；
   code/table 恒独立成 chunk，其 kind 恒精确；多单元文本 chunk 以首单元为准）。

业务在 services 层、纯函数零网络零 DB；kb_chunks 行写入由入库流水线消费本模块
产物（ChunkingResult）完成。日志纪律：本模块零日志调用，留痕走返回值计数与
`Chunk.oversized` 标志（外部内容零落日志）。
"""

import dataclasses
import hashlib
import math
import re
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.rag.ir_schema import (
    VIRTUAL_ROOT_ID,
    IrNode,
    IrNodeType,
    find_document_refs,
    make_document_ref,
    protect_document_refs,
    restore_document_refs,
)

# ---------------------------------------------------------------------------
# 配置（消费面：Nacos Agent Spec `kb` 节快照——配置三分法，AI 行为类热更）
# ---------------------------------------------------------------------------

#: 契约默认值（nacos-config.md §3 kb 节 v1.43；config_center/defaults.py SPEC_CONFIG
#: `kb` 节同源——键缺失回落此处，灰度安全）
DEFAULT_CHUNK_SIZE = 800
DEFAULT_CHUNK_OVERLAP = 120
DEFAULT_PAGE_MAX_CHARS = 1200
DEFAULT_PARA_MAX_CHARS = 400
DEFAULT_SENTENCE_MAX_CHARS = 200
DEFAULT_TABLE_AGG_ROWS = 10


@dataclass(frozen=True)
class ChunkingConfig:
    """逐级切分参数（Agent Spec `kb` 节投影；from_spec 只消费、不回写）。"""

    chunk_size: int = DEFAULT_CHUNK_SIZE
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP
    page_max_chars: int = DEFAULT_PAGE_MAX_CHARS
    para_max_chars: int = DEFAULT_PARA_MAX_CHARS
    sentence_max_chars: int = DEFAULT_SENTENCE_MAX_CHARS
    table_agg_rows: int = DEFAULT_TABLE_AGG_ROWS

    def __post_init__(self) -> None:
        for name in (
            "chunk_size",
            "page_max_chars",
            "para_max_chars",
            "sentence_max_chars",
            "table_agg_rows",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"ChunkingConfig.{name} 须为正整数：{value!r}")
        overlap = self.chunk_overlap
        if isinstance(overlap, bool) or not isinstance(overlap, int) or not (
            0 <= overlap < self.chunk_size
        ):
            raise ValueError(
                f"ChunkingConfig.chunk_overlap 须为整数且 0 ≤ overlap < "
                f"chunk_size（否则切片窗口不前进）：{overlap!r}"
            )

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any]) -> "ChunkingConfig":
        """Agent Spec 快照（AssetSnapshot.payload 形态）→ 配置。

        键缺失回落契约默认值（validator 已在配置中心承担严格校验——此处坏值显式
        ValueError 不静默，防坏配置静默改变切分行为）。
        """
        kb = spec.get("kb") if isinstance(spec, Mapping) else None
        kb = kb if isinstance(kb, Mapping) else {}
        chunking = kb.get("chunking")
        chunking = chunking if isinstance(chunking, Mapping) else {}

        def _int(section: Mapping[str, Any], key: str, default: int) -> int:
            value = section.get(key, default)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"Agent Spec kb.{key} 须为整数：{value!r}")
            return value

        return cls(
            chunk_size=_int(kb, "chunk_size", DEFAULT_CHUNK_SIZE),
            chunk_overlap=_int(kb, "chunk_overlap", DEFAULT_CHUNK_OVERLAP),
            page_max_chars=_int(chunking, "page_max_chars", DEFAULT_PAGE_MAX_CHARS),
            para_max_chars=_int(chunking, "para_max_chars", DEFAULT_PARA_MAX_CHARS),
            sentence_max_chars=_int(
                chunking, "sentence_max_chars", DEFAULT_SENTENCE_MAX_CHARS
            ),
            table_agg_rows=_int(chunking, "table_agg_rows", DEFAULT_TABLE_AGG_ROWS),
        )


# ---------------------------------------------------------------------------
# 产出模型（kb_chunks 行投影原料——流水线消费面）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Chunk:
    """单切片（data-model §1.8 kb_chunks 行 + meta 七键投影）。

    `content_hash` 为模块级计算面（见模块 docstring 第 7 条——kb_chunks 契约无此
    列，持久化归流水线/契约线裁决）；`oversized` 为代码/表格超长整块保留留痕。
    """

    document_id: str | None
    chunk_index: int
    content: str
    token_count: int
    heading_path: list[str]
    kind: str
    raw_type: Any
    node_id: str | None
    loc: dict[str, Any] | None
    references: list[dict[str, str]]
    char_start: int
    char_end: int
    content_hash: str
    oversized: bool = False


@dataclass(frozen=True)
class ChunkingResult:
    """单文档切分结果（正文 + 切片序列 + 留痕计数）。"""

    document_id: str | None
    chunks: list[Chunk]
    document_text: str
    document_content_hash: str
    skipped_units: int
    oversized_units: int


def content_hash(text: str) -> str:
    """归一化文本 sha256（同文本同 hash；64 hex——CHAR(64) 列宽对齐 §1.7）。

    归一化 = 首尾空白剥离（strip）——切片重排/重归一化下稳定的最小归一。
    """
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    """token 估算（kb_chunks.token_count 列「估算」口径）。

    启发式与 `app/agent/tools/url_reader.py:62-74` 同源（全宽字符 1 token/字、
    其余 4 字符 ≈ 1 token、向上取整）——services 层不 import agent 层，本地实现
    保持同一文档化近似，两处注释互指。
    """
    if not text:
        return 0
    total = 0.0
    for ch in text:
        total += 1.0 if unicodedata.east_asian_width(ch) in ("W", "F") else 0.25
    return math.ceil(total)


# ---------------------------------------------------------------------------
# 内部模型
# ---------------------------------------------------------------------------

_LEVEL_PAGE = 0
_LEVEL_PARA = 1
_LEVEL_SENTENCE = 2

#: 句边界终止符（含换行——软换行长文在句级获得切点）；ASCII '.' 后随空白/收尾
#: 才算句点（防 3.14 形态误切）
_SENTENCE_ENDERS = frozenset("。！？!?；;…\n")
_SENTENCE_CLOSERS = frozenset("\"'」』）)】〕")

_PARA_BREAK_RE = re.compile(r"\n{2,}")


@dataclass
class _Unit:
    """扁平叶子单元（原文坐标 + 保护空间文本）。"""

    node: IrNode
    kind: str
    raw_type: Any
    loc: dict[str, Any] | None
    text: str  # 原文空间（占位符完整形态）
    start: int  # 在 document_text 中的偏移（原文空间）
    end: int
    protected: str  # protect_document_refs 产物（无占位符时 == text）
    mapping: dict[str, str]  # PUA 字符 → 原占位符
    spans: list[tuple[int, int]]  # 占位符原文 span（有序）
    page_breaks: tuple[int, ...]  # 页界（保护空间坐标）
    rows: list[list[Any]] | None = None  # table 专属


@dataclass
class _Piece:
    """切片原料（原文坐标）；code/table 独立成 chunk（standalone）。"""

    unit: _Unit
    text: str  # 原文空间文本（占位符已还原）
    abs_start: int
    abs_end: int
    level: int | None  # None=整单元/代码/表格组
    standalone: bool = False
    oversized: bool = False
    tail_exempt: bool = False  # 硬切片自带重叠，不再叠尾巴


@dataclass
class _Buffer:
    """累积缓冲（目标窗贪并；tail=句子级重叠尾巴，不计 char 区间）。"""

    tail: str = ""
    pieces: list[_Piece] = field(default_factory=list)

    @property
    def size(self) -> int:
        if not self.pieces:
            return len(self.tail)
        total = len(self.tail) + sum(len(p.text) for p in self.pieces)
        total += 2 * sum(
            1
            for a, b in zip(self.pieces, self.pieces[1:])
            if a.unit is not b.unit
        )
        return total

    def content(self) -> str:
        parts: list[str] = [self.tail]
        prev: _Piece | None = None
        for piece in self.pieces:
            if prev is not None and prev.unit is not piece.unit:
                parts.append("\n\n")
            parts.append(piece.text)
            prev = piece
        return "".join(parts)


# ---------------------------------------------------------------------------
# 占位符偏移换算（保护空间 ↔ 原文空间）
# ---------------------------------------------------------------------------


def _placeholder_spans(text: str) -> list[tuple[int, int]]:
    """原文中占位符 span（有序）——公开 API 组合（find + make + 顺序 find），
    不触碰 ir_schema 私有正则。"""
    spans: list[tuple[int, int]] = []
    cursor = 0
    for ref in find_document_refs(text):
        full = make_document_ref(ref.node_id, ref.kind, ref.label)
        start = text.find(full, cursor)
        if start < 0:  # 防御：与 find_document_refs 顺序不一致（异常输入）
            raise ValueError("document-ref 占位符定位失败（异常输入）")
        end = start + len(full)
        spans.append((start, end))
        cursor = end
    return spans


def _orig_cut_to_prot(cut: int, spans: list[tuple[int, int]]) -> int:
    """原文切点 → 保护空间切点（落在占位符内部的切点吸附到其起点）。"""
    delta = 0
    for start, end in spans:
        if cut <= start:
            return cut - delta
        if cut < end:
            return start - delta
        delta += (end - start) - 1
    return cut - delta


def _prot_cut_to_orig(cut: int, spans: list[tuple[int, int]]) -> int:
    """保护空间切点 → 原文切点（PUA 字符前后切点分别映射 span 起/止）。"""
    delta = 0
    for start, end in spans:
        if cut <= start - delta:
            return cut + delta
        if cut == start - delta + 1:
            return end
        delta += (end - start) - 1
    return cut + delta


# ---------------------------------------------------------------------------
# 边界识别与逐级降级
# ---------------------------------------------------------------------------


def _cap_of(cfg: ChunkingConfig, level: int) -> int:
    if level == _LEVEL_PAGE:
        return cfg.page_max_chars
    if level == _LEVEL_PARA:
        return cfg.para_max_chars
    return cfg.sentence_max_chars


def _boundaries_at(unit: _Unit, level: int, i0: int, i1: int) -> list[int]:
    """(i0, i1) 开区间内的切点（保护空间坐标；切点=新段起点）。"""
    prot = unit.protected
    bounds: list[int] = []
    if level == _LEVEL_PAGE:
        bounds = [b for b in unit.page_breaks if i0 < b < i1]
    elif level == _LEVEL_PARA:
        # 切点=分隔符起点（"\n\n" 随**下一段**走——段间分隔在合并块内保留为
        # 段落边界、在 chunk 起始处被丢弃，见 _DocState.push）
        bounds = [
            m.start() for m in _PARA_BREAK_RE.finditer(prot, i0, i1) if i0 < m.start() < i1
        ]
    else:
        cursor = i0
        while cursor < i1:
            ch = prot[cursor]
            ender = ch in _SENTENCE_ENDERS or (
                ch == "."
                and (
                    cursor + 1 >= i1
                    or prot[cursor + 1].isspace()
                    or prot[cursor + 1] in _SENTENCE_CLOSERS
                )
            )
            if ender:
                bound = cursor + 1
                while bound < i1 and prot[bound] in _SENTENCE_CLOSERS:
                    bound += 1
                if bound < i1:
                    bounds.append(bound)
                cursor = bound
            else:
                cursor += 1
    return bounds


def _hard_cut(
    unit: _Unit, i0: int, i1: int, cfg: ChunkingConfig
) -> list[tuple[int, int, int, bool]]:
    """无任何语义边界 → 按窗硬切（保护空间窗口；切片自带重叠 → tail_exempt）。"""
    step = cfg.chunk_size - cfg.chunk_overlap
    pieces: list[tuple[int, int, int, bool]] = []
    pos = i0
    while pos < i1:
        end = min(pos + cfg.chunk_size, i1)
        pieces.append((pos, end, _LEVEL_SENTENCE, True))
        if end >= i1:
            break
        pos += step
    return pieces


def _ladder(
    unit: _Unit, i0: int, i1: int, level: int, cfg: ChunkingConfig
) -> list[tuple[int, int, int, bool]]:
    """逐级降级（保护空间窗口 [i0, i1) → 原料片 (pi, pj, level, hard)）。

    上限按**原文空间**长度计量（占位符收缩不影响判定）；边界在保护空间识别
    （占位符=单字符，结构性不被切中）；各级无边界则下钻，句级无边界硬切。
    """
    cap = _cap_of(cfg, level)
    orig_len = _prot_cut_to_orig(i1, unit.spans) - _prot_cut_to_orig(i0, unit.spans)
    if orig_len <= cap:
        return [(i0, i1, level, False)]
    bounds = _boundaries_at(unit, level, i0, i1)
    if not bounds:
        if level < _LEVEL_SENTENCE:
            return _ladder(unit, i0, i1, level + 1, cfg)
        return _hard_cut(unit, i0, i1, cfg)

    cuts = [i0, *[b for b in bounds if i0 < b < i1], i1]
    raw: list[tuple[int, int, int, bool]] = []

    def _orig_seg_len(a: int, b: int) -> int:
        return _prot_cut_to_orig(b, unit.spans) - _prot_cut_to_orig(a, unit.spans)

    seg_start = cuts[0]
    seg_len = 0
    for k in range(1, len(cuts)):
        piece_start, piece_end = cuts[k - 1], cuts[k]
        piece_len = _orig_seg_len(piece_start, piece_end)
        if piece_len > cap:
            # 单段仍超本级上限 → 逐级下钻（页面级超限下钻段落级，以此类推）
            if seg_len:
                raw.append((seg_start, piece_start, level, False))
                seg_start, seg_len = piece_end, 0
            else:
                seg_start = piece_end
            raw.extend(_ladder(unit, piece_start, piece_end, level + 1, cfg))
            continue
        if seg_len and seg_len + piece_len > cap:
            raw.append((seg_start, piece_start, level, False))
            seg_start, seg_len = piece_start, piece_len
        else:
            if seg_len == 0:
                seg_start = piece_start
            seg_len += piece_len
    if seg_len:
        raw.append((seg_start, cuts[-1], level, False))
    return raw


def _snap_tail(text: str, overlap: int) -> str:
    """重叠尾巴：末尾 ≤ overlap 字符，按最后一个句边界吸附（句级碎片跨 chunk
    时的上下文连续性）。"""
    if overlap <= 0 or not text:
        return ""
    window = text[-overlap:] if len(text) > overlap else text
    for idx in range(len(window) - 1, -1, -1):
        if window[idx] in _SENTENCE_ENDERS and window[idx] != "\n":
            return window[: idx + 1]
    return window


# ---------------------------------------------------------------------------
# 单元切分（code 整块 / table 分组 / 文本逐级降级）
# ---------------------------------------------------------------------------


def _render_table(node: IrNode) -> tuple[str, list[list[Any]] | None]:
    """table 节点 → markdown 管道表全文 + rows（缺 rows 视为空单元）。"""
    rows = node.meta.get("rows")
    if not isinstance(rows, list) or not rows:
        return "", None
    col_raw = node.meta.get("col_count")
    col = (
        col_raw
        if isinstance(col_raw, int) and col_raw > 0
        else max((len(r) for r in rows if isinstance(r, list)), default=0) or 1
    )

    def _fmt(row: list[Any]) -> str:
        cells = ["" if c is None else str(c) for c in row]
        if len(cells) < col:
            cells += [""] * (col - len(cells))
        return "| " + " | ".join(cells[:col]) + " |"

    lines = [_fmt(rows[0]), "| " + " | ".join(["---"] * col) + " |"]
    lines.extend(_fmt(r) for r in rows[1:] if isinstance(r, list))
    return "\n".join(lines), rows


def _table_pieces(unit: _Unit, cfg: ChunkingConfig) -> list[_Piece]:
    """表格聚合（行为组原子：单元格/占位符永不跨块；每组重携表头）。"""
    assert unit.rows is not None
    lines = unit.text.split("\n")
    header_lines = lines[:2]
    data_lines = lines[2:]
    # 行偏移（原文空间，相对 unit.text）
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    data_offsets = offsets[2:]
    groups = [
        data_lines[i : i + cfg.table_agg_rows]
        for i in range(0, len(data_lines), cfg.table_agg_rows)
    ] or [[]]
    pieces: list[_Piece] = []
    for gi, group in enumerate(groups):
        if not group and gi == 0 and not data_lines:
            group_text = "\n".join(header_lines)
            g_start, g_end = 0, len(unit.text)
        else:
            first = data_offsets[gi * cfg.table_agg_rows]
            last_line_idx = gi * cfg.table_agg_rows + len(group) - 1
            g_end = data_offsets[last_line_idx] + len(data_lines[last_line_idx])
            group_text = "\n".join(header_lines + group)
            if gi == 0:
                g_start = 0  # 首组内容=表头+数据，区间覆盖整段真实文本
            else:
                g_start = first  # 后续组重携表头为合成上下文，不计区间
        pieces.append(
            _Piece(
                unit=unit,
                text=group_text,
                abs_start=unit.start + g_start,
                abs_end=unit.start + g_end,
                level=None,
                standalone=True,
                oversized=len(group_text) > cfg.chunk_size,
                tail_exempt=True,
            )
        )
    return pieces


def _unit_pieces(unit: _Unit, cfg: ChunkingConfig) -> list[_Piece]:
    """单元 → 原料片（code 整块 / table 分组 / 文本逐级降级）。"""
    if unit.kind == IrNodeType.CODE.value:
        return [
            _Piece(
                unit=unit,
                text=unit.text,
                abs_start=unit.start,
                abs_end=unit.end,
                level=None,
                standalone=True,
                oversized=len(unit.text) > cfg.chunk_size,
                tail_exempt=True,
            )
        ]
    if unit.kind == IrNodeType.TABLE.value:
        return _table_pieces(unit, cfg)
    # 文本类（paragraph/list/quote/image）：逐级降级
    if len(unit.text) <= cfg.page_max_chars and not unit.spans:
        # 低于页面上限整单元成片（保护空间 == 原文空间，零换算快速路径）
        return [
            _Piece(
                unit=unit,
                text=unit.text,
                abs_start=unit.start,
                abs_end=unit.end,
                level=None,
                tail_exempt=True,
            )
        ]
    protected_len = len(unit.protected)
    raw = _ladder(unit, 0, protected_len, _LEVEL_PAGE, cfg)
    pieces: list[_Piece] = []
    for pi, pj, level, hard in raw:
        o_s = _prot_cut_to_orig(pi, unit.spans)
        o_e = _prot_cut_to_orig(pj, unit.spans)
        pieces.append(
            _Piece(
                unit=unit,
                text=restore_document_refs(unit.protected[pi:pj], unit.mapping),
                abs_start=unit.start + o_s,
                abs_end=unit.start + o_e,
                level=level,
                tail_exempt=hard,
            )
        )
    return pieces


# ---------------------------------------------------------------------------
# 单元构建与 DFS 扁平化
# ---------------------------------------------------------------------------

_TEXT_KINDS = frozenset(
    {
        IrNodeType.PARAGRAPH.value,
        IrNodeType.LIST.value,
        IrNodeType.QUOTE.value,
        IrNodeType.IMAGE.value,
    }
)


def _make_unit(node: IrNode, start: int, page_breaks: Mapping[str, Sequence[int]]) -> _Unit | None:
    """叶子节点 → _Unit（空文本 None=跳过）；文本类做占位符保护。"""
    kind = node.type.value
    rows: list[list[Any]] | None = None
    if kind == IrNodeType.TABLE.value:
        text, rows = _render_table(node)
    else:
        text = node.text or ""
    if not text.strip():
        return None
    if kind in _TEXT_KINDS:
        protected, mapping = protect_document_refs(text)  # 残片 → ValueError（§1.18）
        spans = _placeholder_spans(text) if mapping else []
        page_raw = page_breaks.get(node.id, ())
        page_breaks_prot = tuple(
            sorted(_orig_cut_to_prot(int(c), spans) for c in page_raw)
        )
    else:
        # code/table 无内部切点（整块/行为组原子），占位符随整块自然完整
        protected, mapping, spans = text, {}, []
        page_breaks_prot = ()
    return _Unit(
        node=node,
        kind=kind,
        raw_type=node.meta.get("raw_type"),
        loc=node.loc.model_dump(exclude_none=True) if node.loc is not None else None,
        text=text,
        start=start,
        end=start + len(text),
        protected=protected,
        mapping=mapping,
        spans=spans,
        page_breaks=page_breaks_prot,
        rows=rows,
    )


def _walk_document(doc: IrNode) -> Iterator[IrNode]:
    """迭代 DFS（pre-order；深树免递归上限——rebuild_siblings 同款形态）。"""
    stack: list[Iterator[IrNode]] = [iter(doc.children)]
    while stack:
        node = next(stack[-1], None)
        if node is None:
            stack.pop()
            continue
        yield node
        if node.children:
            stack.append(iter(node.children))


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def _build_chain(catalog: Sequence[str], doc_title: str | None) -> list[str]:
    """完整目录链 = catalog 原样 + 接缝追加 doc.title。

    **链内相邻同名保留**（嵌套同名目录是合法形态，合并即制造断层）+ **接缝一处去重**
    （doc.title 与 catalog 末位相同则不追加——同一层的两种口径：链含自身 / 链仅祖先，
    拼接后对两口径都无断层不重复）。`catalog_path` 传入值本身不做二次清洗（链值来自
    导入侧节点标题原文，语义上属展示链）。
    """
    chain = list(catalog)
    if doc_title and (not chain or chain[-1] != doc_title):
        chain.append(doc_title)
    return chain


@dataclass
class _DocState:
    """单文档切分游标（heading 栈 / 缓冲 / 序号 / 留痕）。"""

    document_id: str | None
    cfg: ChunkingConfig
    #: catalog_path 为空时的旧口径前缀（[doc.title]——B17 既有行为，链空分支零去重）
    base_path: list[str] = field(default_factory=list)
    #: 完整目录链（仅 catalog 非空时有意义；接缝去重见 _build_chain）
    chain: list[str] = field(default_factory=list)
    #: chain 成员集合（回显判定预计算——每个 heading 一次 O(1) 查表）
    chain_set: frozenset[str] = field(default_factory=frozenset)
    #: catalog_path 非空（目录链语义开关：关闭时轨迹物化零行为变化）
    has_catalog: bool = False
    #: **交界回显闩锁**（文档序一次性）：初值=has_catalog；文内首个非回显标题置 False
    #: 后永久关闭——回显判定只作用于文首连续段，中后部同名标题一律按普通标题保留
    echo_open: bool = False
    #: (level, title, is_echo)——is_echo 项不入 heading_path（树与缓冲语义不受影响）
    heading_stack: list[tuple[int, str, bool]] = field(default_factory=list)
    buffer: _Buffer = field(default_factory=_Buffer)
    last_piece: _Piece | None = None
    seq: int = 0
    chunks: list[Chunk] = field(default_factory=list)
    skipped_units: int = 0
    oversized_units: int = 0

    @property
    def heading_path(self) -> list[str]:
        # 目录链非空：heading_path = 完整目录链（catalog + 接缝 doc.title）+ 文档内
        # 标题路径（is_echo 项滤除——交界前导回显不入轨迹）。
        # 为空（upload/单文件语境）：完全维持现状（[doc.title] + 文档内标题链，零去重
        # ——纯 upload 通道轨迹逐字节不变）。
        if self.has_catalog:
            return [
                *self.chain,
                *(title for _, title, is_echo in self.heading_stack if not is_echo),
            ]
        return [*self.base_path, *(title for _, title, _ in self.heading_stack)]

    def heading_enter(self, node: IrNode) -> None:
        self.flush()
        level = node.level if node.level is not None else 1
        title = node.title or ""
        if not title:
            return
        # 交界回显判定是**文档序闩锁**（不是栈前缀过滤）：链非空时开，首个非回显标题
        # 关闭后不再判定——否则文内后部顶层同名（如另一章也叫「空间」）会被误滤。
        is_echo = self.echo_open and title in self.chain_set
        if not is_echo:
            self.echo_open = False
        while self.heading_stack and self.heading_stack[-1][0] >= level:
            self.heading_stack.pop()
        self.heading_stack.append((level, title, is_echo))

    def flush(self) -> None:
        if not self.buffer.pieces:
            self.buffer.tail = ""
            self.last_piece = None
            return
        content = self.buffer.content()
        first, last = self.buffer.pieces[0], self.buffer.pieces[-1]
        self.chunks.append(
            Chunk(
                document_id=self.document_id,
                chunk_index=self.seq,
                content=content,
                token_count=estimate_tokens(content),
                heading_path=self.heading_path,
                kind=first.unit.kind,
                raw_type=first.unit.raw_type,
                node_id=first.unit.node.id,
                loc=first.unit.loc,
                references=_chunk_references(content),
                char_start=first.abs_start,
                char_end=last.abs_end,
                content_hash=content_hash(content),
            )
        )
        self.seq += 1
        self.buffer = _Buffer()
        self.last_piece = None

    def push(self, piece: _Piece) -> None:
        if piece.standalone:
            self.flush()
            self._emit_standalone(piece)
            return
        if self.buffer.pieces:
            cross = 2 if self.buffer.pieces[-1].unit is not piece.unit else 0
            if self.buffer.size + cross + len(piece.text) > self.cfg.chunk_size:
                prev_piece = self.last_piece
                self.flush()
                if (
                    piece.level == _LEVEL_SENTENCE
                    and not piece.tail_exempt
                    and prev_piece is not None
                    and prev_piece.unit is piece.unit
                ):
                    self.buffer.tail = _snap_tail(prev_piece.text, self.cfg.chunk_overlap)
        if (
            not self.buffer.pieces
            and not self.buffer.tail
            and piece.text.startswith("\n\n")
        ):
            # chunk 边界替代段间分隔符：新 chunk 起始处的 leading "\n\n" 丢弃
            # （须在潜在 flush 之后判定——flush 前的旧 buffer 不构成本 chunk 起点）
            stripped = piece.text.lstrip("\n")
            lead = len(piece.text) - len(stripped)
            piece = dataclasses.replace(
                piece, text=stripped, abs_start=piece.abs_start + lead
            )
        self.buffer.pieces.append(piece)
        self.last_piece = piece

    def _emit_standalone(self, piece: _Piece) -> None:
        if piece.oversized:
            self.oversized_units += 1
        self.chunks.append(
            Chunk(
                document_id=self.document_id,
                chunk_index=self.seq,
                content=piece.text,
                token_count=estimate_tokens(piece.text),
                heading_path=self.heading_path,
                kind=piece.unit.kind,
                raw_type=piece.unit.raw_type,
                node_id=piece.unit.node.id,
                loc=piece.unit.loc,
                references=_chunk_references(piece.text),
                char_start=piece.abs_start,
                char_end=piece.abs_end,
                content_hash=content_hash(piece.text),
                oversized=piece.oversized,
            )
        )
        self.seq += 1


def _chunk_references(content: str) -> list[dict[str, str]]:
    """按切片筛选 references（§1.18——引用随切片走、永不悬空）。"""
    return [
        {"node_id": ref.node_id, "kind": ref.kind, "label": ref.label}
        for ref in find_document_refs(content)
    ]


def chunk_document(
    doc: IrNode,
    *,
    document_id: str | None = None,
    config: ChunkingConfig | None = None,
    page_breaks: Mapping[str, Sequence[int]] | None = None,
) -> ChunkingResult:
    """单文档 IR → 切片结果（逐级切分主入口）。

    `page_breaks`：节点 id → 该节点文本内的页界偏移（原文坐标）——页式来源由
    归一化/流水线线供给；缺省无页界，页面级自动下钻段落级（钉住语义第 1 条）。
    非文档节点 → ValueError（编程错误，先于任何处理）。
    """
    if doc.type is not IrNodeType.DOCUMENT:
        raise ValueError(f"chunk_document 仅接受 document 节点：{doc.type!r}")
    cfg = config if config is not None else ChunkingConfig()
    breaks: Mapping[str, Sequence[int]] = page_breaks or {}

    # 单趟 DFS pre-order：heading 事件驱动缓冲 flush 与路径栈，叶子单元即时
    # 切片入缓冲（事件序 = chunk 序——两阶段拆分会丢失 heading 边界语义）
    # 目录链与 chain_set 在切分前预计算一次（heading 每遇一次 O(1) 查表）。
    # 链**两种形态均兼容**（链含本文档节点标题 / 链仅祖先 + doc.title 补接缝均可，
    # 拼接 + 接缝去重对两口径都无断层不重复）；为空时维持 B17 现状口径
    # （base_path = catalog + [doc.title] 不参与轨迹物化——轨迹 = [doc.title] + 文内标题链）。
    catalog = list(doc.catalog_path or [])
    has_catalog = bool(catalog)
    base_path = [*catalog, *([doc.title] if doc.title else [])]
    chain = _build_chain(catalog, doc.title)
    state = _DocState(
        document_id=document_id,
        cfg=cfg,
        base_path=base_path,
        chain=chain,
        chain_set=frozenset(chain),
        has_catalog=has_catalog,
        echo_open=has_catalog,
    )
    text_parts: list[str] = []
    flat_len = 0
    for node in _walk_document(doc):
        if node.type is IrNodeType.HEADING:
            state.heading_enter(node)
            continue
        unit = _make_unit(node, flat_len, breaks)
        if unit is None:
            state.skipped_units += 1
            continue
        text_parts.append(unit.text)
        flat_len = unit.end + 2  # "\n\n" 分隔
        for piece in _unit_pieces(unit, cfg):
            state.push(piece)

    document_text = "\n\n".join(text_parts)
    state.flush()

    return ChunkingResult(
        document_id=document_id,
        chunks=state.chunks,
        document_text=document_text,
        document_content_hash=content_hash(document_text),
        skipped_units=state.skipped_units,
        oversized_units=state.oversized_units,
    )


def chunk_forest(
    root: IrNode,
    *,
    document_id: str | None = None,
    config: ChunkingConfig | None = None,
    page_breaks: Mapping[str, Sequence[int]] | None = None,
) -> list[ChunkingResult]:
    """Forest 根（virtual:root 或非文档容器）→ 逐文档切分结果列表。

    根自身为文档节点（非虚拟根哨兵）时按单文档处理；其余形态迭代 document
    子节点（非文档子节点跳过——Forest 契约下根层只挂文档单元）。
    """
    if root.id != VIRTUAL_ROOT_ID and root.type is IrNodeType.DOCUMENT:
        return [
            chunk_document(
                root, document_id=document_id, config=config, page_breaks=page_breaks
            )
        ]
    return [
        chunk_document(
            child, document_id=document_id, config=config, page_breaks=page_breaks
        )
        for child in root.children
        if child.type is IrNodeType.DOCUMENT
    ]
