/**
 * test/unit/thread-list-store.test.ts — threadListStore 单元测试（frontend-api.md §4 第二行）。
 *
 * 批次 4（v1.21 / REQ-058~060，fixture §9 ①②③④）：
 *   - 分页状态机：loadMore 追加 + by thread_id 去重 + hasMore 三态 + loadingMore 防重入
 *     + 代数守卫乱序响应丢弃 + refresh 重置分页态；
 *   - 三态视图/搜索：setViewFilter/setModeFilter/setSearchQuery 参数映射（active=无 archived、
 *     unread=unread=true、archived=archived=true）+ 重置分页 + q trim/截断/清空；
 *   - 管理动作五件：乐观更新 + 失败回滚 + toast（错误信封 message）+ PATCH 响应回填
 *     + toggleArchive/delete 移除条目 total-1 + 409005 toast 且行不移除 + markReadLocally。
 * REST 通道（@/api/threads）与 toast 以 vi.mock 替身注入。
 */
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/client'
import {
  deleteThread,
  listThreads,
  patchThread,
} from '@/api/threads'
import { useThreadListStore } from '@/stores/threadList'
import type { ThreadListData, ThreadListItem } from '@/types/api'

vi.mock('@/api/threads', () => ({
  listThreads: vi.fn(),
  patchThread: vi.fn(),
  deleteThread: vi.fn(),
}))

vi.mock('@/composables/toast', () => ({
  toast: vi.fn(),
}))

vi.mock('@/api/suggestions', () => ({
  fetchSuggestionPool: vi.fn(),
}))

import { fetchSuggestionPool } from '@/api/suggestions'
import { toast } from '@/composables/toast'

const mockedList = vi.mocked(listThreads)
const mockedPatch = vi.mocked(patchThread)
const mockedDelete = vi.mocked(deleteThread)
const mockedToast = vi.mocked(toast)
const mockedSuggest = vi.mocked(fetchSuggestionPool)

/** 批量造条目（updated_at 递减保证服务端 updated_at DESC 序；布尔投影可覆写） */
function makeItems(prefix: string, count: number, extra: Partial<ThreadListItem> = {}): ThreadListItem[] {
  return Array.from({ length: count }, (_, i) => ({
    thread_id: `${prefix}-${String(i + 1).padStart(3, '0')}`,
    title: `问题 ${prefix} ${i + 1}`,
    mode_summary: 'fast' as const,
    turn_count: 1,
    pinned: false,
    is_unread: false,
    archived: false,
    created_at: `2026-11-01T08:${String(i).padStart(2, '0')}:00Z`,
    updated_at: `2026-11-0${Math.max(1, 5 - i)}T08:${String(i).padStart(2, '0')}:00Z`,
    ...extra,
  }))
}

function listData(items: ThreadListItem[], total = items.length): ThreadListData {
  return { total, items }
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  mockedList.mockResolvedValue(listData([]))
})

