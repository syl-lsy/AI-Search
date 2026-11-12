<script setup lang="ts">
/**
 * HomeView（§1 映射：首屏 GET /models 模型 chip（ModelSelect 自取）；提问 → POST /threads →
 * 跳 /thread/{thread_id}（turnStore.ask 已完成建轮+订阅两段式））。
 * 设计稿 01-home：侧栏（App 壳承担）+ 居中 logo/大标题 + 大搜索卡 + 推荐问题卡 + hero-foot。
 * M2 范围留位（本批不实现）：HomeSuggestions 输入推荐面板（SearchCard 已留插槽位）、换一批。
 */
import { ref } from 'vue'
import { useRouter } from 'vue-router'

import SearchCard from '@/components/search/SearchCard.vue'
import UiIcon from '@/components/icons/UiIcon.vue'
import { normalizeError } from '@/api/client'
import { toast } from '@/composables/toast'
import { useTurnStore } from '@/stores/turn'
import type { AskMode } from '@/types/api'

const router = useRouter()
const store = useTurnStore()

const query = ref('')
const mode = ref<AskMode>('fast')
const submitting = ref(false)

const SUGGESTIONS: { text: string; icon: string; deep?: boolean }[] = [
  { text: 'PostgreSQL 17 相比 16 在复制与查询性能上有何差异？', icon: 'i-search' },
  { text: '用深度模式调研：2026 年主流 AI Agent 框架的架构取舍', icon: 'i-wave', deep: true },
  { text: '根据知识库里的设计文档，总结我们检索融合的三种备选方案', icon: 'i-book' },
]

async function submit(q: string = query.value, m?: AskMode): Promise<void> {
  const text = q.trim()
  if (!text || submitting.value) return
  submitting.value = true
  try {
    const data = await store.ask(text, m ?? mode.value)
    query.value = ''
    // §1：跳 /thread/{thread_id}（ask 内已 subscribe）
    await router.push(`/thread/${data.thread_id}`)
  } catch (e) {
    toast(normalizeError(e).message)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <section class="hero" data-testid="home-view">
    <div class="hero-mark"><UiIcon name="i-logo" size="lg" class="hero-logo" /></div>
    <h1 class="hero-title">{{ $t('home.title') }}</h1>

    <!-- 提问输入卡（M2：HomeSuggestions 面板经 suggestions 插槽落位，本批留位不实现） -->
    <SearchCard
      v-model="query"
      v-model:mode="mode"
      :placeholder="$t('home.placeholder')"
      data-testid="home-search-card"
      @submit="submit()"
    />

    <!-- 推荐问题卡（替代 Perplexity 引导卡：快速问答 / 深度调研 / 知识库问答三场景） -->
    <div class="suggest-card">
      <div class="suggest-head">
        <h2>{{ $t('home.suggestTitle') }}</h2>
      </div>
      <button
        v-for="s in SUGGESTIONS"
        :key="s.text"
        type="button"
        class="suggest-item"
        data-testid="home-suggest-item"
        @click="submit(s.text, s.deep ? 'deep' : 'fast')"
      >
        <UiIcon :name="s.icon" />
        {{ s.text }}
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
