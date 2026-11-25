"""飞书 SDK blocks → 统一树 IR 映射器（M3 批次 3 / B20 A 线②——纯函数，零网络零 DB）。

契约锚点：data-model.md §1.18「SDK blocks → IR 映射细则」（2026-11-25 B20 契约批钉定，
确认点②收尾——通用规则 + 42 行映射表 + 块号勘误存档）与 §1.7 parse_meta.feishu
存储侧附加键。本模块为映射表的实现事实源（§1.18 事实源分层句：契约钉表，代码为
实现事实源）；块号字面表以 notebook `feishu_kb_tree_probe.ipynb` 一手锚点为准
（31=table / 19=callout / 34=quote_container / 33=view——SDK `_types` 字段序不可
推导，轻审 P2-1 实证）。

通用规则（§1.18 :549 逐条）：
- 节点 ID：块 `fs:blk:{block_id}`、文档 `fs:node:{node_token}`；
- raw_type：`feishu.<块名>`（枚举外 `feishu.block_<编号>`）；文档节点 meta.raw_type
  = wiki 节点 obj_type 原值、meta.obj_edit_time 契约键；
- **code(14) / table(31) 不降级规则**：无论嵌在哪个容器内恒产出独立 code/table 节点
  （工单 E①② 结构性保证）；
- 容器透明展开：callout(19)/grid(24)/grid_column(25)/view(33)/okr(36) 自身不产节点，
  子块按序挂容器父级；
- 枚举外（16、>42、0）降级（C3=(a)）：自身降 paragraph（无可提取文本则不产节点）、
  children 递归不丢、degraded_blocks["block_<编号>"] 留痕；
- 行内元素：text_run 取 content（带 link 记 `[content](url)`）、mention_doc 树内落
  `<document-ref>` 占位符（id=`fs:node:{目标 node_token}`）树外仅留标题、equation 取
  公式原值、mention_user/inline file/inline_block/link_preview 沿 `[占位]` 字面、
  reminder 丢弃。★挂账（评审 P3，不视为本批缺陷——核实本批无绕过：app/agent 与
  app/services 现零 app.rag 消费）：text_run link url 原样入 KB 语料，**B19 检索
  接线批**必须审 KB 语料消费面是否走 B15 `<external_content>` 包裹 + url_guard
  （ADR-015 第四消费面既有钉注），并裁 link url 的出站/展示形态。

映射要点（映射表逐行）：
- 合并容器（bullet(12)/ordered(13)/todo(17)/quote_container(34)）：子块文本换行拼接
  为一个 list/quote 节点；内嵌 14/31（及 27/23 叶子形态）按不降级规则提升为兄弟节点
  ——正文与结构零丢失；todo 文本前缀 `[x] `/`[ ] `、meta.done=done 原值；
- table(31)：子块 32 吸收进 meta.rows/meta.col_count；合并单元格按「文本落合并区
  左上格、其余格空串」实现 + meta.merged_cells 留痕（merge_info 实测形态待核——
  形态不符时整体忽略防御）；格内 code 文本并入格（矩阵 row_size/column_size 定形，
  不拆独立节点——E①② 的伪表格缺陷治理针对顶层块，格内吸收保正文）；
  **meta 扩容（2026-11-25 表格双工单契约批 v1.20——data-model §1.18 映射表 31 行）**：
  ① `meta.cell_spans` 稠密同构 R×C 矩阵（与 meta.rows 同构、每格 [row_span, col_span]
  ——锚点格承载真实跨度、其余格 [1,1]；无合并区整键省略；构建=行优先扫描+已覆盖
  掩码依 merge_info 序放置锚点；自检失败〔锚点越界/面积溢出/重叠覆盖/矩阵不完整〕
  → 整表降级省略键 + degraded_blocks["table_cell_spans"] 留痕——key 名为实现事实源，
  契约只钉 parse_meta.feishu.degraded_blocks 载体）；② `meta.header_row` /
  `meta.header_column` TableProperty 原值透传（真值写入；false/缺省 → 键省略——
  消费侧缺省 false 同语义，meta 字节最小化沿 cell_spans 缺省省略先例）；消费面=
  chunker 渲染前向填充锚点文本（meta.rows 锚点单写形态不变）；kb_chunks.meta 七键
  投影零扩展；升格节点不计入 degraded_blocks；
- image(27)：先占位 meta.placeholder=true + meta.image_token，素材下载入 MinIO 后由
  worker 回填 meta.minio_key；下载失败保留占位不算文档失败；
- file(23)：file_attachment + children=[] + meta.parse_status="pending"（§13.9⑥ 字面）
  + meta.file_token/meta.file_name；下载后走 REQ-019 归一化管线；
- table 占位升格（30 sheet / 18 bitable，v1.20 契约批）：type=table + `[sheet]`/`[bitable]`
  占位字面恒保留 + meta.placeholder=true + meta.sheet_token/bitable_token 原值留痕；
  升格不计入 degraded_blocks（非降级）；回填=worker 读 sheets API 翻 placeholder +
  物化 meta.rows/meta.col_count（仅 30；18 挂后续工单）；
- paragraph 占位（20/21/26/28/29/35/37~42）：方括号占位字面 + raw_type 留痕 +
  计入 degraded_blocks（按块名计数）。

待核项处置（§1.18 :587 实现批待核项）：ir_schema 键名统一 obj_edit_time（已落
ir_schema.py）；33 view 按「不丢正文」先展开 + transparent_counts 留痕（实现面诊断，
不进 parse_meta——契约 degraded_blocks 仅计降级/占位块）；31 merge_info 见上；slides
obj_type 字面沿实测（不做字面假设——worker 层 unsupported_type 面）。
"""

