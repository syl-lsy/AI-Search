/**
 * test/component/home-threads-view.test.ts — HomeView / ThreadsView 组件层（§1 映射行）。
 * Home：提问 → POST /threads → 跳答案页；推荐问题卡深度场景直提。
 * Threads：列表渲染 + 日期分组 + 筛选 chips + 生成中指示器 + 内嵌搜索直达。
 */
import { createPinia, getActivePinia, setActivePinia } from 'pinia'
import { DOMWrapper, flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { apiGet, apiPatch, apiPost } from '@/api/client'
import { i18n } from '@/i18n'
import { modelsFixture } from '@/mocks/models'
import { suggestionsFixture } from '@/mocks/suggestions'
import { THREAD_ID_FAST, TURN_ID_FAST } from '@/mocks/turnDetails'
import { threadListFixture } from '@/mocks/threads'
import { router } from '@/router'
import { useTurnStore } from '@/stores/turn'
import HomeView from '@/views/HomeView.vue'
import ThreadsView from '@/views/ThreadsView.vue'

import { MockEventSource } from '../helpers/mockEventSource'

vi.mock('@/api/client', () => ({
  API_BASE_URL: '',
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPut: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
  apiDownload: vi.fn(),
  normalizeError: () => ({ name: 'ApiError', code: 'network_error', message: '网络异常，请稍后重试' }),
}))

const mockedGet = vi.mocked(apiGet)
const mockedPatch = vi.mocked(apiPatch)
const mockedPost = vi.mocked(apiPost)

/** IntersectionObserver 替身（jsdom 无此全局）：sentinel 触发断言通道（useInfiniteScroll 消费） */
class MockIntersectionObserver {
  static instances: MockIntersectionObserver[] = []
  cb: IntersectionObserverCallback
  observe = vi.fn()
  disconnect = vi.fn()
  unobserve = vi.fn()
  constructor(cb: IntersectionObserverCallback) {
    this.cb = cb
    MockIntersectionObserver.instances.push(this)
  }
  trigger(isIntersecting = true): void {
    this.cb([{ isIntersecting } as IntersectionObserverEntry], this as unknown as IntersectionObserver)
  }
}

beforeEach(async () => {
  setActivePinia(createPinia())
  localStorage.clear()
  sessionStorage.clear()
  vi.clearAllMocks()
  i18n.global.locale.value = 'zh-CN'
  // URL 分发：/models 与 /threads 形态不同（SearchCard 内嵌 ModelSelect，混喂会崩渲染树）
  mockedGet.mockImplementation(async (url: string) => {
    if (url === '/api/v1/models') return modelsFixture
    if (url === '/api/v1/threads') return threadListFixture
    if (url === '/api/v1/suggestions') return suggestionsFixture
    throw new Error(`unexpected GET ${url}`)
  })
  mockedPost.mockResolvedValue({ thread_id: THREAD_ID_FAST, turn_id: TURN_ID_FAST, status: 'running' })
  // ask() 两段式第二段订阅需要 EventSource（jsdom 无此全局）
  MockEventSource.reset()
  MockIntersectionObserver.instances = []
  vi.stubGlobal('EventSource', MockEventSource)
  vi.stubGlobal('IntersectionObserver', MockIntersectionObserver)
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => setTimeout(() => cb(0), 16))
  await router.push('/')
  await router.isReady()
})

afterEach(async () => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
  await router.push('/')
  await flushPromises()
})

afterEach(async () => {
  await router.push('/')
  await flushPromises()
})

