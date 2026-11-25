"""integration：飞书真凭据冒烟（显式门控，默认 skip——沿 RUN_PADDLE_OCR_SMOKE 先例）。

`RUN_FEISHU_IMPORT_SMOKE=1 make test-integration` 显式启用；主会话/用户执行，
不阻塞常规测试循环。真链路：settings 读凭据（FEISHU_APP_ID/SECRET——只进 .env，
零打印零回显）→ 列知识空间 → 单层节点遍历 → 选首个 docx 文档读 blocks 翻全
（page 块必在）→ file 附件下载面只探错误码路径不实下（下载消耗配额，默认探针只验
blocks 通道）。B20 A 线 SDK 通道的活体佐证；断言结构不断言文案。
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_FEISHU_IMPORT_SMOKE") != "1",
    reason="真飞书凭据链路（wiki/docx 通道）：设 RUN_FEISHU_IMPORT_SMOKE=1 启用",
)


def _require_client() -> object:
    from app.rag import feishu_client

    return feishu_client.get_default_feishu_client()


@pytest.mark.asyncio
async def test_list_spaces_and_read_one_doc_blocks() -> None:
    client = _require_client()
    spaces = await client.list_spaces()  # type: ignore[attr-defined]
    assert spaces, "知识空间列表为空：应用未加为知识库成员或未开通 wiki:wiki:readonly"

    from app.core.config import feishu_settings

    space_id = feishu_settings.space_id or spaces[0].space_id
    page, err = await client.list_space_nodes(space_id)  # type: ignore[attr-defined]
    assert err == "", f"根层节点拉取失败：{err}"
    assert page, "根层节点为空"

    doc = next((n for n in page if n.obj_type == "docx" and n.node_type != "shortcut"), None)
    if doc is None:
        pytest.skip("根层无 docx 节点（结构断言不适用）")

    blocks = await client.list_document_blocks(doc.obj_token)  # type: ignore[attr-defined]
    assert blocks.items, "blocks 为空"
    assert any(b.block_type == 1 for b in blocks.items), "page 根块缺失"


@pytest.mark.asyncio
async def test_blocks_to_ir_mapping_live_shape() -> None:
    """真 blocks → 映射器：结构面断言（refs 非空 + raw_type 前缀 + 页根序存在）。"""
    client = _require_client()
    spaces = await client.list_spaces()  # type: ignore[attr-defined]
    if not spaces:
        pytest.skip("无知识空间")
    from app.core.config import feishu_settings

    space_id = feishu_settings.space_id or spaces[0].space_id
    page, err = await client.list_space_nodes(space_id)  # type: ignore[attr-defined]
    assert err == ""
    doc = next((n for n in page if n.obj_type == "docx" and n.node_type != "shortcut"), None)
    if doc is None:
        pytest.skip("根层无 docx 节点")

    from app.rag.feishu_mapper import map_blocks

    blocks = await client.list_document_blocks(doc.obj_token)  # type: ignore[attr-defined]
    result = map_blocks(blocks.items, space_id=space_id, node_token=doc.node_token)
    assert result.refs or result.root_order is not None
    for ref in result.refs:
        assert ref.node.id.startswith("fs:blk:")
        assert "raw_type" in ref.node.meta
