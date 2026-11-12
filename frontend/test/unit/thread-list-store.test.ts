/**
 * test/unit/thread-list-store.test.ts — threadListStore 单元测试（frontend-api.md §4 第二行）。
 * REST（@/api/client.apiGet）与推荐池通道（@/api/suggestions）以 vi.mock 替身注入。
 */
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { apiGet } from '@/api/client'
import { useThreadListStore } from '@/stores/threadList'
import type { ThreadListData } from '@/types/api'

vi.mock('@/api/client', () => ({
  apiGet: vi.fn(),
}))

vi.mock('@/api/suggestions', () => ({
  fetchSuggestionPool: vi.fn(),
}))

import { fetchSuggestionPool } from '@/api/suggestions'

const mockedGet = vi.mocked(apiGet)
const mockedSuggest = vi.mocked(fetchSuggestionPool)

function listData(items: ThreadListData['items'], total = items.length): ThreadListData {
  return { total, items }
}

function item(id: string, title: string, mode: ThreadListData['items'][number]['mode_summary'] = 'fast') {
  return {
    thread_id: id,
    title,
    mode_summary: mode,
    turn_count: 1,
    created_at: '2026-11-01T08:12:00Z',
    updated_at: '2026-11-01T08:12:00Z',
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('fetchThreads / refresh（§4：列表、total、mode 筛选）', () => {
  it('首拉：写入 items/total；mode 筛选作为查询参数（null 不携带）', async () => {
    mockedGet.mockResolvedValueOnce(listData([item('t1', '问题一')]))
    const store = useThreadListStore()
    await store.fetchThreads({ mode: null })
    expect(mockedGet).toHaveBeenCalledWith('/api/v1/threads', {
      params: { limit: 50, offset: 0 },
    })
    expect(store.items).toHaveLength(1)
    expect(store.total).toBe(1)
  })

  it('mode 筛选：deep 时携带 mode 参数', async () => {
    mockedGet.mockResolvedValueOnce(listData([]))
    const store = useThreadListStore()
    await store.fetchThreads({ mode: 'deep' })
    expect(mockedGet).toHaveBeenCalledWith('/api/v1/threads', {
      params: { limit: 50, offset: 0, mode: 'deep' },
    })
  })

  it('refresh：以当前筛选重拉并覆盖列表', async () => {
    mockedGet.mockResolvedValueOnce(listData([item('t1', '旧')]))
    const store = useThreadListStore()
    store.setModeFilter('fast')
    await store.fetchThreads()
    mockedGet.mockResolvedValueOnce(listData([item('t1', '旧'), item('t2', '新')]))
    await store.refresh()
    expect(mockedGet).toHaveBeenLastCalledWith('/api/v1/threads', {
      params: { limit: 50, offset: 0, mode: 'fast' },
    })
    expect(store.items).toHaveLength(2)
  })

  it('拉取失败：抛出错误由调用方 toast（信封 message 已是中文，§2）', async () => {
    mockedGet.mockRejectedValueOnce(new Error('500001'))
    const store = useThreadListStore()
    await expect(store.fetchThreads()).rejects.toThrow('500001')
  })
})

describe('fetchSuggestPool（§4 v1.10：幂等——已加载直接返回；失败静默不置已加载）', () => {
  it('首次聚焦拉取并缓存', async () => {
    mockedSuggest.mockResolvedValueOnce({ items: ['建议一', '建议二'] })
    const store = useThreadListStore()
    await store.fetchSuggestPool()
    expect(mockedSuggest).toHaveBeenCalledTimes(1)
    expect(store.suggestPool).toEqual(['建议一', '建议二'])
    expect(store.suggestPoolLoaded).toBe(true)
  })

  it('已 loaded 后重聚焦：不发第二次请求（会话级缓存，重聚焦不强刷）', async () => {
    mockedSuggest.mockResolvedValueOnce({ items: ['建议'] })
    const store = useThreadListStore()
    await store.fetchSuggestPool()
    await store.fetchSuggestPool()
    expect(mockedSuggest).toHaveBeenCalledTimes(1)
  })

  it('拉取失败：静默置空、不置 loaded（下次聚焦重试）、不抛错', async () => {
    mockedSuggest.mockRejectedValueOnce(new Error('network'))
    const store = useThreadListStore()
    await expect(store.fetchSuggestPool()).resolves.toBeUndefined()
    expect(store.suggestPool).toEqual([])
    expect(store.suggestPoolLoaded).toBe(false)
    // 下次聚焦重试
    mockedSuggest.mockResolvedValueOnce({ items: ['恢复'] })
    await store.fetchSuggestPool()
    expect(store.suggestPool).toEqual(['恢复'])
    expect(store.suggestPoolLoaded).toBe(true)
  })

  it('空池合法（api.md §3.2：0~8 条）：loaded 置位，后续不再发请求', async () => {
    mockedSuggest.mockResolvedValueOnce({ items: [] })
    const store = useThreadListStore()
    await store.fetchSuggestPool()
    expect(store.suggestPoolLoaded).toBe(true)
    await store.fetchSuggestPool()
    expect(mockedSuggest).toHaveBeenCalledTimes(1)
  })
})
