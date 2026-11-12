/**
 * test/component/turn-block.test.ts — TurnBlock 组件层（评审 P1 修复锁死：DegradedBanner）。
 *
 * 契约口径：api.md §2.5 degraded/degraded_reason + frontend-api.md §3 done 行（finish=degraded →
 * DegradedBanner「研究未完成」显著标注）/ §5 组件清单——降级终态轮与 completed 轮严格可区分，
 * 数据源 = turn.degradedReason（SSE done + 2.5 对账双源，stores/turn.ts）。
 */
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import TurnBlock from '@/components/answer/TurnBlock.vue'
import { i18n } from '@/i18n'
import type { TurnState } from '@/stores/turn'
import type { DegradedReason } from '@/types/api'

function makeTurn(partial: Partial<TurnState>): TurnState {
  return {
    turnId: 'c31b9d74-2a41-4f6e-b3c2-5d6e7f809a1b',
    threadId: '7d2f0a1e-1c3b-4e5a-9d8f-0a1b2c3d4e5f',
    mode: 'fast',
    question: '测试问题',
    status: 'completed',
    answer_md: '',
    thinking_md: '',
    sources: [],
    images: [],
    stages: [],
    memory_hits: null,
    compactSeq: 0,
    compactAt: null,
    incognito: false,
    cancelReason: null,
    followups: null,
    lastSeq: 9,
    errorMsg: null,
    degradedReason: null,
    memorySaved: null,
    durationMs: null,
    ...partial,
  }
}

function mountTurn(turn: TurnState) {
  return mount(TurnBlock, {
    props: { turn },
    global: { plugins: [i18n] },
  })
}

describe('TurnBlock：DegradedBanner（评审 P1——降级终态与 completed 严格可区分）', () => {
  it('completed 轮：无 banner、状态行绿点（dot-success）+「已完成」', () => {
    const w = mountTurn(makeTurn({ answer_md: '正常答案' }))
    expect(w.find('[data-testid="degraded-banner"]').exists()).toBe(false)
    expect(w.find('[data-testid="turn-status"] .dot-success').exists()).toBe(true)
    expect(w.find('[data-testid="turn-status"]').text()).toContain('已完成')
    expect(w.find('[data-testid="turn-status"]').text()).not.toContain('降级')
  })

  it('degraded 轮（timeout）：warning 点 + 答案区顶部显著 banner（研究未完成 + 原因分立文案）', () => {
    const w = mountTurn(
      makeTurn({ status: 'degraded', degradedReason: 'timeout', answer_md: '部分答案' }),
    )
    // 状态行：warning 点 + 「研究未完成（降级）」，不再显示「已完成」
    expect(w.find('[data-testid="turn-status"] .dot-warning').exists()).toBe(true)
    expect(w.find('[data-testid="turn-status"] .dot-success').exists()).toBe(false)
    expect(w.find('[data-testid="turn-status"]').text()).toContain('研究未完成（降级）')
    expect(w.find('[data-testid="turn-status"]').text()).not.toContain('已完成')
    // 答案区顶部显著 banner：主文案 + timeout 原因
    const banner = w.find('[data-testid="degraded-banner"]')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('研究未完成')
    expect(banner.text()).toContain('超时兜底')
    // banner 位置 = 答案区顶部（DOM 顺序在答案容器之前）
    const answer = w.find('[data-testid="turn-answer"]')
    expect(answer.exists()).toBe(true)
    expect(banner.element.nextElementSibling?.getAttribute('data-testid')).toBe('turn-answer')
  })

  it('degraded 三枚举 reason 分立文案（budget / source_failure）', () => {
    const reasons: DegradedReason[] = ['budget', 'source_failure']
    for (const reason of reasons) {
      const w = mountTurn(makeTurn({ status: 'degraded', degradedReason: reason }))
      expect(w.find('[data-testid="degraded-banner"]').text()).toContain('原因：')
    }
    // i18n 键完整性：三枚举均有词条（防漏键渲染裸 key）
    expect(i18n.global.t('answer.degradedReason.timeout')).toContain('超时')
    expect(i18n.global.t('answer.degradedReason.budget')).toContain('预算')
    expect(i18n.global.t('answer.degradedReason.source_failure')).toContain('熔断')
  })

  it('en 界面下 banner 随语言切换（zh/en 双包同步）', () => {
    i18n.global.locale.value = 'en-US'
    const w = mountTurn(makeTurn({ status: 'degraded', degradedReason: 'budget' }))
    expect(w.find('[data-testid="degraded-banner"]').text()).toContain('Research incomplete')
    i18n.global.locale.value = 'zh-CN'
  })
})
