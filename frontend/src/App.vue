<script setup lang="ts">
/**
 * App 根组件 = 组合面（vue-best-practices）：全局布局骨架（侧栏 + 顶栏 + RouterView）+
 * 图标 sprite + Toast + 命令面板出口。页面级 feature 与取数全部下沉 views/ 与 stores/。
 * 布局/视觉 = docs/design/tokens.css 布局骨架（.app/.sidebar/.main/.topbar）+ 01~04 设计稿；
 * 侧栏「最近」= threadListStore（§1 侧栏行：GET /threads?limit=10，进入时拉取）；
 * 顶栏按路由差异化：home=自用版 pill / answer=返回+页签+导出 Markdown（整线程本地下载）+
 * 分享（复制会话链接）/ 其余=简洁；
 * <1080px 侧栏收抽屉（tokens.css body.sb-open 机制）。
 * 侧栏图标区三改（修复项⑧，2026-11-14）：
 *   a) 铃铛 disabled 占位删除（common.notifications 键一并移除）；
 *   b) 放大镜 = 命令面板（CommandPalette，Cmd/Ctrl+K 全局快捷键开合）；
 *   c) 折叠图标桌面语义 = 侧栏 240→56px 图标栏（sidebarCollapsed，localStorage 持久化）；
 *      ≤1080px 抽屉行为保持 sidebarOpen 原状（两个状态语义分离不混用）。
 * 侧栏最近区头激活（v1.24 五项修复小批）：区头整体按钮语义折叠/展开（localStorage
 * `ai-search.recent-collapsed` 持久化，try/catch 同 sidebarCollapsed 模式；chevron 旋转
 * 过渡）+ 筛选钮 hover 显形（hover-reveal 对标 Perplexity）→ 小弹层 全部/快速/深度
 * （store.recentFilter 本地过滤 recentFiltered；UiTooltip 主/副题 threads.filterTitle/Sub）。
 * 底部用户区 = ThemeSwitcher（REQ-054/A38：主题三选弹层入口，v1.18）。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute } from 'vue-router'

import AppIconSprite from '@/components/icons/AppIconSprite.vue'
import UiIcon from '@/components/icons/UiIcon.vue'
import ModelSelect from '@/components/search/ModelSelect.vue'
import CommandPalette from '@/components/common/CommandPalette.vue'
import ThemeSwitcher from '@/components/common/ThemeSwitcher.vue'
import ThreadActionMenu from '@/components/threads/ThreadActionMenu.vue'
import ToastHost from '@/components/common/ToastHost.vue'
import UiTooltip from '@/components/common/UiTooltip.vue'
import { useMediaQuery } from '@/composables/mediaQuery'
import { toast } from '@/composables/toast'
import { usePrefsStore } from '@/stores/prefs'
import { useThreadListStore } from '@/stores/threadList'
import { useTurnStore } from '@/stores/turn'
import { buildThreadLink, buildThreadMarkdown, downloadMarkdown, sanitizeFilename } from '@/utils/exportThread'
import { isUuid } from '@/utils/guards'
import { modeBadgeClass, modeBadgeText, relativeTime } from '@/utils/threadDisplay'
import type { ThreadListItem, ThreadModeSummary } from '@/types/api'

const route = useRoute()
const { t } = useI18n()
const prefs = usePrefsStore()
const listStore = useThreadListStore()
const turnStore = useTurnStore()

const isAnswer = computed(() => route.name === 'answer')
const isHome = computed(() => route.name === 'home')
const sidebarOpen = ref(false)

/** 当前答案页线程 id（isUuid 守卫；非法路由参数不触发导出/分享） */
const answerThreadId = computed<string | null>(() =>
  isAnswer.value && isUuid(route.params.threadId) ? route.params.threadId : null,
)

/* ---------- 顶栏分享 = 复制会话链接（origin + /thread/{threadId}；自用无公开分享后端——已定案） ---------- */
async function shareThreadLink(): Promise<void> {
  const tid = answerThreadId.value
  if (!tid) return
  try {
    await navigator.clipboard.writeText(buildThreadLink(tid))
    toast(t('answer.linkCopied'))
  } catch (error) {
    console.warn('[App] 分享链接复制失败', error)
    toast(t('answer.copyFailed'))
  }
}

/* ---------- 顶栏导出 Markdown = 整线程本地 .md 下载（前端本地零网络） ----------
 * 数据源 = turnStore.threadMetas（AnswerView 2.3 装配快照：标题 + created_at 升序轮次序）
 * + turnStore.turns 轮投影；批次3评审-F3 防静默：快照未就绪（threadMetas 无当前线程）或
 * 轮次投影为空时 toast 明示（answer.exportNotReady）并中止——不产出空文档下载；
 * 有数据时行为不变。 */
function exportThreadMarkdown(): void {
  const tid = answerThreadId.value
  if (!tid) return
  const meta = turnStore.threadMetas[tid]
  const turns = (meta?.turnIds ?? [])
    .map((id) => turnStore.turns[id])
    .filter((turn): turn is NonNullable<typeof turn> => turn !== undefined)
    .map((turn) => ({ question: turn.question, answer_md: turn.answer_md, sources: turn.sources }))
  if (!meta || turns.length === 0) {
    toast(t('answer.exportNotReady'))
    return
  }
  const title = meta.title
  downloadMarkdown(`${sanitizeFilename(title, 'thread')}.md`, buildThreadMarkdown(title, turns))
}

