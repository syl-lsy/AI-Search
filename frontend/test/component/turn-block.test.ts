/**
 * test/component/turn-block.test.ts — TurnBlock 组件层（评审 P1 修复锁死：DegradedBanner）。
 *
 * 契约口径：api.md §2.5 degraded/degraded_reason + frontend-api.md §3 done 行（finish=degraded →
 * DegradedBanner「研究未完成」显著标注）/ §5 组件清单——降级终态轮与 completed 轮严格可区分，
 * 数据源 = turn.degradedReason（SSE done + 2.5 对账双源，stores/turn.ts）。
 */
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import TurnBlock from '@/components/answer/TurnBlock.vue'
import { toast } from '@/composables/toast'
import { i18n } from '@/i18n'
import type { TurnState } from '@/stores/turn'
import type { DegradedReason } from '@/types/api'

vi.mock('@/composables/toast', () => ({
  toast: vi.fn(),
}))
const mockedToast = vi.mocked(toast)

beforeEach(() => {
  // AnswerActions（REQ-057 反馈入口）setup 期消费 turnStore——组件挂载面统一装 pinia
  setActivePinia(createPinia())
})

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
    feedbackHelpful: null,
    lastSeq: 9,
    errorMsg: null,
    errorCode: null,
    refusalPendingClose: false,
    degradedReason: null,
    memorySaved: null,
    durationMs: null,
    exportAvailable: false,
    approval: null,
    approvalPreviews: null,
    awaitingMs: null,
    rewrittenQueries: null,
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

describe('TurnBlock：内容安全温和卡（B13 拒答轮 403002——错误渲染通路的中性温和分支）', () => {
  const SERVER_MSG = '您的提问涉及敏感内容，本轮无法作答。请调整后重试。'

  it('errorCode=403002：温和卡渲染（标题 i18n「内容安全提示」+ 正文=服务端 message）；状态行不再内联错误文本（防双份呈现）', () => {
    const w = mountTurn(
      makeTurn({ status: 'failed', errorCode: '403002', errorMsg: SERVER_MSG, stages: [{ stage_key: 'rewrite', status: 'failed' }] }),
    )
    const card = w.find('[data-testid="safety-card"]')
    expect(card.exists()).toBe(true)
    expect(card.attributes('role')).toBe('status')
    expect(card.text()).toContain('内容安全提示')
    expect(card.text()).toContain(SERVER_MSG)
    // 中性温和分支：卡内无 danger 语义元素（强烈报错红仅允许存在于状态行状态点，错误展示本体不走 danger）
    expect(card.find('.dot-danger').exists()).toBe(false)
    // 状态行不内联错误文本（既有通路分支接管）
    expect(w.find('[data-testid="turn-status"]').text()).not.toContain(SERVER_MSG)
  })

  it('其它错误码（500201）：无温和卡、状态行照旧内联错误文本（既有通路零变化）', () => {
    const w = mountTurn(makeTurn({ status: 'running', errorCode: '500201', errorMsg: '全部搜索源不可用' }))
    expect(w.find('[data-testid="safety-card"]').exists()).toBe(false)
    expect(w.find('[data-testid="turn-status"]').text()).toContain('全部搜索源不可用')
  })

  it('errorMsg 为空（2.5 error 载荷缺形态）：即使 code=403002 也不渲染空卡（无空区块原则）', () => {
    const w = mountTurn(makeTurn({ status: 'failed', errorCode: '403002', errorMsg: null }))
    expect(w.find('[data-testid="safety-card"]').exists()).toBe(false)
  })

  it('en 界面下温和卡标题随语言切换（zh/en 双包同步）', () => {
    expect(i18n.global.t('answer.contentSafetyTitle')).toBe('内容安全提示')
    i18n.global.locale.value = 'en-US'
    const w = mountTurn(makeTurn({ status: 'failed', errorCode: '403002', errorMsg: SERVER_MSG }))
    expect(w.find('[data-testid="safety-card"]').text()).toContain('Content safety notice')
    i18n.global.locale.value = 'zh-CN'
  })
})

