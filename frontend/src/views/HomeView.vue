<script setup lang="ts">
/**
 * HomeView（§1 映射：首屏 GET /models 模型 chip（ModelSelect 自取）；提问 → POST /threads →
 * 跳 /thread/{thread_id}（turnStore.ask 已完成建轮+订阅两段式））。
 * 设计稿 01-home：侧栏（App 壳承担）+ 居中 logo/大标题 + 大搜索卡 + 推荐问题卡 + hero-foot。
 * 输入推荐面板（REQ-047 / 修复项⑦，对标图 3）：SearchCard suggestions 插槽挂 HomeSuggestions——
 *   - query<2 → 热门池前 6（threadListStore.fetchSuggestPool，首次聚焦幂等拉取）；
 *   - query≥2 → 250ms 防抖后切历史匹配（threadListStore.items 标题包含、大小写不敏感、
 *     按 title 精确去重后前 5——同题多线程只保留最新）；
 *     无匹配显示空态；清空回热门池；
 *   - 开合 = textarea focus/blur/escape（blur 延迟 150ms 给条目 click 让路；Esc 立即收起）；
 *   - 选中条目 → 复用 submit() 提交。
 * 推荐问题卡文案走 i18n home.suggestItems（四页文案全部 $t() 口径）。
 */
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import HomeSuggestions from '@/components/search/HomeSuggestions.vue'
import SearchCard from '@/components/search/SearchCard.vue'
import UiIcon from '@/components/icons/UiIcon.vue'
import { normalizeError } from '@/api/client'
import { toast } from '@/composables/toast'
import { useThreadListStore } from '@/stores/threadList'
import { useTurnStore } from '@/stores/turn'
import type { AskMode } from '@/types/api'

const router = useRouter()
const store = useTurnStore()
const listStore = useThreadListStore()

const query = ref('')
const mode = ref<AskMode>('fast')
const submitting = ref(false)

/** 推荐问题 = i18n 键位 + 场景图标 + 提交模式（文案本体在 locales 词表，随语言切换） */
const SUGGESTIONS: { key: 'pg17-vs-16' | 'agent-frameworks' | 'kb-fusion'; icon: string; deep?: boolean }[] = [
  { key: 'pg17-vs-16', icon: 'i-search' },
  { key: 'agent-frameworks', icon: 'i-wave', deep: true },
  { key: 'kb-fusion', icon: 'i-book' },
]

async function submit(q: string = query.value, m?: AskMode): Promise<void> {
  const text = q.trim()
  if (!text || submitting.value) return
  submitting.value = true
  try {
    const data = await store.ask(text, m ?? mode.value)
    query.value = ''
    // 新线程建线程成功 → 重拉线程列表（修复项①顺手修复：侧栏「最近」即时出现新线程；
    // refresh 内同时 best-effort 自愈侧栏 recent（P2-2）——fire-and-forget，失败静默——
    // 列表为辅助导航，错误不阻断提问主链路。
    // 注：编排放视图层而非 turnStore.ask 内——store 侧通道不发额外 GET，测试 mock 队列不被污染）
    void listStore.refresh().catch(() => undefined)
    // §1：跳 /thread/{thread_id}（ask 内已 subscribe）
    await router.push(`/thread/${data.thread_id}`)
  } catch (e) {
    toast(normalizeError(e).message)
  } finally {
    submitting.value = false
  }
}

/* ---------- 输入推荐面板（REQ-047 / 修复项⑦） ---------- */

/** 防抖窗（输入 → 历史匹配切换）与 blur 关闭延迟（条目 click 让路）——修复项⑦规格常量 */
const SUGGEST_DEBOUNCE_MS = 250
const SUGGEST_BLUR_CLOSE_DELAY_MS = 150
/** 面板规格截断：热门池前 6 条 / 历史匹配前 5 条 */
const SUGGEST_HOT_LIMIT = 6
const SUGGEST_HISTORY_LIMIT = 5

const suggestOpen = ref(false)
const debouncedQuery = ref('')
let debounceTimer: ReturnType<typeof setTimeout> | null = null
let blurCloseTimer: ReturnType<typeof setTimeout> | null = null

/**
 * 历史匹配：threadListStore.recent（侧栏「最近」同源，P2-2 起为独立投影）标题包含匹配
 * （大小写不敏感），按 title 精确去重后取前 5 条
 * （用户报告：同题多线程重复显示——同标题多条历史只保留列表序在前者 = 最新一条）。
 */
const historyMatches = computed<string[]>(() => {
  const q = debouncedQuery.value.trim().toLowerCase()
  if (q.length < 2) return []
  const seen = new Set<string>()
  const out: string[] = []
  for (const it of listStore.recent) {
    const title = it.title
    if (!title.toLowerCase().includes(q)) continue
    if (seen.has(title)) continue
    seen.add(title)
    out.push(title)
    if (out.length >= SUGGEST_HISTORY_LIMIT) break
  }
  return out
})

