"""飞书两层树装配器（M3 批次 1 / B16）——纯函数，零网络零 DB。

契约锚点：ARCHITECTURE ADR-018 决策⑤ / 调研
feishu-kb-import-research-20261122.md §11（树装配层设计要点）与 §12（探针实测
931 节点量级）；目录树注 ARCHITECTURE §3.1 `rag/` 行。

两层装配（调研 §11.1）：目录层 wiki node 平铺 → 完全节点树（`assemble_catalog_tree`，
parent 空=根，BFS 建完全树，孤儿挂虚拟根留痕）；内容层 docx block 平铺 → 块树挂
document 节点（`attach_block_tree`，children 数组序为显示序权威）；两层以 parent_id
边关联（根块 parent=所属文档单元，深层块 parent=所属内容块——调研 §13.3）。

输入约定：flat wiki node / docx block 已由 worker（批次 5）映射为 IrNode——双层类型
由 ir_schema 兜底（未知 type → paragraph + meta.raw_type 留痕）；shortcut 经
`meta["node_type"]="shortcut"` 透传（feishu wiki node_type 原始值）。

防御四件（调研 §11.3，逐条对应用例）：
1. **防环**：visited 集合按 id——环边在输入序首个不可达节点处切断，节点及其可达子树
   留树（内容零丢失），`report.cycle_ids` 留痕；
2. **孤儿兜底**：parent 引用缺失的节点挂虚拟根（目录层）/文档根（内容层），
   `report.orphan_ids` 留痕不崩；
3. **截断安全阀**：模块常量 `MAX_NODES`（931 实测量级 ×10 余量；调研 §12——生产
   不设探针式硬上限，上限仅作安全阀），超限按输入序截断，`report.truncated`/
   `truncated_dropped` 留痕（被截父节点的子节点走孤儿兜底）；
4. **shortcut 不展开**：`node_type=shortcut` 节点计为引用边保留本体、整支子树切断
   （传递收集），`report.shortcut_ids`/`shortcut_cut_ids` 留痕。

装配即双写（调研 §13.7/§13.8#2）：parent_id 与 children 同一次遍历双写（构造即一致）；
prev/next 兄弟指针装配期末由 `ir_schema.rebuild_siblings` 重建（内存形态，序列化仍省略）。
"""

from collections import deque
from dataclasses import dataclass, field
from typing import Sequence

from app.rag.ir_schema import (
    VIRTUAL_ROOT_ID,
    IrNode,
    IrNodeType,
    rebuild_siblings,
)

#: 截断安全阀（防御③）——调研 §12 实测 931+ 文档单元 ×10 余量；monkeypatch 供测试
MAX_NODES = 10_000

#: shortcut 透传键/值（feishu wiki node_type 原始值——worker 映射面写入 meta）
SHORTCUT_META_KEY = "node_type"
SHORTCUT_META_VALUE = "shortcut"


@dataclass(slots=True)
class AssembleReport:
    """装配留痕（防御四件 + 去重/计数；进程内诊断用，不入持久化面）。"""

    total_input: int = 0
    attached: int = 0
    root_ids: list[str] = field(default_factory=list)
    orphan_ids: list[str] = field(default_factory=list)
    cycle_ids: list[str] = field(default_factory=list)
    shortcut_ids: list[str] = field(default_factory=list)
    shortcut_cut_ids: list[str] = field(default_factory=list)
    duplicate_ids: list[str] = field(default_factory=list)
    truncated: bool = False
    truncated_dropped: int = 0


@dataclass(slots=True)
class BlockRef:
    """docx block 平铺项：节点 + 原始 children block_id 数组（显示序权威，调研 §11）。"""

    node: IrNode
    children_ids: list[str] = field(default_factory=list)


def _is_shortcut(node: IrNode) -> bool:
    return node.meta.get(SHORTCUT_META_KEY) == SHORTCUT_META_VALUE


def _is_top_parent(parent_id: str | None) -> bool:
    return parent_id is None or parent_id == "" or parent_id == VIRTUAL_ROOT_ID


def _dedupe_and_truncate(
    items: Sequence[IrNode], report: AssembleReport
) -> tuple[list[IrNode], set[str]]:
    """按 id 去重（首个保留）+ MAX_NODES 截断安全阀；返回 (kept, id 集合)。"""
    kept: list[IrNode] = []
    seen: set[str] = set()
    for node in items:
        if node.id in seen:
            report.duplicate_ids.append(node.id)
            continue
        seen.add(node.id)
        kept.append(node)
    if len(kept) > MAX_NODES:
        report.truncated = True
        report.truncated_dropped = len(kept) - MAX_NODES
        kept = kept[:MAX_NODES]
    return kept, {n.id for n in kept}


