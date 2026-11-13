/**
 * test/unit/turn-store.test.ts — turnStore 单元测试（§3 派发表逐行 + §4 store 划分）。
 *
 * REST（@/api/client）与 SSE 传输层（@/sse/turnStream）以 vi.mock 替身注入——
 * 本文件只断言 store 状态机投影与编排行为；§7.4 序列联装见 integration/sse-pipeline.test.ts。
 */
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { apiGet, apiPost } from '@/api/client'
import { toast } from '@/composables/toast'
import { THREAD_ID_FAST, TURN_ID_FAST } from '@/mocks/turnDetails'
import { closeTurnStream, peekTurnStream, subscribeTurn } from '@/sse/turnStream'
import type { SubscribeTurnOptions, TurnStreamController, TurnStreamState } from '@/sse/turnStream'
import { usePrefsStore } from '@/stores/prefs'
import { useTurnStore } from '@/stores/turn'
import type { SseWireEvent, TurnDetail } from '@/types/api'

const THREAD_ID = '7d2f0a1e-1c3b-4e5a-9d8f-0a1b2c3d4e5f'
const TURN_ID = 'c31b9d74-2a41-4f6e-b3c2-5d6e7f809a1b'
const OTHER_TURN_ID = '9f8e7d6c-5b4a-4321-8fed-cba987654321'

vi.mock('@/api/client', () => ({
  apiPost: vi.fn(),
  apiGet: vi.fn(),
  // store 失败兜底链消费（normalizeError 归一 toast 文案）——mock 面须与 store import 面同构
  normalizeError: () => ({
    name: 'ApiError',
    code: 'network_error',
    message: '网络异常，请稍后重试',
  }),
}))

vi.mock('@/sse/turnStream', () => ({
  subscribeTurn: vi.fn(),
  closeTurnStream: vi.fn(),
  peekTurnStream: vi.fn(),
}))

vi.mock('@/composables/toast', () => ({
  toast: vi.fn(),
}))

const mockedPost = vi.mocked(apiPost)
const mockedGet = vi.mocked(apiGet)
const mockedSubscribe = vi.mocked(subscribeTurn)
const mockedClose = vi.mocked(closeTurnStream)
const mockedPeek = vi.mocked(peekTurnStream)
const mockedToast = vi.mocked(toast)

/** 被订阅捕获的传输层回调（onEvent/onStateChange/onClosed） */
function capturedCallbacks(): SubscribeTurnOptions['callbacks'] {
  const call = mockedSubscribe.mock.calls.at(-1)
  if (!call) throw new Error('subscribeTurn 未被调用')
  return call[0]!.callbacks
}

function fakeController(): TurnStreamController {
  return { turnId: TURN_ID, state: 'connecting', closed: false, close: vi.fn() }
}

/** 构造 2.5 TurnDetail（以本文件常量 id 填充） */
function makeDetail(overrides: Partial<TurnDetail> = {}): TurnDetail {
  return {
    turn_id: TURN_ID,
    thread_id: THREAD_ID,
    mode: 'fast',
    status: 'running',
    query: '测试问题',
    query_lang: 'zh',
    rewritten_queries: [],
    parent_turn_id: null,
    answer_md: '',
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
    finished_at: null,
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
  mockedSubscribe.mockImplementation(() => fakeController())
  mockedPeek.mockReturnValue(undefined)
})

/** wire 事件构造（id=seq，data 补 §7.1 信封公共字段；测试 fixture 构造用单一 as 收窄，§8.9①） */
function wire(
  id: number,
  event: SseWireEvent['event'],
  payload: Record<string, unknown>,
  turnId: string = TURN_ID,
): SseWireEvent {
  return {
    id,
    event,
    data: { seq: id, turn_id: turnId, ts: '2026-11-01T08:12:00Z', ...payload },
  } as SseWireEvent
}

