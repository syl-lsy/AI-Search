/**
 * test/integration/fallback-turn.test.ts — B14 兜底收束轮 + politics 拒答轮整页回放
 * （api.md §7.4 v1.25 兜底收束形态 / 增量 B politics 类别，PRD A51 五条 GWT 与 A50 第九条；
 * frontend-api.md §3 v1.29 钉注②③；§9 v1.29 fixture 驱动——B14 测试资产增补批）。
 *
 * 联装形态：AnswerView 真身（App + router）+ turnStore/turnStream 真身 + MockEventSource
 * 驱动 SSE 帧 + REST mock；fake timers + rAF stub（同 sse-pipeline.test.ts 手法）。
 *
 * 断言链：
 *   1. digest 轮回放：digest 单块大 delta 经 turnStream 合帧缓冲不炸、answer_md 整块渲染
 *      （说明行 + [n] 角标与来源卡联动形态）、零 error 事件、done(degraded, reason=null)、
 *      重新生成钮可见（AnswerActions 第 8 钮公式天然覆盖——frontend-api v1.29 钉注②）、
 *      REST 对账字段一致（A51 GWT①「2.5 对账 answer_md=兜底全文、status=degraded」）；
 *   2. partial 轮回放：token 部分流中断 → 句末截断正文渲染持久（A51 GWT②）、sources 保留、
 *      done(degraded) 零 error；
 *   3. politics 拒答轮回放：stage(rewrite,failed,extra.safety=politics) → error(403002 中性话术)
 *      → 无 done → 服务端关闭 → 温和卡渲染、零答案区、零来源面板、不重连、REST 对账 failed
 *      （A50 第九条 GWT「全程零搜索零综合」——序列零 sources/images 事件的结构性投影）。
 *
 * 【B14 修复注记（2026-11-18 同批）】DegradedBanner 真 bug 已修复：TurnBlock isDegraded
 * 改双条件判定（status==='degraded' ∨ degradedReason!==null）+ i18n 增 reason=null 空态
 * 词条（answer.degradedReason.none）——本文件降级面用例（原 skip 挂牌）已摘牌恢复执行，
 * 红转绿锁死回归。
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
import {
  POLITICS_REFUSAL_MESSAGE,
  THREAD_ID_FAST,
  TURN_ID_FALLBACK_DIGEST,
  TURN_ID_FALLBACK_PARTIAL,
  TURN_ID_POLITICS,
  turnDetailFallbackDigestDegraded,
  turnDetailFallbackPartialDegraded,
  turnDetailPoliticsRefused,
} from '@/mocks/turnDetails'
import { threadListFixture } from '@/mocks/threads'
import {
  fallbackDigestSequence,
  fallbackPartialSequence,
  politicsRefusalSequence,
} from '@/mocks/sseSequences'
import { router } from '@/router'
import { useTurnStore } from '@/stores/turn'
import type { SseWireEvent, TurnDetail } from '@/types/api'

import { MockEventSource, ES_READY_STATE } from '../helpers/mockEventSource'

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
function runningSeedOf(detail: TurnDetail): TurnDetail {
  return { ...detail, status: 'running', answer_md: '', sources: [], images: [], stages: [], duration_ms: 0, finished_at: null }
}

let wrapper: VueWrapper | null = null
let terminalNow = false // done/onClosed 对账（REST 权威）时服务端事实翻转通道
let pinia: ReturnType<typeof createPinia> | null = null

beforeEach(() => {
  pinia = createPinia()
  setActivePinia(pinia)
  localStorage.clear()
  sessionStorage.clear()
  vi.clearAllMocks()
  vi.useFakeTimers()
  MockEventSource.reset()
  vi.stubGlobal('EventSource', MockEventSource)
  // rAF → setTimeout(16)（fake timers 可推进；streaming-buffer 规则 8 的测试等价物）
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => setTimeout(() => cb(0), 16))
  i18n.global.locale.value = 'zh-CN'
  terminalNow = false
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

/** 单轮线程 harness：mount 后返回 App wrapper 与同实例 store（DOM + store 双断言面） */
async function mountSingleTurnThread(turnId: string, detail: TurnDetail): Promise<{ app: VueWrapper; store: ReturnType<typeof useTurnStore> }> {
  mockedGet.mockImplementation(async (url: string) => {
    if (url === '/api/v1/threads') return threadListFixture
    if (url === '/api/v1/models') return modelsFixture
    if (url === `/api/v1/threads/${THREAD_ID_FAST}`) {
      return {
        thread_id: THREAD_ID_FAST,
        title: 'B14 兜底轮集成夹具线程',
        mode_summary: 'fast',
        turn_count: 1,
        has_artifacts: false,
        created_at: detail.created_at,
        updated_at: detail.finished_at ?? detail.created_at,
        turns: [
          {
            turn_id: turnId,
            mode: 'fast',
            status: 'running',
            query: detail.query,
            query_lang: 'zh',
            has_report: false,
            degraded: false,
            duration_ms: 0,
            created_at: detail.created_at,
          },
        ],
      }
    }
    if (url === `/api/v1/turns/${turnId}`) {
      return terminalNow ? detail : runningSeedOf(detail)
    }
    throw new Error(`unexpected GET ${url}`)
  })

  await router.push('/')
  await router.isReady()
  const app = mount(App, { global: { plugins: [pinia!, router, i18n] } })
  wrapper = app // afterEach unmount 的句柄（direct-turn harness 同款——漏赋值会让上一个测试的 App 跨用例存活、双实例各自响应路由变化产生双订阅）
  await router.push(`/thread/${THREAD_ID_FAST}`)
  await flushPromises()
  await nextTick()
  return { app, store: useTurnStore() }
}

