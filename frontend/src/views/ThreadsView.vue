<script setup lang="ts">
/**
 * ThreadsView（§1 ThreadsView 行 v1.21 / REQ-058~060；设计稿 04-threads + 设计说明 v1.6 增注）。
 *
 * - 会话页全量加载：首屏 GET /threads?limit=50 + **触底自动加载下一页**（IntersectionObserver
 *   sentinel，D-15；失败行内「加载失败·重试」；hasMore=false 自然止于列表尾，不渲染加载提示）；
 * - 三态视图 radio（ViewFilterRadio 组件：全部/未读/已归档 → archived/unread 参数）与
 *   mode chips（fast/deep）共存、AND 叠加；
 * - 筛选行常显全选框（v1.24，对标 Perplexity）：常规态即可全选当前视图（位于视图 radio
 *   左侧；勾选=items 全量入 selection，选中>0 批量栏照旧替换筛选行）；
 * - 标题搜索（D-19）：独立于内嵌提问卡的搜索框 → q 参数，防抖 250ms（与 HomeSuggestions
 *   同款常量），清空回全量；超 200 字符由 store 收口截断（400001 兜底防线）；
 * - 逐日分组（D-18）：今天/昨天/本年 M月D日/往年 YYYY年M月D日（本地时区；today/yesterday
 *   走 i18n 词条、其余走 Intl.DateTimeFormat 本地化模板）——取代旧「今天/最近 7 天/更早」三桶；
 * - 行级三点菜单（ThreadActionMenu，REQ-059）：活跃视图=置顶·取消置顶/标记为未读/重命名/
 *   归档/删除；归档视图=取消归档/重命名/删除（置顶与标记为未读隐藏）；
 * - 未读点（is_unread）+ 置顶图钉（pinned，仅活跃视图渲染）；
 * - 生成中指示 = turnStore.generatingThreadIds（§3 生命周期 12）；
 * - 内嵌 SearchCard（提问直达）与旧测试契约保留。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'

import SearchCard from '@/components/search/SearchCard.vue'
import ThreadActionMenu from '@/components/threads/ThreadActionMenu.vue'
import ThreadModal from '@/components/threads/ThreadModal.vue'
import UiIcon from '@/components/icons/UiIcon.vue'
import UiTooltip from '@/components/common/UiTooltip.vue'
import ViewFilterRadio from '@/components/threads/ViewFilterRadio.vue'
import { normalizeError } from '@/api/client'
import { toast } from '@/composables/toast'
import { useInfiniteScroll } from '@/composables/infiniteScroll'
import { useThreadListStore } from '@/stores/threadList'
import { useTurnStore } from '@/stores/turn'
import { groupThreadsByDay } from '@/utils/dateGroups'
import { modeBadgeClass, modeBadgeText, relativeTime } from '@/utils/threadDisplay'
import type { AskMode, ThreadModeSummary } from '@/types/api'

const { t, locale } = useI18n()
const router = useRouter()
const listStore = useThreadListStore()
const turnStore = useTurnStore()

const query = ref('')
const mode = ref<AskMode>('fast')
const submitting = ref(false)

const FILTERS: { key: string; value: ThreadModeSummary | null }[] = [
  { key: 'all', value: null },
  { key: 'fast', value: 'fast' },
  { key: 'deep', value: 'deep' },
]

const generating = computed(() => turnStore.generatingThreadIds)

/** 触底自动加载：sentinel → store.loadMore（store 内聚 loadingMore/首屏在途守卫——P2-1） */
const sentinelEl = ref<HTMLElement | null>(null)
useInfiniteScroll(sentinelEl, () => void listStore.loadMore(), {
  enabled: () =>
    listStore.hasMore && !listStore.loadingMore && !listStore.fetchingFirstPage && !listStore.loadMoreFailed,
})

async function setModeFilter(value: ThreadModeSummary | null): Promise<void> {
  try {
    await listStore.setModeFilter(value)
  } catch (e) {
    toast(normalizeError(e).message)
  }
}

/* ---------- 标题搜索（D-19）：防抖 250ms → store.setSearchQuery 收口请求 ----------
 * 本地框初值取 store.searchQuery（P2-3，2026-11-15）：searchQuery 跨路由持久于 store，
 * 返回会话页时列表按其过滤——框同步回显，避免「列表被过滤、框显示空」的失步。 */
