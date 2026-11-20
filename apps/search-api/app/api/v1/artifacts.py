"""artifacts 域路由（api.md §5.3——REQ-016 下载面；§5.2 列表归 M4）。

路由层零业务逻辑：归属校验（404001）/ 路径段一致性 / 锚定校验 / 文件读取均在
ArtifactService.open_download。**下载为 FileResponse（契约「302 预签名或 200
FileResponse 择一」落 200——产物为本机沙箱目录文件非 MinIO 对象）**：必须自设
`request.state.is_stream = True` 跳过成功信封包装（core/middleware/response.py:8-10
非 JSON 响应逃逸面——否则 FileResponse 经 json.loads 必炸 500001，SSE 端点同款
逃逸先例）。
"""
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.services.artifact_service import ArtifactService

router = APIRouter(prefix="/api/v1")


def get_artifact_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ArtifactService:
    """快照与产物域服务依赖缝（与 turns.py 同名 helper 同构——get_db 请求级事务，
    service 只 flush 不 commit；单测经 dependency_overrides 替换）。"""
    return ArtifactService(db)


@router.get("/threads/{thread_id}/artifacts/{artifact_id}/download")
async def download_artifact(
    thread_id: str,
    artifact_id: str,
    request: Request,
    user_id: Annotated[str, Depends(get_current_user)],
    service: Annotated[ArtifactService, Depends(get_artifact_service)],
) -> FileResponse:
    """产物下载（api.md §5.3，REQ-016）：前端只用 export 响应的 download_path 原样
    请求（零新契约面）；404001 = 产物不存在/越权/路径段不符/文件缺失（不泄漏存在性）。
    """
    request.state.is_stream = True  # FileResponse 逃逸信封（response.py:8-10 逃逸面）
    return await service.open_download(thread_id, artifact_id, user_id=user_id)