from dataclasses import dataclass, field
from typing import Any

from app.rag.ir_schema import (
    DOC_KINDS,
    FeishuLoc,
    IrNode,
    make_document_ref,
)
from app.rag.tree_assembler import BlockRef

#: SDK block_type → 官方块名字面表（notebook feishu_kb_tree_probe.ipynb cell 8 一手材料；
#: 16 未文档化不作块类型；锚点断言见 test_kb_ir_schema.py / 本表 == 断言）
BLOCK_FIELD: dict[int, str] = {
    1: "page", 2: "text",
    3: "heading1", 4: "heading2", 5: "heading3", 6: "heading4",
    7: "heading5", 8: "heading6", 9: "heading7", 10: "heading8", 11: "heading9",
    12: "bullet", 13: "ordered", 14: "code", 15: "quote",
    17: "todo", 18: "bitable", 19: "callout", 20: "chat_card", 21: "diagram",
    22: "divider", 23: "file", 24: "grid", 25: "grid_column", 26: "iframe",
    27: "image", 28: "isv", 29: "mindnote", 30: "sheet", 31: "table",
    32: "table_cell", 33: "view", 34: "quote_container", 35: "task",
    36: "okr", 37: "okr_objective", 38: "okr_key_result", 39: "okr_progress",
    40: "add_ons", 41: "jira_issue", 42: "wiki_catalog",
}

#: 透明容器（自身不产节点、子块按序挂容器父级）——19/24/25/33/36
_TRANSPARENT_TYPES: frozenset[int] = frozenset({19, 24, 25, 33, 36})
#: 不产节点（page=文档根 / divider=分隔线）
_NO_NODE_TYPES: frozenset[int] = frozenset({1, 22})
#: 合并容器（子块文本换行拼接为一个节点）
_MERGE_TYPES: frozenset[int] = frozenset({12, 13, 17, 34})
#: 合并容器内提升为兄弟节点的独立形态（14/31 不降级规则 + 27/23 叶子形态不丢）
_PROMOTE_TYPES: frozenset[int] = frozenset({14, 31, 27, 23})
#: paragraph 占位（方括号占位字面 + degraded_blocks 按块名计数）
_PLACEHOLDER_TYPES: frozenset[int] = frozenset(
    {20, 21, 26, 28, 29, 35, 37, 38, 39, 40, 41, 42}
)
#: table 占位升格（data-model §1.18 v1.20 表格双工单契约批 2026-11-25——30 sheet /
#: 18 bitable：type=table + 方括号占位字面恒保留 + placeholder=true + token 原值留痕；
#: 升格不计入 degraded_blocks（非降级）；内容回填=worker 按 sheets API 通道承接
#: （仅 30 sheet；18 bitable 走 Base API 域、本批零回填，回填协议不钉））
_SHEET_UPGRADE_TYPES: frozenset[int] = frozenset({30, 18})
#: 行内占位字面（探针 ipynb:538-561 形态；reminder 丢弃不占位）
_INLINE_PLACEHOLDER = {
    "mention_user": "[mention_user]",
    "file": "[file]",
    "inline_block": "[inline_block]",
    "link_preview": "[link_preview]",
}

#: SDK 直读文档的 parser 通道标识（data-model §1.7 / A34 GWT#1 判定面载体）
PARSER_FEISHU_BLOCKS = "feishu_blocks"


@dataclass(slots=True)
class FeishuMappingResult:
    """映射产物：全量平铺 refs（可直接喂 `tree_assembler.attach_block_tree`，
    各 ref.parent_id/children_ids 已按最终 IR 结构双写）+ 留痕。

    degraded_blocks：`{块名或 block_<编号>: 计数}`（§1.7 parse_meta.feishu.degraded_blocks
    载体——降级/占位块；透明容器不计入）；transparent_counts：透明容器展开计数
    （实现面诊断留痕——33 view 子块形态待核的观测点，不进 parse_meta）。
    """

    refs: list[BlockRef] = field(default_factory=list)
    root_order: list[str] = field(default_factory=list)
    degraded_blocks: dict[str, int] = field(default_factory=dict)
    transparent_counts: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class _Ctx:
    space_id: str
    node_token: str
    doc_id: str
    block_map: dict[str, Any]
    known_node_tokens: frozenset[str]
    node_token_by_obj_token: dict[str, str]
    result: FeishuMappingResult
    emitted: set[str] = field(default_factory=set)  # 已产节点 block_id（防畸形双父引用重复产节点）
    #: 递归路径防环（评审 P3：畸形循环 children 引用会无限递归——路径制 add/discard，
    #: DAG 菱形重复映射既有行为不变；飞书真树无环，纯防御，tree_assembler 先例同款）
    visiting: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# SDK 假体友好的字段读取（对齐 lark-oapi 1.8.0 Block 形态；duck-typing）
