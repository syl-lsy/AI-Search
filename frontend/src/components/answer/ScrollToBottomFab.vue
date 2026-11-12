<script setup lang="ts">
/**
 * ScrollToBottomFab — 顶部向下箭头（frontend-api.md §5 v1.8；REQ-045 / A28；设计稿 .scroll-fab）。
 *
 * - 对话列水平中央、顶部悬空约 34px 圆钮（零高 sticky 槽位，不占文档流——父级装配槽位）；
 * - 显隐 = 滚动位置距底超过约一视口高（AnswerScrollController 判定），淡入淡出过渡
 *   （--duration-base）；流式生成中与回看历史均生效；
 * - 点击 emit('click') → AnswerScrollController.resumeViaFab()（REQ-043 恢复途径② + 平滑滚底）；
 * - a11y：aria-label「滚动到底部」（i18n answer.scrollToBottom）、原生 button 键盘可达可激活；
 * - <720px 窄屏保留、位置不变（与 REQ-044 锚点策略不同，PRD 明文）。
 */
defineProps<{ visible: boolean }>()

defineEmits<{
  click: []
}>()
</script>

<template>
  <Transition name="fab-fade">
    <button
      v-if="visible"
      type="button"
      class="scroll-fab"
      data-testid="scroll-fab"
      :aria-label="$t('answer.scrollToBottom')"
      :title="$t('answer.scrollToBottom')"
      @click="$emit('click')"
    >
      <svg class="icon-sm" aria-hidden="true" focusable="false"><use href="#i-arrow-down" /></svg>
    </button>
  </Transition>
</template>

<style scoped>
.scroll-fab {
  width: 34px;
  height: 34px;
  flex: none;
  display: grid;
  place-items: center;
  border-radius: var(--radius-full);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  color: var(--color-text-secondary);
  box-shadow: var(--shadow-card);
  transition: background var(--duration-fast) var(--ease-out), color var(--duration-fast) var(--ease-out),
    opacity var(--duration-base) var(--ease-out);
}
.scroll-fab:hover {
  background: var(--color-surface-hover);
  color: var(--color-text-primary);
}
/* 淡入淡出（REQ-045：出现/消失带过渡） */
.fab-fade-enter-active,
.fab-fade-leave-active {
  transition: opacity var(--duration-base) var(--ease-out);
}
.fab-fade-enter-from,
.fab-fade-leave-to {
  opacity: 0;
}
</style>
