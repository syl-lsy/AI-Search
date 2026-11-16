<script setup lang="ts">
/**
 * FollowupChips — 后续提问建议行列表（REQ-046/A29 前端面落位，frontend-api.md §5 v1.9 行）。
 *
 * 形态（设计说明 §7，Perplexity 2026-11-03 实测）：纵向行列表——无区头、顶部细分隔线、
 * 每行 = 放大镜图标（#i-search）+ 单行短语式追问；hover 浅底、整行可点；默认透明静默。
 * 展示条件 = followups 非空（开关关闭/生成失败/超时/deep 轮自然不出块——无空态、无错误提示）。
 * 数据 = turn.followups（SSE followups 事件 + 2.5 对账双源，stores/turn.ts）。
 * 行为 = 点击 chip 以该文本为 query 走既有追问流（emit('follow', text) → AnswerView
 * store.followUp；提交体 = chip 文本，REQ-017/REQ-042 照常适用）。
 * 防御性兜底（非重复校验契约——后端 schema_guard 已校验单条非空且 ≤200 字符，api.md §7.2）：
 * 渲染前过滤空串/超长条目、≤5 条截断；文本按原文纯文本渲染不转 markdown。
 * a11y（硬性超越 Perplexity 裸 div）：每行 button + aria-label（完整建议文本）+ Tab 可达；
 * 容器 aria-label = i18n answer.followups。
 */
import { computed } from 'vue'

import UiIcon from '@/components/icons/UiIcon.vue'

const props = defineProps<{
  items: string[]
}>()

const emit = defineEmits<{
  follow: [question: string]
}>()

/** 契约上限（api.md §7.2：items 长度 1~5、单条 ≤200 字符——前端防御性双保险） */
const MAX_ITEMS = 5
const MAX_ITEM_LENGTH = 200

const visibleItems = computed(() =>
  props.items
    .map((item) => item.trim())
    .filter((item) => item.length > 0 && item.length <= MAX_ITEM_LENGTH)
    .slice(0, MAX_ITEMS),
)
</script>

<template>
  <section
    v-if="visibleItems.length > 0"
    class="fu-sec"
    data-testid="followup-chips"
    :aria-label="$t('answer.followups')"
  >
    <button
      v-for="(item, i) in visibleItems"
      :key="`${i}-${item}`"
      type="button"
      class="fu-item"
      :aria-label="item"
      @click="emit('follow', item)"
    >
      <UiIcon name="i-search" class="fu-ico" size="sm" />
      <span class="fu-text">{{ item }}</span>
    </button>
  </section>
</template>

<style scoped>
.fu-sec {
  display: flex;
  flex-direction: column;
  row-gap: 1px; /* 页面级局部值（设计说明 §7 实测口径，间距标尺无 1px 档） */
  border-top: 1px solid var(--color-border-faint); /* 顶部细分隔线（无区头） */
  padding-top: var(--space-5);
  margin: var(--space-2) 0 var(--space-6);
}
.fu-item {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-height: 44px; /* ≥ --hit-min 触控标准（设计说明 §7.4） */
  padding: var(--space-3) var(--space-3);
  margin-inline: calc(var(--space-3) * -1); /* hover 高亮块外扩 -12px（<720px 收敛由页面级媒体查询） */
  border-radius: var(--radius-lg);
  color: var(--color-text-primary);
  font-size: var(--text-md);
  font-weight: var(--weight-regular);
  line-height: 24px; /* 页面级局部值（§5.10 实测精度） */
  text-align: left;
  transition: background var(--duration-fast) var(--ease-out);
}
.fu-item:hover {
  background: var(--color-surface-ghost);
}
.fu-item:hover .fu-ico {
  color: var(--color-text-secondary);
}
.fu-ico {
  flex: none;
  color: var(--color-text-tertiary);
  transition: color var(--duration-fast) var(--ease-out);
}
.fu-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
@media (max-width: 720px) {
  .fu-item {
    margin-inline: calc(var(--space-2) * -1); /* <720px 外扩收敛 -8px（§7.5） */
  }
}
</style>
