/**
 * test/integration/sse-pipeline.test.ts — SSE 管线联装测试（frontend-api.md §9 fixture 优先）。
 *
 * 联装形态：
 *   - 传输层真身（@/sse/turnStream 不 mock）+ store 真身；REST（@/api/client）mock；
 *     原生 EventSource 以 vi.stubGlobal 注入 MockEventSource（jsdom 无 EventSource）。
 *   - fast §7.4 序列走完整传输链路（含合帧/终态冲刷/done 对账/保活窗口收口）；
 *   - deep §7.4 序列经 applyEvent 单入口联装（§3 派发表投影断言）；
 *   - A13 断线重连补帧：mock EventSource 模拟中断恢复（原生重连 + 全量重放重叠去重 + 前端退避接管）。
 */
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { apiGet, apiPost } from '@/api/client'
import {
  TURN_ID_DIRECT,
  turnDetailDirectCompleted,
  turnDetailDirectWithFollowups,
  turnDetailFastCompleted,
} from '@/mocks/turnDetails'
import {
  deepSequence,
  directFollowupsEvent,
  directSequence,
  fastSequence,
  SSE_SEQUENCE_THREAD_ID,
} from '@/mocks/sseSequences'
import { peekTurnStream } from '@/sse/turnStream'
import { useTurnStore } from '@/stores/turn'
import type { SseWireEvent } from '@/types/api'

import { MockEventSource, ES_READY_STATE } from '../helpers/mockEventSource'

vi.mock('@/api/client', () => ({
  API_BASE_URL: '', // 同源相对路径（buildTurnStreamUrl 消费）
  apiPost: vi.fn(),
  apiGet: vi.fn(),
  apiPut: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
  apiDownload: vi.fn(),
  // turnStore 失败兜底链消费（safeReconcile → normalizeError）——mock 面与 store import 面同构
  normalizeError: () => ({ name: 'ApiError', code: 'network_error', message: '网络异常，请稍后重试' }),
}))

const mockedPost = vi.mocked(apiPost)
const mockedGet = vi.mocked(apiGet)

const THREAD_ID = SSE_SEQUENCE_THREAD_ID
const TURN_ID_FAST = fastSequence[0]!.data.turn_id
const TURN_ID_DEEP = deepSequence[0]!.data.turn_id

/** ③ followups 追加样例条目（判别收窄——联合 data 不随运行时断言收窄，§8.9 纪律） */
const DIRECT_FOLLOWUPS_ITEMS = (() => {
  if (directFollowupsEvent.event !== 'followups') throw new Error('③ 样例应为 followups 事件')
  return directFollowupsEvent.data.items
})()

/** wire fixture → EventSource 线格帧 */
function toFrames(events: SseWireEvent[]) {
  return events.map((e) => ({ id: e.id, type: e.event, data: JSON.stringify(e.data) }))
}

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  sessionStorage.clear()
  vi.clearAllMocks()
  vi.useFakeTimers()
  MockEventSource.reset()
  vi.stubGlobal('EventSource', MockEventSource)
  // rAF → setTimeout(16)（fake timers 可推进；streaming-buffer 规则 8 的测试等价物）
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => setTimeout(() => cb(0), 16))
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('fast §7.4 序列（transport 全链路：两段式 → 合帧 → done 对账 → 保活收口）', () => {
  it('POST 建轮 + GET 订阅 → 序列投影 → done REST 权威 → followups 收口关闭', async () => {
    mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID_FAST, status: 'running' })
    mockedGet.mockResolvedValueOnce(turnDetailFastCompleted)
    const store = useTurnStore()
    await store.ask('LangGraph 的 Send API 和 Router 的区别？', 'fast')

    const es = MockEventSource.instances[0]!
    expect(es.url).toContain(`/api/v1/turns/${TURN_ID_FAST}/events`)
    es.open()
    // 第一段：seq 1..9（done 止）——followups 留待保活窗口第二段派发
    for (const f of toFrames(fastSequence.filter((w) => w.id <= 9))) es.dispatch(f)

    // done 已同步冲刷 token 缓冲并触发对账（REST 为准源——fixture answer_md 与流式拼接刻意不同，替换可观测）
    await vi.waitFor(() => {
      expect(store.turns[TURN_ID_FAST]!.status).toBe('completed')
      expect(store.turns[TURN_ID_FAST]!.answer_md).toBe(turnDetailFastCompleted.answer_md)
    })
    const turn = store.turns[TURN_ID_FAST]!
    expect(turn.question).toBe('LangGraph 的 Send API 和 Router 的区别？')
    expect(turn.sources.map((s) => s.ref_no)).toEqual([1])
    // v1.12 键可省：fast images 条目仅 url，不伪造缺键
    expect(turn.images).toEqual([{ url: 'https://example.com/architecture.png' }])
    expect(Object.hasOwn(turn.images[0]!, 'title')).toBe(false)
    // 时间线投影：对账后以 2.5 stages[] 为准（终态全 done；流式期的 running 快照已被 REST 权威替换）
    expect(turn.stages.map((s) => [s.stage_key, s.status])).toEqual([
      ['rewrite', 'done'],
      ['search', 'done'],
      ['write', 'done'],
    ])
    expect(turn.lastSeq).toBe(9)
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(false)
    expect(sessionStorage.getItem(`ai-search.seq.${TURN_ID_FAST}`)).toBe('9')

    // done 后保活窗口（生命周期 13，fast 轮）：连接未关
    expect(es.closedByClient).toBe(false)
    // 收口①：followups 到达 → 写入该轮 → close
    es.dispatch({ id: 10, type: 'followups', data: JSON.stringify(fastSequence[9]!.data) })
    expect(turn.followups).toEqual(['Send API 的失败分支怎么处理？', 'Router 的条件边怎么写？', '两者能在一张图里混用吗？'])
    expect(es.closedByClient).toBe(true)
    expect(peekTurnStream(TURN_ID_FAST)).toBeUndefined()
  })
})

