"""save_snapshot：网页快照工具（M2 批次 1——data-model.md §1.6、ARCH §6.3/§10 Q2、REQ-015）。

深度子代理专用（三工具面之一，DEV §9）；**fast 模式工具集物理不含**（fast 图面模块
零导入 + SEARCH_SOURCE_NAMES 白名单外，AST 哨兵单测锁死——共识约束的代码级保证）。

链路（ARCH §6.3）：Firecrawl markdown → MinIO 对象（object_key 契约
`snapshots/{user_id}/{thread_id}/{turn_id}/{url_hash}.md`）→ snapshots 表登记 →
SourceCard `snapshot_available=true`（REST 投影归 turn_service，批次 4 接线）。

**url_hash 幂等**（uq_snapshots_turn_url）：同轮同 URL 重复执行零重复行/零重复对象——
首查命中既有行即复用返回（不再 put 对象、不再插行）；并发竞态唯一键 IntegrityError
→ 回滚 → 回查既有行幂等复用（对象同键覆盖不产生重复对象）。

**>2MB 口径（ARCH §10 Q2，实现侧裁定，供巡检引用）**：Q2 为 §10 未决问题建议默认值，
冻结契约（data-model.md §1.6）无 2MB 条款且快照行仅元数据无正文列——两个半句
「截断入库 / 对象全量存 MinIO」的可执行交集 = **对象恒全量写入 + 行 size_bytes 全量 +
返回面 oversize 留痕**（供批次 2 research 节点与文档巡检裁决）；「截断入库」在当前
DDL 下无行级承载（api.md §5.1 摘录 2000 字符为读取期口径，与本工具无关）。

**分层注**：本模块属 agent 工具面（非图节点）——图节点纯函数铁律不破（batch 2 经
configurable 注入 session_factory/minio 工厂）；工具自身携带 IO（同 ARCH §6.3 把
表登记显式分配给本工具），session 为**短事务例外**（自管 commit，铁律 10 同
TurnRunner/KB worker 口径）。minio SDK 惰性导入（object_cleanup 同款纪律：SDK 未装
不阻塞应用启动，生产接线前须补运行依赖——见批次 2 派单注记）。
"""

import asyncio
import hashlib
import importlib
import io
import uuid as uuid_module
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import minio_settings
from app.core.logger import BusinessLog
from app.db.models.snapshot import Snapshot

__all__ = [
    "SNAPSHOT_OVERSIZE_BYTES",
    "SnapshotError",
    "object_key_for",
    "save_snapshot",
    "url_hash_for",
]

#: 大文件口径阈值（ARCH §10 Q2「大文件 >2MB」；返回面 oversize 留痕判定值）
SNAPSHOT_OVERSIZE_BYTES = 2 * 1024 * 1024

#: 快照正文形态（§1.6 content_type 默认；Firecrawl markdown）
SNAPSHOT_CONTENT_TYPE = "text/markdown"


class SnapshotError(Exception):
    """快照基建失败（MinIO/DB IO 面）——原异常经 raise from 链入，节点侧记 errors。"""


def url_hash_for(url: str) -> str:
    """sha256(url) hex64（§1.6：唯一键用，URL 可能超长）。"""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def object_key_for(user_id: str, thread_id: str, turn_id: str, url_hash: str) -> str:
    """MinIO 对象键（§1.6 object_key 契约路径，REQ-011 user_id 前缀口径）。"""
    return f"snapshots/{user_id}/{thread_id}/{turn_id}/{url_hash}.md"


def _default_session_factory() -> Any:
    """生产会话工厂（懒导入——工具模块导入期不触 db 引擎单例）。"""
    from app.db.session import get_session_factory

    return get_session_factory()


def _default_minio_client_factory() -> Any:
    """MinIO 客户端惰性构造（object_cleanup 同款：SDK 运行期 importlib 解析）。"""
    minio = importlib.import_module("minio")
    return minio.Minio(
        minio_settings.endpoint,
        access_key=minio_settings.access_key.get_secret_value(),
        secret_key=minio_settings.secret_key.get_secret_value(),
        secure=minio_settings.secure,
    )


