<script setup lang="ts">
/**
 * AnswerView（§1：/thread/:threadId——线程详情骨架 + 逐轮渲染 + SSE 续传编排）。
 *
 * 生命周期（frontend-api.md §3）：
 *   - 进入（L5）：GET /threads/{id}（2.3 骨架 + calibrateThread）→ 逐轮 reconcile(turnId)
 *     （2.5 渲染已持久化部分）→ running/queued 轮 subscribe(turnId)（流式增量/重放幂等）；
 *   - 切走（L11）：onUnmounted → closeStream(turnId)（防双连接注册表在 turnStream.ts）；
 *   - 追问：followUp()（POST 2.4，incognito/on_conflict 注入照旧）→ turnIds 追加 →
 *     滚动恢复途径③（notifyNewTurn）。
 * 滚动三件套（§5 v1.8）：AnswerScrollController（贴底跟随/8px 接管/三途径恢复）+
 * OutlineAnchor（h2 标题 id 数据源）+ ScrollToBottomFab（恢复途径②）。
 * [n] 引用联动（REQ-003）：cite(refNo) → 来源面板对应卡 scrollIntoView + 高亮。
 * 批次 2「Perplexity 交互对齐」（frontend-api.md §1/§5 v1.18）：
 *   - 编辑查询（REQ-055/A39）：TurnBlock emit edit-question → 预填 followQuery 并聚焦
 *     SearchCard（defineExpose focus）——不提交、不改写历史轮次；
 *   - 复刻（REQ-056/A40）：emit rerun(turn) → 原问题原模式走既有 followUp 追问流（REQ-042 照常）；
 *   - 来源（REQ-056/A40）：滚动定位来源面板 + highlightSeq 驱动 2s 高亮（SourcePanel 内聚）；
 *   - followups（REQ-046）：TurnBlock emit follow → mode=fast 走既有 followUp（契约 §5 v1.9）。
 *   - 重新生成（REQ-063/A47，v1.26）：TurnBlock emit regenerate → store.regenerate（api.md
 *     §2.12 POST——同轮原地重渲：状态置 running + 清空渲染态 + SSE 重订阅，§3 生命周期 14；
 *     失败 toast 在 store 内收敛，此处不重复弹）。
 * SSE 连接态横幅：sseState 驱动（handoff §4.4 接线清单 ReconnectBanner）。
 * M2/M4 范围留位（本批不实现）：IncognitoTurnBanner、SupersededBanner、
 * 深度正文内目录（REQ-013）、导出/分享后端链路。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute } from 'vue-router'

import { normalizeError } from '@/api/client'
import { fetchSnapshot } from '@/api/sources'
import { fetchThread } from '@/api/threads'
import OutlineAnchor from '@/components/answer/OutlineAnchor.vue'
import type { OutlineItem } from '@/components/answer/OutlineAnchor.vue'
import ScrollToBottomFab from '@/components/answer/ScrollToBottomFab.vue'
import SourcePanel from '@/components/answer/SourcePanel.vue'
import type { SourcePanelGroup } from '@/components/answer/SourcePanel.vue'
import TurnBlock from '@/components/answer/TurnBlock.vue'
import SearchCard from '@/components/search/SearchCard.vue'
import { AnswerScrollController } from '@/composables/answerScroll'
import { toast } from '@/composables/toast'
import { useThreadListStore } from '@/stores/threadList'
import { useTurnStore } from '@/stores/turn'
import { isHttpUrl, isUuid } from '@/utils/guards'
import { turnIdFromAnchor } from '@/utils/anchors'
import type { AskMode } from '@/types/api'

const route = useRoute()
const { t } = useI18n()
const store = useTurnStore()
const listStore = useThreadListStore()

const threadId = computed<string | null>(() =>
  isUuid(route.params.threadId) ? route.params.threadId : null,
)

const threadTitle = ref('')
const turnIds = ref<string[]>([])
const loadError = ref(false)
const mode = ref<AskMode>('fast')
const followQuery = ref('')
const outlineItems = ref<OutlineItem[]>([])
const outlineVisible = ref(false)
const activeAnchorId = ref<string | null>(null)
/** 当前查看轮（跟随态 prop 源）：锚点 id `turn-{turnId}-q` 派生轮 id（Perplexity 对齐小批，锚点行为零变更） */
const activeTurnId = computed<string | null>(() => turnIdFromAnchor(activeAnchorId.value))
const showCompacting = ref(false)

