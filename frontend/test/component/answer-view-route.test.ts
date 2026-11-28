/**
 * test/component/answer-view-route.test.ts — AnswerView 路由参数切换重载（侧栏「最近」直达）。
 *
 * 回归背景（2026-11-14 修复项①）：App RouterView 无 key，/thread/:threadId 参数变化时
 * AnswerView 实例被复用，onMounted 不再触发 → 旧线程内容残留、旧线程 SSE 不关。
 * 修复形态 = watch(threadId) 重载：prev 线程先 closeStream 全部流 + 复位标题/轮列表/锚点，
 * 再拉取新线程（2.3 骨架 + 逐轮对账 + running/queued 订阅，与 onMounted 同一装配路径）。
 *
 * 联装形态与 app.test.ts 同族：真实 router + i18n 单例 + 独立 pinia + REST mock（fixture 驱动）
 * + MockEventSource 替身（SSE 订阅断言用）。
 */
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { apiGet, apiPost } from '@/api/client'
import App from '@/App.vue'
import { i18n } from '@/i18n'
import { modelsFixture } from '@/mocks/models'
import {
  THREAD_ID_FAST,
  TURN_ID_AWAITING,
  TURN_ID_DEEP,
  TURN_ID_DIRECT,
  TURN_ID_FAST,
  turnDetailDeepCompleted,
  turnDetailDirectCompleted,
  turnDetailFastApproved,
  turnDetailFastAwaiting,
  turnDetailFastCompleted,
  turnDetailFastNoHit,
} from '@/mocks/turnDetails'
import { threadListFixture } from '@/mocks/threads'
import { router } from '@/router'
import type { KbChunkDetail, KbDocument, SnapshotData, Source } from '@/types/api'

import { MockEventSource } from '../helpers/mockEventSource'

/** jsdom 无 scrollIntoView：来源面板跟随 watch（flush:post）会在滚动类测试的晚到 rAF 里触发定位——文件级补桩（浏览器恒有该 API） */
if (typeof Element.prototype.scrollIntoView !== 'function') {
  Element.prototype.scrollIntoView = () => {}
}

/** normalizeError 归一结果可调（regenerate 409006 用例断言服务端 message 直达 toast；缺省=网络兜底文案） */
const normalizeState = vi.hoisted(() => ({ message: '网络异常，请稍后重试' }))

vi.mock('@/api/client', () => ({
  API_BASE_URL: '',
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPut: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
  apiDownload: vi.fn(),
  normalizeError: () => ({ name: 'ApiError', code: 'network_error', message: normalizeState.message }),
}))

const mockedGet = vi.mocked(apiGet)
const mockedPost = vi.mocked(apiPost)

/** 线程 B：threads fixture 第二条（pgvector 对比），轮 = TURN_ID_DEEP（深度完成态 fixture） */
const THREAD_ID_B = threadListFixture.items[1]!.thread_id
const THREAD_TITLE_B = threadListFixture.items[1]!.title

let wrapper: VueWrapper | null = null

/** 运行中轮形态（线程 A 挂载时建立 SSE 订阅 → 切线程断言旧流被关） */
const turnDetailFastRunning: typeof turnDetailFastCompleted = {
  ...turnDetailFastCompleted,
  status: 'running',
  answer_md: '',
  sources: [],
  images: [],
  duration_ms: 0,
}

/** 线程 A 骨架（2.3 形态，运行中轮；乱序用例经 deferred 延迟交付同一对象） */
const threadADetailRunning = {
  thread_id: THREAD_ID_FAST,
  title: 'LangGraph 的 Send API 和 Router 的区别？',
  mode_summary: 'fast' as const,
  turn_count: 1,
  has_artifacts: false,
  created_at: '2026-11-01T08:12:00Z',
  updated_at: '2026-11-01T09:40:11Z',
  turns: [
    {
      turn_id: TURN_ID_FAST,
      mode: 'fast' as const,
      status: 'running' as const,
      query: turnDetailFastRunning.query,
      query_lang: 'zh' as const,
      has_report: false,
      degraded: false,
      duration_ms: 0,
      created_at: '2026-11-01T08:12:00Z',
    },
  ],
}

/** URL 分发 fixture（deferThreadA 传入时线程 A 骨架以该 deferred 交付——乱序用例手动放行） */
function installDispatcher(deferThreadA?: Promise<unknown>): void {
  mockedGet.mockImplementation(async (url: string) => {
    if (url === '/api/v1/models') return modelsFixture
    if (url === '/api/v1/threads') return threadListFixture
    if (url === `/api/v1/threads/${THREAD_ID_FAST}`) {
      return deferThreadA ?? threadADetailRunning
    }
    if (url === `/api/v1/turns/${TURN_ID_FAST}`) return turnDetailFastRunning
    if (url === `/api/v1/threads/${THREAD_ID_B}`) {
      return {
        thread_id: THREAD_ID_B,
        title: THREAD_TITLE_B,
        mode_summary: 'deep',
        turn_count: 2,
        has_artifacts: false,
        created_at: '2026-10-30T10:00:00Z',
        updated_at: '2026-10-30T10:03:21Z',
        turns: [
          {
            turn_id: TURN_ID_DEEP,
            mode: 'deep',
            status: turnDetailDeepCompleted.status,
            query: turnDetailDeepCompleted.query,
            query_lang: 'zh',
            has_report: true,
            degraded: false,
            duration_ms: 62_000,
            created_at: '2026-10-30T10:00:00Z',
          },
          {
            turn_id: TURN_ID_DIRECT,
            mode: 'fast',
            status: turnDetailDirectCompleted.status,
            query: turnDetailDirectCompleted.query,
            query_lang: 'zh',
            has_report: false,
            degraded: false,
            duration_ms: 3_200,
            created_at: '2026-10-30T10:02:00Z',
          },
        ],
      }
    }
    if (url === `/api/v1/turns/${TURN_ID_DEEP}`) return turnDetailDeepCompleted
    if (url === `/api/v1/turns/${TURN_ID_DIRECT}`) return turnDetailDirectCompleted
    throw new Error(`unexpected GET ${url}`)
  })
}

