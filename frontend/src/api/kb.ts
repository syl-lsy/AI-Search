/**
 * src/api/kb.ts — 知识库域端点（api.md §4.1~4.9；frontend-api.md §4 kbStore 行）。
 * 经 src/api/client.ts 唯一出口；URL 契约完整路径（§8.4）；类型全部取 types/api.d.ts 镜像（§8.9）。
 *
 * 消费方：kbStore（文档列表/上传/URL 收藏/删除/导入任务）+ AnswerView 溯源下钻
 * （fetchKbChunk/fetchDocument——§5 KbChunkDrilldown「数据面归 AnswerView 承载」）。
 */
import { apiDelete, apiGet, apiPost } from './client'
import type {
  CollectUrlRequest,
  KbDeleteData,
  KbDocument,
  KbDocumentListData,
  KbDocumentListQuery,
  KbChunkDetail,
  KbImportJobDetail,
  KbImportJobListData,
  KbImportTriggerData,
  KbImportTriggerRequest,
} from '@/types/api'

/** §4.7 M3 仅 feishu 合法（其他值 400001——连接器扩展沿此键位） */
const IMPORT_CONNECTOR: KbImportTriggerRequest['connector'] = 'feishu'

/** §4.3 GET /api/v1/kb/documents —— 文档列表（Library 表格；查询参 `?limit=&offset=&status=&doc_type=`，
 * listThreads §2.2 同形态。P1-A 分页消费面：首屏/触底 limit+offset、ready 已入库计数 status 条件请求） */
export function fetchDocuments(query: KbDocumentListQuery = {}): Promise<KbDocumentListData> {
  return apiGet<KbDocumentListData>('/api/v1/kb/documents', { params: query })
}

/** §4.1 POST /api/v1/kb/documents —— 上传文档（multipart：字段 file + title，缺省标题由服务端取文件名；413001/415001 入口校验） */
export function uploadDocument(file: File, title: string): Promise<KbDocument> {
  // multipart（字段 file+title，§1）；axios 自动设置 multipart boundary，勿手写 Content-Type
  const form = new FormData()
  form.append('file', file)
  form.append('title', title)
  return apiPost<KbDocument>('/api/v1/kb/documents', form)
}

/** §4.2 POST /api/v1/kb/urls —— URL 收藏入库（重复收藏不报错、返回既有条目带 duplicated；422001） */
export function collectUrl(url: string): Promise<KbDocument> {
  // duplicated 为 KbDocument 可选键（api.d.ts §4.2），响应原样透传
  return apiPost<KbDocument>('/api/v1/kb/urls', { url } satisfies CollectUrlRequest)
}

/** §4.4 GET /api/v1/kb/documents/{document_id} —— 单文档详情（字段同列表项；下钻文档面 parse_meta/catalog_path 来源） */
export function fetchDocument(documentId: string): Promise<KbDocument> {
  return apiGet<KbDocument>(`/api/v1/kb/documents/${documentId}`)
}

/** §4.5 DELETE /api/v1/kb/documents/{document_id} —— 删除（三存储级联 kb_chunks‖ES‖MinIO；二次确认归视图层） */
export function removeDocument(documentId: string): Promise<KbDeleteData> {
  return apiDelete<KbDeleteData>(`/api/v1/kb/documents/${documentId}`)
}

/**
 * §4.6 GET /api/v1/sources/{source_id}/kb-chunk —— KB 来源溯源下钻（REQ-053）。
 * 六必选键 + 两可选键 node_token/node_id（仅飞书来源携带、缺则省略不置 null，v1.31）；
 * 防重入归调用方；404001 单码（不存在/越权/已删除）→ 调用方优雅降级，400001 非 KB 来源。
 */
export function fetchKbChunk(sourceId: string): Promise<KbChunkDetail> {
  return apiGet<KbChunkDetail>(`/api/v1/sources/${sourceId}/kb-chunk`)
}

/** §4.7 POST /api/v1/kb/import/jobs —— 手动触发飞书导入批次（409007 进行中重复触发/400001 connector 非法/500001 兜底） */
export function triggerImport(): Promise<KbImportTriggerData> {
  return apiPost<KbImportTriggerData>('/api/v1/kb/import/jobs', { connector: IMPORT_CONNECTOR } satisfies KbImportTriggerRequest)
}

/** §4.8 GET /api/v1/kb/import/jobs —— 导入任务列表（created_at DESC；条目不含 failures——清单随 §4.9 详情） */
export function fetchImportJobs(): Promise<KbImportJobListData> {
  return apiGet<KbImportJobListData>('/api/v1/kb/import/jobs')
}

/** §4.9 GET /api/v1/kb/import/jobs/{job_id} —— 导入任务详情（进度五数 + failure_summary + failures 清单） */
export function fetchImportJobDetail(jobId: string): Promise<KbImportJobDetail> {
  return apiGet<KbImportJobDetail>(`/api/v1/kb/import/jobs/${jobId}`)
}