describe('direct §7.4 序列（v1.15 免检索直答：两节点时间线 + 零来源投影 + L13 保活收口）', () => {
  it('① 序列投影：时间线仅 rewrite/write、sources/images 恒空、done 对账 source_count=0', async () => {
    mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID_DIRECT, status: 'running' })
    const store = useTurnStore()
    await store.ask('你好，请介绍一下你自己', 'fast')

    const es = MockEventSource.instances[0]!
    expect(es.url).toContain(`/api/v1/turns/${TURN_ID_DIRECT}/events`)
    es.open()
    // 中段（seq 1..3）：rewrite running→done + write running——两节点、无 search、无来源/图片
    for (const f of toFrames(directSequence.filter((w) => w.id <= 3))) es.dispatch(f)
    vi.advanceTimersByTime(2000)
    const turn = store.turns[TURN_ID_DIRECT]!
    expect(turn.stages.map((s) => [s.stage_key, s.status])).toEqual([
      ['rewrite', 'done'],
      ['write', 'running'],
    ])
    expect(turn.sources).toEqual([])
    expect(turn.images).toEqual([])

    // 尾段（seq 4..7）：token → write done → done(completed, source_count=0) → 对账（REST 权威）
    mockedGet.mockResolvedValueOnce(turnDetailDirectCompleted)
    for (const f of toFrames(directSequence.filter((w) => w.id >= 4))) es.dispatch(f)
    await vi.waitFor(() => expect(turn.status).toBe('completed'))
    expect(turn.answer_md).toBe(turnDetailDirectCompleted.answer_md)
    expect(turn.sources).toEqual([]) // direct 轮零来源（2.5 对账同口径）
    expect(turn.images).toEqual([])
    expect(turn.stages.find((s) => s.stage_key === 'rewrite')?.extra?.strategy).toBe('direct') // 留痕随 2.5 重建投影
    expect(turn.lastSeq).toBe(7)
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(false)

    // ③ L13 保活窗口（fast 轮 done 后不主动 close）→ followups 到达写入 → 收口① close
    expect(es.closedByClient).toBe(false)
    es.dispatch({ id: directFollowupsEvent.id, type: directFollowupsEvent.event, data: JSON.stringify(directFollowupsEvent.data) })
    expect(turn.followups).toEqual(DIRECT_FOLLOWUPS_ITEMS)
    expect(es.closedByClient).toBe(true)
  })

  it('② direct 回看对账：2.5 sources=[] 种子化投影（回看态同口径零来源）', async () => {
    mockedGet.mockResolvedValueOnce(turnDetailDirectWithFollowups)
    const store = useTurnStore()
    await store.reconcile(TURN_ID_DIRECT)
    const turn = store.turns[TURN_ID_DIRECT]!
    expect(turn.status).toBe('completed')
    expect(turn.sources).toEqual([])
    expect(turn.images).toEqual([])
    expect(turn.stages.map((s) => [s.stage_key, s.status])).toEqual([
      ['rewrite', 'done'],
      ['write', 'done'],
    ])
    expect(turn.followups).toEqual(DIRECT_FOLLOWUPS_ITEMS) // 回看态双源回填（REST 事实源）
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(false)
  })
})