const scrollEl = ref<HTMLElement | null>(null)
const panelEl = ref<HTMLElement | null>(null)
/** 追问卡实例（REQ-055「编辑查询」预填后 focus()——SearchCard defineExpose） */
const followCardEl = ref<InstanceType<typeof SearchCard> | null>(null)
/** 来源面板定位高亮序号（A40「来源」：每次点击自增 → SourcePanel 内聚 2s 高亮态） */
const panelHighlightSeq = ref(0)
/** 来源面板折叠态联动序号（评审 P1/P2-1：onCite/onLocateSources 折叠态先强制展开再定位） */
const panelExpandSeq = ref(0)

/** 可渲染轮次（store 行就位即渲染；reconcile 未返回的轮暂不出现） */
const renderableTurns = computed(() =>
  turnIds.value
    .map((id) => store.turns[id])
    .filter((t): t is NonNullable<typeof t> => t !== undefined),
)

/* ---------- 滚动控制器（REQ-043 状态机装配） ---------- */
let controller: AnswerScrollController | null = null
const showFab = ref(false)
/** 跟随相位镜像（e2e 测试通道：data-scroll-phase——流式静默段下 scrollTop 停驻不可区分
 *  「接管生效」与「流式恰好静默」，相位是黑盒外唯一可靠观测；与 data-testid 同族约定） */
const scrollPhase = ref<AnswerScrollController['phase']>('following')

/** 流式进行中（快速 token / 深度撰写阶段；queued 等待期无内容增长，跟随无害） */
const liveTurn = computed(() => {
  for (let i = turnIds.value.length - 1; i >= 0; i -= 1) {
    const t = store.turns[turnIds.value[i]!]
    if (t && (t.status === 'running' || t.status === 'queued')) return t
  }
  return null
})
const isStreaming = computed(() => liveTurn.value?.status === 'running')

/**
 * 来源面板分组投影（frontend-api.md §5 v1.21 / REQ-061/A45——线程级累计）：
 * renderableTurns 中 sources 非空或该轮 collecting 的轮各成一组、按轮次序排列；direct 轮
 * （零 sources 且非收集态）不出现组——沿 v1.15「无来源不渲染」口径。「收集中」徽标只挂
 * 当前收集组组头（live 轮且已进搜索阶段：sources 到达或 stage_key=search——v1.16 钉正口径，
 * queued/rewrite 期不显示）。追问后新组追加、旧组保留、回看历史轮面板不重置（聚合态天然
 * 满足）；SSE per-turn sources 事件与 §2.5 对账零变更（纯前端聚合消费）。
 */
const panelGroups = computed<SourcePanelGroup[]>(() => {
  const out: SourcePanelGroup[] = []
  for (const turn of renderableTurns.value) {
    const isLive = turn.status === 'running' || turn.status === 'queued'
    const turnCollecting = isLive && (turn.sources.length > 0 || turn.stages.some((s) => s.stage_key === 'search'))
    if (turn.sources.length > 0 || turnCollecting) {
      out.push({ turnId: turn.turnId, question: turn.question, sources: turn.sources, collecting: turnCollecting })
    }
  }
  return out
})
const canStop = computed(() => liveTurn.value !== null)

watch(isStreaming, (v) => controller?.setStreaming(v), { immediate: true })

/** 流式内容增长 → nextTick 后贴底（DOM 更新后 scrollHeight 才反映新内容） */
watch(
  () => {
    const t = liveTurn.value
    return t ? `${t.turnId}:${t.answer_md.length}:${t.thinking_md.length}` : ''
  },
  async (cur, prev) => {
    if (!cur || cur === prev) return
    await nextTick()
    controller?.onContentGrew()
    await refreshOutlineMetrics()
  },
)

