/**
 * test/integration/scroll-follow.test.ts — 流式输出中的接管与恢复（REQ-043 / A26，§5 v1.8）。
 *
 * 联装形态：AnswerView 真身（真实 DOM 滚动容器 + 真实 scroll 事件）+ turnStore/turnStream 真身
 * + MockEventSource 驱动 SSE 帧 + REST mock + 滚动度量 stub（jsdom 无布局引擎——
 * scrollHeight/clientHeight 以可控 getter 注入，scrollTop 以 clamp setter 仿真；这是 jsdom
 * DOM 滚动测试的标准手法，与 unit 层纯替身互补）。
 *
 * 断言链（对应 A26 四条 GWT）：
 *   1. 贴底跟随：流式 token 到达 → 视口自动保持贴底（无布局跳动的即时赋值路径）；
 *   2. 接管：上滚 >8px → 自动跟随立即停止，流式内容继续增长（渲染不中断语义）；
 *   3. 重叠带消歧：接管后距底变化仅由内容增长导致 → 不被拽回；
 *   4. 恢复三途径：① 用户主动下滚进贴底区 ② ScrollToBottomFab 点击 ③ 发送新消息（followUp）。
 */
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'

import { apiGet, apiPost } from '@/api/client'
import App from '@/App.vue'
import { i18n } from '@/i18n'
import { modelsFixture } from '@/mocks/models'
import {
  THREAD_ID_FAST,
  TURN_ID_DEEP,
  TURN_ID_FAST,
  turnDetailFastCompleted,
} from '@/mocks/turnDetails'
import { threadListFixture } from '@/mocks/threads'
import { router } from '@/router'

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

const RUNNING_TURN: typeof turnDetailFastCompleted = {
  ...turnDetailFastCompleted,
  status: 'running',
  answer_md: '',
  sources: [],
  images: [],
  stages: [],
  duration_ms: 0,
  finished_at: null,
  followups: null,
}

/** wire fixture → EventSource 线格帧 */
function frame(seq: number, delta: string): { id: number; type: string; data: string } {
  return {
    id: seq,
    type: 'token',
    data: JSON.stringify({ seq, turn_id: TURN_ID_FAST, ts: '2026-11-01T08:12:05Z', delta }),
  }
}