describe('TurnBlock：canceled 轮状态行（v1.24 修复——canceled 原掉进 doneSeconds 默认分支误显绿点已完成）', () => {
  it('canceled 轮：中性点（dot-neutral，非绿点）+「已取消」+ 已过秒数照常显示；不显「已完成」；无降级 banner', () => {
    expect(i18n.global.t('answer.statusCanceled')).toBe('已取消')
    const w = mountTurn(makeTurn({ status: 'canceled', answer_md: '部分产出', durationMs: 7420 }))
    const status = w.find('[data-testid="turn-status"]')
    expect(status.find('.dot-neutral').exists()).toBe(true)
    expect(status.find('.dot-success').exists()).toBe(false)
    expect(status.find('.dot-warning').exists()).toBe(false)
    expect(status.text()).toContain('已取消')
    expect(status.text()).toContain('7s')
    expect(status.text()).not.toContain('已完成')
    // canceled 与 degraded 互斥终态：无 banner
    expect(w.find('[data-testid="degraded-banner"]').exists()).toBe(false)
    // 部分产出照常渲染（AnswerActions 语义：复制/复刻对 canceled 轮部分内容成立——保持现状）
    expect(w.find('[data-testid="turn-answer"]').exists()).toBe(true)
  })

  it('en 界面：Canceled（双语键完整）', () => {
    i18n.global.locale.value = 'en-US'
    const w = mountTurn(makeTurn({ status: 'canceled', durationMs: 5000 }))
    expect(w.find('[data-testid="turn-status"]').text()).toContain('Canceled')
    i18n.global.locale.value = 'zh-CN'
  })
})

describe('TurnBlock：问题气泡钳制与展开/收起（修复项⑤，对标图 2 形态）', () => {
  const LONG_QUESTION = '这是一条超过一百二十字符的长问题。'.repeat(12) // 204 字符 > 阈值 120

  it('长问题：气泡默认钳制（clamp class）+ 气泡下方展开按钮（aria-expanded=false）', () => {
    const w = mountTurn(makeTurn({ question: LONG_QUESTION }))
    const bubble = w.find('[data-testid="turn-question"] .bubble')
    expect(bubble.classes()).toContain('clamped')
    const toggle = w.find('[data-testid="bubble-toggle"]')
    expect(toggle.exists()).toBe(true)
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(toggle.text()).toContain('展开')
  })

  it('点击展开/收起按钮：clamp 切换 + aria-expanded/文案联动', async () => {
    const w = mountTurn(makeTurn({ question: LONG_QUESTION }))
    const toggle = () => w.find('[data-testid="bubble-toggle"]')
    await toggle().trigger('click')
    expect(w.find('[data-testid="turn-question"] .bubble').classes()).not.toContain('clamped')
    expect(toggle().attributes('aria-expanded')).toBe('true')
    expect(toggle().text()).toContain('收起')
    await toggle().trigger('click')
    expect(w.find('[data-testid="turn-question"] .bubble').classes()).toContain('clamped')
    expect(toggle().attributes('aria-expanded')).toBe('false')
  })

  it('含换行符的问题同样视为长问题（钳制判定 = 超 120 字符或含换行）', () => {
    const w = mountTurn(makeTurn({ question: '第一行\n第二行\n第三行' }))
    expect(w.find('[data-testid="turn-question"] .bubble').classes()).toContain('clamped')
    expect(w.find('[data-testid="bubble-toggle"]').exists()).toBe(true)
  })

  it('短问题（单行 ≤120 字符）：完整展开、无钳制无按钮', () => {
    const w = mountTurn(makeTurn({ question: '测试问题' }))
    const bubble = w.find('[data-testid="turn-question"] .bubble')
    expect(bubble.classes()).not.toContain('clamped')
    expect(w.find('[data-testid="bubble-toggle"]').exists()).toBe(false)
    expect(bubble.text()).toBe('测试问题')
  })

  it('i18n 双语键齐备（answer.expandQuestion / answer.collapseQuestion）', () => {
    expect(i18n.global.t('answer.expandQuestion')).toBe('展开')
    expect(i18n.global.t('answer.collapseQuestion')).toBe('收起')
    i18n.global.locale.value = 'en-US'
    expect(i18n.global.t('answer.expandQuestion')).toBe('Expand')
    expect(i18n.global.t('answer.collapseQuestion')).toBe('Collapse')
    i18n.global.locale.value = 'zh-CN'
  })
})

