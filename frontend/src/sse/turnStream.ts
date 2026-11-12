/**
 * src/sse/turnStream.ts — SSE 传输层唯一出口（frontend-api.md §3 / §8.4 / §8.7）。
 *
 * 职责边界（§8.7 严格解耦）：连接/重连/Last-Event-ID/心跳/合帧节奏/断线续传全部收敛在本文件；
 * 事件经 `turnStore.applyEvent()` 单入口进 store——本模块不 import 任何 store（依赖单向）。
 *
 * 连接生命周期对照（frontend-api.md §3，逐条落位）：
 *   L1  两段式：POST 建轮（store 经 client.ts）→ 本模块 GET 订阅 /api/v1/turns/{turnId}/events。
 *   L2  重连：原生自动重连先行（同一对象不 close，浏览器原生携带 Last-Event-ID，勿自造 header）；
 *       连续失败/致命失败才前端接管，指数退避 1s/2s/4s 封顶 15s（api.md §7.3 第 1 条
 *       「浏览器默认 ~3s，前端叠加指数退避」）。
 *   L3  心跳看门狗：服务端每 15s 发 `: ping` 注释行（api.md §7.1）——原生 EventSource 不派发
 *       注释行（浏览器行为），JS 无法逐行观测；故以「首个业务事件武装后 3×心跳静默」作可实现
 *       近似：武装前静默不判超时（L9 排队轮等待期仅心跳、勿判超时重连）；武装后静默 → 主动
 *       close 再重连。真实连线永不静默超 15s（心跳保活），40~60s 的业务事件空档（deep 读网页
 *       阶段，§7.4 序列实测间距）不误触。
 *   L4  done：先同步冲刷 token/thinking 双通道缓冲（streaming-buffer 规则 10），再交付 done；
 *       非保活轮随后主动 close（对账由 store 在 done 到达时触发，REST 为准源）。
 *   L5  断线续传：重放事件经 seq 门去重（streaming-buffer 规则 24 幂等）——fromSeq 基线由
 *       store 传入（本页已流式渲染过 → lastSeq；刷新/新页 → 0 全量重放重建；早于 compactSeq
 *       → v1.4 放弃增量）。sessionStorage `ai-search.seq.{turnId}` 按 L5 形态持久化。
 *   L6  合成 done（缓冲过期）：与 done 同路径 → store 对账。
 *   L9  排队轮：订阅照常；等待期无业务事件不判超时（看门狗武装前不生效）。
 *   L11 连接注册表：同一 turnId 至多一条活跃连接（subscribeTurn 先关旧再建新）；
 *       visibilitychange 隐藏保持接收（隐藏 ≠ 切走）、回前台冲刷积压；外部 close 由 store
 *       closeStream() 驱动（路由切走）。
 *   L13 done 后保活窗口（fast 轮，holdAfterDone 由 store 按轮 mode 传入）：done 后不主动
 *       close——收口① followups 到达 → 交付后 close；② 服务端关闭/出错 → close 且不重连、
 *       不进 reconnecting；③ 心跳静默 → 静默 close。前端零常量零计时（窗口权威在服务端）。
 *   —   error 非终态（§7.2）：照常交付；error 后连接关闭（failed 无内容轮，§7.1 v1.10 例外
 *       ——重连只会收到同样的 error+close）→ 不重连，onClosed(afterError) 交由 store REST 对账。
 */
import { API_BASE_URL } from '@/api/client'
import { isRecord } from '@/utils/guards'
import type { SseEventName, SseWireEvent } from '@/types/api'

/* =============================================================
 * 常量（值域出处：§3 契约 + streaming-buffer 技能参数表）
 * =========================================================== */

/** api.md §7.1：服务端心跳每 15s 一行注释 `: ping`（不占 seq） */
export const SSE_HEARTBEAT_SECONDS = 15

/**
 * 心跳看门狗阈值：3×心跳（L3 的可实现近似，见文件头注释）。
 * 取值依据：原生 EventSource 观测不到 `: ping` 注释行，literal 15s「无任何行」不可实现；
 * 阈值须大于业务事件最大合法空档（§7.4 深度序列实测最大 41s）且小于心跳保活的异常窗口。
 */
export const HEARTBEAT_WATCHDOG_MS = 3 * SSE_HEARTBEAT_SECONDS * 1000

/** 通道缓冲上限（streaming-buffer 参数表 64KB）：超限立即全量 flush 降级为整段渲染（规则 13） */
export const CHANNEL_OVERFLOW_CHARS = 64 * 1024

