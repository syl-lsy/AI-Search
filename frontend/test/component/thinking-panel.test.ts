/**
 * test/component/thinking-panel.test.ts — ThinkingPanel 组件层（§3 thinking 行 / §5）。
 * 默认折叠渲染、可展开；空内容不渲染（无空区块原则）；流式展开态带光标。
 */
import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import ThinkingPanel from '@/components/markdown/ThinkingPanel.vue'
import { i18n } from '@/i18n'

function mountPanel(markdown: string, streaming: boolean) {
  return mount(ThinkingPanel, {
    props: { markdown, streaming },
    global: { plugins: [i18n] },
  })
}

describe('ThinkingPanel（组件层）', () => {
  it('空内容不渲染（无空区块原则）', () => {
    const w = mountPanel('', true)
    expect(w.find('[data-testid="thinking-panel"]').exists()).toBe(false)
  })

  it('默认折叠：toggle 存在但 body 隐藏（v-show display:none）；aria-expanded=false', () => {
    const w = mountPanel('先比较事件模型…', true)
    const toggle = w.find('[data-testid="thinking-toggle"]')
    expect(toggle.exists()).toBe(true)
    expect(toggle.attributes('aria-expanded')).toBe('false')
    // v-show 内联 display:none（isVisible 语义受 test-utils 版本影响，直接断言内联样式）
    expect(w.find('[data-testid="thinking-body"]').attributes('style')).toContain('display: none')
  })

  it('点击展开：body 展示 + aria-expanded=true；再点收起', async () => {
    const w = mountPanel('思考内容', false)
    await w.find('[data-testid="thinking-toggle"]').trigger('click')
    expect(w.find('[data-testid="thinking-body"]').attributes('style') ?? '').not.toContain('display: none')
    expect(w.find('[data-testid="thinking-toggle"]').attributes('aria-expanded')).toBe('true')
    expect(w.text()).toContain('思考内容')
    await w.find('[data-testid="thinking-toggle"]').trigger('click')
    expect(w.find('[data-testid="thinking-body"]').attributes('style')).toContain('display: none')
  })

  it('流式进行中头部带打字光标（streaming=true），回看/静止不带', () => {
    const w = mountPanel('内容', true)
    expect(w.find('.caret').exists()).toBe(true)
    const w2 = mountPanel('内容', false)
    expect(w2.find('.caret').exists()).toBe(false)
  })

  it('thinking 通道不解析 [n]（§3：不参与引用解析）', () => {
    const w = mountPanel('引用 [1] 待对齐', false)
    expect(w.find('sup.cite').exists()).toBe(false)
    expect(w.text()).toContain('[1]')
  })

  it('折叠态跳过重渲（streaming-buffer 规则 17 面：v-show 隐藏不可见不烧渲染）；展开后按最新源渲染', async () => {
    vi.useFakeTimers()
    const w = mountPanel('初始思考', true)
    // 初渲一次（DOM 恒存在，v-show 仅隐藏）
    expect(w.find('[data-testid="thinking-body"]').text()).toContain('初始思考')
    await w.setProps({ markdown: '初始思考 · 流式增量第二段' })
    await vi.advanceTimersByTimeAsync(200) // 限频窗（70ms）已过
    expect(w.find('[data-testid="thinking-body"]').text()).not.toContain('第二段') // 折叠态不重渲
    await w.find('[data-testid="thinking-toggle"]').trigger('click')
    expect(w.find('[data-testid="thinking-body"]').text()).toContain('第二段') // 展开取最新源渲染
    vi.useRealTimers()
  })

  it('展开态：流式更新按限频跟随渲染（70ms 一批，尾值落地）', async () => {
    vi.useFakeTimers()
    const w = mountPanel('初始', true)
    await w.find('[data-testid="thinking-toggle"]').trigger('click')
    await w.setProps({ markdown: '初始 · 更新段' })
    await vi.advanceTimersByTimeAsync(200)
    expect(w.find('[data-testid="thinking-body"]').text()).toContain('更新段')
    vi.useRealTimers()
  })
})
