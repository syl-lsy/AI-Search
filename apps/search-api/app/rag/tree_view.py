"""完全树视图投影（heading 层级物化 + 文档/森林视图）——纯函数，零 IO 零全局态。

对应契约/事实源：data-model.md §1.18（统一完全树 IR，catalog_ids/catalog_path 物化
C9 口径）/ ARCHITECTURE ADR-018；目录链语义与 `tree_assembler.assemble_catalog_tree`
一致（catalog_ids=祖先 ID 链、catalog_path=[祖先标题…, 自身标题]）。

问题背景：统一树 IR 有两种内容拓扑——
1. **飞书 SDK 通道**（feishu_mapper → attach_block_tree）：内容块**平铺**在 document
   节点下（飞书块模型事实：heading 与正文同为 page 直接子块），heading 只带 `level`；
2. **本地通道**（markitdown/docling 归一化）：内容块**已嵌套**在 heading 子树。

「完全树视图」= heading 层级物化嵌套后的阅读/展示投影：把平铺形态按 level 层栈重排
成嵌套形态；对已嵌套拓扑幂等（只做字段投影，绝不二次挪动）。消费面为 Library 树
展示 / 导出 / 完全树 JSON 等（REST 端点属后续契约批，本模块只出纯投影）。

通道判定按 **IR id 前缀**（data-model §1.18 四前缀定案，替代早期拓扑启发式——启发式
在「全空标题平铺 `[h1, h2, 尾块]`」形态误把尾块吸进标题子树）：`fs:node:`/`fs:blk:`
= 飞书平铺通道 → 层栈物化；`loc:doc:`/`loc:blk:` = 本地/url 已嵌套通道 → 只投影不
重排。以（文档）根节点前缀为准（单文档树单一通道）。

视图卫生（2026-11-25 用户口径，含同日修订）：
- 保留 `id/type/parent_id/title/level/text/meta/loc/children`；**`parent_id` 回填视图
  父节点 id**（视图自洽可导航：嵌套块 = 所嵌 heading 的 id、标题前的根层块 = document
  根 id、document 根自身 = None 树根）——与 IR 本体的 parent_id（生产事实=指向文档
  根）语义不同、各自成立；可选标量为 None 时省略键（项目既有「可选键缺则省略、不置
  null」先例；parent_id 为视图树根时按树根语义置 None 并保留键）；
- 内容块剥除 `catalog_ids/catalog_path/doc_kind`：catalog 信息不进内容块视图；
- `document` 节点自身保留 `catalog_ids`/`catalog_path`（文档根携带目录信息合理）；
  其余节点（含 heading）恒不出 catalog 键——树装配器只对 DOCUMENT 物化 catalog
  （`tree_assembler.assemble_catalog_tree`），heading 面 catalog 值不属契约语义；
- `meta` 原样透传（浅拷贝，改视图不污染 IR；含 raw_type/placeholder/rows 等扩展键）；
- PUA document-ref 占位符文本原样透传，不解释不展开。

三个出口：
- `build_hierarchy_view`：任意 IrNode 子树 → 层级物化视图节点（dict）；
- `build_document_tree_view`：文档级完全树（根保留 catalog 面与 doc_kind）；
- `build_forest_view`：多文档沿 catalog 链挂目录树（空间/目录级完全树）。
"""

from typing import Any, Sequence

from app.rag.ir_schema import IrNode, IrNodeType

#: 需要做 heading 层栈物化的容器类型（其余容器只做递归字段投影）
_HIERARCHY_TYPES: frozenset[IrNodeType] = frozenset(
    {IrNodeType.DOCUMENT, IrNodeType.HEADING}
)

#: 已嵌套通道前缀（本地/url 归一化，data-model §1.18 四前缀定案）；其余合法前缀
#: （fs:node:/fs:blk:）= 飞书平铺通道。单文档树单一通道，判定以（文档）根节点为准。
_NESTED_CHANNEL_PREFIXES: tuple[str, ...] = ("loc:doc:", "loc:blk:")


def _is_heading(node: IrNode) -> bool:
    """是否 heading——只判 type；`level` 缺省按 1 处理（见 `_materialize_blocks`，
    对齐 `chunker.heading_enter` 的既有口径 `node.level if node.level is not None
    else 1`，不因缺 level 退化为普通块）。"""
    return node.type is IrNodeType.HEADING


def _is_nested_channel(node: IrNode) -> bool:
    """按 id 前缀判定内容通道：loc:doc:/loc:blk: = 已嵌套通道（只投影不重排）；
    fs:node:/fs:blk: = 飞书平铺通道（层栈物化）。"""
    return node.id.startswith(_NESTED_CHANNEL_PREFIXES)


