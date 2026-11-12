/**
 * test/component/home-threads-view.test.ts — HomeView / ThreadsView 组件层（§1 映射行）。
 * Home：提问 → POST /threads → 跳答案页；推荐问题卡深度场景直提。
 * Threads：列表渲染 + 日期分组 + 筛选 chips + 生成中指示器 + 内嵌搜索直达。
 */
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { apiGet, apiPost } from '@/api/client'
import { i18n } from '@/i18n'
import { modelsFixture } from '@/mocks/models'
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
const mockedPost = vi.mocked(apiPost)

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
    throw new Error(`unexpected GET ${url}`)
  })
  mockedPost.mockResolvedValue({ thread_id: THREAD_ID_FAST, turn_id: TURN_ID_FAST, status: 'running' })
  // ask() 两段式第二段订阅需要 EventSource（jsdom 无此全局）
  MockEventSource.reset()
  vi.stubGlobal('EventSource', MockEventSource)
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
  it('搜索卡渲染：标题 + placeholder + 推荐/直提入口', () => {
    const w = mount(HomeView, { global: { plugins: [createPinia(), router, i18n] } })
    expect(w.find('[data-testid="home-view"]').text()).toContain('你想了解什么？')
    expect(w.find('[data-testid="home-search-card"]').exists()).toBe(true)
    expect(w.findAll('[data-testid="home-suggest-item"]')).toHaveLength(3)
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
})

describe('ThreadsView（§1：列表 + 筛选 + 日期分组 + 生成中指示器）', () => {
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

  it('筛选 chips：点击深度 → fetchThreads 携带 mode=deep；全部 → 不带 mode 参数', async () => {
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

  it('日期分组：今天/最近 7 天/更早（fixture 三个日期各归一组）', async () => {
    // 以固定「今天」挂载（groups 依赖挂载时刻的 Date.now；fixture item0=当天、item1=2 天前、item2=更早）
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-11-01T15:00:00Z'))
    const w = mountThreads()
    await flushPromises()
    expect(w.text()).toContain('今天')
    expect(w.text()).toContain('最近 7 天')
    expect(w.text()).toContain('更早')
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
})
