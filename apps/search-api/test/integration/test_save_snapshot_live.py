"""integration 层：快照链默认路径活体实测（M2 批次 2 闭环条件「快照落 MinIO+表登记」）。

用例走 save_snapshot **默认路径**（不注入 session_factory / minio_client_factory——
即生产形态）对真 Postgres（.env.test DB_ 组，迁移到 head）+ 真 MinIO（MINIO_ 凭据经
settings 单例读取，密钥零打印零落盘）端到端落一物一行并幂等复用。

修复前形态（live 验证 2026-11-19 发现 B，两缺陷当时使本用例必炸）：
- ①默认 session 巈厂返回 async_sessionmaker 本体，`async with` 彎态错配必抛 TypeError
  （save_snapshot._default_session_factory 已修：再调用一层产出 AsyncSession）；
- ②minio SDK 不在运行依赖，importlib ModuleNotFoundError（pyproject 已补
  minio==7.2.20）。

unit 层（test_save_snapshot.py）已锁注入面语义（幂等/竞态/oversize/失败归一）；
本用例只锁**默认路径端到端可达**——零注入假体，任何默认形态回归（工厂形态/SDK 依赖/
连接串解析）在此爆红。cleanup：对象 remove + 行 delete（migrated_db 截断面不含
snapshots 表，行由本用例自清；thread/turn 归 fixture 截断）。
"""

import asyncio
import importlib
import uuid
from typing import Any

import pytest
from sqlalchemy import delete, select

from app.agent.tools.save_snapshot import object_key_for, save_snapshot, url_hash_for

pytestmark = pytest.mark.requires_db


def _minio_client() -> Any:
    """MinIO 客户端（与 save_snapshot 默认工厂同构：settings 单例 + importlib 解析；
    凭据只在内存流转，零打印零落盘）。"""
    minio = importlib.import_module("minio")
    from app.core.config import minio_settings

    return minio.Minio(
        minio_settings.endpoint,
        access_key=minio_settings.access_key.get_secret_value(),
        secret_key=minio_settings.secret_key.get_secret_value(),
        secure=minio_settings.secure,
    )


async def _run_blocking(fn: Any, /, *args: Any) -> Any:
    """同步 SDK 调用下放线程（与工具侧 asyncio.to_thread 同语义）。"""
    return await asyncio.to_thread(fn, *args)


async def test_save_snapshot_default_path_lands_minio_and_row(migrated_db) -> None:
    """默认路径端到端：读链正文 → MinIO 对象全量落桶 + snapshots 表登记一行；
    同轮同 URL 二次执行幂等复用（零重复行、零重复对象）。

    查询面 = SQLAlchemy ORM 表达式（引擎参数绑定，零字符串拼接 SQL）。
    """
    from app.core.config import common_settings, minio_settings
    from app.db.models.snapshot import Snapshot
    from app.db.models.thread import Thread
    from app.db.models.turn import Turn
    from app.db.session import get_session_factory

    if not minio_settings.access_key.get_secret_value() or not minio_settings.secret_key.get_secret_value():
        pytest.skip("缺 MINIO_ 凭据（.env MINIO_ACCESS_KEY/MINIO_SECRET_KEY）——活体用例跳过")

    user_uuid = uuid.UUID(common_settings.default_user_id)
    thread_uuid = uuid.uuid4()
    turn_uuid = uuid.uuid4()
    url = f"https://example.com/live/{turn_uuid}"
    content = f"# 活体实测正文\n\n{turn_uuid}\n\n" + "正文内容。" * 200
    bucket = minio_settings.bucket_snapshot

    # 轮次宿主行（snapshots.turn_id FK → turns.id；经默认 session 工厂链写入）
    factory = get_session_factory()
    async with factory() as session:
        session.add(Thread(id=thread_uuid, user_id=user_uuid, title="快照链活体实测"))
        await session.flush()
        session.add(
            Turn(
                id=turn_uuid,  # 显式指定：快照 FK 引用本 id（ORM 缺省自生成 uuid4 会错位）
                thread_id=thread_uuid,
                user_id=user_uuid,
                mode="deep",
                query="快照链活体实测",
                chat_model_key="qwen",
                model_name="live-probe",
            )
        )
        await session.commit()

    client = await _run_blocking(_minio_client)
    u_hash = url_hash_for(url)
    expected_key = object_key_for(str(user_uuid), str(thread_uuid), str(turn_uuid), u_hash)
    size = len(content.encode("utf-8"))

    # ---- 首次执行（默认路径：零注入工厂）----
    meta = await save_snapshot(
        url=url,
        content=content,
        user_id=str(user_uuid),
        thread_id=str(thread_uuid),
        turn_id=str(turn_uuid),
    )
    try:
        assert meta["reused"] is False
        assert meta["object_key"] == expected_key  # §1.6 object_key 契约路径
        assert meta["size_bytes"] == size

        # MinIO 对象侧：stat 到且字节数一致（对象全量写入）
        stat = await _run_blocking(client.stat_object, bucket, expected_key)
        assert stat.size == size

        # 表登记侧：恰一行（同轮同 URL）
        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        select(Snapshot).where(
                            Snapshot.turn_id == turn_uuid, Snapshot.url_hash == u_hash
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 1
            assert rows[0].object_key == expected_key
            assert rows[0].url == url

        # ---- 二次执行：幂等复用（零重复行、零重复对象）----
        second = await save_snapshot(
            url=url,
            content=content,
            user_id=str(user_uuid),
            thread_id=str(thread_uuid),
            turn_id=str(turn_uuid),
        )
        assert second["reused"] is True
        assert second["snapshot_id"] == meta["snapshot_id"]
        async with factory() as session:
            rows = (
                (await session.execute(select(Snapshot).where(Snapshot.turn_id == turn_uuid)))
                .scalars()
                .all()
            )
        assert len(rows) == 1
    finally:
        # cleanup：对象 remove + 行 delete（snapshots 不在 migrated_db 截断面，行自清）
        await _run_blocking(client.remove_object, bucket, expected_key)
        async with factory() as session:
            await session.execute(delete(Snapshot).where(Snapshot.turn_id == turn_uuid))
            await session.commit()