beforeEach(async () => {
  setActivePinia(createPinia())
  localStorage.clear()
  sessionStorage.clear()
  vi.clearAllMocks()
  normalizeState.message = '网络异常，请稍后重试'
  i18n.global.locale.value = 'zh-CN'
  MockEventSource.reset()
  vi.stubGlobal('EventSource', MockEventSource)
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => setTimeout(() => cb(0), 16))
  installDispatcher()
  mockedPost.mockResolvedValue({})
  await router.push('/')
  await router.isReady()
})

afterEach(async () => {
  wrapper?.unmount()
  wrapper = null
  await flushPromises()
  vi.unstubAllGlobals()
})

describe('AnswerView 路由参数切换重载（侧栏「最近」直达，修复项①）', () => {
  it('挂载线程 A（运行中轮建立订阅）→ 路由切线程 B → B 取数渲染 + A 流关闭 + 旧内容清除', async () => {
    wrapper = mount(App, { global: { plugins: [createPinia(), router, i18n] } })
    await router.push(`/thread/${THREAD_ID_FAST}`)
    await flushPromises()

    // 线程 A 就位：标题渲染 + 运行中轮订阅（两段式第二段）
    const answerView = () => wrapper!.find('[data-testid="answer-view"]')
    expect(answerView().exists()).toBe(true)
    expect(answerView().text()).toContain('LangGraph 的 Send API 和 Router 的区别？')
    const streamA = MockEventSource.instances.find((i) => i.url.includes(TURN_ID_FAST))
    expect(streamA).toBeDefined()

    mockedGet.mockClear()

    // 侧栏「最近」点击另一线程 = 路由参数变化（组件实例复用）
    await router.push(`/thread/${THREAD_ID_B}`)
    await flushPromises()

    // B 取数：2.3 线程骨架 + 2.5 逐轮对账都打到 B 的资源
    expect(mockedGet).toHaveBeenCalledWith(`/api/v1/threads/${THREAD_ID_B}`)
    expect(mockedGet).toHaveBeenCalledWith(`/api/v1/turns/${TURN_ID_DEEP}`)

    // 渲染更新：B 标题 + B 轮问题出现；A 标题与 A 轮问题清除（断言域=answer-view，
    // 侧栏「最近」列表本就含两个线程标题，不在此域）
    expect(answerView().text()).toContain(THREAD_TITLE_B)
    expect(answerView().text()).toContain(turnDetailDeepCompleted.query)
    expect(answerView().text()).not.toContain('LangGraph 的 Send API 和 Router 的区别？')
    expect(answerView().text()).not.toContain(turnDetailFastCompleted.query)

    // A 的旧流被主动关闭（closeStream；closedByClient=true = 客户端主动 close）
    expect(streamA!.closedByClient).toBe(true)
  })

  it('切到非法 threadId：旧线程流关闭 + 内容清除 + 空态提示（不触 API）', async () => {
    wrapper = mount(App, { global: { plugins: [createPinia(), router, i18n] } })
    await router.push(`/thread/${THREAD_ID_FAST}`)
    await flushPromises()
    const streamA = MockEventSource.instances.find((i) => i.url.includes(TURN_ID_FAST))
    expect(streamA).toBeDefined()

    mockedGet.mockClear()
    await router.push('/thread/not-a-uuid')
    await flushPromises()

    expect(wrapper.find('[data-testid="answer-view"]').text()).toContain('线程不存在或链接无效')
    expect(wrapper.find('[data-testid="answer-view"]').text()).not.toContain(
      'LangGraph 的 Send API 和 Router 的区别？',
    )
    expect(streamA!.closedByClient).toBe(true)
    // 空态不触发任何取数
    expect(mockedGet).not.toHaveBeenCalled()
  })

  it('锚点只保留用户问题条目（修复项③）：deep 标题/direct 正文不再生成锚点条目', async () => {
    wrapper = mount(App, { global: { plugins: [createPinia(), router, i18n] } })
    await router.push(`/thread/${THREAD_ID_B}`)
    await flushPromises()

    // 线程 B 两轮均已渲染（deep 报告带 ## 标题 + direct 带正文——旧实现会产生 4+ 条锚点）
    expect(wrapper.findAll('[data-testid="turn-block"]')).toHaveLength(2)
    // 新实现：锚点条目 = 用户问题，每轮恰一条 `turn-{id}-q`
    const items = wrapper.findAll('[data-testid^="outline-item-"]')
    expect(items).toHaveLength(2)
    expect(items[0]!.attributes('data-testid')).toBe(`outline-item-turn-${TURN_ID_DEEP}-q`)
    expect(items[1]!.attributes('data-testid')).toBe(`outline-item-turn-${TURN_ID_DIRECT}-q`)
    expect(wrapper.find(`[data-testid="outline-item-${TURN_ID_DEEP}-h-0"]`).exists()).toBe(false)
  })

  it('乱序守卫（P1）：A 慢响应后到不覆盖 B 渲染、不产生悬挂订阅（loadSeq 代数守卫）', async () => {
    // A 的线程详情 = 手动放行的 deferred（A 慢、B 快的乱序时序）
    let resolveThreadA: (value: unknown) => void = () => undefined
    const deferredThreadA = new Promise<unknown>((resolve) => {
      resolveThreadA = resolve
    })
    installDispatcher(deferredThreadA)

    wrapper = mount(App, { global: { plugins: [createPinia(), router, i18n] } })
    const answerView = () => wrapper!.find('[data-testid="answer-view"]')

    // A 先发出（fetchThread 挂起：标题/轮列表/订阅均未就位）
    await router.push(`/thread/${THREAD_ID_FAST}`)
    await flushPromises()
    expect(answerView().text()).not.toContain('LangGraph 的 Send API 和 Router 的区别？')
    expect(MockEventSource.instances.some((i) => i.url.includes(TURN_ID_FAST))).toBe(false)

    // 切 B（B 快响应：先完成渲染与对账）
    await router.push(`/thread/${THREAD_ID_B}`)
    await flushPromises()
    expect(answerView().text()).toContain(THREAD_TITLE_B)
    expect(answerView().text()).toContain(turnDetailDeepCompleted.query)

    // A 慢响应此时才到达：无守卫形态下旧 loadThread 会继续 calibrate/reconcile/subscribe(A)
    // → title/turnIds 被 A 覆盖 + A 的 EventSource 悬挂（closeStream 列表只含 B 的轮，永不被关）
    resolveThreadA(threadADetailRunning)
    await flushPromises()

    // 守卫生效：最终渲染仍是 B；A 零订阅（无悬挂连接需要 closeStream 兜底）
    expect(answerView().text()).toContain(THREAD_TITLE_B)
    expect(answerView().text()).toContain(turnDetailDeepCompleted.query)
    expect(answerView().text()).not.toContain('LangGraph 的 Send API 和 Router 的区别？')
    expect(MockEventSource.instances.some((i) => i.url.includes(TURN_ID_FAST))).toBe(false)
  })

  it('切线程清理段补全（P2）：追问草稿清空 + 滚动复位顶部 + 跟随相位复位', async () => {
    wrapper = mount(App, { global: { plugins: [createPinia(), router, i18n] } })
    await router.push(`/thread/${THREAD_ID_FAST}`)
    await flushPromises()

    // A 上：写追问草稿 + 驱动接管相位（scrollTop stub 纯增量逻辑，不依赖布局）
    const followTextarea = () =>
      wrapper!.find('[data-testid="follow-bar"] textarea').element as HTMLTextAreaElement
    await wrapper.find('[data-testid="follow-bar"] textarea').setValue('A 线程的追问草稿')
    const scrollEl = wrapper.find('[data-testid="answer-scroll"]').element as HTMLElement
    let scrollTopValue = 300
    Object.defineProperty(scrollEl, 'scrollTop', {
      configurable: true,
      get: () => scrollTopValue,
      set: (v: number) => {
        scrollTopValue = v
      },
    })
    // 首次 scroll 同步控制器基准位（构造时 lastScrollTop=0，+delta 仅重置上滚累计）
    scrollEl.dispatchEvent(new Event('scroll'))
    await flushPromises()
    // 上滚 200px（delta<0 累计 >8px）→ 接管
    scrollTopValue = 100
    scrollEl.dispatchEvent(new Event('scroll'))
    await flushPromises()
    expect(wrapper.find('[data-testid="answer-scroll"]').attributes('data-scroll-phase')).toBe('taken_over')

    // 切 B：清理段须复位追问草稿 / 滚动位置 / 控制器相位
    await router.push(`/thread/${THREAD_ID_B}`)
    await flushPromises()

    expect(followTextarea().value).toBe('')
    expect(scrollTopValue).toBe(0)
    expect(wrapper.find('[data-testid="answer-scroll"]').attributes('data-scroll-phase')).toBe('following')
  })
})