watch(sidebarOpen, (open) => {
  // tokens.css 抽屉机制以 body.sb-open 为选择器（<1080px）
  document.body.classList.toggle('sb-open', open)
})

/* ---------- 命令面板（修复项⑧b）：放大镜 + Cmd/Ctrl+K 全局快捷键 ---------- */
const paletteOpen = ref(false)
/** 触发按钮 ref（P3：面板关闭时焦点返还触发点，键盘 Tab 位置连续） */
const paletteTriggerEl = ref<HTMLButtonElement | null>(null)

function onPaletteClose(): void {
  paletteOpen.value = false
  paletteTriggerEl.value?.focus()
}

function onGlobalKeydown(e: KeyboardEvent): void {
  // Esc 优先关预览卡（palette/modal 各自监听，互不干扰）
  if (e.key === 'Escape' && recentPreview.value !== null) hideRecentPreview()
  // Esc 关筛选弹层（v1.24；筛选态保留不清）
  if (e.key === 'Escape' && recentFilterOpen.value) recentFilterOpen.value = false
  // 排除 shift/alt 组合（P3：不拦截浏览器 Cmd+Shift+K 等既有能力；面板仅认裸 Cmd/Ctrl+K）
  if (e.shiftKey || e.altKey) return
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    paletteOpen.value = !paletteOpen.value
  }
}

/* ---------- 侧栏折叠桌面语义（修复项⑧c）：>1080px 收图标栏，≤1080px 抽屉原状 ---------- */
const SIDEBAR_COLLAPSED_KEY = 'ai-search.sidebar-collapsed'

function readInitialCollapsed(): boolean {
  try {
    return localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === '1'
  } catch {
    return false // 隐私模式等存储不可用：默认展开
  }
}

const sidebarCollapsed = ref(readInitialCollapsed())
const isNarrowViewport = useMediaQuery('(max-width: 1080px)')

watch(sidebarCollapsed, (collapsed) => {
  try {
    localStorage.setItem(SIDEBAR_COLLAPSED_KEY, collapsed ? '1' : '0')
  } catch {
    // 存储不可用静默（会话内状态仍生效）
  }
})

/** 折叠按钮分派：窄屏 = 抽屉开合（原状）；桌面 = 图标栏折叠 */
function onSidebarToggle(): void {
  if (isNarrowViewport.value) sidebarOpen.value = !sidebarOpen.value
  else sidebarCollapsed.value = !sidebarCollapsed.value
}

/* ---------- 侧栏最近区头（v1.24）：折叠开关（localStorage 持久化）+ 筛选弹层 ----------
 * a) 区头整体按钮语义：点击折叠/展开最近列表（aria-expanded + aria-controls；折叠态保留
 *    区头行、列表 v-show 隐藏；chevron 旋转过渡对标折叠态样式）；
 * b) 筛选钮（hover 区头显形，对标 Perplexity hover-reveal）：弹小弹层选 全部/快速/深度 →
 *    store.setRecentFilter 本地过滤 recentFiltered（mode_summary；与会话页 modeFilter 独立）。 */
const RECENT_COLLAPSED_KEY = 'ai-search.recent-collapsed'

function readInitialRecentCollapsed(): boolean {
  try {
    return localStorage.getItem(RECENT_COLLAPSED_KEY) === '1'
  } catch {
    return false // 存储不可用：默认展开
  }
}

const recentCollapsed = ref(readInitialRecentCollapsed())

watch(recentCollapsed, (collapsed) => {
  try {
    localStorage.setItem(RECENT_COLLAPSED_KEY, collapsed ? '1' : '0')
  } catch {
    // 存储不可用静默（会话内状态仍生效）
  }
})

function toggleRecentCollapsed(): void {
  recentCollapsed.value = !recentCollapsed.value
}

/* ---------- 侧栏已置顶分区（v1.25 对标 Perplexity）：置顶项独立成区 ----------
 * 折叠开关同「最近」区头模式（localStorage ai-search.pinned-collapsed 读写 try/catch；
 * aria-expanded/controls + chevron 旋转过渡）；无置顶项整区不渲染；与「最近」折叠互不影响。 */
const PINNED_COLLAPSED_KEY = 'ai-search.pinned-collapsed'

function readInitialPinnedCollapsed(): boolean {
  try {
    return localStorage.getItem(PINNED_COLLAPSED_KEY) === '1'
  } catch {
    return false // 存储不可用：默认展开
  }
}

const pinnedCollapsed = ref(readInitialPinnedCollapsed())

watch(pinnedCollapsed, (collapsed) => {
  try {
    localStorage.setItem(PINNED_COLLAPSED_KEY, collapsed ? '1' : '0')
  } catch {
    // 存储不可用静默（会话内状态仍生效）
  }
})

function togglePinnedCollapsed(): void {
  pinnedCollapsed.value = !pinnedCollapsed.value
}

interface RecentFilterOption {
  key: 'all' | 'fast' | 'deep'
  value: ThreadModeSummary | null
}
const RECENT_FILTERS: RecentFilterOption[] = [
  { key: 'all', value: null },
  { key: 'fast', value: 'fast' },
  { key: 'deep', value: 'deep' },
]

const recentFilterOpen = ref(false)
const recentSectionEl = ref<HTMLElement | null>(null)

function pickRecentFilter(value: ThreadModeSummary | null): void {
  listStore.setRecentFilter(value)
  recentFilterOpen.value = false
}

/** 外点关闭（mousedown 在区头外；仅筛选弹层打开时） */
function onDocMousedown(e: MouseEvent): void {
  if (!recentFilterOpen.value) return
  const target = e.target
  if (target instanceof Node && recentSectionEl.value?.contains(target)) return
  recentFilterOpen.value = false
}

