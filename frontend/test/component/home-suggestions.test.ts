/**
 * test/component/home-suggestions.test.ts — HomeSuggestions 组件层（REQ-047 / 修复项⑦，
 * 对标图 3 形态：输入「你知道」出现历史问题下拉）。
 *
 * 形态契约（docs/design/设计说明.md §8.1 + 图 3）：输入卡正下方、与卡同宽的浮动面板；
 * 行式列表 icon + 文本单行省略；热门条目 i-search / 历史条目 i-clock；
 * a11y：role=listbox（aria-label=home.suggestLabel）+ 条目 button role=option aria-selected
 * + 空态 role=status（home.suggestEmpty）。
 */
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import HomeSuggestions from '@/components/search/HomeSuggestions.vue'
import { i18n } from '@/i18n'

const HOT = ['热门问题一', '热门问题二', '热门问题三']
const HISTORY = ['LangGraph 的 Send API 和 Router 的区别？', 'RAG 检索优化手段有哪些？']

function mountPanel(props: Partial<{ hotPool: string[]; history: string[]; open: boolean }> = {}) {
  return mount(HomeSuggestions, {
    props: {
      hotPool: props.hotPool ?? HOT,
      history: props.history ?? [],
      open: props.open ?? true,
    },
    global: { plugins: [i18n] },
  })
}

describe('HomeSuggestions（REQ-047 输入推荐面板）', () => {
  it('open=false 不渲染（无空区块）', () => {
    const w = mountPanel({ open: false })
    expect(w.find('[data-testid="home-suggestions"]').exists()).toBe(false)
  })

  it('热门池渲染：listbox 语义 + aria-label + 条目 i-search 图标 + 单行文本', () => {
    const w = mountPanel()
    const panel = w.find('[data-testid="home-suggestions"]')
    expect(panel.exists()).toBe(true)
    expect(panel.attributes('role')).toBe('listbox')
    expect(panel.attributes('aria-label')).toBe('推荐查询')
    const options = panel.findAll('[role="option"]')
    expect(options).toHaveLength(3)
    expect(options[0]?.text()).toBe('热门问题一')
    expect(options[0]?.attributes('aria-selected')).toBe('false')
    // 热门条目图标 = i-search
    expect(options[0]?.find('use').attributes('href')).toBe('#i-search')
    // 单行省略形态类
    expect(options[0]?.classes()).toContain('suggest-row')
  })

  it('历史匹配渲染：条目 i-clock 图标（历史语义与热门区分）', () => {
    const w = mountPanel({ hotPool: [], history: HISTORY })
    const options = w.findAll('[role="option"]')
    expect(options).toHaveLength(2)
    expect(options[0]?.text()).toBe(HISTORY[0])
    expect(options[0]?.find('use').attributes('href')).toBe('#i-clock')
  })

  it('空态：无任何条目时 role=status 空态文案（home.suggestEmpty）', () => {
    const w = mountPanel({ hotPool: [], history: [] })
    const empty = w.find('[role="status"]')
    expect(empty.exists()).toBe(true)
    expect(empty.text()).toBe('无匹配历史')
    expect(w.findAll('[role="option"]')).toHaveLength(0)
  })

  it('点击条目 → select(text) 上抛（文本本体，非索引）', async () => {
    const w = mountPanel({ hotPool: [], history: HISTORY })
    await w.findAll('[role="option"]')[1]!.trigger('click')
    expect(w.emitted('select')).toEqual([[HISTORY[1]]])
  })
})
