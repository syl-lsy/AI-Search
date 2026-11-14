/**
 * test/integration/direct-turn.test.ts — 「收集中」门控整页渲染：direct 轮负向 + 常规 fast 轮
 * 正向（frontend-api.md §3 v1.15 渲染口径，§9 fixture 驱动；api.md §7.4 v1.14 direct 形态）。
 *
 * 联装形态：AnswerView 真身（App + router）+ turnStore/turnStream 真身 + MockEventSource
 * 驱动 SSE 帧 + REST mock（harness 同 scroll-follow.test.ts 手法）。
 *
 * 断言链（§3 派发表 stage/sources 行 v1.15 注 + §9 direct 判定点）：
 *   1. direct 轮：时间线仅 rewrite/write 两节点（缺 search key 即缺节点——stage_key 驱动天然支持）；
 *   2. direct 轮：不渲染来源面板与「收集中」徽标（流式中段与终态、回看态三口径——无空态、
 *      无「来源 0」占位）；
 *   3. direct 轮：答案正文正常渲染（无 [n] 悬挂引用）、无图片区块、无思考流区块、状态行无「来源 N」计数；
 *   4. direct 轮：followups 追加（③）：store 投影后页面无错误、答案区不受影响
 *      （FollowupChips 渲染随 M2/M4 工单落位，本批断言面 = 不渲染空态/无错误提示）；
 *   5. 常规 fast 轮（B9 测试审计 P1 补口——门控收紧 v1.15 注的负向对偶面）：running 轮
 *      stage(search,running) 且 sources 为空 → 面板已在且含「收集中」徽标（不等首批 sources）；
 *      stage(search,done) 后收集态不掉、面板不塌；终态 REST 对账后面板与来源卡正常、徽标熄灭。
 */
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'

import { apiGet } from '@/api/client'
import App from '@/App.vue'
import { i18n } from '@/i18n'
import { modelsFixture } from '@/mocks/models'
import { THREAD_ID_FAST, TURN_ID_DIRECT, TURN_ID_FAST, turnDetailDirectCompleted, turnDetailFastCompleted } from '@/mocks/turnDetails'
import { threadListFixture } from '@/mocks/threads'
import { directFollowupsEvent, directSequence, fastSequence } from '@/mocks/sseSequences'
import { router } from '@/router'
import type { SseWireEvent } from '@/types/api'

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

/** wire fixture → EventSource 线格帧 */
function toFrames(events: SseWireEvent[]) {
  return events.map((e) => ({ id: e.id, type: e.event, data: JSON.stringify(e.data) }))
}

/** 运行中种子（2.5 对账已持久化投影——流式内容由序列重放重建） */
const runningDirectDetail = {
  ...turnDetailDirectCompleted,
  status: 'running' as const,
  answer_md: '',
  stages: [],
  duration_ms: 0,
  finished_at: null,
}

let wrapper: VueWrapper | null = null
let terminalNow = false // done 对账（REST 权威）时服务端事实翻转通道

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  sessionStorage.clear()
  vi.clearAllMocks()
  MockEventSource.reset()
  vi.stubGlobal('EventSource', MockEventSource)
  // streaming-buffer 规则 8 的测试等价物：rAF → setTimeout(16)（真计时器 + vi.waitFor）
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => setTimeout(() => cb(0), 16))
  i18n.global.locale.value = 'zh-CN'
  terminalNow = false
})

afterEach(async () => {
  wrapper?.unmount()
  wrapper = null
  await nextTick()
  vi.unstubAllGlobals()
})

async function mountOnThread(turnStatus: 'running' | 'completed'): Promise<VueWrapper> {
  mockedGet.mockImplementation(async (url: string) => {
    if (url === '/api/v1/threads') return threadListFixture
    if (url === '/api/v1/models') return modelsFixture
    if (url === `/api/v1/threads/${THREAD_ID_FAST}`) {
      return {
        thread_id: THREAD_ID_FAST,
        title: 'direct 轮集成夹具线程',
        mode_summary: 'fast',
        turn_count: 1,
        has_artifacts: false,
        created_at: '2026-11-01T08:15:00Z',
        updated_at: '2026-11-01T08:15:02Z',
        turns: [
          {
            turn_id: TURN_ID_DIRECT,
            mode: 'fast',
            status: turnStatus,
            query: turnDetailDirectCompleted.query,
            query_lang: 'zh',
            has_report: false,
            degraded: false,
            duration_ms: turnStatus === 'running' ? 0 : turnDetailDirectCompleted.duration_ms,
            created_at: '2026-11-01T08:15:00Z',
          },
        ],
      }
    }
    if (url === `/api/v1/turns/${TURN_ID_DIRECT}`) {
      return turnStatus === 'completed' || terminalNow ? turnDetailDirectCompleted : runningDirectDetail
    }
    throw new Error(`unexpected GET ${url}`)
  })

  await router.push('/')
  await router.isReady()
  wrapper = mount(App, { global: { plugins: [createPinia(), router, i18n] } })
  await router.push(`/thread/${THREAD_ID_FAST}`)
  await flushPromises()
  await nextTick()
  return wrapper
}

