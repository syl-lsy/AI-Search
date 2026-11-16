<script setup lang="ts">
/**
 * QuestionHoverActions — 用户问题 hover 操作（REQ-055/A39，frontend-api.md §5 v1.18 落位）。
 *
 * hover（或键盘 focus）问题气泡时于气泡侧显示「编辑查询」「复制查询」两图标：
 *   - 编辑查询 = emit('edit', question) → AnswerView 预填底部追问框并聚焦（不提交，
 *     不改写历史轮次——用户裁决非 Perplexity 原位内联编辑，改后走既有追问流程提交新轮）；
 *   - 复制查询 = query 原文进剪贴板（失败 console 留痕 + 一次性 toast——批次 2 评审 P3
 *     闭合，推翻旧口径「不 toast」；成功零打扰不打断阅读）。
 * 显示触发（P1 双通道）：父级 hover 态（mouseenter/leave）+ 焦点态（focusin/focusout——
 * 键盘 Tab 可达即显示）。图标 i-pencil / i-copy 入 AppIconSprite。
 * v1.24：两钮接入 UiTooltip（主标题替换原生 title；aria-label 保留——接入面补漏，
 * frontend-api.md §5 UiTooltip 行）。
 */
import { useI18n } from 'vue-i18n'

import UiIcon from '@/components/icons/UiIcon.vue'
import UiTooltip from '@/components/common/UiTooltip.vue'
import { toast } from '@/composables/toast'

defineProps<{
  question: string
  /** 显隐态（父级 hover/focus 双通道驱动，TurnBlock 问题行 mouseenter/focusin 维护） */
  visible: boolean
}>()

const emit = defineEmits<{
  edit: [question: string]
}>()

const { t } = useI18n()

/** 复制失败：console 留痕（排障）+ 一次性 toast（剪贴板不可用/拒绝授权；成功零打扰，A39） */
async function copyQuestion(question: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(question)
  } catch (error) {
    console.warn('[QuestionHoverActions] 复制查询失败', error)
    toast(t('answer.copyFailed'))
  }
}
</script>

<template>
  <span class="qact" :class="{ visible }" data-testid="question-hover-actions">
    <UiTooltip :label="$t('answer.editQuery')">
      <button
        type="button"
        class="qact-btn"
        data-testid="qact-edit"
        :aria-label="$t('answer.editQuery')"
        @click="emit('edit', question)"
      >
        <UiIcon name="i-pencil" size="sm" />
      </button>
    </UiTooltip>
    <UiTooltip :label="$t('answer.copyQuery')">
      <button
        type="button"
        class="qact-btn"
        data-testid="qact-copy"
        :aria-label="$t('answer.copyQuery')"
        @click="copyQuestion(question)"
      >
        <UiIcon name="i-copy" size="sm" />
      </button>
    </UiTooltip>
  </span>
</template>

<style scoped>
.qact {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  flex: none;
  opacity: 0;
  visibility: hidden;
  transition: opacity var(--duration-fast) var(--ease-out);
}
/* 显隐由父级 hover/focus 态类驱动（visible class，JS 态可测） */
.qact.visible {
  opacity: 1;
  visibility: visible;
}
.qact-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: var(--radius-full);
  color: var(--color-text-tertiary);
  transition: background var(--duration-fast) var(--ease-out), color var(--duration-fast) var(--ease-out);
}
.qact-btn:hover {
  background: var(--color-surface-ghost);
  color: var(--color-text-primary);
}
</style>
