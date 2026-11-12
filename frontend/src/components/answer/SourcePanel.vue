<script setup lang="ts">
/**
 * SourcePanel — 来源面板（frontend-api.md §5 SourcePanel/SourceCard 行；设计稿 sources-panel）。
 *
 * - SSE sources 批次 + 2.5 对账双源（数据由父级投影 sources 数组）；
 * - 【网络】/【知识库】徽标 = source_type；「收集中」徽标 = collecting（进行中轮）；
 * - 快照胶囊 = snapshot_available → emit('snapshot', sourceId)（api.md §5.1）；
 * - 卡片带 data-source-card=ref_no（[n] 引用联动定位锚点，REQ-003）。
 */
import { ref } from 'vue'

import UiIcon from '@/components/icons/UiIcon.vue'
import type { Source } from '@/types/api'

const props = defineProps<{
  sources: Source[]
  collecting: boolean
}>()

const emit = defineEmits<{
  snapshot: [sourceId: string]
}>()

const open = ref(true)

/** url 可得时展示域名（来源页链接）；KB 来源（url=null）展示 kb_doc_id 短标识 */
function domainOf(src: Source): string {
  if (src.url) {
    try {
      return new URL(src.url).hostname
    } catch {
      return src.url
    }
  }
  return src.kb_doc_id ? src.kb_doc_id.slice(0, 8) : ''
}
</script>

<template>
  <aside class="sources-panel" data-testid="source-panel">
    <button
      type="button"
      class="src-panel-head"
      :class="{ open }"
      :aria-expanded="open ? 'true' : 'false'"
      @click="open = !open"
    >
      <UiIcon name="i-link" size="sm" />
      {{ $t('answer.sources') }}
      <span class="count">{{ sources.length }}</span>
      <span v-if="collecting" class="badge badge-accent">{{ $t('answer.collecting') }}</span>
      <UiIcon name="i-chev-d" size="sm" class="chev" />
    </button>
    <div v-show="open" class="src-body">
      <div
        v-for="src in sources"
        :key="src.source_id"
        class="src-card"
        :data-source-card="src.ref_no"
      >
        <div class="src-head">
          <span class="src-num">{{ src.ref_no }}</span>
          <span class="src-domain">
            <UiIcon :name="src.url ? 'i-globe' : 'i-db'" size="sm" />
            {{ domainOf(src) }}
          </span>
          <span class="src-badges">
            <span v-if="src.source_type === 'kb'" class="badge badge-kb">
              <UiIcon name="i-book" size="sm" />{{ $t('answer.sourceKb') }}
            </span>
            <span v-else class="badge badge-outline">
              <UiIcon name="i-globe" size="sm" />{{ $t('answer.sourceWeb') }}
            </span>
          </span>
        </div>
        <div class="src-title">{{ src.title }}</div>
        <div class="src-snip">{{ src.snippet }}</div>
        <button
          v-if="src.snapshot_available"
          type="button"
          class="snapshot-pill"
          :data-testid="`snapshot-${src.ref_no}`"
          @click="emit('snapshot', src.source_id)"
        >
          <UiIcon name="i-camera" size="sm" />{{ $t('answer.snapshot') }}
        </button>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.sources-panel {
  align-self: start;
  position: sticky;
  top: var(--space-4);
  min-width: 0;
}
.src-panel-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  padding-bottom: var(--space-3);
  width: 100%;
}
.src-panel-head .count {
  color: var(--color-text-primary);
  font-weight: var(--weight-medium);
}
.src-panel-head .chev {
  margin-left: auto;
  transition: transform var(--duration-base) var(--ease-out);
}
.src-panel-head.open .chev {
  transform: rotate(180deg);
}
.src-body {
  display: grid;
  gap: var(--space-3);
}
.src-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--space-4);
  display: grid;
  gap: var(--space-2);
  transition: background var(--duration-fast) var(--ease-out), border-color var(--duration-fast) var(--ease-out);
}
.src-card:hover {
  background: var(--color-surface-hover);
  border-color: var(--color-border-strong);
}
.src-card.cite-hit {
  border-color: var(--color-accent-border);
  background: var(--color-accent-bg);
}
.src-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.src-num {
  width: 20px;
  height: 20px;
  flex: none;
  border-radius: var(--radius-full);
  background: var(--color-surface-active);
  border: 1px solid var(--color-border-strong);
  display: grid;
  place-items: center;
  font-size: 11px;
  color: var(--color-text-secondary);
}
.src-domain {
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.src-badges {
  margin-left: auto;
  display: inline-flex;
  gap: 4px;
  flex: none;
}
.src-title {
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  line-height: 1.5;
}
.src-snip {
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
  line-height: 1.55;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.snapshot-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-full);
  padding: 2px 8px;
  cursor: pointer;
  justify-self: start;
}
.snapshot-pill:hover {
  color: var(--color-text-primary);
}
@media (max-width: 1200px) {
  .sources-panel {
    position: static;
  }
  .src-body {
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  }
}
</style>
