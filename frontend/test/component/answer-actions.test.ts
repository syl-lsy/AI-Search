/**
 * test/component/answer-actions.test.ts — AnswerActions 回答底部操作行（REQ-056/A40 + REQ-057/A41）。
 *
 * 七钮常驻：复制（answer_md 入剪贴板，失败 toast 一次性反馈）/ 分享（= 复制会话链接
 * origin + /thread/{threadId}，成功 toast(answer.linkCopied)、失败 toast(answer.copyFailed)）/
 * 有帮助·没有帮助（互斥 + 同钮再点即取消——api.md §2.9 v1.17 helpful:null，store.sendFeedback
 * 乐观 UI）/ 复刻（emit rerun）/ 来源（emit
 * sources，带计数）/ 更多操作（轻菜单 → 导出 Markdown 前端本地 Blob 下载——不调深度专用
 * export 端点，A40 负向判定）。
 * 反馈接 store.sendFeedback（api/turns.ts 唯一出口，mock 观察提交体与乐观置位）；
 * toast 走 mock 观察文案键（answer.copyFailed），成功路径零打扰不弹。
 */
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { sendTurnFeedback } from '@/api/turns'
import AnswerActions from '@/components/answer/AnswerActions.vue'
import { toast } from '@/composables/toast'
import { i18n, setLocale } from '@/i18n'
import { THREAD_ID_FAST, TURN_ID_FAST } from '@/mocks/turnDetails'
import { useTurnStore } from '@/stores/turn'
import type { TurnState } from '@/stores/turn'

vi.mock('@/api/turns', () => ({
  sendTurnFeedback: vi.fn(),
}))
const mockedSendFeedback = vi.mocked(sendTurnFeedback)

vi.mock('@/composables/toast', () => ({
  toast: vi.fn(),
}))
const mockedToast = vi.mocked(toast)

const QUESTION = 'LangGraph 的 Send API 和 Router 的区别？'
const ANSWER = 'Send API 用于**动态扇出**，Router 用于静态分支 [1]。'

function makeTurn(partial: Partial<TurnState> = {}): TurnState {
  return {
    turnId: TURN_ID_FAST,
    threadId: THREAD_ID_FAST,
    mode: 'fast',
    question: QUESTION,
    status: 'completed',
    answer_md: ANSWER,
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
    degradedReason: null,
    memorySaved: null,
    durationMs: 7420,
    ...partial,
  }
}

/** 种子化 store 投影（AnswerActions 经 useTurnStore 读/写 feedbackHelpful） */
function seedStore(turn: TurnState): ReturnType<typeof useTurnStore> {
  const store = useTurnStore()
  store.turns[turn.turnId] = turn
  return store
}

function mountActions(turn: TurnState): VueWrapper {
  return mount(AnswerActions, { props: { turn }, global: { plugins: [i18n] } })
}