describe('fetchThreads / refresh（§4：列表、total、mode 筛选、offset=0=替换语义）', () => {
  it('首拉：写入 items/total；active 视图与空搜索缺省不带 archived/unread/q 参数', async () => {
    mockedList.mockResolvedValueOnce(listData(makeItems('a', 2)))
    const store = useThreadListStore()
    await store.fetchThreads({ mode: null })
    expect(mockedList).toHaveBeenCalledWith({ limit: 50, offset: 0 })
    expect(store.items).toHaveLength(2)
    expect(store.total).toBe(2)
    expect(store.hasMore).toBe(false)
  })

  it('mode 筛选：deep 时携带 mode 参数（null 不携带）', async () => {
    const store = useThreadListStore()
    await store.fetchThreads({ mode: 'deep' })
    expect(mockedList).toHaveBeenCalledWith({ limit: 50, offset: 0, mode: 'deep' })
  })

  it('fetchThreads offset=0 为替换语义：二次首拉覆盖而非追加', async () => {
    const store = useThreadListStore()
    mockedList.mockResolvedValueOnce(listData(makeItems('a', 2)))
    await store.fetchThreads({ mode: null })
    mockedList.mockResolvedValueOnce(listData(makeItems('b', 1)))
    await store.fetchThreads({ mode: null })
    expect(store.items.map((i) => i.thread_id)).toEqual(['b-001'])
  })

  it('refresh：重置分页态（loadMore 在途/失败标记清除）重拉首屏 + best-effort 自愈侧栏 recent', async () => {
    const store = useThreadListStore()
    mockedList.mockResolvedValueOnce(listData(makeItems('a', 50), 120))
    await store.fetchThreads({ mode: null })
    mockedList.mockResolvedValueOnce(listData(makeItems('b', 20), 120))
    await store.loadMore()
    expect(store.items).toHaveLength(70)
    // refresh 并行两请求：首屏（limit 50）+ 侧栏 recent（limit 10，失败静默）
    mockedList.mockImplementation(async (query?: { limit?: number; offset?: number }) =>
      (query?.limit ?? 50) === 10 ? listData(makeItems('r', 2), 2) : listData(makeItems('c', 50), 120))
    await store.refresh()
    expect(mockedList).toHaveBeenCalledWith({ limit: 50, offset: 0 })
    expect(mockedList).toHaveBeenCalledWith({ limit: 10, offset: 0 })
    expect(store.items.map((i) => i.thread_id)).toEqual(makeItems('c', 50).map((i) => i.thread_id))
    expect(store.recent.map((i) => i.thread_id)).toEqual(['r-001', 'r-002'])
    expect(store.loadingMore).toBe(false)
    expect(store.loadMoreFailed).toBe(false)
  })

  it('拉取失败：抛出错误由调用方 toast（信封 message 已是中文，§2）', async () => {
    mockedList.mockRejectedValueOnce(new ApiError('500001', '内部错误'))
    const store = useThreadListStore()
    await expect(store.fetchThreads()).rejects.toThrow('内部错误')
  })

  it('首屏在途时 loadMore 被吞（P2-1：不产生旧视图 append/视图态失配）', async () => {
    const store = useThreadListStore()
    let resolveFirst!: (v: ThreadListData) => void
    mockedList.mockImplementationOnce(
      () => new Promise<ThreadListData>((resolve) => { resolveFirst = resolve }),
    )
    const first = store.fetchThreads({ mode: null })
    expect(store.fetchingFirstPage).toBe(true)
    // 在途首屏期间触底：loadMore 直接吞掉（不发第二次请求、loadingMore 不置位）
    await store.loadMore()
    expect(mockedList).toHaveBeenCalledTimes(1)
    expect(store.loadingMore).toBe(false)
    resolveFirst(listData(makeItems('a', 50), 120))
    await first
    expect(store.fetchingFirstPage).toBe(false)
    expect(store.items).toHaveLength(50)
    // 首屏落定后 loadMore 恢复可用
    mockedList.mockResolvedValueOnce(listData(makeItems('b', 20), 120))
    await store.loadMore()
    expect(store.items).toHaveLength(70)
  })

  it('loadMore 失败晚于一次成功视图切换：不污染新视图失败态（P2-1）', async () => {
    const store = useThreadListStore()
    mockedList.mockResolvedValueOnce(listData(makeItems('a', 50), 120))
    await store.fetchThreads({ mode: null })
    let rejectMore!: (e: unknown) => void
    mockedList.mockImplementationOnce(
      () => new Promise<ThreadListData>((_, reject) => { rejectMore = reject }),
    )
    const more = store.loadMore()
    // 在途 loadMore 期间切视图（首拉成功换血 items）
    mockedList.mockResolvedValueOnce(listData(makeItems('arch', 3), 3))
    await store.setViewFilter('archived')
    // 旧 loadMore 此时才失败——失败态归属旧视图，不得写入新视图
    rejectMore(new ApiError('network_error', '网络异常，请稍后重试'))
    await more
    expect(store.loadMoreFailed).toBe(false)
    expect(store.items.map((i) => i.thread_id)).toEqual(['arch-001', 'arch-002', 'arch-003'])
  })
})

describe('分页状态机（§4 v1.21 / REQ-058：loadMore/hasMore/loadingMore/代数守卫）', () => {
  it('loadMore：offset=items.length 追加下一页；hasMore 随累积翻转（三态）', async () => {
    const store = useThreadListStore()
    const page1 = makeItems('a', 50)
    mockedList.mockResolvedValueOnce(listData(page1, 120))
    await store.fetchThreads({ mode: null })
    expect(store.hasMore).toBe(true) // 50 < 120
    const page2 = makeItems('b', 50)
    mockedList.mockResolvedValueOnce(listData(page2, 120))
    await store.loadMore()
    expect(mockedList).toHaveBeenLastCalledWith({ limit: 50, offset: 50 })
    expect(store.items).toHaveLength(100) // 累积 append
    expect(store.hasMore).toBe(true)
    const page3 = makeItems('c', 20)
    mockedList.mockResolvedValueOnce(listData(page3, 120))
    await store.loadMore()
    expect(mockedList).toHaveBeenLastCalledWith({ limit: 50, offset: 100 })
    expect(store.items).toHaveLength(120)
    expect(store.hasMore).toBe(false) // 120 == 120：末页收口
    mockedList.mockClear()
    await store.loadMore()
    expect(mockedList).not.toHaveBeenCalled() // hasMore=false 不再发请求
  })

  it('loadMore：by thread_id 去重（服务端窗口漂移重叠条目不重复插入）', async () => {
    const store = useThreadListStore()
    const page1 = makeItems('a', 2)
    mockedList.mockResolvedValueOnce(listData(page1, 3))
    await store.fetchThreads({ mode: null })
    // 第二页返回与首页重叠的 a-002 + 新条目 b-001
    mockedList.mockResolvedValueOnce(
      listData([page1[1]!, ...makeItems('b', 1)], 3),
    )
    await store.loadMore()
    const ids = store.items.map((i) => i.thread_id)
    expect(ids).toEqual(['a-001', 'a-002', 'b-001']) // 重叠的 a-002 不重复
    expect(store.total).toBe(3)
  })

  it('loadingMore 防重入：并发触发只发一次请求', async () => {
    const store = useThreadListStore()
    mockedList.mockResolvedValueOnce(listData(makeItems('a', 50), 120))
    await store.fetchThreads({ mode: null })
    let resolveMore!: (v: ThreadListData) => void
    mockedList.mockImplementationOnce(
      () => new Promise<ThreadListData>((resolve) => { resolveMore = resolve }),
    )
    const p1 = store.loadMore()
    const p2 = store.loadMore()
    expect(store.loadingMore).toBe(true)
    resolveMore(listData(makeItems('b', 50), 120))
    await Promise.all([p1, p2])
    expect(mockedList).toHaveBeenCalledTimes(2) // 首屏 1 次 + loadMore 1 次（第二次被防重入吞掉）
    expect(store.loadingMore).toBe(false)
  })

  it('代数守卫：loadMore 在途时切视图首拉，慢到的旧页响应被丢弃（不覆盖新列表）', async () => {
    const store = useThreadListStore()
    mockedList.mockResolvedValueOnce(listData(makeItems('a', 50), 120))
    await store.fetchThreads({ mode: null })
    let resolveMore!: (v: ThreadListData) => void
    mockedList.mockImplementationOnce(
      () => new Promise<ThreadListData>((resolve) => { resolveMore = resolve }),
    )
    const more = store.loadMore()
    // 切换视图（archived）触发首拉，先返回
    mockedList.mockResolvedValueOnce(listData(makeItems('arch', 3), 3))
    await store.setViewFilter('archived')
    expect(store.items.map((i) => i.thread_id)).toEqual(['arch-001', 'arch-002', 'arch-003'])
    // 旧 loadMore 响应慢到——代数已过期，不得覆盖 archived 视图列表
    resolveMore(listData(makeItems('b', 50), 120))
    await more
    expect(store.items.map((i) => i.thread_id)).toEqual(['arch-001', 'arch-002', 'arch-003'])
  })

  it('loadMore 失败：loadMoreFailed 置位、items 不动；重试成功恢复', async () => {
    const store = useThreadListStore()
    mockedList.mockResolvedValueOnce(listData(makeItems('a', 50), 120))
    await store.fetchThreads({ mode: null })
    mockedList.mockRejectedValueOnce(new ApiError('network_error', '网络异常，请稍后重试'))
    await store.loadMore()
    expect(store.loadMoreFailed).toBe(true)
    expect(store.items).toHaveLength(50)
    expect(store.loadingMore).toBe(false)
    mockedList.mockResolvedValueOnce(listData(makeItems('b', 50), 120))
    await store.loadMore()
    expect(store.loadMoreFailed).toBe(false)
    expect(store.items).toHaveLength(100)
  })
})