/** 热门池投影：query<2 时取前 6；≥2 切历史匹配后隐藏 */
const hotSuggestions = computed<string[]>(() =>
  debouncedQuery.value.trim().length < 2 ? listStore.suggestPool.slice(0, SUGGEST_HOT_LIMIT) : [],
)

watch(query, (v) => {
  if (debounceTimer) clearTimeout(debounceTimer)
  debounceTimer = setTimeout(() => {
    debouncedQuery.value = v
  }, SUGGEST_DEBOUNCE_MS)
})

function onSuggestFocus(): void {
  if (blurCloseTimer) {
    clearTimeout(blurCloseTimer)
    blurCloseTimer = null
  }
  suggestOpen.value = true
  // 首次聚焦幂等拉取（fetchSuggestPool 内置 loaded 门；失败静默置空、不 toast）
  void listStore.fetchSuggestPool()
}

function onSuggestBlur(): void {
  if (blurCloseTimer) clearTimeout(blurCloseTimer)
  blurCloseTimer = setTimeout(() => {
    blurCloseTimer = null
    suggestOpen.value = false
  }, SUGGEST_BLUR_CLOSE_DELAY_MS)
}

function onSuggestEscape(): void {
  suggestOpen.value = false
}

function onSelectSuggestion(text: string): void {
  suggestOpen.value = false
  void submit(text)
}

onUnmounted(() => {
  if (debounceTimer) clearTimeout(debounceTimer)
  if (blurCloseTimer) clearTimeout(blurCloseTimer)
})
</script>

<template>
  <section class="hero" data-testid="home-view">
    <div class="hero-mark"><UiIcon name="i-logo" size="lg" class="hero-logo" /></div>
    <h1 class="hero-title">{{ $t('home.title') }}</h1>

    <!-- 提问输入卡（fluid = 与推荐面板同宽，修复项④b；HomeSuggestions 经 suggestions 插槽落位） -->
    <SearchCard
      v-model="query"
      v-model:mode="mode"
      :placeholder="$t('home.placeholder')"
      fluid
      :suggest-open="suggestOpen"
      data-testid="home-search-card"
      @submit="submit()"
      @focus="onSuggestFocus"
      @blur="onSuggestBlur"
      @escape="onSuggestEscape"
    >
      <template #suggestions>
        <HomeSuggestions
          :hot-pool="hotSuggestions"
          :history="historyMatches"
          :open="suggestOpen"
          @select="onSelectSuggestion"
        />
      </template>
    </SearchCard>

    <!-- 推荐问题卡（替代 Perplexity 引导卡：快速问答 / 深度调研 / 知识库问答三场景） -->
    <div class="suggest-card">
      <div class="suggest-head">
        <h2>{{ $t('home.suggestTitle') }}</h2>
      </div>
      <button
        v-for="s in SUGGESTIONS"
        :key="s.key"
        type="button"
        class="suggest-item"
        data-testid="home-suggest-item"
        @click="submit($t(`home.suggestItems.${s.key}`), s.deep ? 'deep' : 'fast')"
      >
        <UiIcon :name="s.icon" />
        {{ $t(`home.suggestItems.${s.key}`) }}
        <span class="go"><UiIcon name="i-chev-r" size="sm" /></span>
      </button>
    </div>

    <div class="hero-foot">
      <span>{{ $t('home.localRun') }}</span>
      <span class="dot-sep"></span>
      <span>{{ $t('home.dataLocal') }}</span>
    </div>
  </section>
</template>

<style scoped>
.hero {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: var(--space-16) var(--space-6) var(--space-8);
}
.hero-mark {
  color: var(--color-text-primary);
  margin-bottom: var(--space-6);
}
.hero-logo {
  width: 40px;
  height: 40px;
}
.hero-title {
  font-size: var(--text-3xl);
  line-height: var(--text-3xl--lh);
  font-weight: var(--weight-medium);
  letter-spacing: var(--tracking-tight);
  margin-bottom: var(--space-10);
}
.suggest-card {
  width: min(var(--layout-searchcard-w), 100%);
  margin-top: var(--space-8);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  overflow: hidden;
}
.suggest-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-4) var(--space-5) var(--space-2);
}
.suggest-head h2 {
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
}
.suggest-item {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  width: 100%;
  padding: var(--space-4) var(--space-5);
  border-top: 1px solid var(--color-border-faint);
  color: var(--color-text-primary);
  font-size: var(--text-base);
  text-align: left;
  transition: background var(--duration-fast) var(--ease-out);
}
.suggest-item :deep(.icon) {
  color: var(--color-text-tertiary);
}
.suggest-item .go {
  margin-left: auto;
  color: var(--color-text-faint);
}
.suggest-item:hover {
  background: var(--color-surface-ghost);
}
.suggest-item:hover .go {
  color: var(--color-text-secondary);
}
.hero-foot {
  margin-top: auto;
  padding-top: var(--space-12);
  display: flex;
  align-items: center;
  gap: var(--space-4);
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
}
.dot-sep {
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: var(--color-text-faint);
}
</style>