# ---------------------------------------------------------------------------


def _type_of(block: Any) -> int:
    t = getattr(block, "block_type", None)
    return t if isinstance(t, int) else 0


def _payload(block: Any) -> Any:
    """块类型载荷（block.text / block.code / block.table …；枚举外无已知字段 → None）。"""
    name = BLOCK_FIELD.get(_type_of(block))
    return getattr(block, name, None) if name else None


def _unknown_payload(block: Any) -> Any | None:
    """枚举外块的防御性载荷探测（§1.18 通用规则「可提取文本」的读取面）：
    未文档化编号无已知字段名，但新增/历史块可能复用已知载荷形态（elements 结构）——
    遍历已知字段名取首个带 elements 的非空载荷；无则 None（正文按空处理，不产节点）。"""
    for name in BLOCK_FIELD.values():
        candidate = getattr(block, name, None)
        if candidate is not None and getattr(candidate, "elements", None) is not None:
            return candidate
    return None


def _inline_text_from(payload: Any, ctx: "_Ctx") -> str:
    if payload is None:
        return ""
    return "".join(_inline_element_text(elem, ctx) for elem in _elements(payload))


def _child_ids(block: Any) -> list[str]:
    return [c for c in (getattr(block, "children", None) or []) if c]


def _elements(payload: Any) -> list[Any]:
    return [e for e in (getattr(payload, "elements", None) or []) if e is not None]


# ---------------------------------------------------------------------------
# 行内元素（通用规则）
# ---------------------------------------------------------------------------


def _mention_doc_text(mention: Any, ctx: _Ctx) -> str:
    token = getattr(mention, "token", None) or ""
    title = getattr(mention, "title", None) or ""
    target = token if token in ctx.known_node_tokens else ctx.node_token_by_obj_token.get(token)
    if target and target in ctx.known_node_tokens:
        # 树内：document-ref 占位符（id=fs:node:{目标 node_token}——§1.18 占位符契约）
        return make_document_ref(f"fs:node:{target}", "document", title)
    return title  # 不在树内：仅保留标题文字


def _inline_element_text(elem: Any, ctx: _Ctx) -> str:
    run = getattr(elem, "text_run", None)
    if run is not None:
        content = getattr(run, "content", None) or ""
        style = getattr(run, "text_element_style", None)
        url = getattr(getattr(style, "link", None), "url", None) if style is not None else None
        return f"[{content}]({url})" if url else content
    if getattr(elem, "mention_doc", None) is not None:
        return _mention_doc_text(elem.mention_doc, ctx)
    if getattr(elem, "mention_user", None) is not None:
        return _INLINE_PLACEHOLDER["mention_user"]
    if getattr(elem, "equation", None) is not None:
        return getattr(elem.equation, "content", None) or ""
    if getattr(elem, "file", None) is not None:
        return _INLINE_PLACEHOLDER["file"]
    if getattr(elem, "inline_block", None) is not None:
        return _INLINE_PLACEHOLDER["inline_block"]
    if getattr(elem, "link_preview", None) is not None:
        return _INLINE_PLACEHOLDER["link_preview"]
    return ""  # reminder 丢弃（契约通用规则；未知元素同样零输出）


def _inline_text(block: Any, ctx: _Ctx) -> str:
    return _inline_text_from(_payload(block), ctx)


def _code_text(block: Any) -> str:
    """code(14)：elements 逐字拼接——保留换行与缩进、不 strip（§1.18 映射表 14 行）。"""
    payload = _payload(block)
    if payload is None:
        return ""
    parts: list[str] = []
    for elem in _elements(payload):
        run = getattr(elem, "text_run", None)
        if run is not None:
            parts.append(getattr(run, "content", None) or "")
    return "".join(parts)


# ---------------------------------------------------------------------------
# 节点构造
# ---------------------------------------------------------------------------


def _loc(block_id: str, ctx: _Ctx) -> FeishuLoc:
    return FeishuLoc(
        origin="feishu",
        space_id=ctx.space_id,
        node_token=ctx.node_token,
        block_id=block_id,
    )


def _make_node(
    block: Any,
    parent_ir_id: str,
    node_type: str,
    ctx: _Ctx,
    meta: dict[str, Any],
    *,
    text: str | None = None,
    title: str | None = None,
    level: int | None = None,
    callout_direct: bool = False,
) -> IrNode:
    if callout_direct:
        meta = {**meta, "callout": True}  # callout(19) 直接子节点标记（映射表 19 行）
    return IrNode(
        id=f"fs:blk:{block.block_id}",
        parent_id=parent_ir_id,
        type=node_type,  # type: ignore[arg-type]
        title=title,
        level=level,
        text=text,
        loc=_loc(block.block_id, ctx),
        meta=meta,
    )