describe('三态视图与搜索（§4 v1.21：setViewFilter/setModeFilter/setSearchQuery——重置分页+首拉）', () => {
  it('参数映射：active=无 archived/unread；unread=unread=true；archived=archived=true', async () => {
    const store = useThreadListStore()
    await store.setViewFilter('unread')
    expect(mockedList).toHaveBeenLastCalledWith(expect.objectContaining({ unread: true }))
    expect(mockedList).toHaveBeenLastCalledWith(expect.not.objectContaining({ archived: expect.anything() }))
    await store.setViewFilter('archived')
    expect(mockedList).toHaveBeenLastCalledWith(expect.objectContaining({ archived: true }))
    expect(mockedList).toHaveBeenLastCalledWith(expect.not.objectContaining({ unread: expect.anything() }))
    await store.setViewFilter('active')
    expect(mockedList).toHaveBeenLastCalledWith(expect.not.objectContaining({ archived: expect.anything() }))
    expect(mockedList).toHaveBeenLastCalledWith(expect.not.objectContaining({ unread: expect.anything() }))
  })

  it('setViewFilter 值不变时不重复拉取', async () => {
    const store = useThreadListStore()
    await store.setViewFilter('active')
    mockedList.mockClear()
    await store.setViewFilter('active')
    expect(mockedList).not.toHaveBeenCalled()
  })

  it('setModeFilter/setSearchQuery 均重置分页并首拉（q 与 mode/view AND 叠加）', async () => {
    const store = useThreadListStore()
    await store.setViewFilter('unread')
    await store.setModeFilter('deep')
    expect(mockedList).toHaveBeenLastCalledWith(
      expect.objectContaining({ mode: 'deep', unread: true, offset: 0 }),
    )
    await store.setSearchQuery('LangGraph')
    expect(mockedList).toHaveBeenLastCalledWith(
      expect.objectContaining({ q: 'LangGraph', mode: 'deep', unread: true, offset: 0 }),
    )
  })

  it('q：trim 后空串=不过滤（清空回全量）；超 200 字符前端截断（§9 fixture ②）', async () => {
    const store = useThreadListStore()
    await store.setSearchQuery('   LangGraph   ')
    expect(mockedList).toHaveBeenLastCalledWith(expect.objectContaining({ q: 'LangGraph' }))
    await store.setSearchQuery('   ')
    expect(mockedList).toHaveBeenLastCalledWith(expect.not.objectContaining({ q: expect.anything() }))
    const long = 'x'.repeat(260)
    await store.setSearchQuery(long)
    expect(mockedList).toHaveBeenLastCalledWith(expect.objectContaining({ q: 'x'.repeat(200) }))
  })
})

