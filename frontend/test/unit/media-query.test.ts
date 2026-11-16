/**
 * test/unit/media-query.test.ts — useMediaQuery 断点 composable（覆盖率恢复批补测）。
 *
 * 既有行为 characterization：初始 matches 读取、change 事件跟随、旧实现 addListener 回落
 * （MQL 无 addEventListener 仅有 addListener）、卸载清理（removeEventListener / legacy
 * removeListener / legacy 无 removeListener 的 `?.` 短路）、无 window 守卫。
 */
import { defineComponent, h } from 'vue'
import { mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useMediaQuery } from '@/composables/mediaQuery'

type Captured = ReturnType<typeof useMediaQuery>

/** 挂一个仅调用 useMediaQuery 的宿主组件（onUnmounted 需组件上下文） */
function hostWith(query: string, capture: (v: Captured) => void): VueWrapper {
  return mount(
    defineComponent({
      setup() {
        capture(useMediaQuery(query))
        return () => h('div')
      },
    }),
    { global: {} },
  )
}

interface FakeMql {
  matches: boolean
  media: string
  onchange: null
  addEventListener?: (type: string, cb: (e: MediaQueryListEvent) => void) => void
  removeEventListener?: (type: string, cb: (e: MediaQueryListEvent) => void) => void
  addListener?: (cb: () => void) => void
  removeListener?: (cb: () => void) => void
}

function stubMatchMedia(factory: (query: string) => FakeMql): void {
  vi.stubGlobal('matchMedia', vi.fn().mockImplementation(factory))
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('useMediaQuery（断点 composable）', () => {
  it('初始 matches 读取 + change 事件跟随（现代 addEventListener 路径）', () => {
    let listener: ((e: MediaQueryListEvent) => void) | null = null
    // update 读取 mql.matches（非事件参）——持有同一 mql 对象做变更
    const mql: FakeMql = {
      matches: true,
      media: '(max-width: 1200px)',
      onchange: null,
      addEventListener: (_type, cb) => {
        listener = cb
      },
      removeEventListener: () => undefined,
    }
    stubMatchMedia(() => mql)
    let captured: Captured | null = null
    const w = hostWith('(max-width: 1200px)', (v) => {
      captured = v
    })
    expect(captured!.value).toBe(true)
    mql.matches = false
    listener!({ matches: false } as MediaQueryListEvent)
    expect(captured!.value).toBe(false)
    w.unmount()
  })

  it('旧实现回落：无 addEventListener 仅有 addListener → 注册/跟随/卸载 removeListener 清理', () => {
    let legacyCb: (() => void) | null = null
    const removeSpy = vi.fn()
    const mql: FakeMql = {
      matches: false,
      media: '(max-width: 720px)',
      onchange: null,
      addListener: (cb) => {
        legacyCb = cb
      },
      removeListener: removeSpy,
    }
    stubMatchMedia(() => mql)
    let captured: Captured | null = null
    const w = hostWith('(max-width: 720px)', (v) => {
      captured = v
    })
    expect(legacyCb).not.toBeNull()
    mql.matches = true
    legacyCb!()
    expect(captured!.value).toBe(true)
    w.unmount()
    expect(removeSpy).toHaveBeenCalledWith(legacyCb)
  })

  it('旧实现无 removeListener：卸载清理 `?.` 短路零异常', () => {
    stubMatchMedia((query) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => undefined,
      // removeListener 缺失
    }))
    const w = hostWith('(max-width: 720px)', () => undefined)
    expect(() => w.unmount()).not.toThrow()
  })

  it('无 window 守卫：恒 false 且不触 matchMedia（守卫在组件注册前早退，可直调）', () => {
    vi.stubGlobal('window', undefined)
    const matchMediaSpy = vi.fn()
    vi.stubGlobal('matchMedia', matchMediaSpy)
    const captured = useMediaQuery('(max-width: 720px)')
    expect(captured.value).toBe(false)
    expect(matchMediaSpy).not.toHaveBeenCalled()
  })
})