/** 每帧最低消费 floor（streaming-buffer 规则 9：队列空时打字感下限） */
const MIN_CHARS_PER_FRAME = 2

/** 追赶系数 k（streaming-buffer 规则 9：积压 ~1s 内追平的折中） */
const CATCH_UP_FACTOR = 0.12

/** 无 rAF / 页面隐藏态下的帧兜底间隔（streaming-buffer 规则 8） */
const FRAME_FALLBACK_MS = 16

/** §3 生命周期 2：指数退避 1s/2s/4s… 封顶 15s */
const RECONNECT_BACKOFF_BASE_MS = 1000
const RECONNECT_BACKOFF_CAP_MS = 15000

/** WHATWG EventSource readyState 常量（本地声明，供 EventSourceLike 结构类型使用） */
const ES_CONNECTING = 0
const ES_CLOSED = 2

/* =============================================================
 * 类型
 * =========================================================== */

/** 传输层连接状态（store 映射为 §4 的 connected/reconnecting/idle 三态） */
export type TurnStreamState = 'connecting' | 'connected' | 'reconnecting' | 'held' | 'closed'

export interface TurnStreamClosedInfo {
  /** error 事件后连接关闭（failed 无内容轮）——store 应走 REST 对账 */
  afterError: boolean
  /** 保活窗口内的收口（②服务端关闭/③静默）——轮已终态，无 banner 语义 */
  duringHold: boolean
}

export interface TurnStreamCallbacks {
  /** 唯一事件出口 → turnStore.applyEvent()（§8.7 单入口） */
  onEvent: (event: SseWireEvent) => void
  onStateChange: (state: TurnStreamState) => void
  /** 控制器终止（done/error 收口/保活收口）；外部 close 不回调 */
  onClosed: (info: TurnStreamClosedInfo) => void
}

export interface SubscribeTurnOptions {
  turnId: string
  /** seq 门基线（重放去重）；0 = 全量重放重建（store 按 L5/L11 决定） */
  fromSeq?: number
  /** L13：fast 轮 done 后不主动 close（store 按轮 mode 传入） */
  holdAfterDone?: boolean
  callbacks: TurnStreamCallbacks
  /** 测试注入替身；生产缺省 = 原生 EventSource */
  eventSourceFactory?: (url: string) => EventSourceLike
}

export interface TurnStreamController {
  readonly turnId: string
  readonly state: TurnStreamState
  readonly closed: boolean
  /** 外部主动断流（store.closeStream / 注册表替换）；reason=external 不回调 onClosed */
  close(reason?: 'external'): void
}

/**
 * 原生 EventSource 的最小结构类型（§8.9③：测试替身/原生实现同构面）。
 * 处理器字段只写不读（内部仅赋值箭头函数），签名锚定原生 this 形态以保双向结构兼容。
 */
interface EventSourceLike {
  readyState: number
  onopen: ((this: EventSource, ev: Event) => unknown) | null
  onmessage: ((this: EventSource, ev: MessageEvent) => unknown) | null
  onerror: ((this: EventSource, ev: Event) => unknown) | null
  close(): void
}

function defaultEventSourceFactory(url: string): EventSourceLike {
  return new EventSource(url)
}

/* =============================================================
 * 纯逻辑：退避 / 合帧缓冲 / 线格解析
 * =========================================================== */

/** §3 生命周期 2：1s/2s/4s/8s… 封顶 15s */
export function computeBackoffDelayMs(attempt: number): number {
  return Math.min(RECONNECT_BACKOFF_BASE_MS * 2 ** Math.max(0, attempt), RECONNECT_BACKOFF_CAP_MS)
}

/**
 * 分通道缓冲（streaming-buffer 规则 9/10/12/13）：token 与 thinking 各自独立实例，
 * push 只入队不渲染；帧回调按 floor + 积压×k 消费（积压大加速追平）；flushAll 供
 * 终态/abort/切走时同步冲刷（已收内容不丢）。
 */
export class ChannelBuffer {
  private queue: string[] = []
  private pending = 0
  private maxSeqSeen = 0
  private scheduled = false

  constructor(
    private readonly onFlush: (mergedDelta: string, maxSeq: number) => void,
    private readonly schedule: (fn: () => void) => void,
  ) {}

  push(delta: string, seq: number): void {
    if (!delta) return
    this.queue.push(delta)
    this.pending += delta.length
    this.maxSeqSeen = Math.max(this.maxSeqSeen, seq)
    if (this.pending > CHANNEL_OVERFLOW_CHARS) {
      // 规则 13：有界缓冲超限 → 全量 flush 降级整段渲染（隐藏页/断网下唯一无界增长路径）
      this.flushAll()
      return
    }
    this.scheduleDrain()
  }