describe('管理动作五件（§4 v1.21 / REQ-059：乐观更新+失败回滚+响应回填）', () => {
  async function seedActive(): Promise<ReturnType<typeof useThreadListStore>> {
    const store = useThreadListStore()
    mockedList.mockResolvedValueOnce(
      listData(
        makeItems('a', 2, { pinned: false, is_unread: false }),
        2,
      ),
    )
    await store.fetchThreads({ mode: null })
    return store
  }

  it('renameThread：乐观改 title → PATCH 响应回填；排序不变（updated_at 不随动）', async () => {
    const store = await seedActive()
    mockedPatch.mockResolvedValueOnce({
      ...store.items[0]!,
      title: '新标题',
    })
    await store.renameThread('a-001', '  新标题  ')
    expect(mockedPatch).toHaveBeenCalledWith('a-001', { title: '新标题' })
    expect(store.items[0]!.title).toBe('新标题')
    expect(mockedToast).not.toHaveBeenCalled()
  })

  it('renameThread：空串/纯空白/超 120 字符前端拦截（不发 PATCH）', async () => {
    const store = await seedActive()
    await store.renameThread('a-001', '   ')
    await store.renameThread('a-001', 'x'.repeat(121))
    expect(mockedPatch).not.toHaveBeenCalled()
  })

  it('renameThread 失败：title 回滚 + toast 服务端 message', async () => {
    const store = await seedActive()
    mockedPatch.mockRejectedValueOnce(new ApiError('400001', '标题长度超出限制'))
    await store.renameThread('a-001', '新标题')
    expect(store.items[0]!.title).toBe('问题 a 1')
    expect(mockedToast).toHaveBeenCalledWith('标题长度超出限制')
  })

  it('togglePin：乐观翻转 + 置顶行重排到列表顶部；响应回填', async () => {
    const store = await seedActive()
    // a-001 在前（updated_at DESC）。置顶 a-002 → a-002 乐观升到首位
    mockedPatch.mockResolvedValueOnce({ ...store.items[1]!, pinned: true })
    await store.togglePin('a-002')
    expect(mockedPatch).toHaveBeenCalledWith('a-002', { pinned: true })
    expect(store.items.map((i) => i.pinned)).toEqual([true, false])
    expect(store.items[0]!.thread_id).toBe('a-002')
    // 取消置顶 → 恢复 updated_at 序（稳定排序：同为 unpinned 时原相对序保持）
    mockedPatch.mockResolvedValueOnce({ ...store.items[0]!, pinned: false })
    await store.togglePin('a-002')
    expect(store.items[0]!.thread_id).toBe('a-001')
  })

  it('togglePin 失败：pinned 与顺序一并回滚 + toast', async () => {
    const store = await seedActive()
    mockedPatch.mockRejectedValueOnce(new ApiError('404001', '线程不存在'))
    await store.togglePin('a-002')
    expect(store.items[0]!.thread_id).toBe('a-001')
    expect(store.items.map((i) => i.pinned)).toEqual([false, false])
    expect(mockedToast).toHaveBeenCalledWith('线程不存在')
  })

  it('markUnread：乐观 is_unread=true → 响应回填；失败回滚', async () => {
    const store = await seedActive()
    mockedPatch.mockResolvedValueOnce({ ...store.items[0]!, is_unread: true })
    await store.markUnread('a-001')
    expect(mockedPatch).toHaveBeenCalledWith('a-001', { is_unread: true })
    expect(store.items[0]!.is_unread).toBe(true)
    mockedPatch.mockRejectedValueOnce(new ApiError('400001', '请求参数错误'))
    await store.markUnread('a-002')
    expect(store.items[1]!.is_unread).toBe(false)
    expect(mockedToast).toHaveBeenCalledWith('请求参数错误')
  })

  it('markRead（v1.24 菜单未读项）：乐观 is_unread=false → PATCH {is_unread:false} 响应回填；失败回滚 + toast', async () => {
    const store = useThreadListStore()
    mockedList.mockResolvedValueOnce(listData(makeItems('u', 2, { is_unread: true }), 2))
    await store.fetchThreads({ mode: null })
    mockedPatch.mockResolvedValueOnce({ ...store.items[0]!, is_unread: false })
    await store.markRead('u-001')
    expect(mockedPatch).toHaveBeenCalledWith('u-001', { is_unread: false })
    expect(store.items[0]!.is_unread).toBe(false)
    expect(mockedToast).not.toHaveBeenCalled()
    // 失败：乐观翻转回滚（未读点保留）+ toast 服务端 message（同族口径）
    mockedPatch.mockRejectedValueOnce(new ApiError('400001', '请求参数错误'))
    await store.markRead('u-002')
    expect(store.items[1]!.is_unread).toBe(true)
    expect(mockedToast).toHaveBeenCalledWith('请求参数错误')
  })

  it('markRead：侧栏 recent 双列表同步自愈（is_unread 双向）', async () => {
    const store = useThreadListStore()
    mockedList.mockResolvedValueOnce(listData(makeItems('r', 2, { is_unread: true }), 2))
    await store.fetchRecent()
    mockedPatch.mockResolvedValueOnce({ ...store.recent[0]!, is_unread: false })
    await store.markRead('r-001')
    expect(store.recent[0]!.is_unread).toBe(false)
    expect(store.recent[1]!.is_unread).toBe(true)
  })

  it('applyPatchResponse 回填拷贝式写入（评审 P3）：items 与 recent 元素引用独立——同一响应对象不得双列表共享', async () => {
    const store = useThreadListStore()
    // 同一线程同时进 items（会话页）与 recent（侧栏）：统一 thread_id 便于双列表回填同一条
    mockedList.mockImplementation(async (query?: { limit?: number; offset?: number }) =>
      (query?.limit ?? 50) === 10 ? listData(makeItems('r', 1), 1) : listData(makeItems('a', 1), 1))
    await Promise.all([store.fetchThreads({ mode: null }), store.fetchRecent()])
    store.items[0] = { ...store.items[0]!, thread_id: 'r-001' }
    mockedPatch.mockResolvedValueOnce({ ...store.items[0]!, pinned: true })
    await store.togglePin('r-001')
    // 回填后字段一致，但两列表元素必须是独立拷贝（未来任何直接属性写不得穿透双列表）
    expect(store.items[0]!.pinned).toBe(true)
    expect(store.recent[0]!.pinned).toBe(true)
    expect(store.items[0]!).not.toBe(store.recent[0]!)
  })

  it('toggleArchive（活跃视图）：乐观移出当前视图 + total-1；失败回滚插回原位', async () => {
    const store = await seedActive()
    mockedPatch.mockResolvedValueOnce({ ...store.items[1]!, archived: true })
    await store.toggleArchive('a-002')
    expect(mockedPatch).toHaveBeenCalledWith('a-002', { archived: true })
    expect(store.items.map((i) => i.thread_id)).toEqual(['a-001'])
    expect(store.total).toBe(1)
    mockedPatch.mockRejectedValueOnce(new ApiError('400001', '请求参数错误'))
    await store.toggleArchive('a-001')
    expect(store.items.map((i) => i.thread_id)).toEqual(['a-001'])
    expect(store.total).toBe(1)
    expect(mockedToast).toHaveBeenCalledWith('请求参数错误')
  })

  it('deleteThread：乐观移除 + total-1；失败（409005）toast 服务端 message 且行保留', async () => {
    const store = await seedActive()
    mockedDelete.mockResolvedValueOnce({ deleted: true })
    await store.deleteThread('a-002')
    expect(mockedDelete).toHaveBeenCalledWith('a-002')
    expect(store.items.map((i) => i.thread_id)).toEqual(['a-001'])
    expect(store.total).toBe(1)
    // 409005：行不移除
    mockedDelete.mockRejectedValueOnce(new ApiError('409005', '线程有进行中轮次，不可删除'))
    await store.deleteThread('a-001')
    expect(store.items).toHaveLength(1)
    expect(store.total).toBe(1)
    expect(mockedToast).toHaveBeenCalledWith('线程有进行中轮次，不可删除')
  })

  it('markReadLocally：打开详情后本地清未读点（零网络；服务端已随 GET 详情置已读）', async () => {
    const store = useThreadListStore()
    mockedList.mockResolvedValueOnce(
      listData(makeItems('a', 2, { is_unread: true })),
    )
    await store.fetchThreads({ mode: null })
    store.markReadLocally('a-001')
    expect(store.items[0]!.is_unread).toBe(false)
    expect(store.items[1]!.is_unread).toBe(true)
    expect(mockedPatch).not.toHaveBeenCalled()
    // 未知 id 静默
    store.markReadLocally('unknown')
  })

  it('目标不在当前过滤 items 时 total 不动（P3-1：归档视图删活跃线程——recent 剔除、total 不减）', async () => {
    const store = useThreadListStore()
    // 当前视图 = 归档集（total=3），目标 = 活跃线程（只在 recent）
    mockedList.mockResolvedValue(listData(makeItems('arch', 3, { archived: true }), 3))
    await store.fetchThreads({ mode: null })
    mockedList.mockResolvedValueOnce(listData(makeItems('r', 2), 2))
    await store.fetchRecent()
    mockedDelete.mockResolvedValueOnce({ deleted: true })
    await store.deleteThread('r-001')
    // recent 剔除；total 保持 3（hasMore 口径不漂移）
    expect(store.recent.map((i) => i.thread_id)).toEqual(['r-002'])
    expect(store.total).toBe(3)
    // 对偶：toggleArchive 目标在 items 时照常 total-1
    mockedPatch.mockResolvedValueOnce({ ...store.items[0]!, archived: true })
    await store.toggleArchive('arch-001')
    expect(store.total).toBe(2)
  })

  it('动作目标不在当前列表（如按 id 直呼）时静默返回，不发 PATCH/DELETE', async () => {
    const store = await seedActive()
    await store.renameThread('ghost', 'x')
    await store.togglePin('ghost')
    await store.markUnread('ghost')
    await store.toggleArchive('ghost')
    await store.deleteThread('ghost')
    expect(mockedPatch).not.toHaveBeenCalled()
    expect(mockedDelete).not.toHaveBeenCalled()
  })
})