describe('来源面板线程级累计 + [n] 组作用域定位（frontend-api §5 v1.21 / REQ-061/A45）', () => {
  /** 线程 C：两轮各带 1 条来源（ref_no 均从 1 起——跨组必然同号的定位缺陷复现面） */
  const TURN_2ND = 'aaaa1111-1111-4111-8111-111111111111'

  it('两轮各带来源 → 两组渲染（面板头 Σ 计数）；点第二轮 [1] → 高亮第二轮组卡且第一轮组卡无 cite-hit', async () => {
    const turn1 = turnDetailFastCompleted
    const turn2: typeof turnDetailFastCompleted = {
      ...turnDetailFastCompleted,
      turn_id: TURN_2ND,
      query: '第二轮追问：pgvector 与 Milvus 怎么选？',
      answer_md: '第二轮综合回答 [1]。',
      created_at: '2026-10-30T10:02:00Z',
    }
    mockedGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/models') return modelsFixture
      if (url === '/api/v1/threads') return threadListFixture
      if (url === `/api/v1/threads/${THREAD_ID_B}`) {
        return {
          thread_id: THREAD_ID_B,
          title: THREAD_TITLE_B,
          mode_summary: 'fast',
          turn_count: 2,
          has_artifacts: false,
          created_at: '2026-10-30T10:00:00Z',
          updated_at: '2026-10-30T10:03:21Z',
          turns: [
            { turn_id: TURN_ID_FAST, mode: 'fast', status: 'completed', query: turn1.query, query_lang: 'zh', has_report: false, degraded: false, duration_ms: 7_420, created_at: '2026-10-30T10:00:00Z' },
            { turn_id: TURN_2ND, mode: 'fast', status: 'completed', query: turn2.query, query_lang: 'zh', has_report: false, degraded: false, duration_ms: 5_100, created_at: '2026-10-30T10:02:00Z' },
          ],
        }
      }
      if (url === `/api/v1/turns/${TURN_ID_FAST}`) return turn1
      if (url === `/api/v1/turns/${TURN_2ND}`) return turn2
      throw new Error(`unexpected GET ${url}`)
    })

    wrapper = mount(App, { global: { plugins: [createPinia(), router, i18n] } })
    await router.push(`/thread/${THREAD_ID_B}`)
    await flushPromises()

    // 两组渲染（direct 轮无组口径下：本线程两轮均带来源 → 恰两组）；面板头计数 = Σ = 2
    const groups = wrapper.findAll('.src-group')
    expect(groups).toHaveLength(2)
    expect(groups[0]!.attributes('data-turn-group')).toBe(TURN_ID_FAST)
    expect(groups[1]!.attributes('data-turn-group')).toBe(TURN_2ND)
    expect(wrapper.find('.src-panel-head .count').text()).toBe('2')

    // 点第二轮的 [1]（每轮 ref_no 从 1 起——全局 querySelector 会错命中第一轮组卡）
    // jsdom 无 scrollIntoView（cite 定位的前置 DOM 动作）——原型补桩后断言类注入
    Element.prototype.scrollIntoView = function scrollIntoView(): void {}
    const blocks = wrapper.findAll('[data-testid="turn-block"]')
    await blocks[1]!.find('sup.cite').trigger('click')
    await flushPromises()

    const g1Card1 = groups[0]!.find('[data-source-card="1"]')
    const g2Card1 = groups[1]!.find('[data-source-card="1"]')
    expect(g2Card1.classes()).toContain('cite-hit') // 组作用域：命中第二轮组卡
    expect(g1Card1.classes()).not.toContain('cite-hit') // 第一轮同号卡不受影响
  })

  it('打开即已读流转（REQ-060/A44）：含未读线程的侧栏最近项在进入答案页后未读点消失', async () => {
    const unreadItems = [{ ...threadListFixture.items[0]!, is_unread: true }, ...threadListFixture.items.slice(1)]
    mockedGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/models') return modelsFixture
      if (url === '/api/v1/threads') return { total: unreadItems.length, items: unreadItems }
      if (url === `/api/v1/threads/${THREAD_ID_FAST}`) return threadADetailRunning
      if (url === `/api/v1/turns/${TURN_ID_FAST}`) return turnDetailFastRunning
      throw new Error(`unexpected GET ${url}`)
    })
    wrapper = mount(App, { global: { plugins: [createPinia(), router, i18n] } })
    await flushPromises()
    // 侧栏未读点在
    expect(wrapper.find(`[data-testid="sb-unread-${THREAD_ID_FAST}"]`).exists()).toBe(true)
    // 打开该线程详情（GET 详情服务端置已读——前端本地清点）
    await router.push(`/thread/${THREAD_ID_FAST}`)
    await flushPromises()
    expect(wrapper.find(`[data-testid="sb-unread-${THREAD_ID_FAST}"]`).exists()).toBe(false)
  })

  it('onCite 防御守卫（P3-3/安全审计纵深防御）：非 UUID turnId 的 [n] 点击不定位不高亮、零异常', async () => {
    // 带来源的轮但 turn_id 非 UUID（2.3 骨架数据越界形态——路由级 isUuid 只守 threadId，
    // 轮 id 的收口在 onCite 选择器拼接前）
    const badTurn = 'not-a-uuid-turn'
    const badTurnDetail: typeof turnDetailFastCompleted = {
      ...turnDetailFastCompleted,
      turn_id: badTurn,
    }
    mockedGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/models') return modelsFixture
      if (url === '/api/v1/threads') return threadListFixture
      if (url === `/api/v1/threads/${THREAD_ID_B}`) {
        return {
          thread_id: THREAD_ID_B,
          title: THREAD_TITLE_B,
          mode_summary: 'fast',
          turn_count: 1,
          has_artifacts: false,
          created_at: '2026-10-30T10:00:00Z',
          updated_at: '2026-10-30T10:03:21Z',
          turns: [
            { turn_id: badTurn, mode: 'fast', status: 'completed', query: turnDetailFastCompleted.query, query_lang: 'zh', has_report: false, degraded: false, duration_ms: 7_420, created_at: '2026-10-30T10:00:00Z' },
          ],
        }
      }
      if (url === `/api/v1/turns/${badTurn}`) return badTurnDetail
      throw new Error(`unexpected GET ${url}`)
    })
    Element.prototype.scrollIntoView = function scrollIntoView(): void {}
    wrapper = mount(App, { global: { plugins: [createPinia(), router, i18n] } })
    await router.push(`/thread/${THREAD_ID_B}`)
    await flushPromises()
    // 组已渲染（data-turn-group=非 UUID 值——面板聚合不受守卫影响）
    expect(wrapper.find(`[data-turn-group="${badTurn}"]`).exists()).toBe(true)
    // 点 [n]：守卫静默返回——无 cite-hit、无异常（未处理错误会让 vitest 判红）
    await wrapper.find('sup.cite').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.cite-hit')).toHaveLength(0)
  })
})

