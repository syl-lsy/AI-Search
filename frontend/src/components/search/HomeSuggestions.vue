<script setup lang="ts">
/**
 * HomeSuggestions — 首页输入推荐面板（REQ-047 / frontend-api.md §4 suggestPool；修复项⑦，
 * 对标图 3：输入时出现「热门池 / 历史匹配」下拉）。
 *
 * - 数据由父级（HomeView）投影：query<2 → hotPool（Nacos 推荐池前 6）；query≥2（250ms 防抖）
 *   → history（threadListStore.items 标题包含匹配，前 5）；两列表互斥传入；
 * - 形态：SearchCard 卡内 suggestions 插槽落位——绝对定位于输入卡正下方、与卡同宽浮动面板；
 *   行式列表 icon + 文本单行省略（热门 i-search / 历史 i-clock）；
 * - a11y：role=listbox（aria-label=home.suggestLabel）+ 条目 button role=option + aria-selected
 *   （无键盘选中态，恒 false 占位）+ 空态 role=status（home.suggestEmpty）；
 * - 选择 → emit('select', text)：父级复用 submit()（复用 HomeView 提交链路，不另起请求通道）。
 */
import UiIcon from '@/components/icons/UiIcon.vue'

const props = defineProps<{
  /** 热门池（Nacos home.suggest_pool 投影，父级截前 6） */
  hotPool: string[]
  /** 历史匹配（threadListStore 标题包含匹配，父级截前 5） */
  history: string[]
  /** 面板展开态（父级 focus/blur/escape 驱动） */
  open: boolean
}>()

const emit = defineEmits<{
  select: [text: string]
}>()

const hasItems = (): boolean => props.hotPool.length > 0 || props.history.length > 0
</script>

<template>
  <div v-if="open" class="suggest-panel" data-testid="home-suggestions" role="listbox" :aria-label="$t('home.suggestLabel')">
    <template v-if="hasItems()">
      <button
        v-for="text in hotPool"
        :key="`hot-${text}`"
        type="button"
        class="suggest-row"
        role="option"
        aria-selected="false"
        data-testid="suggest-item"
        @click="emit('select', text)"
      >
        <UiIcon name="i-search" size="sm" class="row-ico" />
        <span class="row-text">{{ text }}</span>
      </button>
      <button
        v-for="text in history"
        :key="`his-${text}`"
        type="button"
        class="suggest-row"
        role="option"
        aria-selected="false"
        data-testid="suggest-item"
        @click="emit('select', text)"
      >
        <UiIcon name="i-clock" size="sm" class="row-ico" />
        <span class="row-text">{{ text }}</span>
      </button>
    </template>
    <div v-else class="suggest-empty" role="status">{{ $t('home.suggestEmpty') }}</div>
  </div>
</template>

<style scoped>
/* 输入卡正下方、与卡同宽的浮动面板（包含块 = .search-card 的 position:relative） */
.suggest-panel {
  position: absolute;
  left: 0;
  right: 0;
  top: calc(100% + var(--space-2));
  z-index: 30;
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-floating);
  overflow: hidden;
  padding: var(--space-1) 0;
}
.suggest-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  width: 100%;
  min-height: 36px;
  padding: var(--space-1) var(--space-4);
  text-align: left;
  color: var(--color-text-primary);
  font-size: var(--text-base);
  transition: background var(--duration-fast) var(--ease-out);
}
.suggest-row:hover {
  background: var(--color-surface-ghost);
}
.suggest-row .row-ico {
  color: var(--color-text-tertiary);
  flex: none;
}
.suggest-row .row-text {
  flex: 1 1 auto;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.suggest-empty {
  padding: var(--space-4);
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
  text-align: center;
}
</style>
