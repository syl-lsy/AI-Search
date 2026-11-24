"""统一树 IR pydantic 模型与 ir.json 序列化协议（M3 批次 1 / B16）。

契约锚点：data-model.md §1.18（v1.19，:539-547）/ 调研
feishu-kb-import-research-20261122.md §13.3~13.9（OQ-26 定稿）/ ARCHITECTURE
ADR-018 决策①②。落档后本 schema 为实现事实源（§1.18 事实源分层句），本节为契约锚点。

★装载钉注（data-model.md §1.18 :544 要求，防字面直读落空）：
`serialize_ir` / `deserialize_ir` 的产物**无 prev/next 兄弟指针**——兄弟关系仅内存
形态，序列化省略防循环引用（调研 §13.8#2）。交互式下钻（溯源 UI 展开上下文，A36）
按需拉**单文档** ir.json（MinIO GET 毫秒级）内存走 parent_id 链，**装载必须调
`rebuild_siblings(root)` 由 children 数组序重建 prev/next**（`storage.get_ir_json`
已内置此步，直接消费；绕过 storage 的裸 `deserialize_ir` 调用方须自行重建）。

形态（OQ-26 定稿 §13.9 七条）：Forest——虚拟单根 + 文档森林 + catalog_path；全节点
parent_id/catalog_ids；双层 type（统一枚举 + `meta.raw_type` 全量留痕）；未知类型
降级 paragraph；ID 前缀体系 `fs:node:`/`fs:blk:`/`loc:doc:`/`loc:blk:` **四前缀**
（2026-11-23 用户裁决四前缀定案·契约胜出——URL 文档/块亦用 loc:doc:/loc:blk:，
来源语义由 loc.origin/doc_kind 承载；ARCH ADR-018①/调研 §13.9④ 五前缀旧表述
钉注归文档巡检）。
"""

import re
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class IrNodeType(str, Enum):
    """统一 type 枚举（调研 §13.4 三源映射表的归并层）。"""

    DOCUMENT = "document"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    QUOTE = "quote"
    CODE = "code"
    TABLE = "table"
    IMAGE = "image"
    FILE_ATTACHMENT = "file_attachment"


NODE_TYPE_VALUES: frozenset[str] = frozenset(t.value for t in IrNodeType)

#: 未知类型兜底（§13.9③）：降级 paragraph，原始类型进 `meta.raw_type` 全量留痕（§13.8#4）
UNKNOWN_TYPE_FALLBACK = IrNodeType.PARAGRAPH

#: ID 前缀体系（data-model.md §1.18 :541——**2026-11-23 用户裁决四前缀定案·契约胜出**：
#: 冲突优先级 contract > ARCHITECTURE；URL 文档/块亦用 loc:doc:/loc:blk:，来源语义由
#: loc.origin 三源 union 与 document 节点 doc_kind 承载、ID 不重复编码来源。
#: ARCHITECTURE ADR-018①/调研 §13.9④ 的五前缀旧表述钉注归文档巡检，非代码面）
ID_PREFIXES: tuple[str, ...] = ("fs:node:", "fs:blk:", "loc:doc:", "loc:blk:")

#: Forest 虚拟单根保留哨兵（§1.18「虚拟单根」形态）——前缀体系中唯一的装配器内部
#: 保留值（非三源真实 id，永不作真实节点 id/占位符锚点使用；ir_schema 与
#: tree_assembler 共认，测试锁死）
VIRTUAL_ROOT_ID = "virtual:root"

#: doc_kind 六值（document 节点专属——调研 §13.3）
DOC_KINDS: tuple[str, ...] = ("docx", "file", "sheet", "bitable", "upload", "url")
DocKind = Literal["docx", "file", "sheet", "bitable", "upload", "url"]


class FeishuLoc(BaseModel):
    """飞书源定位（§13.5）：node_token/block_id 天然跨导入稳定（增量 delta 寻址）。"""

    origin: Literal["feishu"]
    space_id: str
    node_token: str
    block_id: str | None = None
    edit_time: int | None = None


class LocalLoc(BaseModel):
    """本地上传源定位（§13.5）：重导=整文档单元替换，溯源靠 page/char_range。"""

    origin: Literal["local"]
    page: int | None = None
    char_range: tuple[int, int] | None = None
    file_object_key: str | None = None  # kb/{user_id}/{doc_id}/original.{ext}（§1.7 :270）