/** 常规 fast 轮挂载（同 harness：运行中轮 2.5 种子 sources/stages 空 + MockEventSource 驱动 §7.4 序列） */
async function mountOnFastThread(): Promise<VueWrapper> {
  mockedGet.mockImplementation(async (url: string) => {
    if (url === '/api/v1/threads') return threadListFixture
    if (url === '/api/v1/models') return modelsFixture
    if (url === `/api/v1/threads/${THREAD_ID_FAST}`) {
      return {
        thread_id: THREAD_ID_FAST,
        title: 'fast 轮收集态集成夹具线程',
        mode_summary: 'fast',
        turn_count: 1,
        has_artifacts: false,
        created_at: '2026-11-01T08:12:00Z',
        updated_at: '2026-11-01T08:12:11Z',
        turns: [
          {
            turn_id: TURN_ID_FAST,
            mode: 'fast',
            status: 'running',
            query: turnDetailFastCompleted.query,
            query_lang: 'zh',
            has_report: false,
            degraded: false,
            duration_ms: 0,
            created_at: '2026-11-01T08:12:00Z',
          },
        ],
      }
    }
    if (url === `/api/v1/turns/${TURN_ID_FAST}`) {
      return {
        ...turnDetailFastCompleted,
        status: 'running' as const,
        answer_md: '',
        sources: [],
        images: [],
        stages: [],
        duration_ms: 0,
        finished_at: null,
      }
    }
    throw new Error(`unexpected GET ${url}`)
  })

  await router.push('/')
  await router.isReady()
  wrapper = mount(App, { global: { plugins: [createPinia(), router, i18n] } })
  await router.push(`/thread/${THREAD_ID_FAST}`)
  await flushPromises()
  await nextTick()
  return wrapper
}

describe('direct 轮整页渲染（§3 v1.15 / §9 v1.15）', () => {
  it('流式中段：无来源面板、无「收集中」徽标，时间线已呈两节点（rewrite running→done + write running）', async () => {
    const app = await mountOnThread('running')
    const es = MockEventSource.instances[0]!
    expect(es.url).toContain(`/api/v1/turns/${TURN_ID_DIRECT}/events`)
    es.open()

    // seq 1..3：rewrite running→done + write running——「收集中」语义门控的直接观察窗
    for (const f of toFrames(directSequence.filter((w) => w.id <= 3))) es.dispatch(f)
    await vi.waitFor(() => {
      expect(app.findAll('[data-testid="timeline"] .tl-step')).toHaveLength(2)
    })
    const tlText = app.find('[data-testid="timeline"]')!.text()
    expect(tlText).toContain('改写意图')
    expect(tlText).toContain('综合中')
    expect(tlText).not.toContain('并行搜索') // 无 search 节点

    expect(app.find('[data-testid="source-panel"]').exists()).toBe(false) // 无来源面板
    expect(app.text()).not.toContain('收集中') // 无「收集中」徽标
    expect(app.text()).not.toContain('来源 0') // 无「来源 0」占位计数
  })

  it('终态（① 序列放完 + done 对账）：答案正文渲染、无来源面板/图片/思考流，状态行无来源计数', async () => {
    const app = await mountOnThread('running')
    const es = MockEventSource.instances[0]!
    es.open()
    terminalNow = true // done 到达即 REST 权威翻转
    for (const f of toFrames(directSequence)) es.dispatch(f)

    await vi.waitFor(() => {
      expect(app.find('[data-testid="answer-renderer"]').text()).toContain(
        '你好！我是 AI 搜索助手',
      )
    })
    expect(app.find('[data-testid="turn-question"]').text()).toContain('你好，请介绍一下你自己')
    // 时间线终态投影：REST 权威（2.5 stages 仅两节点全 done）
    expect(app.findAll('[data-testid="timeline"] .tl-step')).toHaveLength(2)
    // §3 三不渲染：无来源面板 / 无图片区块 / 无思考流区块
    expect(app.find('[data-testid="source-panel"]').exists()).toBe(false)
    expect(app.find('[data-testid="image-row"]').exists()).toBe(false)
    expect(app.find('[data-testid="thinking-panel"]').exists()).toBe(false)
    // 状态行：已完成 + 无「来源 N」计数（TurnBlock sources.length>0 门）
    const status = app.find('[data-testid="turn-status"]')!
    expect(status.text()).toContain('已完成')
    expect(status.text()).not.toContain('来源')
    expect(app.text()).not.toContain('收集中')
  })

  it('回看态（② 2.5 sources=[]）：面板同口径不渲染，答案与两节点时间线正常', async () => {
    const app = await mountOnThread('completed')
    expect(app.find('[data-testid="answer-renderer"]').text()).toContain(
      '你好！我是 AI 搜索助手',
    )
    expect(app.findAll('[data-testid="timeline"] .tl-step')).toHaveLength(2)
    expect(app.find('[data-testid="source-panel"]').exists()).toBe(false)
    expect(app.text()).not.toContain('收集中')
    expect(app.text()).not.toContain('来源 0')
    expect(app.find('[data-testid="turn-status"]').text()).not.toContain('来源')
  })

  it('followups 追加于 direct 轮（③）：store 投影后页面无错误、答案区不受影响、无空态占位', async () => {
    const app = await mountOnThread('running')
    const es = MockEventSource.instances[0]!
    es.open()
    terminalNow = true
    for (const f of toFrames(directSequence)) es.dispatch(f)
    await vi.waitFor(() => {
      expect(app.find('[data-testid="answer-renderer"]').text()).toContain('你好！我是 AI 搜索助手')
    })
    // done 后保活窗口内 followups 到达（L13 收口①）：页面无错误提示、答案区不受影响
    es.dispatch({
      id: directFollowupsEvent.id,
      type: directFollowupsEvent.event,
      data: JSON.stringify(directFollowupsEvent.data),
    })
    await flushPromises()
    expect(app.text()).not.toContain('加载失败')
    expect(app.find('[data-testid="answer-renderer"]').text()).toContain('你好！我是 AI 搜索助手')
    // FollowupChips 渲染随 M2/M4 工单落位——本批断言面：不渲染空态/无错误（store 投影见 sse-pipeline）
    expect(app.find('[data-testid="source-panel"]').exists()).toBe(false)
  })
})

