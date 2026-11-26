<script setup lang="ts">
/**
 * KbDocPagination — Library 文档表页码分页控件（R-3 用户裁决：可选页码分页，替代触底无限滚动）。
 *
 * 形态：「« ‹ 1 … 4 5 6 … 21 › »」——首末页恒显 + 当前页 ±1 窗口 + 省略号（页码间距 >1 折叠）；
 * « ‹ › » = 首/上/下/末钮；总页数文本=共 {n} 页。样式严格 tokens（按钮形态沿用 .btn 圆角/
 * 色板变量，组件内零字面量色值）；a11y：nav aria-label=分页导航、页码钮 aria-current="page" +
 * 逐钮 aria-label（第一页/上一页/第 {n} 页/下一页/最后一页）、边界钮与加载中（disabled prop）
 * 一律 disabled。交互归 store.goToPage（父级 @change 接线——组件零 store 依赖，纯投影）。
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps<{
  page: number
  totalPages: number
  disabled?: boolean
}>()

const emit = defineEmits<{ change: [page: number] }>()

const { t } = useI18n()

/** 页码窗口投影：totalPages≤7 全显无省略号；否则首末恒显 + 当前 ±1（贴边界侧补足 3 连页），
 *  相邻间距 >1 折叠 '…' */
function pageWindow(page: number, totalPages: number): Array<number | 'ellipsis'> {
  if (totalPages <= 7) {
    return Array.from({ length: totalPages }, (_, i) => i + 1)
  }
  const picked = new Set<number>([1, totalPages])
  for (const p of [page - 1, page, page + 1]) {
    if (p >= 1 && p <= totalPages) picked.add(p)
  }
  // 贴边界补足：当前页贴近首/末时该侧窗口保持 3 连页（1 2 3 … / … 19 20 21）
  if (page <= 2) picked.add(3)
  if (page >= totalPages - 1) picked.add(totalPages - 2)
  const sorted = [...picked].sort((a, b) => a - b)
  const out: Array<number | 'ellipsis'> = []
  let prev = 0
  for (const p of sorted) {
    if (p - prev > 1) out.push('ellipsis')
    out.push(p)
    prev = p
  }
  return out
}

const items = computed(() => pageWindow(props.page, props.totalPages))

/** 翻页守卫：disabled/同页/越界静默（« ‹ › » 边界由 disabled 承载，页码窗口恒在界内） */
function go(target: number): void {
  if (props.disabled || target < 1 || target > props.totalPages || target === props.page) return
  emit('change', target)
}
</script>

<template>
  <nav class="kb-pager" :aria-label="t('library.paginationNav')" data-testid="kb-doc-pagination">
    <span class="kb-pager-total">{{ t('library.paginationTotal', { n: totalPages }) }}</span>
    <button
      type="button"
      class="pager-btn"
      :disabled="disabled || page <= 1"
      :aria-label="t('library.paginationFirst')"
      data-testid="kb-pager-first"
      @click="go(1)"
    >«</button>
    <button
      type="button"
      class="pager-btn"
      :disabled="disabled || page <= 1"
      :aria-label="t('library.paginationPrev')"
      data-testid="kb-pager-prev"
      @click="go(page - 1)"
    >‹</button>
    <template v-for="(item, i) in items" :key="`${item}-${i}`">
      <span v-if="item === 'ellipsis'" class="pager-ellipsis" aria-hidden="true">…</span>
      <button
        v-else
        type="button"
        class="pager-btn pager-num"
        :class="{ cur: item === page }"
        :disabled="disabled || item === page"
        :aria-current="item === page ? 'page' : undefined"
        :aria-label="t('library.paginationPage', { n: item })"
        :data-testid="`kb-pager-page-${item}`"
        @click="go(item)"
      >{{ item }}</button>
    </template>
    <button
      type="button"
      class="pager-btn"
      :disabled="disabled || page >= totalPages"
      :aria-label="t('library.paginationNext')"
      data-testid="kb-pager-next"
      @click="go(page + 1)"
    >›</button>
    <button
      type="button"
      class="pager-btn"
      :disabled="disabled || page >= totalPages"
      :aria-label="t('library.paginationLast')"
      data-testid="kb-pager-last"
      @click="go(totalPages)"
    >»</button>
  </nav>
</template>

<style scoped>
.kb-pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  flex-wrap: wrap;
  margin-top: var(--space-4);
}
.kb-pager-total {
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
  margin-right: var(--space-2);
  font-variant-numeric: tabular-nums;
}
/* 页码/方向钮：圆角 pill + tokens 色板（形态沿用 .btn 基类尺度，尺寸收窄为分页控件专用） */
.pager-btn {
  min-width: 32px;
  height: 32px;
  padding: 0 var(--space-2);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-full);
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  font-variant-numeric: tabular-nums;
  transition: background var(--duration-fast) var(--ease-out), color var(--duration-fast) var(--ease-out),
    border-color var(--duration-fast) var(--ease-out);
}
.pager-btn:hover:not(:disabled) {
  background: var(--color-surface-hover);
  color: var(--color-text-primary);
}
.pager-btn.cur {
  background: var(--color-accent-bg);
  color: var(--color-accent);
  border-color: var(--color-accent-border);
  font-weight: var(--weight-medium);
}
.pager-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.pager-ellipsis {
  color: var(--color-text-faint);
  font-size: var(--text-sm);
  user-select: none;
}
</style>
