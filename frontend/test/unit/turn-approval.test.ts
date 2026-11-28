/**
 * test/unit/turn-approval.test.ts — B22 HITL 审批门 turnStore 状态机单元测试
 * （frontend-api.md §3 生命周期 15 + §3 派发表 approval_requested/approval_resolved 两行 v1.40；
 * api.md §2.13/§2.5 approval/§7.2 v1.37 消费面）。
 *
 * 覆盖：
 *   - approval_requested：置 awaiting 态 + approval=pending 载荷 + 预览缓存捕获 + 生成中集合保留
 *   - approval_resolved 四路径收口（submit/timeout → running；cancel/conflict → canceled 终态）
 *   - Last-Event-ID 重连重放语义：store 层 seq 幂等门（同连接重放丢弃 / 全量重放覆盖式刷新幂等 /
 *     resolved 后重放 requested 不回退已裁决形态）
 *   - decideApproval（§2.13）：成功乐观置 running 不等 SSE / 409008 → toast + reconcile /
 *     网络错误 → toast + 弹层保留（态不动）
 *   - reconcile 对账 2.5 approval（刷新恢复只依赖 2.5）：pending 重开弹层形态 / approved 回看态 /
 *     canceled 键省略 → null / awaiting_ms 投影 / 预览缓存流式域保留先例
 *
 * REST（@/api/client）与 SSE 传输层以 vi.mock 替身注入——手法同 turn-store.test.ts。
 */
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { apiGet, apiPost } from '@/api/client'
import { toast } from '@/composables/toast'
import { peekTurnStream, subscribeTurn } from '@/sse/turnStream'
import type { TurnStreamController } from '@/sse/turnStream'
import { useTurnStore } from '@/stores/turn'
import type { ApprovalPreview, SseWireEvent, TurnDetail } from '@/types/api'

const THREAD_ID = '7d2f0a1e-1c3b-4e5a-9d8f-0a1b2c3d4e5f'
const TURN_ID = 'c31b9d74-2a41-4f6e-b3c2-5d6e7f809a1b'
const DOC_ID = '9a77f0e1-1c3b-4e5a-9d8f-0a1b2c3d4e5f'
const CHUNK_ID = '5c2e8a3b-2d4f-4a6b-8c9d-1e2f3a4b5c6d'

/** normalizeError 归一结果可调（decideApproval 错误分支按 code 分流——409008 reconcile / 网络错弹层保留） */
const normalizeState = vi.hoisted(() => ({ code: 'network_error', message: '网络异常，请稍后重试' }))

vi.mock('@/api/client', () => ({
  apiPost: vi.fn(),
  apiGet: vi.fn(),
  normalizeError: () => ({
    name: 'ApiError',
    code: normalizeState.code,
    message: normalizeState.message,
  }),
}))

vi.mock('@/sse/turnStream', () => ({
  subscribeTurn: vi.fn(),
  closeTurnStream: vi.fn(),
  peekTurnStream: vi.fn(),
}))

vi.mock('@/composables/toast', () => ({
  toast: vi.fn(),
}))

const mockedPost = vi.mocked(apiPost)
const mockedGet = vi.mocked(apiGet)
const mockedSubscribe = vi.mocked(subscribeTurn)
const mockedPeek = vi.mocked(peekTurnStream)
const mockedToast = vi.mocked(toast)

function fakeController(): TurnStreamController {
  return { turnId: TURN_ID, state: 'connecting', closed: false, close: vi.fn() }
}

/** 预览条目构造（api.md §7.4 previews 六键形态） */
function makePreview(overrides: Partial<ApprovalPreview> = {}): ApprovalPreview {
  return {
    kb_doc_id: DOC_ID,
    chunk_id: CHUNK_ID,
    title: '内部设计文档-检索融合.md',
    heading_path: ['知识库', '检索融合', 'RRF 融合'],
    excerpt: 'KB 候选与网络候选合并后统一 rerank…（≤200 字符，服务端截断）',
    query_index: 0,
    ...overrides,
  }
}

/** approval_requested 线格 payload（§7.2：verdict/expires_at/previews） */
const REQ_PAYLOAD = {
  verdict: 'has_hit' as const,
  expires_at: '2026-11-01T08:14:06Z',
  previews: [makePreview()],
}

/** approval_resolved 线格 payload（§7.2：choice?/decided_by/resolved_by/next_stage?） */
function resolvedPayload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return { choice: 'kb_only', decided_by: 'user', resolved_by: 'submit', next_stage: 'search', ...overrides }
}

/** wire 事件构造（id=seq；测试 fixture 单一 as 收窄，§8.9①） */
function wire(id: number, event: SseWireEvent['event'], payload: Record<string, unknown>): SseWireEvent {
  return {
    id,
    event,
    data: { seq: id, turn_id: TURN_ID, ts: '2026-11-01T08:12:00Z', ...payload },
  } as SseWireEvent
}