  /** 终态/abort 同步冲刷（规则 10）；队列已空为 no-op */
  flushAll(): void {
    if (this.pending === 0) return
    const merged = this.queue.join('')
    this.queue = []
    this.pending = 0
    this.onFlush(merged, this.maxSeqSeen)
  }

  get pendingChars(): number {
    return this.pending
  }

  private scheduleDrain(): void {
    if (this.scheduled) return
    this.scheduled = true
    this.schedule(() => {
      this.scheduled = false
      this.drain()
    })
  }

  private drain(): void {
    if (this.pending === 0) return
    const budget = Math.max(MIN_CHARS_PER_FRAME, Math.ceil(this.pending * CATCH_UP_FACTOR))
    let out = ''
    while (this.queue.length > 0 && out.length < budget) {
      const head = this.queue[0]!
      const take = Math.min(head.length, budget - out.length)
      out += head.slice(0, take)
      if (take === head.length) this.queue.shift()
      else this.queue[0] = head.slice(take)
    }
    this.pending -= out.length
    this.onFlush(out, this.maxSeqSeen)
    // 积压未尽 → 续排下一帧（追赶直至清空，规则 9）
    if (this.pending > 0) this.scheduleDrain()
  }
}

/**
 * 线格帧 → SseWireEvent 解析。心跳 `: ping` 为注释行：浏览器 EventSource 不派发注释行，
 * 天然不产事件、不占 seq（§7.1）；未知事件名（前向兼容）与畸形 data 静默忽略。
 * id 行 = seq（api.md §7.1）；id 缺失/非法时回退 data.seq。
 * 末尾单一 as：线格形态已过 seq/事件名守卫（§8.9③）；运行时形态防线 = §9 fixture + 评审 diff。
 */
const SSE_EVENT_NAMES: readonly SseEventName[] = [
  'stage',
  'token',
  'thinking',
  'sources',
  'images',
  'context_compact',
  'memory',
  'done',
  'followups',
  'error',
]

export function parseWireEvent(msg: MessageEvent): SseWireEvent | null {
  let data: unknown
  try {
    data = JSON.parse(typeof msg.data === 'string' ? msg.data : '')
  } catch {
    return null
  }
  if (!isRecord(data) || typeof data.seq !== 'number') return null
  const eventName = msg.type as SseEventName
  if (!SSE_EVENT_NAMES.some((name) => name === eventName)) return null
  const idNum = Number(msg.lastEventId)
  const id = Number.isFinite(idNum) && idNum > 0 ? idNum : data.seq
  const payload: unknown = data
  return { id, event: eventName, data: payload } as SseWireEvent
}

/** rAF 可用且页面可见 → rAF；否则 setTimeout 兜底（streaming-buffer 规则 8） */
function scheduleFrame(fn: () => void): void {
  const hidden = typeof document !== 'undefined' && document.visibilityState === 'hidden'
  const raf = typeof requestAnimationFrame === 'function' ? requestAnimationFrame : null
  if (!hidden && raf) {
    raf(() => fn())
  } else {
    setTimeout(fn, FRAME_FALLBACK_MS)
  }
}

/* =============================================================
 * 控制器与注册表
 * =========================================================== */

export function buildTurnStreamUrl(turnId: string): string {
  return `${API_BASE_URL}/api/v1/turns/${turnId}/events`
}

const registry = new Map<string, TurnStream>()

class TurnStream implements TurnStreamController {
  readonly turnId: string
  state: TurnStreamState = 'connecting'
  closed = false

  private readonly opts: SubscribeTurnOptions
  private es: EventSourceLike | null = null
  private seqGate: number
  private errorSeen = false
  private doneSeen = false
  private holding = false
  private closeEmitted = false
  private nativeFailures = 0
  private backoffAttempt = 0
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private watchdogTimer: ReturnType<typeof setTimeout> | null = null
  private watchdogArmed = false
  private readonly tokenChannel: ChannelBuffer
  private readonly thinkingChannel: ChannelBuffer
  private readonly visibilityListener: () => void