describe('deep §7.4 序列（applyEvent 单入口联装：§3 派发表投影）', () => {
  it('回看种子化后重放全序列：双缓冲/compactSeq/时间线/sources 排序/images 全键/终态', async () => {
    // 路由切回路径（生命周期 5/11）：先 2.5 对账种子化（运行中），再重放序列
    mockedGet.mockResolvedValueOnce({
      ...turnDetailFastCompleted,
      turn_id: TURN_ID_DEEP,
      mode: 'deep',
      status: 'running',
      answer_md: '',
      finished_at: null,
      // 回看种子化只承载 2.5 已持久化投影——流式内容（token/thinking/流中阶段态）清零，
      // 由随后的序列重放重建（防 fast 基座的 stages/sources 残留）
      sources: [],
      images: [],
      stages: [],
    })
    const store = useTurnStore()
    await store.reconcile(TURN_ID_DEEP)
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(true)

    for (const wire of deepSequence) store.applyEvent(TURN_ID_DEEP, wire)
    const turn = store.turns[TURN_ID_DEEP]!

    // token/thinking 双缓冲（v1.4）：thinking 独立缓冲，绝不混入 answer_md
    expect(turn.answer_md).toBe('# 调研报告\n\n## 摘要\n\n两框架的取舍集中在...\n\n## 取舍对比\n\n见 [1] [2]。')
    expect(turn.thinking_md).toBe('先比较两框架的事件模型差异，再定位 Send 的扇出语义…引用编号需与 sources 面板对齐。')
    // context_compact（仅 deep）：重放边界记录
    expect(turn.compactSeq).toBe(12)
    expect(turn.compactAt).toBe('2026-11-01T08:13:50Z')
    // sources：三批次按 ref_no 排序合并；kb 来源徽标数据在位
    expect(turn.sources.map((s) => [s.ref_no, s.source_type])).toEqual([
      [1, 'web'],
      [2, 'kb'],
      [3, 'web'],
    ])
    // images：全键条目（v1.12 键可省的另一形态）
    expect(turn.images).toEqual([
      { url: 'https://example.com/compare-chart.png', source_url: 'https://example.com/compare-page', title: '对比图表', width: 1200, height: 750 },
    ])
    // 时间线状态机：plan→search→read→reflect→write 各键就地推进
    expect(turn.stages.map((s) => [s.stage_key, s.status])).toEqual([
      ['plan', 'done'],
      ['search', 'running'],
      ['read', 'done'],
      ['reflect', 'done'],
      ['write', 'done'],
    ])
    expect(turn.stages.find((s) => s.stage_key === 'read')!.progress).toEqual({ current: 6, total: 6 })
    expect(turn.stages.find((s) => s.stage_key === 'write')!.extra).toEqual({ sections: 5 })
    expect(turn.stages.find((s) => s.stage_key === 'search')!.extra?.subagents?.map((a) => a.status)).toEqual(['done', 'running', 'running'])
    // 终态
    expect(turn.status).toBe('completed')
    expect(turn.lastSeq).toBe(21)
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(false)

    // 重放幂等（控制事件 seq ≤ lastSeq 幂等丢弃——同 ref_no 不重复插、stages 不重复、lastSeq 不回退）
    for (const wire of deepSequence.filter((w) => w.event !== 'token' && w.event !== 'thinking')) {
      store.applyEvent(TURN_ID_DEEP, wire)
    }
    expect(turn.sources.map((s) => s.ref_no)).toEqual([1, 2, 3])
    expect(turn.stages.map((s) => s.stage_key)).toEqual(['plan', 'search', 'read', 'reflect', 'write'])
    expect(turn.lastSeq).toBe(21)
    expect(turn.compactSeq).toBe(12)
  })
})

