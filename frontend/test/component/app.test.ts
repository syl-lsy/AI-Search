import { createPinia, getActivePinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { apiGet, apiPost } from '@/api/client'
import App from '@/App.vue'
import { i18n } from '@/i18n'
import { THREAD_ID_FAST, TURN_ID_FAST, turnDetailFastCompleted } from '@/mocks/turnDetails'
import { threadListFixture } from '@/mocks/threads'
import { router } from '@/router'
import { usePrefsStore } from '@/stores/prefs'
import { useTurnStore } from '@/stores/turn'

const VALID_THREAD_ID = THREAD_ID_FAST

/**
 * 组件层装配冒烟：真实 router + i18n 单例 + 独立 pinia（§4 store 依赖隔离）。
 * prefsStore.setLang 写的是 '@/i18n' 单例——挂载也必须用同一单例，切换才对界面响应式生效。
 * REST（@/api/client）mock：组件层不打真网络（fixture 驱动，§9）。
 */
vi.mock('@/api/client', () => ({
  API_BASE_URL: '',
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPut: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
  apiDownload: vi.fn(),
  // 视图层错误归一出口（toast 路径）；组件层不打真网络，兜底文案与真实实现一致
  normalizeError: () => ({ name: 'ApiError', code: 'network_error', message: '网络异常，请稍后重试' }),
}))

const mockedGet = vi.mocked(apiGet)
const mockedPost = vi.mocked(apiPost)

/** 按 URL 分发 fixture：threads 列表 / 线程详情 / 轮次详情 */
mockedGet.mockImplementation(async (url: string) => {
  if (url === '/api/v1/threads') return threadListFixture
  if (url === `/api/v1/threads/${VALID_THREAD_ID}`) {
    return {
      thread_id: VALID_THREAD_ID,
      title: 'LangGraph 的 Send API 和 Router 的区别？',
      mode_summary: 'fast',
      turn_count: 1,
      has_artifacts: false,
      created_at: '2026-11-01T08:12:00Z',
      updated_at: '2026-11-01T09:40:11Z',
      turns: [
        {
          turn_id: TURN_ID_FAST,
          mode: 'fast',
          status: 'completed',
          query: turnDetailFastCompleted.query,
          query_lang: 'zh',
          has_report: false,
          degraded: false,
          duration_ms: 7420,
          created_at: '2026-11-01T08:12:00Z',
        },
      ],
    }
  }
  if (url === `/api/v1/turns/${TURN_ID_FAST}`) return turnDetailFastCompleted
  throw new Error(`unexpected GET ${url}`)
})
mockedPost.mockResolvedValue({})

let wrapper: VueWrapper | null = null

async function mountApp(): Promise<VueWrapper> {
  const pinia = createPinia()
  setActivePinia(pinia)
  wrapper = mount(App, {
    global: { plugins: [pinia, router, i18n] },
  })
  await router.isReady()
  await flushPromises()
  return wrapper
}

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
})

describe('App 壳（组件层冒烟：装配 + 懒加载四页 + i18n）', () => {
  beforeEach(async () => {
    localStorage.clear()
    sessionStorage.clear()
    vi.clearAllMocks()
    i18n.global.locale.value = 'zh-CN'
    await router.push('/')
    await flushPromises()
  })

  it('根壳渲染：侧栏导航 + 图标 sprite + 首页（RouterView 懒加载解析）', async () => {
    const app = await mountApp()
    expect(app.find('[data-testid="app-shell"]').exists()).toBe(true)
    expect(app.find('[data-testid="sidebar"]').exists()).toBe(true)
    expect(app.find('[data-testid="nav-home"]').exists()).toBe(true)
    expect(app.find('[data-testid="home-view"]').text()).toContain('你想了解什么？')
  })

  it('路由切换：/threads 与 /library（懒加载视图就位）', async () => {
    const app = await mountApp()

    await router.push('/threads')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('threads')
    expect(app.find('[data-testid="threads-view"]').exists()).toBe(true)

    await router.push('/library')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('library')
    expect(app.find('[data-testid="library-view"]').exists()).toBe(true)
  })

  it('AnswerView：合法 threadId → 2.3+2.5 渲染轮次（§1 编排）；非法参数渲染空态不触 API（§8.3）', async () => {
    const app = await mountApp()

    await router.push(`/thread/${VALID_THREAD_ID}`)
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('answer')
    expect(mockedGet).toHaveBeenCalledWith(`/api/v1/threads/${VALID_THREAD_ID}`)
    // 轮次投影：问题气泡 + AnswerRenderer 内容（fixture answer_md 首句）
    expect(app.find('[data-testid="answer-view"]').text()).toContain(turnDetailFastCompleted.query)
    expect(app.find('[data-testid="turn-block"]').exists()).toBe(true)
    expect(app.find('[data-testid="answer-renderer"]').text()).toContain('Send API 用于')

    await router.push('/thread/not-a-uuid')
    await flushPromises()
    expect(app.find('[data-testid="answer-view"]').text()).toContain('线程不存在或链接无效')
  })

  it('语言切换即时生效：prefsStore.setLang → 界面文案响应式切 en（§7，不刷新）', async () => {
    const app = await mountApp()
    const prefs = usePrefsStore()
    prefs.setLang('en-US')
    await flushPromises()
    expect(app.find('[data-testid="nav-home"]').text()).toBe('New')
    expect(app.find('[data-testid="home-view"]').text()).toContain('What do you want to know?')
  })

  it('侧栏「最近」行：生成中线程带 i-refresh 旋转指示（title=threads.generating；与 ThreadsView 口径一致，P2）', async () => {
    const app = await mountApp()
    // 侧栏最近列表已拉取（fixture 第一条 = THREAD_ID_FAST）；初始无生成中指示
    expect(app.find(`[data-testid="sb-generating-${THREAD_ID_FAST}"]`).exists()).toBe(false)
    const turnStore = useTurnStore(getActivePinia())
    turnStore.generatingThreadIds.add(THREAD_ID_FAST)
    await flushPromises()
    const indicator = app.find(`[data-testid="sb-generating-${THREAD_ID_FAST}"]`)
    expect(indicator.exists()).toBe(true)
    expect(indicator.attributes('title')).toBe('生成中')
    expect(indicator.find('svg').classes().join(' ')).toContain('icon-sm')
    turnStore.generatingThreadIds.delete(THREAD_ID_FAST)
    await flushPromises()
    expect(app.find(`[data-testid="sb-generating-${THREAD_ID_FAST}"]`).exists()).toBe(false)
  })
})
