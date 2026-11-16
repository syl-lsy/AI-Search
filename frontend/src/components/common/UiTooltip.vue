<script setup lang="ts">
/**
 * UiTooltip — 自定义 tooltip 深色气泡（frontend-api.md §5 v1.23；Perplexity 图标提示形态对标：
 * 主标题粗白 + 可选灰副题两行，圆角近黑底，触发钮下方居中，非原生 title）。
 *
 * 用法：<UiTooltip label="搜索" sub="搜索会话"><button .../></UiTooltip>——默认插槽为触发钮；
 * 语义由触发钮既有 aria-label 承载（气泡 aria-hidden 纯视觉），接入面须移除原 title 属性。
 * 行为：hover/focus 双通道进入，300ms 延迟出现（防误触）、离开/ focusout/Esc 即隐（清除
 * pending 计时器）；定位 = Teleport 到 body + fixed，触发钮下方居中、贴边时左右钳制
 * （简化口径：一律下方，不上下翻转）；纯 CSS 定位零第三方依赖。
 * 消费面（替换既有 title 的全量接入点）见 frontend-api.md §5 UiTooltip 行 v1.23 注。
 */
import { nextTick, onUnmounted, ref, watch } from 'vue'

/** hover 显示延迟（Perplexity 实测 ~300ms 防误触） */
const SHOW_DELAY_MS = 300
/** 气泡距视口边缘最小间距（贴边钳制用） */
const EDGE_GAP = 8
/** 气泡与触发钮下缘的间距 */
const BUBBLE_GAP = 6

withDefaults(
  defineProps<{
    /** 主标题（粗白，一行） */
    label: string
    /** 可选灰副题（第二行；缺省单行） */
    sub?: string
  }>(),
  { sub: undefined },
)

const rootEl = ref<HTMLElement | null>(null)
const bubbleEl = ref<HTMLElement | null>(null)
const visible = ref(false)
const pos = ref({ top: 0, left: 0 })

let showTimer: ReturnType<typeof setTimeout> | null = null

function clearTimer(): void {
  if (showTimer !== null) {
    clearTimeout(showTimer)
    showTimer = null
  }
}

function show(): void {
  if (visible.value || showTimer !== null) return
  showTimer = setTimeout(() => {
    showTimer = null
    void placeAndReveal()
  }, SHOW_DELAY_MS)
}

function hide(): void {
  clearTimer()
  visible.value = false
}

/** 定位后显现：先按锚点矩形摆「下方居中」，气泡挂载后按实测宽度左右钳制进视口 */
async function placeAndReveal(): Promise<void> {
  const anchor = (rootEl.value?.firstElementChild as HTMLElement | null) ?? rootEl.value
  if (!anchor) return
  const rect = anchor.getBoundingClientRect()
  const center = rect.left + rect.width / 2
  pos.value = { top: rect.bottom + BUBBLE_GAP, left: center }
  visible.value = true
  await nextTick()
  const bubble = bubbleEl.value
  if (!bubble || typeof bubble.offsetWidth !== 'number' || bubble.offsetWidth === 0) return
  const half = bubble.offsetWidth / 2
  const min = EDGE_GAP + half
  const max = Math.max(min, window.innerWidth - EDGE_GAP - half)
  pos.value = { top: rect.bottom + BUBBLE_GAP, left: Math.min(Math.max(center, min), max) }
}

function onDocKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape') hide()
}

watch(visible, (v) => {
  if (v) document.addEventListener('keydown', onDocKeydown)
  else document.removeEventListener('keydown', onDocKeydown)
})

onUnmounted(() => {
  clearTimer()
  document.removeEventListener('keydown', onDocKeydown)
})
</script>

<template>
  <span
    ref="rootEl"
    class="ui-tip"
    data-testid="ui-tip"
    @mouseenter="show"
    @mouseleave="hide"
    @focusin="show"
    @focusout="hide"
  >
    <slot />
    <Teleport to="body">
      <div
        v-if="visible"
        ref="bubbleEl"
        class="ui-tip-bubble"
        aria-hidden="true"
        data-testid="ui-tip-bubble"
        :style="{ top: `${pos.top}px`, left: `${pos.left}px` }"
      >
        <span class="ui-tip-label">{{ label }}</span>
        <span v-if="sub" class="ui-tip-sub">{{ sub }}</span>
      </div>
    </Teleport>
  </span>
</template>

<style scoped>
.ui-tip {
  display: inline-flex;
  min-width: 0;
}
/* 禁用态子元素不吃指针事件——hover 事件穿透到包装层（M4 前附件钮等 disabled 占位也有提示） */
.ui-tip :deep(button:disabled) {
  pointer-events: none;
}
/* 深色气泡：--color-tooltip-bg/-text 为主题不变量（两主题恒近黑底浅字，tokens.css v1.4） */
.ui-tip-bubble {
  position: fixed;
  z-index: 70;
  transform: translateX(-50%);
  display: grid;
  gap: 2px;
  max-width: 260px;
  padding: 6px 10px;
  border-radius: var(--radius-md);
  background: var(--color-tooltip-bg);
  color: var(--color-tooltip-text);
  box-shadow: var(--shadow-card);
  pointer-events: none;
  white-space: nowrap;
}
.ui-tip-label {
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  overflow: hidden;
  text-overflow: ellipsis;
}
/* 副题：同色降透明度（灰副题观感，不引第三方色值） */
.ui-tip-sub {
  font-size: var(--text-xs);
  opacity: 0.72;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
