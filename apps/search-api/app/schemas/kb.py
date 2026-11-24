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
