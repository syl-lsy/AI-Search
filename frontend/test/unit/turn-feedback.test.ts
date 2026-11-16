/**
 * test/unit/turn-feedback.test.ts — 轮反馈（REQ-057/A41，frontend-api.md §4 v1.17 / api.md §2.9）。
 *
 * 判定点（frontend-api.md §9 反馈判定点，fixture 驱动）：
 *   ① POST feedback 调用即发且提交体={helpful}；② 乐观 UI 即时置位；
 *   ③ 失败静默回滚至未反馈态且无 toast（仅 console 留痕）、可重试；
 *   ④ 改判再发覆盖（upsert 语义前端面=再点另一钮即发）；⑤ reconcile 对 2.5 feedback_helpful
 *   还原（刷新/回看按钮态还原，undefined（M2 前端点）→ null 同口径）。
 *
 * 独立于 turn-store.test.ts（该文件 mock 面 = client+turnStream+toast；本文件加 mock
 * '@/api/turns'——store 经 api/turns.ts 唯一出口触网，api.md §2.9 端点契约在
 * api-modules.test.ts 断言）。
 */
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { apiGet } from '@/api/client'
import { sendTurnFeedback } from '@/api/turns'
import { toast } from '@/composables/toast'
import { THREAD_ID_FAST, TURN_ID_FAST } from '@/mocks/turnDetails'
import { subscribeTurn } from '@/sse/turnStream'
import type { TurnStreamController } from '@/sse/turnStream'
import { useTurnStore } from '@/stores/turn'
import type { FeedbackResponse, TurnDetail } from '@/types/api'

const THREAD_ID = THREAD_ID_FAST
const TURN_ID = TURN_ID_FAST

vi.mock('@/api/client', () => ({
  apiPost: vi.fn(),
  apiGet: vi.fn(),
  normalizeError: () => ({ name: 'ApiError', code: 'network_error', message: '网络异常，请稍后重试' }),
}))
vi.mock('@/api/turns', () => ({
  sendTurnFeedback: vi.fn(),
}))
vi.mock('@/sse/turnStream', () => ({
  subscribeTurn: vi.fn(),
  closeTurnStream: vi.fn(),
  peekTurnStream: vi.fn(),
}))
vi.mock('@/composables/toast', () => ({ toast: vi.fn() }))

const mockedGet = vi.mocked(apiGet)
const mockedSendFeedback = vi.mocked(sendTurnFeedback)
const mockedToast = vi.mocked(toast)

function fakeController(): TurnStreamController {
  return { turnId: TURN_ID, state: 'connecting', closed: false, close: vi.fn() }
}

function makeDetail(overrides: Partial<TurnDetail> = {}): TurnDetail {
  return {
    turn_id: TURN_ID,
    thread_id: THREAD_ID,
    mode: 'fast',
    status: 'completed',
    query: '测试问题',
    query_lang: 'zh',
    rewritten_queries: [],
    parent_turn_id: null,
    answer_md: '答案',
    sources: [],
    images: [],
    stages: [],
    degraded: false,
    degraded_reason: null,
    error: null,
    usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, cost_usd: 0 },
    duration_ms: 0,
    export_available: false,
    created_at: '2026-11-01T08:12:00Z',
    finished_at: '2026-11-01T08:12:01Z',
    incognito: false,
    cancel_reason: null,
    followups: null,
    ...overrides,
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  sessionStorage.clear()
  vi.clearAllMocks()
  vi.mocked(subscribeTurn).mockImplementation(() => fakeController())
})

/** 种子化一轮（经 ask 路径；subscribeTurn 已 mock 不建流） */
async function seeded(): Promise<ReturnType<typeof useTurnStore>> {
  vi.mocked((await import('@/api/client')).apiPost).mockResolvedValueOnce({
    thread_id: THREAD_ID,
    turn_id: TURN_ID,
    status: 'running',
  })
  const store = useTurnStore()
  await store.ask('测试问题', 'fast')
  return store
}

