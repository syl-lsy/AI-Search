"""归一化管线跨模块公共类型（registry 与三适配器共用，避免循环 import）。

NormalizationResult = 归一化产物单元：单文档树根（loc:doc: 前缀，children=块树）
+ parse_meta。多文档 Forest 装配（虚拟单根）属飞书树装配器/入库流水线职责——本包
以「单文档」为产出单元（ADR-018① Forest 形态中 document 单元的直接生产者）。
"""
from dataclasses import dataclass, field
from typing import Any

from app.rag.ir_schema import IrNode


@dataclass(slots=True)
class NormalizationResult:
    """单文档归一化产物：document 树根 + parse_meta（data-model §1.7 形态）。

    parse_meta 契约四键：`{pages, tables, code_blocks, parser}`——三键对齐 REST 投影
    （pages 页式来源有值否则 None；tables/code_blocks 计数缺省 0；parser=归一化通道，
    **多组件流水记末段实际执行组件**，存储侧留痕不进投影）；存储侧附加键
    （heading_heuristic / heading_outline / ocr）不进投影，随 ir.json 留痕
    （heading_outline 在 `KB_PDF_OUTLINE_CORRECTION=false` 时整键不写）。
    """

    document: IrNode
    parse_meta: dict[str, Any] = field(default_factory=dict)