const SEARCH_DEBOUNCE_MS = 250
const searchInput = ref(listStore.searchQuery)
let searchTimer: ReturnType<typeof setTimeout> | null = null

function onSearchInput(value: string): void {
  searchInput.value = value
  if (searchTimer !== null) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    searchTimer = null
    void listStore.setSearchQuery(value).catch((e: unknown) => toast(normalizeError(e).message))
  }, SEARCH_DEBOUNCE_MS)
}

onBeforeUnmount(() => {
  if (searchTimer !== null) clearTimeout(searchTimer)
})

async function submit(q: string = query.value, m?: AskMode): Promise<void> {
  const text = q.trim()
  if (!text || submitting.value) return
  submitting.value = true
  try {
    const data = await turnStore.ask(text, m ?? mode.value)
    query.value = ''
    await router.push(`/thread/${data.thread_id}`)
  } catch (e) {
    toast(normalizeError(e).message)
  } finally {
    submitting.value = false
  }
}

/* ---------- 逐日分组（今天/昨天/M月D日/往年带年份；本地时区；D-18） ---------- */
const groups = computed(() =>
  groupThreadsByDay(listStore.items, new Date(), {
    todayLabel: t('threads.group_today'),
    yesterdayLabel: t('threads.group_yesterday'),
    locale: locale.value,
  }),
)

/** 活跃视图（非归档）才渲染置顶图钉（归档视图无置顶语义，api.md §2.2 v1.18 排序注） */
const showPin = computed(() => listStore.viewFilter !== 'archived')

/* ---------- 批量操作栏（v1.23；对标 Perplexity Library 批量形态；§4 批量三动作） ----------
 * 行级复选框 hover 显形（CSS）+ 选中行浅灰高亮；批量栏替换筛选行（选中>0）；
 * Esc/清除退出；批量删除前置居中确认 modal（复用 ThreadModal 通用小件）；
 * 动作全部经 store batch 三动作（循环既有单线程动作、部分失败汇总 toast 在 store 内聚）。 */
const selectedIds = computed(() => listStore.selectedIds)
const selectedCount = computed(() => selectedIds.value.size)
const batchActive = computed(() => selectedCount.value > 0)
const allSelected = computed(
  () => listStore.items.length > 0 && listStore.items.every((item) => selectedIds.value.has(item.thread_id)),
)
const someSelected = computed(() => batchActive.value && !allSelected.value)

function onSelectAll(checked: boolean): void {
  if (checked) listStore.setSelection(listStore.items.map((item) => item.thread_id))
  else listStore.clearSelection()
}

const batchConfirmDelete = ref(false)
const batchCancelEl = ref<HTMLButtonElement | null>(null)
/** 打开确认 modal 前的焦点（评审 P3-2：取消/Esc 关闭后返还——与 ThreadActionMenu closeAll(true) 对称；
 *  确认路径不返还——删除成功后批量栏收起、触发钮随 DOM 移除） */
const batchDeleteTriggerEl = ref<HTMLElement | null>(null)

function onBatchDelete(): void {
  batchDeleteTriggerEl.value = document.activeElement as HTMLElement | null
  batchConfirmDelete.value = true
  void nextTick(() => batchCancelEl.value?.focus())
}

function closeBatchConfirm(): void {
  batchConfirmDelete.value = false
  batchDeleteTriggerEl.value?.focus()
}

async function onConfirmBatchDelete(): Promise<void> {
  batchConfirmDelete.value = false
  await listStore.batchDelete([...selectedIds.value])
}

async function onBatchArchive(): Promise<void> {
  await listStore.batchToggleArchive([...selectedIds.value])
}

async function onBatchUnread(): Promise<void> {
  await listStore.batchMarkUnread([...selectedIds.value])
}

/** Esc 退出批量选择（确认 modal 打开时其自身 Esc 优先——ThreadModal capture 级 stopPropagation） */
function onWinKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape' && batchActive.value && !batchConfirmDelete.value) listStore.clearSelection()
}

/* 相对时间与模式徽标（v1.23 抽共享：utils/threadDisplay.ts——侧栏预览卡同源；
 * 模板调用点带 locale/t 参数） */

