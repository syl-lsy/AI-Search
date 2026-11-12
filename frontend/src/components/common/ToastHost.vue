<script setup lang="ts">
// ToastHost — 全站 toast 出口（§2：服务端 message 直达；挂在 App 壳一次）。
import { useToasts } from '@/composables/toast'

const toasts = useToasts()
</script>

<template>
  <div class="toast-host" role="status" aria-live="polite" data-testid="toast-host">
    <TransitionGroup name="toast">
      <div v-for="t in toasts" :key="t.id" class="toast-item">{{ t.message }}</div>
    </TransitionGroup>
  </div>
</template>

<style scoped>
.toast-host {
  position: fixed;
  bottom: var(--space-8);
  left: 50%;
  transform: translateX(-50%);
  z-index: 50;
  display: grid;
  gap: var(--space-2);
  pointer-events: none;
}
.toast-item {
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-floating);
  color: var(--color-text-primary);
  font-size: var(--text-sm);
  padding: var(--space-2) var(--space-4);
  max-width: 480px;
}
.toast-enter-active,
.toast-leave-active {
  transition: opacity var(--duration-base) var(--ease-out), transform var(--duration-base) var(--ease-out);
}
.toast-enter-from,
.toast-leave-to {
  opacity: 0;
  transform: translateY(8px);
}
</style>
