/**
 * test/helpers/mockEventSource.ts — 原生 EventSource 的测试替身。
 *
 * turnStream.ts 通过 eventSourceFactory 注入本替身（§8.9③：无类型边界以本替身形状收窄，
 * turnStream 侧声明最小结构类型 EventSourceLike，不直接依赖全局 EventSource 类型做行为断言）。
 *
 * 行为对齐 WHATWG EventSource：
 *   - readyState：0=CONNECTING / 1=OPEN / 2=CLOSED
 *   - open()：模拟连接建立（含原生断线重连成功后的再次 open）
 *   - dispatch()：模拟服务端事件帧（id/event/data 三行式解析后的派发形态）；
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

  constructor(url: string | URL) {
    this.url = String(url)
    MockEventSource.instances.push(this)
  }

  open(): void {
    this.readyState = ES_READY_STATE.OPEN
    this.onopen?.()
  }

  dispatch(frame: DispatchFrame): void {
    this.onmessage?.(
      new MessageEvent(frame.type, { data: frame.data, lastEventId: String(frame.id) }),
    )
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
