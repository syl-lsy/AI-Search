/**
 * src/stores/threadList.ts — threadListStore（frontend-api.md §4 第二行；setup store，§8.2）。
 *
 * 状态：items/total/modeFilter + suggestPool/suggestPoolLoaded（v1.10，REQ-047 热门池
 * 会话级缓存态——store 内存、非 localStorage，刷新/关标签页后下次聚焦重新拉取）。
 * 动作：fetchThreads/refresh（新线程创建后由页面编排重拉）+ fetchSuggestPool（幂等——
 * 已 loaded 直接返回不发请求；失败静默置空、不置 loaded（下次聚焦重试）、不 toast；
 * 经 src/api/suggestions.ts 唯一出口，§8.4）。
 */
import { ref } from 'vue'
import { defineStore } from 'pinia'

import { apiGet } from '@/api/client'
import { fetchSuggestionPool } from '@/api/suggestions'
import type { ThreadListData, ThreadListItem, ThreadModeSummary } from '@/types/api'

/** api.md §2.2：limit 默认 50（上限 200），offset 默认 0 */
const DEFAULT_LIMIT = 50
const DEFAULT_OFFSET = 0

export interface FetchThreadsOptions {
  limit?: number
  offset?: number
  /** 显式传 null = 不带 mode 参数（全部）；缺省 = 当前筛选 */
  mode?: ThreadModeSummary | null
}

export const useThreadListStore = defineStore('threadList', () => {
  const items = ref<ThreadListItem[]>([])
  const total = ref(0)
  const modeFilter = ref<ThreadModeSummary | null>(null)
  const suggestPool = ref<string[]>([])
  const suggestPoolLoaded = ref(false)

  async function fetchThreads(opts: FetchThreadsOptions = {}): Promise<void> {
    const mode = opts.mode !== undefined ? opts.mode : modeFilter.value
    const params: { limit: number; offset: number; mode?: ThreadModeSummary } = {
      limit: opts.limit ?? DEFAULT_LIMIT,
      offset: opts.offset ?? DEFAULT_OFFSET,
    }
    if (mode !== null) params.mode = mode
    const data = await apiGet<ThreadListData>('/api/v1/threads', { params })
    items.value = data.items
    total.value = data.total
  }

  /** 以当前筛选重拉（新线程创建后由编排层调用刷新侧栏/列表） */
  async function refresh(): Promise<void> {
    await fetchThreads()
  }

  function setModeFilter(mode: ThreadModeSummary | null): void {
    modeFilter.value = mode
  }

  /** 首页推荐池幂等拉取（v1.10）：已 loaded 直接返回；失败静默置空、不置 loaded、不 toast */
  async function fetchSuggestPool(): Promise<void> {
    if (suggestPoolLoaded.value) return
    try {
      const data = await fetchSuggestionPool()
      suggestPool.value = data.items
      suggestPoolLoaded.value = true
    } catch {
      suggestPool.value = []
      suggestPoolLoaded.value = false
    }
  }

  return {
    items,
    total,
    modeFilter,
    suggestPool,
    suggestPoolLoaded,
    fetchThreads,
    refresh,
    setModeFilter,
    fetchSuggestPool,
  }
})