describe('HomeView（§1：提问 → POST /threads → 跳答案页）', () => {
  it('搜索卡渲染：标题 + placeholder + 推荐/直提入口；fluid 同宽（修复项④b，与推荐面板对齐）', () => {
    const w = mount(HomeView, { global: { plugins: [createPinia(), router, i18n] } })
    expect(w.find('[data-testid="home-view"]').text()).toContain('你想了解什么？')
    expect(w.find('[data-testid="home-search-card"]').exists()).toBe(true)
    expect(w.find('[data-testid="home-search-card"]').classes()).toContain('fluid')
    expect(w.findAll('[data-testid="home-suggest-item"]')).toHaveLength(3)
  })

  it('推荐问题文案走 i18n（home.suggestItems，zh-CN 渲染中文文案；P2：散写中文文案迁移收口）', async () => {
    const w = mount(HomeView, { global: { plugins: [createPinia(), router, i18n] } })
    const items = w.findAll('[data-testid="home-suggest-item"]')
    expect(items[0]?.text()).toContain('PostgreSQL 17')
    expect(items[1]?.text()).toContain('AI Agent 框架')
    expect(items[2]?.text()).toContain('检索融合')
    // 切 en 后文案跟随（同一 i18n 单例响应式；DOM 重渲在 nextTick）
    i18n.global.locale.value = 'en-US'
    await flushPromises()
    const en = w.findAll('[data-testid="home-suggest-item"]')
    expect(en[0]?.text()).toContain('PostgreSQL 17 and 16')
    i18n.global.locale.value = 'zh-CN'
    await flushPromises()
  })

  it('输入提交：turnStore.ask（POST /api/v1/threads，注入 mode）→ 跳 /thread/{thread_id}', async () => {
    const w = mount(HomeView, { global: { plugins: [createPinia(), router, i18n] } })
    await w.find('[data-testid="home-search-card"] textarea').setValue('PostgreSQL 17 新特性')
    await w.find('[data-testid="home-search-card"] [data-testid="submit-btn"]').trigger('click')
    // 跳转含懒加载组件解析——waitFor 至路由就位
    await vi.waitFor(
      () => {
        expect(router.currentRoute.value.path).toBe(`/thread/${THREAD_ID_FAST}`)
      },
      { timeout: 3_000, interval: 20 },
    )
    expect(mockedPost).toHaveBeenCalledWith('/api/v1/threads', expect.objectContaining({ query: 'PostgreSQL 17 新特性', mode: 'fast' }))
  })

  it('推荐问题卡深度场景：直提 mode=deep（S2 场景）', async () => {
    const w = mount(HomeView, { global: { plugins: [createPinia(), router, i18n] } })
    await w.findAll('[data-testid="home-suggest-item"]')[1]!.trigger('click')
    await vi.waitFor(
      () => {
        expect(router.currentRoute.value.path).toBe(`/thread/${THREAD_ID_FAST}`)
      },
      { timeout: 3_000, interval: 20 },
    )
    expect(mockedPost).toHaveBeenCalledWith('/api/v1/threads', expect.objectContaining({ mode: 'deep' }))
  })

  it('空输入提交被禁用（fab disabled）', () => {
    const w = mount(HomeView, { global: { plugins: [createPinia(), router, i18n] } })
    expect(w.find('[data-testid="home-search-card"] [data-testid="submit-btn"]').attributes('disabled')).toBeDefined()
  })

  it('新线程提交成功 → threadListStore.refresh（GET /threads 重拉侧栏「最近」，修复项①）', async () => {
    const w = mount(HomeView, { global: { plugins: [createPinia(), router, i18n] } })
    mockedGet.mockClear()
    await w.find('[data-testid="home-search-card"] textarea').setValue('侧栏刷新回归题')
    await w.find('[data-testid="home-search-card"] [data-testid="submit-btn"]').trigger('click')
    await flushPromises()
    expect(mockedPost).toHaveBeenCalledWith('/api/v1/threads', expect.objectContaining({ query: '侧栏刷新回归题' }))
    expect(mockedGet).toHaveBeenCalledWith(
      '/api/v1/threads',
      expect.objectContaining({ params: expect.objectContaining({ limit: 50 }) }),
    )
  })
})