const clipboardWrite = vi.fn<(text: string) => Promise<void>>()

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText: clipboardWrite.mockResolvedValue(undefined) },
    configurable: true,
  })
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('AnswerActions（REQ-056/A40：七钮操作行）', () => {
  it('七钮常驻渲染（复制/分享/有帮助/没有帮助/复刻/来源/更多操作）', () => {
    const w = mountActions(makeTurn())
    for (const id of ['act-copy', 'act-share', 'act-helpful', 'act-not-helpful', 'act-rerun', 'act-sources', 'act-more']) {
      expect(w.find(`[data-testid="${id}"]`).exists()).toBe(true)
    }
  })

  it('复制：answer_md 全文入剪贴板（成功零打扰——flushPromises 后再断言不弹 toast，对齐 turn-block 形态）', async () => {
    const w = mountActions(makeTurn())
    await w.find('[data-testid="act-copy"]').trigger('click')
    await flushPromises()
    expect(clipboardWrite).toHaveBeenCalledWith(ANSWER)
    expect(mockedToast).not.toHaveBeenCalled()
  })

  it('复制失败：console.warn 留痕 + 一次性 toast（answer.copyFailed，zh「复制失败，请手动复制」）', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    clipboardWrite.mockRejectedValueOnce(new Error('blocked'))
    const w = mountActions(makeTurn())
    await w.find('[data-testid="act-copy"]').trigger('click')
    await flushPromises()
    expect(warnSpy).toHaveBeenCalled()
    // 断言文案键：默认 locale zh-CN，toast 文案 = answer.copyFailed 词表值
    expect(mockedToast).toHaveBeenCalledTimes(1)
    expect(mockedToast).toHaveBeenCalledWith(i18n.global.t('answer.copyFailed'))
    expect(i18n.global.t('answer.copyFailed')).toBe('复制失败，请手动复制')
    warnSpy.mockRestore()
  })

  it('分享 = 复制会话链接（origin + /thread/{threadId}）：成功 toast(answer.linkCopied，zh「链接已复制」)', async () => {
    const w = mountActions(makeTurn())
    await w.find('[data-testid="act-share"]').trigger('click')
    await flushPromises()
    expect(clipboardWrite).toHaveBeenCalledWith(`${window.location.origin}/thread/${THREAD_ID_FAST}`)
    expect(mockedToast).toHaveBeenCalledTimes(1)
    expect(mockedToast).toHaveBeenCalledWith(i18n.global.t('answer.linkCopied'))
    expect(i18n.global.t('answer.linkCopied')).toBe('链接已复制')
  })

  it('分享失败：console.warn 留痕 + toast 复用 answer.copyFailed', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    clipboardWrite.mockRejectedValueOnce(new Error('blocked'))
    const w = mountActions(makeTurn())
    await w.find('[data-testid="act-share"]').trigger('click')
    await flushPromises()
    expect(warnSpy).toHaveBeenCalled()
    expect(mockedToast).toHaveBeenCalledTimes(1)
    expect(mockedToast).toHaveBeenCalledWith(i18n.global.t('answer.copyFailed'))
    warnSpy.mockRestore()
  })

  it('复刻：emit rerun 携带该轮原问题（AnswerView 以原问题原模式走既有追问流）', async () => {
    const w = mountActions(makeTurn({ mode: 'deep', question: QUESTION }))
    await w.find('[data-testid="act-rerun"]').trigger('click')
    expect(w.emitted('rerun')).toEqual([[QUESTION]])
  })

  it('来源：emit sources；计数标签渲染（10 个来源带数字形态）', async () => {
    const sources = Array.from({ length: 10 }, (_, i) => ({
      source_id: `f00d000${i}-0000-4000-8000-00000000000${i}`,
      ref_no: i + 1,
      source_type: 'web' as const,
      title: `来源 ${i + 1}`,
      url: `https://example.com/${i + 1}`,
      snippet: '…',
      score: 0.9,
      snapshot_available: false,
      kb_doc_id: null,
    }))
    const w = mountActions(makeTurn({ sources }))
    expect(w.find('[data-testid="act-sources"]').text()).toContain('10')
    await w.find('[data-testid="act-sources"]').trigger('click')
    expect(w.emitted('sources')).toEqual([[]])
  })

  it('反馈（REQ-057/A41）：点击即 POST（提交体经 api/turns）+ 乐观置位选中态（aria-pressed 互斥）', async () => {
    const turn = makeTurn()
    const store = seedStore(turn)
    mockedSendFeedback.mockResolvedValueOnce({ turn_id: TURN_ID_FAST, helpful: true, updated_at: '2026-11-14T08:30:00Z' })
    const w = mountActions(turn)
    expect(w.find('[data-testid="act-helpful"]').attributes('aria-pressed')).toBe('false')
    expect(w.find('[data-testid="act-not-helpful"]').attributes('aria-pressed')).toBe('false')
    await w.find('[data-testid="act-helpful"]').trigger('click')
    expect(mockedSendFeedback).toHaveBeenCalledWith(TURN_ID_FAST, true)
    expect(store.turns[TURN_ID_FAST]!.feedbackHelpful).toBe(true)
    expect(w.find('[data-testid="act-helpful"]').attributes('aria-pressed')).toBe('true')
    expect(w.find('[data-testid="act-not-helpful"]').attributes('aria-pressed')).toBe('false')
  })

  it('反馈取消（api.md §2.9 v1.17 同钮再点即取消）：已选「有帮助」再点 → 发 helpful:null → 本地清除（aria-pressed 复位）', async () => {
    const turn = makeTurn({ feedbackHelpful: true })
    const store = seedStore(turn)
    mockedSendFeedback.mockResolvedValueOnce({ turn_id: TURN_ID_FAST, helpful: null, updated_at: null })
    const w = mountActions(turn)
    expect(w.find('[data-testid="act-helpful"]').attributes('aria-pressed')).toBe('true')
    await w.find('[data-testid="act-helpful"]').trigger('click')
    await flushPromises()
    expect(mockedSendFeedback).toHaveBeenCalledWith(TURN_ID_FAST, null)
    expect(store.turns[TURN_ID_FAST]!.feedbackHelpful).toBeNull()
    expect(w.find('[data-testid="act-helpful"]').attributes('aria-pressed')).toBe('false')
    expect(w.find('[data-testid="act-not-helpful"]').attributes('aria-pressed')).toBe('false')
  })

  it('反馈改判互斥：已有 helpful=true 时点没有帮助 → POST false 且选中态切换', async () => {
    const turn = makeTurn({ feedbackHelpful: true })
    seedStore(turn)
    mockedSendFeedback.mockResolvedValueOnce({ turn_id: TURN_ID_FAST, helpful: false, updated_at: '2026-11-14T08:31:00Z' })
    const w = mountActions(turn)
    expect(w.find('[data-testid="act-helpful"]').attributes('aria-pressed')).toBe('true')
    await w.find('[data-testid="act-not-helpful"]').trigger('click')
    expect(mockedSendFeedback).toHaveBeenCalledWith(TURN_ID_FAST, false)
    expect(w.find('[data-testid="act-helpful"]').attributes('aria-pressed')).toBe('false')
    expect(w.find('[data-testid="act-not-helpful"]').attributes('aria-pressed')).toBe('true')
  })

  it('更多操作轻菜单：展开 → 导出 Markdown → 前端本地 Blob .md 下载（文件名含问题、菜单关闭）', async () => {
    const createObjectURL = vi.fn<(blob: Blob) => string>(() => 'blob:mock-url')
    const revokeObjectURL = vi.fn<(url: string) => void>()
    Object.defineProperty(URL, 'createObjectURL', { value: createObjectURL, configurable: true, writable: true })
    Object.defineProperty(URL, 'revokeObjectURL', { value: revokeObjectURL, configurable: true, writable: true })
    const clickedAnchor: { current: Partial<HTMLAnchorElement> | null } = { current: null }
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      clickedAnchor.current = this
    })
    try {
      const w = mountActions(makeTurn())
      // 收起态不渲染菜单
      expect(w.find('[data-testid="act-more-menu"]').exists()).toBe(false)
      await w.find('[data-testid="act-more"]').trigger('click')
      expect(w.find('[data-testid="act-more-menu"]').exists()).toBe(true)
      await w.find('[data-testid="act-export-md"]').trigger('click')
      await flushPromises()
      // 前端本地生成：Blob 内容 = answer_md；文件名 .md；不依赖 export 端点（零 POST 断言见下）
      expect(createObjectURL).toHaveBeenCalledTimes(1)
      const blob = createObjectURL.mock.calls[0]?.[0]
      expect(blob).toBeInstanceOf(Blob)
      await expect(blob!.text()).resolves.toBe(ANSWER)
      expect(clickedAnchor.current?.download).toBe(`${QUESTION}.md`)
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock-url')
      expect(w.find('[data-testid="act-more-menu"]').exists()).toBe(false)
      expect(mockedSendFeedback).not.toHaveBeenCalled()
    } finally {
      vi.restoreAllMocks()
    }
  })
})

