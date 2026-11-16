<script setup lang="ts">
/**
 * ViewFilterRadio — 三态视图 radio（frontend-api.md §5 v1.21 / REQ-058/A42）：
 * 全部 / 未读 / 已归档 → 请求参数映射（active=archived 缺省、unread=unread=true、
 * archived=archived=true，api.md §2.2 v1.18）；切换重置分页（由 store.setViewFilter 收口）；
 * 与 mode chips、q 搜索 AND 叠加。
 * a11y（契约硬性）：role=radiogroup/radio、方向键可切换、Tab 可达、aria-checked、
 * roving tabindex（选中项 tab 首达）。
 */
import { nextTick, ref } from 'vue'

import { normalizeError } from '@/api/client'
import { toast } from '@/composables/toast'
import { useThreadListStore, type ThreadViewFilter } from '@/stores/threadList'

const props = defineProps<{
  /** v-model：当前视图（store.viewFilter 投影） */
  modelValue: ThreadViewFilter
}>()

const emit = defineEmits<{
  'update:modelValue': [value: ThreadViewFilter]
}>()

const store = useThreadListStore()

const OPTIONS: { value: ThreadViewFilter; key: string }[] = [
  { value: 'active', key: 'threads.viewAll' },
  { value: 'unread', key: 'threads.viewUnread' },
  { value: 'archived', key: 'threads.viewArchived' },
]

/** roving tabindex：选中项 0，其余 -1（Tab 只达选中项，方向键切换） */
function tabIndex(value: ThreadViewFilter): '0' | '-1' {
  return value === props.modelValue ? '0' : '-1'
}

/**
 * 选择切换（P2-4，2026-11-15）：store.setViewFilter 收口请求（重置分页+首拉）；
 * **失败 catch + toast 服务端 message**（与 mode chips 的 setFilter 口径一致）——
 * store 失败时回滚 viewFilter，radio 选中态随 v-model prop 复原（选中态与列表视图恒一致，
 * 不再出现「radio 已切、列表仍旧视图」的失配）。
 */
function select(value: ThreadViewFilter): void {
  if (value === props.modelValue) return
  emit('update:modelValue', value)
  void store.setViewFilter(value).catch((error: unknown) => {
    toast(normalizeError(error).message)
  })
}

/** 方向键切换后焦点随选中项移动（真 radio 语义：选中和焦点同步走） */
const rootEl = ref<HTMLElement | null>(null)

function onArrow(from: ThreadViewFilter, delta: 1 | -1): void {
  const idx = OPTIONS.findIndex((o) => o.value === from)
  const next = OPTIONS[(idx + delta + OPTIONS.length) % OPTIONS.length]!
  select(next.value)
  void nextTick(() => {
    rootEl.value
      ?.querySelector<HTMLButtonElement>(`[data-testid="view-filter-${next.value}"]`)
      ?.focus()
  })
}
</script>

<template>
  <div ref="rootEl" class="view-filter" role="radiogroup" :aria-label="$t('threads.title')" data-testid="view-filter">
    <button
      v-for="o in OPTIONS"
      :key="o.value"
      type="button"
      role="radio"
      class="vf-btn"
      :class="{ on: modelValue === o.value }"
      :aria-checked="modelValue === o.value ? 'true' : 'false'"
      :tabindex="tabIndex(o.value)"
      :data-testid="`view-filter-${o.value}`"
      @click="select(o.value)"
      @keydown.right.prevent="onArrow(o.value, 1)"
      @keydown.down.prevent="onArrow(o.value, 1)"
      @keydown.left.prevent="onArrow(o.value, -1)"
      @keydown.up.prevent="onArrow(o.value, -1)"
    >
      {{ $t(o.key) }}
    </button>
  </div>
</template>

<style scoped>
.view-filter {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  padding: var(--space-1);
  background: var(--color-surface-ghost);
  border-radius: var(--radius-full);
}
.vf-btn {
  padding: var(--space-1) var(--space-3);
  border-radius: var(--radius-full);
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  white-space: nowrap;
  transition: background var(--duration-fast) var(--ease-out), color var(--duration-fast) var(--ease-out);
}
.vf-btn:hover {
  color: var(--color-text-primary);
}
.vf-btn.on {
  background: var(--color-surface-active);
  color: var(--color-text-primary);
  font-weight: var(--weight-medium);
}
.vf-btn:focus-visible {
  outline: 2px solid var(--color-accent-border);
  outline-offset: 1px;
}
</style>
