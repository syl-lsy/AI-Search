"""integration 层：KB IR 存储真实 MinIO roundtrip（M3 批次 1 / B16）。

**零 DB fixture**（并行数据层线在做测试库引导，避免冲突）：本文件不请求任何 DB
fixture（无 requires_db），直连仓库根 .env 的 MINIO_* 组**自建客户端**（与
save_snapshot live 同构；凭据只在内存流转，零打印零落盘）。缺 MINIO_ 凭据或
MinIO 不可达 → skip 带原因（不让命令红掉）。

两用例：
- **自建客户端注入**：自建 Minio 客户端经 client 注入面走 put/get（锁注入缝 + 键约定）；
- **默认工厂路径**：不注入（生产形态）走 put/get——锁惰性工厂对真实 SDK（minio 7.2.20）
  的签名咬合（bucket_exists/make_bucket/put_object/get_object 形态）。
cleanup：对象 remove（uuid4 唯一 user/doc，不触碰既有对象）。
"""

import asyncio
import importlib
import uuid
from typing import Any

import pytest

from app.core.config import minio_settings
from app.rag.ir_schema import IrNode
from app.rag.storage import get_ir_json, ir_object_key, put_ir_json


def _build_client() -> Any:
    """自建 MinIO 客户端（storage 惰性工厂同构：settings 单例 + importlib 解析）。"""
    minio = importlib.import_module("minio")
    return minio.Minio(
        minio_settings.endpoint,
        access_key=minio_settings.access_key.get_secret_value(),
        secret_key=minio_settings.secret_key.get_secret_value(),
        secure=minio_settings.secure,
    )


def _skip_if_minio_unavailable(client: Any) -> None:
    if not minio_settings.access_key.get_secret_value() or not minio_settings.secret_key.get_secret_value():
        pytest.skip("缺 MINIO_ 凭据（仓库根 .env MINIO_ACCESS_KEY/MINIO_SECRET_KEY）——真实 MinIO 用例跳过")
    try:
        client.bucket_exists(minio_settings.bucket_kb)
    except Exception as exc:  # noqa: BLE001 —— 连接失败（网络/容器未起）统一 skip 带原因
        pytest.skip(f"MinIO 不可达（{type(exc).__name__}）——真实 MinIO 用例跳过")


def _tree() -> IrNode:
    doc = IrNode(
        id="fs:node:it-doc",
        type="document",
        title="集成实测文档",
        doc_kind="docx",
        loc={"origin": "feishu", "space_id": "7708", "node_token": "tok-it"},
        meta={"raw_type": "docx"},
    )
    doc.children = [
        IrNode(
            id="fs:blk:it-1",
            parent_id="fs:node:it-doc",
            type="heading",
            level=1,
            title="一级标题",
            meta={"raw_type": 3},
        ),
        IrNode(
            id="fs:blk:it-2",
            parent_id="fs:node:it-doc",
            type="paragraph",
            text="合成正文（无真实数据）",
            meta={"raw_type": 2},
        ),
    ]
    return doc


async def test_ir_json_roundtrip_with_self_built_client() -> None:
    client = _build_client()
    _skip_if_minio_unavailable(client)

    user_id, doc_id = str(uuid.uuid4()), str(uuid.uuid4())
    key = await put_ir_json(user_id, doc_id, _tree(), client=client)
    assert key == ir_object_key(user_id, doc_id)

    back = await get_ir_json(user_id, doc_id, client=client)
    assert back.model_dump() == _tree().model_dump()
    assert back.children[0].next is back.children[1]  # 装载钉注：兄弟指针已重建

    await asyncio.to_thread(client.remove_object, minio_settings.bucket_kb, key)


async def test_ir_json_roundtrip_via_default_factory_path() -> None:
    client = _build_client()
    _skip_if_minio_unavailable(client)

    user_id, doc_id = str(uuid.uuid4()), str(uuid.uuid4())
    tree = _tree()
    key = await put_ir_json(user_id, doc_id, tree)  # 零注入 = 生产默认路径
    back = await get_ir_json(user_id, doc_id)
    assert back.model_dump() == tree.model_dump()
    assert key == ir_object_key(user_id, doc_id)

    await asyncio.to_thread(client.remove_object, minio_settings.bucket_kb, key)