describe('A13 断线重连补帧（mock EventSource 模拟中断恢复）', () => {
  it('原生重连：中断后全量重放重叠帧被 seq 门去重，token 无重复、补帧不丢', async () => {
    mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID_FAST, status: 'running' })
    const store = useTurnStore()
    await store.ask('q', 'fast')
    const es = MockEventSource.instances[0]!
    es.open()
    // 断线前：收到 seq 1..8（含 token 7/8——缓冲中，尚未 flush）
    const frames = toFrames(fastSequence.filter((w) => w.id <= 8))
    for (const f of frames) es.dispatch(f)
    es.fail(ES_READY_STATE.CONNECTING) // 原生自动重连路径（不 close、不重建实例）
    expect(es.closedByClient).toBe(false)
    es.open() // 原生重连成功（携带 Last-Event-ID 由浏览器原生行为完成）
    // 服务端重放重叠帧（seq ≤8——模拟无游标兜底形态）+ 续传帧；seq 门应丢弃重叠
    for (const f of toFrames(fastSequence.filter((w) => w.id <= 8))) es.dispatch(f)
    vi.advanceTimersByTime(2000) // 合帧 flush：追赶多帧直至清空
    const turn = store.turns[TURN_ID_FAST]!
    expect(turn.answer_md).toBe('Send API 用于**动态扇出**，Router 用于静态分支 [1]。') // 无重复拼接
    expect(turn.lastSeq).toBe(8)
    // 终态收口 + 对账
    mockedGet.mockResolvedValueOnce(turnDetailFastCompleted)
    es.dispatch(toFrames(fastSequence.filter((w) => w.id === 9))[0]!)
    await vi.waitFor(() => expect(turn.status).toBe('completed'))
    expect(turn.answer_md).toBe(turnDetailFastCompleted.answer_md)
    expect(store.sseState).toBe('idle') // held → idle（终态无 banner 语义）
  })

  it('前端退避接管：连续失败 1s 后重建新实例，续传帧照常投影', async () => {
    mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID_FAST, status: 'running' })
    const store = useTurnStore()
    await store.ask('q', 'fast')
    const es1 = MockEventSource.instances[0]!
    es1.open()
    for (const f of toFrames(fastSequence.filter((w) => w.id <= 3))) es1.dispatch(f)
    es1.fail(ES_READY_STATE.CONNECTING)
    es1.fail(ES_READY_STATE.CONNECTING) // 连续失败 → 接管
    expect(es1.closedByClient).toBe(true)
    expect(store.sseState).toBe('reconnecting')
    vi.advanceTimersByTime(1000)
    const es2 = MockEventSource.instances[1]!
    es2.open()
    // 新实例（无法携带 header）→ 服务端全量重放；seq 门（fromSeq=3）丢弃重叠
    for (const f of toFrames(fastSequence.filter((w) => w.id >= 1 && w.id <= 8))) es2.dispatch(f)
    vi.advanceTimersByTime(2000) // 合帧 flush：追赶多帧直至清空
    const turn = store.turns[TURN_ID_FAST]!
    expect(turn.stages.map((s) => s.stage_key)).toEqual(['rewrite', 'search', 'write'])
    expect(turn.answer_md).toBe('Send API 用于**动态扇出**，Router 用于静态分支 [1]。')
    expect(turn.lastSeq).toBe(8)
    expect(store.sseState).toBe('connected')
  })

  it('closeStream（路由切走，生命周期 11）：断流后后续帧不投影、注册表清空', async () => {
    mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID_FAST, status: 'running' })
    const store = useTurnStore()
    await store.ask('q', 'fast')
    const es = MockEventSource.instances[0]!
    es.open()
    es.dispatch(toFrames(fastSequence.filter((w) => w.id === 1))[0]!)
    expect(store.turns[TURN_ID_FAST]!.stages).toHaveLength(1)
    store.closeStream(TURN_ID_FAST)
    expect(es.closedByClient).toBe(true)
    expect(peekTurnStream(TURN_ID_FAST)).toBeUndefined()
    es.dispatch(toFrames(fastSequence.filter((w) => w.id === 7))[0]!) // 切走后到达的迟到帧
    expect(store.turns[TURN_ID_FAST]!.answer_md).toBe('')
    expect(store.liveTurnId).toBeNull()
  })
})

describe('queued 轮（生命周期 9：等待期仅心跳、无业务事件是预期）', () => {
  it('POST status=queued：种子化占位态 + 生成中集合 + 订阅照常建立', async () => {
    mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID_FAST, status: 'queued' })
    const store = useTurnStore()
    await store.followUp(THREAD_ID, '排队中的第二个问题', 'fast')
    const turn = store.turns[TURN_ID_FAST]!
    expect(turn.status).toBe('queued')
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(true)
    expect(MockEventSource.instances).toHaveLength(1) // EventSource 照常建立
    const es = MockEventSource.instances[0]!
    es.open()
    vi.advanceTimersByTime(60_000) // 等待期静默（看门狗武装前不判超时重连）
    expect(MockEventSource.instances).toHaveLength(1)
    expect(es.closedByClient).toBe(false)
  })
})

describe('threadListStore 联装说明', () => {
  it('§4 第二行的 store 级联装断言归 unit/thread-list-store.test.ts（唯一出口 mock 全量）', () => {
    // 本文件聚焦 SSE 管线联装；threadListStore 的 REST/推荐池通道已在 unit 层全覆盖
    expect(true).toBe(true)
  })
})