describe('turnStore.sendFeedback（REQ-057/A41：乐观 UI + 静默回滚）', () => {
  it('①②POST 即发且提交体={helpful}；乐观置位先于响应（pending 期按钮态已更新）', async () => {
    const store = await seeded()
    let resolvePost: (v: FeedbackResponse) => void = () => {}
    mockedSendFeedback.mockImplementationOnce(
      () =>
        new Promise<FeedbackResponse>((resolve) => {
          resolvePost = resolve
        }),
    )
    const pending = store.sendFeedback(TURN_ID, true)
    // 乐观置位：POST 未返回时本地即 true
    expect(store.turns[TURN_ID]!.feedbackHelpful).toBe(true)
    expect(mockedSendFeedback).toHaveBeenCalledWith(TURN_ID, true)
    resolvePost({ turn_id: TURN_ID, helpful: true, updated_at: '2026-11-14T08:30:00Z' })
    await pending
    expect(store.turns[TURN_ID]!.feedbackHelpful).toBe(true) // 响应回写一致
  })

  it('④改判：true → false 两次 POST，第二次提交体={helpful:false}，本地态跟随覆盖', async () => {
    const store = await seeded()
    mockedSendFeedback
      .mockResolvedValueOnce({ turn_id: TURN_ID, helpful: true, updated_at: '2026-11-14T08:30:00Z' })
      .mockResolvedValueOnce({ turn_id: TURN_ID, helpful: false, updated_at: '2026-11-14T08:31:00Z' })
    await store.sendFeedback(TURN_ID, true)
    await store.sendFeedback(TURN_ID, false)
    expect(mockedSendFeedback).toHaveBeenNthCalledWith(1, TURN_ID, true)
    expect(mockedSendFeedback).toHaveBeenNthCalledWith(2, TURN_ID, false)
    expect(store.turns[TURN_ID]!.feedbackHelpful).toBe(false)
  })

  it('③失败静默回滚至未反馈态、无 toast（仅 console 留痕）；可重试', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      const store = await seeded()
      mockedSendFeedback.mockRejectedValueOnce(new Error('net down'))
      await store.sendFeedback(TURN_ID, true)
      expect(store.turns[TURN_ID]!.feedbackHelpful).toBeNull() // 回滚至未反馈态
      expect(mockedToast).not.toHaveBeenCalled() // 静默：不 toast
      expect(warnSpy).toHaveBeenCalled() // console 留痕
      // 可重试：第二次成功即落位
      mockedSendFeedback.mockResolvedValueOnce({ turn_id: TURN_ID, helpful: true, updated_at: '2026-11-14T08:32:00Z' })
      await store.sendFeedback(TURN_ID, true)
      expect(store.turns[TURN_ID]!.feedbackHelpful).toBe(true)
    } finally {
      warnSpy.mockRestore()
    }
  })

  it('③改判失败回滚至改判前的既有判定（非吞掉为 null）', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      const store = await seeded()
      mockedSendFeedback.mockResolvedValueOnce({ turn_id: TURN_ID, helpful: true, updated_at: '2026-11-14T08:30:00Z' })
      await store.sendFeedback(TURN_ID, true)
      mockedSendFeedback.mockRejectedValueOnce(new Error('net down'))
      await store.sendFeedback(TURN_ID, false)
      expect(store.turns[TURN_ID]!.feedbackHelpful).toBe(true) // 回滚到 true（改判前值）
      expect(mockedToast).not.toHaveBeenCalled()
    } finally {
      warnSpy.mockRestore()
    }
  })

  it('⑤取消（api.md §2.9 v1.17 helpful=null）：POST body {helpful:null}，响应 helpful:null 回写本地 null', async () => {
    const store = await seeded()
    mockedSendFeedback.mockResolvedValueOnce({ turn_id: TURN_ID, helpful: null, updated_at: null })
    await store.sendFeedback(TURN_ID, null)
    expect(mockedSendFeedback).toHaveBeenCalledWith(TURN_ID, null)
    expect(store.turns[TURN_ID]!.feedbackHelpful).toBeNull()
  })

  it('⑤取消失败：静默回滚至清除前的既有判定（true）、无 toast（仅 console 留痕）', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      const store = await seeded()
      mockedSendFeedback.mockResolvedValueOnce({ turn_id: TURN_ID, helpful: true, updated_at: '2026-11-15T08:30:00Z' })
      await store.sendFeedback(TURN_ID, true)
      mockedSendFeedback.mockRejectedValueOnce(new Error('net down'))
      await store.sendFeedback(TURN_ID, null)
      expect(store.turns[TURN_ID]!.feedbackHelpful).toBe(true)
      expect(mockedToast).not.toHaveBeenCalled()
      expect(warnSpy).toHaveBeenCalled()
    } finally {
      warnSpy.mockRestore()
    }
  })

  it('未知轮防御性忽略：不发请求、不抛错', async () => {
    const store = await seeded()
    await expect(store.sendFeedback('9f8e7d6c-5b4a-4321-8fed-cba987654321', true)).resolves.toBeUndefined()
    expect(mockedSendFeedback).not.toHaveBeenCalled()
  })

  it('乱序守卫（响应分支）：POST1 in-flight → POST2 → POST2 先 resolve → POST1 后到不覆盖（per-turn seq 代数）', async () => {
    const store = await seeded()
    let resolve1!: (v: FeedbackResponse) => void
    let resolve2!: (v: FeedbackResponse) => void
    mockedSendFeedback
      .mockImplementationOnce(
        () =>
          new Promise<FeedbackResponse>((resolve) => {
            resolve1 = resolve
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise<FeedbackResponse>((resolve) => {
            resolve2 = resolve
          }),
      )
    const p1 = store.sendFeedback(TURN_ID, true)
    const p2 = store.sendFeedback(TURN_ID, false)
    // 乐观态：最后一次点击生效（false）
    expect(store.turns[TURN_ID]!.feedbackHelpful).toBe(false)
    // POST2（新点击）先返回 → 回写 false
    resolve2({ turn_id: TURN_ID, helpful: false, updated_at: '2026-11-15T08:31:00Z' })
    await p2
    expect(store.turns[TURN_ID]!.feedbackHelpful).toBe(false)
    // POST1（旧点击）后到：seq 已非最新 → 丢弃不写（不把 true 覆盖回 false 态）
    resolve1({ turn_id: TURN_ID, helpful: true, updated_at: '2026-11-15T08:30:00Z' })
    await p1
    expect(store.turns[TURN_ID]!.feedbackHelpful).toBe(false)
  })

  it('两连败回滚锚点=lastServerConfirmed（批次3评审-F1）：seq1 失败被代数守卫丢弃 → seq2 失败回滚 null（非点击时刻快照 true 假态）', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      const store = await seeded()
      let reject1!: (e: Error) => void
      let reject2!: (e: Error) => void
      mockedSendFeedback
        .mockImplementationOnce(
          () =>
            new Promise<FeedbackResponse>((_, reject) => {
              reject1 = reject
            }),
        )
        .mockImplementationOnce(
          () =>
            new Promise<FeedbackResponse>((_, reject) => {
              reject2 = reject
            }),
        )
      const p1 = store.sendFeedback(TURN_ID, true) // 乐观 true（服务端从未确认）
      const p2 = store.sendFeedback(TURN_ID, false) // 乐观 false；点击时刻快照=true（上一代未确认乐观值）
      expect(store.turns[TURN_ID]!.feedbackHelpful).toBe(false)
      reject1(new Error('seq1 net down'))
      await p1 // 旧代失败：代数守卫丢弃，不回滚（false 保持）
      expect(store.turns[TURN_ID]!.feedbackHelpful).toBe(false)
      reject2(new Error('seq2 net down'))
      await p2
      // 服务端从未落盘任何值 → 回滚到 lastServerConfirmed=null；
      // 旧实现回滚点击时刻快照 true = 「有帮助已选中」假态（服务端无此判定）
      expect(store.turns[TURN_ID]!.feedbackHelpful).toBeNull()
    } finally {
      warnSpy.mockRestore()
    }
  })

  it('成功后再失败回滚到上一服务端确认值（lastServerConfirmed 由响应回写更新）', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      const store = await seeded()
      mockedSendFeedback.mockResolvedValueOnce({ turn_id: TURN_ID, helpful: true, updated_at: '2026-11-15T09:00:00Z' })
      await store.sendFeedback(TURN_ID, true) // 服务端确认 true（锚点更新）
      mockedSendFeedback.mockRejectedValueOnce(new Error('net down'))
      await store.sendFeedback(TURN_ID, null) // 取消失败：回滚到服务端确认值 true（非 null）
      expect(store.turns[TURN_ID]!.feedbackHelpful).toBe(true)
    } finally {
      warnSpy.mockRestore()
    }
  })

  it('乱序守卫（失败分支）：旧请求后到失败不回滚新状态', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      const store = await seeded()
      let reject1!: (e: Error) => void
      mockedSendFeedback
        .mockImplementationOnce(
          () =>
            new Promise<FeedbackResponse>((_, reject) => {
              reject1 = reject
            }),
        )
        .mockResolvedValueOnce({ turn_id: TURN_ID, helpful: false, updated_at: '2026-11-15T08:31:00Z' })
      const p1 = store.sendFeedback(TURN_ID, true)
      await store.sendFeedback(TURN_ID, false) // 新点击先成功落位 false
      expect(store.turns[TURN_ID]!.feedbackHelpful).toBe(false)
      // 旧请求（true）后到失败：seq 非最新 → 丢弃（不回滚、不留痕覆盖新状态）
      reject1(new Error('late failure'))
      await p1
      expect(store.turns[TURN_ID]!.feedbackHelpful).toBe(false)
    } finally {
      warnSpy.mockRestore()
    }
  })
})

