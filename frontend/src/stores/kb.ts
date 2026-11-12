/**
 * src/stores/kb.ts — kbStore（frontend-api.md §4 第四行；setup store，§8.2）。
 *
 * 状态：文档列表、轮询定时器（store 内部句柄）。
 * 动作：upload(file)/collectUrl(url)/remove(id)/fetchDocuments()/pollUntilSettled()/stopPolling()。
 * - 入口拒绝（413001/415001 等）以 ApiError 上抛——视图层 toast、不入表（§1 LibraryView 行）；
 * - 有非终态条目时每 5s 轮询列表（§1；POLL_INTERVAL_MS 为前端 UI 节奏代码常量）；
 * - REST 全部经 client.ts 唯一出口（§8.4；URL 契约完整路径）。
 */
import { ref } from 'vue'
import { defineStore } from 'pinia'

import { apiDelete, apiGet, apiPost } from '@/api/client'
import type { CollectUrlRequest, KbDeleteData, KbDocument } from '@/types/api'

const KB_LIST_URL = '/api/v1/kb/documents'
const KB_URLS_URL = '/api/v1/kb/urls'
/** §1 LibraryView 行：有非终态条目时每 5s 轮询（前端 UI 节奏，实现常量） */
const POLL_INTERVAL_MS = 5_000

const NON_TERMINAL: readonly KbDocument['status'][] = ['pending', 'parsing', 'embedding']

export const useKbStore = defineStore('kb', () => {
  const documents = ref<KbDocument[]>([])
  const loading = ref(false)
  let pollTimer: ReturnType<typeof setTimeout> | null = null

  function hasPendingDocs(): boolean {
    return documents.value.some((d) => NON_TERMINAL.includes(d.status))
  }

  async function fetchDocuments(): Promise<void> {
    loading.value = true
    try {
      const data = await apiGet<{ total: number; items: KbDocument[] }>(KB_LIST_URL)
      documents.value = data.items
    } finally {
      loading.value = false
    }
  }

  async function upload(file: File, title: string): Promise<KbDocument> {
    // multipart（字段 file+title，§1）；axios 自动设置 multipart boundary，勿手写 Content-Type
    const form = new FormData()
    form.append('file', file)
    form.append('title', title)
    const doc = await apiPost<KbDocument>(KB_LIST_URL, form)
    await fetchDocuments()
    schedulePollIfPending()
    return doc
  }

  async function collectUrl(url: string): Promise<KbDocument> {
    const doc = await apiPost<KbDocument>(KB_URLS_URL, { url } satisfies CollectUrlRequest)
    await fetchDocuments()
    schedulePollIfPending()
    return doc
  }

  async function remove(id: string): Promise<KbDeleteData> {
    const data = await apiDelete<KbDeleteData>(`${KB_LIST_URL}/${id}`)
    await fetchDocuments()
    return data
  }

  /** 有非终态条目 → 5s 后重拉，直至全部终态（幂等：已有定时器不重复排） */
  function schedulePollIfPending(): void {
    if (pollTimer !== null || !hasPendingDocs()) return
    pollTimer = setTimeout(async () => {
      pollTimer = null
      await fetchDocuments()
      schedulePollIfPending()
    }, POLL_INTERVAL_MS)
  }

  /** 视图挂载后调用：首拉 + 按需启动轮询 */
  async function pollUntilSettled(): Promise<void> {
    await fetchDocuments()
    schedulePollIfPending()
  }

  /** 视图卸载时停表（轮询器为 store 内部句柄，不随组件卸载自动清理） */
  function stopPolling(): void {
    if (pollTimer !== null) {
      clearTimeout(pollTimer)
      pollTimer = null
    }
  }

  return {
    documents,
    loading,
    fetchDocuments,
    upload,
    collectUrl,
    remove,
    pollUntilSettled,
    stopPolling,
  }
})