/** 2.5 TurnDetail（approval/awaiting_ms 为 v1.37 可选键，overrides 注入） */
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

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  sessionStorage.clear()
  vi.clearAllMocks()
  normalizeState.code = 'network_error'
  normalizeState.message = '网络异常，请稍后重试'
  mockedSubscribe.mockImplementation(() => fakeController())
  mockedPeek.mockReturnValue(undefined)
})

/**
 * 种子一个 running 轮（ask 路径：POST 返回 → seedTurnState + markGenerating(running) + subscribe），
 * 返回 store——awaiting 态在 running 轮上转入（§7.4：rewrite done 后门判定弹窗）。
 */
async function seedRunningTurn(): Promise<ReturnType<typeof useTurnStore>> {
  mockedPost.mockResolvedValueOnce({
    thread_id: THREAD_ID,
    turn_id: TURN_ID,
    status: 'running',
  })
  const store = useTurnStore()
  await store.ask('KB 里怎么融合检索？', 'fast')
  return store
}

describe('approval_requested（§3 派发表 v1.40：置 awaiting 态）', () => {
  it('置 approval=pending 形态（verdict/expires_at/previews 全量）+ status=awaiting_approval + 预览缓存捕获 + lastSeq 推进', async () => {
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    const turn = store.turns[TURN_ID]!
    expect(turn.status).toBe('awaiting_approval')
    expect(turn.approval).toEqual({
      status: 'pending',
      verdict: 'has_hit',
      expires_at: REQ_PAYLOAD.expires_at,
      previews: REQ_PAYLOAD.previews,
    })
    expect(turn.approvalPreviews).toEqual(REQ_PAYLOAD.previews)
    expect(turn.lastSeq).toBe(5)
  })

  it('时间线不新增节点（rewrite 节点已 done、search 未开始——§3 派发表「时间线不新增节点」）', async () => {
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(1, 'stage', { stage_key: 'rewrite', status: 'done' }))
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    expect(store.turns[TURN_ID]!.stages).toHaveLength(1)
    expect(store.turns[TURN_ID]!.stages[0]!.stage_key).toBe('rewrite')
  })

  it('生成中集合保留（awaiting 计入生成中——markGenerating v1.40 语义扩，侧栏指示器审批等待期不消失）', async () => {
    const store = await seedRunningTurn()
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(true) // running 轮已计入
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(true)
  })

  it('同连接重放（seq ≤ lastSeq）被 store 幂等门丢弃：approval 载荷不回退、lastSeq 不回退', async () => {
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD)) // 重放同帧
    const turn = store.turns[TURN_ID]!
    expect(turn.status).toBe('awaiting_approval')
    expect(turn.lastSeq).toBe(5)
    expect(turn.approval).toEqual({
      status: 'pending',
      verdict: 'has_hit',
      expires_at: REQ_PAYLOAD.expires_at,
      previews: REQ_PAYLOAD.previews,
    })
  })

  it('重复帧到达走 seq 幂等门丢弃（终轮评审 M 标题勘正：同 store 实例 seq5 ≤ lastSeq 走门丢弃路径，非覆盖式刷新——刷新路径的覆盖收敛由「重放门」describe 首用例承载）：pending 形态与预览缓存零变化（弹层可见性状态派生幂等——不重复弹）', async () => {
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    // 新连接全量重放（seq 门 fromSeq=0 放行）→ 以最新 payload 覆盖式刷新（§3 派发表「已开则以最新 payload 覆盖式刷新」）
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    const turn = store.turns[TURN_ID]!
    expect(turn.status).toBe('awaiting_approval')
    expect(turn.approval?.status).toBe('pending')
    expect(turn.approvalPreviews).toEqual(REQ_PAYLOAD.previews)
  })
})

