"""`<external_content>` 包裹体共享 helper（ARCH §6.4.3 铁律构造点——M2 批次 1 共享化）。

此前两处独立实现（`synthesis._sources_block` 来源装配 + `followups_service._wrap_*`
来源/答案装配）存在格式漂移风险；收敛为本模块单一实现后，改包裹格式只动一处
（§6.4.3 契约面），两消费面产物逐字节不变（golden 锁面见
`test/unit/test_external_content_helper.py`）。

转义硬边界分工（§6.4.3，与收敛前口径一致）：
- **属性值由 helper 转义**：source/kind 经 html.escape(quote=True)——含引号/尖括号/&/时
  实体化，属性边界不可被内容击穿（提前收口 source 属性/伪造新属性）；
- **内容由调用方转义**：content 含闭标记时同样须实体化为惰性文本（提前闭合包裹→指令
  文本抬升为提示词正文）——synthesis 对 title/snippet、followups 对 title/answer 各自
  escape 后再拼入 content。声明行（软防线）在提示词模板内，转义是硬边界。
"""

import html

__all__ = ["external_content"]


def external_content(content: str, *, source: str, kind: str | None = None) -> str:
    """单条包裹体：`<external_content source="…" type="…">\\n{content}\\n</external_content>`。

    kind=None 时不产 type 属性（answer 块形态）；属性序恒 source→type、换行体恒
    开标记行 / content / 闭标记行（既有 golden 锁面，行为零变更）。
    """
    attrs = f' source="{html.escape(source, quote=True)}"'
    if kind is not None:
        attrs += f' type="{html.escape(kind, quote=True)}"'
    return f"<external_content{attrs}>\n{content}\n</external_content>"