describe('HomeView 输入推荐面板（REQ-047 / 修复项⑦，对标图 3）', () => {
  function mountHome() {
    // 复用 beforeEach 的 active pinia（HomeSuggestions 历史匹配消费 threadListStore——
    // seedThreadList 预置的 store 与挂载必须是同一实例）
    const pinia = getActivePinia()
    if (!pinia) throw new Error('beforeEach 未初始化 active pinia')
    return mount(HomeView, { global: { plugins: [pinia, router, i18n] } })
  }
  const textarea = (w: ReturnType<typeof mountHome>) =>
    w.find('[data-testid="home-search-card"] textarea')

  /** 预置线程列表（真实形态 = App 壳挂载时 fetchThreads 进 threadListStore；HomeView 单挂
   *  测试须自行预置——历史匹配消费 store.items，取数职责在壳层） */
  async function seedThreadList(): Promise<void> {
    const { useThreadListStore } = await import('@/stores/threadList')
    await useThreadListStore().fetchRecent()
  }

  it('首次聚焦 → 面板出现 + 幂等拉取热门池（GET /suggestions）+ 热门前 6 条（i-search）', async () => {
    vi.useFakeTimers()
    const w = mountHome()
    mockedGet.mockClear()
    await textarea(w).trigger('focus')
    await flushPromises()
    expect(w.find('[data-testid="home-suggestions"]').exists()).toBe(true)
    expect(mockedGet).toHaveBeenCalledWith('/api/v1/suggestions')
    expect(w.findAll('[role="option"]')).toHaveLength(6)
    expect(w.findAll('[role="option"]')[0]!.text()).toBe(suggestionsFixture.items[0])
    w.unmount()
    vi.useRealTimers()
  })

  it('输入 ≥2 字符 → 250ms 防抖后切历史匹配（标题包含、大小写不敏感）；清空回热门池', async () => {
    vi.useFakeTimers()
    await seedThreadList()
    const w = mountHome()
    await textarea(w).trigger('focus')
    await flushPromises()
    await textarea(w).setValue('langgraph') // fixture 标题「LangGraph 的 Send API...」大小写不敏感命中
    await vi.advanceTimersByTimeAsync(250)
    const options = w.findAll('[role="option"]')
    expect(options).toHaveLength(1)
    expect(options[0]!.text()).toContain('LangGraph 的 Send API')
    expect(options[0]!.find('use').attributes('href')).toBe('#i-clock')
    // 清空 → 回热门池
    await textarea(w).setValue('')
    await vi.advanceTimersByTimeAsync(250)
    expect(w.findAll('[role="option"]')).toHaveLength(6)
    w.unmount()
    vi.useRealTimers()
  })

  it('无匹配历史 → 空态文案（home.suggestEmpty）', async () => {
    vi.useFakeTimers()
    await seedThreadList()
    const w = mountHome()
    await textarea(w).trigger('focus')
    await flushPromises()
    await textarea(w).setValue('量子色动力学的渐进自由')
    await vi.advanceTimersByTimeAsync(250)
    expect(w.findAll('[role="option"]')).toHaveLength(0)
    expect(w.find('[role="status"]').text()).toBe('无匹配历史')
    w.unmount()
    vi.useRealTimers()
  })

  it('点击历史条目 → 复用 submit 提交（POST 注入历史文本）+ 面板收起', async () => {
    vi.useFakeTimers()
    await seedThreadList()
    const w = mountHome()
    await textarea(w).trigger('focus')
    await flushPromises()
    await textarea(w).setValue('RAG')
    await vi.advanceTimersByTimeAsync(250)
    await w.findAll('[role="option"]')[0]!.trigger('click')
    await flushPromises()
    expect(mockedPost).toHaveBeenCalledWith(
      '/api/v1/threads',
      expect.objectContaining({ query: threadListFixture.items[2]!.title }),
    )
    expect(w.find('[data-testid="home-suggestions"]').exists()).toBe(false)
    w.unmount()
    vi.useRealTimers()
  })

  it('历史匹配按标题去重（用户报告：同题多线程重复显示）：store.items 含重复标题时建议列表唯一（保留列表序在前者）', async () => {
    vi.useFakeTimers()
    // 覆写 URL 分发：线程列表注入重复标题（两条同题 + 一条相邻命中，模拟历史多线程同题）
    const dupTitle = 'LangGraph 的 Send API 和 Router 的区别？'
    const dupItem = (id: string, title: string, day: number) => ({
      thread_id: id,
      title,
      mode_summary: 'fast' as const,
      turn_count: 1,
      created_at: `2026-11-${String(day).padStart(2, '0')}T08:00:00Z`,
      updated_at: `2026-11-${String(day).padStart(2, '0')}T09:00:00Z`,
    })
    mockedGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/threads') {
        return {
          total: 3,
          items: [
            dupItem('a1a1a1a1-1111-4111-8111-111111111111', dupTitle, 3),
            dupItem('a2a2a2a2-2222-4222-8222-222222222222', dupTitle, 2),
            dupItem('a3a3a3a3-3333-4333-8333-333333333333', 'LangGraph Send API 参考', 1),
          ],
        }
      }
      if (url === '/api/v1/models') return modelsFixture
      if (url === '/api/v1/suggestions') return suggestionsFixture
      throw new Error(`unexpected GET ${url}`)
    })
    await seedThreadList()
    const w = mountHome()
    await textarea(w).trigger('focus')
    await flushPromises()
    await textarea(w).setValue('langgraph')
    await vi.advanceTimersByTimeAsync(250)
    const options = w.findAll('[role="option"]')
    // 去重后唯一：同题两条只保留列表序在前者（最新），相邻命中照常出现
    expect(options).toHaveLength(2)
    expect(options[0]!.text()).toContain(dupTitle)
    expect(options[1]!.text()).toContain('LangGraph Send API 参考')
    w.unmount()
    vi.useRealTimers()
  })

  it('Esc 立即收起；blur 延迟 150ms 收起（给条目 click 让路）', async () => {
    vi.useFakeTimers()
    const w = mountHome()
    await textarea(w).trigger('focus')
    await flushPromises()
    // Esc 立即收起
    await textarea(w).trigger('keydown', { key: 'Escape' })
    expect(w.find('[data-testid="home-suggestions"]').exists()).toBe(false)
    // 再聚焦 → blur 后 150ms 内仍在、到期收起
    await textarea(w).trigger('focus')
    expect(w.find('[data-testid="home-suggestions"]').exists()).toBe(true)
    await textarea(w).trigger('blur')
    await vi.advanceTimersByTimeAsync(100)
    expect(w.find('[data-testid="home-suggestions"]').exists()).toBe(true)
    await vi.advanceTimersByTimeAsync(60)
    expect(w.find('[data-testid="home-suggestions"]').exists()).toBe(false)
    w.unmount()
    vi.useRealTimers()
  })
})

