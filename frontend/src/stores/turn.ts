/**
 * src/stores/turn.ts — turnStore（frontend-api.md §4 核心 store；setup store，§8.2）。
 *
 * 职责：
 *   - 当前线程 turns 映射：每轮 question/answer_md/thinking_md/sources/images/stages/
 *     memory_hits/compactSeq/incognito/cancelReason/followups/feedbackHelpful/status/lastSeq
 *     （§4 逐字段），另附 errorMsg/errorCode/degradedReason/memorySaved/compactAt/
 *     refusalPendingClose 六个派生展示/流程字段
 *     （派发表对应 Banner/错误条·温和卡/M5 toast/v1.4 瞬态提示/B13 拒答轮终态收口的数据形态）。
 *   - SSE 连接编排：ask()/followUp() POST 建轮（两段式第一段，经 client.ts 唯一出口）→
 *     subscribe() GET 订阅（第二段，经 turnStream.ts 唯一出口）；所有事件经 applyEvent()
 *     单入口进状态机（§3 派发表 / §8.7 传输与渲染解耦）。
 *   - reconcile()：REST 为准源对账（生命周期 4/5）；终态权威替换、运行中轮保留流式增量
 *     （后端 token/thinking 只发布不落库、answer_md 仅终态落库——apps/search-api/app/services/
 *     turn_service.py:21-22/:753-754，运行中 2.5 answer_md 为空串，替换会清空流式内容）。
 *   - sendFeedback()：轮反馈乐观 UI（v1.17，REQ-057/A41；api.md §2.9——经 api/turns.ts，
 *     失败静默回滚不 toast，reconcile 对 2.5 feedback_helpful 还原）。
 *   - regenerate()：degraded/failed-with-sources fast/deep 轮「仅重新生成」（v1.26 增、v1.30
 *     放开 deep——api.md §2.12 前置第 3 条 v1.28，REQ-063/A47；api.md §2.12——POST → 状态置
 *     running + 清空渲染态 + SSE 重订阅，§3 生命周期 14 清空重渲约定〔deep 形态注 v1.30〕；
 *     失败 409006/404001 toast 既有链路）。
 *   - generatingThreadIds：侧边栏生成中线程集合（生命周期 12），轮生命周期事件驱动、不轮询；
 *     v1.40 语义扩：awaiting_approval 计入生成中（审批等待期指示器不消失；占线判定面
 *     liveTurn/canStop 不扩 awaiting——两判定面互不侵入，§4 turnStore 行 v1.40）。
 *   - approval 状态机（B22 v1.40 增，生命周期 15）：approval_requested 置 awaiting 态 +
 *     pending 载荷 + 预览缓存捕获；approval_resolved 四路径收口（submit/timeout → running、
 *     cancel/conflict → canceled 终态）；decideApproval() POST §2.13（成功本地立即置 running
 *     不等 SSE；409008 → toast + reconcile；网络错 → toast 弹层保留）；reconcile 对账 2.5
 *     approval（刷新恢复只依赖 2.5——pending 重开弹层形态、canceled 键省略投影 null）。
 *     批次 3 评审 M1+M2 增「已裁决重放门」：approvalDecidedTurns 会话标记集 + 2.5 approved
 *     权威恢复 + 终态三重证据——刷新全量重放的 approval_requested 不回退已裁决轮（弹层不重开、
 *     状态不回退、预览缓存自重放帧回填）、approval_resolved(timeout) 不重弹 toast。
 */
import { ref } from 'vue'
import { defineStore } from 'pinia'

import { apiGet, apiPost, normalizeError } from '@/api/client'
import { approveTurn, regenerateTurn, sendTurnFeedback } from '@/api/turns'
import { i18n } from '@/i18n'
import { CONTENT_SAFETY_ERROR_CODE } from '@/constants/errors'
import { toast } from '@/composables/toast'
import { isRecord } from '@/utils/guards'
import { closeTurnStream, subscribeTurn } from '@/sse/turnStream'
import type { TurnStreamState } from '@/sse/turnStream'
import { usePrefsStore } from '@/stores/prefs'
import type {
  ApprovalChoice,
  ApprovalPreview,
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
  TurnApproval,
  TurnDetail,
  TurnImageItem,
  TurnStatus,
} from '@/types/api'

/** §3 生命周期 5：本地 seq 游标的 sessionStorage 键（标签页隔离语义；v1.30 OQ-35 起单键 JSON 双值 {lastSeq, compactSeq}） */
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

/** §3 生命周期 12（v1.40 语义扩）：计入生成中集合的占线状态——awaiting_approval 审批等待期侧栏指示器不消失（契约「awaiting 计入生成中」） */
const GENERATING_STATUSES: readonly TurnStatus[] = ['running', 'queued', 'awaiting_approval']

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