describe('TurnBlock：问题 hover 操作（REQ-055/A39，QuestionHoverActions 接线）', () => {
  const clipboardWrite = vi.fn<(text: string) => Promise<void>>()

  function mountWithClipboard(turn: TurnState) {
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: clipboardWrite.mockResolvedValue(undefined) },
      configurable: true,
    })
    return mountTurn(turn)
  }

  function hoverActions(w: ReturnType<typeof mountTurn>) {
    return w.find('[data-testid="question-hover-actions"]')
  }

  it('默认隐藏；hover 问题行 → 「编辑查询」「复制查询」两图标显示（气泡侧）', async () => {
    const w = mountWithClipboard(makeTurn({}))
    const row = w.find('[data-testid="turn-question"]')
    expect(hoverActions(w).classes()).not.toContain('visible')
    await row.trigger('mouseenter')
    expect(hoverActions(w).classes()).toContain('visible')
    const edit = w.find('[data-testid="qact-edit"]')
    const copy = w.find('[data-testid="qact-copy"]')
    expect(edit.exists()).toBe(true)
    expect(copy.exists()).toBe(true)
    // v1.24：原生 title 换 UiTooltip（语义由 aria-label 承载——气泡 aria-hidden 纯视觉）
    expect(edit.attributes('title')).toBeUndefined()
    expect(copy.attributes('title')).toBeUndefined()
    expect(edit.attributes('aria-label')).toBe('编辑查询')
    expect(copy.attributes('aria-label')).toBe('复制查询')
    // 气泡侧 = 问题气泡（flex-end 右对齐）之前——DOM 序：badge → hover 操作 → 气泡
    const actionsEl = row.element.querySelector('[data-testid="question-hover-actions"]')
    expect(actionsEl?.previousElementSibling?.classList.contains('badge')).toBe(true)
    expect(actionsEl?.nextElementSibling?.classList.contains('bubble-wrap')).toBe(true)
  })

  it('移出隐藏；键盘 focusin 同样显示（P1 hover+focus 双触发）', async () => {
    const w = mountWithClipboard(makeTurn({}))
    const row = w.find('[data-testid="turn-question"]')
    await row.trigger('mouseenter')
    await row.trigger('mouseleave')
    expect(hoverActions(w).classes()).not.toContain('visible')
    await row.trigger('focusin')
    expect(hoverActions(w).classes()).toContain('visible')
    await row.trigger('focusout')
    expect(hoverActions(w).classes()).not.toContain('visible')
  })

  it('点击「复制查询」：query 原文进剪贴板（成功零打扰，不弹 toast）', async () => {
    mockedToast.mockClear()
    const w = mountWithClipboard(makeTurn({ question: '原始查询全文' }))
    await w.find('[data-testid="turn-question"]').trigger('mouseenter')
    await w.find('[data-testid="qact-copy"]').trigger('click')
    await vi.waitFor(() => expect(clipboardWrite).toHaveBeenCalledWith('原始查询全文'))
    expect(mockedToast).not.toHaveBeenCalled()
  })

  it('复制查询失败：console.warn 留痕 + 一次性 toast（answer.copyFailed；批次 2 评审 P3 闭合）', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    mockedToast.mockClear()
    clipboardWrite.mockRejectedValueOnce(new Error('blocked'))
    const w = mountWithClipboard(makeTurn({ question: '原始查询全文' }))
    await w.find('[data-testid="turn-question"]').trigger('mouseenter')
    await w.find('[data-testid="qact-copy"]').trigger('click')
    await vi.waitFor(() => expect(mockedToast).toHaveBeenCalledTimes(1))
    // 断言文案键：默认 locale zh-CN，toast 文案 = answer.copyFailed 词表值
    expect(mockedToast).toHaveBeenCalledWith(i18n.global.t('answer.copyFailed'))
    expect(i18n.global.t('answer.copyFailed')).toBe('复制失败，请手动复制')
    expect(warnSpy).toHaveBeenCalled()
    warnSpy.mockRestore()
  })

  it('点击「编辑查询」：emit edit-question 携带 query 原文（AnswerView 预填追问框，不提交）', async () => {
    const w = mountWithClipboard(makeTurn({ question: '要预填的问题' }))
    await w.find('[data-testid="turn-question"]').trigger('mouseenter')
    await w.find('[data-testid="qact-edit"]').trigger('click')
    expect(w.emitted('edit-question')).toEqual([['要预填的问题']])
  })

  it('图标 sprite 齐备：编辑（i-pencil）/复制（i-copy）经 UiIcon 引用', async () => {
    const w = mountWithClipboard(makeTurn({}))
    await w.find('[data-testid="turn-question"]').trigger('mouseenter')
    expect(w.find('[data-testid="qact-edit"] use[href="#i-pencil"]').exists()).toBe(true)
    expect(w.find('[data-testid="qact-copy"] use[href="#i-copy"]').exists()).toBe(true)
  })
})

