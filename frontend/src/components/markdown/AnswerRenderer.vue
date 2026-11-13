<script setup lang="ts">
/**
 * AnswerRenderer — answer_md 渲染组件（frontend-api.md §5 AnswerRenderer 行）。
 *
 * - markdown-it + DOMPurify（白名单 + §2 http/https 协议白名单）+ highlight.js（renderer.ts）；
 * - [n] 引用标注 → 可点击 sup.cite，点击上抛 cite(refNo) 与 sources 面板 ref_no 联动（REQ-003）；
 * - 标题 id 由渲染器生成（renderer.ts postProcess），headings 事件上抛 = OutlineAnchor 数据源
 *   （frontend-api.md §5：数据源复用 AnswerRenderer 标题 id 机制，SSE/API 协议零变更）；
 * - thinking 折叠区（ThinkingPanel）是本组件上方独立区块（§5），不在本组件内部；
 * - 渲染节奏（streaming-buffer 规则 14/17）：markdown 源经 70ms 限频批驱动重解析——流式期间
 *   「每帧全量重解析」收敛为「≤70ms 一批」，窗口内合批尾帧取最新值（终值不丢）；
 *   headingPrefix 变更不经限频（computed 直连 props，立即生效——多轮去重命名空间静态不变）。
 */
import { computed, watch } from 'vue'

import { renderAnswerMarkdown } from '@/components/markdown/renderer'
import type { HeadingInfo } from '@/components/markdown/renderer'
import { useThrottledSource } from '@/components/markdown/renderThrottle'

const props = defineProps<{
  markdown: string
  /** 标题 id 命名空间（多轮同页去重——如 turn id 短前缀） */
  headingPrefix: string
}>()

const emit = defineEmits<{
  headings: [items: HeadingInfo[]]
  cite: [refNo: number]
}>()

const throttledMarkdown = useThrottledSource(() => props.markdown)
const rendered = computed(() => renderAnswerMarkdown(throttledMarkdown.value, { headingPrefix: props.headingPrefix }))

// watch→emit 属合法副作用通道（computed 内禁止副作用）；headings 供父级装配 OutlineAnchor 条目
watch(
  rendered,
  (r) => {
    emit('headings', r.headings)
  },
  { immediate: true },
)

function refNoFrom(target: EventTarget | null): number | null {
  const el = target instanceof Element ? target.closest('sup.cite[data-ref-no]') : null
  if (!el) return null
  const n = Number(el.getAttribute('data-ref-no'))
  return Number.isFinite(n) && n > 0 ? n : null
}

function onBodyClick(e: MouseEvent): void {
  const refNo = refNoFrom(e.target)
  if (refNo !== null) emit('cite', refNo)
}

/** 键盘可达（sup.cite 携带 tabindex=0 + role=button）：Enter/空格激活，与点击等价 */
function onBodyKeydown(e: KeyboardEvent): void {
  if (e.key !== 'Enter' && e.key !== ' ') return
  const refNo = refNoFrom(e.target)
  if (refNo !== null) {
    e.preventDefault()
    emit('cite', refNo)
  }
}
</script>

<template>
  <div
    class="answer-md"
    data-testid="answer-renderer"
    @click="onBodyClick"
    @keydown="onBodyKeydown"
    v-html="rendered.html"
  />
</template>

<style scoped>
/* 答案排版基线（docs/design/02-answer.html 页面级样式 + tokens 变量） */
.answer-md {
  min-width: 0;
}
.answer-md :deep(p) {
  font-size: var(--text-md);
  line-height: var(--text-md--lh);
  color: var(--color-text-primary);
  margin-bottom: var(--space-4);
}
.answer-md :deep(> p:first-of-type) {
  /* 粗体结论首句（设计稿 .lead 语义） */
  font-size: var(--text-lg);
  line-height: 1.75;
  font-weight: var(--weight-semibold);
}
.answer-md :deep(h2) {
  font-size: var(--text-xl);
  line-height: var(--text-xl--lh);
  font-weight: var(--weight-semibold);
  margin: var(--space-8) 0 var(--space-4);
}
.answer-md :deep(h1),
.answer-md :deep(h3),
.answer-md :deep(h4) {
  font-weight: var(--weight-semibold);
  margin: var(--space-6) 0 var(--space-3);
}
.answer-md :deep(ul),
.answer-md :deep(ol) {
  margin: 0 0 var(--space-4) var(--space-6);
}
.answer-md :deep(li) {
  font-size: var(--text-md);
  line-height: var(--text-md--lh);
  margin-bottom: var(--space-1);
}
.answer-md :deep(a) {
  color: var(--color-accent);
}
.answer-md :deep(a:hover) {
  color: var(--color-accent-hover);
}
.answer-md :deep(blockquote) {
  border-left: 2px solid var(--color-border-strong);
  padding-left: var(--space-3);
  color: var(--color-text-secondary);
  margin: 0 0 var(--space-4);
}
.answer-md :deep(code) {
  font-family: var(--font-mono);
  font-size: 13px;
  background: var(--color-surface-active);
  border: 1px solid var(--color-border);
  border-radius: 4px;
  padding: 1px 6px;
}
.answer-md :deep(pre) {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  padding: var(--space-3) var(--space-4);
  overflow-x: auto;
  margin-bottom: var(--space-4);
}
.answer-md :deep(pre code) {
  background: none;
  border: 0;
  padding: 0;
}
.answer-md :deep(table) {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-sm);
}
.answer-md :deep(th) {
  text-align: left;
  font-weight: var(--weight-medium);
  color: var(--color-text-secondary);
  background: var(--color-surface-active);
  padding: 10px 14px;
  border-bottom: 1px solid var(--color-border);
  white-space: nowrap;
}
.answer-md :deep(td) {
  padding: 10px 14px;
  border-bottom: 1px solid var(--color-border-faint);
  vertical-align: top;
}
.answer-md :deep(img) {
  max-width: 100%;
  border-radius: var(--radius-md);
}
/* 内联引用角标（设计稿 sup.cite：青蓝淡底，点击定位来源卡 REQ-003） */
.answer-md :deep(sup.cite) {
  font-size: 11px;
  line-height: 1;
  color: var(--color-accent);
  background: var(--color-accent-bg);
  border-radius: 4px;
  padding: 2px 4px;
  margin: 0 2px;
  cursor: pointer;
  user-select: none;
}
.answer-md :deep(sup.cite:hover) {
  background: var(--color-accent-border);
}
</style>