  constructor(opts: SubscribeTurnOptions) {
    this.opts = opts
    this.turnId = opts.turnId
    this.seqGate = Math.max(0, opts.fromSeq ?? 0)
    this.tokenChannel = new ChannelBuffer((merged, seq) => this.emitDelta('token', merged, seq), scheduleFrame)
    this.thinkingChannel = new ChannelBuffer((merged, seq) => this.emitDelta('thinking', merged, seq), scheduleFrame)
    this.visibilityListener = () => {
      // L11：回前台（visible）恢复正常帧渲染并冲刷积压（streaming-buffer 规则 8 的 setTimeout
      // 兜底在后台标签页可能被浏览器重度节流，冲刷保证数据不滞留）
      if (!this.closed && typeof document !== 'undefined' && document.visibilityState === 'visible') {
        this.tokenChannel.flushAll()
        this.thinkingChannel.flushAll()
      }
    }
  }

  start(): void {
    this.openEventSource()
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', this.visibilityListener)
    }
  }

  /** L11：路由切走/注册表替换的外部断流——先冲刷缓冲（数据不丢）再 close，不回调 onClosed */
  close(): void {
    if (this.closed) return
    this.tokenChannel.flushAll()
    this.thinkingChannel.flushAll()
    this.teardown()
    this.es?.close()
    this.es = null
    this.closed = true
    this.setState('closed')
    this.removeFromRegistry()
  }

  /* ---------- 连接 ---------- */

  private openEventSource(): void {
    const factory = this.opts.eventSourceFactory ?? defaultEventSourceFactory
    const es = factory(buildTurnStreamUrl(this.turnId))
    this.es = es
    es.onopen = () => {
      if (this.closed) return
      this.nativeFailures = 0
      this.setState('connected')
    }
    es.onmessage = (msg) => {
      if (this.closed) return
      this.nativeFailures = 0
      this.handleMessage(msg)
    }
    es.onerror = () => {
      if (this.closed) return
      this.handleError()
    }
  }

  /* ---------- 帧处理 ---------- */

  private handleMessage(msg: MessageEvent): void {
    const wireEvent = parseWireEvent(msg)
    if (!wireEvent) return
    // 看门狗：首个业务事件武装（L9 排队等待期不判超时），其后每事件重置
    if (!this.watchdogArmed) this.armWatchdog()
    else this.resetWatchdog()
    // seq 门：重放去重/幂等（streaming-buffer 规则 24）
    if (wireEvent.id <= this.seqGate) return
    this.seqGate = wireEvent.id
    this.backoffAttempt = 0 // 健康流量到达 = 重连成功：退避基线归零
    if (wireEvent.event === 'token') {
      this.tokenChannel.push(wireEvent.data.delta, wireEvent.id)
    } else if (wireEvent.event === 'thinking') {
      this.thinkingChannel.push(wireEvent.data.delta, wireEvent.id)
    } else if (wireEvent.event === 'done') {
      // L4：终态先同步冲刷双通道（已收内容不丢，规则 10），再交付 done
      this.tokenChannel.flushAll()
      this.thinkingChannel.flushAll()
      this.opts.callbacks.onEvent(wireEvent)
      this.handleDone()
    } else {
      this.opts.callbacks.onEvent(wireEvent)
      if (wireEvent.event === 'error') {
        this.errorSeen = true // error 非终态（§7.2）：照常交付；后续收口策略见 handleError
      } else if (wireEvent.event === 'followups' && this.holding) {
        this.closeInternal() // L13 收口①：followups 交付后 close
      }
    }
  }

  private handleDone(): void {
    this.doneSeen = true
    // 注意：保活轮不在此解除看门狗——L13 收口③（保活期心跳静默 → 静默 close）依赖它
    if (this.opts.holdAfterDone === true) {
      // L13：done 后不主动 close，等服务端权威收口（前端零常量零计时）
      this.holding = true
      this.setState('held')
    } else {
      this.closeInternal()
    }
  }

  /** 合帧 flush → applyEvent 单入口（delta 通道承载批内最大 seq） */
  private emitDelta(event: 'token' | 'thinking', delta: string, seq: number): void {
    if (event === 'token') {
      this.opts.callbacks.onEvent({ id: seq, event: 'token', data: { seq, turn_id: this.turnId, ts: '', delta } })
    } else {
      this.opts.callbacks.onEvent({ id: seq, event: 'thinking', data: { seq, turn_id: this.turnId, ts: '', delta } })
    }
  }

  /* ---------- 错误与重连 ---------- */

  private handleError(): void {
    if (this.closed) return
    this.disarmWatchdog()
    if (this.holding) {
      // L13 收口②：保活窗口内服务端关闭 → close 且不重连、不进 reconnecting
      this.closeInternal()
      return
    }
    if (this.doneSeen) {
      this.closeInternal()
      return
    }
    if (this.errorSeen) {
      // §7.1 v1.10 例外：error 后直接关闭（failed 无内容轮，含合成 done 豁免路径）——
      // 重连只会收到同样的 error+close → 不重连，交由 store REST 对账终态
      this.closeInternal()
      return
    }
    this.nativeFailures += 1
    const fatal = this.es !== null && this.es.readyState === ES_CLOSED
    // L2：首次瞬时断线 → 不 close，保留浏览器原生自动重连（同一对象原生携带 Last-Event-ID，
    // 勿自造 header）；连续失败或致命失败（CLOSED——非 200/非 event-stream，浏览器不再自动重连）
    // 才由前端接管，指数退避重建（§7.3 第 1 条「前端叠加指数退避」）
    if (this.nativeFailures === 1 && !fatal) {
      this.setState('reconnecting')
      return
    }
    this.es?.close()
    this.es = null
    this.scheduleReconnect()
  }

  private scheduleReconnect(): void {
    this.setState('reconnecting')
    const delay = computeBackoffDelayMs(this.backoffAttempt)
    this.backoffAttempt += 1
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      if (!this.closed) this.openEventSource()
    }, delay)
  }

  /* ---------- 心跳看门狗（L3 可实现近似） ---------- */

  private armWatchdog(): void {
    this.watchdogArmed = true
    this.resetWatchdog()
  }

  private resetWatchdog(): void {
    if (this.watchdogTimer !== null) clearTimeout(this.watchdogTimer)
    this.watchdogTimer = setTimeout(() => {
      this.watchdogTimer = null
      this.onWatchdogFire()
    }, HEARTBEAT_WATCHDOG_MS)
  }

  private disarmWatchdog(): void {
    this.watchdogArmed = false
    if (this.watchdogTimer !== null) {
      clearTimeout(this.watchdogTimer)
      this.watchdogTimer = null
    }
  }

  private onWatchdogFire(): void {
    if (this.closed) return
    if (this.holding) {
      // L13 收口③：保活期心跳静默（服务端异常悬挂）→ 静默 close、不重连
      this.closeInternal()
      return
    }
    // L3：武装后静默超阈值 → 主动 close 再重连（重建对象全量重放，seq 门幂等去重）
    this.disarmWatchdog()
    this.es?.close()
    this.es = null
    this.setState('reconnecting')
    this.openEventSource()
  }

  /* ---------- 终止 ---------- */

  private closeInternal(): void {
    this.teardown()
    this.es?.close()
    this.es = null
    this.closed = true
    this.setState('closed')
    if (!this.closeEmitted) {
      this.closeEmitted = true
      this.opts.callbacks.onClosed({
        afterError: this.errorSeen && !this.doneSeen,
        duringHold: this.holding,
      })
    }
    this.removeFromRegistry()
  }

  private teardown(): void {
    this.disarmWatchdog()
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    if (typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', this.visibilityListener)
    }
  }

  private setState(state: TurnStreamState): void {
    this.state = state
    this.opts.callbacks.onStateChange(state)
  }

  private removeFromRegistry(): void {
    if (registry.get(this.turnId) === this) registry.delete(this.turnId)
  }
}