def _emit(
    block: Any,
    parent_ir_id: str,
    node_type: str,
    ctx: _Ctx,
    meta: dict[str, Any],
    *,
    text: str | None = None,
    title: str | None = None,
    level: int | None = None,
    callout_direct: bool = False,
    children_ids: list[str] | None = None,
) -> str:
    ref = BlockRef(
        node=_make_node(
            block, parent_ir_id, node_type, ctx, meta,
            text=text, title=title, level=level, callout_direct=callout_direct,
        ),
        children_ids=children_ids or [],
    )
    ctx.result.refs.append(ref)
    ctx.emitted.add(block.block_id)
    return ref.node.id


def _count_degraded(ctx: _Ctx, key: str) -> None:
    ctx.result.degraded_blocks[key] = ctx.result.degraded_blocks.get(key, 0) + 1


# ---------------------------------------------------------------------------
# 逐类型映射
# ---------------------------------------------------------------------------


def _map_children(child_ids: list[str], parent_ir_id: str, ctx: _Ctx, callout_direct: bool) -> list[str]:
    out: list[str] = []
    for cid in child_ids:
        blk = ctx.block_map.get(cid)
        if blk is None:
            continue  # 引用缺失（未取回块）：跳过不崩
        if cid in ctx.visiting:
            continue  # 防环（评审 P3）：循环引用跳过该边——块不重入（路径制，finally 摘除）
        ctx.visiting.add(cid)
        try:
            out.extend(_map_block(blk, parent_ir_id, ctx, callout_direct))
        finally:
            ctx.visiting.discard(cid)
    return out


def _build_cell_spans(
    row_count: int, col_count: int, merge_info: list[Any]
) -> list[list[list[int]]] | None:
    """merge_info（与 cells 行主序逐格对齐）→ 稠密 R×C 跨度矩阵（data-model §1.18
    映射表 31 行 v1.20 钉死形态：平铺行主序逐格放置，锚点格 [row_span, col_span]、
    其余格 [1,1]，折行返回与 meta.rows 同构的嵌套矩阵）。

    构建自检四判（任一命中 → None=整表降级省略键；留痕由调用方写 degraded_blocks）：
    ① 锚点越界——锚点位置出矩阵（防御未来 merge_info 语义漂移）；
    ② 面积溢出——跨度出矩阵下/右边界；
    ③ 重叠覆盖——同一格被两个锚点声明（契约字面「消费后仍有未覆盖格」的覆盖
      唯一性判据：稠密形态下每格须恰被一个声明覆盖）；
    ④ 矩阵不完整——merge_info 数 ≠ R×C（cells 短行/缺格 → 有未覆盖格残留）。
    """
    if row_count <= 0 or col_count <= 0 or len(merge_info) != row_count * col_count:
        return None  # 自检④：矩阵不完整（未覆盖格残留）
    grid: list[list[int]] = [[1, 1] for _ in range(row_count * col_count)]  # 平铺逐格（每格 [row_span, col_span]）
    claimed = [False] * (row_count * col_count)
    for idx, info in enumerate(merge_info):
        if info is None:
            continue  # 无跨度信息：保持 [1,1]
        row_span = getattr(info, "row_span", 1) or 1
        col_span = getattr(info, "col_span", 1) or 1
        if not isinstance(row_span, int) or not isinstance(col_span, int):
            return None  # 形态漂移（非整数跨度）：整体降级
        if isinstance(row_span, bool) or isinstance(col_span, bool):
            return None  # 布尔形态拒收（严格整数纪律，validator 同款）
        if row_span <= 1 and col_span <= 1:
            continue
        r0, c0 = divmod(idx, col_count)
        if r0 >= row_count or c0 >= col_count:
            return None  # 自检①：锚点越界
        if r0 + row_span > row_count or c0 + col_span > col_count:
            return None  # 自检②：面积溢出
        for dr in range(row_span):
            for dc in range(col_span):
                pos = (r0 + dr) * col_count + (c0 + dc)
                if claimed[pos]:
                    return None  # 自检③：重叠覆盖（锚点格自身被先序锚点声明同判）
                claimed[pos] = True
        grid[idx] = [row_span, col_span]  # 锚点承载真实跨度；覆盖格保持 [1,1]
    return [grid[r * col_count : (r + 1) * col_count] for r in range(row_count)]