describe('常规 fast 轮收集态正向回归（v1.15 门控收紧的负向对偶面——B9 测试审计 P1 补口）', () => {
  it('stage(search,running) 且 sources 空 → 面板已在含「收集中」徽标；search done 后不掉；终态后面板与来源卡正常', async () => {
    const app = await mountOnFastThread()
    const es = MockEventSource.instances[0]!
    expect(es.url).toContain(`/api/v1/turns/${TURN_ID_FAST}/events`)
    es.open()

    // 窗口 A（P1 核心）：seq 1..3——rewrite running→done + search running，sources 尚空。
    // 门控「已进入 search 阶段」即收集态：面板必须已在（不等首批 sources 到达），
    // 「收集中」徽标点亮、计数 0、无卡片——与 direct 负向断言（无面板/无徽标）精确对偶。
    for (const f of toFrames(fastSequence.filter((w) => w.id <= 3))) es.dispatch(f)
    const panel = await vi.waitFor(() => {
      const el = app.find('[data-testid="source-panel"]')
      expect(el.exists()).toBe(true)
      return el
    })
    expect(panel.text()).toContain('收集中')
    expect(panel.find('.count').text()).toBe('0')
    expect(panel.findAll('.src-card')).toHaveLength(0)
    // search key 在时间线呈现为节点（与 direct 轮 not.toContain('并行搜索') 对偶）
    expect(app.find('[data-testid="timeline"]').text()).toContain('并行搜索')

    // 窗口 B：stage(search,done)（fixture 无此帧——零结果搜索的真实形态：search 完结先于
    // sources 到达）。轮仍 running、stages 仍含 search key → 收集态不掉、面板不塌（不闪断）。
    es.dispatch({
      id: 4,
      type: 'stage',
      data: JSON.stringify({
        seq: 4,
        turn_id: TURN_ID_FAST,
        ts: '2026-11-01T08:12:02Z',
        stage_key: 'search',
        status: 'done',
      }),
    })
    await vi.waitFor(() => {
      expect(app.find('[data-testid="source-panel"]').text()).toContain('收集中')
    })
    expect(app.find('[data-testid="source-panel"]').find('.count').text()).toBe('0')

    // 窗口 C（终态）：done → REST 权威对账（turnDetailFastCompleted：sources=[ref_no 1]）。
    // liveTurn 消失 → collecting=false → 徽标熄灭；面板经 sources.length>0 门持续在，
    // 计数与来源卡正常——「面板仍正常」的终态收口。
    mockedGet.mockResolvedValueOnce(turnDetailFastCompleted)
    es.dispatch({
      id: 5,
      type: 'done',
      data: JSON.stringify({
        seq: 5,
        turn_id: TURN_ID_FAST,
        ts: '2026-11-01T08:12:11Z',
        finish: 'completed',
        degraded: false,
        degraded_reason: null,
        duration_ms: 7420,
        source_count: 1,
        usage: { total_tokens: 8953, cost_usd: 0.0091 },
      }),
    })
    await vi.waitFor(() => {
      expect(app.find('[data-testid="source-panel"]').text()).not.toContain('收集中')
    })
    const panelDone = app.find('[data-testid="source-panel"]')
    expect(panelDone.find('.count').text()).toBe('1')
    expect(panelDone.find('[data-source-card="1"]').exists()).toBe(true)
    expect(app.find('[data-testid="turn-status"]').text()).toContain('1 个来源')
  })
})
