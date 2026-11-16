/**
 * test/component/theme-switcher.test.ts — ThemeSwitcher 组件层（REQ-054/A38）。
 * 左下用户区触发轻弹层「外观」radio 三选（role=radiogroup/radio + aria-checked + 方向键切换）；
 * 切换即时写 dataset.theme（无刷新）；选择持久化 localStorage；关闭后焦点返还触发钮。
 */
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ThemeSwitcher from '@/components/common/ThemeSwitcher.vue'
import { i18n } from '@/i18n'

const mediaListeners = new Set<(e: MediaQueryListEvent) => void>()

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  delete document.documentElement.dataset.theme
  mediaListeners.clear()
  // jsdom 无 matchMedia：stub（matches=false → 浅色解析）
  vi.stubGlobal(
    'matchMedia',
    vi.fn().mockImplementation((query: string) => ({
      matches: query.includes('prefers-color-scheme: dark') ? false : false,
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
})

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

function mountSwitcher(): VueWrapper {
  return mount(ThemeSwitcher, {
    attachTo: document.body, // 焦点返还与 document 级外点关闭须走真实 DOM 语义
    global: { plugins: [i18n] },
  })
}

describe('ThemeSwitcher（REQ-054/A38：外观三选弹层）', () => {
  it('默认收起；触发钮为用户区形态（avatar + 名称），aria-haspopup/aria-expanded 语义', () => {
    const w = mountSwitcher()
    expect(w.find('[data-testid="theme-pop"]').exists()).toBe(false)
    const trigger = w.find('[data-testid="theme-trigger"]')
    expect(trigger.exists()).toBe(true)
    expect(trigger.find('.avatar').exists()).toBe(true)
    expect(trigger.text()).toContain('默认用户')
    expect(trigger.attributes('aria-haspopup')).toBe('true')
    expect(trigger.attributes('aria-expanded')).toBe('false')
    w.unmount()
  })

  it('点击触发 → 弹层展开（aria-expanded=true），radiogroup + 三 radio，选中态 aria-checked', async () => {
    const w = mountSwitcher()
    await w.find('[data-testid="theme-trigger"]').trigger('click')
    const pop = w.find('[data-testid="theme-pop"]')
    expect(pop.exists()).toBe(true)
    expect(w.find('[data-testid="theme-trigger"]').attributes('aria-expanded')).toBe('true')
    const group = w.find('[data-testid="theme-radiogroup"]')
    expect(group.attributes('role')).toBe('radiogroup')
    const radios = group.findAll('[role="radio"]')
    expect(radios).toHaveLength(3)
    expect(radios.map((r) => r.text())).toEqual(['浅色', '深色', '跟随系统'])
    // 缺省 system：跟随系统选中（aria-checked=true），其余 false
    expect(radios[2]!.attributes('aria-checked')).toBe('true')
    expect(radios[0]!.attributes('aria-checked')).toBe('false')
    // roving tabindex：选中项可 Tab 聚焦，未选项 Tab 序后置
    expect(radios[2]!.attributes('tabindex')).toBe('0')
    expect(radios[0]!.attributes('tabindex')).toBe('-1')
    w.unmount()
  })

  it('三选切换：store + localStorage + dataset.theme 即时生效（无刷新）；aria-checked 跟随', async () => {
    const w = mountSwitcher()
    // 选择即关闭弹层 → 重开校验 aria-checked（radio 单选语义）
    await w.find('[data-testid="theme-trigger"]').trigger('click')
    await w.find('[data-testid="theme-opt-light"]').trigger('click')
    expect(document.documentElement.dataset.theme).toBe('light')
    expect(localStorage.getItem('ai-search.theme')).toBe('light')
    await w.find('[data-testid="theme-trigger"]').trigger('click')
    expect(w.find('[data-testid="theme-opt-light"]').attributes('aria-checked')).toBe('true')
    await w.find('[data-testid="theme-opt-dark"]').trigger('click')
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(localStorage.getItem('ai-search.theme')).toBe('dark')
    await w.find('[data-testid="theme-trigger"]').trigger('click')
    await w.find('[data-testid="theme-opt-system"]').trigger('click')
    expect(localStorage.getItem('ai-search.theme')).toBe('system')
    expect(document.documentElement.dataset.theme).toBe('light') // system → 解析浅色（stub matches=false）
    w.unmount()
  })

  it('方向键可切换（A38 a11y 硬性）：ArrowDown/ArrowUp 移动选中并应用', async () => {
    const w = mountSwitcher()
    await w.find('[data-testid="theme-trigger"]').trigger('click')
    const group = w.find('[data-testid="theme-radiogroup"]')
    await group.trigger('keydown', { key: 'ArrowDown' })
    expect(localStorage.getItem('ai-search.theme')).toBe('light')
    await group.trigger('keydown', { key: 'ArrowDown' })
    expect(localStorage.getItem('ai-search.theme')).toBe('dark')
    await group.trigger('keydown', { key: 'ArrowUp' })
    expect(localStorage.getItem('ai-search.theme')).toBe('light')
    w.unmount()
  })

  it('选择后弹层关闭且焦点返还触发钮（A38 关闭焦点返还）', async () => {
    const w = mountSwitcher()
    const trigger = w.find('[data-testid="theme-trigger"]')
    await trigger.trigger('click')
    expect(document.activeElement?.getAttribute('data-testid')).toMatch(/^theme-opt-/)
    await w.find('[data-testid="theme-opt-light"]').trigger('click')
    expect(w.find('[data-testid="theme-pop"]').exists()).toBe(false)
    expect(document.activeElement).toBe(trigger.element)
    w.unmount()
  })

  it('Esc 关闭 + 焦点返还；外点关闭', async () => {
    const w = mountSwitcher()
    const trigger = w.find('[data-testid="theme-trigger"]')
    await trigger.trigger('click')
    const group = w.find('[data-testid="theme-radiogroup"]')
    await group.trigger('keydown', { key: 'Escape' })
    expect(w.find('[data-testid="theme-pop"]').exists()).toBe(false)
    expect(document.activeElement).toBe(trigger.element)

    await trigger.trigger('click')
    expect(w.find('[data-testid="theme-pop"]').exists()).toBe(true)
    document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
    await flushPromises()
    expect(w.find('[data-testid="theme-pop"]').exists()).toBe(false)
    w.unmount()
  })

  it('i18n：外观标题与三选项 zh/en 双语齐备', () => {
    expect(i18n.global.t('theme.appearance')).toBe('外观')
    expect(i18n.global.t('theme.light')).toBe('浅色')
    expect(i18n.global.t('theme.dark')).toBe('深色')
    expect(i18n.global.t('theme.system')).toBe('跟随系统')
    i18n.global.locale.value = 'en-US'
    expect(i18n.global.t('theme.appearance')).toBe('Appearance')
    expect(i18n.global.t('theme.light')).toBe('Light')
    expect(i18n.global.t('theme.dark')).toBe('Dark')
    expect(i18n.global.t('theme.system')).toBe('System')
    i18n.global.locale.value = 'zh-CN'
  })
})