def _map_table(block: Any, parent_ir_id: str, ctx: _Ctx, callout_direct: bool) -> str:
    """table(31)：子块 32 吸收进 meta.rows/meta.col_count（映射表 31 行）。

    合并单元格：文本落合并区左上格、其余格空串 + meta.merged_cells 留痕；merge_info
    形态不符（长度与 cells 不对齐/缺列数）→ 整体忽略（实测形态待核的防御面）。
    格内 code(14) 文本并入格（逐字保留换行缩进）——矩阵由 row_size/column_size 定形，
    不拆独立节点；顶层块的 14/31 不降级规则不受影响。

    meta 扩容（v1.20 契约批，data-model §1.18 映射表 31 行）：合并区在场时随写
    `meta.cell_spans` 稠密同构矩阵（`_build_cell_spans`，自检失败 → 整表降级省略键 +
    degraded_blocks["table_cell_spans"] 留痕——meta.rows/merged_cells 既有口径零变化，
    降级只省新键）；`meta.header_row` / `meta.header_column` TableProperty 原值透传
    （真值写入；false/缺省 → 键省略，消费侧缺省 false 同语义）——chunker 表头
    前向填充（每组重携表头）的判定原料；kb_chunks.meta 七键投影零扩展。
    """
    payload = getattr(block, "table", None)
    prop = getattr(payload, "property", None)
    cell_ids = [c for c in (getattr(payload, "cells", None) or _child_ids(block)) if c]
    col_count = getattr(prop, "column_size", None) if prop is not None else None

    flat: list[str] = []
    for cid in cell_ids:
        cell = ctx.block_map.get(cid)
        flat.append(_cell_text(cell, ctx))
    if col_count:
        rows = [flat[i : i + col_count] for i in range(0, len(flat), col_count)] or [[]]
    else:
        rows = [flat] if flat else [[]]
    col = col_count or max((len(r) for r in rows), default=0) or 1

    merged_cells = 0
    cell_spans: list[list[list[int]]] | None = None
    merge_info = (getattr(prop, "merge_info", None) or []) if prop is not None else []
    if merge_info and col_count and len(merge_info) == len(cell_ids):
        covered: set[tuple[int, int]] = set()
        for idx, info in enumerate(merge_info):
            if info is None:
                continue
            row_span = getattr(info, "row_span", 1) or 1
            col_span = getattr(info, "col_span", 1) or 1
            if row_span == 1 and col_span == 1:
                continue
            r0, c0 = divmod(idx, col)
            for r in range(r0, min(r0 + row_span, len(rows))):
                for c in range(c0, min(c0 + col_span, col)):
                    if (r, c) != (r0, c0):
                        covered.add((r, c))
        for r, c in covered:
            if r < len(rows) and c < len(rows[r]):
                rows[r][c] = ""
        merged_cells = len(covered)
        # v1.20 扩容：稠密跨度矩阵（自检失败 → 降级省略键 + degraded_blocks 留痕；
        # 行文本/merged_cells 既有口径不受影响）
        cell_spans = _build_cell_spans(len(rows), col, merge_info)
        if cell_spans is not None and all(
            span == [1, 1] for span_row in cell_spans for span in span_row
        ):
            cell_spans = None  # 退化形态（merge_info 在场但全 [1,1]=无真实合并区）：
            # 整键省略（契约「缺省=全 [1,1]」缺省语义），非自检失败——零 degraded 留痕
        elif cell_spans is None:
            _count_degraded(ctx, "table_cell_spans")

    meta: dict[str, Any] = {
        "raw_type": "feishu.table",
        "rows": rows,
        "col_count": col,
    }
    if merged_cells:
        meta["merged_cells"] = merged_cells  # 留痕：被吸收格计数
    if cell_spans is not None:
        meta["cell_spans"] = cell_spans
    # header_row/header_column 原值透传（v1.20；真值写入，false/缺省省略——消费侧
    # .get 缺省 false 同语义；表头前向填充〔每组重携表头〕的判定原料）
    if prop is not None:
        header_row = getattr(prop, "header_row", None)
        header_column = getattr(prop, "header_column", None)
        if header_row:
            meta["header_row"] = header_row
        if header_column:
            meta["header_column"] = header_column
    return _emit(block, parent_ir_id, "table", ctx, meta, callout_direct=callout_direct)


def _cell_text(cell: Any, ctx: _Ctx) -> str:
    """table_cell(32) 文本：子块按序提取、换行拼接（31 吸收面；被 31 吸收的 32 不单独产节点）。"""
    if cell is None:
        return ""
    parts: list[str] = []
    seen: set[str] = set()  # 格内防环（评审 P3：畸形循环子块引用——每块文本提取一次）

    def _rec(block: Any) -> None:
        bid = getattr(block, "block_id", None)
        if bid:
            if bid in seen:
                return
            seen.add(bid)
        t = _type_of(block)
        if t == 14:  # 格内 code：逐字保留换行缩进并入格文本
            parts.append(_code_text(block))
            return
        if t in _TRANSPARENT_TYPES or t in _NO_NODE_TYPES:
            for cid in _child_ids(block):
                sub = ctx.block_map.get(cid)
                if sub is not None:
                    _rec(sub)
            return
        if t in _PLACEHOLDER_TYPES:
            parts.append(f"[{BLOCK_FIELD[t]}]")
            return
        txt = _inline_text(block, ctx)
        if txt:
            parts.append(txt)
        for cid in _child_ids(block):
            sub = ctx.block_map.get(cid)
            if sub is not None:
                _rec(sub)

    for cid in _child_ids(cell):
        blk = ctx.block_map.get(cid)
        if blk is not None:
            _rec(blk)
    return "\n".join(p for p in parts if p)


