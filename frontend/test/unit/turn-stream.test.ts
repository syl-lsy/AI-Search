/**
 * test/unit/turn-stream.test.ts — SSE 传输层纯逻辑单测（零 DOM 零网络，EventSource 以替身注入）。
 *
 * 覆盖（frontend-api.md §3 连接生命周期 + §8.7 + streaming-buffer 技能规范）：
 *   - 重连退避计算（§3 生命周期 2：1s/2s/4s，上限 15s）
 *   - 合帧节奏（ChannelBuffer：floor=2 / k=0.12 / 有界 64KB 降级 / flushAll）
 *   - seq 排序与去重（重放幂等，streaming-buffer 规则 24）
 *   - Last-Event-ID 携带的原生路径保留（首次错误不 close → 浏览器原生自动重连带头；
 *     连续失败才前端接管指数退避——api.md §7.3 第 1 条「浏览器默认 ~3s，前端叠加指数退避」）
 *   - 心跳看门狗的可实现近似（原生 EventSource 不派发 `: ping` 注释行 → 以
 *     「武装后 3×心跳静默」近似 §3 生命周期 3；武装前静默不判超时——生命周期 9 排队轮）
 *   - done/终态/保活窗口（生命周期 4/13）/ error 非终态（§7.2）
 *   - 连接注册表防双连接（生命周期 11）
 *   - visibilitychange 隐藏保持接收、回前台冲刷积压（生命周期 11 / streaming-buffer 规则 8）
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  ChannelBuffer,
  HEARTBEAT_WATCHDOG_MS,
  SSE_HEARTBEAT_SECONDS,
  buildTurnStreamUrl,
  closeTurnStream,
  computeBackoffDelayMs,
  peekTurnStream,
  subscribeTurn,
} from '@/sse/turnStream'
import type { SubscribeTurnOptions, TurnStreamState } from '@/sse/turnStream'
import type { SseWireEvent } from '@/types/api'

import { ES_READY_STATE, MockEventSource, mockEventSourceFactory } from '../helpers/mockEventSource'

const TURN_ID = 'c31b9d74-2a41-4f6e-b3c2-5d6e7f809a1b'

/** 构造线格事件帧（id=seq；data=§7.2 payload JSON 单行） */
function frame(id: number, type: string, payload: Record<string, unknown>) {
  return {
    id,
    type,
    data: JSON.stringify({ seq: id, turn_id: TURN_ID, ts: '2026-11-01T08:12:00Z', ...payload }),
  }
}

let rafOriginal: ((cb: FrameRequestCallback) => number) | undefined

beforeEach(() => {
  vi.useFakeTimers()
  MockEventSource.reset()
  // rAF 统一替身 → setTimeout(16)，fake timers 可推进（streaming-buffer 规则 8 的测试等价物）
  rafOriginal = typeof globalThis.requestAnimationFrame === 'function'
    ? globalThis.requestAnimationFrame.bind(globalThis)
    : undefined
  Object.defineProperty(globalThis, 'requestAnimationFrame', {
    value: (cb: FrameRequestCallback) => setTimeout(() => cb(0), 16),
    configurable: true,
    writable: true,
  })
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  if (rafOriginal) {
    Object.defineProperty(globalThis, 'requestAnimationFrame', {
      value: rafOriginal,
      configurable: true,
      writable: true,
    })
  } else {
    // @ts-expect-error 测试环境恢复：jsdom 原生无 rAF 时删除测试注入的替身
    delete globalThis.requestAnimationFrame
  }
})

/** 控制器测试脚手架：注册 + 收集回调 + 返回首个 EventSource 替身 */
function setup(opts?: Partial<SubscribeTurnOptions>) {
  const onEvent = vi.fn<(e: SseWireEvent) => void>()
  const onStateChange = vi.fn<(s: TurnStreamState) => void>()
  const onClosed = vi.fn<(info: { afterError: boolean; duringHold: boolean }) => void>()
  const controller = subscribeTurn({
    turnId: TURN_ID,
    callbacks: { onEvent, onStateChange, onClosed },
    eventSourceFactory: mockEventSourceFactory(),
    ...opts,
  })
  const es = MockEventSource.instances[0]!
  return { controller, es, onEvent, onStateChange, onClosed }
}