class UrlLoc(BaseModel):
    """URL 抓取源定位（§13.5）。"""

    origin: Literal["url"]
    source_url: str
    char_range: tuple[int, int] | None = None


IrLoc = Annotated[FeishuLoc | LocalLoc | UrlLoc, Field(discriminator="origin")]


class IrNode(BaseModel):
    """统一树 IR 节点（调研 §13.3 节点 schema 的 pydantic 落地）。

    内存形态含 prev/next 兄弟指针（serialize 时自动省略——`exclude=True`）；
    `meta` 为类型特有载荷区：`raw_type`（**全量必带**——原始类型编号/obj_type）、
    `parse_status`（file 附件未解析占位——§13.9⑥）、`obj_edit_time`（变更判据）、
    `minio_key` / `lang` / `rows` / `col_count` 等（§13.3 示例形态）。

    `validate_assignment=True`：构造后的字段赋值同样过校验（loc dict → 三源模型强制
    转换、id 前缀面不可被坏赋值破坏）——契约模型防构造后漂移。
    """

    model_config = ConfigDict(validate_assignment=True)

    id: str
    parent_id: str | None = None
    type: IrNodeType
    title: str | None = None
    doc_kind: DocKind | None = None  # document 节点专属（§13.3）
    catalog_ids: list[str] = Field(default_factory=list)  # 祖先文档 ID 链物化（§13.7）
    catalog_path: list[str] = Field(default_factory=list)  # 展示用标题链（非寻址真相）
    children: list["IrNode"] = Field(default_factory=list)
    loc: IrLoc | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    level: int | None = Field(default=None, ge=1, le=9)  # heading 专属 1~9（§13.4）
    text: str | None = None  # 文本载荷节点
    # 内存兄弟指针（§13.8#2）：不进序列化（exclude）——装载由 rebuild_siblings 重建
    prev: "IrNode | None" = Field(default=None, exclude=True, repr=False)
    next: "IrNode | None" = Field(default=None, exclude=True, repr=False)

    @field_validator("id")
    @classmethod
    def _validate_id_prefix(cls, value: str) -> str:
        if value == VIRTUAL_ROOT_ID:
            return value
        for prefix in ID_PREFIXES:
            if value.startswith(prefix) and len(value) > len(prefix):
                return value
        raise ValueError(
            f"IR id 必须为 {'/'.join(ID_PREFIXES)} 之一前缀 + 非空尾段"
            f"（或虚拟根哨兵 {VIRTUAL_ROOT_ID!r}）：{value!r}"
        )

    @model_validator(mode="before")
    @classmethod
    def _fallback_unknown_type(cls, data: Any) -> Any:
        """未知 type 兜底 paragraph（§13.9③）+ 原始类型 setdefault 进 meta.raw_type
        （已有值不覆盖——装配器/worker 侧的全量留痕优先）。"""
        if isinstance(data, dict):
            raw_type = data.get("type")
            if isinstance(raw_type, str) and raw_type not in NODE_TYPE_VALUES:
                data = dict(data)
                data["type"] = UNKNOWN_TYPE_FALLBACK.value
                meta = dict(data.get("meta") or {})
                meta.setdefault("raw_type", raw_type)
                data["meta"] = meta
        return data

    @model_validator(mode="after")
    def _require_raw_type(self) -> "IrNode":
        """`meta.raw_type` 全量必带（§13.8#4：双层 type——原始类型永远可查）。"""
        if "raw_type" not in self.meta:
            raise ValueError("meta.raw_type 必带（双层 type 契约——全量原始类型留痕，§13.8#4）")
        return self


def serialize_ir(root: IrNode) -> str:
    """整树 → ir.json JSON 字符串。

    **无 prev/next 兄弟指针**（字段级 exclude 自动省略）——见模块装载钉注；
    null 值保留（契约 §13.3 示例形态 parent_id: null 字面一致）。
    """
    return root.model_dump_json()


def deserialize_ir(raw: str | bytes) -> IrNode:
    """ir.json → IrNode（**不重建兄弟指针**——装载钉注：调用方须再调
    `rebuild_siblings`；经 `storage.get_ir_json` 的链路已内置）。"""
    return IrNode.model_validate_json(raw)