onMounted(() => {
  // 首屏（viewFilter/searchQuery 为 store 持久态——返回本页保留筛选）
  listStore.fetchThreads().catch((e) => toast(normalizeError(e).message))
  window.addEventListener('keydown', onWinKeydown)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onWinKeydown)
  // 批量选择是会话页视图局部态：离开页面即清（重进时 fetchThreads 首拉亦兜底清空）
  listStore.clearSelection()
})
</script>

<template>
  <section class="page" data-testid="threads-view">
    <div class="page-inner">
      <h1 class="page-title">{{ $t('threads.title') }}</h1>
      <p class="page-sub">{{ $t('threads.subtitle') }}</p>

      <!-- 内嵌搜索框（与首页同款卡片语言，提问直达；§1：历史页内嵌搜索框不挂推荐面板） -->
      <div class="embed-search">
        <SearchCard
          v-model="query"
          v-model:mode="mode"
          compact
          :placeholder="$t('threads.newQuestion')"
          data-testid="threads-search-card"
          @submit="submit()"
        />
      </div>

      <!-- 批量操作栏（v1.23）：选中>0 时替换筛选行——表头全选（indeterminate）+ 已选 N + 三动作 + 清除 -->
      <div v-if="batchActive" class="filter-row batch-bar" data-testid="batch-bar">
        <label class="batch-all">
          <input
            type="checkbox"
            class="row-check"
            :checked="allSelected"
            :indeterminate="someSelected"
            :aria-label="$t('threads.selectAll')"
            data-testid="batch-select-all"
            @change="onSelectAll(($event.target as HTMLInputElement).checked)"
          />
          <span class="batch-count" data-testid="batch-count">{{ $t('threads.selectedCount', { n: selectedCount }) }}</span>
        </label>
        <div class="batch-actions">
          <UiTooltip :label="$t('threads.delete')">
            <button
              type="button"
              class="icon-btn batch-btn danger"
              :aria-label="$t('threads.delete')"
              data-testid="batch-delete"
              @click="onBatchDelete"
            >
              <UiIcon name="i-trash" size="sm" />
            </button>
          </UiTooltip>
          <!-- 归档语义随视图反转（与行级菜单同款：归档视图=取消归档） -->
          <UiTooltip :label="showPin ? $t('threads.archive') : $t('threads.unarchive')">
            <button
              type="button"
              class="icon-btn batch-btn"
              :aria-label="showPin ? $t('threads.archive') : $t('threads.unarchive')"
              data-testid="batch-archive"
              @click="onBatchArchive"
            >
              <UiIcon name="i-archive" size="sm" />
            </button>
          </UiTooltip>
          <!-- 归档视图无未读语义（行级菜单同款隐藏） -->
          <UiTooltip v-if="showPin" :label="$t('threads.markUnread')">
            <button
              type="button"
              class="icon-btn batch-btn"
              :aria-label="$t('threads.markUnread')"
              data-testid="batch-unread"
              @click="onBatchUnread"
            >
              <UiIcon name="i-alert" size="sm" />
            </button>
          </UiTooltip>
          <UiTooltip :label="$t('threads.clearSelection')">
            <button
              type="button"
              class="icon-btn batch-btn"
              :aria-label="$t('threads.clearSelection')"
              data-testid="batch-clear"
              @click="listStore.clearSelection()"
            >
              <UiIcon name="i-close" size="sm" />
            </button>
          </UiTooltip>
        </div>
      </div>
      <!-- 筛选行：常显全选框（v1.24 对标 Perplexity——常规态即可全选当前视图，位于视图
           radio 左侧；勾选=当前 items 全量入 selection〔复用 setSelection〕，选中>0 时
           批量栏照旧替换筛选行）+ 三态视图 radio（REQ-058；选中态/失败 toast 收口在组件内——P2-4）+ 标题搜索（防抖 250ms，D-19） -->
      <div v-else class="filter-row">
        <label class="filter-all">
          <input
            type="checkbox"
            class="row-check"
            :checked="allSelected"
            :indeterminate="someSelected"
            :aria-label="$t('threads.selectAll')"
            data-testid="filter-select-all"
            @change="onSelectAll(($event.target as HTMLInputElement).checked)"
          />
        </label>
        <ViewFilterRadio
          :model-value="listStore.viewFilter"
          data-testid="view-filter-radio"
        />
        <div class="search-box">
          <UiIcon name="i-search" size="sm" class="search-ico" />
          <input
            class="search-input"
            type="search"
            :value="searchInput"
            :placeholder="$t('threads.searchPlaceholder')"
            maxlength="200"
            :aria-label="$t('threads.searchPlaceholder')"
            data-testid="threads-search-input"
            @input="onSearchInput(($event.target as HTMLInputElement).value)"
          />
        </div>
      </div>

      <!-- mode chips（全部/快速/深度；与三态视图/搜索 AND 叠加） -->
      <div class="chips-row">
        <button
          v-for="f in FILTERS"
          :key="f.key"
          type="button"
          class="chip"
          :class="{ active: listStore.modeFilter === f.value }"
          :data-testid="`threads-filter-${f.key}`"
          @click="setModeFilter(f.value)"
        >
          <UiIcon v-if="f.key === 'fast'" name="i-zap" size="sm" />
          <UiIcon v-else-if="f.key === 'deep'" name="i-wave" size="sm" />
          {{ $t(`threads.filter_${f.key}`) }}
        </button>
      </div>

      <!-- 线程列表：逐日分组 + 未读点/置顶图钉 + 行级三点菜单（REQ-058~060） -->
      <div class="thread-list" data-testid="threads-list">
        <template v-for="g in groups" :key="g.key">
          <div class="date-sep" :data-testid="`date-group-${g.key}`">{{ g.label }}</div>
          <div
            v-for="item in g.items"
            :key="item.thread_id"
            class="thread-row"
            :class="{ selected: selectedIds.has(item.thread_id) }"
            :data-testid="`thread-row-${item.thread_id}`"
          >
            <!-- 行级复选框（v1.23）：hover 显形（CSS）、选中常显；独立于行主钮保证键盘可达 -->
            <input
              type="checkbox"
              class="row-check"
              :checked="selectedIds.has(item.thread_id)"
              :aria-label="$t('threads.selectRow')"
              :data-testid="`thread-check-${item.thread_id}`"
              @change="listStore.toggleSelect(item.thread_id)"
            />
            <button
              type="button"
              class="thread-row-main"
              @click="router.push(`/thread/${item.thread_id}`)"
            >
              <span class="t-ico">
                <UiIcon v-if="generating.has(item.thread_id)" name="i-refresh" class="spin" />
                <UiIcon v-else name="i-search" />
              </span>
              <span class="t-main">
                <span class="t-title">
                  <span
                    v-if="item.is_unread"
                    class="dot dot-accent unread-dot"
                    role="img"
                    :title="$t('threads.unreadLabel')"
                    :aria-label="$t('threads.unreadLabel')"
                    :data-testid="`unread-dot-${item.thread_id}`"
                  ></span>
                  <span class="t-text">{{ item.title }}</span>
                  <span
                    v-if="showPin && item.pinned"
                    class="pin-indicator"
                    role="img"
                    :title="$t('threads.pinnedLabel')"
                    :aria-label="$t('threads.pinnedLabel')"
                    :data-testid="`pin-${item.thread_id}`"
                  >
                    <UiIcon name="i-pin" size="sm" />
                  </span>
                  <span class="badge mode-tag" :class="modeBadgeClass(item.mode_summary)">
                    <UiIcon v-if="item.mode_summary === 'fast'" name="i-zap" size="sm" />
                    <UiIcon v-else-if="item.mode_summary === 'deep'" name="i-wave" size="sm" />
                    {{ modeBadgeText(item.mode_summary, t) }}
                  </span>
                  <span v-if="item.turn_count > 1" class="badge badge-outline mode-tag">
                    {{ $t('threads.turnCount', { n: item.turn_count }) }}
                  </span>
                  <span
                    v-if="generating.has(item.thread_id)"
                    class="badge badge-accent mode-tag"
                    :title="$t('threads.generating')"
                  >
                    {{ $t('threads.generating') }}
                  </span>
                </span>
              </span>
              <span class="t-time">{{ relativeTime(item.updated_at, locale) }}</span>
            </button>
            <ThreadActionMenu :thread="item" :variant="showPin ? 'active' : 'archived'" />
          </div>
        </template>
        <p v-if="groups.length === 0" class="empty-hint" role="status">{{ $t('threads.empty') }}</p>

        <!-- 触底加载尾态（D-15）：加载中 spinner / 失败行内重试；hasMore=false 不渲染提示 -->
        <div ref="sentinelEl" class="list-sentinel" aria-hidden="true"></div>
        <div v-if="listStore.loadingMore" class="list-foot" role="status" data-testid="threads-loading-more">
          <UiIcon name="i-refresh" size="sm" class="spin" />
        </div>
        <div v-else-if="listStore.loadMoreFailed" class="list-foot" data-testid="threads-load-failed">
          <span class="foot-text">{{ $t('threads.loadFailed') }}</span>
          <button type="button" class="retry-btn" data-testid="threads-retry" @click="listStore.loadMore()">
            <UiIcon name="i-refresh" size="sm" />{{ $t('threads.retry') }}
          </button>
        </div>
      </div>
    </div>

    <!-- 批量删除二次确认（v1.23：复用 ThreadModal 居中 modal；不可撤销警示；取消不发请求；
         取消/Esc 关闭返还焦点到批量删除钮——P3-2 与菜单路径对称） -->
    <ThreadModal
      :open="batchConfirmDelete"
      :title="$t('threads.delete')"
      role="alertdialog"
      @close="closeBatchConfirm"
    >
      <div class="batch-confirm" data-testid="batch-confirm">
        <p class="batch-confirm-text">{{ $t('threads.batchDeleteConfirm', { n: selectedCount }) }}</p>
        <p class="batch-confirm-warn">{{ $t('threads.batchIrreversible') }}</p>
        <div class="batch-confirm-actions">
          <button type="button" ref="batchCancelEl" class="batch-btn-text" data-testid="batch-cancel" @click="closeBatchConfirm">
            {{ $t('common.cancel') }}
          </button>
          <button type="button" class="batch-btn-text danger" data-testid="batch-confirm-delete" @click="onConfirmBatchDelete">
            {{ $t('threads.delete') }}
          </button>
        </div>
      </div>
    </ThreadModal>
  </section>