def assemble_catalog_tree(flat_nodes: Sequence[IrNode]) -> tuple[IrNode, AssembleReport]:
    """目录层装配：wiki node 平铺 → Forest 完全节点树（虚拟单根）。

    - parent 空（None/空串）或已指向虚拟根哨兵 = 根；
    - BFS 建完全树；孤儿挂虚拟根留痕；环边切断留痕；shortcut 子树整支切断留痕；
    - 装配期末物化 `catalog_ids`/`catalog_path`（祖先 ID 链 / 祖先标题+自身标题链，
      document 节点专属，§13.7「构造即一致」——入参既有值按树重算覆写；catalog_path
      含自身标题〔§1.18 C9=(a)，顶层=[自身标题]、不存在空链〕）；
    - 返回 `(虚拟根 IrNode, AssembleReport)`；虚拟根 children = 真根 + 孤儿 + 环切根
      （全部 parent_id 重接 VIRTUAL_ROOT_ID，输入序）。
    """
    report = AssembleReport(total_input=len(flat_nodes))
    kept, id_set = _dedupe_and_truncate(flat_nodes, report)

    roots: list[IrNode] = []
    orphans: list[IrNode] = []
    children_map: dict[str, list[IrNode]] = {}
    for node in kept:
        if _is_top_parent(node.parent_id):
            roots.append(node)
        elif node.parent_id in id_set:
            children_map.setdefault(node.parent_id or "", []).append(node)
        else:
            # 防御②：孤儿兜底——parent 引用缺失，即刻挂虚拟根（留痕不崩）
            node.parent_id = VIRTUAL_ROOT_ID
            orphans.append(node)
            report.orphan_ids.append(node.id)
    report.root_ids = [n.id for n in roots]

    visited: set[str] = set()
    cut_ids: set[str] = set()

    def _traverse(seeds: Sequence[IrNode]) -> None:
        queue: deque[IrNode] = deque(seeds)
        while queue:
            node = queue.popleft()
            if node.id in visited:
                continue
            visited.add(node.id)
            if _is_shortcut(node):
                # 防御④：计为引用边——本体保留、整支子树切断（传递收集，标记已处理）
                report.shortcut_ids.append(node.id)
                sub_queue: deque[IrNode] = deque(children_map.get(node.id, []))
                while sub_queue:
                    member = sub_queue.popleft()
                    cut_ids.add(member.id)
                    visited.add(member.id)
                    sub_queue.extend(children_map.get(member.id, []))
                    report.shortcut_cut_ids.append(member.id)
                continue
            queue.extend(children_map.get(node.id, []))

    _traverse(roots)
    _traverse(orphans)

    # 防御①：visited 防环——仍未达的 linked 节点必在环上；输入序首个切边成根再遍历
    while True:
        pending = [n for n in kept if n.id not in visited and n.parent_id in id_set]
        if not pending:
            break
        entry = pending[0]
        entry.parent_id = None
        report.cycle_ids.append(entry.id)
        _traverse([entry])

    # children 接线（树内节点）：visited + 非 shortcut 本体 + 非被切子树；
    # 按**当前 parent_id** 过滤——环切边切断后，children_map 中指向切边目标的陈旧边剔除
    for node in kept:
        if node.id in visited and node.id not in cut_ids and not _is_shortcut(node):
            node.children = [
                c for c in children_map.get(node.id, []) if c.parent_id == node.id
            ]

    top_nodes = [n for n in kept if n.id in visited and _is_top_parent(n.parent_id)]
    virtual_root = IrNode(
        id=VIRTUAL_ROOT_ID,
        type=IrNodeType.DOCUMENT,
        title=None,
        meta={"raw_type": "virtual_root", "virtual": True},
    )
    for node in top_nodes:
        node.parent_id = VIRTUAL_ROOT_ID
    virtual_root.children = top_nodes

    # catalog_ids / catalog_path 物化（BFS 沿树；document 节点专属；visited 保险防病态输入）
    # §1.18「catalog_path 含自身标题」（C9=(a)，B20 契约批）：catalog_path=[祖先标题…,
    # 自身标题]，顶层（含孤儿兜底/环切成根）=[自身标题]，不存在空链；catalog_ids 口径
    # 不变（祖先 ID 链，顶层 []）——恒有 len(catalog_path)=len(catalog_ids)+1。链元素
    # 空串占位（无标题文档）保恒等式；chunker 末位同名吸收（§1.18 物化细则），切片
    # 轨迹语义零变化；上传/URL 来源不走本装配器（catalog 列恒 null 零变化）。
    queue: deque[tuple[IrNode, IrNode | None]] = deque((child, None) for child in top_nodes)
    materialized: set[str] = set()
    while queue:
        node, parent = queue.popleft()
        if node.id in materialized:
            continue
        materialized.add(node.id)
        if node.type is IrNodeType.DOCUMENT:
            if parent is None:
                node.catalog_ids = []
                node.catalog_path = [node.title or ""]
            else:
                node.catalog_ids = [*parent.catalog_ids, parent.id]
                node.catalog_path = [*parent.catalog_path, node.title or ""]
        for child in node.children:
            queue.append((child, node))

    rebuild_siblings(virtual_root)
    report.attached = len(kept) - len(report.shortcut_cut_ids)
    return virtual_root, report