describe('来源面板跟随当前查看轮（activeTurnId 派生接线；Perplexity 对齐小批）', () => {
  /** 线程 D：两轮各带来源（滚动 → 锚点激活 → activeTurnId → 面板跟随的端到端接线面） */
  const TURN_F2 = 'aaaa2222-2222-4222-8222-222222222222'

  it('滚动答案列 → 锚点派生 activeTurnId → 组头 group-active 高亮 + 组容器 scrollIntoView(nearest)（锚点行为零变更）', async () => {
    const turn2: typeof turnDetailFastCompleted = {
      ...turnDetailFastCompleted,
      turn_id: TURN_F2,
      query: '第二轮追问：如何评估检索融合效果？',
      answer_md: '第二轮综合回答 [1]。',
      created_at: '2026-10-30T10:02:00Z',
    }
    mockedGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/models') return modelsFixture
      if (url === '/api/v1/threads') return threadListFixture
      if (url === `/api/v1/threads/${THREAD_ID_B}`) {
        return {
          thread_id: THREAD_ID_B,
          title: THREAD_TITLE_B,
          mode_summary: 'fast',
          turn_count: 2,
          has_artifacts: false,
          created_at: '2026-10-30T10:00:00Z',
          updated_at: '2026-10-30T10:03:21Z',
          turns: [
            { turn_id: TURN_ID_FAST, mode: 'fast', status: 'completed', query: turnDetailFastCompleted.query, query_lang: 'zh', has_report: false, degraded: false, duration_ms: 7_420, created_at: '2026-10-30T10:00:00Z' },
            { turn_id: TURN_F2, mode: 'fast', status: 'completed', query: turn2.query, query_lang: 'zh', has_report: false, degraded: false, duration_ms: 5_100, created_at: '2026-10-30T10:02:00Z' },
          ],
        }
      }
      if (url === `/api/v1/turns/${TURN_ID_FAST}`) return turnDetailFastCompleted
      if (url === `/api/v1/turns/${TURN_F2}`) return turn2
      throw new Error(`unexpected GET ${url}`)
    })

    const targets: Element[] = []
    const stub = vi.fn(function (this: Element) {
      targets.push(this)
    })
    Element.prototype.scrollIntoView = stub as typeof Element.prototype.scrollIntoView

    wrapper = mount(App, { attachTo: document.body, global: { plugins: [createPinia(), router, i18n] } })
    await router.push(`/thread/${THREAD_ID_B}`)
    await flushPromises()

    // 初始态：未滚动 → 无高亮、零定位（跟随只在锚点激活变化后生效）
    expect(wrapper.findAll('.src-group')).toHaveLength(2)
    expect(wrapper.findAll('.group-active')).toHaveLength(0)
    expect(targets).toHaveLength(0)

    // 滚动答案列 → onScrollForAnchor（rAF stub 16ms）→ activeAnchorId=末位锚点 → activeTurnId=TURN_F2
    const scrollEl = wrapper.find('[data-testid="answer-scroll"]').element as HTMLElement
    scrollEl.dispatchEvent(new Event('scroll'))
    await new Promise((resolve) => setTimeout(resolve, 30))
    await flushPromises()

    // jsdom 全零几何：所有锚点 top-containerTop=0 ≤96 → active=末位锚点（第 2 轮）
    const titles = wrapper.findAll('.src-group-title')
    expect(titles).toHaveLength(2)
    expect(titles[0]!.classes()).not.toContain('group-active')
    expect(titles[1]!.classes()).toContain('group-active')
    expect(targets).toHaveLength(1)
    expect(targets[0]!.getAttribute('data-turn-group')).toBe(TURN_F2)
    expect(vi.mocked(Element.prototype.scrollIntoView).mock.calls[0]![0]).toEqual({ block: 'nearest' })
  })
})

