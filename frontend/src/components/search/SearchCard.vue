<script setup lang="ts">
/**
 * SearchCard — 提问输入卡（frontend-api.md §5 SearchCard/ModeChips/SubmitFab 行；设计稿三处复用：
 * 首页大卡 / 历史页内嵌 / 答案页追问框）。
 *
 * - Enter 提交（Shift+Enter 换行；IME 组合中的 Enter 不劫持）；submit fab 空输入禁用；
 * - streaming=true（运行中轮）：提交钮变「停止」（§2：onConflict null/reject 现状；M1 最小实现
 *   不携带字段即 reject——非 reject 模式选择器留位）→ emit('cancel')；
 * - 附件/语音为 M4/后续留位（disabled 占位，不挂事件）；
 * - 首页输入推荐面板（REQ-047，M2 范围）本批留位不实现——插槽位见模板注释。
 */
import { computed } from 'vue'

import UiIcon from '@/components/icons/UiIcon.vue'
import ModelSelect from '@/components/search/ModelSelect.vue'
import type { AskMode } from '@/types/api'

const props = defineProps<{
  modelValue: string
  mode: AskMode
  /** 运行中轮 = true：提交钮变停止（追问框场景） */
  streaming?: boolean
  placeholder?: string
  compact?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: string]
  'update:mode': [mode: AskMode]
  submit: []
  cancel: []
}>()

const canSubmit = computed(() => props.modelValue.trim().length > 0)

function onInput(e: Event): void {
  emit('update:modelValue', (e.target as HTMLTextAreaElement).value)
}

function onKeydown(e: KeyboardEvent): void {
  if (e.key !== 'Enter' || e.shiftKey) return
  // IME 组合中的 Enter 不劫持（isComposing + keyCode 229 双口径，设计稿页尾 JS 同款）
  if (e.isComposing || e.keyCode === 229) return
  if (props.streaming) return
  e.preventDefault()
  if (canSubmit.value) emit('submit')
}
</script>

<template>
  <div class="search-card" :class="{ compact }" data-testid="search-card">
    <textarea
      :value="modelValue"
      rows="1"
      :placeholder="placeholder"
      :aria-label="placeholder"
      @input="onInput"
      @keydown="onKeydown"
    />
    <div class="search-tools">
      <button
        type="button"
        class="icon-btn attach-btn"
        :title="$t('common.attach')"
        :aria-label="$t('common.attach')"
        disabled
      >
        <UiIcon name="i-attach" size="sm" />
      </button>
      <button
        type="button"
        class="chip"
        :class="{ active: mode === 'fast' }"
        :aria-pressed="mode === 'fast' ? 'true' : 'false'"
        data-testid="mode-fast"
        @click="emit('update:mode', 'fast')"
      >
        <UiIcon name="i-zap" size="sm" />{{ $t('mode.fast') }}
      </button>
      <button
        type="button"
        class="chip"
        :class="{ active: mode === 'deep' }"
        :aria-pressed="mode === 'deep' ? 'true' : 'false'"
        data-testid="mode-deep"
        @click="emit('update:mode', 'deep')"
      >
        <UiIcon name="i-wave" size="sm" />{{ $t('mode.deep') }}
      </button>
      <div class="st-right">
        <span class="mode-sep" aria-hidden="true"></span>
        <ModelSelect />
        <button
          v-if="streaming"
          type="button"
          class="stop-btn"
          :title="$t('answer.stop')"
          data-testid="stop-btn"
          @click="emit('cancel')"
        >
          <UiIcon name="i-stop" size="sm" />{{ $t('answer.stop') }}
        </button>
        <button
          v-else
          type="button"
          class="submit-fab"
          :disabled="!canSubmit"
          :title="$t('common.submit')"
          data-testid="submit-btn"
          @click="canSubmit && emit('submit')"
        >
          <UiIcon name="i-arrow-up" />
        </button>
      </div>
    </div>
    <!-- 首页输入推荐面板（REQ-047 / HomeSuggestions，M2 范围）留位：面板内嵌于卡片末位、
         随卡片流内展开（设计说明 §8.1），本批不实现——落位时在此追加，仅首页生效。 -->
    <slot name="suggestions" />
  </div>
</template>

<style scoped>
.search-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-card);
  padding: var(--space-4) var(--space-4) var(--space-3);
  transition: border-color var(--duration-base) var(--ease-out);
}
.search-card:focus-within {
  border-color: var(--color-text-faint);
}
.search-card textarea {
  width: 100%;
  min-height: 56px;
  resize: none;
  background: none;
  border: 0;
  outline: none;
  font-size: var(--text-md);
  line-height: var(--text-md--lh);
  color: var(--color-text-primary);
}
.search-card.compact textarea {
  min-height: 44px;
  font-size: var(--text-md);
}
.search-card textarea::placeholder {
  color: var(--color-text-faint);
}
.search-tools {
  margin-top: var(--space-3);
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.search-tools .st-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.attach-btn {
  width: 32px;
  height: 32px;
}
.attach-btn:disabled {
  cursor: default;
  color: var(--color-text-faint);
}
.mode-sep {
  width: 1px;
  height: 20px;
  background: var(--color-border);
  margin: 0 2px;
}
.stop-btn {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  height: 36px;
  padding: 0 var(--space-4);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-full);
  color: var(--color-text-primary);
  font-size: var(--text-sm);
}
.stop-btn:hover {
  background: var(--color-surface-hover);
}
</style>