/**
 * 最近列表（§1 侧栏行 v1.21 / P2-2；v1.25 置顶分区拆分）：store.recentFiltered 独立投影
 * （fetchRecent limit=10 活跃集 + v1.24 区头筛选弹层 mode_summary 本地过滤，不受会话页
 * viewFilter/searchQuery/modeFilter 影响——items 是会话页当前视图列表）；防御性过滤归档
 * （服务端活跃集已保证，UI 不变量兜底）；v1.25 按 pinned 拆两投影——pinnedThreads=已置顶
 * 独立分区（无则整区不渲染）、unpinnedThreads=「最近」区（语义变为只显未置顶）；管理动作
 * 后随 store 双列表同步自愈（取消置顶后条目自动落入「最近」区）。
 */
const activeRecent = computed(() => listStore.recentFiltered.filter((t) => !t.archived))
const pinnedThreads = computed(() => activeRecent.value.filter((t) => t.pinned))
const unpinnedThreads = computed(() => activeRecent.value.filter((t) => !t.pinned))

/* ---------- 侧栏最近项 hover 预览卡 + 标题 marquee（v1.23；用户裁决双形态） ----------
 * 预览卡：hover ≥300ms 浮出（防误触），完整标题 + 相对时间 + 轮数 + 模式徽标（数据全在
 * ThreadListItem，零网络）；锚定行右侧、超下视口上翻；Esc/离开即隐（150ms 宽限供卡内
 * hover）；卡内点击不导航；与三点菜单互斥（行 mousedown 即隐）。
 * marquee：仅标题溢出时滚动（v1.24 终版=经典无缝左滚走马灯：双拷贝 + 30px/s 匀速单方向
 * infinite，见下方常量注），hover 起 / 离停；prefers-reduced-motion 不滚。 */
const PREVIEW_SHOW_DELAY_MS = 300
const PREVIEW_HIDE_GRACE_MS = 150
const PREVIEW_VIEWPORT_GAP = 8
/* marquee 节奏（v1.24 终版=经典无缝左滚走马灯）：匀速常数 30px/s（连续循环下匀速常数
   自然，无回程段无节奏畸变）；distance = 单份拷贝宽 + 接缝 gap——to 点=第二份拷贝左缘
   对齐原起点（循环回 from 时视觉无跳变=无缝）；接缝空档 42px（≈3em@14px text-sm），
   与 CSS .sb-recent-text.marquee .sb-recent-text-inner 的 gap 同值（注释互指防漂移）；
   distance 钳 ≤400px：scrollWidth 测量异常偏大/超长拷贝的视觉速度上限（连续循环下
   语义=更快匀速，400/30≈13.3s 循环周期）。 */
const MARQUEE_SPEED_PX_S = 30
const MARQUEE_COPY_GAP_PX = 42
const MARQUEE_MAX_DISTANCE_PX = 400

interface RecentPreview {
  thread: ThreadListItem
  top: number
  left: number
}

const recentPreview = ref<RecentPreview | null>(null)
const previewEl = ref<HTMLElement | null>(null)
let previewShowTimer: ReturnType<typeof setTimeout> | null = null
let previewHideTimer: ReturnType<typeof setTimeout> | null = null

/** marquee 激活态（单行：同一时刻至多一行 hover） */
const marquee = ref<{ id: string; distance: number; duration: number } | null>(null)
const reduceMotion = useMediaQuery('(prefers-reduced-motion: reduce)')

function clearPreviewShowTimer(): void {
  if (previewShowTimer !== null) {
    clearTimeout(previewShowTimer)
    previewShowTimer = null
  }
}

function clearPreviewHideTimer(): void {
  if (previewHideTimer !== null) {
    clearTimeout(previewHideTimer)
    previewHideTimer = null
  }
}

function hideRecentPreview(): void {
  clearPreviewShowTimer()
  clearPreviewHideTimer()
  recentPreview.value = null
  marquee.value = null
}

function onRecentEnter(thread: ThreadListItem, e: MouseEvent): void {
  const rowEl = e.currentTarget as HTMLElement
  // 卡内回行（离开宽限期内）：取消隐藏排程
  clearPreviewHideTimer()
  if (recentPreview.value?.thread.thread_id === thread.thread_id) return
  clearPreviewShowTimer()
  previewShowTimer = setTimeout(() => {
    previewShowTimer = null
    void openRecentPreview(thread, rowEl)
  }, PREVIEW_SHOW_DELAY_MS)
}

async function openRecentPreview(thread: ThreadListItem, rowEl: HTMLElement): Promise<void> {
  const rect = rowEl.getBoundingClientRect()
  recentPreview.value = { thread, top: rect.top, left: rect.right + PREVIEW_VIEWPORT_GAP }
  // marquee 触发条件 = 溢出才滚；reduced-motion 直接不滚（完整态静态截断维持）。
  // 单份态测量（激活前 scrollWidth=拷贝宽）；distance=拷贝宽+接缝 gap（无缝数学），
  // 钳 ≤400px；duration=distance/30px/s 匀速。
  marquee.value = null
  const textEl = rowEl.querySelector<HTMLElement>('.sb-recent-text')
  if (textEl && !reduceMotion.value && textEl.scrollWidth > textEl.clientWidth) {
    const distance = Math.min(textEl.scrollWidth + MARQUEE_COPY_GAP_PX, MARQUEE_MAX_DISTANCE_PX)
    marquee.value = { id: thread.thread_id, distance, duration: distance / MARQUEE_SPEED_PX_S }
  }
  // 超下视口时上翻（卡片实测高度驱动）
  await nextTick()
  const el = previewEl.value
  const state = recentPreview.value
  if (!el || !state || state.thread.thread_id !== thread.thread_id) return
  const maxTop = window.innerHeight - el.offsetHeight - PREVIEW_VIEWPORT_GAP
  if (state.top > maxTop) {
    recentPreview.value = { ...state, top: Math.max(PREVIEW_VIEWPORT_GAP, maxTop) }
  }
}

