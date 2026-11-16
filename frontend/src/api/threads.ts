/**
 * src/api/threads.ts — 线程域端点（api.md §2.2 列表 / §2.3 详情 / §2.10 PATCH / §2.11 DELETE）。
 * 经 src/api/client.ts 唯一出口；URL 契约完整路径（§8.4）。
 */
import { apiDelete, apiGet, apiPatch } from './client'
import type {
  ThreadDeleteData,
  ThreadDetail,
  ThreadListData,
  ThreadListItem,
  ThreadListQuery,
  ThreadPatchData,
  ThreadPatchRequest,
} from '@/types/api'

/** §2.3 线程详情（答案页骨架；v1.18 起带「未读置已读」幂等写副作用——api.md §2.3 注） */
export function fetchThread(threadId: string): Promise<ThreadDetail> {
  return apiGet<ThreadDetail>(`/api/v1/threads/${threadId}`)
}

/** §2.2 线程列表（会话页首屏/触底翻页 + 侧栏最近；q/archived/unread 为 v1.18 参数增量） */
export function listThreads(query: ThreadListQuery = {}): Promise<ThreadListData> {
  return apiGet<ThreadListData>('/api/v1/threads', { params: query })
}

/** §2.10 部分更新（title/pinned/is_unread/archived 任意组合；响应=更新后投影，updated_at 不随动） */
export function patchThread(threadId: string, body: ThreadPatchRequest): Promise<ThreadPatchData> {
  return apiPatch<ThreadPatchData>(`/api/v1/threads/${threadId}`, body)
}

/** §2.11 物理删除（级联从属数据；running/queued 轮 409005 拒绝） */
export function deleteThread(threadId: string): Promise<ThreadDeleteData> {
  return apiDelete<ThreadDeleteData>(`/api/v1/threads/${threadId}`)
}

/** 供外部推断条目形态（导出类型复用，无运行时代码） */
export type { ThreadListItem }