/** 深度轮终态（completed/degraded）后追问框自动回 fast（REQ-017，前端逻辑） */
watch(
  renderableTurns,
  (turns) => {
    if (mode.value !== 'deep') return
    const deepDone = turns.some((t) => t.mode === 'deep' && (t.status === 'completed' || t.status === 'degraded'))
    if (deepDone) mode.value = 'fast'
  },
)

/** context_compact 瞬态提示（§3：短暂状态，数秒自动消失——页面级 5s 计时） */
let compactTimer: ReturnType<typeof setTimeout> | null = null
watch(
  () => liveTurn.value?.compactAt ?? null,
  (ts) => {
    if (!ts) return
    showCompacting.value = true
    if (compactTimer) clearTimeout(compactTimer)
    compactTimer = setTimeout(() => {
      showCompacting.value = false
    }, 5_000)
  },
)

/* ---------- 锚点装配（修复项③：条目只保留用户问题——AI 回复标题/正文不再是锚点） ---------- */

/** 块首文本截断（问题条目文本 = 去标记后的前 48 字符） */
function plainHead(md: string): string {
  const text = md
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/[#>*`~[\]]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
  return text.length > 48 ? `${text.slice(0, 48)}…` : text
}

function rebuildOutline(): void {
  const items: OutlineItem[] = []
  for (const id of turnIds.value) {
    const t = store.turns[id]
    if (!t) continue
    items.push({ id: `turn-${id}-q`, label: plainHead(t.question) })
  }
  outlineItems.value = items
}

/** 锚点度量重建（isCurrent = P1 装配代数守卫：await nextTick 窗口内切线程时旧代数不写锚点态） */
async function refreshOutlineMetrics(isCurrent?: () => boolean): Promise<void> {
  await nextTick()
  if (isCurrent && !isCurrent()) return
  rebuildOutline()
  // 出现条件（A27）：条目 ≥ 2 或内容超约一屏
  const el = scrollEl.value
  outlineVisible.value = outlineItems.value.length >= 2 || (el !== null && el.scrollHeight > el.clientHeight)
}

function scrollToAnchor(id: string): void {
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

/** 滚动激活态实时跟随（收起态指示器放大 + 展开态文字主题色） */
let anchorRaf: number | null = null
function onScrollForAnchor(): void {
  if (anchorRaf !== null) return
  anchorRaf = requestAnimationFrame(() => {
    anchorRaf = null
    const container = scrollEl.value
    if (!container) return
    const containerTop = container.getBoundingClientRect().top
    let active: string | null = null
    for (const item of outlineItems.value) {
      const el = document.getElementById(item.id)
      if (!el) continue
      if (el.getBoundingClientRect().top - containerTop <= 96) active = item.id
      else break
    }
    activeAnchorId.value = active ?? outlineItems.value[0]?.id ?? null
  })
}

/* ---------- [n] 引用联动（REQ-003；v1.21 组作用域定位）----------
 * 定位 = 该轮组容器（data-turn-group={turnId}）内 querySelector [data-source-card]——
 * 修复全局 querySelector 命中首组同号卡缺陷（每轮 ref_no 从 1 起、跨组必然同号）。
 * turnId 走 isUuid 守卫（安全审计纵深防御：选择器拼接前收窄输入，非法 id 直接静默）。 */
let citeHitTimer: ReturnType<typeof setTimeout> | null = null
async function onCite(turnId: string, refNo: number): Promise<void> {
  if (!isUuid(turnId)) return
  const panel = panelEl.value
  if (!panel) return
  // 折叠态先强制展开（expandSeq 驱动 SourcePanel 双形态切回框架），nextTick 后组作用域定位才可达（P1）
  panelExpandSeq.value += 1
  await nextTick()
  const card = panel.querySelector(`[data-turn-group="${turnId}"] [data-source-card="${refNo}"]`)
  if (!(card instanceof HTMLElement)) return
  card.scrollIntoView({ behavior: 'smooth', block: 'center' })
  card.classList.add('cite-hit')
  if (citeHitTimer) clearTimeout(citeHitTimer)
  citeHitTimer = setTimeout(() => {
    card.classList.remove('cite-hit')
  }, 1_600)
}

/**
 * 「来源」操作行（REQ-056/A40）：面板滚动到位 + 2s 高亮。
 * 高亮态收敛在 SourcePanel（highlightSeq prop 自增驱动）；direct/无来源轮面板不渲染
 * （panelGroups 为空），面板缺位时静默 no-op（与无空态口径一致，不报错）。
 */
async function onLocateSources(): Promise<void> {
  const panel = panelEl.value
  if (!panel || panelGroups.value.length === 0) return
  // 折叠态先强制展开再定位高亮（P2-1：胶囊外框闪烁→面板全形态展开）
  panelExpandSeq.value += 1
  await nextTick()
  panel.scrollIntoView({ behavior: 'smooth', block: 'start' })
  panelHighlightSeq.value += 1
}

/* ---------- 编辑查询（REQ-055/A39）：预填追问框并聚焦——不提交、不改写历史轮次 ---------- */
async function onEditQuestion(question: string): Promise<void> {
  followQuery.value = question
  await nextTick()
  followCardEl.value?.focus()
}

/* ---------- 复刻（REQ-056/A40）：原问题原模式走既有追问流（REQ-042 并发策略照常适用） ---------- */
async function onRerun(turn: { question: string; mode: AskMode }): Promise<void> {
  const tid = threadId.value
  if (!tid || followingUp.value) return
  followingUp.value = true
  try {
    const data = await store.followUp(tid, turn.question, turn.mode)
    turnIds.value.push(data.turn_id)
    // REQ-043 恢复途径③：新轮开始 → 恢复自动跟随
    controller?.notifyNewTurn()
    await refreshOutlineMetrics()
  } catch (e) {
    toast(normalizeError(e).message)
  } finally {
    followingUp.value = false
  }
}

/* ---------- 重新生成（REQ-063/A47，v1.26）：degraded/failed-with-sources fast 轮仅重跑
 * synthesis（api.md §2.12）——同轮原地重渲（状态置 running + 清空渲染态 + SSE 重订阅），
 * 不新增轮（turnIds 不变）；失败 toast 已在 store.regenerate 内收敛（409006/404001/网络），
 * 此处不再重复弹。regenerate 后 liveTurn 扫到 running 轮 → isStreaming/canStop 既有链路
 * 自动接管（流式跟随 + 停止钮），零改动。 */
async function onRegenerate(turn: { turnId: string }): Promise<void> {
  await store.regenerate(turn.turnId)
}

/* ---------- followups chips（REQ-046）：点击 chip = 该文本追问（契约 §5 v1.9：mode=fast 默认） ---------- */
async function onFollowChip(question: string): Promise<void> {
  const tid = threadId.value
  if (!tid || followingUp.value) return
  followingUp.value = true
  try {
    const data = await store.followUp(tid, question, 'fast')
    turnIds.value.push(data.turn_id)
    controller?.notifyNewTurn()
    await refreshOutlineMetrics()
  } catch (e) {
    toast(normalizeError(e).message)
  } finally {
    followingUp.value = false
  }
}

/* ---------- 快照（api.md §5.1：10 分钟预签名 URL，新开页；出口面 = api/sources.ts） ---------- */
const snapshotLoading = ref(false)
async function onSnapshot(sourceId: string): Promise<void> {
  if (snapshotLoading.value) return
  snapshotLoading.value = true
  try {
    const data = await fetchSnapshot(sourceId)
    if (isHttpUrl(data.download_url)) {
      window.open(data.download_url, '_blank', 'noopener,noreferrer')
    } else {
      toast(t('answer.snapshotUnavailable'))
    }
  } catch (e) {
    toast(normalizeError(e).message)
  } finally {
    snapshotLoading.value = false
  }
}

/* ---------- 追问（2.4；§1 AnswerView 行） ---------- */
const followingUp = ref(false)
async function onFollowSubmit(): Promise<void> {
  const tid = threadId.value
  const q = followQuery.value.trim()
  if (!tid || !q || followingUp.value) return
  followingUp.value = true
  try {
    const data = await store.followUp(tid, q, mode.value)
    followQuery.value = ''
    turnIds.value.push(data.turn_id)
    // REQ-043 恢复途径③：发送新消息/新一轮开始 → 恢复自动跟随
    controller?.notifyNewTurn()
    await refreshOutlineMetrics()
  } catch (e) {
    toast(normalizeError(e).message)
  } finally {
    followingUp.value = false
  }
}

async function onCancel(): Promise<void> {
  const t = liveTurn.value
  if (!t) return
  try {
    await store.cancel(t.turnId)
  } catch (e) {
    toast(normalizeError(e).message)
  }
}

/* ---------- 生命周期接线（L5 进入 / L11 切走 / 路由参数切换重载） ---------- */

/**
 * 装配代数（P1 stale-request 守卫）：watch(threadId) 每次触发先自增使在途旧代数全部失效；
 * loadThread 捕获自己的代数、在每个 await 点后校验——A 慢 B 快乱序时 A 的后到响应不再覆盖
 * B 的 title/turnIds，也不会补发 calibrate/reconcile/subscribe 造成 closeStream 兜底之外
 * 的悬挂连接。
 */
let loadSeq = 0

/** 线程装配（L5 取数段；watch(threadId) 唯一调用路径） */
async function loadThread(tid: string): Promise<void> {
  const seq = ++loadSeq
  try {
    const detail = await fetchThread(tid)
    if (seq !== loadSeq) return
    threadTitle.value = detail.title
    turnIds.value = detail.turns.map((t) => t.turn_id)
    // 打开即已读（api.md §2.3 v1.18 写副作用——服务端已置 read_at=now()；前端本地清侧栏/
    // 会话页未读点，零额外请求，REQ-060/A44 未读流转）
    listStore.markReadLocally(tid)
    // 线程装配快照（2.3 权威数据 → App 顶栏导出/分享消费；覆盖式随切线程重装更新）
    store.setThreadMeta(tid, { title: detail.title, turnIds: detail.turns.map((t) => t.turn_id) })
    store.calibrateThread(tid, detail.turns.map((t) => t.status))
    // P3：逐轮对账并行化（Promise.allSettled）——单轮对账失败不再串行阻断后续轮与订阅段；
    // 失败兜底语义统一走 store.safeReconcile（连接态复位 + toast + 非终态有限重试）
    await Promise.allSettled(detail.turns.map((turn) => store.safeReconcile(turn.turn_id)))
    if (seq !== loadSeq) return
    for (const turn of detail.turns) {
      const status = store.turns[turn.turn_id]?.status
      if (status === 'running' || status === 'queued') store.subscribe(turn.turn_id)
    }
    await refreshOutlineMetrics(() => seq === loadSeq)
  } catch (e) {
    if (seq !== loadSeq) return
    loadError.value = true
    toast(normalizeError(e).message)
  }
}

onMounted(() => {
  const el = scrollEl.value
  if (el) {
    controller = new AnswerScrollController(el)
    controller.onStateChange = (s) => {
      showFab.value = s.showFab
      scrollPhase.value = s.phase
    }
    el.addEventListener('scroll', onScrollForAnchor, { passive: true })
  }
})

/**
 * 路由参数切换重载（修复项①：RouterView 复用 AnswerView 实例，onMounted 不再触发）：
 * 侧栏「最近」点击另一线程时组件不重挂——watch(threadId) 接管：
 * 先自增装配代数使在途旧 loadThread 全部失效（清理段执行前，P1），prev 线程再 closeStream
 * 全部流（连接注册表保证同 turnId 至多一条连接；liveTurnId/sseState 随 closeStream 复位）
 * + 复位标题/轮列表/锚点/错误态，再 loadThread 新线程。
 */
watch(threadId, async (tid, prev) => {
  loadSeq += 1
  if (prev) {
    for (const id of turnIds.value) store.closeStream(id)
    threadTitle.value = ''
    turnIds.value = []
    outlineItems.value = []
    activeAnchorId.value = null
    loadError.value = false
    // P2 清理段补全：① A 的追问草稿不带入 B；③ 控制器相位复位恢复跟随（notifyNewTurn
    //   与首次挂载路径同口径——B 新内容自动跟随）；② 视口复位顶部（显式置 0 须在
    //   notifyNewTurn 的 stickToBottom 之后执行——stick 于 DOM 尚未重挂帧读取旧
    //   scrollHeight 会落到 A 的底部，随后的 0 保证最终停在顶部）
    followQuery.value = ''
    controller?.notifyNewTurn()
    if (scrollEl.value) scrollEl.value.scrollTop = 0
  }
  if (!tid) return
  await loadThread(tid)
}, { immediate: true })

onUnmounted(() => {
  // L11：路由切走主动断流（研究不中断——连接与后端运行解耦，ADR-003）
  for (const id of turnIds.value) store.closeStream(id)
  const el = scrollEl.value
  if (el) el.removeEventListener('scroll', onScrollForAnchor)
  controller?.detach()
  controller = null
  if (compactTimer) clearTimeout(compactTimer)
  if (citeHitTimer) clearTimeout(citeHitTimer)
  if (anchorRaf !== null) cancelAnimationFrame(anchorRaf)
})
</script>

<template>
  <section class="answer-page" data-testid="answer-view">
    <div v-if="!threadId" class="answer-empty" role="status">{{ $t('common.invalidThread') }}</div>
    <template v-else>
      <div v-if="loadError" class="answer-empty" role="status">{{ $t('answer.loadFailed') }}</div>

      <div ref="scrollEl" class="content" data-testid="answer-scroll" :data-scroll-phase="scrollPhase">
        <div class="answer-body">
          <div class="answer-grid">
            <div class="answer-col" data-testid="answer-col">
              <h1 v-if="threadTitle" class="thread-title">{{ threadTitle }}</h1>

              <!-- SSE 连接态横幅（handoff §4.4 接线清单 ReconnectBanner） -->
              <div v-if="store.sseState === 'reconnecting'" class="sse-banner" role="status" data-testid="sse-banner">
                <span class="dot dot-warning"></span>{{ $t('answer.sseReconnecting') }}
              </div>
              <div v-else-if="store.sseState === 'connected'" class="sse-banner" role="status" data-testid="sse-connected">
                <span class="dot dot-accent"></span>{{ $t('answer.sseConnected') }}
              </div>

              <TurnBlock
                v-for="turn in renderableTurns"
                :key="turn.turnId"
                :turn="turn"
                :compact-note="liveTurn?.turnId === turn.turnId && showCompacting"
                @cite="onCite"
                @edit-question="onEditQuestion"
                @rerun="() => onRerun(turn)"
                @sources="onLocateSources"
                @follow="onFollowChip"
                @regenerate="() => onRegenerate(turn)"
              />

              <p v-if="renderableTurns.length === 0 && !loadError" class="answer-empty" role="status">
                {{ $t('answer.emptyThread') }}
              </p>
            </div>

            <!-- 来源面板门控（§5 v1.21 分组语义）：panelGroups 为空整块不渲染（direct 轮
                无面板/无徽标/无「来源 0」占位）；groups = 按轮分组投影（REQ-061 线程级
                累计）；highlight-seq = 操作行「来源」定位高亮（A40，SourcePanel 内聚 2s） -->
            <div ref="panelEl" class="panel-host">
              <SourcePanel
                v-if="panelGroups.length > 0"
                :groups="panelGroups"
                :highlight-seq="panelHighlightSeq"
                :active-turn-id="activeTurnId"
                :expand-seq="panelExpandSeq"
                @snapshot="onSnapshot"
              />
            </div>
          </div>
        </div>

        <!-- 底部固定追问框（常驻模式切换 REQ-007；运行中提交钮=停止 §2） -->
        <div class="follow-bar">
          <!-- 卡片宿主（相对定位包含块）：fab 与卡片同参照系——宿主顶边=卡片顶边，
              .fab-above-input 的 bottom=宿主 100%+--space-2 即「卡片顶上方 8px」（e2e
              A28 几何断言以卡片（follow-bar testid 元素）顶边为基准，gap 恒=--space-2） -->
          <div class="follow-card-host">
            <!-- 向下箭头（REQ-045）：悬停于输入框上方中央 -->
            <div class="fab-above-input">
              <ScrollToBottomFab :visible="showFab" @click="controller?.resumeViaFab()" />
            </div>
            <SearchCard
              ref="followCardEl"
              v-model="followQuery"
              v-model:mode="mode"
              :streaming="canStop"
              :placeholder="canStop ? $t('answer.followDisabledHint') : $t('answer.followPlaceholder')"
              data-testid="follow-bar"
              @submit="onFollowSubmit"
              @cancel="onCancel"
            />
          </div>
          <div class="sse-note">
            <span class="dot" :class="store.sseState === 'connected' ? 'dot-accent' : ''" style="width: 6px; height: 6px"></span>
            {{ store.sseState === 'connected' ? $t('answer.sseNote') : $t('answer.sseIdleNote') }}
          </div>
        </div>
      </div>

      <!-- 右侧导航锚点（REQ-044）：fixed 悬浮层，不占文档流 -->
      <OutlineAnchor
        :items="outlineItems"
        :active-id="activeAnchorId"
        :visible="outlineVisible"
        @select="scrollToAnchor"
      />
    </template>
  </section>
</template>

<style scoped>
.answer-page {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
}
.answer-empty {
  padding: var(--space-8);
  color: var(--color-text-tertiary);
  font-size: var(--text-base);
  text-align: center;
}
.content {
  flex: 1;
  overflow-y: auto;
  min-height: 0;
}
.answer-body {
  padding: var(--space-4) var(--space-8) var(--space-16);
}
.answer-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) var(--layout-sources-w);
  gap: var(--space-8);
  max-width: 1160px;
  margin: 0 auto;
}
.answer-col {
  min-width: 0;
}
.thread-title {
  font-size: var(--text-xl);
  font-weight: var(--weight-semibold);
  margin-bottom: var(--space-6);
}
.sse-banner {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  margin-bottom: var(--space-4);
}
/* 卡片宿主：居中收窄到卡片宽度（max-width/margin 自 .follow-bar :deep(.search-card) 上移），
   并作为 .fab-above-input 的相对定位包含块——宿主顶边=卡片顶边，fab 定位与卡片同参照系
   （修复：此前锚在 .follow-bar 边框盒上，顶部 padding 使实际 gap=16+8=24px，与注释意图
   「卡片顶 + --space-2」错位） */