describe('ask()（生命周期 1：POST 建轮 + GET 订阅两段式；§2 注入约定）', () => {
  it('prefs 全缺省：提交体仅 {query, mode}（model/incognito/on_conflict 均不携带）', async () => {
    mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID, status: 'running' })
    const store = useTurnStore()
    await store.ask('测试问题', 'fast')
    expect(mockedPost).toHaveBeenCalledWith('/api/v1/threads', { query: '测试问题', mode: 'fast' })
  })

  it('prefs 有值：提交体注入 model/incognito/on_conflict（从 prefsStore 统一注入，禁止组件散拼）', async () => {
    const prefs = usePrefsStore()
    prefs.setModel('glm')
    prefs.toggleIncognito()
    prefs.setOnConflict('enqueue')
    mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID, status: 'running' })
    const store = useTurnStore()
    await store.ask('测试问题', 'deep')
    expect(mockedPost).toHaveBeenCalledWith('/api/v1/threads', {
      query: '测试问题',
      mode: 'deep',
      model: 'glm',
      incognito: true,
      on_conflict: 'enqueue',
    })
  })

  it('建轮成功即种子化 turn + 生成中集合加入 + 订阅（fast 轮 holdAfterDone=true）', async () => {
    mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID, status: 'running' })
    const store = useTurnStore()
    const data = await store.ask('测试问题', 'fast')
    expect(data.turn_id).toBe(TURN_ID)
    const turn = store.turns[TURN_ID]!
    expect(turn.question).toBe('测试问题')
    expect(turn.status).toBe('running')
    expect(turn.mode).toBe('fast')
    expect(turn.lastSeq).toBe(0)
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(true)
    expect(store.liveTurnId).toBe(TURN_ID)
    expect(mockedSubscribe).toHaveBeenCalledTimes(1)
    expect(mockedSubscribe.mock.calls[0]![0].holdAfterDone).toBe(true)
  })

  it('deep 轮 holdAfterDone=false（生命周期 13 仅 fast 轮保活）', async () => {
    mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID, status: 'running' })
    const store = useTurnStore()
    await store.ask('深度问题', 'deep')
    expect(mockedSubscribe.mock.calls[0]![0].holdAfterDone).toBe(false)
  })

  it('POST 失败：抛出错误、不种子化、不进生成中集合、不订阅', async () => {
    mockedPost.mockRejectedValueOnce(new Error('409001'))
    const store = useTurnStore()
    await expect(store.ask('重复问题', 'fast')).rejects.toThrow('409001')
    expect(store.turns[TURN_ID]).toBeUndefined()
    expect(store.generatingThreadIds.size).toBe(0)
    expect(mockedSubscribe).not.toHaveBeenCalled()
  })
})

describe('followUp()（§1 AnswerView 追问：POST /threads/{id}/turns，注入口径同 ask）', () => {
  it('提交体带 mode + prefs 注入；种子化并订阅', async () => {
    const prefs = usePrefsStore()
    prefs.setOnConflict('interrupt')
    mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID, status: 'queued' })
    const store = useTurnStore()
    await store.followUp(THREAD_ID, '追问问题', 'fast')
    expect(mockedPost).toHaveBeenCalledWith(`/api/v1/threads/${THREAD_ID}/turns`, {
      query: '追问问题',
      mode: 'fast',
      on_conflict: 'interrupt',
    })
    expect(store.turns[TURN_ID]!.status).toBe('queued')
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(true)
  })
})