describe('折叠态联动（评审 P1/P2-1：折叠胶囊不回退 REQ-003/A40 契约）', () => {
  /** 线程 E：两轮各带来源（复用双轮 harness——折叠态 [n]/「来源」操作行的展开+定位断言面） */
  const TURN_E2 = 'bbbb3333-3333-4333-8333-333333333333'

  function installTurns(): void {
    const turn2: typeof turnDetailFastCompleted = {
      ...turnDetailFastCompleted,
      turn_id: TURN_E2,
      query: '第二轮追问：折叠态联动验证。',
      answer_md: '第二轮综合回答 [1]。',
      created_at: '2026-10-30T10:02:00Z',
    }
    mockedGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/models') return modelsFixture
      if (url === '/api/v1/threads') return threadListFixture
      if (url === `/api/v1/threads/${THREAD_ID_B}`) {
        return {
          thread_id: THREAD_ID_B,
          title: THREAD_TITLE_B,
          mode_summary: 'fast',
          turn_count: 2,
          has_artifacts: false,
          created_at: '2026-10-30T10:00:00Z',
          updated_at: '2026-10-30T10:03:21Z',
          turns: [
            { turn_id: TURN_ID_FAST, mode: 'fast', status: 'completed', query: turnDetailFastCompleted.query, query_lang: 'zh', has_report: false, degraded: false, duration_ms: 7_420, created_at: '2026-10-30T10:00:00Z' },
            { turn_id: TURN_E2, mode: 'fast', status: 'completed', query: turn2.query, query_lang: 'zh', has_report: false, degraded: false, duration_ms: 5_100, created_at: '2026-10-30T10:02:00Z' },
          ],
        }
      }
      if (url === `/api/v1/turns/${TURN_ID_FAST}`) return turnDetailFastCompleted
      if (url === `/api/v1/turns/${TURN_E2}`) return turn2
      throw new Error(`unexpected GET ${url}`)
    })
  }

  it('折叠态点正文 [n] → 面板自动展开 + 第二轮组卡 cite-hit（REQ-003 折叠不静默失效）', async () => {
    installTurns()
    wrapper = mount(App, { global: { plugins: [createPinia(), router, i18n] } })
    await router.push(`/thread/${THREAD_ID_B}`)
    await flushPromises()

    await wrapper.find('.src-panel-head').trigger('click')
    expect(wrapper.find('[data-testid="source-pill"]').exists()).toBe(true)

    const blocks = wrapper.findAll('[data-testid="turn-block"]')
    await blocks[1]!.find('sup.cite').trigger('click')
    await flushPromises()

    expect(wrapper.find('.src-frame').exists()).toBe(true)
    expect(wrapper.find('[data-testid="source-pill"]').exists()).toBe(false)
    const groups = wrapper.findAll('.src-group')
    expect(groups[1]!.find('[data-source-card="1"]').classes()).toContain('cite-hit')
    expect(groups[0]!.find('[data-source-card="1"]').classes()).not.toContain('cite-hit')
  })

  it('折叠态点「来源」操作行 → 面板自动展开 + panel-hit 定位高亮（A40 不退化）', async () => {
    installTurns()
    wrapper = mount(App, { global: { plugins: [createPinia(), router, i18n] } })
    await router.push(`/thread/${THREAD_ID_B}`)
    await flushPromises()

    await wrapper.find('.src-panel-head').trigger('click')
    expect(wrapper.find('[data-testid="source-pill"]').exists()).toBe(true)

    await wrapper.findAll('[data-testid="act-sources"]')[0]!.trigger('click')
    await flushPromises()

    expect(wrapper.find('.src-frame').exists()).toBe(true)
    expect(wrapper.find('[data-testid="source-panel"]').classes()).toContain('panel-hit')
  })
})

