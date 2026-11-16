/**
 * test/component/ui-tooltip.test.ts — UiTooltip 组件层（frontend-api.md §5 v1.23：
 * 自定义 tooltip 气泡——主标题粗白 + 可选灰副题，hover 300ms 延迟显隐，Teleport 到 body
 * 定位触发钮下方居中、贴边钳制；非原生 title；a11y：气泡 aria-hidden，语义由触发钮
 * aria-label 承载）。
 */
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import UiTooltip from '@/components/common/UiTooltip.vue'

/** Teleport 后经 document 查询（wrapper.find 不穿 Teleport） */
function bubble(): HTMLElement | null {
  return document.querySelector('[data-testid="ui-tip-bubble"]')
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  document.body.innerHTML = ''
})

async function mountTip(props: { label: string; sub?: string } = { label: '搜索' }) {
  const w = mount(UiTooltip, {
    props,
    attachTo: document.body,
    slots: {
      default: '<button type="button" class="icon-btn" aria-label="触发钮">B</button>',
    },
  })
  await flushPromises()
  return w
}

describe('UiTooltip 显隐（v1.23：hover 300ms 延迟出现、离开即隐）', () => {
  it('默认不渲染气泡；hover 满 300ms 后出现（Teleport 到 body）', async () => {
    const w = await mountTip()
    expect(bubble()).toBeNull()
    await w.find('.ui-tip').trigger('mouseenter')
    vi.advanceTimersByTime(299)
    expect(bubble()).toBeNull() // 延迟窗口内不出现
    vi.advanceTimersByTime(1)
    await flushPromises()
    expect(bubble()).not.toBeNull()
    expect(bubble()!.textContent).toContain('搜索')
    w.unmount()
  })

  it('300ms 内离开：pending 计时器取消，气泡不出现', async () => {
    const w = await mountTip()
    const root = w.find('.ui-tip')
    await root.trigger('mouseenter')
    vi.advanceTimersByTime(100)
    await root.trigger('mouseleave')
    vi.advanceTimersByTime(400)
    expect(bubble()).toBeNull()
    w.unmount()
  })

  it('离开即隐（已显示态 mouseleave 立即移除，不等待延迟）', async () => {
    const w = await mountTip()
    const root = w.find('.ui-tip')
    await root.trigger('mouseenter')
    vi.advanceTimersByTime(300)
    await flushPromises()
    expect(bubble()).not.toBeNull()
    await root.trigger('mouseleave')
    expect(bubble()).toBeNull()
    w.unmount()
  })

  it('键盘 focus 显示 / focusout 隐藏（a11y：焦点通道与 hover 同款语义）', async () => {
    const w = await mountTip()
    const root = w.find('.ui-tip')
    await root.trigger('focusin')
    vi.advanceTimersByTime(300)
    await flushPromises()
    expect(bubble()).not.toBeNull()
    await root.trigger('focusout')
    expect(bubble()).toBeNull()
    w.unmount()
  })

  it('Esc 隐藏已显示气泡', async () => {
    const w = await mountTip()
    await w.find('.ui-tip').trigger('mouseenter')
    vi.advanceTimersByTime(300)
    await flushPromises()
    expect(bubble()).not.toBeNull()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await flushPromises()
    expect(bubble()).toBeNull()
    w.unmount()
  })
})

describe('UiTooltip 形态（主标题 + 可选副题；定位；a11y）', () => {
  it('sub prop：气泡两行（主标题粗 + 副题）；无 sub 时单行', async () => {
    const w = await mountTip({ label: '搜索', sub: '搜索会话' })
    await w.find('.ui-tip').trigger('mouseenter')
    vi.advanceTimersByTime(300)
    await flushPromises()
    const el = bubble()!
    expect(el.querySelectorAll('.ui-tip-label')).toHaveLength(1)
    expect(el.querySelector('.ui-tip-label')!.textContent).toBe('搜索')
    expect(el.querySelector('.ui-tip-sub')!.textContent).toBe('搜索会话')
    w.unmount()

    const w2 = await mountTip({ label: '返回' })
    await w2.find('.ui-tip').trigger('mouseenter')
    vi.advanceTimersByTime(300)
    await flushPromises()
    expect(bubble()!.querySelector('.ui-tip-sub')).toBeNull()
    w2.unmount()
  })

  it('气泡 aria-hidden（语义由触发钮 aria-label 承载）；定位样式为固定像素偏移', async () => {
    const w = await mountTip()
    await w.find('.ui-tip').trigger('mouseenter')
    vi.advanceTimersByTime(300)
    await flushPromises()
    const el = bubble()!
    expect(el.getAttribute('aria-hidden')).toBe('true')
    // 触发钮下方居中：top = rect.bottom + 6（jsdom rect 全 0 → top=6px）；left 带 translateX(-50%) 钳制
    expect(el.style.top).toBe('6px')
    expect(el.style.left).not.toBe('')
    w.unmount()
  })

  it('连续 hover 多次不重复叠计时器（二次 hover 正常显隐）', async () => {
    const w = await mountTip()
    const root = w.find('.ui-tip')
    await root.trigger('mouseenter')
    vi.advanceTimersByTime(300)
    await flushPromises()
    await root.trigger('mouseleave')
    await root.trigger('mouseenter')
    vi.advanceTimersByTime(300)
    await flushPromises()
    expect(bubble()).not.toBeNull()
    await root.trigger('mouseleave')
    expect(bubble()).toBeNull()
    w.unmount()
  })
})