describe('computeBackoffDelayMs（§3 生命周期 2：1s/2s/4s，上限 15s）', () => {
  it('前四次指数：1000/2000/4000/8000', () => {
    expect(computeBackoffDelayMs(0)).toBe(1000)
    expect(computeBackoffDelayMs(1)).toBe(2000)
    expect(computeBackoffDelayMs(2)).toBe(4000)
    expect(computeBackoffDelayMs(3)).toBe(8000)
  })

  it('第 5 次起封顶 15000', () => {
    expect(computeBackoffDelayMs(4)).toBe(15000)
    expect(computeBackoffDelayMs(10)).toBe(15000)
  })
})

describe('ChannelBuffer（streaming-buffer：floor=2 / k=0.12 / 有界 / flushAll）', () => {
  function makeBuffer() {
    const flushes: { text: string; maxSeq: number }[] = []
    const scheduled: (() => void)[] = []
    const buffer = new ChannelBuffer(
      (text, maxSeq) => flushes.push({ text, maxSeq }),
      (fn) => {
        scheduled.push(fn)
      },
    )
    return { buffer, flushes, scheduled }
  }

  it('floor=2：单字符积压每帧至少消费 2 字符', () => {
    const { buffer, flushes, scheduled } = makeBuffer()
    buffer.push('a', 1)
    expect(buffer.pendingChars).toBe(1)
    scheduled[0]!()
    expect(flushes).toEqual([{ text: 'a', maxSeq: 1 }])
    expect(buffer.pendingChars).toBe(0)
  })

  it('追赶系数 k=0.12：100 字符积压每帧消费 ceil(100×0.12)=12 字符', () => {
    const { buffer, flushes, scheduled } = makeBuffer()
    buffer.push('x'.repeat(100), 1)
    scheduled[0]!()
    expect(flushes[0]!.text).toHaveLength(12)
    expect(buffer.pendingChars).toBe(88)
  })

  it('多帧追赶：积压按帧递减直至清空', () => {
    const { buffer, flushes, scheduled } = makeBuffer()
    buffer.push('y'.repeat(30), 1)
    scheduled[0]!() // ceil(30*.12)=4
    scheduled[1]!() // ceil(26*.12)=4
    scheduled[2]!() // ceil(22*.12)=3
    const drained = flushes.reduce((sum, f) => sum + f.text.length, 0)
    expect(drained).toBe(11)
    expect(buffer.pendingChars).toBe(19)
  })

  it('多帧合帧：同帧多 delta 合并为单次 flush，maxSeq 取批内最大', () => {
    const { buffer, flushes, scheduled } = makeBuffer()
    buffer.push('abcdefghij', 3) // 10 chars
    buffer.push('klmnopqrst', 7) // 10 chars → 积压 20
    scheduled[0]!() // budget = max(2, ceil(20×0.12)) = 3
    expect(flushes).toEqual([{ text: 'abc', maxSeq: 7 }])
  })

  it('flushAll：终态冲刷剩余缓冲一次性交付（streaming-buffer 规则 10）', () => {
    const { buffer, flushes, scheduled } = makeBuffer()
    buffer.push('he', 1)
    buffer.push('llo', 2)
    expect(scheduled).toHaveLength(1) // push 时仅排程一次
    buffer.flushAll()
    expect(flushes).toEqual([{ text: 'hello', maxSeq: 2 }])
    expect(buffer.pendingChars).toBe(0)
    // 队列已空：再 flushAll 不重复交付；遗留的帧回调触发后也不重复交付
    buffer.flushAll()
    scheduled[0]!()
    expect(flushes).toHaveLength(1)
  })

  it('有界缓冲：积压超 64KB 立即全量 flush 降级为整段渲染（规则 13，不等帧）', () => {
    const { buffer, flushes, scheduled } = makeBuffer()
    buffer.push('z'.repeat(70_000), 9)
    expect(flushes).toHaveLength(1)
    expect(flushes[0]!.text).toHaveLength(70_000)
    expect(flushes[0]!.maxSeq).toBe(9)
    expect(scheduled).toHaveLength(0)
  })

  it('空 delta 忽略；不排程无谓帧', () => {
    const { buffer, scheduled } = makeBuffer()
    buffer.push('', 1)
    expect(buffer.pendingChars).toBe(0)
    expect(scheduled).toHaveLength(0)
  })
})