function onRecentLeave(): void {
  clearPreviewShowTimer()
  clearPreviewHideTimer()
  previewHideTimer = setTimeout(() => {
    previewHideTimer = null
    hideRecentPreview()
  }, PREVIEW_HIDE_GRACE_MS)
}

/** 模式徽标/相对时间共享展示函数（utils/threadDisplay.ts，会话页同源） */
function previewModeBadgeClass(mode: ThreadModeSummary): string {
  return modeBadgeClass(mode)
}

function previewModeBadgeText(mode: ThreadModeSummary): string {
  return modeBadgeText(mode, t)
}

function previewRelativeTime(iso: string): string {
  return relativeTime(iso, prefs.lang)
}

/** marquee 内联样式（溢出行注入距离/时长变量；非激活行不注入） */
function marqueeStyle(threadId: string): Record<string, string> | undefined {
  const m = marquee.value
  if (!m || m.id !== threadId) return undefined
  return { '--mq-distance': `${m.distance}px`, '--mq-duration': `${m.duration}s` }
}


function setLang(locale: 'zh-CN' | 'en-US'): void {
  prefs.setLang(locale)
}

onMounted(() => {
  // §1 侧栏行：GET /threads?limit=10（最近；P2-2 起走 store.fetchRecent 独立投影）；
  // 失败静默（侧栏为辅助导航）
  listStore.fetchRecent().catch(() => undefined)
  window.addEventListener('keydown', onGlobalKeydown)
  document.addEventListener('mousedown', onDocMousedown)
})

onUnmounted(() => {
  window.removeEventListener('keydown', onGlobalKeydown)
  document.removeEventListener('mousedown', onDocMousedown)
})
</script>

