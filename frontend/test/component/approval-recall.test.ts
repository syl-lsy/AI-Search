/**
 * test/component/approval-recall.test.ts — ApprovalRecall 审批回看折叠条（B22 批次 3 第三段：
 * 回看态——用户裁决轮一行折叠条默认收起、不进导出〔OQ-04〕；web 直达轮（no_hit）与 shadow 轮
 * 不展示任何审批痕迹；frontend-api.md §5 ApprovalSheet 行回看态句 + 设计稿 06-approval-sheet.html
 * 补节基准〔折叠条 strip + 展开预览/note，D8 提案文案不收编——note 前缀走实施新增键随批上报〕）。
 *
 * 数据源 = turn.approval（approved 形态）+ approvalPreviews（流式域缓存——2.5 approved 无 previews
 * 键）+ awaiting_ms（等待时长展示，实施新增键随批上报）；挂载点 = TurnBlock 问题下方（draft 补节）。
 */
import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import { i18n } from '@/i18n'
import { approvalPreviewFixtures, TURN_ID_AWAITING } from '@/mocks/turnDetails'
import ApprovalRecall from '@/components/answer/ApprovalRecall.vue'
import type { TurnState } from '@/stores/turn'

function makeTurn(overrides: Partial<TurnState> = {}): TurnState {
  return {
    turnId: TURN_ID_AWAITING,
    threadId: '7d2f0a1e-1c3b-4e5a-9d8f-0a1b2c3d4e5f',
    question: 'KB 里怎么融合检索？',
    mode: 'fast',
    status: 'running',
    answer_md: '',
    thinking_md: '',
    sources: [],
    images: [],
    stages: [],
    errorMsg: null,
    errorCode: null,
    refusalPendingClose: false,
    degradedReason: null,
    memorySaved: null,
    memory_hits: null,
    compactSeq: 0,
    compactAt: null,
    incognito: false,
    cancelReason: null,
    followups: null,
    lastSeq: 0,
    feedbackHelpful: null,
    durationMs: null,
    exportAvailable: false,
    approval: null,
    approvalPreviews: null,
    awaitingMs: null,
    rewrittenQueries: null,
    ...overrides,
  }
}

function mountRecall(turn: TurnState) {
  const w = mount(ApprovalRecall, { props: { turn }, global: { plugins: [i18n] } })
  cw = w
  return w
}

/** 当前测试 wrapper（strip()/body() 无参调用落点——mountRecall 赋值） */
let cw: ReturnType<typeof mountRecall> | null = null
const strip = () => cw!.find('[data-testid="approval-recall-strip"]')
const body = () => cw!.find('[data-testid="approval-recall-body"]')

describe('可见性（approved 形态唯一——web 直达/shadow/pending/canceled 零审批痕迹）', () => {
  it('approved 形态：折叠条渲染（问题下方一行，默认收起）', () => {
    const w = mountRecall(makeTurn({ approval: { status: 'approved', choice: 'kb_only', decided_by: 'user' }, approvalPreviews: [...approvalPreviewFixtures] }))
    expect(w.find('[data-testid="approval-recall"]').exists()).toBe(true)
    expect(strip().exists()).toBe(true)
    expect(body().exists()).toBe(false) // 默认收起
    w.unmount()
  })

  it('no_hit（web 直达轮）：整块不渲染——用户当时没被问过，事后提示只会制造困惑（OQ-04/契约回看态句）', () => {
    const w = mountRecall(makeTurn({ approval: { status: 'no_hit', verdict: 'no_hit' } }))
    expect(w.find('[data-testid="approval-recall"]').exists()).toBe(false)
    w.unmount()
  })

  it('shadow 观测态：整块不渲染（shadow 行为一律现状、零弹层零折叠条）', () => {
    const w = mountRecall(makeTurn({ approval: { status: 'shadow', verdict: 'has_hit' } }))
    expect(w.find('[data-testid="approval-recall"]').exists()).toBe(false)
    w.unmount()
  })

  it('pending（awaiting 在途）与 approval=null（非门轮/canceled 键省略）：整块不渲染', () => {
    const w1 = mountRecall(makeTurn({ status: 'awaiting_approval', approval: { status: 'pending', verdict: 'has_hit', expires_at: '2026-11-01T08:14:06Z', previews: [...approvalPreviewFixtures] } }))
    expect(w1.find('[data-testid="approval-recall"]').exists()).toBe(false)
    w1.unmount()
    const w2 = mountRecall(makeTurn({ approval: null }))
    expect(w2.find('[data-testid="approval-recall"]').exists()).toBe(false)
    w2.unmount()
  })

  it('timeout 收口轮（decided_by=timeout，choice=both）同为已裁决轮：折叠条渲染', () => {
    const w = mountRecall(makeTurn({ approval: { status: 'approved', choice: 'both', decided_by: 'timeout' } }))
    expect(strip().exists()).toBe(true)
    expect(strip().text()).toContain('两者都要')
    w.unmount()
  })

  it('canceled 轮防御对称化（批次 3 残留 P3 清偿）：审批通过后轮又被取消的内存窗口（done(finish=canceled) 不触碰 approval、陈旧 approved 对象留存至 reconcile 投影 null）整块不渲染——轮级 status 与 approval.status 双维判定，与弹层可见性派生同构', () => {
    const w = mountRecall(makeTurn({
      status: 'canceled',
      cancelReason: 'user',
      approval: { status: 'approved', choice: 'both', decided_by: 'user' },
      approvalPreviews: [...approvalPreviewFixtures],
    }))
    expect(w.find('[data-testid="approval-recall"]').exists()).toBe(false)
    w.unmount()
  })

  it('防御排除仅限 canceled：completed/degraded 终态已裁决轮折叠条照常渲染（审批通过后正常答完的回看面不误伤）', () => {
    const w1 = mountRecall(makeTurn({ status: 'completed', approval: { status: 'approved', choice: 'kb_only', decided_by: 'user' } }))
    expect(w1.find('[data-testid="approval-recall"]').exists()).toBe(true)
    w1.unmount()
    const w2 = mountRecall(makeTurn({ status: 'degraded', approval: { status: 'approved', choice: 'web_only', decided_by: 'timeout' } }))
    expect(w2.find('[data-testid="approval-recall"]').exists()).toBe(true)
    w2.unmount()
  })
})

