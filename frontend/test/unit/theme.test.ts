/**
 * test/unit/theme.test.ts — 主题三选（REQ-054/A38，frontend-api.md §2/§4 v1.18 prefsStore.theme）。
 *
 * store 面：theme 三档持久化（localStorage `ai-search.theme`，缺省 system，try/catch）+
 * resolvedTheme 解析（system → matchMedia('(prefers-color-scheme: dark)')）。
 * 生效机制面（useThemeEffect，App/ThemeSwitcher 装配）：watch 写
 * document.documentElement.dataset.theme；system 档监听媒体查询 change 实时跟随（无需刷新）；
 * 卸载清理（onUnmounted）。
 */
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, nextTick } from 'vue'
import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useThemeEffect } from '@/composables/theme'
import { usePrefsStore } from '@/stores/prefs'
import { isThemePreference } from '@/utils/guards'

const mediaListeners = new Set<(e: MediaQueryListEvent) => void>()
let systemDark = false

/** jsdom 无 matchMedia：可控 stub 驱动（登记 change 监听以便模拟系统切换） */
function stubMatchMedia(): void {
  vi.stubGlobal(
    'matchMedia',
    vi.fn().mockImplementation((query: string) => ({
      matches: query.includes('prefers-color-scheme: dark') ? systemDark : false,
      media: query,
      onchange: null,
      addEventListener: (_type: string, cb: (e: MediaQueryListEvent) => void) => {
        mediaListeners.add(cb)
      },
      removeEventListener: (_type: string, cb: (e: MediaQueryListEvent) => void) => {
        mediaListeners.delete(cb)
      },
      addListener: vi.fn(),
      removeListener: vi.fn(),
    })),
  )
}

/** 模拟操作系统外观切换：更新 matches + 派发 change（A38 实时跟随路径） */
function flipSystemDark(next: boolean): void {
  systemDark = next
  for (const cb of [...mediaListeners]) cb({ matches: next } as MediaQueryListEvent)
}

/** useThemeEffect 装配宿主（App/ThemeSwitcher 同款；unmount 触发 onUnmounted 清理） */
function mountHost() {
  return mount(defineComponent({ setup: () => { useThemeEffect(); return () => '' } }))
}

function datasetTheme(): string {
  return document.documentElement.dataset.theme ?? ''
}

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  delete document.documentElement.dataset.theme
  mediaListeners.clear()
  systemDark = false
  stubMatchMedia()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('guards.isThemePreference（localStorage 无类型边界守卫，§8.9③）', () => {
  it('三合法档收窄；脏值拒绝', () => {
    expect(isThemePreference('light')).toBe(true)
    expect(isThemePreference('dark')).toBe(true)
    expect(isThemePreference('system')).toBe(true)
    expect(isThemePreference('blue')).toBe(false)
    expect(isThemePreference(null)).toBe(false)
    expect(isThemePreference(42)).toBe(false)
  })
})

describe('prefsStore.theme（REQ-054：三档持久化 + resolvedTheme 解析）', () => {
  it('缺省 system；resolvedTheme 按当前系统偏好解析', () => {
    systemDark = true
    const prefs = usePrefsStore()
    expect(prefs.theme).toBe('system')
    expect(prefs.resolvedTheme).toBe('dark')
  })

  it('setTheme：即时生效 + 持久化 ai-search.theme；显式档不受系统切换影响', () => {
    const prefs = usePrefsStore()
    prefs.setTheme('light')
    expect(prefs.theme).toBe('light')
    expect(prefs.resolvedTheme).toBe('light')
    expect(localStorage.getItem('ai-search.theme')).toBe('light')
    flipSystemDark(true) // 显式 light 不随系统变化
    expect(prefs.resolvedTheme).toBe('light')
    prefs.setTheme('dark')
    expect(prefs.resolvedTheme).toBe('dark')
    expect(localStorage.getItem('ai-search.theme')).toBe('dark')
  })

  it('system 档实时跟随：syncSystemPrefersDark 更新 resolvedTheme（监听 change 的数据面）', () => {
    const prefs = usePrefsStore()
    expect(prefs.resolvedTheme).toBe('light')
    prefs.syncSystemPrefersDark(true)
    expect(prefs.resolvedTheme).toBe('dark')
    prefs.syncSystemPrefersDark(false)
    expect(prefs.resolvedTheme).toBe('light')
  })

  it('初始化收窄：localStorage 脏值回退默认 system（§8.9③）', () => {
    localStorage.setItem('ai-search.theme', 'blue')
    const prefs = usePrefsStore()
    expect(prefs.theme).toBe('system')
  })

  it('存储不可用静默降级：读失败回默认、写失败仅内存态（try/catch）', () => {
    const readSpy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    const writeSpy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    const prefs = usePrefsStore()
    expect(prefs.theme).toBe('system') // 读失败 → 默认
    prefs.setTheme('dark') // 写失败 → 仅内存态
    expect(prefs.theme).toBe('dark')
    readSpy.mockRestore()
    writeSpy.mockRestore()
  })
})

describe('useThemeEffect 生效机制（dataset 写入 + system 跟随 + 卸载清理）', () => {
  it('挂载即写 document.documentElement.dataset.theme（resolvedTheme 具体值）', async () => {
    systemDark = true
    const prefs = usePrefsStore()
    expect(prefs.resolvedTheme).toBe('dark')
    mountHost()
    await nextTick()
    expect(datasetTheme()).toBe('dark')
  })

  it('切换档位无刷新生效：setTheme → dataset 即时更新（A38 CSS 变量切换）', async () => {
    mountHost()
    const prefs = usePrefsStore()
    prefs.setTheme('light')
    await nextTick()
    expect(datasetTheme()).toBe('light')
    prefs.setTheme('dark')
    await nextTick()
    expect(datasetTheme()).toBe('dark')
    prefs.setTheme('system')
    await nextTick()
    expect(datasetTheme()).toBe('light') // systemDark=false
  })

  it('system 档监听媒体查询 change 实时跟随（无需刷新，A38）', async () => {
    mountHost()
    await nextTick()
    expect(datasetTheme()).toBe('light')
    flipSystemDark(true)
    await nextTick()
    expect(datasetTheme()).toBe('dark')
    flipSystemDark(false)
    await nextTick()
    expect(datasetTheme()).toBe('light')
  })

  it('卸载清理：change 监听移除（卸载后系统切换不再改写 dataset）', () => {
    const before = mediaListeners.size
    const host = mountHost()
    expect(mediaListeners.size).toBe(before + 1)
    host.unmount()
    expect(mediaListeners.size).toBe(before)
    flipSystemDark(true)
    expect(datasetTheme()).not.toBe('dark')
  })
})
