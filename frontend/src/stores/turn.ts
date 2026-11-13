/**
 * src/stores/turn.ts — turnStore（frontend-api.md §4 核心 store；setup store，§8.2）。
 *
 * 职责：
 *   - 当前线程 turns 映射：每轮 question/answer_md/thinking_md/sources/images/stages/
 *     memory_hits/compactSeq/incognito/cancelReason/followups/status/lastSeq（§4 逐字段），
 *     另附 errorMsg/degradedReason/memorySaved/compactAt 四个派生展示字段（派发表对应
 *     Banner/错误条/M5 toast/v1.4 瞬态提示的数据形态就绪）。
 *   - SSE 连接编排：ask()/followUp() POST 建轮（两段式第一段，经 client.ts 唯一出口）→
 *     subscribe() GET 订阅（第二段，经 turnStream.ts 唯一出口）；所有事件经 applyEvent()
 *     单入口进状态机（§3 派发表 / §8.7 传输与渲染解耦）。
 *   - reconcile()：REST 为准源对账（生命周期 4/5）；终态权威替换、运行中轮保留流式增量
 *     （后端 token/thinking 只发布不落库、answer_md 仅终态落库——apps/search-api/app/services/
 *     turn_service.py:21-22/:753-754，运行中 2.5 answer_md 为空串，替换会清空流式内容）。
 *   - generatingThreadIds：侧边栏生成中线程集合（生命周期 12），轮生命周期事件驱动、不轮询。
 *   - closeStream()：路由切走主动断流（生命周期 11，连接注册表保证同 turnId 至多一条连接）。
 */
import { ref } from 'vue'
import { defineStore } from 'pinia'

import { apiGet, apiPost, normalizeError } from '@/api/client'
import { toast } from '@/composables/toast'
import { closeTurnStream, subscribeTurn } from '@/sse/turnStream'
import type { TurnStreamState } from '@/sse/turnStream'
import { usePrefsStore } from '@/stores/prefs'
import type {
  AskMode,
  CancelReason,
  CancelTurnData,
  CreateThreadRequest,
  DegradedReason,
  MemoryHits,
  SseStageEvent,
  SseWireEvent,
  Source,
  StageExtra,
  StageKey,
  StageStatus,
  StartTurnData,
  TurnDetail,
  TurnImageItem,
  TurnStatus,
} from '@/types/api'

/** §3 生命周期 5：本地 last seq 的 sessionStorage 键（标签页隔离语义） */
const SEQ_STORAGE_PREFIX = 'ai-search.seq.'

/** P3：seq 持久化节流窗（streaming-buffer 参数表「落库节流」200ms；控制事件/终态豁免） */
const SEQ_PERSIST_MIN_INTERVAL_MS = 200

/** P2：对账失败重试上限（初败后再试 3 次）；重试间隔（REST 为准源的收敛窗口） */
const RECONCILE_RETRY_MAX = 3
const RECONCILE_RETRY_DELAY_MS = 1000

const TERMINAL_STATUSES: readonly TurnStatus[] = ['completed', 'degraded', 'failed', 'canceled']

function isTerminalStatus(status: TurnStatus): boolean {
  return TERMINAL_STATUSES.includes(status)
}

/** §4：sse 连接态三枚举（传输层五态 → 三态映射） */
export type SseConnectionState = 'connected' | 'reconnecting' | 'idle'

function toConnectionState(state: TurnStreamState): SseConnectionState {
  if (state === 'connected') return 'connected'
  if (state === 'reconnecting') return 'reconnecting'
  return 'idle'
}

/** 时间线节点（§3 stage 派发行：按 stage_key 就地更新的状态机节点） */
export interface TimelineStage {
  stage_key: StageKey
  status: StageStatus
  label?: string
  detail?: string
  progress?: { current: number; total?: number }
  extra?: StageExtra
}

