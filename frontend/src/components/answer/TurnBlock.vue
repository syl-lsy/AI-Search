<script setup lang="ts">
/**
 * TurnBlock — 单轮对话块（bubble + 模式徽标 + 状态行 + Timeline + thinking + images + 答案渲染）。
 * 组合件（vue-best-practices：feature 拆分），数据 = turnStore 行投影（TurnState）。
 * 锚点 id 约定：问题块 `turn-{id}-q`、快答容器 `turn-{id}-a`、深度报告标题 id 由 AnswerRenderer 生成。
 */
import { computed } from 'vue'

import UiIcon from '@/components/icons/UiIcon.vue'
import AnswerRenderer from '@/components/markdown/AnswerRenderer.vue'
import ThinkingPanel from '@/components/markdown/ThinkingPanel.vue'
import ImageCardRow from '@/components/answer/ImageCardRow.vue'
import Timeline from '@/components/answer/Timeline.vue'
import type { HeadingInfo } from '@/components/markdown/renderer'
import type { TurnState } from '@/stores/turn'

const props = defineProps<{ turn: TurnState; compactNote?: boolean }>()

const emit = defineEmits<{
  headings: [turnId: string, items: HeadingInfo[]]
  cite: [refNo: number]
}>()

const isLive = computed(() => props.turn.status === 'running' || props.turn.status === 'queued')
const doneSeconds = computed(() =>
  props.turn.durationMs !== null && props.turn.durationMs > 0
    ? Math.max(1, Math.round(props.turn.durationMs / 1000))
    : null,
)
/** 降级终态（done finish=degraded / 2.5 degraded 双源）：与 completed 严格区分（§3 done 行） */
const isDegraded = computed(() => props.turn.degradedReason !== null)
const qId = computed(() => `turn-${props.turn.turnId}-q`)
const aId = computed(() => `turn-${props.turn.turnId}-a`)
const headingPrefix = computed(() => props.turn.turnId.slice(0, 8))

function onHeadings(items: HeadingInfo[]): void {
  emit('headings', props.turn.turnId, items)
}
</script>

<template>
  <article class="turn-block" data-testid="turn-block">
    <div :id="qId" class="bubble-row" data-testid="turn-question">
      <span class="badge" :class="turn.mode === 'deep' ? 'badge-outline' : 'badge-accent'">
        <UiIcon :name="turn.mode === 'deep' ? 'i-wave' : 'i-zap'" size="sm" />
        {{ turn.mode === 'deep' ? $t('mode.deep') : $t('mode.fast') }}
      </span>
      <div class="bubble">{{ turn.question }}</div>
    </div>

    <div class="status-row" data-testid="turn-status">
      <span
        class="dot"
        :class="
          isLive
            ? 'dot-accent'
            : turn.status === 'failed'
              ? 'dot-danger'
              : isDegraded
                ? 'dot-warning'
                : 'dot-success'
        "
      ></span>
      <template v-if="isLive">{{ $t('answer.statusRunning') }}</template>
      <template v-else-if="isDegraded">
        {{ $t('answer.statusDegraded') }}<template v-if="doneSeconds !== null"> <b>{{ doneSeconds }}s</b></template>
      </template>
      <template v-else-if="doneSeconds !== null">
        {{ $t('answer.statusDone') }} <b>{{ doneSeconds }}s</b>
      </template>
      <template v-else>{{ $t('answer.statusDone') }}</template>
      <template v-if="turn.sources.length > 0">· {{ turn.sources.length }} {{ $t('answer.sourceCount') }}</template>
      <template v-if="turn.errorMsg">· {{ turn.errorMsg }}</template>
    </div>

    <Timeline :stages="turn.stages" :status="turn.status" :show-compact-note="compactNote === true" />

    <ThinkingPanel :markdown="turn.thinking_md" :streaming="turn.status === 'running'" />

    <ImageCardRow :images="turn.images" />

    <!-- DegradedBanner（frontend-api.md §3 done 行 / §5 组件清单；api.md §2.5：研究未完成显著标注）：
         数据源 turn.degradedReason（SSE done + 2.5 对账双源，stores/turn.ts），reason 三枚举分立文案 -->
    <div v-if="isDegraded" class="degraded-banner" role="status" data-testid="degraded-banner">
      <UiIcon name="i-alert" size="sm" class="degraded-ico" />
      <span class="degraded-main">{{ $t('answer.degradedBanner') }}</span>
      <span class="degraded-reason">{{ $t(`answer.degradedReason.${turn.degradedReason}`) }}</span>
    </div>

    <div v-if="turn.answer_md" :id="aId" data-testid="turn-answer">
      <AnswerRenderer
        :markdown="turn.answer_md"
        :heading-prefix="headingPrefix"
        @headings="onHeadings"
        @cite="(n) => emit('cite', n)"
      />
    </div>
  </article>
</template>

<style scoped>
.turn-block {
  margin-bottom: var(--space-10);
}
.bubble-row {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-6);
}
.bubble {
  max-width: 72%;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--space-3) var(--space-4);
  font-size: var(--text-md);
  line-height: var(--text-md--lh);
}
.status-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
  margin-bottom: var(--space-5);
}
/* DegradedBanner（降级终态显著标注；tokens warning 语义色，§8.6 不自创样式） */
.degraded-banner {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  background: var(--color-warning-bg);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  padding: var(--space-2) var(--space-3);
  margin-bottom: var(--space-4);
  font-size: var(--text-sm);
}
.degraded-banner .degraded-ico {
  color: var(--color-warning);
}
.degraded-banner .degraded-main {
  color: var(--color-warning);
  font-weight: var(--weight-medium);
}
.degraded-banner .degraded-reason {
  color: var(--color-text-tertiary);
}
</style>
