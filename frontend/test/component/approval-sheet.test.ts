/**
 * test/component/approval-sheet.test.ts — ApprovalSheet 审批弹层（B22 v1.40 批次 3 第二段：
 * KB 门业务实例——通用壳 HitlSheet + 业务皮，用户 2026-11-28 组件架构裁决；
 * frontend-api.md §5 ApprovalSheet 行 / §3 生命周期 15 / api.md §2.13/§7.2 消费面）。
 *
 * 覆盖：
 *   - 壳承载形态：Teleport body（避容器裁剪）+ fixed 底部覆盖层 + z-index 层级表扩展位——
 *     面板 testid=hitl-sheet（壳统一），业务内容经 preview/choices/note 三 slot 注入
 *   - 预览区 ≤3 条文本插值直渲（KB 语料=外部内容：禁 v-html/禁 markdown——类标签原文逐字
 *     保真，KbChunkDrilldown 先例；heading_path 逐段插值）+ **纯展示不可点**（2026-11-28
 *     冲突裁决：无链接无按钮无点击面，看全文链路挂批次 5 前契约增量批 §4.6 chunk 直查）
 *   - 四选项（点击即提交）+ 自定义展开（note ≤500 计数与截断提示 + 范围三钮缺省 both 预选）
 *   - 倒计时：服务端 expires_at 为准、前端仅展示——末 15s 提示条（壳 hitl-timeout-hint）、
 *     零本地超时动作（超点不调 cancel/approve，判定权在服务端）
 *   - 关闭=canceled(user) 确认：× 与「取消本轮」两入口 → 壳关闭确认区 → 确认后
 *     store.cancelApproval（POST /cancel + 本地立即收口、弹层随状态派生关闭）
 *   - 409008：store.decideApproval 内 toast + reconcile；弹层保留可重试（网络错同）
 *   - a11y：role=dialog/aria-label/焦点 open 落面板/键盘 Esc 不触发取消（取消仅两个显式入口）
 *
 * store 直连（契约「弹层可见性由状态派生」）——测试以真 store + vi.mock api/client 全链驱动；
 * 壳机制本身（倒计时/确认/错误态/定位）由 hitl-sheet.test.ts 独立覆盖。
 */
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { apiGet, apiPost } from '@/api/client'
import { toast } from '@/composables/toast'
import { i18n } from '@/i18n'
import ApprovalSheet from '@/components/answer/ApprovalSheet.vue'
import { useTurnStore } from '@/stores/turn'
import type { SseWireEvent, TurnDetail } from '@/types/api'
import { MockEventSource } from '../helpers/mockEventSource'

const THREAD_ID = '7d2f0a1e-1c3b-4e5a-9d8f-0a1b2c3d4e5f'
const TURN_ID = 'c31b9d74-2a41-4f6e-b3c2-5d6e7f809a1b'

const normalizeState = vi.hoisted(() => ({ code: 'network_error', message: '网络异常，请稍后重试' }))

vi.mock('@/api/client', () => ({
  apiPost: vi.fn(),
  apiGet: vi.fn(),
  // API_BASE_URL：turnStream.ts 真模块顶层 import（本文件不 mock turnStream——组件不触流式
  // 编排），缺失该 export 会在模块绑定期抛错
  API_BASE_URL: '',
  normalizeError: () => ({
    name: 'ApiError',
    code: normalizeState.code,
    message: normalizeState.message,
  }),
}))

vi.mock('@/composables/toast', () => ({
  toast: vi.fn(),
}))

const mockedPost = vi.mocked(apiPost)
const mockedGet = vi.mocked(apiGet)
const mockedToast = vi.mocked(toast)

const EXPIRES_AT = '2026-11-01T08:14:06Z' // 08:12:06 + 120s（seed 系统时间 08:12:06）

function makePreview(index: number, overrides: Record<string, unknown> = {}) {
  return {
    kb_doc_id: '9a77f0e1-1c3b-4e5a-9d8f-0a1b2c3d4e5f',
    chunk_id: `5c2e8a3b-2d4f-4a6b-8c9d-${String(index).padStart(12, '0')}`,
    title: '内部设计文档-检索融合.md',
    heading_path: ['知识库', '检索融合', 'RRF 融合'],
    excerpt: 'KB 候选与网络候选合并后统一 rerank，跨子问题全局去重后按分数排序（≤200 字符，服务端截断）',
    query_index: index,
    ...overrides,
  }
}