describe('buildTurnStreamUrl（§3：GET /api/v1/turns/{turnId}/events，经 vite proxy/直连 origin 拼装）', () => {
  it('契约完整路径 + API_BASE_URL 前缀', () => {
    expect(buildTurnStreamUrl(TURN_ID)).toBe(`${import.meta.env.VITE_API_BASE_URL || ''}/api/v1/turns/${TURN_ID}/events`)
  })
})

describe('seq 门：重放去重与幂等（streaming-buffer 规则 24）', () => {
  it('同 seq 重复派发只应用一次；乱序迟到（seq 回退）丢弃', () => {
    const { es, onEvent } = setup()
    es.open()
    es.dispatch(frame(1, 'stage', { stage_key: 'rewrite', status: 'running' }))
    es.dispatch(frame(1, 'stage', { stage_key: 'rewrite', status: 'running' })) // 重放重复
    es.dispatch(frame(3, 'stage', { stage_key: 'search', status: 'running' }))
    es.dispatch(frame(2, 'stage', { stage_key: 'rewrite', status: 'done' })) // 迟到旧事件
    const seqs = onEvent.mock.calls.map((c) => c[0]!.id)
    expect(seqs).toEqual([1, 3])
  })

  it('畸形 data（非法 JSON / 缺 seq）与未知事件名静默忽略（前向兼容）', () => {
    const { es, onEvent } = setup()
    es.open()
    es.dispatch({ id: 1, type: 'stage', data: 'not-json' })
    es.dispatch({ id: 2, type: 'stage', data: JSON.stringify({ turn_id: TURN_ID }) })
    es.dispatch({ id: 3, type: 'future_event', data: JSON.stringify({ seq: 3, turn_id: TURN_ID }) })
    expect(onEvent).not.toHaveBeenCalled()
  })

  it('心跳 `: ping` 不占 seq：注释行浏览器不派发，seq 门不受影响（§7.1）', () => {
    const { es, onEvent } = setup()
    es.open()
    es.dispatch(frame(1, 'stage', { stage_key: 'rewrite', status: 'running' }))
    // 模拟心跳期间无任何派发（注释行不可见）→ 随后正常事件照常通过 seq 门
    es.dispatch(frame(2, 'token', { delta: 'A' }))
    expect(onEvent).toHaveBeenCalledTimes(1)
    expect(onEvent.mock.calls[0]![0].id).toBe(1)
  })
})

describe('重连（§3 生命周期 2 + api.md §7.3：原生先行携带 Last-Event-ID，前端叠加指数退避）', () => {
  it('首次错误不 close（保留原生自动重连带 Last-Event-ID 路径）→ 状态 reconnecting', () => {
    const { es, onStateChange } = setup()
    es.open()
    es.dispatch(frame(1, 'stage', { stage_key: 'rewrite', status: 'running' }))
    es.fail(ES_READY_STATE.CONNECTING)
    expect(es.closedByClient).toBe(false)
    expect(onStateChange).toHaveBeenLastCalledWith('reconnecting')
    // 原生重连等待期内不创建新实例（同一对象原生重连）
    vi.advanceTimersByTime(30_000)
    expect(MockEventSource.instances).toHaveLength(1)
  })

  it('原生重连成功（再次 open）→ 恢复 connected、退避计数归零', () => {
    const { es, onStateChange } = setup()
    es.open()
    es.fail(ES_READY_STATE.CONNECTING)
    es.open() // 原生重连成功
    expect(onStateChange).toHaveBeenLastCalledWith('connected')
  })

  it('连续失败才前端接管：close 旧实例 + 1s 后重建；再失败 2s、4s、… 封顶 15s', () => {
    setup()
    const es1 = MockEventSource.instances[0]!
    es1.open()
    es1.fail(ES_READY_STATE.CONNECTING) // 第 1 次：原生模式
    es1.fail(ES_READY_STATE.CONNECTING) // 第 2 次：接管
    expect(es1.closedByClient).toBe(true)

    vi.advanceTimersByTime(999)
    expect(MockEventSource.instances).toHaveLength(1)
    vi.advanceTimersByTime(1)
    const es2 = MockEventSource.instances[1]!
    expect(es2.url).toContain(`/api/v1/turns/${TURN_ID}/events`)
    es2.open()
    es2.fail(ES_READY_STATE.CONNECTING)
    es2.fail(ES_READY_STATE.CONNECTING)
    vi.advanceTimersByTime(1999)
    expect(MockEventSource.instances).toHaveLength(2)
    vi.advanceTimersByTime(1)
    const es3 = MockEventSource.instances[2]!
    es3.open()
    es3.fail(ES_READY_STATE.CONNECTING)
    es3.fail(ES_READY_STATE.CONNECTING)
    vi.advanceTimersByTime(3999)
    expect(MockEventSource.instances).toHaveLength(3)
    vi.advanceTimersByTime(1)
    const es4 = MockEventSource.instances[3]!
    es4.open()
    es4.fail(ES_READY_STATE.CONNECTING)
    es4.fail(ES_READY_STATE.CONNECTING)
    vi.advanceTimersByTime(7999)
    expect(MockEventSource.instances).toHaveLength(4)
    vi.advanceTimersByTime(1)
    const es5 = MockEventSource.instances[4]!
    es5.open()
    es5.fail(ES_READY_STATE.CONNECTING)
    es5.fail(ES_READY_STATE.CONNECTING)
    vi.advanceTimersByTime(14_999)
    expect(MockEventSource.instances).toHaveLength(5)
    vi.advanceTimersByTime(1) // 封顶 15s
    expect(MockEventSource.instances).toHaveLength(6)
  })

  it('致命失败（readyState=CLOSED，非 200/非 event-stream）直接走前端退避路径', () => {
    setup()
    const es1 = MockEventSource.instances[0]!
    es1.fail(ES_READY_STATE.CLOSED)
    expect(es1.closedByClient).toBe(true)
    vi.advanceTimersByTime(1000)
    expect(MockEventSource.instances).toHaveLength(2)
  })
})