def attach_block_tree(
    doc_node: IrNode,
    blocks: Sequence[BlockRef],
    *,
    root_order: Sequence[str] | None = None,
) -> tuple[IrNode, AssembleReport]:
    """内容层装配：docx block 平铺 → 块树，挂 document 节点。

    - 根块：parent_id == doc_node.id；显示序权威 = 页块 children 数组（`root_order`
      透传；缺省按输入序）；未列入数组者按输入序补尾（不丢内容）；
    - 深层块：parent_id = 所属块 id；显示序权威 = 该块 `BlockRef.children_ids`；
    - 孤儿（parent 引用缺失/空）兜底挂文档根留痕；环边切断挂文档根留痕；
    - shortcut 文档守卫（防御④内容层）：doc_node 为 shortcut → 拒绝挂内容（不展开实体）；
    - 装配期末 parent_id 双写一致 + `rebuild_siblings` 重建兄弟指针；
    - 块节点的 catalog_ids/catalog_path（document 专属目录语义）零触碰。
    """
    report = AssembleReport(total_input=len(blocks))

    # 防御④（内容层）：shortcut 文档计为引用边，不展开实体
    if doc_node.meta.get(SHORTCUT_META_KEY) == SHORTCUT_META_VALUE:
        report.shortcut_ids.append(doc_node.id)
        rebuild_siblings(doc_node)
        return doc_node, report

    kept_refs, id_set = _dedupe_and_truncate([b.node for b in blocks], report)
    order_map: dict[str, list[str]] = {}
    for ref in blocks:
        order_map.setdefault(ref.node.id, ref.children_ids)  # 同 id 首个保留（与去重一致）

    doc_id = doc_node.id

    root_blocks: list[IrNode] = []
    orphans: list[IrNode] = []
    children_map: dict[str, list[IrNode]] = {}
    for node in kept_refs:
        if node.parent_id == doc_id:
            root_blocks.append(node)
        elif node.parent_id in id_set:
            children_map.setdefault(node.parent_id or "", []).append(node)
        else:
            # 防御②：孤儿兜底（含 parent 空/缺失）——挂文档根（留痕不崩）
            node.parent_id = doc_id
            orphans.append(node)
            report.orphan_ids.append(node.id)

    visited: set[str] = set()

    def _traverse(seeds: Sequence[IrNode]) -> None:
        queue: deque[IrNode] = deque(seeds)
        while queue:
            node = queue.popleft()
            if node.id in visited:
                continue
            visited.add(node.id)
            queue.extend(children_map.get(node.id, []))

    _traverse(root_blocks)
    _traverse(orphans)
    while True:
        pending = [n for n in kept_refs if n.id not in visited and n.parent_id in id_set]
        if not pending:
            break
        entry = pending[0]
        entry.parent_id = doc_id
        report.cycle_ids.append(entry.id)
        _traverse([entry])

    def _ordered(children: Sequence[IrNode], preferred: Sequence[str] | None) -> list[IrNode]:
        if not preferred:
            return list(children)
        child_map = {c.id: c for c in children}
        listed: list[IrNode] = []
        listed_ids: set[str] = set()
        for i in preferred:  # 数组序权威；重复 id 只取一次（防御畸形数组），未列入者按输入序补尾
            if i in child_map and i not in listed_ids:
                listed.append(child_map[i])
                listed_ids.add(i)
        return [*listed, *(c for c in children if c.id not in listed_ids)]

    doc_children = _ordered([*(n for n in kept_refs if n.id in visited and n.parent_id == doc_id)], root_order)
    for node in doc_children:
        node.parent_id = doc_id
    doc_node.children = [*doc_node.children, *doc_children]
    report.root_ids = [n.id for n in doc_children]

    queue: deque[IrNode] = deque(doc_children)
    wired: set[str] = set()
    while queue:
        parent = queue.popleft()
        if parent.id in wired:
            continue
        wired.add(parent.id)
        # linked children 全量在 children_map；按当前 parent_id 过滤（剔除环切后的陈旧边）
        kids = [c for c in children_map.get(parent.id, []) if c.parent_id == parent.id]
        parent.children = _ordered(kids, order_map.get(parent.id))
        for child in parent.children:
            child.parent_id = parent.id  # 构造即一致（双写）
            queue.append(child)

    rebuild_siblings(doc_node)
    report.attached = len(visited)
    return doc_node, report
