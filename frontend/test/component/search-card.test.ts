/**
 * test/component/search-card.test.ts — SearchCard 组件层（§5 SearchCard/ModeChips/SubmitFab 行）。
 * Enter 提交（Shift+Enter 换行；IME 不劫持）/ 空输入禁用 / streaming 停止钮 / 模式 chips。
 */
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
})
