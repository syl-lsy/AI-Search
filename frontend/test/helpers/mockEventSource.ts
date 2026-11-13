/**
 * test/helpers/mockEventSource.ts — 原生 EventSource 的测试替身。
 *
 * turnStream.ts 通过 eventSourceFactory 注入本替身（§8.9③：无类型边界以本替身形状收窄，
 * turnStream 侧声明最小结构类型 EventSourceLike，不直接依赖全局 EventSource 类型做行为断言）。
 *
 * 行为对齐 WHATWG EventSource：
 *   - readyState：0=CONNECTING / 1=OPEN / 2=CLOSED
 *   - open()：模拟连接建立（含原生断线重连成功后的再次 open）
 *   - dispatch()：模拟服务端事件帧（id/event/data 三行式解析后的派发形态）——**具名事件派发
 *     语义（P0 修复锁死）**：event: <name> 帧只派发给 addEventListener(name) 注册的 listener，
 *     绝不进 onmessage；仅 type='message'（或无 event 行）帧进 onmessage / 'message' listener。
 *     【批次 8 P0 教训】旧实现无视 type 直调 onmessage，与浏览器派发语义脱节 → turnStream
 *     仅挂 onmessage 的缺陷在单测假绿、真机失明（9 条 e2e 确定性红，详见 answer.spec.ts 头注）。
 *     心跳 `: ping` 为注释行，浏览器不派发——替身同样不提供心跳派发（不可见性即真实行为）。
 *   - fail(readyState)：模拟连接错误；readyState=0（CONNECTING）代表浏览器将原生自动重连，
 *     readyState=2（CLOSED）代表致命失败（非 200/非 event-stream，浏览器不再重连）。
 */

export const ES_READY_STATE = { CONNECTING: 0, OPEN: 1, CLOSED: 2 } as const

export interface DispatchFrame {
  id: number
  type: string
  data: string
}

type MessageListener = (ev: MessageEvent) => void

export class MockEventSource {
  static instances: MockEventSource[] = []
  static reset(): void {
    MockEventSource.instances = []
  }

  url: string
  readyState: number = ES_READY_STATE.CONNECTING
  /** 客户端主动 close 置 true（原生 close 后浏览器不再重连） */
  closedByClient = false

  onopen: (() => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null

  /** addEventListener 注册表（type → listeners；WHATWG 具名事件派发的唯一通道） */
  private readonly listeners = new Map<string, Set<MessageListener>>()

  constructor(url: string | URL) {
    this.url = String(url)
    MockEventSource.instances.push(this)
  }

  open(): void {
    this.readyState = ES_READY_STATE.OPEN
    this.onopen?.()
  }

  addEventListener(type: string, listener: MessageListener): void {
    const set = this.listeners.get(type) ?? new Set<MessageListener>()
    set.add(listener)
    this.listeners.set(type, set)
  }

  removeEventListener(type: string, listener: MessageListener): void {
    this.listeners.get(type)?.delete(listener)
  }

  /** 测试断言面：某 type 是否已有注册 listener（传输层具名注册锁死用） */
  hasListener(type: string): boolean {
    return (this.listeners.get(type)?.size ?? 0) > 0
  }

  /**
   * 按 WHATWG 语义派发一帧：type='message' → onmessage + 'message' listener；
   * 具名 type → 仅该 name 的 addEventListener listener（onmessage 收不到）。
   */
  dispatch(frame: DispatchFrame): void {
    const ev = new MessageEvent(frame.type, { data: frame.data, lastEventId: String(frame.id) })
    if (frame.type === 'message') {
      this.onmessage?.(ev)
      for (const fn of [...(this.listeners.get('message') ?? [])]) fn(ev)
      return
    }
    for (const fn of [...(this.listeners.get(frame.type) ?? [])]) fn(ev)
  }

  /** 模拟连接错误。readyStateAfter=0（CONNECTING）→ 原生将自动重连；=2（CLOSED）→ 致命失败 */
  fail(readyStateAfter: number = ES_READY_STATE.CONNECTING): void {
    this.readyState = readyStateAfter
    this.onerror?.(new Event('error'))
  }

  close(): void {
    this.closedByClient = true
    this.readyState = ES_READY_STATE.CLOSED
  }
}

/** 创建替身工厂（注入 turnStream 的 eventSourceFactory） */
export function mockEventSourceFactory(): (url: string) => MockEventSource {
  return (url: string) => new MockEventSource(url)
}