def _map_sheet_upgrade(block: Any, t: int, parent_ir_id: str, ctx: _Ctx, callout_direct: bool) -> str:
    """sheet(30)/bitable(18) 占位升格为 table（§1.18 v1.20 映射表 30/18 行）。

    - type=table；text=`[sheet]`/`[bitable]` 占位字面**恒保留**（纯文本消费面兜底）；
    - raw_type=`feishu.sheet`/`feishu.bitable` + `meta.placeholder=true`；
    - `meta.sheet_token`/`meta.bitable_token` = 块载荷 token 原值留痕（SDK 1.8.0 内省
      实测 `Sheet{token, row_size, column_size}` / `Bitable{token, view_type}`；载荷或
      token 缺失 → 空串，worker 回填扫描按空 token 跳过）；
    - **升格不计入 degraded_blocks**（非降级，有明确协议面——契约两行同款钉注）；
    - children 不映射（31 真表格同款：矩阵形态类型不挂子节点——docx 内嵌 sheet/bitable
      块实际无 children；正文零丢失由回填段承接）。
    """
    name = BLOCK_FIELD[t]  # "sheet" | "bitable"
    payload = _payload(block)
    token = getattr(payload, "token", None) or ""
    meta: dict[str, Any] = {
        "raw_type": f"feishu.{name}",
        "placeholder": True,
        f"{name}_token": token,
    }
    return _emit(
        block, parent_ir_id, "table", ctx, meta,
        text=f"[{name}]", callout_direct=callout_direct,
    )


def _map_placeholder(block: Any, parent_ir_id: str, ctx: _Ctx, callout_direct: bool) -> str:
    """paragraph 占位：方括号占位字面 + raw_type 留痕 + degraded_blocks 按块名计数。"""
    name = BLOCK_FIELD[_type_of(block)]
    _count_degraded(ctx, name)
    return _emit(
        block,
        parent_ir_id,
        "paragraph",
        ctx,
        {"raw_type": f"feishu.{name}"},
        text=f"[{name}]",
        callout_direct=callout_direct,
    )


def _map_merge_container(block: Any, t: int, parent_ir_id: str, ctx: _Ctx, callout_direct: bool) -> list[str]:
    """合并容器（12/13/17/34）：子块文本换行拼接为一个 list/quote 节点。

    内嵌 14/31（不降级规则）与 27/23（叶子形态不丢）提升为兄弟节点（挂容器父级、
    顺次排在合并节点后）；透明容器与 page/divider 在收集面原地展开；其余块计入文本
    拼接（嵌套合并容器/标题/引用/占位/枚举外均可提取文本——正文零丢失）。
    """
    own = _inline_text(block, ctx)
    if t == 17:  # todo：文本前缀 [x]/[ ] + meta.done 原值
        style = getattr(_payload(block), "style", None)
        done = getattr(style, "done", None) if style is not None else None
        own = (f"[x] {own}" if done else f"[ ] {own}") if own else ""
    else:
        done = None

    text_parts: list[str] = [own] if own else []
    promoted: list[str] = []

    def _collect(child_ids: list[str]) -> None:
        for cid in child_ids:
            blk = ctx.block_map.get(cid)
            if blk is None:
                continue
            ct = _type_of(blk)
            if ct in _PROMOTE_TYPES:
                # 提升为合并节点的兄弟（挂 parent_ir_id；合并容器收集面统一非 callout 直连）
                promoted.extend(_map_block(blk, parent_ir_id, ctx, callout_direct=False))
            elif ct in _TRANSPARENT_TYPES:
                name = BLOCK_FIELD[ct]
                ctx.result.transparent_counts[name] = ctx.result.transparent_counts.get(name, 0) + 1
                _collect(_child_ids(blk))
            elif ct in _NO_NODE_TYPES:
                _collect(_child_ids(blk))
            else:
                txt = _collect_text_of(blk, ct, ctx)
                if txt:
                    text_parts.append(txt)
                _collect(_child_ids(blk))

    def _collect_text_of(blk: Any, ct: int, c: _Ctx) -> str:
        if ct in _MERGE_TYPES:  # 嵌套合并容器：自身文本并入（children 由 _collect 继续）
            txt = _inline_text(blk, c)
            if ct == 17:
                style = getattr(_payload(blk), "style", None)
                if txt and getattr(style, "done", None):
                    txt = f"[x] {txt}"
                elif txt:
                    txt = f"[ ] {txt}"
            return txt
        if ct in _PLACEHOLDER_TYPES:
            return f"[{BLOCK_FIELD[ct]}]"
        return _inline_text(blk, c)

    _collect(_child_ids(block))
    merged = "\n".join(p for p in text_parts if p)
    out: list[str] = []
    if merged:
        if t == 34:  # quote_container → quote
            meta: dict[str, Any] = {"raw_type": "feishu.quote_container"}
            out.append(_emit(block, parent_ir_id, "quote", ctx, meta, text=merged, callout_direct=callout_direct))
        else:
            meta = {"raw_type": f"feishu.{BLOCK_FIELD[t]}", "list_kind": BLOCK_FIELD[t]}
            if t == 17:
                meta["done"] = done
            out.append(_emit(block, parent_ir_id, "list", ctx, meta, text=merged, callout_direct=callout_direct))
    out.extend(promoted)
    return out