describe('TurnBlock：回答底部操作行 + followups 接线（REQ-056/REQ-046 前端面）', () => {
  it('答案区渲染 AnswerActions；rerun/sources 事件上抛', async () => {
    const w = mountTurn(makeTurn({ answer_md: '答案正文' }))
    const actions = w.find('[data-testid="answer-actions"]')
    expect(actions.exists()).toBe(true)
    await actions.find('[data-testid="act-rerun"]').trigger('click')
    await actions.find('[data-testid="act-sources"]').trigger('click')
    expect(w.emitted('rerun')).toEqual([[makeTurn({}).question]])
    expect(w.emitted('sources')).toEqual([[]])
  })

  it('followups 非空 → FollowupChips 渲染于答案下方；点击 chip 上抛 follow', async () => {
    const w = mountTurn(makeTurn({ answer_md: '答案正文', followups: ['追问一', '追问二'] }))
    const chips = w.find('[data-testid="followup-chips"]')
    expect(chips.exists()).toBe(true)
    const items = chips.findAll('button')
    expect(items.length).toBe(2)
    await items[0]!.trigger('click')
    expect(w.emitted('follow')).toEqual([['追问一']])
  })

  it('followups 为空/null → 零渲染（无空区块原则）', () => {
    const w = mountTurn(makeTurn({ answer_md: '答案正文', followups: null }))
    expect(w.find('[data-testid="followup-chips"]').exists()).toBe(false)
    const w2 = mountTurn(makeTurn({ answer_md: '答案正文', followups: [] }))
    expect(w2.find('[data-testid="followup-chips"]').exists()).toBe(false)
  })
})

describe('TurnBlock：审批回看折叠条接线（B22 v1.40 批次 3 第三段——问题下方一行，ApprovalRecall 承载）', () => {
  it('approved 轮：折叠条渲染于问题下方（默认收起）；no_hit（web 直达）与 approval=null 轮零审批痕迹', () => {
    const approved = mountTurn(makeTurn({
      approval: { status: 'approved', choice: 'kb_only', decided_by: 'user' },
      approvalPreviews: null,
    }))
    const rail = approved.find('[data-testid="approval-recall-strip"]')
    expect(rail.exists()).toBe(true)
    expect(rail.text()).toContain('已按『仅知识库』回答')
    // 问题下方：rail 位于问题气泡块之后、状态行之前（draft 06 补节落位）
    expect(approved.find('[data-testid="turn-question"]').exists()).toBe(true)
    approved.unmount()

    const noHit = mountTurn(makeTurn({ approval: { status: 'no_hit', verdict: 'no_hit' } }))
    expect(noHit.find('[data-testid="approval-recall"]').exists()).toBe(false) // web 直达轮不显示任何条
    noHit.unmount()

    const nonGate = mountTurn(makeTurn({ approval: null }))
    expect(nonGate.find('[data-testid="approval-recall"]').exists()).toBe(false) // 非门轮零痕迹
    nonGate.unmount()
  })
})
