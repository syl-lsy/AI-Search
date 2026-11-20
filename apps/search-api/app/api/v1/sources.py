"""sources 域路由（api.md §5.1——REQ-015 快照查看，仅深度）。

路由层零业务逻辑：归属校验（404001）/ 404002 无快照判定 / MinIO 预签名与摘录
读取均在 ArtifactService.get_source_snapshot（§5「快照与产物」同域服务——快照读取
与产物下载共用域服务）。
"""
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.schemas.artifact import SnapshotDetail
from app.services.artifact_service import ArtifactService

router = APIRouter(prefix="/api/v1")


def get_snapshot_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ArtifactService:
    """快照读取服务依赖缝（默认 MinIO 惰性工厂——活体链路；单测经
    dependency_overrides 替换或直注 mock 工厂）。"""
    return ArtifactService(db)


@router.get("/sources/{source_id}/snapshot", response_model=SnapshotDetail)
async def get_source_snapshot(
    source_id: str,
    user_id: Annotated[str, Depends(get_current_user)],
    service: Annotated[ArtifactService, Depends(get_snapshot_service)],
) -> SnapshotDetail:
    """快照查看（api.md §5.1，REQ-015）：快照元数据 + 10 分钟 MinIO 预签名 URL +
    开头 2000 字符摘录（弹窗内预览）。source_id 来自 SSE sources 事件与 §2.5
    sources[]；404001 来源不存在/越权、404002 无快照（快速模式来源或深度未抓原文）。
    """
    return await service.get_source_snapshot(source_id, user_id=user_id)