def rebuild_siblings(root: IrNode) -> IrNode:
    """由 children 数组序重建 prev/next 兄弟指针（data-model §1.18 :544 装载钉注）。

    重建范围 = root 的各层 children（兄弟关系仅由 children 数组序定义）；**root 自身
    的 prev/next 不在重建范围**（其兄弟位置须由父层装配/装载负责——装配器组合场景
    attach_block_tree 不抹掉目录层已接好的同级指针）。迭代 DFS（深树免递归上限）；
    返回 root（链式调用友好）。
    """
    stack: list[IrNode] = [root]
    while stack:
        node = stack.pop()
        siblings = node.children
        for i, child in enumerate(siblings):
            child.prev = siblings[i - 1] if i > 0 else None
            child.next = siblings[i + 1] if i + 1 < len(siblings) else None
            stack.append(child)
    return root


# ---------------------------------------------------------------------------
# document-ref 占位符工具（§1.18 / §13.8#3——切片间引用占位符协议）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DocumentRef:
    """占位符三元组（§13.6：`<document-ref id="{node_id}" kind="{type}">标签</document-ref>`）。"""

    node_id: str
    kind: str
    label: str


DOCUMENT_REF_TEMPLATE = '<document-ref id="{node_id}" kind="{kind}">{label}</document-ref>'

_DOCUMENT_REF_RE = re.compile(
    r'<document-ref id="(?P<id>[^"]+)" kind="(?P<kind>[^"]+)">(?P<label>.*?)</document-ref>',
    re.DOTALL,
)

#: PUA 私有区起点（契约钉 U+F0000 起）；容量覆盖 Plane 15+16 PUA（~12.9 万/文档段）
PUA_BASE = 0xF0000
_PUA_END = 0x10FFFD


def make_document_ref(node_id: str, kind: str, label: str) -> str:
    """构造 document-ref 占位符（形态唯一出口——禁止手拼）。"""
    return DOCUMENT_REF_TEMPLATE.format(node_id=node_id, kind=kind, label=label)


def find_document_refs(text: str) -> list[DocumentRef]:
    """提取全部闭合占位符（切分期 references 筛选与完整性校验的读取面）。"""
    return [
        DocumentRef(node_id=m.group("id"), kind=m.group("kind"), label=m.group("label"))
        for m in _DOCUMENT_REF_RE.finditer(text)
    ]


def protect_document_refs(text: str) -> tuple[str, dict[str, str]]:
    """占位符完整性校验 + PUA U+F0000 私有区单字符替换保护（防切分切碎）。

    返回 `(保护后文本, {PUA 字符: 原占位符文本})`；每个占位符 → 恰 1 个字符，切碎后
    `restore_document_refs` 还原逐字节一致。残留 `<document-ref` / `</document-ref`
    片段（未闭合/嵌套破坏）→ ValueError（契约：切分时占位符完整性校验）。输入已含
    PUA 私有区字符时偏移上移避碰撞（既有字符不参与映射、还原不触碰）。
    """
    offset = PUA_BASE
    for ch in text:
        code = ord(ch)
        if code >= offset:
            offset = code + 1
    matches = _DOCUMENT_REF_RE.findall(text)
    if offset + len(matches) > _PUA_END:
        raise ValueError("document-ref 数量超出 PUA 私有区容量（异常输入）")
    mapping: dict[str, str] = {}
    counter = 0

    def _sub(match: re.Match[str]) -> str:
        nonlocal counter
        pua = chr(offset + counter)
        counter += 1
        mapping[pua] = match.group(0)
        return pua

    protected = _DOCUMENT_REF_RE.sub(_sub, text)
    if "<document-ref" in protected or "</document-ref" in protected:
        raise ValueError("document-ref 占位符不完整（未闭合或残留片段）——切分前完整性校验拒绝")
    return protected, mapping


def restore_document_refs(text: str, mapping: dict[str, str]) -> str:
    """PUA 字符 → 原占位符文本（切分完成后还原；逐字节一致语义见 ir_schema 测试）。"""
    for pua, original in mapping.items():
        text = text.replace(pua, original)
    return text
