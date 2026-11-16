/**
 * test/component/followup-chips.test.ts — FollowupChips 组件层（REQ-046/A29 前端面落位，
 * frontend-api.md §5 v1.9 行 + 设计说明 §7 行列表形态）。
 *
 * 展示条件 = followups 非空（无空态渲染）；≤5 条；渲染前过滤空串/超长条目（>200 字符，
 * 防御性兜底——后端 schema_guard 已校验，前端双保险非重复校验契约）；行为 = 点击 chip
 * 以该文本走既有追问流（emit follow → AnswerView followUp）；a11y = button + aria-label
 * （完整文本）+ 容器 aria-label（i18n answer.followups）。
 */
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import FollowupChips from '@/components/answer/FollowupChips.vue'
import { i18n } from '@/i18n'

function mountChips(items: string[]) {
  return mount(FollowupChips, { props: { items }, global: { plugins: [i18n] } })
}

describe('FollowupChips（REQ-046：短答下方追问建议行列表）', () => {
  it('容器 aria-label（i18n answer.followups）；每条为 button 语义 + aria-label 完整文本', () => {
    const w = mountChips(['追问一', '这是一条相对长一些的追问建议文本'])
    expect(w.find('[data-testid="followup-chips"]').attributes('aria-label')).toBe('后续提问建议')
    const buttons = w.findAll('button')
    expect(buttons).toHaveLength(2)
    expect(buttons[0]!.attributes('aria-label')).toBe('追问一')
    expect(buttons[0]!.text()).toBe('追问一')
    expect(buttons[1]!.attributes('aria-label')).toBe('这是一条相对长一些的追问建议文本')
  })

  it('点击 chip：emit follow 携带该条文本（AnswerView 走既有 followUp 追问流）', async () => {
    const w = mountChips(['Send API 的失败分支怎么处理？'])
    await w.findAll('button')[0]!.trigger('click')
    expect(w.emitted('follow')).toEqual([['Send API 的失败分支怎么处理？']])
  })

  it('≤5 条截断（契约 items 1~5；防御性截断多余条目）', () => {
    const w = mountChips(['一', '二', '三', '四', '五', '六', '七'])
    expect(w.findAll('button')).toHaveLength(5)
    expect(w.text()).not.toContain('六')
  })

  it('渲染前过滤空串与超长条目（>200 字符防御性兜底）', () => {
    const long = 'x'.repeat(201)
    const w = mountChips(['', '   ', long, '合法追问'])
    const buttons = w.findAll('button')
    expect(buttons).toHaveLength(1)
    expect(buttons[0]!.text()).toBe('合法追问')
  })

  it('空数组 / 全部被过滤 → 零渲染（无空区块、无空态）', () => {
    expect(mountChips([]).find('[data-testid="followup-chips"]').exists()).toBe(false)
    expect(mountChips(['', '   ']).find('[data-testid="followup-chips"]').exists()).toBe(false)
  })

  it('行列表形态（设计说明 §7）：行内放大镜图标 + 单行短语文本；图标 aria-hidden', () => {
    const w = mountChips(['怎么处理失败分支？'])
    const row = w.find('button')
    expect(row.find('use[href="#i-search"]').exists()).toBe(true)
    expect(row.find('.icon[aria-hidden="true"], .icon-sm[aria-hidden="true"]').exists()).toBe(true)
    expect(row.classes()).toContain('fu-item')
  })

  it('i18n 双语键齐备（answer.followups 容器标签）', () => {
    expect(i18n.global.t('answer.followups')).toBe('后续提问建议')
    i18n.global.locale.value = 'en-US'
    expect(i18n.global.t('answer.followups')).toBe('Suggested follow-ups')
    i18n.global.locale.value = 'zh-CN'
  })
})
