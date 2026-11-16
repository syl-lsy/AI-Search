/**
 * test/component/question-hover-actions.test.ts — QuestionHoverActions 组件层
 * （REQ-055/A39 v1.18 落位 + v1.24 tooltip 接入补漏：UiTooltip 替换原生 title）。
 *
 * 断言面：两钮（编辑查询/复制查询）无原生 title、aria-label 保留、包 UiTooltip 包装层；
 * hover 300ms 气泡显主标题（ui-tooltip 同款形态）；编辑 emit 接线回归（包装不破坏既有行为）。
 */
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import QuestionHoverActions from '@/components/answer/QuestionHoverActions.vue'
import { i18n } from '@/i18n'

const QUESTION = '什么是 LangGraph 的 Send API？'

function mountActions(visible = true): VueWrapper {
  return mount(QuestionHoverActions, {
    props: { question: QUESTION, visible },
    global: { plugins: [i18n] },
    attachTo: document.body,
  })
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  document.body.innerHTML = ''
})

describe('QuestionHoverActions（v1.24：UiTooltip 替换原生 title）', () => {
  it('两钮无原生 title、aria-label 保留、包 UiTooltip 包装层', () => {
    const w = mountActions()
    for (const id of ['qact-edit', 'qact-copy']) {
      const btn = w.find(`[data-testid="${id}"]`)
      expect(btn.exists()).toBe(true)
      expect(btn.attributes('title')).toBeUndefined()
      expect(btn.attributes('aria-label')).not.toBeUndefined()
      expect((btn.element as HTMLElement).closest('.ui-tip')).not.toBeNull()
    }
  })

  it('hover 编辑钮 300ms → 气泡「编辑查询」；hover 复制钮 → 「复制查询」（leave 即隐）', async () => {
    const w = mountActions()
    const editWrap = w.find('[data-testid="qact-edit"]').element.closest('.ui-tip')!
    editWrap.dispatchEvent(new MouseEvent('mouseenter'))
    vi.advanceTimersByTime(300)
    await flushPromises()
    let bubble = document.querySelector('[data-testid="ui-tip-bubble"]')
    expect(bubble).not.toBeNull()
    expect(bubble!.textContent).toContain('编辑查询')
    editWrap.dispatchEvent(new MouseEvent('mouseleave'))
    await flushPromises()
    expect(document.querySelector('[data-testid="ui-tip-bubble"]')).toBeNull()

    const copyWrap = w.find('[data-testid="qact-copy"]').element.closest('.ui-tip')!
    copyWrap.dispatchEvent(new MouseEvent('mouseenter'))
    vi.advanceTimersByTime(300)
    await flushPromises()
    bubble = document.querySelector('[data-testid="ui-tip-bubble"]')
    expect(bubble).not.toBeNull()
    expect(bubble!.textContent).toContain('复制查询')
  })

  it('编辑钮点击 emit edit 携带问题原文（回归：tooltip 包装不破坏既有接线）', async () => {
    const w = mountActions()
    await w.find('[data-testid="qact-edit"]').trigger('click')
    expect(w.emitted('edit')).toEqual([[QUESTION]])
  })
})