/** 每轮状态投影（§4 turnStore 行字段 + 派生展示字段） */
export interface TurnState {
  turnId: string
  threadId: string
  mode: AskMode
  question: string
  status: TurnStatus
  /** 答案增量（token 通道；合帧后追加） */
  answer_md: string
  /** 思考流增量（thinking 通道；独立缓冲不入 answer_md，§3 v1.4 双缓冲） */
  thinking_md: string
  sources: Source[]
  images: TurnImageItem[]
  stages: TimelineStage[]
  memory_hits: MemoryHits | null
  /** 最近一次 context_compact 的 seq（重放边界，v1.4） */
  compactSeq: number
  /** 最近一次 context_compact 的 ts（「正在整理研究上下文」瞬态提示的数据形态） */
  compactAt: string | null
  /** 发起时 prefsStore + 2.5 对账双源（生命周期 7） */
  incognito: boolean | null
  cancelReason: CancelReason | null
  /** SSE followups 事件与 reconcile() 对账双源写入（v1.9） */
  followups: string[] | null
  lastSeq: number
  /** error 事件 message（错误条展示；error 非终态，§3 派发表） */
  errorMsg: string | null
  /** done(degraded) 携带（DegradedBanner 数据） */
  degradedReason: DegradedReason | null
  /** done.memory_saved（M5「已记住」toast 数据；无痕轮恒无） */
  memorySaved: number | null
  /** 轮耗时（SSE done.duration_ms / 2.5 duration_ms 双源；进行中 null——状态行「已完成 N秒」） */
  durationMs: number | null
}

function seedTurnState(init: {
  turnId: string
  threadId: string
  mode: AskMode
  question: string
  status: TurnStatus
  incognito: boolean | null
}): TurnState {
  return {
    turnId: init.turnId,
    threadId: init.threadId,
    mode: init.mode,
    question: init.question,
    status: init.status,
    answer_md: '',
    thinking_md: '',
    sources: [],
    images: [],
    stages: [],
    memory_hits: null,
    compactSeq: 0,
    compactAt: null,
    incognito: init.incognito,
    cancelReason: null,
    followups: null,
    lastSeq: 0,
    errorMsg: null,
    degradedReason: null,
    memorySaved: null,
    durationMs: null,
  }
}

function stageFromRecord(record: TurnDetail['stages'][number]): TimelineStage {
  return {
    stage_key: record.stage_key,
    status: record.status,
    label: record.label,
    ...(record.extra !== undefined ? { extra: record.extra } : {}),
  }
}