def _map_unknown(block: Any, t: int, parent_ir_id: str, ctx: _Ctx, callout_direct: bool) -> list[str]:
    """枚举外（16、>42、0）降级（C3=(a)）：留痕不静默、正文不丢失。

    自身降 paragraph（可提取文本为空则不产节点）；children 逐个递归适用、挂自身
    父级（降级不改变子块挂载层）；degraded_blocks["block_<编号>"] 计数。
    """
    key = f"block_{t}"
    _count_degraded(ctx, key)
    out: list[str] = []
    txt = _inline_text_from(_unknown_payload(block), ctx)  # 已知载荷字段形态的防御性提取（16 等无字段 → 空）
    if txt:
        out.append(
            _emit(
                block, parent_ir_id, "paragraph", ctx, {"raw_type": f"feishu.{key}"},
                text=txt, callout_direct=callout_direct,
            )
        )
    out.extend(_map_children(_child_ids(block), parent_ir_id, ctx, callout_direct))
    return out


def _map_block(block: Any, parent_ir_id: str, ctx: _Ctx, callout_direct: bool) -> list[str]:
    t = _type_of(block)

    if t in _NO_NODE_TYPES:  # 1 page（文档根——children 即文档 children）/ 22 divider
        return _map_children(_child_ids(block), parent_ir_id, ctx, callout_direct)

    if t in _TRANSPARENT_TYPES:  # 19/24/25/33/36：透明展开（33 view 留痕计数）
        name = BLOCK_FIELD[t]
        ctx.result.transparent_counts[name] = ctx.result.transparent_counts.get(name, 0) + 1
        return _map_children(_child_ids(block), parent_ir_id, ctx, callout_direct=(t == 19))

    if t in _MERGE_TYPES:  # 12/13/17/34
        return _map_merge_container(block, t, parent_ir_id, ctx, callout_direct)

    if t == 31:
        return [_map_table(block, parent_ir_id, ctx, callout_direct)]

    if t in _SHEET_UPGRADE_TYPES:  # 30 sheet / 18 bitable：table 占位升格（worker 回填承接）
        return [_map_sheet_upgrade(block, t, parent_ir_id, ctx, callout_direct)]

    if t == 14:  # code：不降级规则——恒独立 code 节点；text 逐字拼接不 strip
        meta_code: dict[str, Any] = {"raw_type": "feishu.code"}
        style = getattr(_payload(block), "style", None)
        if style is not None:
            meta_code["lang"] = getattr(style, "language", None)  # style.language 原值
            alias = getattr(style, "language_alias", None)  # 防御读取（SDK 1.8.0 无该字段）
            if alias is not None:
                meta_code["lang_alias"] = alias
        node_id = _emit(
            block, parent_ir_id, "code", ctx, meta_code,
            text=_code_text(block), callout_direct=callout_direct,
        )
        return [node_id, *_map_children(_child_ids(block), parent_ir_id, ctx, callout_direct)]

    if t == 23:  # file：file_attachment 占位（下载后走 REQ-019 归一化管线——worker 面）
        payload = getattr(block, "file", None)
        file_token = getattr(payload, "token", None) or ""
        file_name = getattr(payload, "name", None) or ""
        node_id = _emit(
            block,
            parent_ir_id,
            "file_attachment",
            ctx,
            {
                "raw_type": "feishu.file",
                "parse_status": "pending",
                "file_token": file_token,
                "file_name": file_name,
            },
            title=file_name or None,
            callout_direct=callout_direct,
        )
        return [node_id, *_map_children(_child_ids(block), parent_ir_id, ctx, callout_direct)]

    if t == 27:  # image：先占位；素材下载入 MinIO 后 worker 回填 meta.minio_key
        payload = getattr(block, "image", None)
        image_token = getattr(payload, "token", None) or ""
        node_id = _emit(
            block,
            parent_ir_id,
            "image",
            ctx,
            {
                "raw_type": "feishu.image",
                "placeholder": True,
                "image_token": image_token,
            },
            callout_direct=callout_direct,
        )
        return [node_id, *_map_children(_child_ids(block), parent_ir_id, ctx, callout_direct)]

    if 3 <= t <= 11:  # heading1~9：level = 编号 − 2（1~9）
        text = _inline_text(block, ctx)
        if not text:
            return []
        node_id = _emit(
            block,
            parent_ir_id,
            "heading",
            ctx,
            {"raw_type": f"feishu.{BLOCK_FIELD[t]}"},
            title=text,
            level=t - 2,
            callout_direct=callout_direct,
        )
        return [node_id, *_map_children(_child_ids(block), parent_ir_id, ctx, callout_direct)]

    if t == 15:  # quote：单块引用
        text = _inline_text(block, ctx)
        if not text:
            return []
        node_id = _emit(
            block, parent_ir_id, "quote", ctx, {"raw_type": "feishu.quote"},
            text=text, callout_direct=callout_direct,
        )
        return [node_id, *_map_children(_child_ids(block), parent_ir_id, ctx, callout_direct)]

    if t in _PLACEHOLDER_TYPES:
        return [_map_placeholder(block, parent_ir_id, ctx, callout_direct)]

    if t == 2:  # text：paragraph（行内元素按通用规则）
        text = _inline_text(block, ctx)
        out: list[str] = []
        if text:
            out.append(
                _emit(block, parent_ir_id, "paragraph", ctx, {"raw_type": "feishu.text"},
                      text=text, callout_direct=callout_direct)
            )
        out.extend(_map_children(_child_ids(block), parent_ir_id, ctx, callout_direct))
        return out

    return _map_unknown(block, t, parent_ir_id, ctx, callout_direct)  # 16 / >42 / 0


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def map_blocks(
    blocks: list[Any],
    *,
    space_id: str,
    node_token: str,
    known_node_tokens: frozenset[str] | set[str] = frozenset(),
    node_token_by_obj_token: dict[str, str] | None = None,
) -> FeishuMappingResult:
    """docx v1 blocks 平铺 → IR refs（喂 `attach_block_tree`）+ 留痕。

    - 显示序权威 = page(1) 块 children 数组（§1.18 映射表 1 行「children 即文档
      children」；缺 page 块的防御形态 = 输入序中 parent 不在块集者视作顶层）；
    - 引用缺失（children 指向未取回块）跳过不崩（client 分页翻全后正常不触达）。
    """
    block_map: dict[str, Any] = {}
    order: list[Any] = []
    for b in blocks:
        bid = getattr(b, "block_id", None)
        if not bid or bid in block_map:
            continue
        block_map[bid] = b
        order.append(b)

    result = FeishuMappingResult()
    ctx = _Ctx(
        space_id=space_id,
        node_token=node_token,
        doc_id=f"fs:node:{node_token}",
        block_map=block_map,
        known_node_tokens=frozenset(known_node_tokens),
        node_token_by_obj_token=node_token_by_obj_token or {},
        result=result,
    )

    page = next((b for b in order if _type_of(b) == 1), None)
    if page is not None:
        root_ids = _child_ids(page)
    else:
        root_ids = [
            getattr(b, "block_id", "")
            for b in order
            if getattr(b, "parent_id", "") not in block_map
        ]

    top_ids: list[str] = []
    for cid in root_ids:
        blk = block_map.get(cid)
        if blk is None or getattr(blk, "block_id", "") in ctx.emitted:
            continue  # 引用缺失跳过；已被上层收编的块不重复产节点（畸形双父引用防御）
        if cid in ctx.visiting:
            continue  # 防环（评审 P3）：根层同款守卫
        ctx.visiting.add(cid)
        try:
            top_ids.extend(_map_block(blk, ctx.doc_id, ctx, callout_direct=False))
        finally:
            ctx.visiting.discard(cid)
    result.root_order = top_ids
    return result


