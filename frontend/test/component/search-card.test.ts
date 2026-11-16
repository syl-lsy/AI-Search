/**
 * test/component/search-card.test.ts — SearchCard 组件层（§5 SearchCard/ModeChips/SubmitFab 行）。
 * Enter 提交（Shift+Enter 换行；IME 不劫持）/ 空输入禁用 / streaming 停止钮 / 模式 chips
 * + 修复项④：fluid 同宽 prop / 整卡可点聚焦 / 焦点反馈整卡化（样式契约）。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { createPinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import SearchCard from '@/components/search/SearchCard.vue'
import { i18n } from '@/i18n'

vi.mock('@/api/client', () => ({
  API_BASE_URL: '',
  apiGet: vi.fn().mockResolvedValue({ options: [], default: 'qwen' }),
  apiPost: vi.fn(),
  apiPut: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
  apiDownload: vi.fn(),
  normalizeError: () => ({ name: 'ApiError', code: 'network_error', message: '网络异常，请稍后重试' }),
}))

function mountCard(props: Partial<{ modelValue: string; mode: 'fast' | 'deep'; streaming: boolean }> = {}) {
  return mount(SearchCard, {
    props: {
      modelValue: props.modelValue ?? '',
      mode: props.mode ?? 'fast',
      streaming: props.streaming ?? false,
      placeholder: '问任何事情…',
    },
    global: { plugins: [createPinia(), i18n] },
  })
}

describe('SearchCard（三处复用输入卡）', () => {
  it('输入 → update:modelValue 上抛', async () => {
    const w = mountCard()
    await w.find('textarea').setValue('你好')
    expect(w.emitted('update:modelValue')?.at(-1)).toEqual(['你好'])
  })

  it('Enter 提交（有输入时）；Shift+Enter 换行不提交', async () => {
    const w = mountCard({ modelValue: '问题' })
    await w.find('textarea').trigger('keydown', { key: 'Enter' })
    expect(w.emitted('submit')).toHaveLength(1)
    await w.find('textarea').trigger('keydown', { key: 'Enter', shiftKey: true })
    expect(w.emitted('submit')).toHaveLength(1)
  })

  it('IME 组合中的 Enter 不劫持（isComposing / keyCode 229 双口径）', async () => {
    const w = mountCard({ modelValue: '问题' })
    await w.find('textarea').trigger('keydown', { key: 'Enter', isComposing: true })
    await w.find('textarea').trigger('keydown', { key: 'Enter', keyCode: 229 })
    expect(w.emitted('submit')).toBeUndefined()
  })

  it('空输入：submit fab 禁用且 Enter 不提交', async () => {
    const w = mountCard({ modelValue: '   ' })
    expect(w.find('[data-testid="submit-btn"]').attributes('disabled')).toBeDefined()
    await w.find('textarea').trigger('keydown', { key: 'Enter' })
    expect(w.emitted('submit')).toBeUndefined()
  })

  it('streaming=true：提交钮变停止钮（§2 reject/null 现状）；点击 → cancel 上抛', async () => {
    const w = mountCard({ modelValue: '问题', streaming: true })
    expect(w.find('[data-testid="submit-btn"]').exists()).toBe(false)
    expect(w.find('[data-testid="stop-btn"]').exists()).toBe(true)
    await w.find('[data-testid="stop-btn"]').trigger('click')
    expect(w.emitted('cancel')).toHaveLength(1)
    // streaming 态 Enter 不触发提交
    await w.find('textarea').trigger('keydown', { key: 'Enter' })
    expect(w.emitted('submit')).toBeUndefined()
  })

  it('模式 chips 切换 → update:mode 上抛 + active 态标注（aria-pressed）', async () => {
    const w = mountCard()
    expect(w.find('[data-testid="mode-fast"]').classes()).toContain('active')
    await w.find('[data-testid="mode-deep"]').trigger('click')
    expect(w.emitted('update:mode')?.at(-1)).toEqual(['deep'])
    await w.find('[data-testid="mode-fast"]').trigger('click')
    expect(w.emitted('update:mode')?.at(-1)).toEqual(['fast'])
  })

  it('附件按钮为 M4 留位（disabled 占位）', () => {
    const w = mountCard()
    expect(w.find('.attach-btn').attributes('disabled')).toBeDefined()
  })

  it('fluid prop：根元素带 fluid class（首页与推荐面板同宽 = --layout-searchcard-w，修复项④b）', () => {
    const w = mount(SearchCard, {
      props: { modelValue: '', mode: 'fast', placeholder: '问任何事情…', fluid: true },
      global: { plugins: [createPinia(), i18n] },
    })
    expect(w.find('[data-testid="search-card"]').classes()).toContain('fluid')
  })

  it('默认（无 fluid）：根元素不带 fluid class（答案页追问卡/历史页内嵌不受影响）', () => {
    const w = mountCard()
    expect(w.find('[data-testid="search-card"]').classes()).not.toContain('fluid')
  })

  it('整卡可点（修复项④c）：点击卡片空白区 → 聚焦 textarea', async () => {
    const w = mount(SearchCard, {
      props: { modelValue: '', mode: 'fast', placeholder: '问任何事情…' },
      global: { plugins: [createPinia(), i18n] },
      attachTo: document.body,
    })
    await w.find('[data-testid="search-card"]').trigger('click')
    expect(document.activeElement).toBe(w.find('textarea').element)
    w.unmount()
  })

  it('整卡可点不劫持交互元素：点击工具行 chip/提交钮不触发聚焦（事件目标为交互元素时忽略）', async () => {
    const w = mount(SearchCard, {
      props: { modelValue: '问题', mode: 'fast', placeholder: '问任何事情…' },
      global: { plugins: [createPinia(), i18n] },
      attachTo: document.body,
    })
    await w.find('[data-testid="mode-fast"]').trigger('click')
    expect(document.activeElement).not.toBe(w.find('textarea').element)
    expect(w.emitted('update:mode')?.at(-1)).toEqual(['fast'])
    await w.find('[data-testid="submit-btn"]').trigger('click')
    expect(document.activeElement).not.toBe(w.find('textarea').element)
    expect(w.emitted('submit')).toHaveLength(1)
    w.unmount()
  })

  it('焦点反馈落在整卡（修复项④a）：textarea 无全局绿环覆盖 + :focus-within accent 描边（样式契约锁定）', () => {
    // vitest 默认不注入 SFC CSS（css:false）——样式断言以源码契约形态锁定：
    // a) textarea:focus-visible box-shadow:none（全局 tokens :focus-visible 绿环不上收 textarea）
    // b) .search-card:focus-within 描边走 tokens accent（--color-accent-border）
    const source = readFileSync(resolve(process.cwd(), 'src/components/search/SearchCard.vue'), 'utf8')
    expect(source).toMatch(/\.search-card textarea:focus-visible\s*\{[^}]*box-shadow:\s*none/)
    expect(source).toMatch(/\.search-card:focus-within\s*\{[^}]*--color-accent-border/)
  })
})
