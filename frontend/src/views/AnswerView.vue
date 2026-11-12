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
 * SSE 连接态横幅：sseState 驱动（handoff §4.4 接线清单 ReconnectBanner）。
 * M2/M4 范围留位（本批不实现）：FollowupChips、IncognitoTurnBanner、SupersededBanner、
 * 深度正文内目录（REQ-013）、导出/分享后端链路。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute } from 'vue-router'

import { apiGet, normalizeError } from '@/api/client'
import { fetchThread } from '@/api/threads'
import OutlineAnchor from '@/components/answer/OutlineAnchor.vue'
import type { OutlineItem } from '@/components/answer/OutlineAnchor.vue'
import ScrollToBottomFab from '@/components/answer/ScrollToBottomFab.vue'
import SourcePanel from '@/components/answer/SourcePanel.vue'
import TurnBlock from '@/components/answer/TurnBlock.vue'
import SearchCard from '@/components/search/SearchCard.vue'
import { AnswerScrollController } from '@/composables/answerScroll'
import { toast } from '@/composables/toast'
import type { HeadingInfo } from '@/components/markdown/renderer'
import { useTurnStore } from '@/stores/turn'
import { isHttpUrl, isUuid } from '@/utils/guards'
import type { AskMode, SnapshotData } from '@/types/api'

const route = useRoute()
const { t } = useI18n()
const store = useTurnStore()

const threadId = computed<string | null>(() =>
  isUuid(route.params.threadId) ? route.params.threadId : null,
)

const threadTitle = ref('')
const turnIds = ref<string[]>([])
const loadError = ref(false)
const mode = ref<AskMode>('fast')
const followQuery = ref('')
const headingsByTurn = ref<Record<string, HeadingInfo[]>>({})
const outlineItems = ref<OutlineItem[]>([])
const outlineVisible = ref(false)
const activeAnchorId = ref<string | null>(null)
const showCompacting = ref(false)

const scrollEl = ref<HTMLElement | null>(null)
const panelEl = ref<HTMLElement | null>(null)

/** 可渲染轮次（store 行就位即渲染；reconcile 未返回的轮暂不出现） */
const renderableTurns = computed(() =>
  turnIds.value
    .map((id) => store.turns[id])
    .filter((t): t is NonNullable<typeof t> => t !== undefined),
)

/* ---------- 滚动控制器（REQ-043 状态机装配） ---------- */
let controller: AnswerScrollController | null = null
const showFab = ref(false)

/** 流式进行中（快速 token / 深度撰写阶段；queued 等待期无内容增长，跟随无害） */
const liveTurn = computed(() => {
  for (let i = turnIds.value.length - 1; i >= 0; i -= 1) {
    const t = store.turns[turnIds.value[i]!]
    if (t && (t.status === 'running' || t.status === 'queued')) return t
  }
  return null
})
const isStreaming = computed(() => liveTurn.value?.status === 'running')

/** 来源面板投影：进行中轮优先（liveTurnId），否则最新一轮（回看态） */
const panelTurnId = computed(() => store.liveTurnId ?? turnIds.value[turnIds.value.length - 1] ?? null)
const panelSources = computed(() => store.turns[panelTurnId.value ?? '']?.sources ?? [])
const collecting = computed(() => liveTurn.value !== null)
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

/* ---------- 锚点装配（OutlineAnchor 数据源 = AnswerRenderer 标题 id 机制） ---------- */

