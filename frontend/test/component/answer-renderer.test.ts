/**
 * test/component/answer-renderer.test.ts — AnswerRenderer 组件层（渲染 + 事件上抛）。
 * 纯渲染逻辑（净化/[n]/标题 id）归 unit/markdown-render.test.ts；本层断言装配行为：
 * v-html 注入、cite 点击/键盘上抛、headings 事件（OutlineAnchor 数据源）。
 */
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import AnswerRenderer from '@/components/markdown/AnswerRenderer.vue'

describe('AnswerRenderer（组件层）', () => {
  it('渲染 markdown 内容（v-html 注入）', () => {
    const w = mount(AnswerRenderer, { props: { markdown: '# 大标题\n\n正文段落', headingPrefix: 't0' } })
    expect(w.find('[data-testid="answer-renderer"]').exists()).toBe(true)
    expect(w.find('h1').text()).toBe('大标题')
    expect(w.text()).toContain('正文段落')
  })

  it('headings 事件上抛（immediate）——OutlineAnchor 数据源（标题 id 机制）', () => {
    const w = mount(AnswerRenderer, {
      props: { markdown: '## 分层关系\n\n正文', headingPrefix: 't9' },
    })
    const emitted = w.emitted('headings')
    expect(emitted).toHaveLength(1)
    const items = emitted![0]![0] as { id: string; text: string }[]
    expect(items).toHaveLength(1)
    expect(items[0]).toMatchObject({ id: 't9-h-0', text: '分层关系' })
    expect(w.find('h2').attributes('id')).toBe('t9-h-0')
  })

  it('点击 sup.cite → cite(refNo) 上抛（[n] 与 sources 联动入口）', async () => {
    const w = mount(AnswerRenderer, {
      props: { markdown: '动态扇出 [3]。', headingPrefix: 't0' },
    })
    await w.find('sup.cite').trigger('click')
    expect(w.emitted('cite')).toEqual([[3]])
  })

  it('键盘 Enter 于 sup.cite → cite 上抛（tabindex+role=button 可达）', async () => {
    const w = mount(AnswerRenderer, {
      props: { markdown: '见 [2]。', headingPrefix: 't0' },
    })
    const sup = w.find('sup.cite')
    expect(sup.attributes('tabindex')).toBe('0')
    await sup.trigger('keydown', { key: 'Enter' })
    expect(w.emitted('cite')).toEqual([[2]])
    await sup.trigger('keydown', { key: ' ' })
    expect(w.emitted('cite')).toHaveLength(2)
  })

  it('markdown 增量更新（流式 token 追加）→ 内容与 headings 跟随刷新', async () => {
    const w = mount(AnswerRenderer, {
      props: { markdown: '第一段', headingPrefix: 't1' },
    })
    await w.setProps({ markdown: '第一段\n\n## 新章节' })
    expect(w.text()).toContain('第一段')
    expect(w.emitted('headings')!.length).toBeGreaterThanOrEqual(2)
    const latest = w.emitted('headings')!.at(-1)![0] as { text: string }[]
    expect(latest[0]?.text).toBe('新章节')
  })
})
