<script setup lang="ts">
/**
 * ThreadsView（§1 ThreadsView 行：GET /threads?limit=50&mode= 列表 + 筛选 chips + 日期分组）。
 * 设计稿 04-threads：标题/副标题 + 内嵌搜索框（提问直达）+ 筛选 chips + 线程列表。
 * - 日期分组（今天/最近 7 天/更早）纯前端（§1）；「已归档」chip 一期隐藏（无归档数据）；
 * - 无痕轮不设列表徽标（MDQ-8）；ThreadRow 生成中旋转指示 = turnStore.generatingThreadIds
 *   （§3 生命周期 12，纯前端状态渲染，i18n threads.generating）；
 * - 2.2 items 无轮次状态/附件字段：设计稿 t-sub/附件胶囊无数据源——不渲染（M1 口径）。
 */
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'

import SearchCard from '@/components/search/SearchCard.vue'
import UiIcon from '@/components/icons/UiIcon.vue'
import { normalizeError } from '@/api/client'
import { toast } from '@/composables/toast'
import { useThreadListStore } from '@/stores/threadList'
import { useTurnStore } from '@/stores/turn'
import type { AskMode, ThreadListItem, ThreadModeSummary } from '@/types/api'

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

async function setFilter(value: ThreadModeSummary | null): Promise<void> {
  listStore.setModeFilter(value)
  try {
    await listStore.fetchThreads({ mode: value, limit: 50 })
  } catch (e) {
    toast(normalizeError(e).message)
  }
}

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

/* ---------- 日期分组（今天 / 最近 7 天 / 更早，纯前端） ---------- */
interface DateGroup {
  key: 'today' | 'week' | 'earlier'
  items: ThreadListItem[]
}

const groups = computed<DateGroup[]>(() => {
  const now = Date.now()
  const buckets: Record<DateGroup['key'], ThreadListItem[]> = { today: [], week: [], earlier: [] }
  for (const item of listStore.items) {
    const updated = new Date(item.updated_at).getTime()
    if (!Number.isFinite(updated)) {
      buckets.earlier.push(item)
      continue
    }
    const startOfToday = new Date()
    startOfToday.setHours(0, 0, 0, 0)
    if (updated >= startOfToday.getTime()) buckets.today.push(item)
    else if (now - updated <= 7 * 24 * 3600 * 1000) buckets.week.push(item)
    else buckets.earlier.push(item)
  }
  const out: DateGroup[] = []
  if (buckets.today.length > 0) out.push({ key: 'today', items: buckets.today })
  if (buckets.week.length > 0) out.push({ key: 'week', items: buckets.week })
  if (buckets.earlier.length > 0) out.push({ key: 'earlier', items: buckets.earlier })
  return out
})

function relativeTime(iso: string): string {
  const then = new Date(iso).getTime()
  if (!Number.isFinite(then)) return ''
  const tag = locale.value === 'en-US' ? 'en' : 'zh-CN'
  const rtf = new Intl.RelativeTimeFormat(tag, { numeric: 'auto' })
  const minutes = Math.round((then - Date.now()) / 60_000)
  if (Math.abs(minutes) < 60) return rtf.format(minutes, 'minute')
  const hours = Math.round(minutes / 60)
  if (Math.abs(hours) < 24) return rtf.format(hours, 'hour')
  const days = Math.round(hours / 24)
  return rtf.format(days, 'day')
}

function modeBadgeClass(modeSummary: ThreadModeSummary): string {
  return modeSummary === 'deep' ? 'badge-outline' : 'badge-accent'
}

function modeBadgeText(modeSummary: ThreadModeSummary): string {
  switch (modeSummary) {
    case 'fast':
      return t('mode.fast')
    case 'deep':
      return t('mode.deep')
    case 'mixed':
      return t('threads.modeMixed')
  }
}

onMounted(() => {
  listStore
    .fetchThreads({ mode: listStore.modeFilter, limit: 50 })
    .catch((e) => toast(normalizeError(e).message))
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

      <!-- 筛选 chips（一期列表只读回看；「已归档」chip 一期隐藏——§1） -->
      <div class="filter-row">
        <button
          v-for="f in FILTERS"
          :key="f.key"
          type="button"
          class="chip"
          :class="{ active: listStore.modeFilter === f.value }"
          :data-testid="`threads-filter-${f.key}`"
          @click="setFilter(f.value)"
        >
          <UiIcon v-if="f.key === 'fast'" name="i-zap" size="sm" />
          <UiIcon v-else-if="f.key === 'deep'" name="i-wave" size="sm" />
          {{ $t(`threads.filter_${f.key}`) }}
        </button>
      </div>

      <!-- 线程列表：按更新时间倒序（REQ-008：标题/模式徽标/轮次/更新时间） -->
      <div class="thread-list" data-testid="threads-list">
        <template v-for="g in groups" :key="g.key">
          <div class="date-sep">{{ $t(`threads.group_${g.key}`) }}</div>
          <button
            v-for="item in g.items"
            :key="item.thread_id"
            type="button"
            class="thread-row"
            :data-testid="`thread-row-${item.thread_id}`"
            @click="router.push(`/thread/${item.thread_id}`)"
          >
            <span class="t-ico">
              <UiIcon v-if="generating.has(item.thread_id)" name="i-refresh" class="spin" />
              <UiIcon v-else name="i-search" />
            </span>
            <span class="t-main">
              <span class="t-title">
                <span class="t-text">{{ item.title }}</span>
                <span class="badge mode-tag" :class="modeBadgeClass(item.mode_summary)">
                  <UiIcon v-if="item.mode_summary === 'fast'" name="i-zap" size="sm" />
                  <UiIcon v-else-if="item.mode_summary === 'deep'" name="i-wave" size="sm" />
                  {{ modeBadgeText(item.mode_summary) }}
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
            <span class="t-time">{{ relativeTime(item.updated_at) }}</span>
          </button>
        </template>
        <p v-if="groups.length === 0" class="empty-hint" role="status">{{ $t('threads.empty') }}</p>
      </div>
    </div>
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
  gap: var(--space-2);
  margin-bottom: var(--space-4);
}
.thread-list {
  display: grid;
}
.thread-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3);
  border-radius: var(--radius-md);
  text-align: left;
  transition: background var(--duration-fast) var(--ease-out);
}
.thread-row:hover {
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