describe('心跳看门狗（§3 生命周期 3 的可实现近似：武装后 3×15s 静默 → close 再重连）', () => {
  it('常量口径：SSE_HEARTBEAT_SECONDS=15，看门狗=3×心跳', () => {
    expect(SSE_HEARTBEAT_SECONDS).toBe(15)
    expect(HEARTBEAT_WATCHDOG_MS).toBe(3 * SSE_HEARTBEAT_SECONDS * 1000)
  })

  it('武装前静默不判超时（生命周期 9：排队轮等待期仅心跳、勿判超时重连）', () => {
    const { es } = setup()
    es.open()
    vi.advanceTimersByTime(HEARTBEAT_WATCHDOG_MS * 10)
    expect(MockEventSource.instances).toHaveLength(1)
    expect(es.closedByClient).toBe(false)
  })

  it('武装后（首个业务事件）静默超阈值 → 主动 close 再重连（新实例）', () => {
    setup()
    const es1 = MockEventSource.instances[0]!
    es1.open()
    es1.dispatch(frame(1, 'stage', { stage_key: 'rewrite', status: 'running' }))
    vi.advanceTimersByTime(HEARTBEAT_WATCHDOG_MS - 1)
    expect(MockEventSource.instances).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(es1.closedByClient).toBe(true)
    expect(MockEventSource.instances).toHaveLength(2) // 看门狗立即重建
  })

  it('业务事件重置看门狗：44000ms 静默 + 事件 + 44000ms 静默不触发', () => {
    const { es } = setup()
    es.open()
    es.dispatch(frame(1, 'stage', { stage_key: 'rewrite', status: 'running' }))
    vi.advanceTimersByTime(HEARTBEAT_WATCHDOG_MS - 1000)
    es.dispatch(frame(2, 'token', { delta: 'x' }))
    vi.advanceTimersByTime(HEARTBEAT_WATCHDOG_MS - 1000)
    expect(MockEventSource.instances).toHaveLength(1)
    expect(es.closedByClient).toBe(false)
  })
})