describe('approval_resolved（四路径收口）', () => {
  it('submit：status→running + approval 置已裁决形态（choice/decided_by=user）+ 预览缓存保留（回看折叠条展开数据源）+ 生成中集合保留；零 toast（乐观 UI 已即时反馈）', async () => {
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    store.applyEvent(TURN_ID, wire(6, 'approval_resolved', resolvedPayload()))
    const turn = store.turns[TURN_ID]!
    expect(turn.status).toBe('running')
    expect(turn.approval).toEqual({ status: 'approved', choice: 'kb_only', decided_by: 'user' })
    expect(turn.approvalPreviews).toEqual(REQ_PAYLOAD.previews)
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(true)
    expect(mockedToast).not.toHaveBeenCalled()
  })

  it('timeout（choice=both——on_timeout=hybrid 缺省续流，api.md §7.4:1082）：status→running + decided_by=timeout + toast 轻提示「已按『两者都要』继续」（§3 派发表 approval_resolved 行 v1.40）——本层零本地超时动作（判定权在服务端）', async () => {
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    store.applyEvent(TURN_ID, wire(6, 'approval_resolved', resolvedPayload({ choice: 'both', decided_by: 'timeout', resolved_by: 'timeout' })))
    const turn = store.turns[TURN_ID]!
    expect(turn.status).toBe('running')
    expect(turn.approval).toEqual({ status: 'approved', choice: 'both', decided_by: 'timeout' })
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(true)
    expect(mockedToast).toHaveBeenCalledTimes(1)
    expect(mockedToast).toHaveBeenCalledWith('已按『两者都要』继续')
  })

  it('timeout 无 choice（on_timeout=cancel 静默收尾——后端 collect_expired cancel 分支 resolved 无 choice 键）：零本地置位（status 保持 awaiting + approval 保持 pending + 生成中集合保留）——done(finish=canceled, cancel_reason=timeout) 紧随其后权威收口', async () => {
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    store.applyEvent(TURN_ID, wire(6, 'approval_resolved', resolvedPayload({ choice: undefined, decided_by: 'timeout', resolved_by: 'timeout', next_stage: undefined })))
    const turn = store.turns[TURN_ID]!
    expect(turn.status).toBe('awaiting_approval')
    expect(turn.approval?.status).toBe('pending')
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(true)
    expect(mockedToast).not.toHaveBeenCalled()
    store.applyEvent(TURN_ID, wire(7, 'done', { finish: 'canceled', cancel_reason: 'timeout', source_count: 0, memory_saved: 0, duration_ms: 0 }))
    expect(store.turns[TURN_ID]!.status).toBe('canceled')
    expect(store.turns[TURN_ID]!.cancelReason).toBe('timeout')
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(false)
  })

  it('cancel：canceled 终态收口（status=canceled + approval/previews 清 null + 生成中集合移除）；cancelReason 由随后 done(cancel,user) 权威补写', async () => {
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    // cancel 收口无 choice——键省略（§7.2），decided_by=system
    store.applyEvent(TURN_ID, wire(6, 'approval_resolved', resolvedPayload({ choice: undefined, decided_by: 'system', resolved_by: 'cancel', next_stage: undefined })))
    const turn = store.turns[TURN_ID]!
    expect(turn.status).toBe('canceled')
    expect(turn.approval).toBeNull()
    expect(turn.approvalPreviews).toBeNull()
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(false)
    expect(turn.cancelReason).toBeNull() // 待 done(cancel) 补写
    store.applyEvent(TURN_ID, wire(7, 'done', { finish: 'canceled', cancel_reason: 'user', source_count: 0, memory_saved: 0, duration_ms: 0 }))
    expect(store.turns[TURN_ID]!.cancelReason).toBe('user')
  })

  it('conflict（interrupt/rollback 直接收尾）：同 canceled 收口（轮随后 done(canceled,superseded|rolled_back)）', async () => {
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    store.applyEvent(TURN_ID, wire(6, 'approval_resolved', resolvedPayload({ choice: undefined, decided_by: 'system', resolved_by: 'conflict', next_stage: undefined })))
    const turn = store.turns[TURN_ID]!
    expect(turn.status).toBe('canceled')
    expect(turn.approval).toBeNull()
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(false)
  })

  it('重放乱序防护：resolved(seq6) 后重放 requested(seq5) 被幂等门丢弃——approval 保持已裁决形态不回退 pending（Last-Event-ID 重连重放语义）', async () => {
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    store.applyEvent(TURN_ID, wire(6, 'approval_resolved', resolvedPayload()))
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD)) // 重放旧帧
    const turn = store.turns[TURN_ID]!
    expect(turn.status).toBe('running') // 不回退 awaiting
    expect(turn.approval?.status).toBe('approved') // 不回退 pending
  })
})