</template>

<style scoped>
.page {
  flex: 1;
  overflow-y: auto;
  min-height: 0;
}
.page-inner {
  max-width: 920px;
  margin: 0 auto;
  padding: var(--space-8) var(--space-8) var(--space-16);
}
.page-title {
  font-size: var(--text-2xl);
  line-height: var(--text-2xl--lh);
  font-weight: var(--weight-semibold);
}
.page-sub {
  margin-top: var(--space-1);
  color: var(--color-text-tertiary);
  font-size: var(--text-base);
  margin-bottom: var(--space-6);
}
.embed-search {
  margin-bottom: var(--space-6);
}
.filter-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  flex-wrap: wrap;
  margin-bottom: var(--space-3);
}
/* 标题搜索框（q 参数；独立于内嵌提问卡） */
.search-box {
  position: relative;
  display: flex;
  align-items: center;
  flex: 1 1 220px;
  max-width: 320px;
  min-width: 180px;
}
.search-ico {
  position: absolute;
  left: var(--space-3);
  color: var(--color-text-tertiary);
  pointer-events: none;
}
.search-input {
  width: 100%;
  padding: var(--space-2) var(--space-3) var(--space-2) calc(var(--space-3) * 2 + 16px);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-full);
  background: var(--color-surface);
  color: var(--color-text-primary);
  font-size: var(--text-sm);
  transition: border-color var(--duration-fast) var(--ease-out);
}
.search-input::placeholder {
  color: var(--color-text-faint);
}
.search-input:focus {
  outline: none;
  border-color: var(--color-accent-border);
}
.chips-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-4);
}
.thread-list {
  display: grid;
}
.thread-row {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  border-radius: var(--radius-md);
}
/* 行级复选框（v1.23）：hover 显形、选中/聚焦常显（对标 Perplexity 行勾选；键盘可达） */
.row-check {
  flex: none;
  width: 16px;
  height: 16px;
  margin: 0 var(--space-1) 0 var(--space-2);
  accent-color: var(--color-accent);
  cursor: pointer;
  opacity: 0;
  transition: opacity var(--duration-fast) var(--ease-out);
}
.thread-row:hover .row-check,
.row-check:checked,
.row-check:focus-visible {
  opacity: 1;
}
/* 选中行浅灰高亮（批量态） */
.thread-row.selected {
  background: var(--color-surface-ghost);
}
/* 批量操作栏（v1.23）：替换筛选行——全选 + 已选 N + 动作钮组 */
.batch-bar {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--space-2) var(--space-3);
}
.batch-all {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
}
.batch-all .row-check {
  margin: 0;
  opacity: 1;
}
/* 筛选行常显全选框（v1.24）：位于视图 radio 左侧，未选中空框也常显（行级 .row-check
   的 hover 显形规则不适用——覆盖为恒显） */
