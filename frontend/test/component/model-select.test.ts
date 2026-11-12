/**
 * test/component/model-select.test.ts — ModelSelect 组件层（§5 ModelSelect 行 / REQ-010 / §2 v1.7）。
 * GET /models 数据 + prefsStore；label 服务端常量；无用户选择展示 default；下拉选择持久化。
 */
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { apiGet } from '@/api/client'
import ModelSelect from '@/components/search/ModelSelect.vue'
import { modelsFixture } from '@/mocks/models'
import { i18n } from '@/i18n'
import { usePrefsStore } from '@/stores/prefs'

vi.mock('@/api/client', () => ({
  API_BASE_URL: '',
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPut: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
  apiDownload: vi.fn(),
  normalizeError: () => ({ name: 'ApiError', code: 'network_error', message: '网络异常，请稍后重试' }),
}))

const mockedGet = vi.mocked(apiGet)

function mountSelect() {
  return mount(ModelSelect, { global: { plugins: [i18n] } })
}

describe('ModelSelect（REQ-010）', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    vi.clearAllMocks()
    mockedGet.mockResolvedValue(modelsFixture)
  })

  it('首屏拉取 GET /models：无用户选择展示 default（不本地硬编码，§2 v1.7 注）', async () => {
    const w = mountSelect()
    await flushPromises()
    expect(mockedGet).toHaveBeenCalledWith('/api/v1/models')
    expect(w.find('[data-testid="model-select"]').text()).toContain('Qwen')
  })

  it('用户已选模型（prefsStore）优先展示 label（服务端常量，不拼接）', async () => {
    const prefs = usePrefsStore()
    prefs.setModel('glm')
    const w = mountSelect()
    await flushPromises()
    expect(w.find('[data-testid="model-select"]').text()).toContain('GLM')
  })

  it('下拉选择：菜单渲染 options → pick 持久化 prefs.model 并收起', async () => {
    const w = mountSelect()
    await flushPromises()
    await w.find('[data-testid="model-select"]').trigger('click')
    const menu = w.find('[data-testid="model-menu"]')
    expect(menu.exists()).toBe(true)
    expect(menu.findAll('[role="option"]')).toHaveLength(3)
    await menu.findAll('[role="option"]')[2]!.trigger('click')
    const prefs = usePrefsStore()
    expect(prefs.model).toBe('deepseek')
    expect(localStorage.getItem('ai-search.model')).toBe('deepseek')
    expect(w.find('[data-testid="model-menu"]').exists()).toBe(false)
  })

  it('Esc 收起 + aria-expanded/haspopup 标注', async () => {
    const w = mountSelect()
    await flushPromises()
    const chip = w.find('[data-testid="model-select"]')
    expect(chip.attributes('aria-haspopup')).toBe('listbox')
    await chip.trigger('click')
    expect(chip.attributes('aria-expanded')).toBe('true')
    await w.find('.ms-root').trigger('keydown', { key: 'Escape' })
    expect(chip.attributes('aria-expanded')).toBe('false')
  })

  it('GET /models 失败：chip 静默降级（toast 兜底文案，不阻塞页面）', async () => {
    mockedGet.mockRejectedValueOnce(new Error('network down'))
    const w = mountSelect()
    await flushPromises()
    expect(w.find('[data-testid="model-select"]').exists()).toBe(true)
    expect(w.find('[data-testid="model-menu"]').exists()).toBe(false)
  })
})