# ---------------------------------------------------------------------------
# 文档节点与 parse_meta.feishu（data-model §1.7 存储侧附加键 / §1.18 通用规则）
# ---------------------------------------------------------------------------


def build_document_node(
    *,
    node_token: str,
    title: str,
    obj_type: str,
    obj_edit_time: int | None,
    space_id: str,
) -> IrNode:
    """wiki 节点 → document 节点：id=fs:node:{node_token}；meta.raw_type=obj_type 原值、
    meta.obj_edit_time 契约键（§1.18 通用规则）；obj_type 须 ∈ DOC_KINDS（mindnote/slides
    等 unsupported 类型在 worker 层走 skip 留痕，不到本文档节点）。"""
    if obj_type not in DOC_KINDS:
        raise ValueError(
            f"obj_type 须 ∈ {DOC_KINDS}（unsupported 类型走 worker 层 skip 留痕）：{obj_type!r}"
        )
    return IrNode(
        id=f"fs:node:{node_token}",
        type="document",  # type: ignore[arg-type]  # pydantic 校验接受 str 字面（IrNodeType.DOCUMENT）
        doc_kind=obj_type,  # type: ignore[arg-type]
        title=title or None,
        meta={"raw_type": obj_type, "obj_edit_time": obj_edit_time},
        loc=FeishuLoc(
            origin="feishu",
            space_id=space_id,
            node_token=node_token,
            obj_edit_time=obj_edit_time,
        ),
    )


def build_feishu_parse_meta(
    *,
    space_id: str,
    node_token: str,
    obj_type: str,
    obj_edit_time: int | None,
    degraded_blocks: dict[str, int],
) -> dict[str, Any]:
    """parse_meta.feishu 子对象（§1.7 存储侧附加键，不进 REST 投影）。

    四键必带（同步 diff 的节点寻址与变更比对依据——api.md §4.7 判定序①③④的本地
    比对基准）；degraded_blocks 可选（无降级块时省略键）；非飞书来源省略整个 feishu
    键（不产此对象即可）。`parser` 由 worker 以 PARSER_FEISHU_BLOCKS 置顶层键。
    """
    meta: dict[str, Any] = {
        "space_id": space_id,
        "node_token": node_token,
        "obj_type": obj_type,
        "obj_edit_time": obj_edit_time,
    }
    if degraded_blocks:
        meta["degraded_blocks"] = dict(degraded_blocks)
    return meta