describe('折叠条文案与展开（choice 复用四选项词条；预览/note/等待时长回显）', () => {
  it('条文案 = 已按『{choice}』回答 · 查看当时的知识库命中（有 previews 形态维持契约钉字；choice 复用四选项词条，choice→词条映射四值全覆盖）', async () => {
    for (const [choice, label] of [
      ['kb_only', '仅知识库'],
      ['web_only', '仅网络'],
      ['both', '两者都要'],
      ['custom', '自定义'],
    ] as const) {
      const w = mountRecall(makeTurn({ approval: { status: 'approved', choice, decided_by: 'user' }, approvalPreviews: [...approvalPreviewFixtures] }))
      expect(strip().text()).toContain(`已按『${label}』回答`)
      expect(strip().text()).toContain('查看当时的知识库命中')
      w.unmount()
    }
  })

  it('展开：预览逐条文本插值直渲（类标签保真、零元素节点注入、无链接无按钮）+ note 回显（补充指令前缀）+ 等待时长行（awaiting_ms）', async () => {
    const w = mountRecall(makeTurn({
      approval: { status: 'approved', choice: 'kb_only', note: '优先引用 ADR-017 结论', decided_by: 'user' },
      approvalPreviews: [...approvalPreviewFixtures],
      awaitingMs: 9200,
    }))
    await strip().trigger('click')
    await flushPromises()
    expect(body().exists()).toBe(true)
    expect(strip().attributes('aria-expanded')).toBe('true')
    // 预览直渲：title/heading_path/excerpt 逐条
    const pvTitles = w.findAll('[data-testid="approval-recall-pv-title"]')
    expect(pvTitles.length).toBe(2)
    expect(pvTitles[0]!.text()).toBe('内部设计文档-检索融合.md')
    expect(w.find('[data-testid="approval-recall-pv-excerpt"]').text()).toContain('统一 rerank')
    // 类标签保真：第二条 excerpt 含 <code> 原文（文本插值——无元素节点注入）
    const excerpts = w.findAll('[data-testid="approval-recall-pv-excerpt"]')
    expect(excerpts[1]!.text()).toContain('<code>原文</code>')
    expect(excerpts[1]!.find('code').exists()).toBe(false)
    // 预览区无链接无按钮（回看面同弹层直渲纪律、零跳转义务——draft 补节注）
    expect(w.find('[data-testid="approval-recall-body"]').findAll('a, button').length).toBe(0)
    // note 回显 + 等待时长
    expect(w.find('[data-testid="approval-recall-note"]').text()).toContain('补充指令')
    expect(w.find('[data-testid="approval-recall-note"]').text()).toContain('优先引用 ADR-017 结论')
    expect(w.find('[data-testid="approval-recall-waited"]').text()).toContain('9') // 审批等待 9 秒
  })

  it('再点收起：body 卸载、aria-expanded 翻 false（默认收起→展开→收起往返）', async () => {
    const w = mountRecall(makeTurn({ approval: { status: 'approved', choice: 'both', decided_by: 'user' }, approvalPreviews: [...approvalPreviewFixtures] }))
    await strip().trigger('click')
    await flushPromises()
    expect(body().exists()).toBe(true)
    await strip().trigger('click')
    await flushPromises()
    expect(body().exists()).toBe(false)
    expect(strip().attributes('aria-expanded')).toBe('false')
    w.unmount()
  })

  it('缺省面：2.5 对账后 approvalPreviews=null（流式缓存未建立）展开仅 note/等待时长零预览卡；note 缺省与 awaitingMs 非零才显示对应行', async () => {
    const w = mountRecall(makeTurn({ approval: { status: 'approved', choice: 'web_only', decided_by: 'user' } }))
    await strip().trigger('click')
    await flushPromises()
    expect(w.findAll('[data-testid="approval-recall-pv-title"]').length).toBe(0) // 无预览卡
    expect(w.find('[data-testid="approval-recall-note"]').exists()).toBe(false) // 无 note 行
    expect(w.find('[data-testid="approval-recall-waited"]').exists()).toBe(false) // awaitingMs=null 无等待行
    w.unmount()

    const w2 = mountRecall(makeTurn({ approval: { status: 'approved', choice: 'web_only', note: 'x', decided_by: 'user' }, awaitingMs: 0 }))
    await strip().trigger('click')
    await flushPromises()
    expect(w2.find('[data-testid="approval-recall-waited"]').exists()).toBe(false) // 0 毫秒非零才携带口径
    w2.unmount()
  })
})

