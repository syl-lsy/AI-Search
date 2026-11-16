/**
 * test/component/timeline.test.ts — Timeline 步骤时间线组件层（覆盖率恢复批补测）。
 *
 * 纯投影组件：SSE stage 状态机（done/running/failed/pending 图标与副行）+ 排队占位 +
 * compact 瞬态提示 + 展开/折叠状态机（生成中默认展开、终态默认折叠、用户切换让位）。
 * 既有行为 characterization（无行为变更——pure coverage recovery）。
 */
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import Timeline from '@/components/answer/Timeline.vue'
import { i18n } from '@/i18n'
import type { TimelineStage } from '@/stores/turn'
import type { TurnStatus } from '@/types/api'

function stage(partial: Partial<TimelineStage> & Pick<TimelineStage, 'stage_key' | 'status'>): TimelineStage {
  return { ...partial }
}

function mountTimeline(stages: TimelineStage[], status: TurnStatus, showCompactNote = false) {
  return mount(Timeline, {
    props: { stages, status, showCompactNote },
    global: { plugins: [i18n] },
  })
}

const MIXED: TimelineStage[] = [
  stage({ stage_key: 'rewrite', status: 'done', label: '改写查询' }),
  stage({ stage_key: 'search', status: 'running', progress: { current: 2, total: 5 } }),
  stage({ stage_key: 'read', status: 'failed' }),
]

describe('Timeline 步骤时间线', () => {
  it('无步骤且非排队 → 整块不渲染（hasSteps false 分支）', () => {
    const w = mountTimeline([], 'completed')
    expect(w.find('[data-testid="timeline"]').exists()).toBe(false)
  })

  it('排队轮：占位节点 + 头部排队说明 + 默认展开', () => {
    const w = mountTimeline([], 'queued')
    expect(w.find('[data-testid="timeline"]').exists()).toBe(true)
    expect(w.find('[data-testid="queued-placeholder"]').exists()).toBe(true)
    expect(w.find('.tl-head').text()).toContain('排队中')
    expect(w.find('.tl-head').attributes('aria-expanded')).toBe('true')
  })

  it('生成中默认展开：混合状态图标（done ✓ / running ●+caret+进度 / pending 序号）', () => {
    const w = mountTimeline(MIXED, 'running')
    const steps = w.findAll('.tl-step')
    expect(steps).toHaveLength(3)
    // done：勾图标；running：● + caret + 进度 2/5；failed：! 与 danger 语义类
    expect(steps[0]!.find('.tl-ico svg').exists()).toBe(true)
    expect(steps[1]!.find('.tl-label .caret').exists()).toBe(true)
    expect(steps[1]!.find('.tl-sub').text()).toBe('2/5')
    expect(steps[2]!.text()).toContain('!')
    expect(steps[2]!.find('.tl-ico').classes()).toContain('failed')
    // label 缺省回落 stage_key
    expect(steps[2]!.find('.tl-label').text()).toContain('read')
    expect(w.find('.tl-head').attributes('aria-expanded')).toBe('true')
  })

  it('进度 total 缺省回落省略号；detail 副行渲染；deep 子代理 pills 双态（done ok / running run）', () => {
    const stages: TimelineStage[] = [
      stage({
        stage_key: 'plan',
        status: 'done',
        label: '检索',
        progress: { current: 3 },
        detail: '两路并行',
        extra: {
          strategy: 'web',
          subagents: [
            { subtopic: 'A 主题', status: 'done' },
            { subtopic: 'B 主题', status: 'running' },
          ],
        },
      }),
    ]
    const w = mountTimeline(stages, 'running')
    expect(w.find('.tl-sub').text()).toBe('3/…')
    expect(w.findAll('.tl-sub')[2]!.text()).toBe('两路并行')
    expect(w.findAll('.tl-sub')[1]!.text()).toContain('A 主题')
    const pills = w.findAll('.ap')
    expect(pills).toHaveLength(2)
    expect(pills[0]!.classes()).toContain('ok')
    expect(pills[1]!.classes()).toContain('run')
  })

  it('终态默认折叠（aria-expanded=false，body v-show 隐藏）；手动点击切换', async () => {
    const w = mountTimeline(MIXED, 'completed')
    expect(w.find('.tl-head').attributes('aria-expanded')).toBe('false')
    await w.find('.tl-head').trigger('click')
    expect(w.find('.tl-head').attributes('aria-expanded')).toBe('true')
    await w.find('.tl-head').trigger('click')
    expect(w.find('.tl-head').attributes('aria-expanded')).toBe('false')
  })

  it('状态迁移状态机：running→completed 自动折叠；completed→queued 自动展开（用户手动态让位）', async () => {
    const w = mountTimeline(MIXED, 'running')
    await w.setProps({ status: 'completed' })
    expect(w.find('.tl-head').attributes('aria-expanded')).toBe('false')
    await w.setProps({ status: 'queued' })
    expect(w.find('.tl-head').attributes('aria-expanded')).toBe('true')
  })

  it('compact 瞬态提示：showCompactNote 驱动渲染', () => {
    const w = mountTimeline(MIXED, 'running', true)
    expect(w.find('[data-testid="compact-note"]').exists()).toBe(true)
    expect(w.find('[data-testid="compact-note"]').text()).toContain('正在整理研究上下文')
  })
})