describe('applyEvent 派发表（§3 事件 → store 单一状态机）', () => {
  async function seeded(mode: 'fast' | 'deep' = 'fast') {
    mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID, status: 'running' })
    const store = useTurnStore()
    await store.ask('测试问题', mode)
    return store
  }

  it('stage：按 stage_key 就地状态机 pending→running→done；progress/detail/extra 就位；重复 key 不插新节点', async () => {
    const store = await seeded()
    store.applyEvent(TURN_ID, wire(1, 'stage', { stage_key: 'rewrite', status: 'running', label: '改写意图' }))
    store.applyEvent(TURN_ID, wire(2, 'stage', { stage_key: 'rewrite', status: 'done' }))
    store.applyEvent(TURN_ID, wire(3, 'stage', { stage_key: 'search', status: 'running', label: '并行搜索', progress: { current: 1, total: 3 }, detail: 'SearXNG 0/3', extra: { subtopics: 3 } }))
    const stages = store.turns[TURN_ID]!.stages
    expect(stages).toHaveLength(2)
    expect(stages[0]).toMatchObject({ stage_key: 'rewrite', status: 'done' })
    expect(stages[1]).toMatchObject({
      stage_key: 'search',
      status: 'running',
      label: '并行搜索',
      progress: { current: 1, total: 3 },
      detail: 'SearXNG 0/3',
      extra: { subtopics: 3 },
    })
    expect(store.turns[TURN_ID]!.lastSeq).toBe(3)
  })

  it('token/thinking 双缓冲（v1.4）：各自通道独立追加，thinking 绝不入 answer_md', async () => {
    const store = await seeded()
    store.applyEvent(TURN_ID, wire(1, 'token', { delta: '答案A' }))
    store.applyEvent(TURN_ID, wire(2, 'thinking', { delta: '思考B' }))
    store.applyEvent(TURN_ID, wire(3, 'token', { delta: '答案C' }))
    store.applyEvent(TURN_ID, wire(4, 'thinking', { delta: '思考D' }))
    const turn = store.turns[TURN_ID]!
    expect(turn.answer_md).toBe('答案A答案C')
    expect(turn.thinking_md).toBe('思考B思考D')
  })

  it('sources：批次追加按 ref_no 排序；同 ref_no 更新不重复插', async () => {
    const store = await seeded()
    const s1 = { source_id: 'a', ref_no: 2, source_type: 'web', title: '第二', url: 'https://b.example', snippet: '', score: 0.8, snapshot_available: false, kb_doc_id: null }
    const s2 = { source_id: 'b', ref_no: 1, source_type: 'web', title: '第一', url: 'https://a.example', snippet: '', score: 0.9, snapshot_available: false, kb_doc_id: null }
    store.applyEvent(TURN_ID, wire(1, 'sources', { batch: [s1], total: 2 }))
    store.applyEvent(TURN_ID, wire(2, 'sources', { batch: [s2], total: 2 }))
    expect(store.turns[TURN_ID]!.sources.map((s) => s.ref_no)).toEqual([1, 2])
    // 同 ref_no 更新（snapshot_available false→true）不重复插
    store.applyEvent(TURN_ID, wire(3, 'sources', { batch: [{ ...s2, snapshot_available: true }], total: 2 }))
    const sources = store.turns[TURN_ID]!.sources
    expect(sources).toHaveLength(2)
    expect(sources.find((s) => s.ref_no === 1)!.snapshot_available).toBe(true)
  })

  it('images：一次性写入（至多一次事件）；v1.12 键可省条目原样保留（仅 url 不伪造缺键）', async () => {
    const store = await seeded()
    store.applyEvent(TURN_ID, wire(5, 'images', { images: [{ url: 'https://example.com/a.png' }] }))
    expect(store.turns[TURN_ID]!.images).toEqual([{ url: 'https://example.com/a.png' }])
    expect(Object.hasOwn(store.turns[TURN_ID]!.images[0]!, 'title')).toBe(false)
  })

  it('context_compact（仅 deep）：记录 compactSeq（重放边界）与 compactAt', async () => {
    const store = await seeded('deep')
    store.applyEvent(TURN_ID, wire(12, 'context_compact', { summarized_messages: 12, released_tokens: 58000, retained_tokens: 9000, offloaded_files: [] }))
    expect(store.turns[TURN_ID]!.compactSeq).toBe(12)
    expect(store.turns[TURN_ID]!.compactAt).toBe('2026-11-01T08:12:00Z')
  })

  it('memory：memory_hits 写入（M6 数据形态）', async () => {
    const store = await seeded()
    store.applyEvent(TURN_ID, wire(1, 'memory', { items: [{ memory_id: 'm1', text_head: '回答使用中文', category: 'style', channel: 'prompt', score: 0.6 }], profile_used: true, truncated: false }))
    expect(store.turns[TURN_ID]!.memory_hits).toEqual({ profile_used: true, items: [{ memory_id: 'm1', text_head: '回答使用中文', category: 'style', channel: 'prompt', score: 0.6 }] })
  })

  it('done：终态置位 + cancelReason 写入 + 生成中集合移除（生命周期 12）', async () => {
    const store = await seeded()
    store.applyEvent(TURN_ID, wire(9, 'done', { finish: 'canceled', degraded: false, degraded_reason: null, duration_ms: 100, source_count: 0, cancel_reason: 'superseded' }))
    const turn = store.turns[TURN_ID]!
    expect(turn.status).toBe('canceled')
    expect(turn.cancelReason).toBe('superseded')
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(false)
  })

  it('done(completed) 无 cancel_reason：cancelReason=null；finish=degraded 记录 degradedReason', async () => {
    const store = await seeded()
    store.applyEvent(TURN_ID, wire(9, 'done', { finish: 'degraded', degraded: true, degraded_reason: 'timeout', duration_ms: 100, source_count: 0 }))
    const turn = store.turns[TURN_ID]!
    expect(turn.status).toBe('degraded')
    expect(turn.cancelReason).toBeNull()
    expect(turn.degradedReason).toBe('timeout')
  })

  it('followups（v1.9）：写入该轮 followups；重放同 seq 幂等不重复写入', async () => {
    const store = await seeded()
    const ev = wire(10, 'followups', { items: ['追问一', '追问二'] })
    store.applyEvent(TURN_ID, ev)
    expect(store.turns[TURN_ID]!.followups).toEqual(['追问一', '追问二'])
    store.applyEvent(TURN_ID, ev) // seq ≤ lastSeq → 控制事件幂等门丢弃
    expect(store.turns[TURN_ID]!.followups).toEqual(['追问一', '追问二'])
    expect(store.turns[TURN_ID]!.lastSeq).toBe(10)
  })

  it('error（非终态）：errorMsg + 对应节点标 failed；status 与生成中集合不动', async () => {
    const store = await seeded()
    store.applyEvent(TURN_ID, wire(1, 'stage', { stage_key: 'search', status: 'running' }))
    store.applyEvent(TURN_ID, wire(2, 'error', { code: '500201', message: '全部搜索源不可用', stage_key: 'search' }))
    const turn = store.turns[TURN_ID]!
    expect(turn.errorMsg).toBe('全部搜索源不可用')
    expect(turn.status).toBe('running')
    expect(turn.stages.find((s) => s.stage_key === 'search')!.status).toBe('failed')
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(true)
  })

  it('控制事件 seq ≤ lastSeq 幂等丢弃（重放去重）；token/thinking 豁免（合帧 maxSeq==lastSeq）', async () => {
    const store = await seeded()
    store.applyEvent(TURN_ID, wire(5, 'token', { delta: 'A' }))
    expect(store.turns[TURN_ID]!.lastSeq).toBe(5)
    store.applyEvent(TURN_ID, wire(5, 'sources', { batch: [], total: 0 })) // 控制事件同 seq → 丢
    expect(store.turns[TURN_ID]!.sources).toHaveLength(0)
    store.applyEvent(TURN_ID, wire(5, 'token', { delta: 'B' })) // 合帧 flush maxSeq==lastSeq → 豁免
    expect(store.turns[TURN_ID]!.answer_md).toBe('AB')
  })

  it('未知轮 / turn_id 不匹配：防御性忽略', async () => {
    const store = await seeded()
    expect(() => store.applyEvent('00000000-0000-4000-8000-000000000000', wire(1, 'token', { delta: 'x' }))).not.toThrow()
    store.applyEvent(TURN_ID, wire(1, 'token', { delta: 'x' }, OTHER_TURN_ID))
    expect(store.turns[TURN_ID]!.answer_md).toBe('')
  })

  it('lastSeq 推进时持久化 sessionStorage ai-search.seq.{turnId}（生命周期 5 形态保留）', async () => {
    const store = await seeded()
    store.applyEvent(TURN_ID, wire(3, 'token', { delta: 'x' }))
    expect(sessionStorage.getItem(`ai-search.seq.${TURN_ID}`)).toBe('3')
  })
})