describe('无 previews 降级形态（批次 3 评审 M4：刷新恢复 approved 无 previews——文案承诺与展开内容一致）', () => {
  it('无 previews 有审计面（note/等待时长）：条文案降级为裁决摘要（不含「查看当时的知识库命中」承诺）；展开=note+等待时长零预览卡', async () => {
    const w = mountRecall(makeTurn({
      approval: { status: 'approved', choice: 'custom', note: '只看 2024 年之后的资料', decided_by: 'user' },
      awaitingMs: 9200,
    }))
    expect(strip().text()).toContain('已按『自定义』回答')
    expect(strip().text()).not.toContain('查看当时的知识库命中') // 承诺降级（无内容可展开）
    await strip().trigger('click')
    await flushPromises()
    expect(body().exists()).toBe(true) // 仍可展开（审计面）
    expect(strip().attributes('aria-expanded')).toBe('true')
    expect(w.findAll('[data-testid="approval-recall-pv-title"]').length).toBe(0) // 预览区段不渲染
    expect(w.find('[data-testid="approval-recall-note"]').text()).toContain('只看 2024 年之后的资料')
    expect(w.find('[data-testid="approval-recall-waited"]').text()).toContain('9')
    w.unmount()
  })

  it('无 previews 且无 note 且无等待时长：strip 退化为非交互摘要（非 button、无 aria-expanded/aria-controls、点击零展开面）', async () => {
    const w = mountRecall(makeTurn({ approval: { status: 'approved', choice: 'kb_only', decided_by: 'timeout' } }))
    expect(strip().element.tagName.toLowerCase()).not.toBe('button')
    expect(strip().attributes('aria-expanded')).toBeUndefined()
    expect(strip().attributes('aria-controls')).toBeUndefined()
    await strip().trigger('click')
    await flushPromises()
    expect(body().exists()).toBe(false) // 零内容零展开面
    w.unmount()
  })

  it('展开面 id 带 turnId 后缀（评审 L8：多轮同屏 id 唯一——aria-controls 关联不串轮）', async () => {
    const turnId = 'aaaa1111-2a41-4f6e-b3c2-5d6e7f809a1b'
    const w = mountRecall(makeTurn({ turnId, approval: { status: 'approved', choice: 'both', decided_by: 'user' }, approvalPreviews: [...approvalPreviewFixtures] }))
    await strip().trigger('click')
    await flushPromises()
    const bodyEl = w.find('[data-testid="approval-recall-body"]')
    expect(bodyEl.attributes('id')).toBe(`approval-recall-body-${turnId}`)
    expect(strip().attributes('aria-controls')).toBe(`approval-recall-body-${turnId}`)
    w.unmount()
  })
})