describe('decideApproval（§2.13：POST /turns/{id}/approval）', () => {
  it('成功：POST body={choice}（note 空串不携带）；本地立即 status=running + approval 置 approved(user) 形态不等 SSE（乐观 UI）；生成中集合保留', async () => {
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    mockedPost.mockResolvedValueOnce({ turn_id: TURN_ID, status: 'running', choice: 'kb_only' })
    await store.decideApproval(TURN_ID, 'kb_only')
    expect(mockedPost).toHaveBeenCalledWith(`/api/v1/turns/${TURN_ID}/approval`, { choice: 'kb_only' })
    const turn = store.turns[TURN_ID]!
    expect(turn.status).toBe('running')
    expect(turn.approval).toEqual({ status: 'approved', choice: 'kb_only', decided_by: 'user' })
    expect(turn.approvalPreviews).toEqual(REQ_PAYLOAD.previews) // 回看折叠条立即可展开
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(true)
    expect(mockedToast).not.toHaveBeenCalled()
  })

  it('成功携带 note（非空串随 POST body 下发；§2.13 note 任意 choice 可附带）', async () => {
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    mockedPost.mockResolvedValueOnce({ turn_id: TURN_ID, status: 'running', choice: 'custom' })
    await store.decideApproval(TURN_ID, 'custom', '只看 2024 年之后的资料')
    expect(mockedPost).toHaveBeenCalledWith(`/api/v1/turns/${TURN_ID}/approval`, { choice: 'custom', note: '只看 2024 年之后的资料' })
    expect(store.turns[TURN_ID]!.approval).toEqual({ status: 'approved', choice: 'custom', decided_by: 'user' })
  })

  it('409008（另一标签页已裁决/已超时收口）：toast 服务端 message + reconcile 发起（2.5 权威态收敛）+ 失败路径不置位（status 保持 awaiting）', async () => {
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    normalizeState.code = '409008'
    normalizeState.message = '该轮已不在待审批状态'
    mockedPost.mockRejectedValueOnce(new Error('409008'))
    mockedGet.mockResolvedValueOnce(makeDetail({ status: 'running' })) // reconcile 权威：另一标签页已裁决 → running
    await store.decideApproval(TURN_ID, 'kb_only')
    expect(mockedToast).toHaveBeenCalledWith('该轮已不在待审批状态')
    expect(mockedGet).toHaveBeenCalledWith(`/api/v1/turns/${TURN_ID}`)
    // 失败路径不乐观置位；reconcile 已以 2.5 权威收敛 status=running（弹层随权威态自动关）
    expect(store.turns[TURN_ID]!.status).toBe('running')
    expect(store.turns[TURN_ID]!.approval).toBeNull() // 非 awaiting 轮 2.5 approval 键省略
  })

  it('网络错误：toast + status 保持 awaiting + approval 保持 pending（弹层保留可重试，不发起 reconcile）', async () => {
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    normalizeState.code = 'network_error'
    normalizeState.message = '网络异常，请稍后重试'
    mockedPost.mockRejectedValueOnce(new Error('network'))
    await store.decideApproval(TURN_ID, 'kb_only')
    expect(mockedToast).toHaveBeenCalledWith('网络异常，请稍后重试')
    expect(mockedGet).not.toHaveBeenCalled()
    const turn = store.turns[TURN_ID]!
    expect(turn.status).toBe('awaiting_approval')
    expect(turn.approval?.status).toBe('pending')
  })
})

describe('cancelApproval（v1.40：弹层「取消本轮」落点 = 复用 cancel 走 §2.7 awaiting 分支——canceled(user) 不进历史）', () => {
  it('成功：POST /cancel + 本地立即收口（status=canceled + approval/previews 清 null + 生成中集合移除）；done(cancel,user) 随后幂等补写 cancelReason', async () => {
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    mockedPost.mockResolvedValueOnce({ turn_id: TURN_ID, status: 'canceling' })
    await store.cancelApproval(TURN_ID)
    expect(mockedPost).toHaveBeenCalledWith(`/api/v1/turns/${TURN_ID}/cancel`)
    const turn = store.turns[TURN_ID]!
    expect(turn.approval).toBeNull()
    expect(turn.approvalPreviews).toBeNull()
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(false)
    // approval_resolved(resolved_by=cancel) 与 done(canceled,user) 随后到达幂等收敛
    store.applyEvent(TURN_ID, wire(6, 'approval_resolved', resolvedPayload({ choice: undefined, decided_by: 'system', resolved_by: 'cancel', next_stage: undefined })))
    store.applyEvent(TURN_ID, wire(7, 'done', { finish: 'canceled', cancel_reason: 'user', source_count: 0, memory_saved: 0, duration_ms: 0 }))
    expect(store.turns[TURN_ID]!.status).toBe('canceled')
    expect(store.turns[TURN_ID]!.cancelReason).toBe('user')
  })

  it('失败（网络错）：toast、态不动（status 保持 awaiting + approval 保持 pending）——弹层保留可重试', async () => {
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    mockedPost.mockRejectedValueOnce(new Error('network'))
    await store.cancelApproval(TURN_ID)
    expect(mockedToast).toHaveBeenCalledWith('网络异常，请稍后重试')
    const turn = store.turns[TURN_ID]!
    expect(turn.status).toBe('awaiting_approval')
    expect(turn.approval?.status).toBe('pending')
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(true)
  })
})

