/**
 * src/mocks/threads.ts — 线程列表静态 fixture（api.md §2.2 镜像形态；§9 fixture 优先）。
 */
import type { ThreadListData, ThreadListItem } from '@/types/api'

export const threadItems: ThreadListItem[] = [
  {
    thread_id: '7d2f0a1e-1c3b-4e5a-9d8f-0a1b2c3d4e5f',
    title: 'LangGraph 的 Send API 和 Router 的区别？',
    mode_summary: 'mixed',
    turn_count: 3,
    created_at: '2026-11-01T08:12:00Z',
    updated_at: '2026-11-01T09:40:11Z',
  },
  {
    thread_id: 'b2a4c6e8-1357-4f9b-a1c3-5d7f9b1d3f51',
    title: '对比 pgvector 与 Milvus，个人知识库场景怎么选？',
    mode_summary: 'fast',
    turn_count: 1,
    created_at: '2026-10-30T10:00:00Z',
    updated_at: '2026-10-30T10:03:21Z',
  },
  {
    thread_id: 'c9d8e7f6-2468-4acf-9e0d-1b3d5f7a9c1e',
    title: 'RAG 检索增强生成有哪些主流优化手段？',
    mode_summary: 'deep',
    turn_count: 2,
    created_at: '2026-09-15T02:30:00Z',
    updated_at: '2026-09-15T02:41:07Z',
  },
]

export const threadListFixture: ThreadListData = {
  total: threadItems.length,
  items: threadItems,
}