describe('reconcile()（生命周期 4/5：REST 为准源对账）', () => {
  it('未知轮（刷新/新标签页首屏）：以 2.5 全量种子化', async () => {
    mockedGet.mockResolvedValueOnce(makeDetail({
      status: 'completed',
      answer_md: '完整答案',
      followups: ['回看追问'],
      finished_at: '2026-11-01T08:12:11Z',
    }))
    const store = useTurnStore()
    await store.reconcile(TURN_ID)
    const turn = store.turns[TURN_ID]!
    expect(turn.status).toBe('completed')
    expect(turn.answer_md).toBe('完整答案')
    expect(turn.followups).toEqual(['回看追问'])
  })

  it('终态轮：answer_md 以 REST 为准源替换；cancel_reason/incognito 双源对账', async () => {
    mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID, status: 'running' })
    const store = useTurnStore()
    await store.ask('测试问题', 'fast')
    store.applyEvent(TURN_ID, wire(1, 'token', { delta: '流式增量' }))
    mockedGet.mockResolvedValueOnce(makeDetail({
      status: 'canceled',
      answer_md: 'REST 权威答案',
      cancel_reason: 'user',
      incognito: true,
    }))
    await store.reconcile(TURN_ID)
    const turn = store.turns[TURN_ID]!
    expect(turn.answer_md).toBe('REST 权威答案')
    expect(turn.cancelReason).toBe('user')
    expect(turn.incognito).toBe(true)
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(false)
  })

  it('运行中轮对账：answer_md 保留流式增量（token 不落库、REST 运行中为空串——不可倒退清空）', async () => {
    mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID, status: 'running' })
    const store = useTurnStore()
    await store.ask('测试问题', 'fast')
    store.applyEvent(TURN_ID, wire(1, 'token', { delta: '流式增量' }))
    mockedGet.mockResolvedValueOnce(makeDetail({ status: 'running', answer_md: '', sources: [] }))
    await store.reconcile(TURN_ID)
    expect(store.turns[TURN_ID]!.answer_md).toBe('流式增量')
  })

  it('运行中轮对账：生成中集合按状态校准保留（生命周期 12 reconcile 同口径）', async () => {
    mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID, status: 'running' })
    const store = useTurnStore()
    await store.ask('测试问题', 'fast')
    mockedGet.mockResolvedValueOnce(makeDetail({ status: 'running' }))
    await store.reconcile(TURN_ID)
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(true)
  })

  it('compactSeq 为传输层域：对账不重置（REST 无 context_compact 信息）', async () => {
    mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID, status: 'running' })
    const store = useTurnStore()
    await store.ask('测试问题', 'deep')
    store.applyEvent(TURN_ID, wire(12, 'context_compact', { summarized_messages: 1, released_tokens: 1, retained_tokens: 1, offloaded_files: [] }))
    mockedGet.mockResolvedValueOnce(makeDetail({ status: 'running', mode: 'deep' }))
    await store.reconcile(TURN_ID)
    expect(store.turns[TURN_ID]!.compactSeq).toBe(12)
  })
})

