/**
 * src/composables/toast.ts — 轻量 toast（frontend-api.md §2 信封约定：
 * 「code !== "0" 抛业务错误，message 直接 toast（中文文案服务端已备好）」）。
 * 模块级单例队列 + 4s 自动消失；ToastHost 全站挂载一次。
 */
import { readonly, shallowRef } from 'vue'

export interface ToastItem {
  id: number
  message: string
}

const DISPLAY_MS = 4_000

const items = shallowRef<ToastItem[]>([])
let seq = 0
const timers = new Map<number, ReturnType<typeof setTimeout>>()

function dismiss(id: number): void {
  const t = timers.get(id)
  if (t) {
    clearTimeout(t)
    timers.delete(id)
  }
  items.value = items.value.filter((x) => x.id !== id)
}

/** 推送一条 toast（重复消息不堆叠——同文本仅刷新计时） */
export function toast(message: string): void {
  const existing = items.value.find((x) => x.message === message)
  if (existing) {
    const t = timers.get(existing.id)
    if (t) clearTimeout(t)
    timers.set(
      existing.id,
      setTimeout(() => dismiss(existing.id), DISPLAY_MS),
    )
    return
  }
  seq += 1
  const id = seq
  items.value = [...items.value, { id, message }]
  timers.set(
    id,
    setTimeout(() => dismiss(id), DISPLAY_MS),
  )
}

/** ToastHost 消费（只读） */
export function useToasts() {
  return readonly(items)
}