def _project_node(
    node: IrNode,
    children: list[dict[str, Any]],
    *,
    parent_id: str | None,
) -> dict[str, Any]:
    """单节点字段投影 + 已投影 children（视图卫生规则唯一出口）。

    `parent_id` 为**视图父节点 id**（非 IR 本体值；None 仅出现于视图树根），由调用方
    按嵌套位置传入，保证 `child.parent_id == 父节点.id` 的视图自洽性。
    """
    view: dict[str, Any] = {
        "id": node.id,
        "parent_id": parent_id,
        "type": node.type.value,
        "meta": dict(node.meta),  # 浅拷贝：视图可改，IR 不受污染
    }
    if node.title is not None:
        view["title"] = node.title
    if node.level is not None:
        view["level"] = node.level
    if node.text is not None:
        view["text"] = node.text
    if node.loc is not None:
        view["loc"] = node.loc.model_dump(mode="json")
    if node.type is IrNodeType.DOCUMENT:
        # 仅 document 节点物化 catalog 面（对齐 tree_assembler 只对 DOCUMENT 物化；
        # heading 等其他节点恒不出 catalog 键）
        view["catalog_ids"] = list(node.catalog_ids)
        view["catalog_path"] = list(node.catalog_path)
    view["children"] = children
    return view


def _project_plain(node: IrNode, parent_id: str | None) -> dict[str, Any]:
    """非层级容器（table/list/…）的递归投影：嵌套原样保留，不重排。"""
    return _project_node(
        node,
        [_project_plain(child, node.id) for child in node.children],
        parent_id=parent_id,
    )


def _materialize_blocks(
    children: Sequence[IrNode], parent_id: str, *, nested_channel: bool
) -> list[dict[str, Any]]:
    """heading 层栈物化：平铺块序 → 嵌套视图层。

    通道按 id 前缀判定（`_is_nested_channel`，由调用方在（文档）根层算好并逐层
    下传——单文档树单一通道）：已嵌套通道（loc: 前缀）只做字段投影、绝不二次挪动；
    平铺通道（fs: 前缀）按层栈重排。残余说明：若病态输入在同树混用前缀，按根节点
    通道统一处理，不做逐节点通道切换。

    平铺通道遍历孩子序：heading 入栈成嵌套节点（弹栈至更浅的父标题；`level` 缺省
    按 1），非 heading 块挂栈顶最近 heading 的 children、栈空挂当前层根；每个节点
    恰好访问一次（投影前后节点计数守恒）。`parent_id` 为当前层根的视图父 id
    （document/heading 节点 id）；各视图节点 parent_id 回填其嵌套位置的上一级 id。
    """
    if nested_channel:
        return [_project_plain(child, parent_id) for child in children]

    roots: list[dict[str, Any]] = []
    stack: list[tuple[int, dict[str, Any]]] = []  # (level, view)——栈内恒为祖先标题链
    for child in children:
        if _is_heading(child):
            level = child.level if child.level is not None else 1
            while stack and stack[-1][0] >= level:
                stack.pop()
            owner_id = stack[-1][1]["id"] if stack else parent_id
            view = _project_node(
                child,
                _materialize_blocks(
                    child.children, child.id, nested_channel=nested_channel
                ),
                parent_id=owner_id,
            )
            if stack:
                stack[-1][1]["children"].append(view)
            else:
                roots.append(view)
            stack.append((level, view))
        else:
            # 非 heading 容器的深层嵌套（table 格内子块 / list 子块）照旧递归投影
            owner_id = stack[-1][1]["id"] if stack else parent_id
            view = _project_plain(child, owner_id)
            if stack:
                stack[-1][1]["children"].append(view)
            else:
                roots.append(view)
    return roots


def build_hierarchy_view(node: IrNode) -> dict[str, Any]:
    """任意 IrNode 子树 → 层级物化视图节点（dict）。

    `document`/`heading` 节点的孩子集合走 heading 层栈物化（平铺→嵌套）；其余类型
    子树只做递归字段投影（深层非 heading 嵌套原样保留）。通道按传入节点 id 前缀
    判定（`_is_nested_channel`——单文档树单一通道）。字段面见模块 docstring 视图
    卫生规则：可选标量 None 省略键，恒在 `id/parent_id/type/meta/children`（传入
    节点即视图树根，parent_id=None）。
    """
    if node.type in _HIERARCHY_TYPES:
        children = _materialize_blocks(
            node.children, node.id, nested_channel=_is_nested_channel(node)
        )
    else:
        children = [_project_plain(child, node.id) for child in node.children]
    return _project_node(node, children, parent_id=None)


