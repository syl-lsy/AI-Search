/**
 * test/component/outline-anchor.test.ts — OutlineAnchor 组件层（REQ-044 / A27）。
 * 渲染条件 + a11y 硬性（button 语义 / Tab 可达 / aria-current 标激活项）+ <720px 隐藏
 * （useMediaQuery 经 window.matchMedia stub 驱动）。
 */
import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import OutlineAnchor from '@/components/answer/OutlineAnchor.vue'
import type { OutlineItem } from '@/components/answer/OutlineAnchor.vue'
import { i18n } from '@/i18n'

const ITEMS: OutlineItem[] = [
  { id: 'turn-a-q', label: 'LangGraph 和 deepagents 是什么关系？' },
  { id: 'turn-a-h-0', label: '分层关系' },
  { id: 'turn-a-h-1', label: '如何选择' },
]

function mountAnchor(props: { items?: OutlineItem[]; activeId: string | null; visible: boolean }) {
  return mount(OutlineAnchor, {
    props: { items: ITEMS, ...props },
    global: { plugins: [i18n] },
  })
}

let matchMediaMatches = false

beforeEach(() => {
  matchMediaMatches = false
  // jsdom 无布局：matchMedia 以可控 stub 驱动 <720px 行为（A27 窄屏隐藏判定）
  vi.stubGlobal(
    'matchMedia',
    vi.fn().mockImplementation((query: string) => ({
      matches: query.includes('max-width: 720px') ? matchMediaMatches : false,
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
    })),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('OutlineAnchor（REQ-044 / A27）', () => {
  it('visible=false 不渲染（无空区块）', () => {
    const w = mountAnchor({ activeId: null, visible: false })
    expect(w.find('[data-testid="outline-anchor"]').exists()).toBe(false)
  })

  it('items 为空不渲染', () => {
    const w = mountAnchor({ items: [], activeId: null, visible: true })
    expect(w.find('[data-testid="outline-anchor"]').exists()).toBe(false)
  })

  it('visible=true 渲染收起态胶囊：条目为 button 语义（Tab 可达的原生按钮）', () => {
    const w = mountAnchor({ activeId: null, visible: true })
    const nav = w.find('[data-testid="outline-anchor"]')
    expect(nav.exists()).toBe(true)
    const buttons = nav.findAll('button')
    expect(buttons).toHaveLength(3)
    expect(buttons.every((b) => b.attributes('type') === 'button')).toBe(true)
    expect(nav.attributes('aria-label')).toBe('对话内容导航')
  })

  it('激活项携带 aria-current（a11y 硬性）', () => {
    const w = mountAnchor({ activeId: 'turn-a-h-0', visible: true })
    const buttons = w.findAll('.toc-item')
    expect(buttons[1]?.attributes('aria-current')).toBe('true')
    expect(buttons[1]?.classes()).toContain('active')
    expect(buttons[0]?.attributes('aria-current')).toBeUndefined()
  })

  it('点击条目 → select(id) 上抛（父级平滑滚动定位）', async () => {
    const w = mountAnchor({ activeId: null, visible: true })
    await w.find('[data-testid="outline-item-turn-a-h-1"]').trigger('click')
    expect(w.emitted('select')).toEqual([['turn-a-h-1']])
  })

  it('<720px 窄屏隐藏（REQ-044；与 REQ-045 箭头保留策略不同）', async () => {
    matchMediaMatches = true
    const w = mountAnchor({ activeId: null, visible: true })
    await w.vm.$nextTick()
    expect(w.find('[data-testid="outline-anchor"]').exists()).toBe(false)
  })
})