describe('subscribe 对账失败兜底（P2：裸 void reconcile 加 catch）', () => {
  it('onClosed 收口对账失败：sseState 复位 idle + toast 首次失败一次 + 非终态轮重试收敛', async () => {
    vi.useFakeTimers()
    try {
      mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID, status: 'running' })
      const store = useTurnStore()
      await store.ask('测试问题', 'fast')
      const callbacks = capturedCallbacks()
      callbacks.onStateChange('connected')
      expect(store.sseState).toBe('connected')
      mockedGet.mockRejectedValueOnce(new Error('net down'))
      callbacks.onClosed({ afterError: true, duringHold: false })
      await vi.advanceTimersByTimeAsync(0)
      expect(store.sseState).toBe('idle') // ① 连接态复位
      expect(mockedToast).toHaveBeenCalledTimes(1) // ② toast 兜底（失败链仅首次）
      expect(mockedToast.mock.calls[0]?.[0]).toBe('网络异常，请稍后重试')
      expect(store.turns[TURN_ID]!.status).toBe('running') // 首败未收敛
      // ③ 有限重试：第 1 次重试成功 → 终态收敛、生成中集合移除
      mockedGet.mockResolvedValueOnce(makeDetail({ status: 'failed', error: 'boom' }))
      await vi.advanceTimersByTimeAsync(10_000)
      expect(store.turns[TURN_ID]!.status).toBe('failed')
      expect(store.generatingThreadIds.has(THREAD_ID)).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('重试耗尽（初败 + 3 次重试全败）：GET 至多 4 次、toast 仅一次；生成中集合移除 + errorMsg 兜底（不永卡指示器；状态字段仍以 REST 为准源）', async () => {
    vi.useFakeTimers()
    try {
      mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID, status: 'running' })
      const store = useTurnStore()
      await store.ask('测试问题', 'fast')
      const callbacks = capturedCallbacks()
      mockedGet.mockRejectedValue(new Error('net down'))
      callbacks.onClosed({ afterError: true, duringHold: false })
      await vi.advanceTimersByTimeAsync(60_000)
      expect(mockedGet).toHaveBeenCalledTimes(4) // 初次 + 3 次有限重试
      expect(mockedToast).toHaveBeenCalledTimes(1) // 失败链只提示一次（防 toast 雪崩）
      expect(store.generatingThreadIds.has(THREAD_ID)).toBe(false) // 指示器不再永转
      expect(store.turns[TURN_ID]!.errorMsg).not.toBeNull() // 错误条可见（失败语义不吞）
      expect(store.turns[TURN_ID]!.status).toBe('running') // 未伪造本地终态（REST 为准源契约不破）
    } finally {
      vi.useRealTimers()
    }
  })

  it('done 路径对账失败：同样复位连接态 + toast 兜底（生命周期 4 收口）', async () => {
    vi.useFakeTimers()
    try {
      mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID, status: 'running' })
      const store = useTurnStore()
      await store.ask('测试问题', 'fast')
      const callbacks = capturedCallbacks()
      mockedGet.mockRejectedValueOnce(new Error('net down'))
      callbacks.onEvent(wire(9, 'done', { finish: 'completed', degraded: false, degraded_reason: null, duration_ms: 1, source_count: 0 }))
      await vi.advanceTimersByTimeAsync(0)
      expect(store.sseState).toBe('idle')
      expect(mockedToast).toHaveBeenCalledTimes(1)
      expect(store.turns[TURN_ID]!.status).toBe('completed') // done 事件已本地置终态 → 无需重试
      await vi.advanceTimersByTimeAsync(60_000)
      expect(mockedGet).toHaveBeenCalledTimes(1) // 终态已收敛：不重试
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('seq 持久化节流（P3：persistSeq 200ms 一次；控制事件/终态立即落）', () => {
  it('delta 事件窗口内节流跳过（内存 lastSeq 照常推进）；控制事件/终态立即落', async () => {
    vi.useFakeTimers()
    try {
      const store = useTurnStore()
      mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID, status: 'running' })
      await store.ask('测试问题', 'fast')
      const key = `ai-search.seq.${TURN_ID}`
      store.applyEvent(TURN_ID, wire(1, 'token', { delta: 'a' }))
      expect(sessionStorage.getItem(key)).toBe('1') // 首写立即（上次落盘为 0 时刻）
      store.applyEvent(TURN_ID, wire(2, 'token', { delta: 'b' }))
      expect(store.turns[TURN_ID]!.lastSeq).toBe(2) // 内存照常推进
      expect(sessionStorage.getItem(key)).toBe('1') // 200ms 窗口内节流跳过
      await vi.advanceTimersByTimeAsync(200)
      store.applyEvent(TURN_ID, wire(3, 'sources', { batch: [], total: 0 }))
      expect(sessionStorage.getItem(key)).toBe('3') // 控制事件立即落
      store.applyEvent(TURN_ID, wire(4, 'token', { delta: 'c' }))
      expect(sessionStorage.getItem(key)).toBe('3') // 刚落过 → 窗口内跳过
      store.applyEvent(TURN_ID, wire(9, 'done', { finish: 'completed', degraded: false, degraded_reason: null, duration_ms: 1, source_count: 0 }))
      expect(sessionStorage.getItem(key)).toBe('9') // 终态立即落（崩溃恢复游标不落后于终态）
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('subscribe()（生命周期 5/11：fromSeq 门与防双连接）', () => {
  it('已有流式状态的轮（路由切回）：fromSeq=turn.lastSeq（本页内容含 ≤lastSeq 增量）', async () => {
    mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID, status: 'running' })
    const store = useTurnStore()
    await store.ask('测试问题', 'fast')
    store.applyEvent(TURN_ID, wire(5, 'token', { delta: 'x' }))
    mockedSubscribe.mockClear()
    store.subscribe(TURN_ID)
    expect(mockedSubscribe.mock.calls[0]![0].fromSeq).toBe(5)
  })

  it('刷新后新页（turn.lastSeq=0）：fromSeq=0 全量重放重建（原生 EventSource 无法首连带头，幂等重建）', async () => {
    sessionStorage.setItem(`ai-search.seq.${TURN_ID}`, '9')
    mockedGet.mockResolvedValueOnce(makeDetail({ status: 'running', answer_md: '' }))
    const store = useTurnStore()
    await store.reconcile(TURN_ID)
    store.subscribe(TURN_ID)
    expect(mockedSubscribe.mock.calls[0]![0].fromSeq).toBe(0)
  })

  it('本地游标早于 compactSeq（v1.4）：放弃增量、fromSeq=0 全量对账重建', async () => {
    mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID, status: 'running' })
    const store = useTurnStore()
    await store.ask('测试问题', 'deep')
    const turn = store.turns[TURN_ID]!
    turn.compactSeq = 20 // 模拟 context_compact 记录在案的边界（> lastSeq 的形态防御）
    turn.lastSeq = 5
    mockedSubscribe.mockClear()
    store.subscribe(TURN_ID)
    expect(mockedSubscribe.mock.calls[0]![0].fromSeq).toBe(0)
  })

  it('未知轮不订阅（编排层须先 reconcile）', () => {
    const store = useTurnStore()
    store.subscribe(TURN_ID)
    expect(mockedSubscribe).not.toHaveBeenCalled()
  })

  it('sseState 映射：connected/reconnecting 直传；held/closed/connecting → idle', async () => {
    mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID, status: 'running' })
    const store = useTurnStore()
    await store.ask('测试问题', 'fast')
    const callbacks = capturedCallbacks()
    const cases: [TurnStreamState, 'connected' | 'reconnecting' | 'idle'][] = [
      ['connected', 'connected'],
      ['reconnecting', 'reconnecting'],
      ['held', 'idle'],
      ['closed', 'idle'],
      ['connecting', 'idle'],
    ]
    for (const [transport, expected] of cases) {
      callbacks.onStateChange(transport)
      expect(store.sseState).toBe(expected)
    }
  })

  it('onClosed（异常收口）且轮非终态 → 触发 REST 对账；终态轮不再对账', async () => {
    mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID, status: 'running' })
    const store = useTurnStore()
    await store.ask('测试问题', 'fast')
    const callbacks = capturedCallbacks()
    mockedGet.mockResolvedValueOnce(makeDetail({ status: 'failed', error: 'boom' }))
    callbacks.onClosed({ afterError: true, duringHold: false })
    await vi.waitFor(() => expect(store.turns[TURN_ID]!.status).toBe('failed'))

    mockedGet.mockClear()
    store.applyEvent(TURN_ID, wire(9, 'done', { finish: 'completed', degraded: false, degraded_reason: null, duration_ms: 1, source_count: 0 }))
    callbacks.onClosed({ afterError: false, duringHold: false })
    expect(mockedGet).not.toHaveBeenCalled()
  })

  it('done 到达即对账（生命周期 4；v1.9 注：对账照旧在 done 到达时执行）', async () => {
    mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID, status: 'running' })
    const store = useTurnStore()
    await store.ask('测试问题', 'fast')
    const callbacks = capturedCallbacks()
    mockedGet.mockResolvedValueOnce(makeDetail({ status: 'completed', answer_md: 'REST 答案' }))
    callbacks.onEvent(wire(9, 'done', { finish: 'completed', degraded: false, degraded_reason: null, duration_ms: 1, source_count: 0 }))
    await vi.waitFor(() => expect(store.turns[TURN_ID]!.answer_md).toBe('REST 答案'))
  })

  it('closeStream（路由切走）：传输层注册表关闭 + liveTurnId/sseState 复位', async () => {
    mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID, status: 'running' })
    const store = useTurnStore()
    await store.ask('测试问题', 'fast')
    store.closeStream(TURN_ID)
    expect(mockedClose).toHaveBeenCalledWith(TURN_ID)
    expect(store.liveTurnId).toBeNull()
    expect(store.sseState).toBe('idle')
  })
})