/** decideApproval 决策结果（批次 3 评审 L7：ok=false 时 message=服务端/网络错误文案，供弹层内错误条呈现） */
export interface ApprovalDecisionResult {
  ok: boolean
  message: string | null
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
  /**
   * 轮反馈现值（v1.17，REQ-057/A41）：sendFeedback 响应回写 + reconcile() 对账
   * 2.5 feedback_helpful 双源；SSE 无对应事件。null=未反馈。
   */
  feedbackHelpful: boolean | null
  lastSeq: number
  /** error 事件 message（错误条展示；error 非终态，§3 派发表） */
  errorMsg: string | null
  /** error 码（SSE error.code / 2.5 error.code 双源；B13 温和卡按 403002 分支判定） */
  errorCode: string | null
  /**
   * B13 拒答轮「等待关闭」标记（仅 error code=403002 置位）：流随后关闭且未见 done → 终态收口
   * （立即移出生成中集合 + REST 对账；传输层已保证该收口不重连）。done 若仍到达则按既有终态
   * 逻辑走（done 先置终态 → onClosed 的 isTerminalStatus 门拦截，标记不再生效）。
   */
  refusalPendingClose: boolean
  /** done(degraded) 携带（DegradedBanner 数据） */
  degradedReason: DegradedReason | null
  /** done.memory_saved（M5「已记住」toast 数据；无痕轮恒无） */
  memorySaved: number | null
  /** 轮耗时（SSE done.duration_ms / 2.5 duration_ms 双源；进行中 null——状态行「已完成 N秒」） */
  durationMs: number | null
  /**
   * 服务端报告导出可用（批次 6，api.md §2.5 v1.10 判据：mode=deep ∧ answer_md 非空 ∧
   * status∈{completed,degraded}）。数据源唯一 = 2.5 对账回写（SSE 无对应事件）；种子缺省
   * false（ask/followUp 新轮不渲染「导出报告」，api.md §2.5 判据须待 REST 权威）——
   * 消费面 = AnswerActions 更多菜单「导出报告」门控（§5 ⑦ v1.30）。
   */
  exportAvailable: boolean
  /**
   * B22 HITL 审批门投影（v1.40 增，api.md §2.5 approval/§7.2 v1.37 消费面；frontend-api.md
   * §4 turnStore 行 v1.40）：SSE approval_requested/approval_resolved 与 reconcile() 对账
   * 双源写入——四形态 pending/approved/no_hit/shadow；非门轮键省略投影 null（§2.5「键整体
   * 省略不置 null」的 store 面）；v1.41 钉：status=canceled 的审批行 approval 键整体省略
   * ——canceled 轮回看面无审批折叠条。消费面 = ApprovalSheet 弹层与回看态折叠条（批次 3）。
   */
  approval: TurnApproval | null
  /**
   * approval_requested 捕获的命中预览缓存（pending 形态 previews；回看折叠条「查看当时的
   * 知识库命中」展开数据源）。流式域字段——2.5 approved 形态无 previews 键，对账不清本地
   * 缓存（沿 thinking_md/compactSeq「REST 无对应字段一律保留」先例）；2.5 pending 形态
   * previews 全量在 payload（刷新恢复弹层重开数据面），对账捕获覆盖。canceled 收口清 null
   * （取消轮不进历史、无折叠条）。
   */
  approvalPreviews: ApprovalPreview[] | null
  /**
   * awaiting_approval 累计时长毫秒（v1.37，api.md §2.5 awaiting_ms——非零才携带，键省略
   * 投影 null；SSE 无对应事件、唯一数据源 2.5 对账回写）。消费面 = 回看折叠条等待时长行
   * （批次 3 第三段；折叠条展开面「审批等待 N 秒」）。
   */
  awaitingMs: number | null
  /**
   * 改写子问题（api.md §2.5 rewritten_queries 顶层权威源；SSE 面无对应键——stage(rewrite,done)
   * extra 仅 hitl 留痕，§7.4 带审批形态实测无 rewritten_queries，故唯一写入点=reconcile 对账）。
   * 消费面 = ApprovalSheet「命中的子问题」行（query_index 对位，批次 3 第三段 AnswerView 装配）。
   */
  rewrittenQueries: string[] | null
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
    feedbackHelpful: null,
    lastSeq: 0,
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
  /**
   * 线程装配快照（App 顶栏导出/分享的数据源）：threadId → 2.3 线程标题 + 轮次序
   * （turns 按 created_at 升序——store.turns 为轮 id 键映射无序，整线程导出按此序重建时序）。
   * 写入点唯一 = AnswerView.loadThread（2.3 取数成功后）；顶栏导出/分享消费。
   */
  const threadMetas = ref<Record<string, { title: string; turnIds: string[] }>>({})

  /** 线程装配快照写入（AnswerView.loadThread 调用；覆盖式——watch(threadId) 切线程重装即更新） */
  function setThreadMeta(threadId: string, meta: { title: string; turnIds: string[] }): void {
    threadMetas.value[threadId] = meta
  }

  /* ---------- 生成中集合（生命周期 12：轮生命周期事件驱动，不轮询） ---------- */

  /**
   * v1.40 语义扩（B22）：awaiting_approval 计入生成中——审批等待期侧栏指示器不消失
   * （frontend-api.md §4 generatingThreadIds 行 v1.40「awaiting_approval 计入生成中」）。
   * 占线判定面与此互不侵入：AnswerView liveTurn/canStop/isStreaming 仍只认 running|queued
   * （awaiting 不是流式、停止钮不出现——停止语义由 ApprovalSheet「取消本轮」承接，生命周期 15）。
   */
  function markGenerating(threadId: string, status: TurnStatus): void {
    if (GENERATING_STATUSES.includes(status)) {
      generatingThreadIds.value.add(threadId)
    } else {
      generatingThreadIds.value.delete(threadId)
    }
  }