export const useTurnStore = defineStore('turn', () => {
  const turns = ref<Record<string, TurnState>>({})
  const liveTurnId = ref<string | null>(null)
  const sseState = ref<SseConnectionState>('idle')
  const generatingThreadIds = ref(new Set<string>())

  /* ---------- 生成中集合（生命周期 12：轮生命周期事件驱动，不轮询） ---------- */

  function markGenerating(threadId: string, status: TurnStatus): void {
    if (status === 'running' || status === 'queued') {
      generatingThreadIds.value.add(threadId)
    } else {
      generatingThreadIds.value.delete(threadId)
    }
  }

  /** 进入页面/切回时以 2.3 turns[].status 校准当前线程（生命周期 12 对账口径） */
  function calibrateThread(threadId: string, statuses: TurnStatus[]): void {
    markGenerating(threadId, statuses.some((s) => s === 'running' || s === 'queued') ? 'running' : 'completed')
  }

  /* ---------- seq 游标持久化（生命周期 5 形态保留；P3 写入节流） ---------- */

  /** 每 turn 上次落盘时刻（节流窗判定；turnId → epoch ms） */
  const seqPersistedAt = new Map<string, number>()

  /**
   * sessionStorage 游标写入（P3 节流）：delta 事件 200ms 窗口内至多落一次（写放大收敛——
   * 每帧一次 setItem 换成 ≤5 次/秒）；控制事件与终态（done）豁免立即落（重连续传游标与
   * 终态恢复不落后）。窗口内跳过的游标仅落后内存 lastSeq ≤200ms——续传按重放幂等门
   * （seq 去重）兜底，不产生语义偏差。
   */
  function persistSeq(turnId: string, seq: number, options: { immediate?: boolean } = {}): void {
    if (seq <= 0) return
    const now = Date.now()
    if (options.immediate === true) {
      seqPersistedAt.set(turnId, now)
    } else {
      const last = seqPersistedAt.get(turnId) ?? 0
      if (now - last < SEQ_PERSIST_MIN_INTERVAL_MS) return
      seqPersistedAt.set(turnId, now)
    }
    try {
      sessionStorage.setItem(SEQ_STORAGE_PREFIX + turnId, String(seq))
    } catch {
      // 存储不可用（隐私模式等）静默：续传退化为全量重放（幂等）
    }
  }

  function readStoredSeq(turnId: string): number {
    try {
      const raw = sessionStorage.getItem(SEQ_STORAGE_PREFIX + turnId)
      const n = Number(raw)
      return Number.isFinite(n) && n > 0 ? n : 0
    } catch {
      return 0
    }
  }

  /**
   * 订阅 seq 门基线（生命周期 5/11）。原生 EventSource 新建对象无法携带 Last-Event-ID 头
   * （浏览器仅在「同一对象」自动重连时原生携带，勿自造 header）——因此：
   *   - 本页已流式渲染过（lastSeq>0，answer_md/thinking_md 含 ≤lastSeq 增量）→ 门=lastSeq：
   *     重建连接的全量重放中被门丢弃 ≤lastSeq 部分，仅应用增量（幂等续传）。
   *   - 本页尚无流式内容（刷新/新页：lastSeq=0）→ 门=0 全量重放重建（运行中轮 REST answer_md
   *     为空串，重放完整重建；终态轮无需订阅，直接 reconcile）。契约「若刷新丢失则全量重放，
     幂等」的落地路径。
   *   - 本地游标早于 compactSeq（v1.4 重放边界）→ 门=0 放弃增量，全量对账重建。
   * sessionStorage 序列按 §3 L5 形态持久化（readStoredSeq 供游标通道落地后的接入与诊断）。
   */
  function resolveFromSeq(turn: TurnState): number {
    if (turn.lastSeq > 0 && turn.compactSeq <= turn.lastSeq) return turn.lastSeq
    return 0
  }

  /* ---------- 派发表（§3：事件 → store 单一状态机；applyEvent 唯一入口） ---------- */

  function upsertStage(turn: TurnState, data: SseStageEvent): void {
    const patch: TimelineStage = {
      stage_key: data.stage_key,
      status: data.status,
      ...(data.label !== undefined ? { label: data.label } : {}),
      ...(data.detail !== undefined ? { detail: data.detail } : {}),
      ...(data.progress !== undefined ? { progress: data.progress } : {}),
      ...(data.extra !== undefined ? { extra: data.extra } : {}),
    }
    const existing = turn.stages.find((s) => s.stage_key === data.stage_key)
    if (existing) Object.assign(existing, patch)
    else turn.stages.push(patch)
  }

  function markStageFailed(turn: TurnState, stageKey: StageKey): void {
    const existing = turn.stages.find((s) => s.stage_key === stageKey)
    if (existing) existing.status = 'failed'
    else turn.stages.push({ stage_key: stageKey, status: 'failed' })
  }

  /** sources：批次追加按 ref_no 排序；同 ref_no 更新不重复插（§3 派发表） */
  function mergeSources(turn: TurnState, batch: Source[]): void {
    for (const src of batch) {
      const idx = turn.sources.findIndex((s) => s.ref_no === src.ref_no)
      if (idx >= 0) turn.sources[idx] = src
      else turn.sources.push(src)
    }
    turn.sources.sort((a, b) => a.ref_no - b.ref_no)
  }

  function applyEvent(turnId: string, event: SseWireEvent): void {
    const turn = turns.value[turnId]
    if (!turn || event.data.turn_id !== turnId) return
    const seq = event.data.seq
    // 控制事件幂等门：seq ≤ lastSeq 丢弃（重放去重）。token/thinking 豁免——合帧 flush
    // 携带批内 maxSeq==lastSeq，其去重由传输层 seq 门承担（重复 push 已在源头消除）。
    const isDelta = event.event === 'token' || event.event === 'thinking'
    const isTerminalWrite = event.event === 'done' // 终态游标立即落（崩溃恢复不落后于终态）
    if (!isDelta && seq <= turn.lastSeq) return
    turn.lastSeq = Math.max(turn.lastSeq, seq)
    // P3 节流：控制事件/终态立即落，delta 事件 200ms 窗口内至多一次
    persistSeq(turnId, turn.lastSeq, { immediate: !isDelta || isTerminalWrite })
    switch (event.event) {
      case 'stage':
        upsertStage(turn, event.data)
        break
      case 'token':
        turn.answer_md += event.data.delta
        break
      case 'thinking':
        // 双缓冲（v1.4）：独立追加，绝不入 answer_md；默认折叠渲染的数据形态
        turn.thinking_md += event.data.delta
        break
      case 'sources':
        mergeSources(turn, event.data.batch)
        break
      case 'images':
        // 至多一次事件；条目键可省（v1.12：缺键不渲染对应元数据，渲染侧口径）
        turn.images = event.data.images
        break
      case 'context_compact':
        turn.compactSeq = Math.max(turn.compactSeq, seq)
        turn.compactAt = event.data.ts
        break
      case 'memory':
        turn.memory_hits = { profile_used: event.data.profile_used, items: event.data.items }
        break
      case 'done':
        turn.status = event.data.finish
        turn.degradedReason = event.data.degraded ? event.data.degraded_reason : null
        turn.cancelReason = event.data.cancel_reason ?? null
        turn.memorySaved = event.data.memory_saved ?? null
        turn.durationMs = event.data.duration_ms
        generatingThreadIds.value.delete(turn.threadId)
        break
      case 'followups':
        turn.followups = event.data.items
        break
      case 'error':
        // error 非终态：保留已生成内容 + 错误条 + 节点标 failed；生成中集合不动（只认 done/对账）
        turn.errorMsg = event.data.message
        if (event.data.stage_key !== undefined) markStageFailed(turn, event.data.stage_key)
        break
    }
  }

  /* ---------- 两段式：POST 建轮（第一段）→ subscribe（第二段） ---------- */

  function buildAskBody(query: string, mode: AskMode): CreateThreadRequest {
    // §2 注入约定：incognito/on_conflict 从 prefsStore 统一注入（禁止组件散拼）；
    // null/false 不携带字段走服务端缺省；model 无用户选择不携带（服务端缺省 chat.default）
    const prefs = usePrefsStore()
    const body: CreateThreadRequest = { query, mode }
    if (prefs.model !== null) body.model = prefs.model
    if (prefs.incognito) body.incognito = true
    if (prefs.onConflict !== null) body.on_conflict = prefs.onConflict
    return body
  }

  async function ask(query: string, mode: AskMode): Promise<StartTurnData> {
    const prefs = usePrefsStore()
    const data = await apiPost<StartTurnData>('/api/v1/threads', buildAskBody(query, mode))
    turns.value[data.turn_id] = seedTurnState({
      turnId: data.turn_id,
      threadId: data.thread_id,
      mode,
      question: query,
      status: data.status,
      incognito: prefs.incognito,
    })
    markGenerating(data.thread_id, data.status)
    subscribe(data.turn_id)
    return data
  }

  async function followUp(threadId: string, query: string, mode: AskMode): Promise<StartTurnData> {
    const prefs = usePrefsStore()
    const data = await apiPost<StartTurnData>(`/api/v1/threads/${threadId}/turns`, buildAskBody(query, mode))
    turns.value[data.turn_id] = seedTurnState({
      turnId: data.turn_id,
      threadId,
      mode,
      question: query,
      status: data.status,
      incognito: prefs.incognito,
    })
    markGenerating(threadId, data.status)
    subscribe(data.turn_id)
    return data
  }

  /** 停止（§2.7：running 与 queued 同入口；canceling 仅为响应信号，终态随 done(cancel) 到达） */
  async function cancel(turnId: string): Promise<CancelTurnData> {
    return apiPost<CancelTurnData>(`/api/v1/turns/${turnId}/cancel`)
  }

  /* ---------- SSE 订阅编排 ---------- */

  function subscribe(turnId: string): void {
    const turn = turns.value[turnId]
    if (!turn) return // 编排层须先 reconcile 种子化（生命周期 5：先 REST 再订阅）
    subscribeTurn({
      turnId,
      fromSeq: resolveFromSeq(turn),
      holdAfterDone: turn.mode === 'fast', // L13：仅 fast 轮进入 done 后保活窗口
      callbacks: {
        onEvent: (event) => {
          applyEvent(turnId, event)
          if (event.event === 'done') {
            // 生命周期 4：收到 done → 对账（REST 为准源）；v1.9 注：保活轮照旧在 done 到达时执行
            void safeReconcile(turnId)
          }
        },
        onStateChange: (state) => {
          if (liveTurnId.value === turnId) sseState.value = toConnectionState(state)
        },
        onClosed: (info) => {
          if (liveTurnId.value === turnId && !info.duringHold) sseState.value = 'idle'
          const t = turns.value[turnId]
          // done 之外的非终态关闭（error 后关闭/无 done 的异常收口）→ REST 对账收敛终态
          if (t && !isTerminalStatus(t.status)) void safeReconcile(turnId)
        },
      },
    })
    liveTurnId.value = turnId
  }

  function closeStream(turnId: string): void {
    closeTurnStream(turnId)
    if (liveTurnId.value === turnId) {
      liveTurnId.value = null
      sseState.value = 'idle'
    }
  }

  /* ---------- REST 对账（生命周期 4/5：以 REST 为准源；失败兜底 P2） ---------- */

  /** 对账失败链状态：turnId → 已重试次数（防 toast 雪崩 + 有限重试窗口判定） */
  const reconcileRetries = new Map<string, number>()
  const reconcileRetryTimers = new Map<string, ReturnType<typeof setTimeout>>()

  /**
   * safeReconcile —— subscribe 回调的对账唯一入口（评审留档 P2：原先两处裸 void reconcile
   * 无 catch，GET 失败静默 → failed 无内容轮永卡 running / generatingThreadIds 不移除）：
   *   ① 失败即复位连接态（该轮为活跃流时 sseState → idle，重连横幅不再悬置）；
   *   ② toast 兜底（normalizeError 中文文案直达；同轮失败链仅首次提示，防雪崩）；
   *   ③ 非终态轮有限重试（初败后再试至多 RECONCILE_RETRY_MAX 次，间隔 1s）——重试收敛即
   *      消除「卡 running」；耗尽为网络长断路径：生成中集合移除（指示器不永转）+ errorMsg
   *      兜底（错误条可见），status 字段仍以 REST 为准源不伪造本地终态，待网络恢复后由
   *      重新进入页面/重订阅对账收敛。
   */
  async function safeReconcile(turnId: string): Promise<void> {
    try {
      await reconcile(turnId)
      reconcileRetries.delete(turnId)
      const timer = reconcileRetryTimers.get(turnId)
      if (timer !== undefined) {
        clearTimeout(timer)
        reconcileRetryTimers.delete(turnId)
      }
    } catch (error) {
      handleReconcileFailure(turnId, error)
    }
  }

  function handleReconcileFailure(turnId: string, error: unknown): void {
    // ① 连接态复位（当前活跃流为该轮时）
    if (liveTurnId.value === turnId) sseState.value = 'idle'
    // ② toast 兜底（失败链仅首次）
    const firstFailure = !reconcileRetries.has(turnId)
    reconcileRetries.set(turnId, (reconcileRetries.get(turnId) ?? 0) + 1)
    if (firstFailure) toast(normalizeError(error).message)
    const turn = turns.value[turnId]
    // 终态已收敛（done 先置位后对账失败等）：无需重试
    if (!turn || isTerminalStatus(turn.status)) {
      reconcileRetries.delete(turnId)
      return
    }
    // ③ 有限重试耗尽：指示器移除 + 错误条兜底（防永卡生成中）
    if (reconcileRetries.get(turnId)! > RECONCILE_RETRY_MAX) {
      reconcileRetries.delete(turnId)
      generatingThreadIds.value.delete(turn.threadId)
      turn.errorMsg = turn.errorMsg ?? normalizeError(error).message
      return
    }
    const previous = reconcileRetryTimers.get(turnId)
    if (previous !== undefined) clearTimeout(previous)
    reconcileRetryTimers.set(
      turnId,
      setTimeout(() => {
        reconcileRetryTimers.delete(turnId)
        void safeReconcile(turnId)
      }, RECONCILE_RETRY_DELAY_MS),
    )
  }

  async function reconcile(turnId: string): Promise<void> {
    const detail = await apiGet<TurnDetail>(`/api/v1/turns/${turnId}`)
    const existing = turns.value[turnId]
    const turn =
      existing ??
      (turns.value[turnId] = seedTurnState({
        turnId,
        threadId: detail.thread_id,
        mode: detail.mode,
        question: detail.query,
        status: detail.status,
        incognito: detail.incognito,
      }))
    const terminal = isTerminalStatus(detail.status)
    // answer_md：终态 REST 权威替换（后端终态先落库后发 done）；运行中轮 REST 为空串
    // （token 只发布不落库）→ 保留流式增量，防对账清空
    if (terminal) turn.answer_md = detail.answer_md
    turn.status = detail.status
    turn.sources = [...detail.sources]
    turn.images = [...detail.images]
    turn.stages = detail.stages.map(stageFromRecord)
    turn.incognito = detail.incognito
    turn.cancelReason = detail.cancel_reason
    turn.followups = detail.followups
    turn.memory_hits = detail.memory_hits ?? null
    turn.errorMsg = detail.error
    turn.degradedReason = detail.degraded ? detail.degraded_reason : null
    turn.durationMs = detail.duration_ms
    // lastSeq/compactSeq/thinking_md 为传输层/流式域：REST 无对应字段，一律保留
    markGenerating(turn.threadId, detail.status)
  }

  return {
    turns,
    liveTurnId,
    sseState,
    generatingThreadIds,
    ask,
    followUp,
    cancel,
    subscribe,
    applyEvent,
    reconcile,
    /** 失败兜底对账（subscribe 回调/视图挂载统一入口；裸 reconcile 仅诊断用） */
    safeReconcile,
    closeStream,
    calibrateThread,
    // 内部诊断用（生命周期 5 形态校验）
    readStoredSeq,
  }
})