/** TurnDetail 2.5 形态（approval 为 v1.37 可选键，overrides 注入） */
function makeDetail(overrides: Partial<TurnDetail> = {}): TurnDetail {
  return {
    turn_id: TURN_ID,
    thread_id: THREAD_ID,
    mode: 'fast',
    status: 'running',
    query: '测试问题',
    query_lang: 'zh',
    rewritten_queries: [],
    parent_turn_id: null,
    answer_md: '',
    sources: [],
    images: [],
    stages: [],
    degraded: false,
    degraded_reason: null,
    error: null,
    usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, cost_usd: 0 },
    duration_ms: 0,
    export_available: false,
    created_at: '2026-11-01T08:12:00Z',
    finished_at: null,
    incognito: false,
    cancel_reason: null,
    followups: null,
    ...overrides,
  }
}

/** wire 事件构造（id=seq；测试 fixture 单一 as 收窄，§8.9①） */
function wire(id: number, event: SseWireEvent['event'], payload: Record<string, unknown>): SseWireEvent {
  return {
    id,
    event,
    data: { seq: id, turn_id: TURN_ID, ts: '2026-11-01T08:12:00Z', ...payload },
  } as SseWireEvent
}

/** 种子 awaiting 轮：ask（running）→ approval_requested（awaiting + pending 载荷） */
async function seedAwaiting(previews: ReturnType<typeof makePreview>[] = [makePreview(0)]) {
  mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID, status: 'running' })
  const store = useTurnStore()
  await store.ask('KB 里怎么融合检索？', 'fast')
  store.applyEvent(TURN_ID, wire(5, 'approval_requested', {
    verdict: 'has_hit',
    expires_at: EXPIRES_AT,
    previews,
  }))
  return store
}

/** 挂载（Teleport 后经 document 断言；anchorEl=null = 贴视口底退化形态） */
function mountSheet(rewrittenQueries?: string[]) {
  return mount(ApprovalSheet, {
    props: { turnId: TURN_ID, anchorEl: null, ...(rewrittenQueries !== undefined ? { rewrittenQueries } : {}) },
    global: { plugins: [i18n] },
  })
}

/** 面板=壳统一 testid（hitl-sheet——Teleport 后经 document 断言） */
const sheet = (): Element | null => document.querySelector('[data-testid="hitl-sheet"]')
const previews = (): NodeListOf<Element> => document.querySelectorAll('[data-testid^="approval-preview-"]')
const excerptAt = (i: number): string | null =>
  document.querySelector(`[data-testid="approval-preview-${i}"] [data-testid="approval-pv-excerpt"]`)?.textContent ?? null