describe('reconcile 对账 approval（2.5 为准源——刷新恢复只依赖 2.5，§3 生命周期 15）', () => {
  it('approval=pending：投影 pending 形态 + 预览缓存全量捕获（弹层重开数据面）+ awaiting_ms 对账 + 生成中集合保留（markGenerating awaiting 计入）', async () => {
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(1, 'stage', { stage_key: 'rewrite', status: 'running' }))
    const previews = [makePreview(), makePreview({ chunk_id: 'aaaaaaaa-2d4f-4a6b-8c9d-1e2f3a4b5c6d', query_index: 1 })]
    mockedGet.mockResolvedValueOnce(makeDetail({
      status: 'awaiting_approval',
      approval: { status: 'pending', verdict: 'has_hit', expires_at: '2026-11-01T08:14:06Z', previews },
      awaiting_ms: 42000,
    }))
    await store.reconcile(TURN_ID)
    const turn = store.turns[TURN_ID]!
    expect(turn.status).toBe('awaiting_approval')
    expect(turn.approval?.status).toBe('pending')
    expect(turn.approvalPreviews).toEqual(previews)
    expect(turn.awaitingMs).toBe(42000)
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(true)
  })

  it('approval=approved：回看态投影（choice/note/decided_by 权威）+ 本地预览缓存保留（2.5 approved 无 previews——沿 thinking_md/compactSeq 流式域保留先例）', async () => {
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    const cached = store.turns[TURN_ID]!.approvalPreviews
    mockedGet.mockResolvedValueOnce(makeDetail({
      status: 'running',
      approval: { status: 'approved', choice: 'kb_only', note: '只看 2024 年之后的资料', decided_by: 'user' },
    }))
    await store.reconcile(TURN_ID)
    const turn = store.turns[TURN_ID]!
    expect(turn.approval).toEqual({ status: 'approved', choice: 'kb_only', note: '只看 2024 年之后的资料', decided_by: 'user' })
    expect(turn.approvalPreviews).toEqual(cached) // 不被对账清空
    expect(turn.awaitingMs).toBeNull() // 非零才携带——键省略投影 null
  })

  it('非门轮 approval 键省略：approval=null（api.md §2.5——非门轮键整体省略不置 null 语义的 store 投影面）', async () => {
    const store = await seedRunningTurn()
    mockedGet.mockResolvedValueOnce(makeDetail({ status: 'running' }))
    await store.reconcile(TURN_ID)
    expect(store.turns[TURN_ID]!.approval).toBeNull()
    expect(store.turns[TURN_ID]!.approvalPreviews).toBeNull()
  })

  it('approval 键省略且本地预览缓存有值：对称清理 approvalPreviews=null（终轮评审 M 修复——非 pending 且非 approved 形态清陈旧缓存，与 approval=null 同步）', async () => {
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    expect(store.turns[TURN_ID]!.approvalPreviews).toEqual(REQ_PAYLOAD.previews) // 前置：缓存有值
    mockedGet.mockResolvedValueOnce(makeDetail({ status: 'running' })) // 2.5 approval 键省略
    await store.reconcile(TURN_ID)
    expect(store.turns[TURN_ID]!.approval).toBeNull()
    expect(store.turns[TURN_ID]!.approvalPreviews).toBeNull() // 陈旧缓存不残留（回看折叠条无数据源即不渲染）
  })

  it('canceled 轮 approval 键整体省略（v1.38 钉）：approval=null——canceled 轮回看面无审批折叠条', async () => {
    const store = useTurnStore()
    mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID, status: 'awaiting_approval' })
    await store.ask('KB 里怎么融合检索？', 'fast')
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    mockedGet.mockResolvedValueOnce(makeDetail({ status: 'canceled', cancel_reason: 'user' }))
    await store.reconcile(TURN_ID)
    const turn = store.turns[TURN_ID]!
    expect(turn.status).toBe('canceled')
    expect(turn.approval).toBeNull()
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(false)
  })

  it('awaiting_ms 边界（第四段补）：键缺省与 0 值均投影 null（api.md §2.5 非零才携带口径）+ rewrittenQueries 权威捕获', async () => {
    const store = await seedRunningTurn()
    // 键缺省 → null
    mockedGet.mockResolvedValueOnce(makeDetail({ status: 'awaiting_approval', approval: { status: 'pending', verdict: 'has_hit', expires_at: REQ_PAYLOAD.expires_at, previews: [makePreview()] } }))
    await store.reconcile(TURN_ID)
    expect(store.turns[TURN_ID]!.awaitingMs).toBeNull()
    // 0 值（键携带但零——非零才携带口径同样投影 null）
    mockedGet.mockResolvedValueOnce(makeDetail({
      status: 'awaiting_approval',
      awaiting_ms: 0,
      rewritten_queries: ['子问题一', '子问题二'],
      approval: { status: 'pending', verdict: 'has_hit', expires_at: REQ_PAYLOAD.expires_at, previews: [makePreview()] },
    }))
    await store.reconcile(TURN_ID)
    expect(store.turns[TURN_ID]!.awaitingMs).toBeNull()
    // rewritten_queries 顶层权威源捕获（弹层「命中的子问题」行数据面）
    expect(store.turns[TURN_ID]!.rewrittenQueries).toEqual(['子问题一', '子问题二'])
  })
})

