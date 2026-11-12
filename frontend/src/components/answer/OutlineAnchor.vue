<script setup lang="ts">
/**
 * OutlineAnchor — 右侧导航锚点（frontend-api.md §5 v1.8；REQ-044 / A27；设计稿 .toc-rail）。
 *
 * - 形态对齐 DeepSeek 实测参照（设计说明 §6.2）：fixed 视口右缘 16px 垂直居中，收起态 34px
 *   半透明胶囊（--color-surface-translucent），横线指示器 8×2px/行距 30px/激活 12×3px 主题色，
 *   悬停左展 242px 卡片（13px 单行 ellipsis + 右侧横线，激活行主题色 + 字重 500）；
 * - 出现条件 = items ≥ 2 或内容超约一屏（visible 由父级判定）；否则不渲染（无空区块）；
 * - a11y 硬性（A27）：条目 button 语义 / Tab 可达（原生 button）/ Enter 激活 / aria-current
 *   标激活项——色彩一律 tokens.css 映射（不逐色复刻 DeepSeek）；
 * - <720px 窄屏隐藏（REQ-044，useMediaQuery 驱动 + CSS 双保险）。
 */
import { computed } from 'vue'

import { useMediaQuery } from '@/composables/mediaQuery'

export interface OutlineItem {
  /** 目标元素 id（turn 问题块 / AnswerRenderer 标题 id / 快答容器 id） */
  id: string
  /** 条目文本（块首文本截断，父级负责截断） */
  label: string
}

const props = defineProps<{
  items: OutlineItem[]
  /** 激活项 id（父级滚动实时跟随） */
  activeId: string | null
  /** 出现条件判定（items ≥ 2 或内容超约一屏） */
  visible: boolean
}>()

const emit = defineEmits<{
  select: [id: string]
}>()

const isNarrow = useMediaQuery('(max-width: 720px)')

const shown = computed(() => props.visible && props.items.length > 0 && !isNarrow.value)
</script>

<template>
  <nav v-if="shown" class="toc-rail" data-testid="outline-anchor" :aria-label="$t('answer.outlineNav')">
    <button
      v-for="item in items"
      :key="item.id"
      type="button"
      class="toc-item"
      :class="{ active: item.id === activeId }"
      :aria-current="item.id === activeId ? 'true' : undefined"
      :data-testid="`outline-item-${item.id}`"
      @click="emit('select', item.id)"
    >
      <span class="toc-txt">{{ item.label }}</span>
      <span class="toc-bar" aria-hidden="true"></span>
    </button>
  </nav>
</template>

<style scoped>
.toc-rail {
  position: fixed;
  right: var(--space-4);
  top: 50%;
  transform: translateY(-50%);
  z-index: 20;
  display: flex;
  flex-direction: column;
  width: 34px;
  max-height: 300px;
  padding: var(--space-2) 0;
  overflow-x: hidden;
  overflow-y: auto;
  background: var(--color-surface-translucent);
  border: 1px solid transparent; /* 展开态描边占位，避免展开瞬间宽度跳动 */
  border-radius: var(--radius-lg);
  transition: width var(--duration-base) var(--ease-out),
    background var(--duration-base) var(--ease-out),
    border-color var(--duration-base) var(--ease-out),
    box-shadow var(--duration-base) var(--ease-out);
}
.toc-rail:hover {
  width: 242px;
  background: var(--color-surface);
  border-color: var(--color-border);
  box-shadow: var(--shadow-card);
}
.toc-item {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--space-2);
  height: 30px;
  flex: none;
  padding: 0 var(--space-2);
  border-radius: var(--radius-sm);
  text-align: left;
  transition: background var(--duration-fast) var(--ease-out);
}
.toc-item:hover {
  background: var(--color-surface-ghost);
}
.toc-txt {
  flex: 1 1 auto;
  min-width: 0;
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  opacity: 0;
  transition: opacity var(--duration-base) var(--ease-out);
}
.toc-item.active .toc-txt {
  color: var(--color-accent);
  font-weight: var(--weight-medium);
}
.toc-rail:hover .toc-txt {
  opacity: 1;
}
.toc-bar {
  width: 8px;
  height: 2px;
  flex: none;
  border-radius: var(--radius-full);
  background: var(--color-text-faint);
  transition: width var(--duration-fast) var(--ease-out), height var(--duration-fast) var(--ease-out),
    background var(--duration-fast) var(--ease-out);
}
.toc-item:hover .toc-bar {
  background: var(--color-text-tertiary);
}
.toc-item.active .toc-bar {
  width: 12px;
  height: 3px;
  background: var(--color-accent);
}
/* <720px 窄屏隐藏（REQ-044；useMediaQuery 已驱动 v-if，此处为 CSS 双保险） */
@media (max-width: 720px) {
  .toc-rail {
    display: none;
  }
}
</style>
