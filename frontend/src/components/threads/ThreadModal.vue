<script setup lang="ts">
/**
 * ThreadModal — 居中 modal 通用小件（frontend-api.md §5 v1.23；Perplexity 居中弹窗形态对标：
 * 遮罩变暗 + 面板居中 + 标题行 + 右上 × 关闭钮）。
 *
 * 职责边界：本组件只承载「壳」——Teleport 到 body、遮罩/×/Esc 三条关闭通道（emit close，
 * 焦点返还由调用方处理）、role/aria-label 语义；标题、正文、按钮全部经 props + 默认插槽
 * 由调用方组装（ThreadActionMenu 重命名/删除确认与会话页批量删除确认三处复用，§5）。
 * a11y：aria-modal、role=dialog|alertdialog（调用方按语义传）、Esc 关闭；面板内 Tab 序
 * 由 DOM 序自然形成（本组件不实现焦点圈闭，M1 范围内所有用法均为 1~2 个可聚焦元素）。
 */
import { onUnmounted, watch } from 'vue'
import { useI18n } from 'vue-i18n'

const props = withDefaults(
  defineProps<{
    open: boolean
    title: string
    /** 语义角色：普通输入/确认 dialog；不可逆危险确认用 alertdialog */
    role?: 'dialog' | 'alertdialog'
  }>(),
  { role: 'dialog' },
)

const emit = defineEmits<{ close: [] }>()

const { t } = useI18n()

/** Esc 关闭（document 级；stopPropagation 防止页面级 Esc 语义——批量选择清除等——同时触发）。
 *  IME 守卫（评审 P3）：组合期 Esc = 取消拼音候选，不得连带关 modal 丢半成品输入
 *  （与重命名输入 Enter isComposing 守卫同族对称）。 */
function onDocKeydown(e: KeyboardEvent): void {
  if (e.isComposing) return
  if (e.key !== 'Escape') return
  e.stopPropagation()
  emit('close')
}

watch(
  () => props.open,
  (open) => {
    if (open) document.addEventListener('keydown', onDocKeydown, true)
    else document.removeEventListener('keydown', onDocKeydown, true)
  },
  { immediate: true },
)

onUnmounted(() => document.removeEventListener('keydown', onDocKeydown, true))
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="tm-overlay"
      data-testid="thread-modal-overlay"
      @click="emit('close')"
    >
      <div
        class="tm-panel"
        :role="role"
        aria-modal="true"
        :aria-label="title"
        @click.stop
      >
        <div class="tm-head">
          <h2 class="tm-title">{{ title }}</h2>
          <button
            type="button"
            class="tm-x"
            :aria-label="t('common.cancel')"
            data-testid="thread-modal-close"
            @click="emit('close')"
          >
            ×
          </button>
        </div>
        <slot />
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
/* 遮罩变暗（Perplexity 居中 modal 形态；与 tokens.css .sb-backdrop 同值 rgba(0,0,0,.5)——
   遮罩属浮层通用黑，无语义色对应 token，与布局骨架先例一致） */
.tm-overlay {
  position: fixed;
  inset: 0;
  z-index: 60;
  display: grid;
  place-items: center;
  background: rgba(0, 0, 0, 0.5);
  padding: var(--space-4);
}
.tm-panel {
  width: min(420px, 100%);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-floating);
  padding: var(--space-4) var(--space-5) var(--space-5);
}
.tm-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  margin-bottom: var(--space-3);
}
.tm-title {
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  color: var(--color-text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tm-x {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  flex: none;
  border-radius: var(--radius-full);
  color: var(--color-text-tertiary);
  font-size: var(--text-lg);
  line-height: 1;
  transition: background var(--duration-fast) var(--ease-out), color var(--duration-fast) var(--ease-out);
}
.tm-x:hover {
  background: var(--color-surface-ghost);
  color: var(--color-text-primary);
}
</style>