describe('regenerate 重新生成接线（v1.26 REQ-063/A47：第 8 钮 → store.regenerate → 重订阅）', () => {
  /** degraded-with-sources fast 轮（api.md §2.12 前置形态：可见性门控全部满足） */
  const turnDetailDegraded: typeof turnDetailFastCompleted = {
    ...turnDetailFastCompleted,
    status: 'degraded',
    degraded: true,
    degraded_reason: 'source_failure',
  }

  function installDegraded(): void {
    mockedGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/models') return modelsFixture
      if (url === '/api/v1/threads') return threadListFixture
      if (url === `/api/v1/threads/${THREAD_ID_FAST}`) {
        return {
          thread_id: THREAD_ID_FAST,
          title: 'LangGraph 的 Send API 和 Router 的区别？',
          mode_summary: 'fast' as const,
          turn_count: 1,
          has_artifacts: false,
          created_at: '2026-11-01T08:12:00Z',
          updated_at: '2026-11-01T09:40:11Z',
          turns: [
            {
              turn_id: TURN_ID_FAST,
              mode: 'fast' as const,
              status: 'degraded' as const,
              query: turnDetailDegraded.query,
              query_lang: 'zh' as const,
              has_report: false,
              degraded: true,
              duration_ms: 7_420,
              created_at: '2026-11-01T08:12:00Z',
            },
          ],
        }
      }
      if (url === `/api/v1/turns/${TURN_ID_FAST}`) return turnDetailDegraded
      throw new Error(`unexpected GET ${url}`)
    })
  }

  it('degraded 轮点第 8 钮 → POST §2.12 regenerate → 状态条复用 running（生成中）+ DegradedBanner 退场 + 答案区清空 + 重订阅', async () => {
    installDegraded()
    wrapper = mount(App, { global: { plugins: [createPinia(), router, i18n] } })
    await router.push(`/thread/${THREAD_ID_FAST}`)
    await flushPromises()

    // degraded 原态：横幅 + 第 8 钮可见；终态轮不订阅（无 EventSource）
    expect(wrapper.find('[data-testid="degraded-banner"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="act-regenerate"]').exists()).toBe(true)
    expect(MockEventSource.instances.filter((i) => i.url.includes(TURN_ID_FAST))).toHaveLength(0)

    mockedPost.mockResolvedValueOnce({ turn_id: TURN_ID_FAST, status: 'running' })
    await wrapper.find('[data-testid="act-regenerate"]').trigger('click')
    await flushPromises()

    expect(mockedPost).toHaveBeenCalledWith(`/api/v1/turns/${TURN_ID_FAST}/regenerate`)
    // 重订阅（同通道续推：旧轨迹重放幂等丢弃、二次 done 关流对账——store 层判定单测已锁）
    const streams = MockEventSource.instances.filter((i) => i.url.includes(TURN_ID_FAST))
    expect(streams).toHaveLength(1)
    // 状态条复用 TurnBlock isLive 既有分支（零改动验证项）
    expect(wrapper.find('[data-testid="turn-status"]').text()).toContain('生成中')
    expect(wrapper.find('[data-testid="degraded-banner"]').exists()).toBe(false)
    // 清空重渲：answer_md 清位 → 答案区渲染块退场（新 token 到达后随流式重现）
    expect(wrapper.find('[data-testid="turn-answer"]').exists()).toBe(false)
  })

  it('regenerate running 态复用取消链路（canStop → 提交钮变停止）；Esc/cancel 零改动验证', async () => {
    installDegraded()
    wrapper = mount(App, { global: { plugins: [createPinia(), router, i18n] } })
    await router.push(`/thread/${THREAD_ID_FAST}`)
    await flushPromises()

    mockedPost.mockResolvedValueOnce({ turn_id: TURN_ID_FAST, status: 'running' })
    await wrapper.find('[data-testid="act-regenerate"]').trigger('click')
    await flushPromises()

    // SearchCard streaming=canStop（liveTurn 扫到 running 轮）→ 停止钮形态（提交钮退位）
    const stopBtn = wrapper.find('[data-testid="follow-bar"] [data-testid="stop-btn"]')
    expect(stopBtn.exists()).toBe(true)
    expect(wrapper.find('[data-testid="follow-bar"] [data-testid="submit-btn"]').exists()).toBe(false)

    mockedPost.mockClear()
    mockedPost.mockResolvedValueOnce({ turn_id: TURN_ID_FAST, status: 'canceling' })
    await stopBtn.trigger('click')
    await flushPromises()
    expect(mockedPost).toHaveBeenCalledWith(`/api/v1/turns/${TURN_ID_FAST}/cancel`)
  })

  it('409006：POST 落败 → toast 服务端 message + 轮保持 degraded 原态（横幅/答案区/按钮都在，零重订阅）', async () => {
    installDegraded()
    wrapper = mount(App, { global: { plugins: [createPinia(), router, i18n] } })
    await router.push(`/thread/${THREAD_ID_FAST}`)
    await flushPromises()

    normalizeState.message = '轮次不支持重新生成'
    mockedPost.mockRejectedValueOnce(new Error('409006'))
    await wrapper.find('[data-testid="act-regenerate"]').trigger('click')
    await flushPromises()

    expect(mockedPost).toHaveBeenCalledWith(`/api/v1/turns/${TURN_ID_FAST}/regenerate`)
    expect(wrapper.find('[data-testid="toast-host"]').text()).toContain('轮次不支持重新生成')
    // 原态保持：degraded 横幅 + 答案区 + 第 8 钮（可再次点击）；无重订阅
    expect(wrapper.find('[data-testid="degraded-banner"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="turn-answer"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="act-regenerate"]').exists()).toBe(true)
    expect(MockEventSource.instances.filter((i) => i.url.includes(TURN_ID_FAST))).toHaveLength(0)
  })
})