  /** 进入页面/切回时以 2.3 turns[].status 校准当前线程（生命周期 12 对账口径；v1.40 awaiting 同口径） */
  function calibrateThread(threadId: string, statuses: TurnStatus[]): void {
    markGenerating(threadId, statuses.some((s) => GENERATING_STATUSES.includes(s)) ? 'running' : 'completed')
  }

  /* ---------- seq 游标持久化（生命周期 5 形态保留；P3 写入节流） ---------- */

  /** 每 turn 上次落盘时刻（节流窗判定；turnId → epoch ms） */
  const seqPersistedAt = new Map<string, number>()

  /**
   * sessionStorage 游标写入（P3 节流 + v1.30 OQ-35 双值形态）：单键 JSON
   * `{lastSeq, compactSeq}`（与 lastSeq 同轮同键族——compactSeq 是「lastSeq < compactSeq →
   * 放弃增量续传」判定依据，刷新后判定不再依赖内存态）。delta 事件 200ms 窗口内至多落一次
   * （写放大收敛——每帧一次 setItem 换成 ≤5 次/秒）；控制事件与终态（done）豁免立即落。
   * 注意：context_compact 事件的主调用（applyEvent 持久化点）先于 switch 写 turn.compactSeq
   * ——此刻读到的 compactSeq 是上一事件值；case 内的二次 immediate 落盘兜住同帧性
   * （两个 setItem 同步连发，最终值正确）。
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
      const compactSeq = turns.value[turnId]?.compactSeq ?? 0
      sessionStorage.setItem(SEQ_STORAGE_PREFIX + turnId, JSON.stringify({ lastSeq: seq, compactSeq }))
    } catch {
      // 存储不可用（隐私模式等）静默：续传退化为全量重放（幂等）
    }
  }

  /** 落盘游标形态（v1.30 OQ-35）：compactSeq=null = 无记录（旧纯数字格式 / compactSeq 键缺席或 ≤0 / 解析失败） */
  interface StoredSeqCursor {
    lastSeq: number
    compactSeq: number | null
  }

  /**
   * 读落盘游标（双形态兼容）：JSON `{lastSeq, compactSeq}` 为现行形态；批次 5 前的旧纯数字
   * 串（"9"）= compactSeq 无记录（宽路径口径：旧游标无 compact 证据 → 视同不可续传）。
   * 读写 try/catch 同 §2 惯例；任何解析失败回落 lastSeq=0（全量重放幂等）。
   */
  function readStoredCursor(turnId: string): StoredSeqCursor {
    try {
      const raw = sessionStorage.getItem(SEQ_STORAGE_PREFIX + turnId)
      if (raw === null) return { lastSeq: 0, compactSeq: null }
      if (raw.startsWith('{')) {
        const parsed: unknown = JSON.parse(raw)
        if (
          isRecord(parsed) &&
          typeof parsed.lastSeq === 'number' &&
          Number.isFinite(parsed.lastSeq) &&
          parsed.lastSeq > 0
        ) {
          const compact = parsed.compactSeq
          const compactSeq =
            typeof compact === 'number' && Number.isFinite(compact) && compact > 0 ? compact : null
          return { lastSeq: parsed.lastSeq, compactSeq }
        }
        return { lastSeq: 0, compactSeq: null }
      }
      const n = Number(raw)
      return { lastSeq: Number.isFinite(n) && n > 0 ? n : 0, compactSeq: null }
    } catch {
      return { lastSeq: 0, compactSeq: null }
    }
  }

  /** 落盘 lastSeq 游标（regenerate 重放门抬升与诊断消费面；readStoredCursor 的数值投影） */
  function readStoredSeq(turnId: string): number {
    return readStoredCursor(turnId).lastSeq
  }