/** 块首文本截断（快答合并 1 条的条目文本 = 去标记后的前 48 字符） */
function plainHead(md: string): string {
  const text = md
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/[#>*`~[\]]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
  return text.length > 48 ? `${text.slice(0, 48)}…` : text
}

function truncateLabel(text: string): string {
  return text.length > 48 ? `${text.slice(0, 48)}…` : text
}

function rebuildOutline(): void {
  const items: OutlineItem[] = []
  for (const id of turnIds.value) {
    const t = store.turns[id]
    if (!t) continue
    items.push({ id: `turn-${id}-q`, label: plainHead(t.question) })
    const heads = headingsByTurn.value[id]
    if (heads && heads.length > 0) {
      // 深度报告按 h2 拆分（粒度裁决：内容块级）；快速短答合并 1 条
      for (const h of heads) items.push({ id: h.id, label: truncateLabel(h.text) })
    } else if (t.answer_md) {
      items.push({ id: `turn-${id}-a`, label: plainHead(t.answer_md) })
    }
  }
  outlineItems.value = items
}

async function refreshOutlineMetrics(): Promise<void> {
  await nextTick()
  rebuildOutline()
  // 出现条件（A27）：条目 ≥ 2 或内容超约一屏
  const el = scrollEl.value
  outlineVisible.value = outlineItems.value.length >= 2 || (el !== null && el.scrollHeight > el.clientHeight)
}

function onTurnHeadings(turnId: string, items: HeadingInfo[]): void {
  headingsByTurn.value[turnId] = items
  void refreshOutlineMetrics()
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

/* ---------- [n] 引用联动（REQ-003）：定位来源卡 + 高亮 ---------- */
let citeHitTimer: ReturnType<typeof setTimeout> | null = null
function onCite(refNo: number): void {
  const panel = panelEl.value
  if (!panel) return
  const card = panel.querySelector(`[data-source-card="${refNo}"]`)
  if (!(card instanceof HTMLElement)) return
  card.scrollIntoView({ behavior: 'smooth', block: 'center' })
  card.classList.add('cite-hit')
  if (citeHitTimer) clearTimeout(citeHitTimer)
  citeHitTimer = setTimeout(() => {
    card.classList.remove('cite-hit')
  }, 1_600)
}

/* ---------- 快照（api.md §5.1：10 分钟预签名 URL，新开页） ---------- */
const snapshotLoading = ref(false)
async function onSnapshot(sourceId: string): Promise<void> {
  if (snapshotLoading.value) return
  snapshotLoading.value = true
  try {
    const data = await apiGet<SnapshotData>(`/api/v1/sources/${sourceId}/snapshot`)
    if (isHttpUrl(data.download_url)) {
      window.open(data.download_url, '_blank', 'noopener')
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

/* ---------- 生命周期接线（L5 进入 / L11 切走） ---------- */
onMounted(async () => {
  const el = scrollEl.value
  if (el) {
    controller = new AnswerScrollController(el)
    controller.onStateChange = (s) => {
      showFab.value = s.showFab
    }
    el.addEventListener('scroll', onScrollForAnchor, { passive: true })
  }
  const tid = threadId.value
  if (!tid) return
  try {
    const detail = await fetchThread(tid)
    threadTitle.value = detail.title
    turnIds.value = detail.turns.map((t) => t.turn_id)
    store.calibrateThread(tid, detail.turns.map((t) => t.status))
    for (const turn of detail.turns) {
      await store.reconcile(turn.turn_id)
    }
    for (const turn of detail.turns) {
      const status = store.turns[turn.turn_id]?.status
      if (status === 'running' || status === 'queued') store.subscribe(turn.turn_id)
    }
    await refreshOutlineMetrics()
  } catch (e) {
    loadError.value = true
    toast(normalizeError(e).message)
  }
})

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

      <div ref="scrollEl" class="content" data-testid="answer-scroll">
        <div class="answer-body">
          <div class="answer-grid">
            <div class="answer-col" data-testid="answer-col">
              <!-- 顶部向下箭头（REQ-045）：零高 sticky 槽位悬停于对话列顶部中央 -->
              <div class="scroll-fab-slot">
                <ScrollToBottomFab :visible="showFab" @click="controller?.resumeViaFab()" />
              </div>

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
                @headings="onTurnHeadings"
                @cite="onCite"
              />

              <p v-if="renderableTurns.length === 0 && !loadError" class="answer-empty" role="status">
                {{ $t('answer.emptyThread') }}
              </p>
            </div>

            <div ref="panelEl" class="panel-host">
              <SourcePanel :sources="panelSources" :collecting="collecting" @snapshot="onSnapshot" />
            </div>
          </div>
        </div>

        <!-- 底部固定追问框（常驻模式切换 REQ-007；运行中提交钮=停止 §2） -->
        <div class="follow-bar">
          <SearchCard
            v-model="followQuery"
            v-model:mode="mode"
            :streaming="canStop"
            :placeholder="canStop ? $t('answer.followDisabledHint') : $t('answer.followPlaceholder')"
            data-testid="follow-bar"
            @submit="onFollowSubmit"
            @cancel="onCancel"
          />
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
.scroll-fab-slot {
  position: sticky;
  top: var(--space-3);
  z-index: 20;
  height: 0;
  display: flex;
  justify-content: center;
}
.follow-bar {
  position: sticky;
  bottom: 0;
  z-index: 10;
  padding: var(--space-4) var(--space-8) var(--space-5);
  background: linear-gradient(180deg, rgba(25, 26, 26, 0) 0%, var(--color-bg-primary) 28%);
}
.follow-bar :deep(.search-card) {
  max-width: 900px;
  margin: 0 auto;
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
