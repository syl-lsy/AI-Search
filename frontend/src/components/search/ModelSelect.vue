<script setup lang="ts">
/**
 * ModelSelect — 对话模型切换 chip（frontend-api.md §5 ModelSelect 行；REQ-010）。
 *
 * - 数据源 = GET /models（src/api/models.ts 唯一出口）+ prefsStore；
 * - label 为服务端常量（api.md §3.1：前端不拼接不回退）；当前值展示 prefs.model，
 *   无用户选择展示 default（§2 v1.7 注：初值不本地硬编码；提交体不携带 model 走服务端缺省）；
 * - 最小下拉（button + listbox）：Esc/失焦收起；选中项 aria-selected 标注。
 */
import { computed, onMounted, ref } from 'vue'

import { fetchModels } from '@/api/models'
import { normalizeError } from '@/api/client'
import UiIcon from '@/components/icons/UiIcon.vue'
import { toast } from '@/composables/toast'
import { usePrefsStore } from '@/stores/prefs'
import type { ChatModelKey } from '@/types/api'

const prefs = usePrefsStore()

const options = ref<{ key: ChatModelKey; label: string }[]>([])
const defaultKey = ref<ChatModelKey | null>(null)
const open = ref(false)
const rootEl = ref<HTMLElement | null>(null)

const currentKey = computed<ChatModelKey | null>(() => prefs.model ?? defaultKey.value)
const currentLabel = computed(() => options.value.find((o) => o.key === currentKey.value)?.label ?? currentKey.value ?? '')

onMounted(async () => {
  try {
    const data = await fetchModels()
    options.value = data.options
    defaultKey.value = data.default
  } catch (e) {
    // 模型清单不可用：chip 静默展示占位（不打断页面主链路；服务端 message 仅 toast 一次）
    toast(normalizeError(e).message)
  }
})

function toggle(): void {
  open.value = !open.value
}

function pick(key: ChatModelKey): void {
  prefs.setModel(key)
  open.value = false
}

function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape') open.value = false
}

function onBlurRoot(e: FocusEvent): void {
  // 焦点移出组件根（含下拉）→ 收起；relatedTarget 在根内则保持
  const next = e.relatedTarget
  if (next instanceof Node && rootEl.value?.contains(next)) return
  open.value = false
}
</script>

<template>
  <div ref="rootEl" class="ms-root" @keydown="onKeydown" @focusout="onBlurRoot">
    <button
      type="button"
      class="model-select"
      data-testid="model-select"
      :aria-haspopup="'listbox'"
      :aria-expanded="open ? 'true' : 'false'"
      @click="toggle"
    >
      <UiIcon name="i-wave" size="sm" />
      <span>{{ $t('common.model') }}<template v-if="currentLabel"> · {{ currentLabel }}</template></span>
      <UiIcon name="i-chev-d" size="sm" />
    </button>
    <ul v-if="open && options.length > 0" class="ms-menu" role="listbox" data-testid="model-menu">
      <li v-for="o in options" :key="o.key">
        <button
          type="button"
          role="option"
          class="ms-item"
          :class="{ sel: o.key === currentKey }"
          :aria-selected="o.key === currentKey ? 'true' : 'false'"
          @click="pick(o.key)"
        >
          {{ o.label }}
          <span v-if="o.key === currentKey" class="ms-check">✓</span>
        </button>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.ms-root {
  position: relative;
  display: inline-flex;
}
.model-select {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  padding: 0 var(--space-2) 0 var(--space-3);
  border-radius: var(--radius-full);
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
}
.model-select:hover {
  background: var(--color-surface-hover);
  color: var(--color-text-primary);
}
.ms-menu {
  position: absolute;
  bottom: calc(100% + 6px);
  right: 0;
  min-width: 160px;
  margin: 0;
  padding: var(--space-1);
  list-style: none;
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-floating);
  z-index: 30;
}
.ms-item {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-sm);
  color: var(--color-text-primary);
  font-size: var(--text-base);
  text-align: left;
}
.ms-item:hover {
  background: var(--color-surface-ghost);
}
.ms-item.sel {
  color: var(--color-accent);
  font-weight: var(--weight-medium);
}
.ms-check {
  font-size: var(--text-sm);
}
</style>
