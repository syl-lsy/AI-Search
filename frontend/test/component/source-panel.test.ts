/**
 * test/component/source-panel.test.ts — SourcePanel 组件层（§5 SourcePanel/SourceCard 行）。
 * 【网络】/【知识库】徽标 = source_type；「收集中」徽标 = collecting；快照胶囊条件渲染与
 * snapshot 事件上抛；卡片 data-source-card = ref_no（[n] 引用联动锚点，REQ-003）。
 */
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import SourcePanel from '@/components/answer/SourcePanel.vue'
import { i18n } from '@/i18n'
import type { Source } from '@/types/api'

function makeSource(partial: Partial<Source> & { source_id: string; ref_no: number }): Source {
  return {
    source_type: 'web',
    title: 'LangGraph docs',
    url: `https://docs.example.com/${partial.ref_no}`,
    snippet: 'Send lets you fan out…',
    score: 0.9,
    snapshot_available: false,
    kb_doc_id: null,
    ...partial,
  }
}

function mountPanel(sources: Source[], collecting: boolean) {
  return mount(SourcePanel, {
    props: { sources, collecting },
    global: { plugins: [i18n] },
  })
}

describe('SourcePanel（§5）', () => {
  it('空来源渲染空面板骨架（head 计数 0，无卡片）', () => {
    const w = mountPanel([], false)
    expect(w.find('[data-testid="source-panel"]').exists()).toBe(true)
    expect(w.find('.count').text()).toBe('0')
    expect(w.findAll('.src-card')).toHaveLength(0)
  })

  it('「收集中」徽标随 collecting 显隐（§3 sources 行）', () => {
    const on = mountPanel([makeSource({ source_id: 'a', ref_no: 1 })], true)
    expect(on.text()).toContain('收集中')
    const off = mountPanel([makeSource({ source_id: 'a', ref_no: 1 })], false)
    expect(off.text()).not.toContain('收集中')
  })

  it('【网络】/【知识库】徽标按 source_type 区分（REQ-021）', () => {
    const w = mountPanel(
      [
        makeSource({ source_id: 'a', ref_no: 1, source_type: 'web' }),
        makeSource({ source_id: 'b', ref_no: 2, source_type: 'kb', url: null, kb_doc_id: '9a770003-0000-4000-8000-000000000003' }),
      ],
      false,
    )
    const cards = w.findAll('.src-card')
    expect(cards[0]?.find('.badge-kb').exists()).toBe(false)
    expect(cards[0]?.text()).toContain('网络')
    expect(cards[1]?.find('.badge-kb').exists()).toBe(true)
    expect(cards[1]?.text()).toContain('知识库')
  })

  it('快照胶囊仅在 snapshot_available 时渲染；点击上抛 snapshot(source_id)（api.md §5.1）', async () => {
    const w = mountPanel(
      [
        makeSource({ source_id: 'plain', ref_no: 1, snapshot_available: false }),
        makeSource({ source_id: 'snap', ref_no: 2, snapshot_available: true }),
      ],
      false,
    )
    expect(w.find('[data-testid="snapshot-1"]').exists()).toBe(false)
    await w.find('[data-testid="snapshot-2"]').trigger('click')
    expect(w.emitted('snapshot')).toEqual([['snap']])
  })

  it('卡片携带 data-source-card=ref_no（[n] 引用联动定位锚点）', () => {
    const w = mountPanel([makeSource({ source_id: 'a', ref_no: 3 })], false)
    expect(w.find('[data-source-card="3"]').exists()).toBe(true)
  })

  it('面板折叠切换（aria-expanded）', async () => {
    const w = mountPanel([], false)
    const head = w.find('.src-panel-head')
    expect(head.attributes('aria-expanded')).toBe('true')
    await head.trigger('click')
    expect(head.attributes('aria-expanded')).toBe('false')
  })
})
