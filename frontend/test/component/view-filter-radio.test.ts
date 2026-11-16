/**
 * test/component/view-filter-radio.test.ts — ViewFilterRadio 组件层
 * （frontend-api.md §5 v1.21 / REQ-058/A42：三态视图 radio）。
 * 断言面：role=radiogroup/radio、aria-checked、roving tabindex、方向键切换（含焦点跟随）、
 * 点击选择 → store.setViewFilter 参数映射。
 */
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/client'
import { listThreads } from '@/api/threads'
import { i18n } from '@/i18n'
import ViewFilterRadio from '@/components/threads/ViewFilterRadio.vue'
import { toast } from '@/composables/toast'
import { useThreadListStore } from '@/stores/threadList'

vi.mock('@/api/threads', () => ({
  listThreads: vi.fn(),
  patchThread: vi.fn(),
  deleteThread: vi.fn(),
}))

vi.mock('@/composables/toast', () => ({ toast: vi.fn() }))

const mockedList = vi.mocked(listThreads)
const mockedToast = vi.mocked(toast)

let activePinia: ReturnType<typeof createPinia>

beforeEach(() => {
  activePinia = createPinia()
  setActivePinia(activePinia)
  vi.clearAllMocks()
  mockedList.mockResolvedValue({ total: 0, items: [] })
})

afterEach(() => {
  document.body.innerHTML = ''
})

function mountRadio() {
  const w = mount(ViewFilterRadio, {
    props: { modelValue: 'active' as const },
    global: { plugins: [activePinia, i18n] },
    attachTo: document.body, // 方向键焦点跟随须走真实 DOM 语义
  })
  return { w, store: useThreadListStore() }
}

describe('ViewFilterRadio（三态视图 radio）', () => {
  it('三选项渲染：全部/未读/已归档；role=radiogroup/radio + aria-checked', () => {
    const { w } = mountRadio()
    expect(w.find('[role="radiogroup"]').exists()).toBe(true)
    const radios = w.findAll('[role="radio"]')
    expect(radios).toHaveLength(3)
    expect(radios.map((x) => x.text())).toEqual(['全部', '未读', '已归档'])
    expect(radios[0]!.attributes('aria-checked')).toBe('true')
    expect(radios[1]!.attributes('aria-checked')).toBe('false')
    // roving tabindex：选中项 0，其余 -1
    expect(radios[0]!.attributes('tabindex')).toBe('0')
    expect(radios[1]!.attributes('tabindex')).toBe('-1')
  })

  it('点击未读 → update:modelValue=unread + store.setViewFilter（GET 带 unread=true）', async () => {
    const { w, store } = mountRadio()
    await w.find('[data-testid="view-filter-unread"]').trigger('click')
    await flushPromises()
    expect(w.emitted('update:modelValue')).toEqual([['unread']])
    expect(store.viewFilter).toBe('unread')
    expect(mockedList).toHaveBeenLastCalledWith(expect.objectContaining({ unread: true }))
  })

  it('方向键切换：ArrowRight active→unread，焦点随选中项移动；循环到 archived', async () => {
    const { w, store } = mountRadio()
    const radios = w.findAll('[role="radio"]')
    await radios[0]!.trigger('keydown', { key: 'ArrowRight' })
    await flushPromises()
    expect(store.viewFilter).toBe('unread')
    // 模拟父级 v-model 回写后再断言选中态/焦点
    await w.setProps({ modelValue: 'unread' })
    expect(w.find('[data-testid="view-filter-unread"]').attributes('aria-checked')).toBe('true')
    // 焦点随选中项（attachTo 后 focus 生效）
    expect(document.activeElement?.getAttribute('data-testid')).toBe('view-filter-unread')
    // 再 ArrowRight → archived；再 ArrowRight 循环回 active
    await w.find('[data-testid="view-filter-unread"]').trigger('keydown', { key: 'ArrowRight' })
    await flushPromises()
    await w.setProps({ modelValue: 'archived' })
    expect(store.viewFilter).toBe('archived')
    await w.find('[data-testid="view-filter-archived"]').trigger('keydown', { key: 'ArrowRight' })
    await flushPromises()
    await w.setProps({ modelValue: 'active' })
    expect(store.viewFilter).toBe('active')
    // ArrowLeft 反向
    await w.find('[data-testid="view-filter-active"]').trigger('keydown', { key: 'ArrowLeft' })
    await flushPromises()
    expect(store.viewFilter).toBe('archived')
  })

  it('同值点击 no-op（不重复发请求）', async () => {
    const { w } = mountRadio()
    mockedList.mockClear()
    await w.find('[data-testid="view-filter-active"]').trigger('click')
    await flushPromises()
    expect(mockedList).not.toHaveBeenCalled()
    expect(w.emitted('update:modelValue')).toBeUndefined()
  })

  it('切换失败（P2-4）：toast 服务端 message + store 状态回滚（radio 选中态随 prop 复原）', async () => {
    const { w, store } = mountRadio()
    mockedList.mockRejectedValueOnce(new ApiError('400001', '请求参数错误'))
    await w.find('[data-testid="view-filter-archived"]').trigger('click')
    await flushPromises()
    expect(mockedToast).toHaveBeenCalledWith('请求参数错误')
    // store 回滚 → 选中态与列表视图保持一致（active）
    expect(store.viewFilter).toBe('active')
    expect(w.find('[data-testid="view-filter-active"]').attributes('aria-checked')).toBe('true')
    expect(w.find('[data-testid="view-filter-archived"]').attributes('aria-checked')).toBe('false')
  })
})