<template>
  <div class="app" data-testid="app-shell">
    <AppIconSprite />

    <!-- ================= 侧栏 ================= -->
    <aside class="sidebar" :class="{ collapsed: sidebarCollapsed }" data-testid="sidebar">
      <div class="sb-head">
        <RouterLink class="sb-logo" to="/" title="AI Search" data-testid="sidebar-logo">
          <svg class="icon-lg" aria-hidden="true"><use href="#i-logo" /></svg>
        </RouterLink>
        <div class="sb-head-actions">
          <!-- 放大镜 = 命令面板（修复项⑧b；Cmd/Ctrl+K 同入口）；v1.23 自定义 tooltip 替代原生 title -->
          <UiTooltip :label="$t('common.searchPlaceholder')" :sub="$t('common.searchSub')">
            <button
              ref="paletteTriggerEl"
              type="button"
              class="icon-btn"
              :aria-label="$t('common.searchPlaceholder')"
              data-testid="open-palette"
              @click="paletteOpen = true"
            >
              <UiIcon name="i-search" />
            </button>
          </UiTooltip>
          <!-- 折叠：桌面 = 图标栏折叠（sidebarCollapsed）；窄屏 = 抽屉（sidebarOpen，原状） -->
          <UiTooltip :label="sidebarCollapsed ? $t('common.openSidebar') : $t('common.collapseSidebar')">
            <button
              type="button"
              class="icon-btn"
              :aria-label="sidebarCollapsed ? $t('common.openSidebar') : $t('common.collapseSidebar')"
              :aria-expanded="sidebarCollapsed ? 'false' : 'true'"
              data-testid="sidebar-toggle"
              @click="onSidebarToggle"
            >
              <UiIcon name="i-panel" />
            </button>
          </UiTooltip>
        </div>
      </div>
      <nav class="sb-nav" aria-label="主导航">
        <RouterLink class="nav-item" to="/" data-testid="nav-home" @click="sidebarOpen = false">
          <UiIcon name="i-plus" />{{ $t('nav.home') }}
        </RouterLink>
        <RouterLink class="nav-item" to="/library" data-testid="nav-library" @click="sidebarOpen = false">
          <UiIcon name="i-book" />{{ $t('nav.library') }}
        </RouterLink>
        <RouterLink class="nav-item" to="/threads" data-testid="nav-threads" @click="sidebarOpen = false">
          <UiIcon name="i-clock" />{{ $t('nav.threads') }}
        </RouterLink>

        <!-- 已置顶分区（v1.25 对标 Perplexity）：置顶项独立成区（区头行样式与「最近」区头同款，
            无筛选钮）；无置顶项整区不渲染（template v-if 单点包控区头+列表）；折叠与「最近」
            互不影响；行结构与「最近」行同款（生成中指示/未读点/预览卡/marquee/三点菜单照常）
            + 标题右侧 i-pin 状态图钉 -->
        <template v-if="pinnedThreads.length > 0">
        <div class="sb-section" data-testid="sb-pinned-section">
          <button
            type="button"
            class="sb-section-toggle"
            :aria-expanded="pinnedCollapsed ? 'false' : 'true'"
            aria-controls="sb-pinned-list"
            data-testid="sb-pinned-toggle"
            @click="togglePinnedCollapsed"
          >
            <span>{{ $t('nav.pinned') }}</span>
            <UiIcon name="i-chev-d" size="sm" class="sb-chev" :class="{ flip: pinnedCollapsed }" data-testid="sb-pinned-chev" />
          </button>
        </div>
        <div id="sb-pinned-list" v-show="!pinnedCollapsed" class="sb-recent-list" data-testid="sb-pinned-list">
          <!-- 行结构与「最近」行同款（互指：sb-recent-row 块）——置顶项多 i-pin 状态图钉 -->
          <div
            v-for="t in pinnedThreads"
            :key="t.thread_id"
            class="sb-recent-row"
            :data-testid="`sb-pinned-row-${t.thread_id}`"
            @mouseenter="onRecentEnter(t, $event)"
            @mouseleave="onRecentLeave"
            @mousedown="hideRecentPreview"
          >
            <RouterLink
              class="sb-recent"
              :to="`/thread/${t.thread_id}`"
              @click="sidebarOpen = false"
            >
              <span
                v-if="turnStore.generatingThreadIds.has(t.thread_id)"
                class="sb-gen"
                :data-testid="`sb-generating-${t.thread_id}`"
                :title="$t('threads.generating')"
              >
                <UiIcon name="i-refresh" size="sm" class="spin" />
              </span>
              <span
                v-if="t.is_unread"
                class="dot dot-accent sb-unread"
                role="img"
                :title="$t('threads.unreadLabel')"
                :aria-label="$t('threads.unreadLabel')"
                :data-testid="`sb-unread-${t.thread_id}`"
              ></span>
              <span
                class="sb-recent-text"
                :class="{ marquee: marquee?.id === t.thread_id }"
                :style="marqueeStyle(t.thread_id)"
              ><span class="sb-recent-text-inner"
                ><span class="sb-recent-copy">{{ t.title }}</span><span
                  v-if="marquee?.id === t.thread_id"
                  class="sb-recent-copy"
                  aria-hidden="true"
                >{{ t.title }}</span></span></span>
              <!-- 置顶状态图钉（v1.25）：纯状态指示（accent——与 ThreadsView 置顶图钉同色系，
                  用户主动强调态跨面一致；非 hover 语义）；title/aria-label=threads.pinnedLabel
                  （评审 P3：与 ThreadsView 同语义图钉可访问名称对齐） -->
              <UiIcon
                name="i-pin"
                size="sm"
                class="sb-pin-ind"
                role="img"
                :title="$t('threads.pinnedLabel')"
                :aria-label="$t('threads.pinnedLabel')"
              />
            </RouterLink>
            <ThreadActionMenu :thread="t" variant="active" />
          </div>
        </div>
        </template>
        <!-- 最近区头（v1.24）：整体按钮语义折叠/展开（aria-expanded/controls + chevron 旋转过渡）
            + 筛选钮 hover 显形（hover-reveal 对标 Perplexity）→ 小弹层 全部/快速/深度 -->
        <div ref="recentSectionEl" class="sb-section">
          <button
            type="button"
            class="sb-section-toggle"
            :aria-expanded="recentCollapsed ? 'false' : 'true'"
            aria-controls="sb-recent-list"
            data-testid="sb-recent-toggle"
            @click="toggleRecentCollapsed"
          >
            <span>{{ $t('nav.recent') }}</span>
            <UiIcon name="i-chev-d" size="sm" class="sb-chev" :class="{ flip: recentCollapsed }" data-testid="sb-recent-chev" />
          </button>
          <span class="sb-section-tools">
            <UiTooltip :label="$t('threads.filterTitle')" :sub="$t('threads.filterSub')">
              <button
                type="button"
                class="sb-filter-btn"
                :aria-label="$t('threads.filterTitle')"
                aria-haspopup="true"
                :aria-expanded="recentFilterOpen ? 'true' : 'false'"
                data-testid="sb-recent-filter"
                @click="recentFilterOpen = !recentFilterOpen"
              >
                <UiIcon name="i-filter" size="sm" />
              </button>
            </UiTooltip>
          </span>
          <!-- 筛选弹层（复用既有弹层语言：绝对定位小白卡 + 选项列表；选项=全部/快速/深度） -->
          <div
            v-if="recentFilterOpen"
            class="sb-filter-pop"
            role="menu"
            :aria-label="$t('threads.filterTitle')"
            data-testid="sb-recent-filter-pop"
          >
            <button
              v-for="f in RECENT_FILTERS"
              :key="f.key"
              type="button"
              role="menuitemradio"
              class="sb-filter-item"
              :class="{ active: listStore.recentFilter === f.value }"
              :aria-checked="listStore.recentFilter === f.value ? 'true' : 'false'"
              :data-testid="`sb-filter-${f.key}`"
              @click="pickRecentFilter(f.value)"
            >
              <UiIcon v-if="listStore.recentFilter === f.value" name="i-check" size="sm" />
              {{ $t(`threads.filter_${f.key}`) }}
            </button>
          </div>
        </div>
        <!-- 最近列表（REQ-058/059，v1.21；v1.23 增 hover 预览卡 + 标题 marquee 双形态；v1.24
            区头折叠 v-show；v1.25 语义收窄=只显未置顶项——置顶项已拆入上方独立分区）：
            不自设内滚（随 .sb-nav 整体滚动）+ 未读点 + 行级三点菜单（ThreadActionMenu 与会话页共用；
            动作经 threadListStore 动作面，侧栏随 store 自愈） -->
        <div id="sb-recent-list" v-show="!recentCollapsed" class="sb-recent-list" data-testid="sb-recent-list">
          <div
            v-for="t in unpinnedThreads"
            :key="t.thread_id"
            class="sb-recent-row"
            :data-testid="`sb-recent-row-${t.thread_id}`"
            @mouseenter="onRecentEnter(t, $event)"
            @mouseleave="onRecentLeave"
            @mousedown="hideRecentPreview"
          >
            <RouterLink
              class="sb-recent"
              :to="`/thread/${t.thread_id}`"
              @click="sidebarOpen = false"
            >
              <!-- 生成中指示（turnStore.generatingThreadIds 事件驱动；title 口径与 ThreadsView 一致） -->
              <span
                v-if="turnStore.generatingThreadIds.has(t.thread_id)"
                class="sb-gen"
                :data-testid="`sb-generating-${t.thread_id}`"
                :title="$t('threads.generating')"
              >
                <UiIcon name="i-refresh" size="sm" class="spin" />
              </span>
              <!-- 未读点（is_unread，REQ-060；aria-label 走 threads.unreadLabel） -->
              <span
                v-if="t.is_unread"
                class="dot dot-accent sb-unread"
                role="img"
                :title="$t('threads.unreadLabel')"
                :aria-label="$t('threads.unreadLabel')"
                :data-testid="`sb-unread-${t.thread_id}`"
              ></span>
              <!-- 截断标题：hover 溢出时 marquee（v1.23；完整标题由预览卡承载，原生 title 移除）。
                  v1.24 经典无缝左滚走马灯：仅激活行渲染第二份拷贝（aria-hidden 纯视觉，
                  避免全列表双份 DOM）——to 点=第二份拷贝左缘对齐原起点，循环无跳变 -->
              <span
                class="sb-recent-text"
                :class="{ marquee: marquee?.id === t.thread_id }"
                :style="marqueeStyle(t.thread_id)"
              ><span class="sb-recent-text-inner"
                ><span class="sb-recent-copy">{{ t.title }}</span><span
                  v-if="marquee?.id === t.thread_id"
                  class="sb-recent-copy"
                  aria-hidden="true"
                >{{ t.title }}</span></span></span>
            </RouterLink>
            <ThreadActionMenu :thread="t" variant="active" />
          </div>
          <!-- 「查看全部」→ /threads（REQ-058；threads.viewAll 词条复用，Perplexity「All」同形） -->
          <RouterLink
            class="sb-view-all"
            to="/threads"
            data-testid="sb-view-all"
            @click="sidebarOpen = false"
          >
            {{ $t('threads.viewAll') }}
            <UiIcon name="i-chev-r" size="sm" />
          </RouterLink>
        </div>
      </nav>
      <div class="sb-foot">
        <span class="sb-plan"><UiIcon name="i-zap" size="sm" />{{ $t('common.planLabel') }}</span>
        <!-- 主题三选入口（REQ-054/A38）：用户区整体为触发钮，点击弹「外观」radio 三选轻弹层
            （useThemeEffect 装配于 ThemeSwitcher setup——dataset 写入/system 跟随/卸载清理随组件） -->
        <ThemeSwitcher data-testid="theme-switcher" />
      </div>
    </aside>
    <div v-if="sidebarOpen" class="sb-backdrop" data-testid="sb-backdrop" @click="sidebarOpen = false"></div>

    <!-- ================= 主区 ================= -->
    <div class="main">
      <header class="topbar" data-testid="topbar">
        <div class="tb-left">
          <UiTooltip :label="$t('common.openSidebar')">
            <button type="button" class="icon-btn sb-fab" :aria-label="$t('common.openSidebar')" @click="sidebarOpen = true">
              <UiIcon name="i-menu" />
            </button>
          </UiTooltip>
          <template v-if="isAnswer">
            <UiTooltip :label="$t('common.back')">
              <button type="button" class="icon-btn" :aria-label="$t('common.back')" @click="$router.back()">
                <UiIcon name="i-back" />
              </button>
            </UiTooltip>
            <nav class="tabs">
              <a class="tab active" href="#" @click.prevent>
                <UiIcon name="i-logo" size="sm" />{{ $t('answer.tabAnswer') }}
              </a>
              <!-- 链接/图片页签：M2 范围留位（渲染不激活） -->
              <span class="tab disabled"><UiIcon name="i-link" size="sm" />{{ $t('answer.tabLinks') }}</span>
              <span class="tab disabled"><UiIcon name="i-image" size="sm" />{{ $t('answer.tabImages') }}</span>
            </nav>
          </template>
          <span v-else-if="isHome" class="top-pill">{{ $t('common.planFree') }}</span>
        </div>
        <div class="tb-right">
          <template v-if="isAnswer">
            <!-- 导出 Markdown = 整线程前端本地 .md 下载（utils/exportThread.ts 共享实现，零网络） -->
            <UiTooltip :label="$t('answer.exportMd')">
              <button
                type="button"
                class="btn btn-bordered btn-sm"
                data-testid="topbar-export"
                @click="exportThreadMarkdown"
              >
                <UiIcon name="i-download" size="sm" />{{ $t('answer.exportMd') }}
              </button>
            </UiTooltip>
            <!-- 分享 = 复制会话链接（自用无公开分享后端——已定案；成功/失败 toast 反馈） -->
            <UiTooltip :label="$t('answer.share')">
              <button
                type="button"
                class="btn btn-bordered btn-sm"
                data-testid="topbar-share"
                @click="shareThreadLink"
              >
                <UiIcon name="i-share" size="sm" />{{ $t('answer.share') }}
              </button>
            </UiTooltip>
          </template>
          <UiTooltip :label="$t('common.langTitle')">
            <div class="seg">
              <button
                type="button"
                :class="{ on: prefs.lang === 'zh-CN' }"
                data-testid="lang-zh"
                @click="setLang('zh-CN')"
              >中</button>
              <button
                type="button"
                :class="{ on: prefs.lang === 'en-US' }"
                data-testid="lang-en"
                @click="setLang('en-US')"
              >EN</button>
            </div>
          </UiTooltip>
          <ModelSelect />
        </div>
      </header>

      <main class="page-host">
        <RouterView />
      </main>
    </div>

    <ToastHost />
    <CommandPalette :open="paletteOpen" @close="onPaletteClose" />

    <!-- 侧栏最近项 hover 预览卡（v1.23）：Teleport body 防侧栏 overflow 裁剪；锚定行右侧、
        超下视口上翻（openRecentPreview 实测高度驱动）；纯预览——卡内点击不导航 -->
    <Teleport to="body">
      <div
        v-if="recentPreview"
        ref="previewEl"
        class="sb-preview"
        data-testid="sb-preview-card"
        :style="{ top: `${recentPreview.top}px`, left: `${recentPreview.left}px` }"
        role="presentation"
        @mouseenter="clearPreviewHideTimer"
        @mouseleave="onRecentLeave"
        @mousedown.stop.prevent
        @click.prevent
      >
        <div class="sb-preview-title">{{ recentPreview.thread.title }}</div>
        <div class="sb-preview-meta">
          <span class="badge" :class="previewModeBadgeClass(recentPreview.thread.mode_summary)">
            {{ previewModeBadgeText(recentPreview.thread.mode_summary) }}
          </span>
          <span class="sb-preview-meta-item">{{ $t('threads.turnCount', { n: recentPreview.thread.turn_count }) }}</span>
          <span class="sb-preview-meta-item">{{ previewRelativeTime(recentPreview.thread.updated_at) }}</span>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.app {
  display: flex;
  height: 100vh;
  overflow: hidden;
}
/* 侧栏折叠桌面语义（修复项⑧c）：>1080px 收 56px 图标栏——logo 保留居中、主导航变纯图标
   （文字 font-size:0 隐藏）、最近列表隐藏、底部用户区简化为头像；≤1080px 抽屉行为不受影响
   （全部规则收在 min-width 断点内，与 sidebarOpen 抽屉状态语义分离） */