describe('cancel()（§2.7：running 与 queued 同入口）', () => {
  it('POST cancel 端点并透传响应', async () => {
    mockedPost.mockResolvedValueOnce({ turn_id: TURN_ID, status: 'canceling' })
    const store = useTurnStore()
    const data = await store.cancel(TURN_ID)
    expect(mockedPost).toHaveBeenCalledWith(`/api/v1/turns/${TURN_ID}/cancel`)
    expect(data.status).toBe('canceling')
  })
})

describe('calibrateThread()（生命周期 12：进入页面/切回以 2.3 turns[].status 校准生成中集合）', () => {
  it('存在 running/queued → 加入；全终态 → 移除', () => {
    const store = useTurnStore()
    store.calibrateThread(THREAD_ID, ['completed', 'running'])
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(true)
    store.calibrateThread(THREAD_ID, ['completed', 'canceled'])
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(false)
  })
})

describe('done.memory_saved 数据形态就绪（M5 toast 数据，无痕轮恒无）', () => {
  it('done 携带 memory_saved>0 时记录；缺省（无痕轮/未携带）为 null', async () => {
    mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID, status: 'running' })
    const store = useTurnStore()
    await store.ask('测试问题', 'fast')
    store.applyEvent(TURN_ID, wire(9, 'done', { finish: 'completed', degraded: false, degraded_reason: null, duration_ms: 1, source_count: 0 }))
    expect(store.turns[TURN_ID]!.memorySaved).toBeNull()
    store.applyEvent(TURN_ID, wire(10, 'done', { finish: 'completed', degraded: false, degraded_reason: null, duration_ms: 1, source_count: 0, memory_saved: 2 }))
    expect(store.turns[TURN_ID]!.memorySaved).toBe(2)
  })
})

describe('fixture 一致性（镜像锁死：TURN_ID_FAST 序列可被本 store 直接消费）', () => {
  it('turnDetails fixture 的线程/轮 id 与本测试常量同构（防 fixture 漂移）', () => {
    expect(THREAD_ID_FAST).toBe(THREAD_ID)
    expect(TURN_ID_FAST).toBe(TURN_ID)
  })
})