describe('占线判定面边界（第四段补：v1.40 两判定面互不侵入——生成中集合含 awaiting、占线面不扩）', () => {
  it('calibrateThread（2.3 校准）：awaiting_approval 计入生成中集合（刷新恢复侧栏指示器不消失）；全终态轮移除', async () => {
    mockedPost.mockResolvedValueOnce({ thread_id: THREAD_ID, turn_id: TURN_ID, status: 'running' })
    const store = useTurnStore()
    await store.ask('KB 里怎么融合检索？', 'fast')
    // 刷新恢复路径：2.3 turns[].status 含 awaiting → 生成中集合保留
    store.calibrateThread(THREAD_ID, ['awaiting_approval'])
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(true)
    // 全终态 → 移除（既有校准口径零变化）
    store.calibrateThread(THREAD_ID, ['completed'])
    expect(store.generatingThreadIds.has(THREAD_ID)).toBe(false)
  })
})

describe('重放门——已裁决轮不回退不重弹（批次 3 评审 M1+M2：阶段 2 running 刷新全量重放防护）', () => {
  it('刷新全量重放 requested：reconcile 先行恢复已裁决形态（2.5 approved）后重放 requested(seq5) 不回退 awaiting/pending——状态保持 running（弹层派生面不开）、预览缓存自重放 payload 回填', async () => {
    // 首会话：requested → resolved(submit) 收口
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    store.applyEvent(TURN_ID, wire(6, 'approval_resolved', resolvedPayload()))
    // 刷新：新 store（内存态清零）→ reconcile 先行（2.5 approved 权威恢复）→ 订阅全量重放
    setActivePinia(createPinia())
    const store2 = useTurnStore()
    mockedGet.mockResolvedValueOnce(makeDetail({
      status: 'running',
      approval: { status: 'approved', choice: 'kb_only', decided_by: 'user' },
    }))
    await store2.reconcile(TURN_ID)
    expect(store2.turns[TURN_ID]!.status).toBe('running')
    expect(store2.turns[TURN_ID]!.approval?.status).toBe('approved')
    expect(store2.turns[TURN_ID]!.approvalPreviews).toBeNull() // 刷新清空流式域缓存
    // 全量重放（刷新路径 seq 门放行）：requested(seq5) 命中已裁决门
    store2.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    const turn = store2.turns[TURN_ID]!
    expect(turn.status).toBe('running') // 不回退 awaiting——弹层状态派生面不开
    expect(turn.approval?.status).toBe('approved') // 不回退 pending
    expect(turn.approvalPreviews).toEqual(REQ_PAYLOAD.previews) // 回看折叠条展开数据源自重放帧回填
    // 随后 resolved(seq6) 重放幂等收敛（approved 形态以服务端权威收敛、不回退）
    store2.applyEvent(TURN_ID, wire(6, 'approval_resolved', resolvedPayload()))
    expect(store2.turns[TURN_ID]!.status).toBe('running')
    expect(store2.turns[TURN_ID]!.approval).toEqual({ status: 'approved', choice: 'kb_only', decided_by: 'user' })
  })

  it('带 note 裁决轮刷新全量重放：resolved(seq6) 重放收敛保留 2.5 权威恢复的 note（终轮评审 M 修复——note 唯一权威源是 2.5，SSE resolved payload 无 note 键；ApprovalRecall 补充指令行 running 中刷新不闪没）', async () => {
    // 首会话：requested → resolved(submit) 收口
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    store.applyEvent(TURN_ID, wire(6, 'approval_resolved', resolvedPayload()))
    // 刷新：reconcile 先行——2.5 approved 携带 note 权威恢复（补充指令唯一跨刷新证据源）
    setActivePinia(createPinia())
    const store2 = useTurnStore()
    mockedGet.mockResolvedValueOnce(makeDetail({
      status: 'running',
      approval: { status: 'approved', choice: 'kb_only', note: '只看 2024 年之后的资料', decided_by: 'user' },
    }))
    await store2.reconcile(TURN_ID)
    expect(store2.turns[TURN_ID]!.approval).toEqual({
      status: 'approved',
      choice: 'kb_only',
      note: '只看 2024 年之后的资料',
      decided_by: 'user',
    })
    // 全量重放：requested(seq5) 命中重放门（note 不动）→ resolved(seq6) 收敛——既有 note 保留不抹掉
    store2.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    const midApproval = store2.turns[TURN_ID]!.approval
    expect(midApproval?.status === 'approved' && midApproval.note).toBe('只看 2024 年之后的资料')
    store2.applyEvent(TURN_ID, wire(6, 'approval_resolved', resolvedPayload()))
    expect(store2.turns[TURN_ID]!.status).toBe('running')
    expect(store2.turns[TURN_ID]!.approval).toEqual({
      status: 'approved',
      choice: 'kb_only',
      note: '只看 2024 年之后的资料',
      decided_by: 'user',
    })
  })

  it('timeout 收口轮刷新全量重放：toast 只弹一次（首会话实时帧弹一次；刷新重放 requested+resolved 不再重弹）', async () => {
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    store.applyEvent(TURN_ID, wire(6, 'approval_resolved', resolvedPayload({ choice: 'both', decided_by: 'timeout', resolved_by: 'timeout' })))
    expect(mockedToast).toHaveBeenCalledTimes(1) // 首会话实时帧：轻提示恰好一次
    // 刷新：reconcile 先行（2.5 approved decided_by=timeout 重建已裁决标记）→ 全量重放
    setActivePinia(createPinia())
    const store2 = useTurnStore()
    mockedGet.mockResolvedValueOnce(makeDetail({
      status: 'running',
      approval: { status: 'approved', choice: 'both', decided_by: 'timeout' },
    }))
    await store2.reconcile(TURN_ID)
    store2.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    store2.applyEvent(TURN_ID, wire(6, 'approval_resolved', resolvedPayload({ choice: 'both', decided_by: 'timeout', resolved_by: 'timeout' })))
    expect(store2.turns[TURN_ID]!.status).toBe('running') // 重放不回退 awaiting
    expect(mockedToast).toHaveBeenCalledTimes(1) // 仍只一次——重放帧不再弹
  })

  it('decideApproval 乐观裁决后刷新全量重放：同门防护（reconcile 以 2.5 approved 重建标记 → requested 不回退）', async () => {
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    mockedPost.mockResolvedValueOnce({ turn_id: TURN_ID, status: 'running', choice: 'web_only' })
    await store.decideApproval(TURN_ID, 'web_only')
    setActivePinia(createPinia())
    const store2 = useTurnStore()
    mockedGet.mockResolvedValueOnce(makeDetail({
      status: 'running',
      approval: { status: 'approved', choice: 'web_only', decided_by: 'user' },
    }))
    await store2.reconcile(TURN_ID)
    store2.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    const turn = store2.turns[TURN_ID]!
    expect(turn.status).toBe('running')
    expect(turn.approval).toEqual({ status: 'approved', choice: 'web_only', decided_by: 'user' })
  })

  it('终态轮陈旧重放帧防御：canceled 轮上重放 requested(seq5) 不复活审批 UI（状态不回退、approval 保持 null）', async () => {
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    store.applyEvent(TURN_ID, wire(6, 'approval_resolved', resolvedPayload({ choice: undefined, decided_by: 'system', resolved_by: 'cancel', next_stage: undefined })))
    setActivePinia(createPinia())
    const store2 = useTurnStore()
    mockedGet.mockResolvedValueOnce(makeDetail({ status: 'canceled', cancel_reason: 'user' }))
    await store2.reconcile(TURN_ID)
    // canceled 轮不重订阅（AnswerView 只订阅 running/queued/awaiting）——防御面：陈旧重放帧若达亦不得复活
    store2.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    const turn = store2.turns[TURN_ID]!
    expect(turn.status).toBe('canceled')
    expect(turn.approval).toBeNull()
  })

  it('终态轮陈旧 resolved 重放防御（终轮评审 M 修复，与 requested ③对称）：canceled 轮重放 resolved(submit)（seq 门放行）不把终态拉回 running', async () => {
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    store.applyEvent(TURN_ID, wire(6, 'approval_resolved', resolvedPayload({ choice: undefined, decided_by: 'system', resolved_by: 'cancel', next_stage: undefined })))
    setActivePinia(createPinia())
    const store2 = useTurnStore()
    mockedGet.mockResolvedValueOnce(makeDetail({ status: 'canceled', cancel_reason: 'user' }))
    await store2.reconcile(TURN_ID)
    // 防御面：canceled 轮不重订阅（AnswerView 只订阅 running/queued/awaiting）——陈旧 resolved
    // 帧若达（reconcile 后 lastSeq=0、seq 门放行）不得回写 status 复活运行态
    store2.applyEvent(TURN_ID, wire(6, 'approval_resolved', resolvedPayload()))
    expect(store2.turns[TURN_ID]!.status).toBe('canceled')
    expect(store2.turns[TURN_ID]!.cancelReason).toBe('user') // done 权威补写值不被重放帧扰动
  })

  it('生命周期 5 时序互补断言（评审 L10）：reconcile 未种子的轮 subscribe 零订阅；reconcile 先行后订阅成立且 fromSeq=0（刷新全量重放路径；终轮评审 M 补 fromSeq 断言）', async () => {
    const store = useTurnStore()
    store.subscribe(TURN_ID)
    expect(mockedSubscribe).not.toHaveBeenCalled() // 未种子化零订阅（编排层须先 REST 再订阅）
    mockedGet.mockResolvedValueOnce(makeDetail({
      status: 'awaiting_approval',
      approval: { status: 'pending', verdict: 'has_hit', expires_at: REQ_PAYLOAD.expires_at, previews: [makePreview()] },
    }))
    await store.reconcile(TURN_ID)
    store.subscribe(TURN_ID)
    expect(mockedSubscribe).toHaveBeenCalledTimes(1)
    expect(mockedSubscribe.mock.calls[0]![0].turnId).toBe(TURN_ID)
    // 刷新全量重放路径：内存 lastSeq 与落盘游标均空 → resolveFromSeq 返回基线 0（全量重放）
    expect(mockedSubscribe.mock.calls[0]![0].fromSeq).toBe(0)
  })

  it('同页已流式路径 fromSeq=基线（终轮评审 M 补 resolveFromSeq ④断言）：内存游标即重放门基线——增量续传', async () => {
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(3, 'stage', { stage_key: 'rewrite', status: 'done' }))
    expect(store.turns[TURN_ID]!.lastSeq).toBe(3)
    store.subscribe(TURN_ID) // ask 内部已订阅一次（calls[0]）——此处为重建连接的第二跳（calls[1]）
    expect(mockedSubscribe).toHaveBeenCalledTimes(2)
    // 基线 = max(sessionStorage 游标, 内存 lastSeq) = 3（重放 ≤3 帧被 seq 门丢弃，仅应用增量）
    expect(mockedSubscribe.mock.calls[1]![0].fromSeq).toBe(3)
  })
})