describe('done 与终态（生命周期 4）+ token/thinking 合帧冲刷顺序', () => {
  it('done 到达先同步冲刷双通道缓冲，再交付 done（顺序保证，规则 10）', () => {
    const { es, onEvent } = setup()
    es.open()
    es.dispatch(frame(5, 'token', { delta: 'abc' }))
    es.dispatch(frame(6, 'token', { delta: 'def' }))
    es.dispatch(frame(7, 'thinking', { delta: '思考' }))
    es.dispatch(frame(8, 'done', { finish: 'completed', degraded: false, degraded_reason: null, duration_ms: 1, source_count: 0 }))
    const events = onEvent.mock.calls.map((c) => [c[0]!.event, c[0]!.id])
    expect(events).toEqual([
      ['token', 6], // 合帧后携带批内最大 seq
      ['thinking', 7],
      ['done', 8],
    ])
    const tokenEvent = onEvent.mock.calls.find((c) => c[0]!.event === 'token')![0]
    expect(tokenEvent!.data).toMatchObject({ delta: 'abcdef', seq: 6, turn_id: TURN_ID })
  })

  it('非保活（holdAfterDone 缺省）：done 后客户端主动 close 并回调 onClosed', () => {
    const { es, controller, onClosed } = setup()
    es.open()
    es.dispatch(frame(9, 'done', { finish: 'completed', degraded: false, degraded_reason: null, duration_ms: 1, source_count: 0 }))
    expect(es.closedByClient).toBe(true)
    expect(controller.state).toBe('closed')
    expect(onClosed).toHaveBeenCalledTimes(1)
    expect(onClosed.mock.calls[0]![0]).toEqual({ afterError: false, duringHold: false })
  })

  it('外部 closeStream（路由切走，生命周期 11）：close 且不回调 onClosed（store 主动方无需对账）', () => {
    const { controller, es, onClosed } = setup()
    es.open()
    controller.close('external')
    expect(es.closedByClient).toBe(true)
    expect(onClosed).not.toHaveBeenCalled()
  })
})

describe('done 后保活窗口（生命周期 13，holdAfterDone=true——仅 fast 轮由 store 传入）', () => {
  const doneFrame = frame(9, 'done', { finish: 'completed', degraded: false, degraded_reason: null, duration_ms: 1, source_count: 0 })

  it('done 后不主动 close（等服务端权威收口）', () => {
    const { es, controller } = setup({ holdAfterDone: true })
    es.open()
    es.dispatch(doneFrame)
    expect(es.closedByClient).toBe(false)
    expect(controller.state).toBe('held')
  })

  it('收口①：followups 到达 → 交付后 close', () => {
    const { es, controller, onEvent, onClosed } = setup({ holdAfterDone: true })
    es.open()
    es.dispatch(doneFrame)
    es.dispatch(frame(10, 'followups', { items: ['追问一', '追问二'] }))
    expect(onEvent.mock.calls.map((c) => c[0]!.event)).toEqual(['done', 'followups'])
    expect(es.closedByClient).toBe(true)
    expect(controller.state).toBe('closed')
    expect(onClosed).toHaveBeenCalledTimes(1)
  })

  it('收口②：保活期内服务端关闭（onerror）→ close、不重连、不进 reconnecting', () => {
    const { es, controller, onClosed } = setup({ holdAfterDone: true })
    es.open()
    es.dispatch(doneFrame)
    es.fail(ES_READY_STATE.CONNECTING)
    expect(es.closedByClient).toBe(true)
    expect(controller.state).toBe('closed')
    expect(onClosed).toHaveBeenCalledTimes(1)
    expect(onClosed.mock.calls[0]![0]).toEqual({ afterError: false, duringHold: true })
    vi.advanceTimersByTime(60_000)
    expect(MockEventSource.instances).toHaveLength(1) // 不重连
  })

  it('收口③：保活期心跳静默 → 静默 close、不重连', () => {
    const { es } = setup({ holdAfterDone: true })
    es.open()
    es.dispatch(frame(1, 'stage', { stage_key: 'rewrite', status: 'running' }))
    es.dispatch(doneFrame)
    vi.advanceTimersByTime(HEARTBEAT_WATCHDOG_MS)
    expect(es.closedByClient).toBe(true)
    expect(MockEventSource.instances).toHaveLength(1) // 不重连
  })
})