def build_document_tree_view(document: IrNode) -> dict[str, Any]:
    """文档级完全树视图：根保留 catalog 面与 doc_kind，children=层级物化结果。

    根层键固定七项 `id/type/title/doc_kind/catalog_ids/catalog_path/children`
    （顶层消费面，键恒在、值可为 null——与内容块视图的「可选键省略」口径不同；
    document 根是树根，无 parent_id 键，也**不带 loc**）。两种根形态差异：本函数是
    单文档七键根；`build_forest_view` 的文档节点在七键文档视图之外**另有 `loc` 键**
    （溯源下钻以森林为入口时的 node_token 回跳投影，见该函数）。children 各节点
    parent_id 回填视图父 id（根层块 = document.id，嵌套块 = 所嵌 heading id）。
    """
    return {
        "id": document.id,
        "type": document.type.value,
        "title": document.title,
        "doc_kind": document.doc_kind,
        "catalog_ids": list(document.catalog_ids),
        "catalog_path": list(document.catalog_path),
        "children": _materialize_blocks(
            document.children, document.id, nested_channel=_is_nested_channel(document)
        ),
    }


def _catalog_node(
    node_id: str,
    node_type: str,
    title: str,
    document: dict[str, Any] | None,
    loc: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """森林目录节点构造（目录前缀与文档叶共用形态，键恒在）。

    `loc` 仅文档节点有值（document IR 的 loc JSON dict——溯源下钻以森林为入口时
    拿 node_token 回跳投影）；合成目录层为 None。
    """
    return {
        "id": node_id,
        "type": node_type,  # "catalog"（合成目录）| "document"（文档自身）
        "title": title,
        "document": document,
        "loc": loc,
        "children": [],
    }


def _document_loc(document: IrNode) -> dict[str, Any] | None:
    """森林层 loc 投影（JSON 模式 dict；无 loc 则 None——森林节点键恒在口径）。"""
    if document.loc is None:
        return None
    return document.loc.model_dump(mode="json")


def _ancestor_title(document: IrNode, depth: int) -> str:
    """目录链第 depth 层祖先标题（C9：catalog_path=[祖先标题…, 自身标题]）。

    畸形输入（catalog_path 缺失/长度不匹配）时回落空串——不伪造标题。
    """
    if depth < len(document.catalog_path) - 1:
        return document.catalog_path[depth]
    return ""


def _own_title(document: IrNode) -> str:
    """文档自身标题：catalog_path 末位（C9）；空链/畸形链回落 document.title。"""
    ids = document.catalog_ids
    path = document.catalog_path
    if len(path) == len(ids) + 1:
        return path[-1]
    return document.title or ""


def build_forest_view(documents: Sequence[IrNode]) -> dict[str, Any]:
    """多文档完全树（空间/目录级）：沿 catalog_ids/catalog_path 挂目录树。

    输出 `{"roots": [...], "document_count": N}`；`document_count` 按实际入树文档数
    计（重复文档归并后只算一次，不是入参长度）。目录节点形态（键恒在）：
    `{"id", "type", "title", "document", "loc", "children"}`——
    - `type="catalog"`：合成目录层（仅作为其他文档的祖先，自身不在入参中），
      `document`/`loc` 恒为 None；
    - `type="document"`：该节点自身是文档，`document` = `build_document_tree_view`
      产物（七键根、不含 loc），`loc` = document IR 的 loc JSON dict（溯源下钻以
      森林为入口时的 node_token 回跳投影；无 loc 为 None）；同一文档既是父又是子
      （wiki 目录文档）时同节点承载文档视图 + 子节点，不重复挂叶。
    同链同父（祖先链按 ID 链身份归并，首见标题胜出）；链尾=文档自身标题（C9
    catalog_path 含自身标题——祖先层数=len(catalog_ids)，不重复挂一层）；空链文档
    挂 roots 顶层（无空目录层）。兄弟序=入参序。
    """
    roots: list[dict[str, Any]] = []
    index: dict[tuple[str, ...], dict[str, Any]] = {}

    for document in documents:
        ids = list(document.catalog_ids)
        parent_children = roots
        for depth, ancestor_id in enumerate(ids):
            key = tuple(ids[: depth + 1])
            node = index.get(key)
            if node is None:
                node = _catalog_node(
                    ancestor_id,
                    "catalog",
                    _ancestor_title(document, depth),
                    None,
                )
                index[key] = node
                parent_children.append(node)
            parent_children = node["children"]

        leaf_key = (*ids, document.id)
        node = index.get(leaf_key)
        if node is None:
            node = _catalog_node(
                document.id,
                "document",
                _own_title(document),
                build_document_tree_view(document),
                _document_loc(document),
            )
            index[leaf_key] = node
            parent_children.append(node)
        else:
            # 该文档此前已作为其他文档的祖先目录层出现：升级为文档节点（补文档视图；
            # 标题以文档自身为准——目录层首见标题可能来自其他文档的 catalog_path）
            node["type"] = "document"
            node["title"] = _own_title(document)
            node["document"] = build_document_tree_view(document)
            node["loc"] = _document_loc(document)

    return {
        "roots": roots,
        "document_count": len({document.id for document in documents}),
    }