describe('ThreadsView（§1 v1.21：全量加载 + 三态视图 + 逐日分组 + 搜索 + 行级菜单）', () => {
  function mountThreads() {
    return mount(ThreadsView, { global: { plugins: [createPinia(), router, i18n] } })
  }

  it('列表渲染：行 = 标题 + 模式徽标 + 轮次徽标 + 相对时间；生成中集合命中时显示指示徽标', async () => {
    const w = mountThreads()
    await flushPromises()
    expect(w.findAll('[data-testid^="thread-row-"]')).toHaveLength(3)
    const row = w.find(`[data-testid="thread-row-${THREAD_ID_FAST}"]`)
    expect(row.text()).toContain(threadListFixture.items[0]!.title)
    expect(row.text()).toContain('轮') // turn_count 徽标
    // 生成中集合（turnStore.generatingThreadIds）命中 → threads.generating 徽标 + 旋转图标
    const turnStore = useTurnStore()
    turnStore.generatingThreadIds.add(THREAD_ID_FAST)
    await flushPromises()
    expect(w.find(`[data-testid="thread-row-${THREAD_ID_FAST}"]`).text()).toContain('生成中')
  })

  it('mode chips：点击深度 → setModeFilter 携带 mode=deep；全部 → 不带 mode 参数', async () => {
    const w = mountThreads()
    await flushPromises()
    mockedGet.mockClear()
    await w.find('[data-testid="threads-filter-deep"]').trigger('click')
    await flushPromises()
    expect(mockedGet).toHaveBeenCalledWith('/api/v1/threads', expect.objectContaining({ params: expect.objectContaining({ mode: 'deep' }) }))
    await w.find('[data-testid="threads-filter-all"]').trigger('click')
    await flushPromises()
    expect(mockedGet).toHaveBeenLastCalledWith('/api/v1/threads', expect.objectContaining({ params: expect.not.objectContaining({ mode: expect.anything() }) }))
  })

  it('三态视图 radio：切换 未读/已归档 → unread/archived 参数映射（重置分页+首拉）', async () => {
    const w = mountThreads()
    await flushPromises()
    mockedGet.mockClear()
    await w.find('[data-testid="view-filter-unread"]').trigger('click')
    await flushPromises()
    expect(mockedGet).toHaveBeenLastCalledWith('/api/v1/threads', expect.objectContaining({ params: expect.objectContaining({ unread: true }) }))
    await w.find('[data-testid="view-filter-archived"]').trigger('click')
    await flushPromises()
    expect(mockedGet).toHaveBeenLastCalledWith('/api/v1/threads', expect.objectContaining({ params: expect.objectContaining({ archived: true }) }))
    expect(mockedGet).toHaveBeenLastCalledWith('/api/v1/threads', expect.objectContaining({ params: expect.not.objectContaining({ unread: expect.anything() }) }))
    // 回到全部：无 archived/unread 参数
    await w.find('[data-testid="view-filter-active"]').trigger('click')
    await flushPromises()
    expect(mockedGet).toHaveBeenLastCalledWith('/api/v1/threads', expect.objectContaining({ params: expect.not.objectContaining({ archived: expect.anything() }) }))
  })

  it('逐日分组四形态（时钟注入）：今天/昨天/本年 M月D日/往年 带年份（D-18 取代三桶）', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(2026, 10, 1, 15, 0, 0))
    const day = (m: number, d: number, y = 2026) =>
      Array.from({ length: 1 }, (_, i) => ({
        thread_id: `t${m}${d}${y}-${i}`,
        title: `题 ${m}-${d}-${y}`,
        mode_summary: 'fast' as const,
        turn_count: 1,
        pinned: false,
        is_unread: false,
        archived: false,
        created_at: new Date(y, m - 1, d, 9).toISOString(),
        updated_at: new Date(y, m - 1, d, 9).toISOString(),
      }))
    const items = [...day(11, 1), ...day(10, 31), ...day(5, 12), ...day(11, 1, 2025)]
    mockedGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/models') return modelsFixture
      if (url === '/api/v1/threads') return { total: items.length, items }
      throw new Error(`unexpected GET ${url}`)
    })
    const w = mountThreads()
    await flushPromises()
    const text = w.find('[data-testid="threads-list"]').text()
    expect(text).toContain('今天')
    expect(text).toContain('昨天')
    expect(text).toContain('5月12日')
    expect(text).toContain('2025年11月1日')
    // 旧三桶词条不再出现
    expect(text).not.toContain('最近 7 天')
    expect(text).not.toContain('更早')
  })

  it('标题搜索（D-19）：防抖 250ms 后携带 q（快击只发一次）；清空回全量；与 mode 叠加', async () => {
    vi.useFakeTimers()
    const w = mountThreads()
    await vi.advanceTimersByTimeAsync(0)
    mockedGet.mockClear()
    const input = w.find('[data-testid="threads-search-input"]')
    await input.setValue('Lang')
    await input.setValue('LangGraph')
    // 防抖窗口内不发请求
    await vi.advanceTimersByTimeAsync(100)
    expect(mockedGet).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(150)
    await flushPromises()
    expect(mockedGet).toHaveBeenCalledTimes(1) // 快击合并为一次
    expect(mockedGet).toHaveBeenLastCalledWith('/api/v1/threads', expect.objectContaining({ params: expect.objectContaining({ q: 'LangGraph' }) }))
    // 与 mode chips 叠加
    await w.find('[data-testid="threads-filter-deep"]').trigger('click')
    await vi.advanceTimersByTimeAsync(0)
    expect(mockedGet).toHaveBeenLastCalledWith('/api/v1/threads', expect.objectContaining({ params: expect.objectContaining({ q: 'LangGraph', mode: 'deep' }) }))
    // 清空回全量（无 q）
    await input.setValue('')
    await vi.advanceTimersByTimeAsync(250)
    expect(mockedGet).toHaveBeenLastCalledWith('/api/v1/threads', expect.objectContaining({ params: expect.not.objectContaining({ q: expect.anything() }) }))
    w.unmount()
    vi.useRealTimers()
  })

  it('触底自动加载（IntersectionObserver sentinel）：追加下一页；hasMore=false 后不再发请求', async () => {
    vi.useFakeTimers()
    const page1 = threadListFixture.items
    const extra = (n: number, prefix: string) =>
      Array.from({ length: n }, (_, i) => ({
        thread_id: `${prefix}-${String(i).padStart(3, '0')}`,
        title: `补充题 ${prefix} ${i}`,
        mode_summary: 'fast' as const,
        turn_count: 1,
        pinned: false,
        is_unread: false,
        archived: false,
        created_at: '2026-10-01T00:00:00Z',
        updated_at: '2026-10-01T00:00:00Z',
      }))
    mockedGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/models') return modelsFixture
      if (url === '/api/v1/threads') {
        const params = (mockedGet.mock.calls.at(-1)?.[1] as { params?: { offset?: number } })?.params
        return (params?.offset ?? 0) === 0
          ? { total: 7, items: [...page1, ...extra(2, 'p1')] }
          : { total: 7, items: extra(2, 'p2') } // offset=5 末页
      }
      throw new Error(`unexpected GET ${url}`)
    })
    const w = mountThreads()
    await vi.advanceTimersByTimeAsync(0)
    expect(w.findAll('[data-testid^="thread-row-"]')).toHaveLength(5)
    // IntersectionObserver mock：sentinel 进入视口 → loadMore
    const io = MockIntersectionObserver.instances.at(-1)
    expect(io).toBeDefined()
    io!.trigger(true)
    await vi.advanceTimersByTimeAsync(0)
    expect(mockedGet).toHaveBeenLastCalledWith('/api/v1/threads', expect.objectContaining({ params: expect.objectContaining({ offset: 5 }) }))
    expect(w.findAll('[data-testid^="thread-row-"]')).toHaveLength(7) // 追加 + 7==total 收口
    // 加载尾态：hasMore=false → 无加载提示
    expect(w.find('[data-testid="threads-loading-more"]').exists()).toBe(false)
    w.unmount()
    vi.useRealTimers()
  })

  it('触底失败：行内「加载失败·重试」；重试成功恢复', async () => {
    vi.useFakeTimers()
    mockedGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/models') return modelsFixture
      if (url === '/api/v1/threads') {
        const params = (mockedGet.mock.calls.at(-1)?.[1] as { params?: { offset?: number } })?.params
        if ((params?.offset ?? 0) > 0) throw new Error('network') // loadMore 行内失败态（不 toast）
        return { total: 5, items: [...threadListFixture.items, { ...threadListFixture.items[0]!, thread_id: 'a-different-one' }] }
      }
      throw new Error(`unexpected GET ${url}`)
    })
    const w = mountThreads()
    await vi.advanceTimersByTimeAsync(0)
    const io = MockIntersectionObserver.instances.at(-1)!
    io.trigger(true)
    await vi.advanceTimersByTimeAsync(0)
    expect(w.find('[data-testid="threads-load-failed"]').exists()).toBe(true)
    expect(w.find('[data-testid="threads-retry"]').text()).toContain('重试')
    // 重试成功：恢复无失败态（本轮 mock 回默认实现）
    mockedGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/models') return modelsFixture
      if (url === '/api/v1/threads') return threadListFixture
      throw new Error(`unexpected GET ${url}`)
    })
    await w.find('[data-testid="threads-retry"]').trigger('click')
    await vi.advanceTimersByTimeAsync(0)
    expect(w.find('[data-testid="threads-load-failed"]').exists()).toBe(false)
    w.unmount()
    vi.useRealTimers()
  })

  it('未读点 + 置顶图钉：is_unread/pinned 驱动；归档视图不渲染图钉', async () => {
    const unreadItem = { ...threadListFixture.items[0]!, is_unread: true, pinned: true }
    const items = [
      unreadItem,
      { ...threadListFixture.items[1]!, pinned: true },
      threadListFixture.items[2]!,
    ]
    mockedGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/models') return modelsFixture
      if (url === '/api/v1/threads') return { total: items.length, items }
      throw new Error(`unexpected GET ${url}`)
    })
    const w = mountThreads()
    await flushPromises()
    expect(w.find(`[data-testid="unread-dot-${unreadItem.thread_id}"]`).exists()).toBe(true)
    expect(w.find(`[data-testid="pin-${unreadItem.thread_id}"]`).attributes('title')).toBe('已置顶')
    expect(w.find(`[data-testid="pin-${items[1]!.thread_id}"]`).exists()).toBe(true)
    // 切归档视图 → 图钉隐藏（未读点渲染与归档投影数据相关——archived 行 is_unread 形态由服务端投影，前端按数据渲染）
    await w.find('[data-testid="view-filter-archived"]').trigger('click')
    await flushPromises()
    expect(w.find(`[data-testid="pin-${unreadItem.thread_id}"]`).exists()).toBe(false)
  })

  it('行级三点菜单动作分发：置顶 → PATCH {pinned:true}（经 store，组件零直接触网）', async () => {
    const w = mountThreads()
    await flushPromises()
    const row = w.find(`[data-testid="thread-row-${THREAD_ID_FAST}"]`)
    await row.find('[data-testid="tam-trigger"]').trigger('click')
    // 弹层 Teleport 到 body（v1.25 裁剪修复）——document 级查询（wrapper.find 不穿 Teleport）
    const items = Array.from(document.querySelectorAll('[role="menuitem"]')).map((el) => new DOMWrapper(el as HTMLElement))
    expect(items.map((x) => x.text())).toEqual(['置顶', '标记为未读', '重命名', '归档', '删除'])
    mockedPatch.mockResolvedValueOnce({ ...threadListFixture.items[0]!, pinned: true })
    await items[0]!.trigger('click')
    await flushPromises()
    // PATCH 经 api/threads.ts → client 唯一出口（URL = 契约完整路径 + body）
    expect(mockedPatch).toHaveBeenCalledWith(`/api/v1/threads/${THREAD_ID_FAST}`, { pinned: true })
  })

  it('空列表空态（不报错）', async () => {
    mockedGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/models') return modelsFixture
      if (url === '/api/v1/threads') return { total: 0, items: [] }
      throw new Error(`unexpected GET ${url}`)
    })
    const w = mountThreads()
    await flushPromises()
    expect(w.text()).toContain('暂无线程')
  })

  it('搜索框与 store.searchQuery 同步初始化（P2-3：返回会话页回显过滤词，列表与框一致）', async () => {
    // 同一 pinia 实例预置 store 过滤态（跨路由持久的 searchQuery），再以同实例挂载
    const pinia = createPinia()
    setActivePinia(pinia)
    const { useThreadListStore } = await import('@/stores/threadList')
    const store = useThreadListStore()
    await store.setSearchQuery('LangGraph')
    const w = mount(ThreadsView, { global: { plugins: [pinia, router, i18n] } })
    await flushPromises()
    const input = w.find('[data-testid="threads-search-input"]')
    expect((input.element as HTMLInputElement).value).toBe('LangGraph')
    // 列表请求携带同一 q（框与列表一致）
    expect(mockedGet).toHaveBeenCalledWith(
      '/api/v1/threads',
      expect.objectContaining({ params: expect.objectContaining({ q: 'LangGraph' }) }),
    )
  })
})