.follow-card-host {
  position: relative;
  max-width: 900px;
  margin: 0 auto;
}
/* 向下箭头（REQ-045）：输入框上方中央悬空——bottom = 宿主(卡片)顶上方 --space-2 */
.fab-above-input {
  position: absolute;
  left: 50%;
  bottom: calc(100% + var(--space-2));
  transform: translateX(-50%);
  z-index: 20;
}
.follow-bar {
  position: sticky;
  bottom: 0;
  z-index: 10;
  padding: var(--space-4) var(--space-8) var(--space-5);
  /* 渐隐遮罩主背景透明变体（v1.3 主题 token）：主题切换时跟随主背景色相（REQ-054） */
  background: linear-gradient(180deg, var(--color-bg-primary-translucent) 0%, var(--color-bg-primary) 28%);
}
.follow-bar :deep(.search-card) {
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-floating);
}
.sse-note {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: var(--space-2);
  padding-top: var(--space-2);
  color: var(--color-text-faint);
  font-size: var(--text-xs);
}
.panel-host {
  min-width: 0;
}
@media (max-width: 1200px) {
  .answer-grid {
    grid-template-columns: 1fr;
  }
  .panel-host {
    order: -1;
  }
}
@media (max-width: 720px) {
  .answer-body {
    padding: var(--space-4) var(--space-4) var(--space-12);
  }
  .follow-bar {
    padding: var(--space-3) var(--space-4) var(--space-4);
  }
}
</style>
