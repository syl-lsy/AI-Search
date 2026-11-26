"""kb 域路由（api.md §4.1~§4.5；M3/B17）。

路由层零业务逻辑：校验（白名单枚举 400001）与依赖注入在本层，上传/收藏/列表/
详情/删除全下沉 KbService；入库入队经 BackgroundTasks 调 `get_kb_pipeline` 缝。
入队时序（评审 P1 钉正）：Starlette ≥1.10 事件序为 handler → background →
get_db teardown——teardown commit 晚于 background，故上传路由在 add_task 前
对落象行显式 commit（铁律 10 短事务例外口径），保证 worker 新 session 读到
已提交行。

投影纪律：路由以 `model_dump(mode="json", exclude_unset=True)` 出 JSON——
source_url 等「可选键缺则省略」契约在序列化口径上落实（response_model 全量
dump 会补 null 键，故不用）。
"""
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, get_db
from app.exception import BadRequestException
from app.exception.kb import FeishuNotConfiguredException
from app.schemas.kb import (
    KbChunkDetail,
    KbDocumentListQuery,
    KbImportJobCreateRequest,
    KbUrlCollectRequest,
)
from app.rag.import_service import KbImportJobService
from app.rag.import_worker import get_import_worker
from app.rag.pipeline import KbPipeline, _default_spec_payload, get_kb_pipeline
from app.rag.service import KbService

router = APIRouter(prefix="/api/v1", tags=["kb"])

#: §4.3 过滤值域（镜像 db/models/kb.KbDocStatus 六态与 doc_type 五值——
#: 旧 parsing/embedding 作废；api.md §4.3 v1.30）
_STATUS_VALUES: frozenset[str] = frozenset(
    {"pending", "normalizing", "chunking", "indexing", "ready", "failed"}
)
_DOC_TYPE_VALUES: frozenset[str] = frozenset({"pdf", "md", "txt", "docx", "url"})

#: §4.7 connector 值域（M3 仅 feishu 合法——连接器扩展沿此键位）
_CONNECTOR_VALUES: frozenset[str] = frozenset({"feishu"})


def get_kb_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> KbService:
    """KB 服务依赖缝（单测经 dependency_overrides 替换；url_fetcher 一期缺省
    None——collect_url 对未配置通道显式 422001，不静默）。spec 快照供给接
    pipeline._default_spec_payload 同源：B15 SSRF 护栏据此读 Nacos spec
    safety.url_guard（缺省 lambda:{} 会恒落 defaults 种子，单键热更回退门
    失效——评审 P1 钉正；与 pipeline 自身装配 _default_spec_payload 同形态，
    与 turn_service._capture_spec_payload 快照消费口径一致）。es_indexer 注入
    get_kb_es_indexer()（B18 删除级联 ES 腿——best-effort WARN 不阻断删除）。"""
    from app.rag.indexers.es_indexer import get_kb_es_indexer

    return KbService(
        db,
        spec_payload_provider=_default_spec_payload,
        es_indexer=get_kb_es_indexer(),
    )


def get_kb_import_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> KbImportJobService:
    """导入任务服务依赖缝（单测经 dependency_overrides 替换）。"""
    return KbImportJobService(db)


def _require_feishu_credentials_with(app_id: str, app_secret: str) -> None:
    """凭据前置校验（纯函数面）：app_id/secret 任一为空 → 500001 兜底面
    （api.md §4.7——凭据只进 .env 禁入 Nacos，裁决 #8；业务异常出口防裸
    Exception 的 ERROR traceback 噪音，铁律 8；零凭据值回显）。"""
    if not app_id or not app_secret:
        raise FeishuNotConfiguredException(
            "飞书凭据未配置，无法触发知识库导入",
            detail="FEISHU_APP_ID / FEISHU_APP_SECRET 未配置（凭据只进 .env）",
        )


def _require_feishu_credentials() -> None:
    """凭据前置（settings 单例读取——密钥只经 settings 消费，零 str 化零打印）。"""
    from app.core.config import feishu_settings

    _require_feishu_credentials_with(
        feishu_settings.app_id.get_secret_value(),
        feishu_settings.app_secret.get_secret_value(),
    )


@router.post("/kb/documents", status_code=201)
async def upload_document(
    file: UploadFile,
    user_id: Annotated[str, Depends(get_current_user)],
    service: Annotated[KbService, Depends(get_kb_service)],
    pipeline: Annotated[KbPipeline, Depends(get_kb_pipeline)],
    background_tasks: BackgroundTasks,
    title: Annotated[str, Form()] = "",
) -> dict[str, Any]:
    """上传文档（§4.1）：multipart 落象 pending → BackgroundTasks 入库入队。"""
    data = await file.read()
    created = await service.upload_document(
        data, file.filename or "", title or None, user_id=user_id
    )
    # 显式提交后才入队（评审 P1 时序钉正）：Starlette ≥1.10 事件序 handler →
    # background → get_db teardown——teardown commit 晚于 background，行未提交
    # 则 worker 新 session SELECT 读不到（process_document 判 "gone" 静默放弃，
    # 文档永停 pending）；此处提交后 get_db teardown 二次 commit 为空操作。
    await service.db.commit()
    background_tasks.add_task(pipeline.enqueue, created.document_id)
    return created.model_dump(mode="json", exclude_unset=True)


@router.post("/kb/urls")
async def collect_url(
    body: KbUrlCollectRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    service: Annotated[KbService, Depends(get_kb_service)],
) -> dict[str, Any]:
    """URL 收藏（§4.2）：护栏 → 去重 → 抓取落象 pending。"""
    collected = await service.collect_url(body.url, user_id=user_id)
    return collected.model_dump(mode="json", exclude_unset=True)