describe('重连重放边界（第四段补：乐观 UI 与 SSE 权威帧收敛——Last-Event-ID 重放语义补角）', () => {
  it('decideApproval 乐观置 running 后 approval_resolved 到达：幂等收敛不回退（approved 形态以服务端 decided_by 权威收敛）；随后重放 requested(seq-1) 被 seq 门丢弃', async () => {
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    // 本地乐观：POST 成功 → running + approved(user)，不等 SSE
    mockedPost.mockResolvedValueOnce({ turn_id: TURN_ID, status: 'running', choice: 'kb_only' })
    await store.decideApproval(TURN_ID, 'kb_only')
    expect(store.turns[TURN_ID]!.status).toBe('running')
    // SSE resolved(seq6) 到达（服务端权威收口——decided_by 权威收敛、状态不回退 awaiting）
    store.applyEvent(TURN_ID, wire(6, 'approval_resolved', resolvedPayload()))
    const turn = store.turns[TURN_ID]!
    expect(turn.status).toBe('running')
    expect(turn.approval).toEqual({ status: 'approved', choice: 'kb_only', decided_by: 'user' })
    // 重放 requested(seq5) 走 seq 门丢弃（断线重连全量重放形态）——不回退 pending
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    expect(store.turns[TURN_ID]!.approval?.status).toBe('approved')
    expect(store.turns[TURN_ID]!.status).toBe('running')
  })

  it('cancelApproval 本地收口后重放 done(canceled,user) 与 resolved(cancel)：幂等（canceled 终态不回退、cancelReason 不被覆盖）', async () => {
    const store = await seedRunningTurn()
    store.applyEvent(TURN_ID, wire(5, 'approval_requested', REQ_PAYLOAD))
    mockedPost.mockResolvedValueOnce({ turn_id: TURN_ID, status: 'canceling' })
    await store.cancelApproval(TURN_ID)
    expect(store.turns[TURN_ID]!.status).toBe('canceled')
    // done(seq6) 权威补写 cancelReason
    store.applyEvent(TURN_ID, wire(6, 'done', { finish: 'canceled', cancel_reason: 'user', source_count: 0, memory_saved: 0, duration_ms: 0 }))
    expect(store.turns[TURN_ID]!.cancelReason).toBe('user')
    // 重放 resolved(cancel, seq5 旧帧) 走 seq 门丢弃（重放乱序防护对取消路径成立）
    store.applyEvent(TURN_ID, wire(5, 'approval_resolved', resolvedPayload({ choice: undefined, decided_by: 'system', resolved_by: 'cancel', next_stage: undefined })))
    const turn = store.turns[TURN_ID]!
    expect(turn.status).toBe('canceled')
    expect(turn.approval).toBeNull()
    expect(turn.cancelReason).toBe('user') // 不被重放帧回退
  })
})