describe('快照入口接线（批次 6：SourcePanel 快照胶囊 → SnapshotModal 弹窗预览，api.md §5.1）', () => {
  const SOURCE_ID_SNAP = turnDetailDeepCompleted.sources[0]!.source_id
  const SNAPSHOT: SnapshotData = {
    snapshot_id: 'e5cc1111-1111-4111-8111-111111111111',
    url: 'https://example.com/original-page',
    content_type: 'text/html',
    size_bytes: 20480,
    fetched_at: '2026-11-01T08:12:03Z',
    download_url: 'http://127.0.0.1:9000/ai-search-snapshots/e5cc1111?X-Amz-Signature=abc',
    excerpt: '# 原文标题\n\n第一段正文。',
  }

  /** 线程 B（deep 完成态，source 1 snapshot_available=true）+ 快照端点分发 */
  function installWithSnapshot(downloadUrl: string = SNAPSHOT.download_url): void {
    mockedGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/models') return modelsFixture
      if (url === '/api/v1/threads') return threadListFixture
      if (url === `/api/v1/threads/${THREAD_ID_B}`) {
        return {
          thread_id: THREAD_ID_B,
          title: THREAD_TITLE_B,
          mode_summary: 'deep',
          turn_count: 2,
          has_artifacts: false,
          created_at: '2026-10-30T10:00:00Z',
          updated_at: '2026-10-30T10:03:21Z',
          turns: [
            { turn_id: TURN_ID_DEEP, mode: 'deep', status: turnDetailDeepCompleted.status, query: turnDetailDeepCompleted.query, query_lang: 'zh', has_report: true, degraded: false, duration_ms: 62_000, created_at: '2026-10-30T10:00:00Z' },
            { turn_id: TURN_ID_DIRECT, mode: 'fast', status: turnDetailDirectCompleted.status, query: turnDetailDirectCompleted.query, query_lang: 'zh', has_report: false, degraded: false, duration_ms: 3_200, created_at: '2026-10-30T10:02:00Z' },
          ],
        }
      }
      if (url === `/api/v1/turns/${TURN_ID_DEEP}`) return turnDetailDeepCompleted
      if (url === `/api/v1/turns/${TURN_ID_DIRECT}`) return turnDetailDirectCompleted
      if (url === `/api/v1/sources/${SOURCE_ID_SNAP}/snapshot`) {
        return { ...SNAPSHOT, download_url: downloadUrl }
      }
      throw new Error(`unexpected GET ${url}`)
    })
  }

  it('点快照胶囊 → SnapshotModal 打开（Teleport body：excerpt/元数据/打开原文外链）→ Esc 关闭', async () => {
    installWithSnapshot()
    wrapper = mount(App, { global: { plugins: [createPinia(), router, i18n] } })
    await router.push(`/thread/${THREAD_ID_B}`)
    await flushPromises()

    // 深度轮 source 1 快照胶囊在位（snapshot_available=true）
    const snapIcon = wrapper.find('[data-testid="snapshot-1"]')
    expect(snapIcon.exists()).toBe(true)
    await snapIcon.trigger('click')
    await flushPromises()

    // 弹窗内容（Teleport 到 body——wrapper.find 不可达，走 document 查询）
    const excerpt = document.querySelector('[data-testid="snapshot-excerpt"]')
    expect(excerpt).not.toBeNull()
    expect(excerpt!.textContent).toContain('# 原文标题')
    expect(excerpt!.textContent).toContain('第一段正文。')
    const openOriginal = document.querySelector<HTMLAnchorElement>('[data-testid="snapshot-open-original"]')
    expect(openOriginal?.getAttribute('href')).toBe(SNAPSHOT.download_url)
    expect(document.querySelector('[data-testid="snapshot-meta"]')).not.toBeNull()

    // Esc 关闭（window.open 直开旧形态 → 弹窗化：关闭通道复用 ThreadModal）
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await flushPromises()
    expect(document.querySelector('[data-testid="snapshot-excerpt"]')).toBeNull()
  })

  it('download_url 非 http（协议白名单守卫）：快照地址不可用 toast 保留 + 弹窗仍打开（摘录预览为主面）且无外链钮', async () => {
    installWithSnapshot('javascript:alert(1)')
    wrapper = mount(App, { global: { plugins: [createPinia(), router, i18n] } })
    await router.push(`/thread/${THREAD_ID_B}`)
    await flushPromises()

    await wrapper.find('[data-testid="snapshot-1"]').trigger('click')
    await flushPromises()

    // 不可用 toast（既有链路保留——answer.snapshotUnavailable）
    expect(wrapper.find('[data-testid="toast-host"]').text()).toContain('快照地址不可用')
    // 摘录预览不因外链不可用而缺席；外链钮被 isHttpUrl 守卫隐藏
    expect(document.querySelector('[data-testid="snapshot-excerpt"]')).not.toBeNull()
    expect(document.querySelector('[data-testid="snapshot-open-original"]')).toBeNull()
  })
})

describe('溯源下钻守卫（CR-6/SA-2：§4.6 路径参数 sourceId 进 URL 前过 isUuid）', () => {
  const KB_TURN = 'cccc4444-4444-4444-8444-444444444444'
  const KB_CHUNK: KbChunkDetail = {
    chunk_id: '5c2e0001-0000-4000-8000-000000000001',
    document_id: '9a770001-0000-4000-8000-000000000002',
    document_title: '内部设计文档-检索融合.md',
    content: 'KB 候选与网络候选合并后统一 rerank...',
    heading_path: ['第三章 检索融合'],
    page: 12,
    paragraph: 4,
  }
  const KB_DOC: KbDocument = {
    document_id: KB_CHUNK.document_id,
    doc_type: 'md',
    title: '内部设计文档-检索融合.md',
    file_size_bytes: 102_400,
    status: 'ready',
    fail_reason: null,
    chunk_count: 42,
    created_at: '2026-11-01T08:12:00Z',
    parse_meta: { pages: null, tables: 3, code_blocks: 2 },
    catalog_path: null,
  }

  function kbSource(sourceId: string, kbChunkId: string): Source {
    return {
      source_id: sourceId,
      ref_no: 1,
      source_type: 'kb',
      title: '内部设计文档-检索融合.md',
      url: null,
      snippet: 'KB 候选与网络候选合并后统一 rerank...',
      score: 0.8871,
      snapshot_available: false,
      kb_doc_id: KB_DOC.document_id,
      kb_chunk_id: kbChunkId,
    }
  }

  function installKbThread(source: Source): void {
    mockedGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/models') return modelsFixture
      if (url === '/api/v1/threads') return threadListFixture
      if (url === `/api/v1/threads/${THREAD_ID_B}`) {
        return {
          thread_id: THREAD_ID_B,
          title: THREAD_TITLE_B,
          mode_summary: 'fast',
          turn_count: 1,
          has_artifacts: false,
          created_at: '2026-10-30T10:00:00Z',
          updated_at: '2026-10-30T10:03:21Z',
          turns: [
            { turn_id: KB_TURN, mode: 'fast', status: 'completed', query: 'KB 溯源守卫用例', query_lang: 'zh', has_report: false, degraded: false, duration_ms: 3_200, created_at: '2026-10-30T10:02:00Z' },
          ],
        }
      }
      if (url === `/api/v1/turns/${KB_TURN}`) {
        return { ...turnDetailFastCompleted, turn_id: KB_TURN, answer_md: 'KB 溯源回答 [1]。', sources: [source] }
      }
      if (url === `/api/v1/sources/${source.source_id}/kb-chunk`) return KB_CHUNK
      if (url === `/api/v1/kb/documents/${KB_CHUNK.document_id}`) return KB_DOC
      throw new Error(`unexpected GET ${url}`)
    })
  }

  it('非法 sourceId → 守卫拦截零请求、弹窗不开（§4.6 路径参数进 URL 前过 isUuid，与 threadId 纪律对称）', async () => {
    installKbThread(kbSource('not-a-uuid-source', '5c2e0001-0000-4000-8000-000000000001'))
    wrapper = mount(App, { global: { plugins: [createPinia(), router, i18n] } })
    await router.push(`/thread/${THREAD_ID_B}`)
    await flushPromises()
    mockedGet.mockClear()
    await wrapper.find('[data-testid="source-kb-drill"]').trigger('click')
    await flushPromises()
    // 守卫拦截：零请求（dispatcher 对未预期 URL 恒 throw——若守卫失守触网此处必红）；弹窗不开
    expect(mockedGet).not.toHaveBeenCalled()
    expect(document.querySelector('[data-testid="kb-chunk-drilldown"]')).toBeNull()
  })

  it('合法 sourceId + 非 UUID kb_chunk_id（legacy 形态）→ §4.6 请求照发（kbChunkId 无消费面、不再是前置门）', async () => {
    installKbThread(kbSource('f00d9001-0000-4000-8000-000000000001', 'legacy-chunk-ref'))
    wrapper = mount(App, { global: { plugins: [createPinia(), router, i18n] } })
    await router.push(`/thread/${THREAD_ID_B}`)
    await flushPromises()
    mockedGet.mockClear()
    await wrapper.find('[data-testid="source-kb-drill"]').trigger('click')
    await flushPromises()
    expect(mockedGet).toHaveBeenCalledWith('/api/v1/sources/f00d9001-0000-4000-8000-000000000001/kb-chunk')
    expect(document.querySelector('[data-testid="kb-chunk-drilldown"]')).not.toBeNull()
  })
})