/** 关闭确认两步辅助：入口点击 → 确认区出现（未确认前零 POST） */
async function openConfirm(entry: 'hitl-close' | 'hitl-cancel'): Promise<void> {
  document.querySelector<HTMLButtonElement>(`[data-testid="${entry}"]`)!.click()
  await flushPromises()
  expect(document.querySelector('[data-testid="hitl-close-confirm"]')).not.toBeNull()
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  normalizeState.code = 'network_error'
  normalizeState.message = '网络异常，请稍后重试'
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-11-01T08:12:06Z'))
  document.body.innerHTML = ''
  // 真模块 turnStream 保留（本文件不 mock '@/sse/turnStream'）——jsdom 无原生 EventSource，
  // 以全局替身注入（answer-view-route.test.ts:155 同款先例）：seedAwaiting→store.ask→subscribe
  // 走真订阅链路创建替身实例（永不 open、零帧派发），替身仅承载订阅副作用
  MockEventSource.reset()
  vi.stubGlobal('EventSource', MockEventSource)
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('渲染（壳承载：Teleport body + 底部 sheet 覆盖层 + 状态派生）', () => {
  it('awaiting+pending：Teleport 到 body 渲染壳面板 role=dialog（aria-label=approvalTitle、position:fixed 底部覆盖层）', async () => {
    await seedAwaiting()
    const w = mountSheet()
    await flushPromises()
    const panel = sheet()
    expect(panel).not.toBeNull()
    expect(w.find('[data-testid="hitl-sheet"]').exists()).toBe(false)
    expect(document.body.contains(panel!)).toBe(true)
    expect(panel!.getAttribute('role')).toBe('dialog')
    expect(panel!.getAttribute('aria-label')).toBe('知识库里找到相关内容')
    expect(panel!.getAttribute('aria-modal')).toBe('false') // 非模态覆盖层（不劫持页面滚动）
    expect((panel as HTMLElement).style.position).toBe('fixed')
    w.unmount()
  })

  it('非 pending 形态（approved 回看态）：整块不渲染——decideApproval 乐观收敛后弹层随状态派生关闭（不等 SSE）', async () => {
    const store = await seedAwaiting()
    mockedPost.mockResolvedValueOnce({ turn_id: TURN_ID, status: 'running', choice: 'kb_only' })
    const w = mountSheet()
    await flushPromises()
    expect(sheet()).not.toBeNull()
    await store.decideApproval(TURN_ID, 'kb_only')
    await flushPromises()
    expect(sheet()).toBeNull() // status=running 状态派生自动关
    w.unmount()
  })
})

describe('预览区（≤3 条文本插值直渲——KB 语料外部内容安全面；纯展示不可点）', () => {
  it('逐条渲染 title/heading_path 面包屑/excerpt/query_index 对位（rewrittenQueries 提供时）', async () => {
    await seedAwaiting([makePreview(0), makePreview(1)])
    const w = mountSheet(['KB 里怎么融合检索？', '混合检索怎么做？'])
    await flushPromises()
    expect(previews().length).toBe(2)
    expect(document.querySelector('[data-testid="approval-preview-0"] [data-testid="approval-pv-title"]')!.textContent).toBe('内部设计文档-检索融合.md')
    const path0 = document.querySelector('[data-testid="approval-preview-0"] [data-testid="approval-pv-path"]')!
    expect(path0.textContent).toContain('知识库')
    expect(path0.textContent).toContain('检索融合')
    expect(path0.textContent).toContain('RRF 融合')
    expect(excerptAt(0)).toContain('统一 rerank')
    expect(document.querySelector('[data-testid="approval-preview-0"] [data-testid="approval-pv-query"]')!.textContent).toBe('KB 里怎么融合检索？')
    expect(document.querySelector('[data-testid="approval-preview-1"] [data-testid="approval-pv-query"]')!.textContent).toBe('混合检索怎么做？')
    w.unmount()
  })

  it('直渲保真：类标签原文逐字保留为文本（禁 v-html——DOM 无新增元素节点），KbChunkDrilldown 先例', async () => {
    await seedAwaiting([makePreview(0, {
      title: '<b>加粗标题</b> & <script>alert(1)</script>',
      heading_path: ['目录<i>x</i>', '转义 & 段'],
      excerpt: '类标签 <code>原文</code> 逐字保真 & 不转 markdown',
    })])
    const w = mountSheet()
    await flushPromises()
    const titleEl = document.querySelector('[data-testid="approval-preview-0"] [data-testid="approval-pv-title"]')!
    expect(titleEl.textContent).toBe('<b>加粗标题</b> & <script>alert(1)</script>')
    expect(titleEl.querySelector('b')).toBeNull() // 文本插值——无元素节点注入
    expect(titleEl.querySelector('script')).toBeNull()
    const pathEl = document.querySelector('[data-testid="approval-preview-0"] [data-testid="approval-pv-path"]')!
    expect(pathEl.textContent).toContain('<i>x</i>')
    expect(pathEl.querySelector('i')).toBeNull()
    expect(excerptAt(0)).toContain('<code>原文</code>')
    w.unmount()
  })

  it('纯展示不可点（2026-11-28 冲突裁决）：预览区内零链接零按钮零可点击面——看全文链路挂批次 5 前契约增量批', async () => {
    await seedAwaiting([makePreview(0), makePreview(1), makePreview(2)])
    const w = mountSheet()
    await flushPromises()
    const pv = document.querySelector('[data-testid="approval-previews"]')!
    expect(pv.querySelectorAll('a, button')).toHaveLength(0) // 无链接无按钮
    expect(pv.getAttribute('role')).toBe('list') // 列表语义（非交互面）
    w.unmount()
  })
})

describe('四选项与自定义展开（点击即提交；note ≤500 计数与截断）', () => {
  it('四选项文案渲染；点「仅知识库」→ POST body {choice:"kb_only"} + 乐观置 running（弹层关）', async () => {
    const store = await seedAwaiting()
    const w = mountSheet()
    await flushPromises()
    const kbOnly = document.querySelector<HTMLButtonElement>('[data-testid="approval-choice-kb_only"]')!
    expect(kbOnly.textContent).toContain('仅知识库')
    expect(document.querySelector('[data-testid="approval-choice-web_only"]')!.textContent).toContain('仅网络')
    expect(document.querySelector('[data-testid="approval-choice-both"]')!.textContent).toContain('两者都要')
    expect(document.querySelector('[data-testid="approval-custom-toggle"]')!.textContent).toContain('自定义')
    mockedPost.mockResolvedValueOnce({ turn_id: TURN_ID, status: 'running', choice: 'kb_only' })
    kbOnly.click()
    await flushPromises()
    expect(mockedPost).toHaveBeenCalledWith(`/api/v1/turns/${TURN_ID}/approval`, { choice: 'kb_only' })
    expect(store.turns[TURN_ID]!.status).toBe('running')
    expect(sheet()).toBeNull() // 乐观关弹层
    w.unmount()
  })

  it('自定义展开：note 输入 600 字（maxlength 属性 + 提交 slice 双防线）→ 计数/500 + 截断提示可见；点「两者都要」范围钮提交携带 ≤500 字 note', async () => {
    await seedAwaiting()
    const w = mountSheet()
    await flushPromises()
    document.querySelector<HTMLButtonElement>('[data-testid="approval-custom-toggle"]')!.click()
    await flushPromises()
    const note = document.querySelector<HTMLTextAreaElement>('[data-testid="approval-note"]')!
    expect(note.maxLength).toBe(500)
    note.value = '长'.repeat(600)
    note.dispatchEvent(new Event('input'))
    await flushPromises()
    // 截断提示在 length ≥500 可见（maxlength 的 DOM 截断行为随宿主环境——提交侧 slice 兜底保证 ≤500）；
    // 计数断言须在提交前——成功提交即乐观关弹层（面板随 v-if 卸载）
    expect(document.querySelector('[data-testid="approval-note-limit"]')).not.toBeNull()
    expect(document.querySelector('[data-testid="approval-note-count"]')!.textContent!.endsWith('/500')).toBe(true)
    const both = document.querySelector<HTMLButtonElement>(
      '[data-testid="approval-custom"] [data-testid="approval-custom-choice-both"]',
    )!
    mockedPost.mockResolvedValueOnce({ turn_id: TURN_ID, status: 'running', choice: 'both' })
    both.click()
    await flushPromises()
    expect(mockedPost).toHaveBeenCalledWith(`/api/v1/turns/${TURN_ID}/approval`, { choice: 'both', note: '长'.repeat(500) })
    w.unmount()
  })

  it('自定义范围钮 aria-pressed 反映真实所选（批次 3 评审 L5）：缺省「两者都要」aria-pressed=true；提交失败（弹层保留）后所选钮 aria-pressed 真实翻转；customOpen 时与主选项钮 testid 不重复', async () => {
    await seedAwaiting()
    const w = mountSheet()
    await flushPromises()
    document.querySelector<HTMLButtonElement>('[data-testid="approval-custom-toggle"]')!.click()
    await flushPromises()
    // testid 命名空间分离：主组恒 1 个 kb_only，自定义组走 approval-custom-choice-*
    expect(document.querySelectorAll('[data-testid="approval-choice-kb_only"]').length).toBe(1)
    expect(document.querySelectorAll('[data-testid="approval-custom-choice-kb_only"]').length).toBe(1)
    const pressed = (sel: string): string | null =>
      document.querySelector<HTMLButtonElement>(`[data-testid="${sel}"]`)!.getAttribute('aria-pressed')
    expect(pressed('approval-custom-choice-both')).toBe('true') // 缺省两者（契约「范围选择缺省两者」）
    expect(pressed('approval-custom-choice-kb_only')).toBe('false')
    normalizeState.code = 'network_error'
    normalizeState.message = '网络异常，请稍后重试'
    mockedPost.mockRejectedValueOnce(new Error('network'))
    document.querySelector<HTMLButtonElement>('[data-testid="approval-custom-choice-kb_only"]')!.click()
    await flushPromises()
    // 失败路径弹层保留：aria-pressed 反映真实所选（用户点的是 kb_only，不是缺省 both）
    expect(pressed('approval-custom-choice-kb_only')).toBe('true')
    expect(pressed('approval-custom-choice-both')).toBe('false')
    w.unmount()
  })
})

describe('倒计时（服务端 expires_at 为准，前端仅展示——零本地超时动作）', () => {
  it('剩余 >15s：无提示条；推进至剩 14s：role=status 提示条可见（壳承载）；超过点：提示隐藏且不触发任何取消/裁决请求', async () => {
    await seedAwaiting()
    const w = mountSheet()
    await flushPromises()
    expect(document.querySelector('[data-testid="hitl-timeout-hint"]')).toBeNull() // 剩 120s >15
    vi.setSystemTime(new Date('2026-11-01T08:13:52Z')) // 剩 14s
    await flushPromises()
    vi.advanceTimersByTime(1100) // 驱动 tick interval
    await flushPromises()
    const hint = document.querySelector('[data-testid="hitl-timeout-hint"]')
    expect(hint).not.toBeNull()
    expect(hint!.getAttribute('role')).toBe('status')
    expect(hint!.textContent).toContain('即将按『两者都要』继续')
    vi.setSystemTime(new Date('2026-11-01T08:14:10Z')) // 超过点 4s
    await flushPromises()
    vi.advanceTimersByTime(1100)
    await flushPromises()
    expect(document.querySelector('[data-testid="hitl-timeout-hint"]')).toBeNull()
    // 零本地超时动作：cancel（/cancel）与 approve（/approval）均未被调用
    expect(mockedPost).not.toHaveBeenCalledWith(`/api/v1/turns/${TURN_ID}/cancel`)
    expect(mockedPost).not.toHaveBeenCalledWith(`/api/v1/turns/${TURN_ID}/approval`, expect.anything())
    w.unmount()
  })
})

describe('关闭=canceled(user) 确认与错误态（409008/网络错）', () => {
  it('「取消本轮」弱钮 → 壳关闭确认 → 确认后 store.cancelApproval：POST /cancel + 本地立即收口（status=canceled、approval=null）+ 弹层随状态派生关闭；未确认前零 POST', async () => {
    const store = await seedAwaiting()
    const w = mountSheet()
    await flushPromises()
    await openConfirm('hitl-cancel')
    // 确认前零 cancel POST（mockedPost 此前仅承载 seedAwaiting→ask 的建轮调用）
    expect(mockedPost).not.toHaveBeenCalledWith(`/api/v1/turns/${TURN_ID}/cancel`)
    mockedPost.mockResolvedValueOnce({ turn_id: TURN_ID, status: 'canceling' })
    document.querySelector<HTMLButtonElement>('[data-testid="hitl-confirm-close"]')!.click()
    await flushPromises()
    expect(mockedPost).toHaveBeenCalledWith(`/api/v1/turns/${TURN_ID}/cancel`)
    const turn = store.turns[TURN_ID]!
    expect(turn.status).toBe('canceled')
    expect(turn.approval).toBeNull()
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(false)
    expect(sheet()).toBeNull()
    w.unmount()
  })

  it('标题行 × 同入口（aria-label=取消本轮）：确认 → POST /cancel 收口——两关闭入口统一确认机制', async () => {
    const store = await seedAwaiting()
    const w = mountSheet()
    await flushPromises()
    const close = document.querySelector<HTMLButtonElement>('[data-testid="hitl-close"]')!
    expect(close.getAttribute('aria-label')).toBe('取消本轮')
    await openConfirm('hitl-close')
    mockedPost.mockResolvedValueOnce({ turn_id: TURN_ID, status: 'canceling' })
    document.querySelector<HTMLButtonElement>('[data-testid="hitl-confirm-close"]')!.click()
    await flushPromises()
    expect(mockedPost).toHaveBeenCalledWith(`/api/v1/turns/${TURN_ID}/cancel`)
    expect(store.turns[TURN_ID]!.status).toBe('canceled')
    w.unmount()
  })

  it('确认区「继续等待」撤回：确认区消失、零 POST、弹层保留可继续裁决', async () => {
    await seedAwaiting()
    const w = mountSheet()
    await flushPromises()
    await openConfirm('hitl-cancel')
    document.querySelector<HTMLButtonElement>('[data-testid="hitl-confirm-stay"]')!.click()
    await flushPromises()
    expect(document.querySelector('[data-testid="hitl-close-confirm"]')).toBeNull()
    expect(mockedPost).not.toHaveBeenCalledWith(`/api/v1/turns/${TURN_ID}/cancel`) // 撤回零 POST
    expect(sheet()).not.toBeNull()
    w.unmount()
  })

  it('取消在途与提交共用 submitting 门（批次 3 评审 L6）：cancel POST 未决期间选项钮与两关闭入口 disabled（防双动作）；成功收口弹层关闭', async () => {
    const store = await seedAwaiting()
    const w = mountSheet()
    await flushPromises()
    let resolveCancel!: (v: unknown) => void
    mockedPost.mockImplementationOnce(() => new Promise((resolve) => { resolveCancel = resolve }))
    await openConfirm('hitl-cancel')
    document.querySelector<HTMLButtonElement>('[data-testid="hitl-confirm-close"]')!.click()
    await flushPromises()
    // 在途冻结：busy 单一来源（提交门）经壳透传——选项钮与 ×/弱取消钮全部禁用
    expect(document.querySelector<HTMLButtonElement>('[data-testid="approval-choice-kb_only"]')!.disabled).toBe(true)
    expect(document.querySelector<HTMLButtonElement>('[data-testid="hitl-close"]')!.disabled).toBe(true)
    expect(document.querySelector<HTMLButtonElement>('[data-testid="hitl-cancel"]')!.disabled).toBe(true)
    resolveCancel({ turn_id: TURN_ID, status: 'canceling' })
    await flushPromises()
    expect(mockedPost).toHaveBeenCalledWith(`/api/v1/turns/${TURN_ID}/cancel`)
    expect(store.turns[TURN_ID]!.status).toBe('canceled')
    expect(sheet()).toBeNull() // 本地立即收口 → 状态派生关闭
    w.unmount()
  })

  it('409008（另一标签页已裁决）：toast 服务端 message + reconcile 发起；弹层按权威态保留/收敛', async () => {
    await seedAwaiting()
    const w = mountSheet()
    await flushPromises()
    normalizeState.code = '409008'
    normalizeState.message = '该轮已不在待审批状态'
    mockedPost.mockRejectedValueOnce(new Error('409008'))
    mockedGet.mockResolvedValueOnce(makeDetail({ status: 'awaiting_approval', approval: { status: 'pending', verdict: 'has_hit', expires_at: EXPIRES_AT, previews: [makePreview(0)] } }))
    document.querySelector<HTMLButtonElement>('[data-testid="approval-choice-kb_only"]')!.click()
    await flushPromises()
    expect(mockedToast).toHaveBeenCalledWith('该轮已不在待审批状态')
    expect(mockedGet).toHaveBeenCalledWith(`/api/v1/turns/${TURN_ID}`) // reconcile 权威收敛
    // 409008 同走弹层内错误条（评审 L7：toast 之外 role=alert 同步呈现）
    const err = document.querySelector('[data-testid="hitl-error"]')
    expect(err).not.toBeNull()
    expect(err!.textContent).toContain('该轮已不在待审批状态')
    // 权威态仍 awaiting+pending → 弹层保留可重试（提交中禁用已复位）
    expect(sheet()).not.toBeNull()
    const kbOnly = document.querySelector<HTMLButtonElement>('[data-testid="approval-choice-kb_only"]')!
    expect(kbOnly.disabled).toBe(false)
    w.unmount()
  })

  it('网络错误：toast + 弹层保留（status 保持 awaiting）可重试；提交中选项钮禁用防重入、完成后恢复', async () => {
    await seedAwaiting()
    const w = mountSheet()
    await flushPromises()
    let resolvePost!: (v: unknown) => void
    mockedPost.mockImplementationOnce(() => new Promise((resolve) => { resolvePost = resolve }))
    document.querySelector<HTMLButtonElement>('[data-testid="approval-choice-kb_only"]')!.click()
    await flushPromises()
    const kbOnly = document.querySelector<HTMLButtonElement>('[data-testid="approval-choice-kb_only"]')!
    expect(kbOnly.disabled).toBe(true) // 提交中防重入（busy 经壳 slot 作用域透传）
    resolvePost({ turn_id: TURN_ID, status: 'running', choice: 'kb_only' })
    await flushPromises()
    expect(mockedToast).not.toHaveBeenCalled()
    w.unmount()

    // 失败路径：态不动、按钮恢复可重试（独立 pinia——上一段 decideApproval 成功已将该轮标入
    // 已裁决重放门〔评审 M1 语义〕，同轮重放 requested 不再置 awaiting；两段本就是独立等待期）
    setActivePinia(createPinia())
    const store = await seedAwaiting()
    const w2 = mountSheet()
    await flushPromises()
    mockedPost.mockRejectedValueOnce(new Error('network'))
    document.querySelector<HTMLButtonElement>('[data-testid="approval-choice-kb_only"]')!.click()
    await flushPromises()
    expect(mockedToast).toHaveBeenCalledWith('网络异常，请稍后重试')
    expect(store.turns[TURN_ID]!.status).toBe('awaiting_approval')
    expect(document.querySelector<HTMLButtonElement>('[data-testid="approval-choice-kb_only"]')!.disabled).toBe(false)
    w2.unmount()
  })

  it('弹层内错误条接线（批次 3 评审 L7）：网络错 → toast 之外壳 errorText（role=alert）同步呈现服务端文案；重试成功弹层随状态关闭不残留', async () => {
    await seedAwaiting()
    const w = mountSheet()
    await flushPromises()
    expect(document.querySelector('[data-testid="hitl-error"]')).toBeNull() // 初态零错误条（死面已接线）
    normalizeState.code = 'network_error'
    normalizeState.message = '网络异常，请稍后重试'
    mockedPost.mockRejectedValueOnce(new Error('network'))
    document.querySelector<HTMLButtonElement>('[data-testid="approval-choice-kb_only"]')!.click()
    await flushPromises()
    const err = document.querySelector('[data-testid="hitl-error"]')
    expect(err).not.toBeNull()
    expect(err!.getAttribute('role')).toBe('alert')
    expect(err!.textContent).toContain('网络异常，请稍后重试')
    // 重试成功：乐观置 running → 弹层随状态关闭（错误条随卸载消失）
    mockedPost.mockResolvedValueOnce({ turn_id: TURN_ID, status: 'running', choice: 'kb_only' })
    document.querySelector<HTMLButtonElement>('[data-testid="approval-choice-kb_only"]')!.click()
    await flushPromises()
    expect(sheet()).toBeNull()
    w.unmount()
  })
})

describe('可访问性（焦点管理 / aria / 键盘）', () => {
  it('open 时焦点落入面板（tabindex=-1 承接）；Esc 不触发取消（取消仅 ×/「取消本轮」两个显式入口——防误按丢轮）', async () => {
    await seedAwaiting()
    const w = mountSheet()
    await flushPromises()
    const panel = sheet() as HTMLElement
    expect(document.activeElement).toBe(panel)
    panel.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()
    expect(sheet()).not.toBeNull() // Esc 零副作用
    expect(mockedPost).not.toHaveBeenCalledWith(`/api/v1/turns/${TURN_ID}/cancel`)
    w.unmount()
  })
})