@router.get("/kb/documents")
async def list_documents(
    user_id: Annotated[str, Depends(get_current_user)],
    service: Annotated[KbService, Depends(get_kb_service)],
    status: str = "",
    doc_type: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """文档列表（§4.3）：PageResponse 信封（total/items/page/page_size）；空串
    =不过滤；旧枚举值（如 parsing）→ 400001（400）。"""
    if status and status not in _STATUS_VALUES:
        raise BadRequestException(
            "status 取值非法", detail=f"status={status}（六态值域外）"
        )
    if doc_type and doc_type not in _DOC_TYPE_VALUES:
        raise BadRequestException(
            "doc_type 取值非法", detail=f"doc_type={doc_type}（五值值域外）"
        )
    query = KbDocumentListQuery(
        limit=limit, offset=offset, status=status or None, doc_type=doc_type or None
    )
    page = await service.list_documents(query, user_id=user_id)
    return {
        "total": page.total,
        "items": [
            item.model_dump(mode="json", exclude_unset=True) for item in page.items
        ],
        "page": page.page,
        "page_size": page.page_size,
    }


@router.get("/kb/documents/{document_id}")
async def get_document(
    document_id: str,
    user_id: Annotated[str, Depends(get_current_user)],
    service: Annotated[KbService, Depends(get_kb_service)],
) -> dict[str, Any]:
    """文档详情（§4.4）：404001 不存在/越权/非法标识同语义。"""
    doc = await service.get_document(document_id, user_id=user_id)
    return doc.model_dump(mode="json", exclude_unset=True)


@router.delete("/kb/documents/{document_id}")
async def delete_document(
    document_id: str,
    user_id: Annotated[str, Depends(get_current_user)],
    service: Annotated[KbService, Depends(get_kb_service)],
) -> dict[str, Any]:
    """删除文档（§4.5）：行删级联 + MinIO 前缀清理 best-effort；200 {"deleted": true}。"""
    result = await service.delete_document(document_id, user_id=user_id)
    return result.model_dump(mode="json", exclude_unset=True)


# ---- 飞书导入任务（api.md §4.7~4.9；M3/B20 B 线④）----


@router.post("/kb/import/jobs")
async def create_import_job(
    body: KbImportJobCreateRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    service: Annotated[KbImportJobService, Depends(get_kb_import_service)],
    worker: Annotated[Any, Depends(get_import_worker)],
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """手动触发飞书导入批次（§4.7）：建行 pending → BackgroundTasks 入队 worker。

    图外导入 worker（ADR-011）：可中断可恢复（断点游标）、5 QPS 令牌桶、失败分类
    清单重试；不进图拓扑、零 SSE 事件。错误：400001（connector 非法）/ 409007
    （已有 pending/running 任务）/ 500001（FEISHU 凭据未配置兜底面）。
    """
    if body.connector not in _CONNECTOR_VALUES:
        raise BadRequestException(
            "connector 取值非法", detail=f"connector={body.connector}（M3 仅 feishu）"
        )
    _require_feishu_credentials()
    created = await service.create_job(body.connector, user_id=user_id)
    # 显式提交后才入队（评审 P1 时序钉正同款）：handler → background → get_db
    # teardown——worker 新 session 须读到已提交行
    await service.db.commit()
    background_tasks.add_task(worker.enqueue, created.job_id)
    return created.model_dump(mode="json", exclude_unset=True)


@router.get("/kb/import/jobs")
async def list_import_jobs(
    user_id: Annotated[str, Depends(get_current_user)],
    service: Annotated[KbImportJobService, Depends(get_kb_import_service)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """导入任务列表（§4.8）：?limit=50&offset=0（§0 分页约定）+ created_at DESC；
    items 同详情条目不含 failures（失败清单随详情返回）。

    评审 P3：limit/offset 走 FastAPI Query 校验通道（越界 → RequestValidationError
    → 400001）——handler 体内手动实例化 KbDocumentListQuery 会触发未注册 ERROR_MAP
    的 pydantic ValidationError → 兜底 500001，语义错位。"""
    page = await service.list_jobs(limit, offset, user_id=user_id)
    return {
        "total": page.total,
        "items": [
            item.model_dump(mode="json", exclude_unset=True) for item in page.items
        ],
        "page": page.page,
        "page_size": page.page_size,
    }


@router.get("/kb/import/jobs/{job_id}")
async def get_import_job(
    job_id: str,
    user_id: Annotated[str, Depends(get_current_user)],
    service: Annotated[KbImportJobService, Depends(get_kb_import_service)],
) -> dict[str, Any]:
    """导入任务详情（§4.9）：进度五数/失败分类/failures 清单；404001 不存在或越权。"""
    detail = await service.get_job(job_id, user_id=user_id)
    return detail.model_dump(mode="json", exclude_unset=True)


@router.get("/sources/{source_id}/kb-chunk")
async def get_source_kb_chunk(
    source_id: str,
    user_id: Annotated[str, Depends(get_current_user)],
    service: Annotated[KbService, Depends(get_kb_service)],
) -> dict[str, Any]:
    """KB 来源溯源下钻（api.md §4.6——REQ-053/A36，已冻结契约纯实现批 B19）。

    路由层零业务逻辑：归属/存在/删除语义全在 KbService.get_chunk_drilldown
    （404001 单码）；非 KB 来源 400001。投影 `model_dump(mode="json",
    exclude_unset=True)`——node_token/node_id 可选键缺则省略（page/paragraph 恒
    出现——DTO 构造时显式 set），本地/URL 来源两可选键省略不置 null。溯源内容
    仅进 UI 不进 LLM（前端 DOMPurify——frontend-api.md §5）。"""
    detail: KbChunkDetail = await service.get_chunk_drilldown(source_id, user_id=user_id)
    return detail.model_dump(mode="json", exclude_unset=True)