describe('B22 HITL 审批门接线（v1.40 批次 3 第三段：刷新恢复弹层重开 + awaiting 订阅 + 回看折叠条）', () => {
  /** 审批门线程 2.3 骨架（status=awaiting_approval|running 复用） */
  function approvalThreadDetail(status: 'awaiting_approval' | 'running') {
    return {
      thread_id: THREAD_ID_FAST,
      title: '审批门线程',
      mode_summary: 'fast' as const,
      turn_count: 1,
      has_artifacts: false,
      created_at: '2026-11-01T08:12:00Z',
      updated_at: '2026-11-01T09:40:11Z',
      turns: [
        {
          turn_id: TURN_ID_AWAITING,
          mode: 'fast' as const,
          status,
          query: 'KB 里怎么融合检索？',
          query_lang: 'zh' as const,
          has_report: false,
          degraded: false,
          duration_ms: 0,
          created_at: '2026-11-01T08:12:00Z',
        },
      ],
    }
  }

  it('awaiting 轮线程加载（刷新恢复）：2.5 对账 approval=pending → ApprovalSheet 弹层重开（恢复只依赖 2.5 不依赖事件重放，§3 生命周期 15）+ awaiting 轮照常建立订阅（api.md §7.1 等待期事件流——裁决后 approval_resolved/阶段 2 事件可达）', async () => {
    mockedGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/models') return modelsFixture
      if (url === '/api/v1/threads') return threadListFixture
      if (url === `/api/v1/threads/${THREAD_ID_FAST}`) return approvalThreadDetail('awaiting_approval')
      if (url === `/api/v1/turns/${TURN_ID_AWAITING}`) return turnDetailFastAwaiting
      throw new Error(`unexpected GET ${url}`)
    })
    wrapper = mount(App, { global: { plugins: [createPinia(), router, i18n] } })
    await router.push(`/thread/${THREAD_ID_FAST}`)
    await flushPromises()

    // 刷新恢复：2.5 approval=pending 全量在 payload → 弹层重开（Teleport body 壳面板）
    const panel = document.querySelector('[data-testid="hitl-sheet"]')
    expect(panel).not.toBeNull()
    expect(panel!.getAttribute('aria-label')).toBe('知识库里找到相关内容')
    expect(panel!.textContent).toContain('内部设计文档-检索融合.md') // 预览数据随 2.5 恢复
    // awaiting 轮照常建立订阅（等待期事件流；此前仅 running|queued 订阅为刷新恢复缺口）
    expect(MockEventSource.instances.some((i) => i.url.includes(TURN_ID_AWAITING))).toBe(true)
    // 占线判定面互不侵入（v1.40 第四段补）：liveTurn/canStop 仍只认 running|queued——
    // awaiting 态停止钮不出现（停止语义由弹层「取消本轮」承接，§3 生命周期 15）、
    // 提交钮在位（追问条非占线形态，placeholder 非 followDisabledHint）
    expect(wrapper!.find('[data-testid="follow-bar"] [data-testid="stop-btn"]').exists()).toBe(false)
    expect(wrapper!.find('[data-testid="follow-bar"] [data-testid="submit-btn"]').exists()).toBe(true)
  })

  it('approved 回看轮：无弹层 + 问题下方折叠条渲染；no_hit（web 直达）轮零审批痕迹', async () => {
    mockedGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/models') return modelsFixture
      if (url === '/api/v1/threads') return threadListFixture
      if (url === `/api/v1/threads/${THREAD_ID_FAST}`) return approvalThreadDetail('running')
      if (url === `/api/v1/turns/${TURN_ID_AWAITING}`) return turnDetailFastApproved
      throw new Error(`unexpected GET ${url}`)
    })
    wrapper = mount(App, { global: { plugins: [createPinia(), router, i18n] } })
    await router.push(`/thread/${THREAD_ID_FAST}`)
    await flushPromises()

    // approved 形态不渲染弹层（回看态折叠条不开弹层——§3 生命周期 15 刷新恢复句）
    expect(document.querySelector('[data-testid="hitl-sheet"]')).toBeNull()
    // 折叠条在 TurnBlock 内（VTU 挂载为 detached DOM——组件域断言走 wrapper.find；
    // document.querySelector 仅可达 Teleport body 内容）
    const rail = wrapper!.find('[data-testid="approval-recall-strip"]')
    expect(rail.exists()).toBe(true)
    expect(rail.text()).toContain('已按『仅知识库』回答')
    wrapper!.unmount()
    wrapper = null
    MockEventSource.reset()
    await flushPromises()

    // no_hit（web 直达轮）：零弹层零折叠条
    mockedGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/models') return modelsFixture
      if (url === '/api/v1/threads') return threadListFixture
      if (url === `/api/v1/threads/${THREAD_ID_FAST}`) return approvalThreadDetail('running')
      if (url === `/api/v1/turns/${TURN_ID_AWAITING}`) return turnDetailFastNoHit
      throw new Error(`unexpected GET ${url}`)
    })
    wrapper = mount(App, { global: { plugins: [createPinia(), router, i18n] } })
    await router.push(`/thread/${THREAD_ID_FAST}`)
    await flushPromises()
    expect(document.querySelector('[data-testid="hitl-sheet"]')).toBeNull()
    expect(wrapper!.find('[data-testid="approval-recall-strip"]').exists()).toBe(false)
  })
})