.filter-all {
  display: inline-flex;
  align-items: center;
  flex: none;
}
.filter-all .row-check {
  margin: 0;
  opacity: 1;
}
.batch-count {
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  color: var(--color-text-primary);
  white-space: nowrap;
}
.batch-actions {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
}
.batch-btn {
  width: 32px;
  height: 32px;
  color: var(--color-text-secondary);
}
.batch-btn:hover {
  color: var(--color-text-primary);
}
.batch-btn.danger:hover {
  color: var(--color-danger);
}
/* 批量删除确认（ThreadModal 槽内容） */
.batch-confirm-text {
  font-size: var(--text-base);
  color: var(--color-text-primary);
  font-weight: var(--weight-medium);
}
.batch-confirm-warn {
  margin-top: var(--space-2);
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}
.batch-confirm-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
  margin-top: var(--space-4);
}
.batch-btn-text {
  padding: var(--space-2) var(--space-4);
  border-radius: var(--radius-sm);
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  border: 1px solid var(--color-border);
  transition: background var(--duration-fast) var(--ease-out), color var(--duration-fast) var(--ease-out);
}
.batch-btn-text:hover {
  background: var(--color-surface-ghost);
  color: var(--color-text-primary);
}
.batch-btn-text.danger {
  background: var(--color-danger);
  border-color: transparent;
  color: var(--color-text-inverse);
}
.batch-btn-text.danger:hover {
  background: var(--color-danger);
  color: var(--color-text-inverse);
  filter: brightness(1.08);
}
.thread-row-main {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3);
  border-radius: var(--radius-md);
  text-align: left;
  flex: 1;
  min-width: 0;
  transition: background var(--duration-fast) var(--ease-out);
}
.thread-row:hover .thread-row-main {
  background: var(--color-surface-ghost);
}
.thread-row .t-ico {
  color: var(--color-text-tertiary);
  flex: none;
  display: inline-flex;
}
.t-main {
  min-width: 0;
  flex: 1;
}
.t-title {
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
  color: var(--color-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.t-title .t-text {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
/* 未读点（is_unread；行级圆点——v1.21 REQ-060） */
.unread-dot {
  flex: none;
  width: 8px;
  height: 8px;
}
/* 置顶图钉（pinned；仅活跃视图渲染） */
.pin-indicator {
  display: inline-flex;
  flex: none;
  color: var(--color-accent);
}
.t-time {
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
  white-space: nowrap;
  min-width: 64px;
  text-align: right;
  flex: none;
}
.mode-tag {
  flex: none;
}
.date-sep {
  padding: var(--space-5) var(--space-3) var(--space-2);
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
}
/* 触底加载尾态 */
.list-sentinel {
  height: 1px;
}
.list-foot {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  padding: var(--space-5);
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
}
.foot-text {
  color: var(--color-text-tertiary);
}
.retry-btn {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  padding: var(--space-1) var(--space-3);
  border-radius: var(--radius-full);
  border: 1px solid var(--color-border);
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  transition: background var(--duration-fast) var(--ease-out), color var(--duration-fast) var(--ease-out);
}
.retry-btn:hover {
  background: var(--color-surface-ghost);
  color: var(--color-text-primary);
}
.spin {
  animation: spin-rotate 1.2s linear infinite;
}
@keyframes spin-rotate {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}
.empty-hint {
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
  padding: var(--space-6);
  text-align: center;
}
@media (max-width: 720px) {
  .page-inner {
    padding: var(--space-6) var(--space-4) var(--space-12);
  }
}
</style>