async def _find_snapshot(session: Any, turn_uuid: uuid_module.UUID, u_hash: str) -> Any:
    """同轮同 URL 既有行查询（幂等首查 / 竞态回查共用；AsyncSession 面）。"""
    result = await session.execute(
        select(Snapshot).where(Snapshot.turn_id == turn_uuid, Snapshot.url_hash == u_hash)
    )
    return result.scalar_one_or_none()


def _meta(row: Any, *, reused: bool) -> dict[str, Any]:
    """行 → 工具返回元数据（正文本体不回传——上下文面只留引用键）。"""
    size = int(row.size_bytes)
    return {
        "snapshot_id": row.id,
        "url": row.url,
        "url_hash": row.url_hash,
        "object_key": row.object_key,
        "size_bytes": size,
        "reused": reused,
        "oversize": size > SNAPSHOT_OVERSIZE_BYTES,
    }


def _ensure_bucket_and_put(client: Any, object_key: str, content: str) -> None:
    """建桶（缺失时）+ 全量对象写入（同步 SDK 面，asyncio.to_thread 下放）。

    compose 无建桶初始化容器（deploy/minio 仅 policy.json）——工具侧自兜桶存在性；
    对象恒全量字节（「对象全量存 MinIO」，Q2 半句的可执行面）。
    """
    bucket = minio_settings.bucket_snapshot
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
    data = content.encode("utf-8")
    client.put_object(
        bucket,
        object_key,
        io.BytesIO(data),
        length=len(data),
        content_type=SNAPSHOT_CONTENT_TYPE,
    )


async def save_snapshot(
    *,
    url: str,
    content: str,
    user_id: str,
    thread_id: str,
    turn_id: str,
    session_factory: Callable[[], Any] | None = None,
    minio_client_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """写快照对象 + 登记 snapshots 行（幂等；返回引用元数据，不含正文本体）。

    失败语义：MinIO 写失败 → SnapshotError（零行登记——不产孤儿行）；登记唯一键
    冲突（并发竞态）→ 幂等复用既有行（WARN 留痕）；标识/入参非法 → ValueError
    （编程错误，先于任何 IO）。
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("url 必填（非空白）")
    if not content:
        raise ValueError("content 必填（非空）")
    try:
        user_uuid = uuid_module.UUID(str(user_id))
        thread_uuid = uuid_module.UUID(str(thread_id))
        turn_uuid = uuid_module.UUID(str(turn_id))
    except ValueError as exc:
        raise ValueError(f"标识须为合法 UUID：{exc}") from exc

    u_hash = url_hash_for(url)
    object_key = object_key_for(str(user_id), str(thread_id), str(turn_id), u_hash)
    size_bytes = len(content.encode("utf-8"))
    session_factory = session_factory or _default_session_factory
    minio_client_factory = minio_client_factory or _default_minio_client_factory

    async with session_factory() as session:
        existing = await _find_snapshot(session, turn_uuid, u_hash)
        if existing is not None:
            return _meta(existing, reused=True)  # 幂等复用：零重复行/零重复对象

        client = await asyncio.to_thread(minio_client_factory)
        try:
            await asyncio.to_thread(_ensure_bucket_and_put, client, object_key, content)
        except Exception as exc:
            raise SnapshotError(
                f"快照对象写入失败：{type(exc).__name__}（零行登记）"
            ) from exc

        row = Snapshot(
            user_id=user_uuid,
            thread_id=thread_uuid,
            turn_id=turn_uuid,
            url=url,
            url_hash=u_hash,
            object_key=object_key,
            content_type=SNAPSHOT_CONTENT_TYPE,
            size_bytes=size_bytes,
            fetched_at=datetime.now(timezone.utc),
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError as exc:
            # 并发竞态：唯一键 (turn_id, url_hash) 冲突 → 回查既有行幂等复用
            # （对象同键覆盖不产生重复对象；先到行的元数据为准）
            await session.rollback()
            existing = await _find_snapshot(session, turn_uuid, u_hash)
            if existing is None:
                raise SnapshotError("快照登记冲突且既有行不可见（异常竞态）") from exc
            BusinessLog(
                message="快照登记竞态冲突——幂等复用既有行（零重复行）",
                action="快照登记",
                entity="snapshot",
                entity_id=str(existing.id),
            ).warning()
            return _meta(existing, reused=True)
        return _meta(row, reused=False)