@media (min-width: 1081px) {
  .sidebar {
    transition: width var(--duration-base) var(--ease-out);
  }
  .sidebar.collapsed {
    width: var(--layout-sidebar-collapsed-w);
  }
  .sidebar.collapsed .sb-head {
    flex-direction: column;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-3) 0;
  }
  .sidebar.collapsed .sb-head-actions {
    flex-direction: column;
  }
  .sidebar.collapsed .nav-item {
    justify-content: center;
    gap: 0;
    padding: 0;
    font-size: 0;
  }
  .sidebar.collapsed .sb-section,
  .sidebar.collapsed .sb-recent-list {
    display: none;
  }
  .sidebar.collapsed .sb-plan {
    display: none;
  }
  .sidebar.collapsed .sb-user {
    justify-content: center;
  }
  /* 用户区现为 ThemeSwitcher 触发钮（REQ-054）：折叠态只留头像，名称/副题与弹层收起 */
  .sidebar.collapsed .theme-trigger-text {
    display: none;
  }
}
/* 侧栏「最近」列表（v1.21 REQ-058/059）：列表不自设限高/内滚（用户裁决 2026-11-16：去掉嵌套
   滚动区，分区列表自然撑开、随 .sb-nav 整体滚动——对标 Perplexity 侧栏单一滚动容器）；
   行级 = 链接（flex:1）+ 三点菜单（hover 显现，ThreadActionMenu 内聚显隐规则）；
   『已置顶』分区与『最近』分区共用本列表样式。纯布局语义（非色彩/间距，无对应 token） */
