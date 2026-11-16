"""线程对象 best-effort 清理（MinIO 快照/产物前缀删除，api.md §2.11 v1.18 / D-04）。

线程物理删除后，关联的对象存储条目（快照 `snapshots/{user_id}/{thread_id}/…`、后续
产物）按前缀 best-effort 删除；**失败只落 WARN 日志、不回滚 DB 删除**——强一致
（先清对象、失败即 500 回滚）会让 MinIO 抖动阻断删除主流程，自用量级下孤儿对象挂
二期存储治理（data-model §1.2 v1.14 底注 / OQ-10）。

实现边界：
- 注入缝 `ThreadObjectPurger`——ThreadService 构造注入，单测用假体断言调用面与
  WARN 路径；默认实现 `MinioThreadObjectPurger`；
- minio SDK 惰性导入（`importlib`）：0002-snapshots 迁移尚未落地期 SDK 不在运行依赖，
  缺 SDK/缺凭据/网络故障统一走失败分支（WARN 后返回），删除主流程不受影响；
- 同步 SDK 调用经 `asyncio.to_thread` 下放，不阻塞事件循环。
"""
from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable
from typing import Any, Protocol

from app.core.config import minio_settings
from app.core.logger import BusinessLog


class ThreadObjectPurger(Protocol):
    """线程关联对象清理注入缝（ThreadService 构造注入；单测假体实现同签名）。"""

    async def purge_thread_objects(self, *, user_id: str, thread_id: str) -> None:
        """删除该线程前缀下的对象；实现方自兜异常（失败不向上抛，仅日志）。"""
        ...


def _default_client_factory() -> Any:
    """MinIO 客户端惰性构造（SDK 未安装 / 凭据缺失时抛错，由调用方 WARN 跳过）。

    minio 不在当前运行依赖（快照表随 0002-snapshots 落地）：模块级 import 会让
    「未落地功能」阻塞应用启动，故走 importlib 运行期解析。
    """
    minio = importlib.import_module("minio")
    return minio.Minio(
        minio_settings.endpoint,
        access_key=minio_settings.access_key.get_secret_value(),
        secret_key=minio_settings.secret_key.get_secret_value(),
        secure=minio_settings.secure,
    )


class MinioThreadObjectPurger:
    """MinIO 前缀删除实现：`snapshots/{user_id}/{thread_id}/` 下全部对象。

    对象键契约见 data-model.md §1.6（`object_key` 列格式）——前缀以 user_id/thread_id
    双段收敛，避免跨用户误删（REQ-011 前缀口径）。
    """

    def __init__(self, *, client_factory: Callable[[], Any] | None = None) -> None:
        self._client_factory = client_factory or _default_client_factory

    async def purge_thread_objects(self, *, user_id: str, thread_id: str) -> None:
        prefix = f"snapshots/{user_id}/{thread_id}/"
        bucket = minio_settings.bucket_snapshot
        try:
            client = await asyncio.to_thread(self._client_factory)
            names = await asyncio.to_thread(self._list_keys, client, bucket, prefix)
            if names:
                errors = await asyncio.to_thread(self._remove_keys, client, bucket, names)
                for error in errors:
                    # 逐对象失败（minio-py DeleteError：name/code/message）——不聚合吞掉，
                    # 每个 key 留一条可追溯痕迹（孤儿对象治理的检索面）
                    BusinessLog(
                        message=(
                            "线程对象删除失败（best-effort，孤儿对象挂二期治理）："
                            f"key={getattr(error, 'name', '?')} "
                            f"code={getattr(error, 'code', '?')} "
                            f"message={getattr(error, 'message', '?')}"
                        ),
                        action="线程对象清理",
                        entity="thread",
                        entity_id=thread_id,
                    ).warning()
        except Exception as exc:  # noqa: BLE001 —— best-effort：任何失败只 WARN 不阻断
            BusinessLog(
                message=f"线程对象清理失败（best-effort，孤儿对象挂二期治理）：{exc}",
                action="线程对象清理",
                entity="thread",
                entity_id=thread_id,
            ).warning()

    @staticmethod
    def _list_keys(client: Any, bucket: str, prefix: str) -> list[str]:
        return [
            obj.object_name
            for obj in client.list_objects(bucket, prefix=prefix, recursive=True)
        ]

    @staticmethod
    def _remove_keys(client: Any, bucket: str, names: list[str]) -> list[Any]:
        """批量删除并**强制迭代** minio-py 的惰性 generator。

        `remove_objects` 是生成器函数：调用只构造迭代器，批量删除的 HTTP 请求在首次
        迭代时才发出（官方用法 `for err in client.remove_objects(...)`）——丢弃返回值
        等于清理静默零操作。此处 list() 消费到底，逐条返回 DeleteError 供调用方逐条
        记 WARN（成功删除的对象不产出条目）。

        载荷形态（minio-py 7.2.x）：`_delete_objects` 构造 `DeleteRequest(...)`，其
        `toxml()` 对每项调 `obj.toxml(element)`（src/minio/deleteobjects.py）——**必须
        传 `DeleteObject` 实例**；dict 载荷在消费点报 `AttributeError: 'dict' object
        has no attribute 'toxml'`（被外层 except 吞成 WARN + 零删除）。`DeleteObject`
        不在 minio 包根导出，只存在于 `minio.deleteobjects` 子模块，故一并惰性导入
        （与 `_default_client_factory` 的 minio 惰性导入纪律一致：SDK 未安装期不阻塞
        应用启动，失败统一走 WARN 分支）。
        """
        deleteobjects = importlib.import_module("minio.deleteobjects")
        return list(
            client.remove_objects(
                bucket, [deleteobjects.DeleteObject(name) for name in names]
            )
        )