describe('流式滚动：接管与恢复（REQ-043 / A26）', () => {
  let wrapper: VueWrapper | null = null
  let scrollTopValue = 0
  const metrics = { h: 800, ch: 400 }
  let scrollContainer: HTMLElement | null = null

  beforeEach(() => {
    vi.clearAllMocks()
    MockEventSource.reset()
    vi.stubGlobal('EventSource', MockEventSource)
    // streaming-buffer 规则 8 的测试等价物：rAF → setTimeout(16)（真计时器 + vi.waitFor）
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => setTimeout(() => cb(0), 16))
    localStorage.clear()
    sessionStorage.clear()
    i18n.global.locale.value = 'zh-CN'
    scrollTopValue = 0
    metrics.h = 800
    metrics.ch = 400
  })

  afterEach(async () => {
    wrapper?.unmount()
    wrapper = null
    scrollContainer = null
    await nextTick()
    vi.unstubAllGlobals()
  })

  async function mountOnThread(): Promise<VueWrapper> {
    // 可变轮详情：done 对账（REST 权威）时服务端事实已变化——mock 以可变对象模拟（就地变异）
    const turnDetailState: typeof RUNNING_TURN = { ...RUNNING_TURN }
    mockedGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/threads') return threadListFixture
      if (url === '/api/v1/models') return modelsFixture
      if (url === `/api/v1/threads/${THREAD_ID_FAST}`) {
        return {
          thread_id: THREAD_ID_FAST,
          title: '流式滚动集成夹具线程',
          mode_summary: 'fast',
          turn_count: 1,
          has_artifacts: false,
          created_at: '2026-11-01T08:12:00Z',
          updated_at: '2026-11-01T09:40:11Z',
          turns: [
            {
              turn_id: TURN_ID_FAST,
              mode: 'fast',
              status: turnDetailState.status,
              query: RUNNING_TURN.query,
              query_lang: 'zh',
              has_report: false,
              degraded: false,
              duration_ms: 0,
              created_at: '2026-11-01T08:12:00Z',
            },
          ],
        }
      }
      if (url === `/api/v1/turns/${TURN_ID_FAST}`) return turnDetailState
      throw new Error(`unexpected GET ${url}`)
    })
    ;(mountOnThread as unknown as { __state?: { setDone(): void } }).__state = {
      setDone: () => {
        turnDetailState.status = 'completed'
        turnDetailState.answer_md = '终态落库内容'
        turnDetailState.duration_ms = 7420
      },
    }
    mockedPost.mockResolvedValue({})

    setActivePinia(createPinia())
    await router.push('/')
    await router.isReady()
    wrapper = mount(App, { global: { plugins: [createPinia(), router, i18n] } })
    await router.push(`/thread/${THREAD_ID_FAST}`)
    await flushPromises()
    await nextTick()

    scrollContainer = wrapper.find('[data-testid="answer-scroll"]').element as HTMLElement
    // jsdom 无布局：注入滚动度量（controller 经属性动态读取，mount 后注入同样生效）
    Object.defineProperty(scrollContainer, 'scrollHeight', { configurable: true, get: () => metrics.h })
    Object.defineProperty(scrollContainer, 'clientHeight', { configurable: true, get: () => metrics.ch })
    Object.defineProperty(scrollContainer, 'scrollTop', {
      configurable: true,
      get: () => scrollTopValue,
      set: (v: number) => {
        scrollTopValue = Math.max(0, Math.min(v, metrics.h - metrics.ch))
      },
    })
    scrollContainer.scrollTo = vi.fn() // jsdom 无 Element.scrollTo
    return wrapper
  }

  /** 模拟用户滚动（先赋值后派发真实 scroll 事件——等价用户拖滚动条/滚轮） */
  function userScroll(top: number): void {
    if (!scrollContainer) throw new Error('no scroll container')
    scrollContainer.scrollTop = top
    scrollContainer.dispatchEvent(new Event('scroll'))
  }

  /** 已渲染答案文本（经 wrapper 作用域——test-utils mount 挂在游离 div，document 查不到） */
  function rendererText(app: VueWrapper): string {
    return app.find('[data-testid="answer-renderer"]').text()
  }

  /** 流式 token：内容增长 + SSE 帧派发 */
  async function streamToken(app: VueWrapper, seq: number, delta: string, newHeight: number): Promise<void> {
    metrics.h = newHeight
    const es = MockEventSource.instances[0]!
    es.dispatch(frame(seq, delta))
    // token 已入渲染管线（真实 DOM 文本出现）——以 DOM 状态断言「流式不中断」
    await vi.waitFor(
      () => {
        expect(rendererText(app)).toContain(delta)
      },
      { timeout: 3_000, interval: 20 },
    )
  }

  it('贴底跟随 → 上滚接管 → 重叠带不拽回 → 三途径恢复', async () => {
    const app = await mountOnThread()

    const es = MockEventSource.instances[0]!
    expect(es.url).toContain(`/api/v1/turns/${TURN_ID_FAST}/events`)
    es.open()

    // ── A26-1 贴底跟随 ──
    await streamToken(app, 1, 'Send API 用于', 900)
    await vi.waitFor(
      () => {
        expect(scrollTopValue).toBe(500) // clamp(900) = 900 - 400：自动贴底
      },
      { timeout: 3_000, interval: 20 },
    )

    await streamToken(app, 2, '动态扇出', 1100)
    await vi.waitFor(
      () => {
        expect(scrollTopValue).toBe(700) // 持续跟随
      },
      { timeout: 3_000, interval: 20 },
    )

    // ── A26-2 接管：上滚 >8px（一次 -300）→ 自动跟随立即停止；流式内容继续增长 ──
    userScroll(300)
    await streamToken(app, 3, 'Router 用于静态分支', 1400)
    expect(rendererText(app)).toContain('Router 用于静态分支')
    expect(scrollTopValue).toBe(300) // 未被拽回（渲染照常、仅停止自动滚动）

    // ── A26-4 消歧：重叠带/内容增长导致的距底变化不恢复 ──
    await streamToken(app, 4, '更多内容', 1700)
    await nextTick()
    expect(scrollTopValue).toBe(300)

    // ── 恢复①：用户主动下滚进入贴底区 ──
    userScroll(1300) // 距底 = 1700-1300-400 = 0 < 40
    await streamToken(app, 5, '恢复后继续', 2000)
    await vi.waitFor(
      () => {
        expect(scrollTopValue).toBe(1600) // 恢复跟随 → 贴底 clamp(2000)
      },
      { timeout: 3_000, interval: 20 },
    )

    // ── 恢复②：ScrollToBottomFab 点击（先制造离底超一屏 + Fab 可见）──
    userScroll(200) // 距底 = 2000-200-400 = 1400 > 一屏 400 → showFab
    await vi.waitFor(
      () => {
        expect(app.find('[data-testid="scroll-fab"]').exists()).toBe(true)
      },
      { timeout: 3_000, interval: 20 },
    )
    await app.find('[data-testid="scroll-fab"]').trigger('click')
    // 恢复后内容增长继续跟随（smooth 滚动由真实浏览器完成；断言状态机恢复）
    await streamToken(app, 6, 'Fab 恢复后内容', 2300)
    await vi.waitFor(
      () => {
        expect(scrollTopValue).toBe(1900) // clamp(2300) = 1900
      },
      { timeout: 3_000, interval: 20 },
    )

    // ── 恢复③：发送新消息（followUp → 新一轮；运行中提交钮=停止（§2），先让轮到终态）──
    const es2 = MockEventSource.instances[0]!
    es2.dispatch({
      id: 7,
      type: 'done',
      data: JSON.stringify({
        seq: 7,
        turn_id: TURN_ID_FAST,
        ts: '2026-11-01T08:12:11Z',
        finish: 'completed',
        degraded: false,
        degraded_reason: null,
        duration_ms: 7420,
        source_count: 0,
        usage: { total_tokens: 1, cost_usd: 0 },
      }),
    })
    ;(mountOnThread as unknown as { __state?: { setDone(): void } }).__state?.setDone()
    // done → 对账（REST 权威 completed）→ canStop=false → 提交钮回归
    await vi.waitFor(
      () => {
        expect(app.find('[data-testid="follow-bar"] [data-testid="submit-btn"]').exists()).toBe(true)
      },
      { timeout: 3_000, interval: 20 },
    )
    userScroll(400) // 上滚 1500 → 接管
    await nextTick()
    expect(scrollTopValue).toBe(400)
    mockedPost.mockResolvedValueOnce({
      thread_id: THREAD_ID_FAST,
      turn_id: TURN_ID_DEEP,
      status: 'running',
    })
    await app.find('[data-testid="follow-bar"] textarea').setValue('追问一轮')
    await app.find('[data-testid="follow-bar"] [data-testid="submit-btn"]').trigger('click')
    await flushPromises()
    // notifyNewTurn → 恢复跟随 + 即时贴底
    await vi.waitFor(
      () => {
        expect(scrollTopValue).toBe(1900) // 即时贴底（h=2300, max=1900）
      },
      { timeout: 3_000, interval: 20 },
    )
    // 新一轮已建流（两段式第二段）
    expect(MockEventSource.instances.some((i) => i.url.includes(TURN_ID_DEEP))).toBe(true)
  })
})
