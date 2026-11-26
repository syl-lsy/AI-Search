<script setup lang="ts">
/**
 * KbDeleteConfirmModal — 删除级联确认（plan-m3-b6-f6 §1.1；api.md §4.5「前端删除动作二次确认
 * 并提示级联范围」；设计稿 ovl-del）。ThreadModal 壳 role=alertdialog（不可逆危险确认语义，
 * ThreadActionMenu 删除确认先例）+ 三存储清单（kb_chunks ‖ ES ‖ MinIO）。
 * 焦点管理：open 落取消钮（批量确认先例「聚焦取消钮」）、close/卸载归还先前焦点。
 */
import { nextTick, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import ThreadModal from '@/components/threads/ThreadModal.vue'
import UiIcon from '@/components/icons/UiIcon.vue'
import type { KbDocument } from '@/types/api'

const props = defineProps<{
  open: boolean
  doc: KbDocument | null
}>()

const emit = defineEmits<{ close: []; confirm: [] }>()

const { t } = useI18n()

const cancelEl = ref<HTMLButtonElement | null>(null)
let previouslyFocused: HTMLElement | null = null

watch(
  () => props.open,
  async (open) => {
    if (open) {
      previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null
      await nextTick()
      cancelEl.value?.focus()
    } else if (previouslyFocused !== null) {
      previouslyFocused.focus()
      previouslyFocused = null
    }
  },
)
onUnmounted(() => {
  if (previouslyFocused !== null) {
    previouslyFocused.focus()
    previouslyFocused = null
  }
})
</script>

<template>
  <ThreadModal :open="open" :title="t('library.deleteTitle')" role="alertdialog" @close="emit('close')">
    <div data-testid="kb-delete-confirm">
      <div class="del-target">{{ doc?.title }}</div>
      <p class="del-warn">
        {{ t('library.deleteWarn') }}
      </p>
      <!-- 三存储级联清单（api.md §4.5：kb_chunks（pg 事实源）‖ ES 索引 ‖ MinIO 原件+ir.json） -->
      <div class="store-list">
        <div class="store-row">
          <span class="store-ico"><UiIcon name="i-db" size="sm" /></span>
          <div class="store-txt">
            <div class="store-name">{{ $t('library.deleteStorePgName') }}</div>
            <div class="store-detail">{{ $t('library.deleteStorePgDetail') }}</div>
          </div>
          <span v-if="doc !== null" class="store-count">{{ $t('library.deleteChunkUnit', { n: doc.chunk_count }) }}</span>
        </div>
        <div class="store-row">
          <span class="store-ico"><UiIcon name="i-layers" size="sm" /></span>
          <div class="store-txt">
            <div class="store-name">{{ $t('library.deleteStoreEsName') }}</div>
            <div class="store-detail">{{ $t('library.deleteStoreEsDetail') }}</div>
          </div>
          <span v-if="doc !== null" class="store-count">{{ $t('library.deleteChunkUnit', { n: doc.chunk_count }) }}</span>
        </div>
        <div class="store-row">
          <span class="store-ico"><UiIcon name="i-box" size="sm" /></span>
          <div class="store-txt">
            <div class="store-name">{{ $t('library.deleteStoreMinioName') }}</div>
            <div class="store-detail">{{ $t('library.deleteStoreMinioDetail') }}</div>
          </div>
        </div>
      </div>
      <p class="del-note">{{ $t('library.deleteCascadeNote') }}</p>
      <div class="del-actions">
        <button
          ref="cancelEl"
          type="button"
          class="btn btn-bordered"
          data-testid="kb-delete-cancel"
          @click="emit('close')"
        >
          {{ $t('common.cancel') }}
        </button>
        <button type="button" class="btn btn-danger" data-testid="kb-delete-confirm-btn" @click="emit('confirm')">
          {{ $t('library.deleteConfirm') }}
        </button>
      </div>
    </div>
  </ThreadModal>
</template>

<style scoped>
.del-target {
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
  color: var(--color-text-primary);
  margin-bottom: var(--space-2);
  word-break: break-all;
}
.del-warn {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
  margin: 0 0 var(--space-3);
}
.store-list {
  display: grid;
  gap: var(--space-2);
}
.store-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface-ghost);
}
.store-ico {
  width: 28px;
  height: 28px;
  flex: none;
  display: grid;
  place-items: center;
  border-radius: var(--radius-sm);
  background: var(--color-surface-active);
  color: var(--color-text-secondary);
}
.store-txt {
  min-width: 0;
  flex: 1;
}
.store-name {
  font-size: var(--text-sm);
  color: var(--color-text-primary);
  font-weight: var(--weight-medium);
}
.store-detail {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  margin-top: 1px;
}
.store-count {
  flex: none;
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  font-variant-numeric: tabular-nums;
}
.del-note {
  margin: var(--space-3) 0 0;
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  line-height: 1.6;
}
.del-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
  margin-top: var(--space-4);
}
/* .btn-danger 已合入 tokens.css 全局按钮变体（2026-11-26 用户裁决，OQ-2 清偿——
   原 scoped #ef5a5f/#fff 字面量随批收编为 --color-danger-hover token），此处零组件级样式 */
</style>