describe('第 8 钮「重新生成」（v1.26 REQ-063/A47：可见性门控 + 分发，frontend-api.md §5 ⑧）', () => {
  const SOURCE_A = {
    source_id: 'f00d0001-0000-4000-8000-000000000001',
    ref_no: 1,
    source_type: 'web' as const,
    title: '来源一',
    url: 'https://a.example.com/x',
    snippet: '…',
    score: 0.9,
    snapshot_available: false,
    kb_doc_id: null,
  }

  it('可见：degraded + 有来源 + fast（i-replay 图标 + UiTooltip 包装 + aria-label=answer.regenerate）', () => {
    const w = mountActions(makeTurn({ status: 'degraded', degradedReason: 'source_failure', sources: [SOURCE_A] }))
    const btn = w.find('[data-testid="act-regenerate"]')
    expect(btn.exists()).toBe(true)
    expect(btn.classes()).toContain('aa-labeled')
    expect(btn.text()).toContain('重新生成')
    expect(btn.attributes('aria-label')).toBe('重新生成')
    expect(btn.find('svg use').attributes('href')).toBe('#i-replay')
    expect((btn.element as HTMLElement).closest('.ui-tip')).not.toBeNull()
    expect(btn.attributes('title')).toBeUndefined()
  })

  it('可见：failed + 有来源 + fast（崩溃恢复 failed-with-sources 同样可重生成，api.md §2.12 前置 1）', () => {
    const w = mountActions(makeTurn({ status: 'failed', errorMsg: '综合失败', sources: [SOURCE_A] }))
    expect(w.find('[data-testid="act-regenerate"]').exists()).toBe(true)
  })

  it('不可见：completed（非 degraded/failed 终态）', () => {
    const w = mountActions(makeTurn({ sources: [SOURCE_A] }))
    expect(w.find('[data-testid="act-regenerate"]').exists()).toBe(false)
  })

  it('不可见：canceled', () => {
    const w = mountActions(makeTurn({ status: 'canceled', cancelReason: 'user', sources: [SOURCE_A] }))
    expect(w.find('[data-testid="act-regenerate"]').exists()).toBe(false)
  })

  it('不可见：failed 零来源（api.md §2.12 前置 2：sources 非空）', () => {
    const w = mountActions(makeTurn({ status: 'failed', errorMsg: '综合失败', sources: [] }))
    expect(w.find('[data-testid="act-regenerate"]').exists()).toBe(false)
  })

  it('不可见：deep 轮（api.md §2.12 前置 3：mode=fast——deep regenerate 挂 M2 深度工单）', () => {
    const w = mountActions(makeTurn({ mode: 'deep', status: 'degraded', degradedReason: 'budget', sources: [SOURCE_A] }))
    expect(w.find('[data-testid="act-regenerate"]').exists()).toBe(false)
  })

  it('不可见：running（非 isLive——流式中不渲染防御位）', () => {
    const w = mountActions(makeTurn({ status: 'running', sources: [SOURCE_A], durationMs: null }))
    expect(w.find('[data-testid="act-regenerate"]').exists()).toBe(false)
  })

  it('点击 emit regenerate（无参——AnswerView 以轮闭包承接 store.regenerate）', async () => {
    const w = mountActions(makeTurn({ status: 'degraded', degradedReason: 'source_failure', sources: [SOURCE_A] }))
    await w.find('[data-testid="act-regenerate"]').trigger('click')
    expect(w.emitted('regenerate')).toEqual([[]])
  })

  it('i18n 双语（§7 v1.26 词条）：zh-CN「重新生成」/ en-US「Regenerate」', () => {
    expect(i18n.global.t('answer.regenerate')).toBe('重新生成')
    setLocale('en-US')
    expect(i18n.global.t('answer.regenerate')).toBe('Regenerate')
    setLocale('zh-CN')
  })
})