  /**
   * 订阅 seq 门基线（生命周期 5/11；v1.30 OQ-35 compactSeq 刷新恢复口径 + v1.32 P2-1 准入
   * 前提注记）。原生 EventSource 新建对象无法携带 Last-Event-ID 头（浏览器仅在「同一对象」
   * 自动重连时原生携带，勿自造 header）——fromSeq 为纯客户端重放去重门，服务端首连恒全量
   * 重放。判定序：
   *   ① 基线 = max(落盘 lastSeq, 内存 lastSeq)（同页路径保真——同页落盘游标恒 ≤ 内存，
   *     落盘仅兜内存节流窗；刷新后内存归 0、基线即落盘游标）。
   *   ② compactSeq 边界判定（内存 + 落盘双源证据取大）：> 基线 → 放弃增量返 0（v1.4 重放
   *     边界——本地游标早于最近一次压缩）。
   *   ③ 刷新路径（cursor.lastSeq > 内存 lastSeq）增量续传准入证据 = **「REST 可承载被跳过
   *     区间」**（v1.32 批次 6 收口轮 P2-1 修法，frontend-api.md §3 生命周期 5【v1.32 注记】）：
   *     落盘游标携带 compactSeq 记录（口径合规证据）∧ REST 对账回写的 answer_md 非空
   *     （终态权威已落库）两者齐备才允许增量续传；运行中轮 token/thinking 只发布不落库、
   *     2.5 answer_md 为空串——增量跳过 1..N 的正文无处可重建（刷新恢复丢帧），故一律放弃
   *     增量返 0 走全量重放（等效恢复批次 5 刷新行为；直到后端为 deep 运行轮持久化分段
   *     报告前口径不变，后端立项另议）；compactSeq 记录缺失（fast 轮恒无 / 旧纯数字格式 /
   *     记录丢失）同判放弃。放弃路径 = REST 全量对账后重新订阅（loadThread 既有 reconcile
   *     先行满足——宽路径灰度安全，宁全量不丢帧）。
   *   ④ 其余（同页已流式）→ 门 = 基线：重建连接的全量重放中被门丢弃 ≤基线部分，仅应用
   *     增量（幂等续传）；本页无任何游标（新标签页）→ 门=0 全量重放重建。
   */
  function resolveFromSeq(turn: TurnState): number {
    const cursor = readStoredCursor(turn.turnId)
    const baseline = Math.max(cursor.lastSeq, turn.lastSeq)
    const compactEvidence = Math.max(turn.compactSeq, cursor.compactSeq ?? 0)
    if (compactEvidence > baseline) return 0
    // 刷新路径准入（P2-1）：compactSeq 记录与 REST 持久化证据（answer_md 非空）任一缺席
    // → 增量跳过的内容无法由 REST 承载 → 全量重放
    if (cursor.lastSeq > turn.lastSeq && (cursor.compactSeq === null || turn.answer_md === '')) return 0
    return baseline
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
        // 生命周期 14 清空重渲边界（v1.26，REQ-063；双评审 P2 修复）：stage(write,running) 是
        // 新一轮渲染边界——regenerate 重订阅的全量重放中旧 token/thinking 会先再累积（delta
        // 通道豁免 seq 门，新标签页无落盘游标时可达），边界到达且答案区/思考流已有渲染态 →
        // 两者同时清位重新累积。thinking 必清的语义依据：thinking 为持久渲染面（ThinkingPanel
        // 终态后常驻、REST 无对应字段不对账），旧代+新代拼接即持久错 mixed。事件序安全性：
        // thinking 仅与 token 同构交替、严格晚于 write-running（§7.2 v1.4 / §7.4 fixture——deep
        // seq 13 write-running → seq 14 首个 thinking），故旧代 thinking 全部早于新边界 seq、
        // 触发条件含 thinking_md 恒无误伤（常规首轮流 write 前两缓冲恒空，条件不成立零行为变化）。
        if (
          event.data.stage_key === 'write' &&
          event.data.status === 'running' &&
          (turn.answer_md !== '' || turn.thinking_md !== '')
        ) {
          turn.answer_md = ''
          turn.thinking_md = ''
        }
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
        // OQ-35（v1.30）：compactSeq 是刷新恢复「放弃增量」判定依据，随游标同帧落盘——主
        // persistSeq 调用（switch 前）读到的还是上一事件 compactSeq，此处二次 immediate 兜底
        persistSeq(turnId, turn.lastSeq, { immediate: true })
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
      case 'followups': {
        // 安全守卫（评审 P3-3）：items 须为字符串数组才入 state——镜像类型 string[] 是静态
        // 形态，运行时契约回归混入非字符串会崩 FollowupChips 渲染（§8.9⑥ 盲区口径的运行时
        // 防线）；非法静默丢弃、维持既有值（与后端生成失败静默语义一致）。seq 门在进入
        // switch 前已推进 lastSeq：游标与内容分离，丢弃不卡重放。
        const items = event.data.items
        if (Array.isArray(items) && items.every((s) => typeof s === 'string')) {
          turn.followups = items
        }
        break
      }
      case 'approval_requested': {
        // 批次 3 评审 M1：重放门「认已裁决」——三重证据任一命中即不回退不重开弹层：
        //   ① 本会话已见过该轮收口（approvalDecidedTurns——resolved/乐观裁决/取消/对账标记）；
        //   ② 2.5 权威已恢复 approved 形态（刷新 reconcile 先行恢复，弹层状态派生面不开）；
        //   ③ 轮已终态（陈旧重放帧——终态轮审批 UI 不得复活）。
        // 命中路径仅回填预览缓存：已裁决形态且缓存为空时自重放 payload 捕获（2.5 approved 无
        // previews 键——重放帧是刷新后回看折叠条展开数据面的唯一来源；canceled/其他形态不捕获）。
        if (
          approvalDecidedTurns.has(turnId) ||
          turn.approval?.status === 'approved' ||
          isTerminalStatus(turn.status)
        ) {
          if (turn.approval?.status === 'approved' && turn.approvalPreviews === null) {
            turn.approvalPreviews = event.data.previews
          }
          break
        }
        // B22（v1.40 派发表）：置 awaiting 态 + approval=pending 载荷（verdict/expires_at/previews
        // 全量）+ 预览缓存捕获；时间线不新增节点（rewrite 已 done、search 未开始）；生成中集合
        // 保留（markGenerating awaiting 计入）。幂等门在 switch 前已推进 lastSeq——同连接重放
        // （seq ≤ lastSeq）不达此处；新连接全量重放（fromSeq=0）到达时覆盖式刷新同 payload，
        // 弹层可见性由状态派生（approval pending ∧ status awaiting）天然幂等不重复弹。
        turn.status = 'awaiting_approval'
        turn.approval = {
          status: 'pending',
          verdict: event.data.verdict,
          expires_at: event.data.expires_at,
          previews: event.data.previews,
        }
        turn.approvalPreviews = event.data.previews
        markGenerating(turn.threadId, 'awaiting_approval')
        break
      }
      case 'approval_resolved': {
        // B22（v1.40 派发表）四路径收口，以 choice 键有无分流（api.md §7.2——「timeout 收口=缺省
        // 策略值 hybrid 对应 both；cancel 收口无 choice——键省略」；后端 collect_expired 的
        // on_timeout=cancel 静默收尾分支同样无 choice 键）：
        //   choice 在（submit / timeout 续流）→ running + 已裁决形态（预览缓存保留——事件 payload
        //     无 previews，回看折叠条展开数据源不因收口丢失）；resolved_by=timeout 时 toast 轻提示
        //     「已按『两者都要』继续」（§3 派发表行 v1.40；submit 不 toast——乐观 UI 已即时反馈）。
        //   choice 无（cancel 用户取消 / conflict interrupt·rollback 直接收尾）→ canceled 终态收口
        //     （生成中集合移除；cancelReason 由随后 done(cancel,user|superseded|rolled_back) 权威
        //     补写——§7.4 取消形态）。另一标签页同轮收敛：本端未提交过裁决时本帧即远端裁决通知
        //     （状态派生关弹层对账）。
        //   choice 无且 resolved_by=timeout（on_timeout=cancel 静默收尾）→ 零本地置位——本帧不置
        //     running/裁决形态（避免「已按两者都要继续」误导与生成中集合抖动），done(finish=canceled,
        //     cancel_reason=timeout) 紧随其后权威收口（§2.5 v1.38 第 4 cancel_reason 值）。
        // 批次 3 评审 M2：本帧到达前的已裁决标记决定 timeout toast 是否重弹（刷新全量重放的
        // resolved(timeout) 帧命中标记 → 不重弹）；任意分支收口后统一标记（同会话后续重放帧全部入门）。
        const payload = event.data
        const alreadyDecided = approvalDecidedTurns.has(turnId)
        approvalDecidedTurns.add(turnId)
        // 终态轮陈旧重放帧防御（批次 3 终轮评审 M，与 approval_requested 分支③对称）：
        // reconcile 后内存 lastSeq=0 时陈旧 resolved 帧可过 seq 门——终态不得被回写复活运行/取消态。
        const staleTerminal = isTerminalStatus(turn.status)
        if (payload.choice === undefined) {
          if (payload.resolved_by === 'cancel' || payload.resolved_by === 'conflict') {
            turn.approval = null // canceled 轮 approval 键整体省略（v1.41，api.md §2.5 v1.38）
            turn.approvalPreviews = null // 取消轮不进历史、无折叠条，预览缓存随收口清位
            if (!staleTerminal) turn.status = 'canceled'
            generatingThreadIds.value.delete(turn.threadId)
          }
        } else {
          // alreadyDecided（重放收敛）保留既有 note（批次 3 终轮评审 M）：note 唯一权威源是 2.5
          // （SSE resolved payload 无 note 键），无条件覆盖会把 reconcile 自 2.5 恢复的补充指令
          // 抹掉——ApprovalRecall 指令行在 running 中刷新窗口闪没。非 alreadyDecided 首帧路径
          // 既有形态为 pending（无 note），透传 undefined 与原行为零差异。
          const preservedNote =
            alreadyDecided && turn.approval?.status === 'approved' ? turn.approval.note : undefined
          turn.approval = {
            status: 'approved',
            choice: payload.choice,
            decided_by: payload.decided_by,
            ...(preservedNote !== undefined ? { note: preservedNote } : {}),
          }
          if (!staleTerminal) turn.status = 'running'
          if (payload.resolved_by === 'timeout' && !alreadyDecided) {
            toast(i18n.global.t('answer.approvalTimeoutContinued'))
          }
        }
        break
      }
      case 'error':
        // error 非终态：保留已生成内容 + 错误条 + 节点标 failed；生成中集合不动（只认 done/对账）
        turn.errorMsg = event.data.message
        turn.errorCode = event.data.code
        if (event.data.stage_key !== undefined) markStageFailed(turn, event.data.stage_key)
        // B13 拒答轮：403002 内容安全拒绝 → 置「等待关闭」标记（传输层同步置同款标记）——
        // 流随后关闭且未见 done → onClosed 终态收口（移出生成中集合 + 对账、无重连无横幅）；
        // 防御：该 error 后若仍收到 done，按既有终态逻辑走（done 先置终态，本标记失效）。
        if (event.data.code === CONTENT_SAFETY_ERROR_CODE) turn.refusalPendingClose = true
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

  /**
   * 重新生成（v1.26 增，REQ-063/A47；v1.30 放开 deep——api.md §2.12 前置第 3 条 v1.28 /
   * frontend-api.md §3 生命周期 14 deep 形态注）：degraded/failed-with-sources fast/deep 轮
   * 「仅重新生成」——复用既有 sources 只重跑 synthesis（deep 重放起点=write，无
   * plan/search/read/reflect 段重跑；api.md §7.4 v1.28）。
   *   ① POST /api/v1/turns/{turnId}/regenerate（经 api/turns.ts 唯一出口；请求体空）；
   *   ② 成功 → 状态置 running + 清空该轮渲染态：answer_md/thinking_md 缓冲与旧终态展示字段
   *     （errorMsg/degradedReason/durationMs——状态条/DegradedBanner 随 TurnBlock isLive
   *     既有分支接管，零改动）；sources/stages 保留（api.md §2.12：不重检索、时间线按
   *     stage(write,running) 就地重渲，第二次 done 对账时 REST 权威回填）；
   *   ③ 重订阅 SSE（同通道续推，api.md §7.1 通道重开注）：重放门抬升为 max(内存 lastSeq,
   *     sessionStorage 落盘游标——readStoredCursor 双形态兼容)——同页/同标签页刷新路径旧轨迹
   *     （旧 done/error/token/thinking）在传输层与派发层双门幂等丢弃；新标签页无落盘游标 →
   *     全量重放由「stage(write,running) 清空重渲边界」（applyEvent，token/thinking 双缓冲
   *     同清——双评审 P2）兜底 delta 泄漏，旧 done/error 瞬态由二次 done + 2.5 对账收敛
   *     （REST 为准源）；deep 轮同构零新分支（生命周期判定与 fast 同构，frontend-api.md
   *     §3 生命周期 14 deep 形态注 v1.30）；
   *   ④ 失败（409006/404001/网络）→ toast（normalizeError 服务端中文 message 直达，
   *     既有链路），状态不清空、不订阅——轮保持原终态可再次点击；进行中重复点击由服务端
   *     条件 UPDATE 以同码 409006 拒绝（§2.12，前端不再加防抖门）。
   */
  async function regenerate(turnId: string): Promise<void> {
    const turn = turns.value[turnId]
    if (!turn) return
    try {
      await regenerateTurn(turnId)
    } catch (error) {
      toast(normalizeError(error).message)
      return
    }
    turn.status = 'running'
    turn.answer_md = ''
    turn.thinking_md = ''
    turn.errorMsg = null
    turn.errorCode = null
    turn.refusalPendingClose = false
    turn.degradedReason = null
    turn.durationMs = null
    // 重放幂等门抬升（生命周期 14）：内存 lastSeq 随刷新归零，本标签页曾观测的游标仍在
    // sessionStorage（L5 形态，persistSeq done 即时落）——max() 合成重放基线（落盘不可用
    // 时回退内存值；两者皆 0 = 新标签页全量重放，由边界清空 + 二次对账收敛）
    turn.lastSeq = Math.max(turn.lastSeq, readStoredSeq(turnId))
    markGenerating(turn.threadId, 'running')
    subscribe(turnId)
  }

  /**
   * 轮反馈（v1.17，REQ-057/A41；api.md §2.9 upsert 改判 + 取消——同钮再点即取消，v1.17）：
   *   - 乐观 UI：本地即置 → POST（经 api/turns.ts 唯一出口）→ 响应回写；
   *   - helpful: true/false = upsert 改判；null = 清除反馈（删除该轮反馈行，响应
   *     helpful=null 且 updated_at=null——同钮再点即取消，Perplexity 实机行为，v1.17）；
   *   - 失败静默回滚至 lastServerConfirmed（服务端最后一次确认值）、不 toast
   *     （404004/网络错误同口径，仅 console 留痕，可重试）；批次3评审-F1：回滚锚点不再是
   *     点击时刻快照——快照可能是上一代未被确认的乐观值（两连败滚出「未落盘假态」）；
   *   - 改判 = 再点另一按钮再次调用（每次调用独立乐观置位 → POST 覆盖）；
   *   - 乱序守卫（评审 P2-2）：per-turn 请求代数（feedbackSeqs）——快速互点两钮 + 响应乱序
   *     时，非最新代数的响应/失败一律丢弃（不回写、不回滚），旧请求不再覆盖新状态。
   */
  async function sendFeedback(turnId: string, helpful: boolean | null): Promise<void> {
    const turn = turns.value[turnId]
    if (!turn) return
    const seq = (feedbackSeqs.get(turnId) ?? 0) + 1
    feedbackSeqs.set(turnId, seq)
    turn.feedbackHelpful = helpful
    try {
      const data = await sendTurnFeedback(turnId, helpful)
      if (feedbackSeqs.get(turnId) !== seq) return // 旧响应后到：丢弃不写（新点击已接管状态）
      turn.feedbackHelpful = data.helpful
      feedbackServerConfirmed.set(turnId, data.helpful) // 锚点回写（批次3评审-F1）
    } catch (error) {
      if (feedbackSeqs.get(turnId) !== seq) return // 旧请求失败后到：不回滚新状态、不留痕
      // 回滚锚点 = lastServerConfirmed（null=服务端从未确认 → 回未反馈态）
      turn.feedbackHelpful = feedbackServerConfirmed.get(turnId) ?? null
      // 静默留痕（frontend-api.md §1 v1.17：失败静默回滚不 toast——回滚后回未反馈/改判前态）
      console.warn(`[turnStore] feedback 回滚（turn=${turnId}, helpful=${String(helpful)}）`, error)
    }
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
          if (t && !isTerminalStatus(t.status)) {
            // B13 拒答轮终态收口：error(403002) 等待关闭 + 流关闭未见 done → 立即移出生成中
            // 集合（不等对账回写——REST 慢/失败时指示器不永转）；对账随后收敛 status=failed
            // 权威终态。重连豁免与 sseState 复位（无 ReconnectBanner）由传输层收口保证。
            if (t.refusalPendingClose) generatingThreadIds.value.delete(t.threadId)
            void safeReconcile(turnId)
          }
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

  /** 反馈请求代数（评审 P2-2 乱序守卫）：turnId → 最新请求序号；响应/失败按代数判定丢弃 */
  const feedbackSeqs = new Map<string, number>()

  /**
   * 已裁决标记集（批次 3 评审 M1+M2：重放门「认已裁决」）：turnId → 本会话已见过该轮审批收口。
   * 写入点四个：applyEvent approval_resolved 任意分支收口、decideApproval 乐观成功、
   * cancelApproval 本地收口、reconcile 对账 2.5 approved（刷新路径唯一跨刷新证据源——刷新后
   * 内存集清零，由 2.5 权威态重建）。命中门 = 重放 approval_requested 不回退状态不重开弹层、
   * 重放 approval_resolved(timeout) 不重弹 toast。审批门每轮至多一次（api.md §2.5），per-turn
   * 集合无过期面。
   */
  const approvalDecidedTurns = new Set<string>()

  /**
   * 服务端最后确认值（批次3评审-F1 失败回滚锚点）：turnId → 最后一次被服务端确认的
   * feedbackHelpful（缺省=null=从未确认）。写入点两个：sendFeedback 响应成功回写、
   * reconcile() 对账（2.5 feedback_helpful 为准源）。失败回滚目标 = 此锚点而非点击时刻
   * 快照——快照可能是上一代未被确认的乐观值（两连败会滚出「服务端从未落盘」的 UI 假态）。
   */
  const feedbackServerConfirmed = new Map<string, boolean | null>()

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
    turn.feedbackHelpful = detail.feedback_helpful ?? null
    // 对账值同为服务端确认（批次3评审-F1：回滚锚点随准源同步）
    feedbackServerConfirmed.set(turnId, turn.feedbackHelpful)
    turn.memory_hits = detail.memory_hits ?? null
    // 2.5 error 为 TurnError 对象载荷（data-model.md §1.3 {code,message,stage_key?}；镜像勘误
    // 2026-11-15）——只取 message 文本保持 errorMsg: string|null 形状（TurnBlock 渲染零改动，
    // 直写对象会渲染 [object Object]）；SSE error 事件路径（event.data.message）本就是字符串。
    // errorCode 双源同取（B13 温和卡按 403002 分支——SSE error 与 2.5 回看两路径同款渲染）。
    turn.errorMsg = detail.error?.message ?? null
    turn.errorCode = detail.error?.code ?? null
    turn.degradedReason = detail.degraded ? detail.degraded_reason : null
    turn.durationMs = detail.duration_ms
    turn.exportAvailable = detail.export_available
    // B22（v1.40）：approval 投影对账（2.5 为准源——刷新恢复只依赖 2.5 不依赖事件重放，§3
    // 生命周期 15）。非门轮/canceled 轮键整体省略（v1.41）→ null；pending 形态 previews 全量
    // 在 payload → 捕获预览缓存（弹层重开数据面）；approved 形态 2.5 无 previews → 保留本地
    // 缓存（沿 thinking_md/compactSeq「REST 无对应字段一律保留」流式域先例——回看折叠条展开
    // 数据源不被对账清空）；其余形态（no_hit/shadow/键省略）→ 对称清理陈旧缓存（批次 3 终轮
    // 评审 M，与 approval=null 同步）。awaiting_ms 非零才携带（键省略投影 null）。
    turn.approval = detail.approval ?? null
    // 批次 3 评审 M1：2.5 approved 权威恢复 = 刷新路径的已裁决标记重建（重放门唯一跨刷新证据源）
    if (detail.approval?.status === 'approved') approvalDecidedTurns.add(turnId)
    // 非零才携带（api.md §2.5「零等待零键」）：键缺省与 0 值同投影 null（0 在契约面不该出现，
    // 出现即视为无等待段——防御面与键省略同口径，s4 第四段边界用例锁死）。
    turn.awaitingMs = detail.awaiting_ms ? detail.awaiting_ms : null
    if (detail.approval?.status === 'pending') {
      turn.approvalPreviews = detail.approval.previews
    } else if (detail.approval?.status !== 'approved') {
      turn.approvalPreviews = null
    }
    // B22（批次 3 第三段）：rewritten_queries 顶层权威源捕获（api.md §2.5 :218——恒出现；
    // SSE 面无对应键〔stage(rewrite,done) extra 仅 hitl 留痕〕，故唯一写入点=对账）。
    // 消费面 = ApprovalSheet「命中的子问题」行（query_index 对位）。
    turn.rewrittenQueries = detail.rewritten_queries
    // lastSeq/compactSeq/thinking_md 为传输层/流式域：REST 无对应字段，一律保留
    markGenerating(turn.threadId, detail.status)
  }

  /**
   * 审批裁决（B22 v1.40 增，REQ-069/A54；frontend-api.md §4 decideApproval 行 + §3 生命周期 15；
   * api.md §2.13 经 api/turns.ts approveTurn 唯一出口）：POST §2.13 →
   *   ① 成功（202 首次/200 幂等）：本地立即置 running + approval 置已裁决形态（decided_by=user）
   *     ——乐观 UI 不等 SSE（SSE approval_resolved 与阶段 2 事件流到达时按 seq 幂等收敛）；预览
   *     缓存保留（回看折叠条立即可展开）；生成中集合保留（running 计入）。弹层关闭由视图层
   *     状态派生（status!=='awaiting_approval' 即关），本层零弹层耦合。
   *   ② 409008（HTTP 409 错误信封——另一标签页已裁决/已超时收口）：toast 服务端 message +
   *     reconcile()（以 2.5 权威态收敛——权威 status/approval 到位后弹层随状态派生自动关）。
   *   ③ 其余失败（400001 choice 非法/note 超 500 字、404001 归属校验、网络错误）：toast、
   *     态不动（status 保持 awaiting、approval 保持 pending）——弹层保留可重试（生命周期 15）。
   *   批次 3 评审 L7：返回结构化结果（ok/message）——失败文案同时供弹层内错误条（errorText）
   *   呈现，toast 语义不变（实现内部细节，契约失败语义零变更）。
   * POST-first 模式（非乐观-first）：失败路径零本地置位，无回滚需求（对照 sendFeedback 乐观
   * UI——反馈失败要回滚选中态，裁决失败仅 toast+权威收敛）。
   */
  async function decideApproval(turnId: string, choice: ApprovalChoice, note?: string): Promise<ApprovalDecisionResult> {
    const turn = turns.value[turnId]
    if (!turn) return { ok: false, message: null }
    try {
      await approveTurn(turnId, note !== undefined && note !== '' ? { choice, note } : { choice })
    } catch (error) {
      const normalized = normalizeError(error)
      toast(normalized.message)
      if (normalized.code === '409008') {
        void safeReconcile(turnId)
      }
      return { ok: false, message: normalized.message }
    }
    turn.status = 'running'
    turn.approval = { status: 'approved', choice, decided_by: 'user' }
    approvalDecidedTurns.add(turnId)
    markGenerating(turn.threadId, 'running')
    return { ok: true, message: null }
  }

  /**
   * 弹层「取消本轮」（B22 v1.40 增，frontend-api.md §4 cancelApproval 行：复用既有 cancel()
   * 走 §2.7 awaiting 分支——api.md §2.7「awaiting_approval 轮可取消：直接落库终态
   * canceled(cancel_reason=user)，无协程在跑、不经取消标志」；关闭弹层=canceled(user) 不进
   * 历史〔G 裁决〕，审计留痕在 turn_approvals 表——v1.41 canceled 审批行 approval 键整体省略）：
   *   ① POST /api/v1/turns/{turnId}/cancel（经既有 cancel() 出口）；
   *   ② 成功 → 本地立即收口（契约「关弹层由 approval_resolved(resolved_by=cancel) 派发或本地
   *     立即收口均可，实现随批次 3 定」——取本地立即）：status 置 canceled + approval/previews
   *     清 null + 生成中集合移除；approval_resolved(resolved_by=cancel) 与 done(finish=canceled,
   *     cancel_reason=user) 随后到达按 seq 幂等收敛（cancelReason 由 done 权威补写）；
   *   ③ 失败（网络错等）：toast、态不动——弹层保留可重试。
   */
  async function cancelApproval(turnId: string): Promise<void> {
    const turn = turns.value[turnId]
    if (!turn) return
    try {
      await cancel(turnId)
    } catch (error) {
      toast(normalizeError(error).message)
      return
    }
    turn.approval = null
    turn.approvalPreviews = null
    turn.status = 'canceled'
    approvalDecidedTurns.add(turnId) // 批次 3 评审 M1：取消收口同标记已裁决门（陈旧重放帧不得复活弹层）
    generatingThreadIds.value.delete(turn.threadId)
  }

  return {
    turns,
    liveTurnId,
    sseState,
    generatingThreadIds,
    threadMetas,
    setThreadMeta,
    ask,
    followUp,
    cancel,
    sendFeedback,
    regenerate,
    decideApproval,
    cancelApproval,
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
