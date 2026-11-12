/**
 * test/unit/scroll-controller.test.ts — AnswerScrollController 状态机（TDD 先行，REQ-043 / A26）。
 *
 * 阈值行为全部按 PRD REQ-043 原文锁死：
 *   - 贴底（距底 < 40px）+ 流式 → 新内容到达自动跟随保持贴底（40 为实现常量，不进 Nacos）；
 *   - 上滚位移 > 8px（严格大于）→ 立即接管；接管不中断流式渲染（渲染管线在本测试域外，控制器只停止自动滚动）；
 *   - 恢复三途径：① 接管后用户主动下滚进入贴底区（<40px）；② 点击 ScrollToBottomFab；③ 发送新消息/新一轮；
 *   - 消歧：接管后视口停留（距底仍 <40px 的重叠带）或距底变化仅由流式内容增长导致 → 不恢复（不被拽回）；
 *   - 历史回看态（非流式）零自动滚动；
 *   - scroll 监听 passive 化（REQ-043 工程约束）。
 *
 * 视口以「可注入的结构替身」驱动（scrollTop/scrollHeight/clientHeight 为纯字段）——
 * 状态机判定只依赖这三个度量 + scroll 事件，不依赖真实布局引擎；与真实 DOM 的联动由
 * integration/scroll-follow.test.ts（真实元素 + 度量 stub + 真实 scroll 事件）承担。
 */
import { describe, expect, it, vi } from 'vitest'

import {
  AnswerScrollController,
  FOLLOW_NEAR_BOTTOM_PX,
  TAKEOVER_UP_SCROLL_PX,
} from '@/composables/answerScroll'
import type { ScrollViewportLike } from '@/composables/answerScroll'

/** 可控视口替身：字段直改模拟用户滚动（dispatch 后触发控制器 scroll 处理器）与内容增长。
 *  scrollTop 仿真真实 DOM 的 clamp 语义（[0, scrollHeight - clientHeight]）。 */
function makeViewport(init?: {
  scrollTop?: number
  scrollHeight?: number
  clientHeight?: number
}): ScrollViewportLike & {
  dispatchScroll(): void
  scrollToCalls: { top: number; behavior: string }[]
} {
  const state = { top: init?.scrollTop ?? 0, h: init?.scrollHeight ?? 1000, ch: init?.clientHeight ?? 400 }
  const listeners: Array<() => void> = []
  const scrollToCalls: { top: number; behavior: string }[] = []
  const viewport: ScrollViewportLike & {
    dispatchScroll(): void
    scrollToCalls: { top: number; behavior: string }[]
  } = {
    get scrollTop() {
      return Math.max(0, Math.min(state.top, state.h - state.ch))
    },
    set scrollTop(v: number) {
      state.top = v
    },
    get scrollHeight() {
      return state.h
    },
    set scrollHeight(v: number) {
      state.h = v
    },
    get clientHeight() {
      return state.ch
    },
    set clientHeight(v: number) {
      state.ch = v
    },
    addEventListener: vi.fn(
      (type: string, listener: () => void, options?: AddEventListenerOptions) => {
        expect(type).toBe('scroll')
        listeners.push(listener)
        // REQ-043 工程约束：scroll 监听 passive 化
        expect(options?.passive).toBe(true)
      },
    ),
    removeEventListener: vi.fn((type: string, listener: () => void) => {
      expect(type).toBe('scroll')
      const i = listeners.indexOf(listener)
      if (i >= 0) listeners.splice(i, 1)
    }),
    scrollTo: vi.fn((options?: { top?: number; behavior?: 'auto' | 'smooth' }) => {
      scrollToCalls.push({ top: options?.top ?? 0, behavior: options?.behavior ?? 'auto' })
    }),
    scrollToCalls,
    dispatchScroll() {
      for (const fn of [...listeners]) fn()
    },
  }
  return viewport
}

/** 便捷断言：距底值 */
function distanceFromBottom(v: { scrollTop: number; scrollHeight: number; clientHeight: number }): number {
  return v.scrollHeight - v.scrollTop - v.clientHeight
}

