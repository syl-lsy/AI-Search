"""KB 域 Pydantic DTO（api.md §4.1~§4.5 响应契约；M3/B17）。

投影纪律（front 冻结契约镜像）：
- §4.1 KbDocumentCreated 恒八键（**无 source_url**——md/pdf 行不暴露来源）；
- §4.2 KbDocumentCollected = 八键 + source_url + duplicated（十键）；
- §4.3/§4.4 KbDocument 恒十三键 + source_url **可选键**（仅 url 行出现——
  服务层只对 url 行设置该键，构造期未设置的键经 `model_dump(exclude_unset=True)`
  序列化即「缺则省略」，路由层按此口径出 JSON，不置 null、不伪造）；
- parse_meta 存储四键 → REST 三键投影（parser 为存储侧键不进投影），
  空对象/缺省 → null（投影归 app/rag/pipeline.project_parse_meta）。

本模块只承载 wire 形态，不承载校验规则（枚举合法性在 api 路由层 + 服务层承担，
状态值域镜像 db/models/kb.KbDocStatus 六态——契约变更两侧同步）。
"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class KbDocumentCreated(BaseModel):
    """§4.1 上传响应（201；恒八键，无 source_url）。"""

    document_id: str
    doc_type: str
    title: str
    file_size_bytes: int
    status: str
    fail_reason: str | None
    chunk_count: int
    created_at: datetime


class KbDocumentCollected(BaseModel):
    """§4.2 URL 收藏响应（200；八键 + source_url + duplicated）。"""

    document_id: str
    doc_type: str
    title: str
    source_url: str
    file_size_bytes: int | None
    status: str
    fail_reason: str | None
    chunk_count: int
    created_at: datetime
    duplicated: bool


class KbDocument(BaseModel):
    """§4.3/§4.4 列表项与详情（恒十三键 + source_url 可选键缺则省略）。"""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    doc_type: str
    title: str
    source_url: str | None = Field(default=None)  # 可选键：仅 url 行由服务层设置
    file_size_bytes: int | None
    status: str
    fail_reason: str | None
    chunk_count: int
    created_at: datetime
    updated_at: datetime
    ready_at: datetime | None
    parse_meta: dict[str, object] | None
    catalog_ids: list[str] | None
    catalog_path: str | None


class KbDeleteData(BaseModel):
    """§4.5 删除响应 data（{"deleted": true}）。"""

    deleted: bool


class KbUrlCollectRequest(BaseModel):
    """§4.2 URL 收藏请求体（url 必填——缺省经 RequestValidationError → 400001）。"""

    url: str


class KbDocumentListQuery(BaseModel):
    """§4.3 列表查询投影（空串=不过滤由路由层转 None；枚举校验在路由层 400001）。"""

    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)
    status: str | None = None
    doc_type: str | None = None


# ---------------------------------------------------------------------------
# 飞书导入任务（api.md §4.7~4.9；B20 B 线④——键位镜像 data-model §1.17）
# ---------------------------------------------------------------------------


class KbImportJobCreateRequest(BaseModel):
    """§4.7 请求体（connector：M3 仅 feishu 合法——其他值路由层 400001）。"""

    connector: str


class KbImportJobCreated(BaseModel):
    """§4.7 响应 data（触发成功：{job_id, status:"pending"}）。"""

    job_id: str
    status: str


class KbImportFailure(BaseModel):
    """失败清单条目（四键；document_id=null=文档未建成——api.md §4.9）。"""

    title: str
    category: str
    message: str
    document_id: str | None


class KbImportJobEntry(BaseModel):
    """§4.8 列表条目 = §4.9 详情条目不含 failures（失败清单随详情返回）。"""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    connector: str
    trigger: str
    status: str
    total_docs: int
    processed_docs: int
    imported_docs: int
    failed_docs: int
    skipped_docs: int
    failure_summary: list[dict[str, object]]
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class KbImportJobDetail(KbImportJobEntry):
    """§4.9 详情（failures 按处理序排列；仅本端点返回、不设分页——自用 931+ 量级）。"""

    failures: list[KbImportFailure]


class KbChunkDetail(BaseModel):
    """§4.6 KB 来源溯源下钻响应 data（REQ-053；v1.30/v1.31 已批准——纯实现批 B19）。

    六必选（chunk_id/document_id/document_title/content/heading_path/page/paragraph）
    + 两可选（node_token/node_id 仅飞书来源携带、缺则省略——沿 v1.12 images 可选键
    先例不置 null；**修订注 2**：fs:blk: 来源保留 node_id、省略 node_token）。
    `page`/`paragraph` 键恒出现、值可空（无定位 null——契约字面）。
    **溯源内容仅进 UI 不进 LLM**（content 前端渲染过 DOMPurify——frontend-api §5）。
    """

    chunk_id: str
    document_id: str
    document_title: str
    content: str
    heading_path: list[str]
    page: int | None
    paragraph: int | None
    node_token: str | None = None
    node_id: str | None = None
