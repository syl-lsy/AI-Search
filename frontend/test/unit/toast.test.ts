/**
 * test/unit/toast.test.ts — toast 单例语义锁（前批评审补强：composables/toast.ts 行为契约
 * 以 fake timers 钉死——模块级单例队列 + 4s 自动消失 + 同文案刷新计时不堆叠）。
 *
 * 该模块为有状态单例且无重置出口：每条用例 vi.resetModules + 动态 import 取全新模块状态，
 * 用例间互不污染（seq/id 计数器、items 队列、timers 全部随模块重载归零）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/** 每条用例独立加载模块（vi.resetModules 后动态 import = 全新单例状态） */
function loadToastModule(): Promise<typeof import('@/composables/toast')> {
  return import('@/composables/toast')
}

describe('toast 单例语义（composables/toast.ts）', () => {
  beforeEach(() => {
    vi.resetModules()
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('同文案两次推送：队列长度仍为 1 且 id 不变（不堆叠，仅复用既有条目）', async () => {
    const { toast, useToasts } = await loadToastModule()
    toast('网络异常，请稍后重试')
    const first = useToasts().value
    expect(first).toHaveLength(1)
    const firstId = first[0]!.id
    toast('网络异常，请稍后重试')
    const second = useToasts().value
    expect(second).toHaveLength(1)
    expect(second[0]!.id).toBe(firstId)
  })

  it('不同文案推送：队列两条（各持独立 id）', async () => {
    const { toast, useToasts } = await loadToastModule()
    toast('第一条')
    toast('第二条')
    const items = useToasts().value
    expect(items.map((x) => x.message)).toEqual(['第一条', '第二条'])
    expect(items[0]!.id).not.toBe(items[1]!.id)
  })

  it('4s 自动消失：3999ms 仍在、4000ms 消失', async () => {
    const { toast, useToasts } = await loadToastModule()
    toast('限时提示')
    vi.advanceTimersByTime(3_999)
    expect(useToasts().value).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(useToasts().value).toHaveLength(0)
  })

  it('同文案再推刷新计时：3s 时再推 → 满 4s（自再推时刻起算）才消失', async () => {
    const { toast, useToasts } = await loadToastModule()
    toast('刷新计时')
    vi.advanceTimersByTime(3_000)
    expect(useToasts().value).toHaveLength(1)
    toast('刷新计时') // 计时刷新：从 3s 时刻重新起算 4s
    vi.advanceTimersByTime(3_999)
    expect(useToasts().value).toHaveLength(1) // 6999ms 仍在（未按首推 4s 在 4000ms 消失）
    vi.advanceTimersByTime(1)
    expect(useToasts().value).toHaveLength(0) // 7000ms 消失
  })
})
