/**
 * test/component/answer-renderer.test.ts — AnswerRenderer 组件层（渲染 + 事件上抛）。
 * 纯渲染逻辑（净化/[n]/标题 id）归 unit/markdown-render.test.ts；本层断言装配行为：
 * v-html 注入、cite 点击/键盘上抛、标题 id 生成（renderer.ts 契约保留）。
 * 【2026-11-14 修复项③】headings 上抛事件已移除（锚点只保留用户问题）——标题 id 生成
 * 本身保留（锚点 scrollIntoView 定位目标），组件不再向父级上抛标题集。
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

  it('标题 id 生成保留（锚点定位目标）；headings 事件不再上抛（修复项③）', () => {
    const w = mount(AnswerRenderer, {
      props: { markdown: '## 分层关系\n\n正文', headingPrefix: 't9' },
    })
    expect(w.find('h2').attributes('id')).toBe('t9-h-0')
    expect(w.emitted('headings')).toBeUndefined()
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

  it('markdown 增量更新（流式 token 追加）→ 内容跟随刷新且无 headings 上抛', async () => {
    const w = mount(AnswerRenderer, {
      props: { markdown: '第一段', headingPrefix: 't1' },
    })
    await w.setProps({ markdown: '第一段\n\n## 新章节' })
    expect(w.text()).toContain('第一段')
    expect(w.text()).toContain('新章节')
    expect(w.find('h2').attributes('id')).toBe('t1-h-0')
    expect(w.emitted('headings')).toBeUndefined()
  })
})