describe('AnswerScrollController（REQ-043 状态机）', () => {
  it('常量锁死：贴底阈值 40px、接管阈值 8px（实现常量，不进 Nacos）', () => {
    expect(FOLLOW_NEAR_BOTTOM_PX).toBe(40)
    expect(TAKEOVER_UP_SCROLL_PX).toBe(8)
  })

  it('流式 + 跟随态：内容增长 → 自动贴底（即时赋值，clamp 后距底 0，无平滑）', () => {
    const v = makeViewport({ scrollTop: 600, scrollHeight: 1000, clientHeight: 400 })
    const c = new AnswerScrollController(v)
    c.setStreaming(true)
    // 距底 0（贴底）
    expect(distanceFromBottom(v)).toBe(0)
    v.scrollHeight = 1400 // 流式内容增长
    c.onContentGrew()
    expect(v.scrollTop).toBe(1000) // clamp 到 scrollHeight - clientHeight
    expect(distanceFromBottom(v)).toBe(0)
    c.detach()
  })

  it('非流式（历史回看态）：内容增长零自动滚动（REQ-043 明确不做）', () => {
    const v = makeViewport({ scrollTop: 600, scrollHeight: 1000, clientHeight: 400 })
    const c = new AnswerScrollController(v)
    c.setStreaming(false)
    v.scrollHeight = 2000
    c.onContentGrew()
    expect(v.scrollTop).toBe(600) // 未动
    c.detach()
  })

  it('流式中上滚 9px（>8px）→ 立即接管；接管后内容增长不再自动滚动', () => {
    const v = makeViewport({ scrollTop: 600, scrollHeight: 1000, clientHeight: 400 })
    const c = new AnswerScrollController(v)
    c.setStreaming(true)
    // 贴底（600 距底 0），随后用户上滚 9px
    v.scrollTop = 591
    v.dispatchScroll()
    expect(c.phase).toBe('taken_over')
    // 流式内容继续增长（渲染照常，控制器仅停止自动滚动）
    v.scrollHeight = 1600
    c.onContentGrew()
    expect(v.scrollTop).toBe(591) // 未被拽回
    c.detach()
  })

  it('上滚恰 8px（不大于阈值）→ 不接管', () => {
    const v = makeViewport({ scrollTop: 600, scrollHeight: 1000, clientHeight: 400 })
    const c = new AnswerScrollController(v)
    c.setStreaming(true)
    v.scrollTop = 592
    v.dispatchScroll()
    expect(c.phase).toBe('following')
    c.detach()
  })

  it('上滚分多次累计 >8px → 接管（位移按累计判定）；向下滚动重置累计', () => {
    const v = makeViewport({ scrollTop: 600, scrollHeight: 1000, clientHeight: 400 })
    const c = new AnswerScrollController(v)
    c.setStreaming(true)
    v.scrollTop = 596
    v.dispatchScroll() // -4
    expect(c.phase).toBe('following')
    v.scrollTop = 598
    v.dispatchScroll() // +2（向下）→ 重置累计
    v.scrollTop = 593
    v.dispatchScroll() // -5
    expect(c.phase).toBe('following') // 累计 5 ≤ 8
    v.scrollTop = 588
    v.dispatchScroll() // -5 → 累计 10 > 8
    expect(c.phase).toBe('taken_over')
    c.detach()
  })

  it('消歧（重叠带）：接管后距底仍 <40px 的停留态 → 不被自动拽回', () => {
    // 上滚 10px 接管后距底 10px（<40 重叠带）
    const v = makeViewport({ scrollTop: 590, scrollHeight: 1000, clientHeight: 400 })
    const c = new AnswerScrollController(v)
    c.setStreaming(true)
    expect(distanceFromBottom(v)).toBe(10)
    // 接管（该次上滚由前一次 dispatch 完成）
    v.scrollTop = 580
    v.dispatchScroll()
    expect(c.phase).toBe('taken_over')
    // 流式内容增长使距底变化（非用户滚动）→ 不恢复
    v.scrollHeight = 2000
    c.onContentGrew()
    expect(c.phase).toBe('taken_over')
    expect(v.scrollTop).toBe(580)
    c.detach()
  })

  it('恢复①：接管后用户主动下滚进入贴底区（距底 <40px）→ 恢复跟随', () => {
    const v = makeViewport({ scrollTop: 590, scrollHeight: 1000, clientHeight: 400 })
    const c = new AnswerScrollController(v)
    c.setStreaming(true)
    v.scrollTop = 300
    v.dispatchScroll() // 大幅上滚 → 接管
    expect(c.phase).toBe('taken_over')
    v.scrollHeight = 2400
    // 用户主动下滚：进入贴底区（距底 30 < 40）
    v.scrollTop = 2400 - 400 - 30
    v.dispatchScroll()
    expect(c.phase).toBe('following')
    // 恢复后流式增长继续自动贴底
    v.scrollHeight = 2600
    c.onContentGrew()
    expect(v.scrollTop).toBe(2200) // clamp 到 scrollHeight - clientHeight
    c.detach()
  })

  it('恢复①反例：接管后用户下滚但未进入贴底区（距底 ≥40px）→ 维持接管', () => {
    const v = makeViewport({ scrollTop: 590, scrollHeight: 1000, clientHeight: 400 })
    const c = new AnswerScrollController(v)
    c.setStreaming(true)
    v.scrollTop = 300
    v.dispatchScroll()
    expect(c.phase).toBe('taken_over')
    v.scrollHeight = 2400
    v.scrollTop = 2400 - 400 - 500 // 距底 500
    v.dispatchScroll()
    expect(c.phase).toBe('taken_over')
    c.detach()
  })

  it('恢复②：resumeViaFab → 立即恢复跟随 + 平滑滚到底（REQ-045 联动）', () => {
    const v = makeViewport({ scrollTop: 300, scrollHeight: 1000, clientHeight: 400 })
    const c = new AnswerScrollController(v)
    c.setStreaming(true)
    v.scrollTop = 100
    v.dispatchScroll()
    expect(c.phase).toBe('taken_over')
    c.resumeViaFab()
    expect(c.phase).toBe('following')
    expect(v.scrollToCalls.at(-1)?.behavior).toBe('smooth')
    // 恢复后内容增长继续跟随
    v.scrollHeight = 1500
    c.onContentGrew()
    expect(v.scrollTop).toBe(1100) // clamp 到 scrollHeight - clientHeight
    c.detach()
  })

  it('恢复③：notifyNewTurn（发送新消息/新一轮）→ 恢复跟随并即时贴底', () => {
    const v = makeViewport({ scrollTop: 100, scrollHeight: 1000, clientHeight: 400 })
    const c = new AnswerScrollController(v)
    c.setStreaming(true)
    v.scrollTop = 50
    v.dispatchScroll()
    expect(c.phase).toBe('taken_over')
    c.notifyNewTurn()
    expect(c.phase).toBe('following')
    expect(v.scrollTop).toBe(600) // 即时贴底（clamp 到最大滚动位）
    c.detach()
  })

  it('ScrollToBottomFab 显隐：距底超过约一视口高 → 显示；贴底 → 隐藏（REQ-045）', () => {
    const v = makeViewport({ scrollTop: 0, scrollHeight: 1000, clientHeight: 400 })
    const c = new AnswerScrollController(v)
    expect(c.showFab).toBe(false)
    // 距底 610 > 一屏 400 → 显示
    v.scrollTop = 90
    v.dispatchScroll()
    expect(c.showFab).toBe(true)
    // 滚回贴底（距底 0）→ 隐藏
    v.scrollTop = 600
    v.dispatchScroll()
    expect(c.showFab).toBe(false)
    c.detach()
  })

  it('detach 移除 scroll 监听（重复挂载/卸载安全）', () => {
    const v = makeViewport()
    const c = new AnswerScrollController(v)
    expect(v.addEventListener).toHaveBeenCalledTimes(1)
    c.detach()
    expect(v.removeEventListener).toHaveBeenCalledTimes(1)
    // detach 后 scroll 事件不再驱动状态
    const before = c.phase
    v.scrollTop = 0
    v.dispatchScroll()
    expect(c.phase).toBe(before)
  })

  it('状态回调：phase/showFab 变化经 onStateChange 通知（供 Vue ref 镜像）', () => {
    const v = makeViewport({ scrollTop: 0, scrollHeight: 1000, clientHeight: 400 })
    const c = new AnswerScrollController(v)
    const seen: string[] = []
    c.onStateChange = (s) => seen.push(`${s.phase}/${s.showFab}`)
    c.setStreaming(true)
    v.scrollTop = 390 // 距底 610 > 一屏
    v.dispatchScroll()
    c.scrollTopByUser(299) // 程序化上滚 >8（等价用户拖滚动条）
    v.dispatchScroll()
    expect(seen.length).toBeGreaterThan(0)
    expect(c.phase).toBe('taken_over')
    c.detach()
  })

  it('scrollTopByUser：直接以程序化赋值模拟用户滚动（供组件层装配）', () => {
    const v = makeViewport({ scrollTop: 0, scrollHeight: 1000, clientHeight: 400 })
    const c = new AnswerScrollController(v)
    c.setStreaming(true)
    c.scrollTopByUser(500) // 程序化置于中部（等价用户拖滚动条）
    v.dispatchScroll()
    c.scrollTopByUser(400)
    v.dispatchScroll() // 上滚 100 → 接管
    expect(c.phase).toBe('taken_over')
    c.detach()
  })
})