describe('侧栏 recent 独立投影（P2-2：fetchRecent 与管理动作自愈）', () => {
  it('fetchRecent：limit=10 缺省活跃集（无 archived/unread/q 参数）→ 写 recent，不动 items/total/hasMore', async () => {
    const store = useThreadListStore()
    mockedList.mockResolvedValueOnce(listData(makeItems('a', 50), 120))
    await store.fetchThreads({ mode: null })
    const recentFixture = makeItems('r', 2, { is_unread: true })
    mockedList.mockResolvedValueOnce(listData(recentFixture, 2))
    await store.fetchRecent()
    expect(mockedList).toHaveBeenLastCalledWith({ limit: 10, offset: 0 })
    expect(store.recent.map((i) => i.thread_id)).toEqual(['r-001', 'r-002'])
    // 分页列表不受影响
    expect(store.items).toHaveLength(50)
    expect(store.total).toBe(120)
    expect(store.hasMore).toBe(true)
  })

  it('recent 不受 viewFilter/searchQuery 影响（侧栏数据污染回归）', async () => {
    const store = useThreadListStore()
    mockedList.mockResolvedValueOnce(listData(makeItems('r', 2), 2))
    await store.fetchRecent()
    // 切归档视图 + 搜索（items 换血）→ recent 保持侧栏最近集
    mockedList.mockResolvedValue(listData(makeItems('arch', 3), 3))
    await store.setViewFilter('archived')
    await store.setSearchQuery('LangGraph')
    expect(store.recent.map((i) => i.thread_id)).toEqual(['r-001', 'r-002'])
    expect(store.items.map((i) => i.thread_id)).toEqual(['arch-001', 'arch-002', 'arch-003'])
  })

  it('renameThread 成功 → recent 同步改名；失败 → recent 回滚', async () => {
    const store = useThreadListStore()
    mockedList.mockResolvedValueOnce(listData(makeItems('r', 2), 2))
    await store.fetchRecent()
    mockedPatch.mockResolvedValueOnce({ ...store.recent[0]!, title: '新标题' })
    await store.renameThread('r-001', '新标题')
    expect(store.recent[0]!.title).toBe('新标题')
    mockedPatch.mockRejectedValueOnce(new ApiError('400001', '请求参数错误'))
    await store.renameThread('r-002', '失败名')
    expect(store.recent[1]!.title).toBe('问题 r 2')
  })

  it('deleteThread/toggleArchive 成功 → recent 剔除；失败回滚', async () => {
    const store = useThreadListStore()
    mockedList.mockResolvedValueOnce(listData(makeItems('r', 3), 3))
    await store.fetchRecent()
    mockedDelete.mockResolvedValueOnce({ deleted: true })
    await store.deleteThread('r-002')
    expect(store.recent.map((i) => i.thread_id)).toEqual(['r-001', 'r-003'])
    mockedPatch.mockRejectedValueOnce(new ApiError('409005', '线程有进行中轮次，不可删除'))
    await store.toggleArchive('r-001')
    expect(store.recent.map((i) => i.thread_id)).toEqual(['r-001', 'r-003'])
    expect(mockedToast).toHaveBeenCalledWith('线程有进行中轮次，不可删除')
  })

  it('togglePin 成功 → recent 置顶重排；markReadLocally → recent 未读点同步清除', async () => {
    const store = useThreadListStore()
    mockedList.mockResolvedValueOnce(listData(makeItems('r', 2, { is_unread: true }), 2))
    await store.fetchRecent()
    mockedPatch.mockResolvedValueOnce({ ...store.recent[1]!, pinned: true })
    await store.togglePin('r-002')
    expect(store.recent[0]!.thread_id).toBe('r-002')
    store.markReadLocally('r-001')
    expect(store.recent[0]!.is_unread).toBe(true)
    store.markReadLocally('r-002')
    expect(store.recent[1]!.is_unread).toBe(false)
  })
})