/* =============================================================
 * 注册表 API（store 消费面；SSE 只经本文件触网，§8.4）
 * =========================================================== */

/**
 * 订阅一轮事件流（L1 的 GET 段）。同一 turnId 至多一条活跃连接（L11 防双连接）：
 * 注册表中已有旧连接时先 close 再新建——覆盖路由快速往返、同轮组件重复挂载等场景。
 */
export function subscribeTurn(opts: SubscribeTurnOptions): TurnStreamController {
  const prev = registry.get(opts.turnId)
  if (prev) prev.close()
  const stream = new TurnStream(opts)
  registry.set(opts.turnId, stream)
  stream.start()
  return stream
}

/** 主动断流（L11 路由切走 → store.closeStream）；未注册 turnId 无副作用 */
export function closeTurnStream(turnId: string): void {
  const stream = registry.get(turnId)
  if (stream) {
    stream.close()
    registry.delete(turnId)
  }
}

/** 查询注册表中的活跃连接（测试/诊断用） */
export function peekTurnStream(turnId: string): TurnStreamController | undefined {
  return registry.get(turnId)
}

/** 便于断言的 readyState 引用（重连策略测试使用；CONNECTING=0/OPEN=1/CLOSED=2） */
export const ES_READY_STATE_CONNECTING = ES_CONNECTING
export const ES_READY_STATE_CLOSED = ES_CLOSED