describe('error 事件（error 非终态，§7.2；error 后直接关闭 = failed 无内容轮）', () => {
  const errorFrame = frame(3, 'error', { code: '500201', message: '全部搜索源不可用', stage_key: 'search' })

  it('error 照常交付（不终态、不断流）；随后 done(degraded) 正常收口', () => {
    const { es, onEvent, onClosed } = setup()
    es.open()
    es.dispatch(errorFrame)
    expect(onEvent).toHaveBeenCalledTimes(1)
    es.dispatch(frame(4, 'done', { finish: 'degraded', degraded: true, degraded_reason: 'source_failure', duration_ms: 1, source_count: 0 }))
    expect(onEvent.mock.calls.map((c) => c[0]!.event)).toEqual(['error', 'done'])
    expect(onClosed).toHaveBeenCalledTimes(1)
  })

  it('error 后连接关闭（无 done）→ 不重连、onClosed(afterError=true) 交由 REST 对账', () => {
    const { es, onClosed } = setup()
    es.open()
    es.dispatch(errorFrame)
    es.fail(ES_READY_STATE.CONNECTING)
    expect(onClosed).toHaveBeenCalledWith({ afterError: true, duringHold: false })
    vi.advanceTimersByTime(60_000)
    expect(MockEventSource.instances).toHaveLength(1) // 不重连（重连只会收到同样的 error+close）
  })

  it('error 前的一般断线仍走正常重连路径（error 尚未到达）', () => {
    setup()
    const es1 = MockEventSource.instances[0]!
    es1.open()
    es1.dispatch(frame(1, 'stage', { stage_key: 'rewrite', status: 'running' }))
    es1.fail(ES_READY_STATE.CONNECTING)
    es1.fail(ES_READY_STATE.CONNECTING)
    vi.advanceTimersByTime(1000)
    expect(MockEventSource.instances).toHaveLength(2)
  })
})

describe('visibilitychange（生命周期 11：隐藏保持接收 + 回前台冲刷积压）', () => {
  it('隐藏期间照常入缓冲（连接不 close）；回前台立即冲刷积压（不等帧）', () => {
    const { es, onEvent } = setup()
    es.open()
    // 隐藏：不推进 timers，模拟浏览器后台对 setTimeout 的重度节流——事件滞留缓冲（不丢）
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true })
    es.dispatch(frame(1, 'token', { delta: '积压内容' }))
    expect(onEvent).not.toHaveBeenCalled()
    expect(es.closedByClient).toBe(false) // 隐藏 ≠ 切走：连接保持

    // 回前台：立即冲刷积压（生命周期 11：回前台恢复正常帧渲染并冲刷积压）
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
    expect(onEvent).toHaveBeenCalledTimes(1)
    expect(onEvent.mock.calls[0]![0].event).toBe('token')
  })

  it('隐藏态下入缓冲走 setTimeout 兜底消费（streaming-buffer 规则 8）', () => {
    const { es, onEvent } = setup()
    es.open()
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true })
    es.dispatch(frame(1, 'token', { delta: 'x' }))
    vi.advanceTimersByTime(16)
    expect(onEvent).toHaveBeenCalledTimes(1)
  })
})

describe('连接注册表（生命周期 11 防双连接：同一 turnId 至多一条活跃连接）', () => {
  it('重复订阅同轮：先 close 旧连接再新建', () => {
    const first = subscribeTurn({
      turnId: TURN_ID,
      callbacks: { onEvent: vi.fn(), onStateChange: vi.fn(), onClosed: vi.fn() },
      eventSourceFactory: mockEventSourceFactory(),
    })
    const es1 = MockEventSource.instances[0]!
    const second = subscribeTurn({
      turnId: TURN_ID,
      callbacks: { onEvent: vi.fn(), onStateChange: vi.fn(), onClosed: vi.fn() },
      eventSourceFactory: mockEventSourceFactory(),
    })
    expect(es1.closedByClient).toBe(true)
    expect(first.closed).toBe(true)
    expect(second.closed).toBe(false)
    expect(peekTurnStream(TURN_ID)).toBe(second)
  })

  it('closeTurnStream：关闭并移除注册；未注册 turnId 无副作用', () => {
    subscribeTurn({
      turnId: TURN_ID,
      callbacks: { onEvent: vi.fn(), onStateChange: vi.fn(), onClosed: vi.fn() },
      eventSourceFactory: mockEventSourceFactory(),
    })
    closeTurnStream(TURN_ID)
    expect(peekTurnStream(TURN_ID)).toBeUndefined()
    expect(() => closeTurnStream('00000000-0000-4000-8000-000000000000')).not.toThrow()
  })
})