describe('reconcile 对 2.5 feedback_helpful 还原（A41 刷新/回看按钮态还原）', () => {
  it('feedback_helpful=true/false 还原为对应按钮态', async () => {
    mockedGet.mockResolvedValueOnce(makeDetail({ feedback_helpful: true }))
    const store = useTurnStore()
    await store.reconcile(TURN_ID)
    expect(store.turns[TURN_ID]!.feedbackHelpful).toBe(true)

    mockedGet.mockResolvedValueOnce(makeDetail({ feedback_helpful: false }))
    await store.reconcile(TURN_ID)
    expect(store.turns[TURN_ID]!.feedbackHelpful).toBe(false)
  })

  it('null=未反馈；字段缺席（M2 前端点/旧后端）同样归 null', async () => {
    mockedGet.mockResolvedValueOnce(makeDetail({ feedback_helpful: null }))
    const store = useTurnStore()
    await store.reconcile(TURN_ID)
    expect(store.turns[TURN_ID]!.feedbackHelpful).toBeNull()

    const detail = makeDetail()
    delete (detail as Partial<TurnDetail>).feedback_helpful // 模拟字段缺席（可选字段语义）
    mockedGet.mockResolvedValueOnce(detail)
    await store.reconcile(TURN_ID)
    expect(store.turns[TURN_ID]!.feedbackHelpful).toBeNull()
  })

  it('刷新/回看还原后改判照常（还原态 → 本地改判 → POST 覆盖）', async () => {
    mockedGet.mockResolvedValueOnce(makeDetail({ feedback_helpful: false }))
    const store = useTurnStore()
    await store.reconcile(TURN_ID)
    expect(store.turns[TURN_ID]!.feedbackHelpful).toBe(false)
    mockedSendFeedback.mockResolvedValueOnce({ turn_id: TURN_ID, helpful: true, updated_at: '2026-11-14T08:33:00Z' })
    await store.sendFeedback(TURN_ID, true)
    expect(store.turns[TURN_ID]!.feedbackHelpful).toBe(true)
    expect(mockedSendFeedback).toHaveBeenCalledWith(TURN_ID, true)
  })
})