describe('AnswerActions tooltip 接入（v1.24：UiTooltip 替换原生 title——接入面补漏）', () => {
  const BUTTON_IDS = ['act-copy', 'act-share', 'act-helpful', 'act-not-helpful', 'act-rerun', 'act-sources', 'act-more'] as const

  it('七钮全部包 UiTooltip 包装层（closest .ui-tip）且无原生 title；aria-label 保留', () => {
    const w = mountActions(makeTurn())
    for (const id of BUTTON_IDS) {
      const btn = w.find(`[data-testid="${id}"]`)
      expect(btn.exists()).toBe(true)
      expect(btn.attributes('title')).toBeUndefined()
      expect(btn.attributes('aria-label')).not.toBeUndefined()
      expect((btn.element as HTMLElement).closest('.ui-tip')).not.toBeNull()
    }
  })

  it('hover 复制钮 300ms → 气泡显主标题「复制」（ui-tooltip 同款形态；离开即隐）', async () => {
    vi.useFakeTimers()
    try {
      const w = mountActions(makeTurn())
      const wrap = w.find('[data-testid="act-copy"]').element.closest('.ui-tip')!
      wrap.dispatchEvent(new MouseEvent('mouseenter'))
      vi.advanceTimersByTime(300)
      await flushPromises()
      const bubbles = document.querySelectorAll('[data-testid="ui-tip-bubble"]')
      expect(bubbles).toHaveLength(1)
      expect(bubbles[0]!.textContent).toContain('复制')
      wrap.dispatchEvent(new MouseEvent('mouseleave'))
      await flushPromises()
      expect(document.querySelectorAll('[data-testid="ui-tip-bubble"]')).toHaveLength(0)
    } finally {
      vi.useRealTimers()
    }
  })
})
