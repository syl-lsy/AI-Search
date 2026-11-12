/**
 * src/api/threads.ts — 线程详情（api.md §2.3 GET /threads/{thread_id}，2.3 骨架）。
 * 经 src/api/client.ts 唯一出口；URL 契约完整路径（§8.4）。
 */
import { apiGet } from './client'
import type { ThreadDetail } from '@/types/api'

export function fetchThread(threadId: string): Promise<ThreadDetail> {
  return apiGet<ThreadDetail>(`/api/v1/threads/${threadId}`)
}
