<script setup lang="ts">
/**
 * ThinkingPanel — 思考流折叠区（frontend-api.md §3 thinking 行 / §5 AnswerRenderer 行）。
 *
 * - thinking_md 默认折叠渲染、可展开；markdown 通道 = renderPlainMarkdown（不解析 [n]、
 *   不产标题 id，§3：不参与 [n] 引用解析）；
 * - 空内容不渲染（无空区块原则）；回看态不渲染（thinking_md 为流式域，对账不恢复）——
 *   由父级以「仅流式轮渲染」保证，组件内再以空内容兜底；
 * - 流式进行中展开态末尾带打字光标（tokens .caret）；
 * - 渲染节奏（streaming-buffer 规则 14/17）：markdown 源经 70ms 限频批驱动重解析；
 *   折叠态（v-show 隐藏）跳过重渲——挂载时初渲一次（DOM 恒存在，v-show 仅隐藏），
 *   折叠期间的源变更不合入渲染，展开时统一按最新源渲染（评审留档 P2）。
 */
import { ref, shallowRef, watch } from 'vue'

import { renderPlainMarkdown } from '@/components/markdown/renderer'
import type { RenderedMarkdown } from '@/components/markdown/renderer'
import { useThrottledSource } from '@/components/markdown/renderThrottle'
import UiIcon from '@/components/icons/UiIcon.vue'

const props = defineProps<{
  markdown: string
  streaming: boolean
}>()

const open = ref(false)

// 初渲一次（折叠态 DOM 恒存在；后续渲染由下方 watch 驱动）
const rendered = shallowRef<RenderedMarkdown>(renderPlainMarkdown(props.markdown))
const throttledMarkdown = useThrottledSource(() => props.markdown)
watch([throttledMarkdown, open], ([source, isOpen]) => {
  if (!isOpen) return // 折叠态跳过重渲（不可见不烧渲染；展开时按最新源统一渲染）
  rendered.value = renderPlainMarkdown(source)
})
const bodyId = `thinking-body-${Math.random().toString(36).slice(2, 8)}`
</script>

<template>
  <section
    v-if="markdown.length > 0"
    class="thinking"
    data-testid="thinking-panel"
  >
    <button
      type="button"
      class="thinking-head"
      :aria-expanded="open ? 'true' : 'false'"
      :aria-controls="bodyId"
      data-testid="thinking-toggle"
      @click="open = !open"
    >
      <UiIcon name="i-chev-d" size="sm" class="chev" />
      {{ $t('answer.thinking') }}
      <span v-if="streaming" class="caret" aria-hidden="true"></span>
    </button>
    <div v-show="open" :id="bodyId" class="thinking-body" data-testid="thinking-body">
      <div class="thinking-md" v-html="rendered.html" />
    </div>
  </section>
</template>

<style scoped>
.thinking {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-primary);
  margin-bottom: var(--space-5);
}
.thinking-head {
  width: 100%;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
}
.thinking-head:hover {
  color: var(--color-text-primary);
}
.thinking-head .chev {
  transition: transform var(--duration-base) var(--ease-out);
}
.thinking-head[aria-expanded='true'] .chev {
  transform: rotate(180deg);
}
.thinking-body {
  padding: var(--space-2) var(--space-4) var(--space-4);
  border-top: 1px solid var(--color-border-faint);
}
.thinking-md {
  font-size: var(--text-sm);
  line-height: 1.6;
  color: var(--color-text-tertiary);
  max-height: 280px;
  overflow-y: auto;
}
.thinking-md :deep(p) {
  margin: 0 0 var(--space-2);
}
</style>