.sb-recent-list {
  display: grid;
}
/* ---- 最近区头（v1.24）：折叠开关钮 + hover 显形筛选钮 + 筛选弹层 ----
   （.sb-section 基础样式在 tokens.css：flex/space-between/字号色——此处仅补组件级扩展） */
.sb-section {
  position: relative; /* 弹层锚点 */
}
.sb-section-toggle {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  flex: 1;
  min-width: 0;
  padding: 0;
  color: inherit;
  font: inherit;
  text-align: left;
  border-radius: var(--radius-sm);
}
.sb-chev {
  flex: none;
  transition: transform var(--duration-fast) var(--ease-out);
}
/* 折叠态 chevron 指右（对标侧栏折叠语义）；展开态向下 */
.sb-chev.flip {
  transform: rotate(-90deg);
}
/* hover-reveal（对标 Perplexity）：筛选钮 hover 区头/键盘聚焦时显形 */
.sb-section-tools {
  display: inline-flex;
  flex: none;
  opacity: 0;
  transition: opacity var(--duration-fast) var(--ease-out);
}
.sb-section:hover .sb-section-tools,
.sb-section:focus-within .sb-section-tools {
  opacity: 1;
}
.sb-filter-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: var(--radius-sm);
  color: var(--color-text-tertiary);
  transition: background var(--duration-fast) var(--ease-out), color var(--duration-fast) var(--ease-out);
}
.sb-filter-btn:hover {
  background: var(--color-surface-ghost);
  color: var(--color-text-primary);
}
/* 筛选弹层：绝对定位小白卡 + 选项列表（复用既有弹层语言——tam-pop 同族取值） */
.sb-filter-pop {
  position: absolute;
  right: var(--space-2);
  top: calc(100% - var(--space-1));
  z-index: 40;
  min-width: 128px;
  padding: var(--space-1);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-floating);
  display: grid;
}
.sb-filter-item {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-sm);
  font-size: var(--text-sm);
  color: var(--color-text-primary);
  text-align: left;
  white-space: nowrap;
  transition: background var(--duration-fast) var(--ease-out);
}
.sb-filter-item:hover,
.sb-filter-item:focus-visible {
  background: var(--color-surface-ghost);
}
.sb-filter-item.active {
  color: var(--color-accent);
  font-weight: var(--weight-medium);
}
.sb-recent-row {
  position: relative;
  display: flex;
  align-items: center;
  min-width: 0;
}
.sb-recent {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
/* 激活态高亮：当前浏览中的会话行——RouterLink 自动携带 router-link-active（Vue Router
   前缀匹配，/thread/{id} 恰好等于当前会话页；首页/会话页等其余路由零误亮），置顶/最近
   两分区共用本选择器（同 .sb-recent 类，无附加选择器）。视觉语言对齐 tokens.css
   .sb-recent.active 先例（面底+主文字色）但底色改取 --color-surface-active：浅色主题
   --color-surface 与 --color-bg-sidebar 同值（#f7f7f8）激活不可见，surface-active 两主题
   均可辨（暗 #2d3031 / 亮 #e3e3e6）且强于 hover ghost——激活=当前所处会话、hover=悬停
   预览，强度分层；文字色 secondary→primary 提升可读性，未读点/图钉/生成中指示等子元素
   各自带色不受影响；深浅色全部由 tokens 变量驱动。特异性（两个类 + data-v 属性）压过
   tokens.css .sb-recent:hover（0,2,0）——激活行 hover 不闪变。 */
.sb-recent.router-link-active {
  background: var(--color-surface-active);
  color: var(--color-text-primary);
}
.sb-unread {
  flex: none;
  width: 8px;
  height: 8px;
}
/* 置顶状态图钉（v1.25）：accent 色——置顶=用户主动强调态，与 ThreadsView 置顶图钉
   .pin-indicator 同色系跨面一致（纯状态指示，无 hover 语义） */
.sb-pin-ind {
  flex: none;
  color: var(--color-accent);
}
.sb-recent-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
/* marquee（v1.23；v1.24 终版=经典无缝左滚走马灯）：仅溢出行激活——hover 起 / 离停
   （离停复位跳回起点=走马灯惯例，可接受）；距离/时长由内联变量注入（时长=distance/
   30px/s 匀速）；reduced-motion 直接禁用动画（完整态维持静态截断，完整标题由预览卡承载） */
.sb-recent-text-inner {
  display: inline-block;
  white-space: nowrap;
}
.sb-recent-text.marquee {
  text-overflow: clip;
}
.sb-recent-text.marquee .sb-recent-text-inner {
  display: inline-flex; /* 双拷贝行排（走马灯；非激活行保持单份 inline-block 截断） */
  gap: 42px; /* 拷贝接缝空档（≈3em@14px）——与 TS MARQUEE_COPY_GAP_PX 同值，无缝距离=单份宽+此值 */
  animation: sb-marquee var(--mq-duration, 10s) linear infinite;
}
@keyframes sb-marquee {
  /* 经典无缝左滚（v1.24 终版）：单方向匀速 infinite——to 点=第二份拷贝左缘对齐原起点
     （distance=单份拷贝宽+接缝 gap），循环回 from 时第一/二份视觉互换、无跳变 */
  from {
    transform: translateX(0);
  }
  to {
    transform: translateX(calc(-1 * var(--mq-distance, 0px)));
  }
}
@media (prefers-reduced-motion: reduce) {
  .sb-recent-text.marquee .sb-recent-text-inner {
    animation: none;
  }
}
/* hover 预览卡（v1.23）：白底描边阴影卡片，锚定行右侧盖住主区；完整标题不截断 */
.sb-preview {
  position: fixed;
  z-index: 65;
  width: 280px;
  max-width: calc(100vw - 16px);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-floating);
  padding: var(--space-3);
}
.sb-preview-title {
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
  color: var(--color-text-primary);
  line-height: 1.5;
  white-space: normal;
  word-break: break-word;
}
.sb-preview-meta {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
  margin-top: var(--space-2);
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}
/* 「查看全部」→ /threads（threads.viewAll；次要层级样式） */
.sb-view-all {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-md);
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
  transition: background var(--duration-fast) var(--ease-out), color var(--duration-fast) var(--ease-out);
}
.sb-view-all:hover {
  background: var(--color-surface-ghost);
  color: var(--color-text-primary);
}
.sb-gen {
  display: inline-flex;
  flex: none;
  color: var(--color-accent);
}
.spin {
  animation: sb-spin-rotate 1.2s linear infinite;
}
@keyframes sb-spin-rotate {
  to {
    transform: rotate(360deg);
  }
}
.page-host {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}
.top-pill {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  height: 32px;
  padding: 0 var(--space-3);
  border-radius: var(--radius-full);
  border: 1px solid var(--color-accent-border);
  color: var(--color-accent);
  font-size: var(--text-sm);
}
.tab.disabled {
  color: var(--color-text-faint);
  cursor: default;
}
</style>