describe('B14 digest 兜底轮回放（api.md §7.4 v1.25 / PRD A51 GWT①）', () => {
  it('单块大 delta 回放：answer_md 整块渲染（[n] 角标 1..5）、零 error、重新生成钮可见、REST 对账一致', async () => {
    const { app, store } = await mountSingleTurnThread(TURN_ID_FALLBACK_DIGEST, turnDetailFallbackDigestDegraded)
    const es = MockEventSource.instances[0]!
    expect(es.url).toContain(`/api/v1/turns/${TURN_ID_FALLBACK_DIGEST}/events`)
    es.open()

    // 第一段（seq 1..7）：rewrite/search/write 阶段态——答案区尚空（无 token）
    for (const f of toFrames(fallbackDigestSequence.filter((w) => w.id <= 7))) es.dispatch(f)
    vi.advanceTimersByTime(200)
    expect(app.find('[data-testid="turn-answer"]').exists()).toBe(false)

    // 第二段（seq 8..10）：digest 单块 token → write done(extra.fallback) → done(degraded)
    terminalNow = true
    for (const f of toFrames(fallbackDigestSequence.filter((w) => w.id >= 8))) es.dispatch(f)
    // 合帧缓冲追赶分帧（budget=pending×k）+ renderThrottle 70ms 尾帧——单块大 delta 落地不炸不截断
    vi.advanceTimersByTime(2000)

    // digest 整块渲染：说明行、[n] 角标 1..5（AnswerRenderer cite 机制——与 sources 卡联动）、尾注
    await vi.waitFor(() => {
      expect(app.find('[data-testid="answer-renderer"]').text()).toContain('综合回答未能完成')
      expect(app.find('[data-testid="answer-renderer"]').text()).toContain('其余 1 条来源见下方来源面板')
    })
    const citeNos = app.findAll('[data-testid="answer-renderer"] sup.cite').map((s) => Number(s.attributes('data-ref-no')))
    expect(citeNos).toEqual([1, 2, 3, 4, 5])

    // done 后 REST 对账（REST 为准源）：终态权威替换 answer_md / sources / stages / status
    await vi.waitFor(() => {
      expect(store.turns[TURN_ID_FALLBACK_DIGEST]!.answer_md).toBe(turnDetailFallbackDigestDegraded.answer_md)
    })
    expect(store.turns[TURN_ID_FALLBACK_DIGEST]!.status).toBe('degraded')
    expect(store.turns[TURN_ID_FALLBACK_DIGEST]!.sources).toHaveLength(6)
    expect(store.turns[TURN_ID_FALLBACK_DIGEST]!.stages.map((s) => [s.stage_key, s.status])).toEqual([
      ['rewrite', 'done'],
      ['search', 'done'],
      ['write', 'done'],
    ])
    expect(store.turns[TURN_ID_FALLBACK_DIGEST]!.stages.find((s) => s.stage_key === 'write')?.extra?.fallback).toBe('synthesis_digest')
    // 零 error 事件：errorMsg/errorCode 恒空（「超时不能报错收束」裁决①的 store 投影面）
    expect(store.turns[TURN_ID_FALLBACK_DIGEST]!.errorMsg).toBeNull()
    expect(store.turns[TURN_ID_FALLBACK_DIGEST]!.errorCode).toBeNull()

    // images 可选事件照常渲染（兜底轮 images 不动不重发——regenerate 互操作前置）
    expect(app.find('[data-testid="image-row"]').exists()).toBe(true)
    expect(app.find('[data-testid="source-panel"] .count').text()).toBe('6')
    // 重新生成钮可见（AnswerActions 第 8 钮公式天然覆盖：status=degraded ∧ sources>0 ∧ fast
    // ——frontend-api.md §3 v1.29 钉注②；点击语义断言见 component/answer-actions.test.ts）
    expect(app.find('[data-testid="act-regenerate"]').exists()).toBe(true)
    // 终态后 fast 轮 L13 保活窗口照旧（非兜底轮特判——done 后不主动 close）
    expect(es.closedByClient).toBe(false)
  })

  // 【B14 修复回归用例（原 skip 挂牌已摘）】症状→根因→修复留痕（审计面）：
  //   症状：兜底收束轮（done(finish=degraded, degraded=true, degraded_reason=null)）终态后
  //     DegradedBanner 不渲染、状态行显绿点「已完成 Ns」——降级终态与 completed 不可区分。
  //   根因：TurnBlock.vue 原判定 `isDegraded = turn.degradedReason !== null`，而 turn.ts
  //     done/reconcile 双路径 `degradedReason = degraded ? degraded_reason : null`——reason=null
  //     轮投影恒 null；TurnState 无 degraded 布尔字段，status='degraded' 未被判定消费。
  //   契约依据：frontend-api.md §3 v1.29 钉注②「done(finish=degraded) 走既有 DegradedBanner
  //     承载（degraded_reason=null 不影响渲染面）」；api.md §7.4 v1.25 兜底收束形态；
  //     PRD v1.28 A51 GWT①（research 未完成显著标注沿 §3 done 行既有口径）。
  //   修复（2026-11-18 同批落地）：isDegraded 改双条件（status==='degraded' ∨
  //     degradedReason!==null）+ i18n 增 answer.degradedReason.none 空态词条（zh/en）。
  //   本用例断言按契约口径先行写就、修复后原样转绿（红→绿证据见 B14 修复批汇报）。
  //   注意：digest 与 partial 拆为两个独立用例（单测试内二次 mount 会让前一个 App 跨用例
  //   存活、双实例各自响应路由变化产生双订阅——MockEventSource.instances 索引漂移）。
  it('digest 兜底轮降级面（reason=null）：DegradedBanner 渲染 + 状态行 warning「研究未完成」（B14 修复回归锁死）', async () => {
    const { app } = await mountSingleTurnThread(TURN_ID_FALLBACK_DIGEST, turnDetailFallbackDigestDegraded)
    const es = MockEventSource.instances[0]!
    es.open()
    terminalNow = true
    for (const f of toFrames(fallbackDigestSequence)) es.dispatch(f)
    vi.advanceTimersByTime(2000)
    await vi.waitFor(() => {
      expect(app.find('[data-testid="answer-renderer"]').text()).toContain('综合回答未能完成')
    })
    // DegradedBanner 显著标注（frontend-api §3 done 行既有口径——降级终态与 completed 严格可区分）
    const banner = app.find('[data-testid="degraded-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('研究未完成')
    // reason=null → 'none' 空态词条（B14 新增，非三枚举文案）
    expect(banner.text()).toContain('回答生成未完成')
    // 状态行：warning 点 +「研究未完成（降级）」，不显绿点「已完成」
    const status = app.find('[data-testid="turn-status"]')
    expect(status.find('.dot-warning').exists()).toBe(true)
    expect(status.find('.dot-success').exists()).toBe(false)
    expect(status.text()).toContain('研究未完成（降级）')
    expect(status.text()).not.toContain('已完成')
  })

  it('partial 兜底轮降级面（reason=null）：DegradedBanner 渲染（B14 修复回归锁死，与 digest 同根因）', async () => {
    const { app } = await mountSingleTurnThread(TURN_ID_FALLBACK_PARTIAL, turnDetailFallbackPartialDegraded)
    const es = MockEventSource.instances[0]!
    es.open()
    terminalNow = true
    for (const f of toFrames(fallbackPartialSequence)) es.dispatch(f)
    vi.advanceTimersByTime(2000)
    await vi.waitFor(() => {
      expect(app.find('[data-testid="answer-renderer"]').text()).toContain('物理层纠错')
    })
    expect(app.find('[data-testid="degraded-banner"]').exists()).toBe(true)
    expect(app.find('[data-testid="degraded-banner"]').text()).toContain('回答生成未完成')
  })
})

describe('B14 partial 兜底轮回放（api.md §7.4 v1.25 / PRD A51 GWT②）', () => {
  it('token 部分流中断：句末截断正文渲染、sources 保留、done(degraded) 零 error、REST 对账一致', async () => {
    const { app, store } = await mountSingleTurnThread(TURN_ID_FALLBACK_PARTIAL, turnDetailFallbackPartialDegraded)
    const es = MockEventSource.instances[0]!
    es.open()

    // 中段（seq 1..6）：阶段态 + sources 批次在位（write running 后部分流未至）
    for (const f of toFrames(fallbackPartialSequence.filter((w) => w.id <= 6))) es.dispatch(f)
    vi.advanceTimersByTime(200)
    await vi.waitFor(() => {
      expect(app.find('[data-testid="source-panel"] .count').text()).toBe('3')
    })
    expect(app.find('[data-testid="turn-answer"]').exists()).toBe(false)

    // 尾段（seq 7..11）：token×3 部分流 → 【中断】→ write done(extra.fallback) → done(degraded)
    terminalNow = true
    for (const f of toFrames(fallbackPartialSequence.filter((w) => w.id >= 7))) es.dispatch(f)
    vi.advanceTimersByTime(2000)
    await vi.waitFor(() => {
      expect(app.find('[data-testid="answer-renderer"]').text()).toContain('物理层纠错')
    })

    // done 后 REST 对账：句末截断正文持久（A51 GWT②「刷新回看 partial 全文持久可见」读通道）
    await vi.waitFor(() => {
      expect(store.turns[TURN_ID_FALLBACK_PARTIAL]!.answer_md).toBe(turnDetailFallbackPartialDegraded.answer_md)
    })
    expect(store.turns[TURN_ID_FALLBACK_PARTIAL]!.answer_md).toMatch(/[。！？!?.]$/) // 残句不呈现、无伪造补全
    expect(store.turns[TURN_ID_FALLBACK_PARTIAL]!.status).toBe('degraded')
    expect(store.turns[TURN_ID_FALLBACK_PARTIAL]!.errorMsg).toBeNull() // 零 error 事件
    // sources 保留（partial 轮恒有 sources——regenerate 互操作前置第 2 条）
    expect(store.turns[TURN_ID_FALLBACK_PARTIAL]!.sources).toHaveLength(3)
    expect(store.turns[TURN_ID_FALLBACK_PARTIAL]!.stages.find((s) => s.stage_key === 'write')?.extra?.fallback).toBe('synthesis_partial')
    // 零图片轮无图片区块；重新生成钮可见（公式覆盖——GWT③ regenerate 互操作入口）
    expect(app.find('[data-testid="image-row"]').exists()).toBe(false)
    expect(app.find('[data-testid="act-regenerate"]').exists()).toBe(true)
    expect(es.closedByClient).toBe(false) // fast 轮 L13 保活照旧
  })
})

describe('B14 politics 拒答轮回放（api.md §7.4 v1.25 增量 B / PRD A50 第九条 GWT）', () => {
  it('error(403002)+close 无 done：温和卡渲染、零答案区、零来源面板、不重连、REST 对账 failed', async () => {
    const { app, store } = await mountSingleTurnThread(TURN_ID_POLITICS, turnDetailPoliticsRefused)
    expect(store.generatingThreadIds.has(THREAD_ID_FAST)).toBe(true)
    const es = MockEventSource.instances[0]!
    expect(es.url).toContain(`/api/v1/turns/${TURN_ID_POLITICS}/events`)
    es.open()

    // 三帧回放：stage(rewrite,running) → stage(rewrite,failed,extra) → error(403002)（无 done）
    for (const f of toFrames(politicsRefusalSequence)) es.dispatch(f)
    await flushPromises()

    // 温和卡（B13 既有通路，politics 为值域扩容零渲染改动——frontend-api v1.29 钉注③）
    const card = app.find('[data-testid="safety-card"]')
    expect(card.exists()).toBe(true)
    expect(card.text()).toContain('内容安全提示')
    expect(card.text()).toContain(POLITICS_REFUSAL_MESSAGE)
    // 零答案区 + 零来源面板（拒答轮全程零搜索零综合——A50 第九条 GWT 的渲染投影）
    expect(app.find('[data-testid="turn-answer"]').exists()).toBe(false)
    expect(app.find('[data-testid="source-panel"]').exists()).toBe(false)
    // 时间线 rewrite failed 节点（既有 Timeline failed 渲染承担）
    expect(app.find('[data-testid="timeline"] .tl-step.failed').exists()).toBe(true)
    expect(app.find('[data-testid="timeline"]').text()).toContain('改写意图')
    // 拒答轮无重新生成入口（failed ∧ sources=0 → canRegenerate=false）
    expect(app.find('[data-testid="act-regenerate"]').exists()).toBe(false)

    // 服务端按契约直接关闭（无 done）→ B13 既有收口链：生成中集合立即清除 + 不重连
    terminalNow = true
    es.fail(ES_READY_STATE.CONNECTING)
    expect(store.generatingThreadIds.has(THREAD_ID_FAST)).toBe(false)
    expect(es.closedByClient).toBe(true)
    vi.advanceTimersByTime(60_000) // 推进越看门狗窗：无新 EventSource 实例（A13 断线重连豁免）
    expect(MockEventSource.instances).toHaveLength(1)
    // REST 对账收敛权威终态（turns.error 复用承载——温和卡文案保持）
    await vi.waitFor(() => {
      expect(store.turns[TURN_ID_POLITICS]!.status).toBe('failed')
      expect(store.turns[TURN_ID_POLITICS]!.errorMsg).toBe(POLITICS_REFUSAL_MESSAGE)
      expect(store.turns[TURN_ID_POLITICS]!.errorCode).toBe('403002')
    })
  })
})
