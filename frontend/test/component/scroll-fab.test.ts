/**
 * test/component/scroll-fab.test.ts — ScrollToBottomFab 组件层（REQ-045 / A28）。
 * 显隐 + aria-label（i18n answer.scrollToBottom）+ 点击上抛（恢复途径②入口）+ 淡入淡出过渡载体。
 */
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ScrollToBottomFab from '@/components/answer/ScrollToBottomFab.vue'
import { i18n } from '@/i18n'

function mountFab(visible: boolean) {
  return mount(ScrollToBottomFab, {
    props: { visible },
    global: { plugins: [i18n] },
  })
}

describe('ScrollToBottomFab（REQ-045 / A28）', () => {
  it('visible=false 不渲染（贴底淡出后离场）', () => {
    const w = mountFab(false)
    expect(w.find('[data-testid="scroll-fab"]').exists()).toBe(false)
  })

  it('visible=true 渲染：圆钮形态类 + aria-label「滚动到底部」+ 下箭头图标', () => {
    const w = mountFab(true)
    const fab = w.find('[data-testid="scroll-fab"]')
    expect(fab.exists()).toBe(true)
    expect(fab.attributes('aria-label')).toBe('滚动到底部')
    expect(fab.attributes('title')).toBe('滚动到底部')
    expect(fab.classes()).toContain('scroll-fab')
    expect(fab.find('use').attributes('href')).toBe('#i-arrow-down')
  })

  it('点击 → click 上抛（父级接 AnswerScrollController.resumeViaFab）', async () => {
    const w = mountFab(true)
    await w.find('[data-testid="scroll-fab"]').trigger('click')
    expect(w.emitted('click')).toHaveLength(1)
  })

  it('显隐切换经 Transition 载体（淡入淡出 = --duration-base）', async () => {
    const w = mountFab(false)
    await w.setProps({ visible: true })
    expect(w.find('[data-testid="scroll-fab"]').exists()).toBe(true)
    await w.setProps({ visible: false })
    expect(w.find('[data-testid="scroll-fab"]').exists()).toBe(false)
  })

  it('en 界面 aria-label 切换为 Scroll to bottom（i18n 响应式，A28 随界面语言）', async () => {
    const w = mountFab(true)
    i18n.global.locale.value = 'en-US'
    await w.vm.$nextTick()
    expect(w.find('[data-testid="scroll-fab"]').attributes('aria-label')).toBe('Scroll to bottom')
    i18n.global.locale.value = 'zh-CN'
    await w.vm.$nextTick()
    expect(w.find('[data-testid="scroll-fab"]').attributes('aria-label')).toBe('滚动到底部')
  })
})