describe('三态 setter 失败回滚（P2-4：网络失败时选中态与列表一致回退）', () => {
  it.each([
    ['setViewFilter', (s: ReturnType<typeof useThreadListStore>) => s.setViewFilter('archived')],
    ['setModeFilter', (s: ReturnType<typeof useThreadListStore>) => s.setModeFilter('deep')],
    ['setSearchQuery', (s: ReturnType<typeof useThreadListStore>) => s.setSearchQuery('LangGraph')],
  ] as const)('%s 首拉失败 → 状态回滚并重新抛出（由调用方 toast）', async (_name, run) => {
    const store = useThreadListStore()
    mockedList.mockResolvedValueOnce(listData(makeItems('a', 1)))
    await store.fetchThreads({ mode: null })
    mockedList.mockRejectedValueOnce(new ApiError('network_error', '网络异常，请稍后重试'))
    await expect(run(store)).rejects.toThrow('网络异常，请稍后重试')
    expect(store.viewFilter).toBe('active')
    expect(store.modeFilter).toBeNull()
    expect(store.searchQuery).toBe('')
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

describe('批量选择（v1.23：selectedIds——视图切换/搜索重置清空）', () => {
  it('toggleSelect：选中/取消翻转；setSelection 整组替换；clearSelection 清空', async () => {
    const store = useThreadListStore()
    expect(store.selectedIds.size).toBe(0)
    store.toggleSelect('a-001')
    store.toggleSelect('a-002')
    expect([...store.selectedIds]).toEqual(['a-001', 'a-002'])
    store.toggleSelect('a-001')
    expect([...store.selectedIds]).toEqual(['a-002'])
    store.setSelection(['b-001', 'b-002'])
    expect([...store.selectedIds]).toEqual(['b-001', 'b-002'])
    store.clearSelection()
    expect(store.selectedIds.size).toBe(0)
  })

  it('三态视图切换 / 模式筛选 / 搜索重置（首拉收口）时清空选择', async () => {
    const store = useThreadListStore()
    mockedList.mockResolvedValueOnce(listData(makeItems('a', 3)))
    await store.fetchThreads({ mode: null })
    store.setSelection(['a-001', 'a-002'])
    await store.setViewFilter('archived')
    expect(store.selectedIds.size).toBe(0)
    store.setSelection(['a-001'])
    await store.setModeFilter('deep')
    expect(store.selectedIds.size).toBe(0)
    store.setSelection(['a-001'])
    await store.setSearchQuery('LangGraph')
    expect(store.selectedIds.size).toBe(0)
  })

  it('重新进入会话页（fetchThreads offset=0 替换语义）同样清空选择', async () => {
    const store = useThreadListStore()
    mockedList.mockResolvedValueOnce(listData(makeItems('a', 2)))
    await store.fetchThreads({ mode: null })
    store.setSelection(['a-001'])
    mockedList.mockResolvedValueOnce(listData(makeItems('a', 2)))
    await store.fetchThreads({ mode: null })
    expect(store.selectedIds.size).toBe(0)
  })

  it('勾选后经单线程菜单路径删除/归档（乐观移除）→ selectedIds 同步剔除该 id（评审 P2：第三态收敛——否则批量计数虚高、runBatch 对幽灵 id 静默计入失败）', async () => {
    const store = useThreadListStore()
    mockedList.mockResolvedValueOnce(listData(makeItems('a', 3)))
    await store.fetchThreads({ mode: null })
    store.setSelection(['a-001', 'a-002'])
    mockedDelete.mockResolvedValueOnce({ deleted: true })
    await store.deleteThread('a-001')
    expect(store.selectedIds.has('a-001')).toBe(false)
    expect([...store.selectedIds]).toEqual(['a-002'])
    // 归档同族（乐观移除路径）
    mockedPatch.mockResolvedValueOnce({ ...store.items[0]!, archived: true })
    await store.toggleArchive('a-002')
    expect(store.selectedIds.size).toBe(0)
  })
})

describe('批量动作三件（v1.23：batchDelete/batchToggleArchive/batchMarkUnread——循环既有单线程动作，部分失败汇总 toast）', () => {
  async function seed(n = 3): Promise<ReturnType<typeof useThreadListStore>> {
    const store = useThreadListStore()
    mockedList.mockResolvedValueOnce(listData(makeItems('a', n)))
    await store.fetchThreads({ mode: null })
    return store
  }

  it('batchDelete 全成功：逐个调用既有 deleteThread、条目移除 total 收口、选择清空、零 toast', async () => {
    const store = await seed(3)
    store.setSelection(['a-001', 'a-002'])
    mockedDelete.mockResolvedValue({ deleted: true })
    await store.batchDelete(['a-001', 'a-002'])
    expect(mockedDelete).toHaveBeenCalledWith('a-001')
    expect(mockedDelete).toHaveBeenCalledWith('a-002')
    expect(store.items.map((i) => i.thread_id)).toEqual(['a-003'])
    expect(store.total).toBe(1)
    expect(store.selectedIds.size).toBe(0)
    expect(mockedToast).not.toHaveBeenCalled()
  })

  it('batchDelete 部分失败（409005）：失败行回滚保留、汇总 toast 含「成功/失败」与 409005 服务端 message、选择清空', async () => {
    const store = await seed(3)
    store.setSelection(['a-001', 'a-002'])
    mockedDelete.mockImplementation(async (id: string) => {
      if (id === 'a-002') throw new ApiError('409005', '线程有进行中轮次，不可删除')
      return { deleted: true }
    })
    await store.batchDelete(['a-001', 'a-002'])
    expect(store.items.map((i) => i.thread_id)).toEqual(['a-002', 'a-003'])
    expect(store.total).toBe(2)
    expect(store.selectedIds.size).toBe(0)
    expect(mockedToast).toHaveBeenCalledTimes(1)
    const msg = mockedToast.mock.calls[0]![0] as string
    expect(msg).toContain('成功 1')
    expect(msg).toContain('失败 1')
    expect(msg).toContain('线程有进行中轮次，不可删除')
  })

  it('batchToggleArchive：活跃视图=逐个归档移出列表；部分失败回滚保留', async () => {
    const store = await seed(3)
    store.setSelection(['a-001', 'a-002'])
    mockedPatch.mockImplementation(async (id: string) => {
      if (id === 'a-002') throw new ApiError('400001', '请求参数错误')
      return { ...makeItems('a', 3).find((i) => i.thread_id === id)!, archived: true }
    })
    await store.batchToggleArchive(['a-001', 'a-002'])
    expect(mockedPatch).toHaveBeenCalledWith('a-001', { archived: true })
    expect(store.items.map((i) => i.thread_id)).toEqual(['a-002', 'a-003'])
    expect(mockedToast).toHaveBeenCalledTimes(1)
    expect(mockedToast.mock.calls[0]![0]).toContain('成功 1')
    expect(store.selectedIds.size).toBe(0)
  })

  it('batchToggleArchive 归档视图=取消归档语义（随条目 archived 翻转，与行级菜单同款）', async () => {
    const store = useThreadListStore()
    mockedList.mockResolvedValueOnce(listData(makeItems('arch', 2, { archived: true })))
    await store.setViewFilter('archived')
    store.setSelection(['arch-001', 'arch-002'])
    mockedPatch.mockResolvedValue({ ...makeItems('arch', 1, { archived: false })[0]!, archived: false })
    await store.batchToggleArchive(['arch-001', 'arch-002'])
    expect(mockedPatch).toHaveBeenCalledWith('arch-001', { archived: false })
    expect(mockedPatch).toHaveBeenCalledWith('arch-002', { archived: false })
    expect(store.items).toHaveLength(0)
    expect(mockedToast).not.toHaveBeenCalled()
  })

  it('batchMarkUnread：逐个 is_unread=true（行保留在列表）；部分失败汇总', async () => {
    const store = await seed(2)
    store.setSelection(['a-001', 'a-002'])
    mockedPatch.mockImplementation(async (id: string) => {
      if (id === 'a-002') throw new ApiError('409005', '线程有进行中轮次，不可标记')
      return { ...store.items[0]!, thread_id: id, is_unread: true }
    })
    await store.batchMarkUnread(['a-001', 'a-002'])
    expect(mockedPatch).toHaveBeenCalledWith('a-001', { is_unread: true })
    expect(store.items[0]!.is_unread).toBe(true)
    expect(store.items).toHaveLength(2) // 行不移除
    expect(mockedToast).toHaveBeenCalledTimes(1)
    expect(mockedToast.mock.calls[0]![0]).toContain('失败 1')
    expect(store.selectedIds.size).toBe(0)
  })

  it('单动作静默选项：silent 不 toast 且 onError 透传服务端 message；缺省行为不变（toast + 布尔返回）', async () => {
    const store = await seed(2)
    mockedDelete.mockRejectedValueOnce(new ApiError('409005', '线程有进行中轮次，不可删除'))
    const errors: string[] = []
    const okSilent = await store.deleteThread('a-001', { silent: true, onError: (m) => errors.push(m) })
    expect(okSilent).toBe(false)
    expect(errors).toEqual(['线程有进行中轮次，不可删除'])
    expect(mockedToast).not.toHaveBeenCalled()
    expect(store.items).toHaveLength(2) // 回滚
    mockedDelete.mockResolvedValueOnce({ deleted: true })
    const okDefault = await store.deleteThread('a-001')
    expect(okDefault).toBe(true)
    // 缺省（非 silent）失败：toast 照旧 + false 返回（目标换仍在列表的 a-002）
    mockedDelete.mockRejectedValueOnce(new ApiError('409005', '线程有进行中轮次，不可删除'))
    const okFail = await store.deleteThread('a-002')
    expect(okFail).toBe(false)
    expect(mockedToast).toHaveBeenCalledWith('线程有进行中轮次，不可删除')
    expect(store.items.map((i) => i.thread_id)).toEqual(['a-002']) // 回滚保留
  })
})

describe('响应数据快照化（v1.25 潜伏缺陷显性化修复：store 不得与数据源共享可变引用）', () => {
  it('fetchRecent 后 recent 与响应数组引用隔离：乐观元素替换/排序不污染数据源对象', async () => {
    const store = useThreadListStore()
    const source = makeItems('r', 2)
    mockedList.mockResolvedValueOnce(listData(source, 2))
    await store.fetchRecent()
    // 乐观元素替换（togglePin 路径同款：recent.value[idx] = {...}）不得穿透到原数组
    mockedPatch.mockResolvedValueOnce({ ...store.recent[0]!, pinned: true })
    await store.togglePin('r-001')
    expect(source[0]!.pinned).toBe(false)
    expect(store.recent[0]!.pinned).toBe(true)
  })

  it('fetchThreads 后 items 与响应数组引用隔离（同一污染面）', async () => {
    const store = useThreadListStore()
    const source = makeItems('a', 2)
    mockedList.mockResolvedValueOnce(listData(source, 2))
    await store.fetchThreads({ mode: null })
    mockedPatch.mockResolvedValueOnce({ ...store.items[0]!, pinned: true })
    await store.togglePin('a-001')
    expect(source[0]!.pinned).toBe(false)
    expect(store.items[0]!.pinned).toBe(true)
  })
})

describe('recentFilter（v1.24 侧栏筛选弹层）：recent 按 mode_summary 本地过滤（store 态共享）', () => {
  it('缺省 null=recent 全量；setRecentFilter("fast") → recentFiltered 仅 fast；setRecentFilter(null) 复位', async () => {
    const store = useThreadListStore()
    mockedList.mockResolvedValueOnce(
      listData([
        ...makeItems('f', 1, { mode_summary: 'fast' }),
        ...makeItems('d', 1, { mode_summary: 'deep' }),
        ...makeItems('m', 1, { mode_summary: 'mixed' }),
      ], 3),
    )
    await store.fetchRecent()
    expect(store.recentFilter).toBeNull()
    expect(store.recentFiltered).toHaveLength(3)
    store.setRecentFilter('fast')
    expect(store.recentFilter).toBe('fast')
    expect(store.recentFiltered.map((i) => i.mode_summary)).toEqual(['fast'])
    store.setRecentFilter('deep')
    expect(store.recentFiltered.map((i) => i.mode_summary)).toEqual(['deep'])
    store.setRecentFilter(null)
    expect(store.recentFiltered).toHaveLength(3)
  })

  it('recentFilter 与会话页 modeFilter 互不干扰（侧栏语义独立）', async () => {
    const store = useThreadListStore()
    mockedList.mockResolvedValueOnce(listData(makeItems('a', 2)))
    await store.fetchThreads({ mode: null })
    store.setRecentFilter('fast')
    expect(store.modeFilter).toBeNull()
    store.setModeFilter('deep')
    expect(store.recentFilter).toBe('fast')
  })
})
