<script setup lang="ts">
/**
 * Timeline — 步骤时间线（frontend-api.md §5 Timeline 行；docs/design/02-answer.html 视觉）。
 *
 * - SSE stage 状态机投影（stages: pending/running/done/failed + progress + subagents pills）；
 * - 排队轮占位节点（v1.5：status=queued 驱动，非 stage 事件——api.md 不为排队发事件）；
 * - 「正在整理研究上下文」瞬态提示（v1.4 context_compact——showCompactNote 由父级 5s 计时驱动）；
 * - 生成中默认展开、终态默认折叠（用户可切换）。
 */
import { computed, ref, watch } from 'vue'

import UiIcon from '@/components/icons/UiIcon.vue'
import type { TimelineStage } from '@/stores/turn'
import type { TurnStatus } from '@/types/api'

const props = defineProps<{
  stages: TimelineStage[]
  status: TurnStatus
  showCompactNote: boolean
}>()

const RUNNING_STATUSES: readonly TurnStatus[] = ['running', 'queued']

/** 生成中默认展开、终态默认折叠；用户手动切换后跟随用户选择 */
const open = ref(RUNNING_STATUSES.includes(props.status))
watch(
  () => props.status,
  (s, prev) => {
    // 终态自动折叠；新进入 running/queued 自动展开（用户手动态让位于状态迁移）
    if (RUNNING_STATUSES.includes(s)) open.value = true
    else if (prev !== undefined && RUNNING_STATUSES.includes(prev)) open.value = false
  },
)

const doneCount = computed(() => props.stages.filter((s) => s.status === 'done').length)
const totalSteps = computed(() => props.stages.length)
const hasSteps = computed(() => props.stages.length > 0 || props.status === 'queued')
const bodyId = `tl-body-${Math.random().toString(36).slice(2, 8)}`
</script>

<template>
  <div v-if="hasSteps" class="timeline" data-testid="timeline">
    <button
      type="button"
      class="tl-head"
      :class="{ open }"
      :aria-expanded="open ? 'true' : 'false'"
      :aria-controls="bodyId"
      @click="open = !open"
    >
      <UiIcon name="i-chev-d" size="sm" class="chev" />
      {{ $t('answer.researchProcess') }}
      <template v-if="status === 'queued'">· {{ $t('turn.queued') }}</template>
      <template v-else-if="totalSteps > 0">· {{ doneCount }}/{{ totalSteps }}</template>
      <span v-if="showCompactNote" class="tl-compact" data-testid="compact-note">
        <span class="dot dot-accent" style="width: 6px; height: 6px"></span>
        {{ $t('timeline.compacting') }}
      </span>
    </button>
    <div v-show="open" :id="bodyId" class="tl-body">
      <div v-if="status === 'queued'" class="tl-step pending" data-testid="queued-placeholder">
        <div class="tl-ico"><span class="ring">…</span></div>
        <div class="tl-main"><div class="tl-label">{{ $t('turn.queued') }}</div></div>
      </div>
      <div
        v-for="(step, i) in stages"
        :key="step.stage_key"
        class="tl-step"
        :class="step.status"
      >
        <div class="tl-ico" :class="step.status">
          <span class="ring">
            <UiIcon v-if="step.status === 'done'" name="i-check" size="sm" />
            <template v-else-if="step.status === 'running'">●</template>
            <template v-else-if="step.status === 'failed'">!</template>
            <template v-else>{{ i + 1 }}</template>
          </span>
        </div>
        <div class="tl-main">
          <div class="tl-label">
            {{ step.label ?? step.stage_key }}
            <span v-if="step.status === 'running'" class="caret" aria-hidden="true"></span>
          </div>
          <div v-if="step.progress" class="tl-sub">{{ step.progress.current }}/{{ step.progress.total ?? '…' }}</div>
          <div v-if="step.extra?.subagents?.length" class="tl-sub">
            <span class="agent-pills">
              <span
                v-for="a in step.extra.subagents"
                :key="a.subtopic"
                class="ap"
                :class="a.status === 'done' ? 'ok' : 'run'"
              >{{ a.subtopic }} {{ a.status === 'done' ? '✓' : '●' }}</span>
            </span>
          </div>
          <div v-if="step.detail" class="tl-sub">{{ step.detail }}</div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.timeline {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-primary);
  margin-bottom: var(--space-6);
}
.tl-head {
  width: 100%;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
}
.tl-head:hover {
  color: var(--color-text-primary);
}
.tl-head .chev {
  transition: transform var(--duration-base) var(--ease-out);
}
.tl-head.open .chev {
  transform: rotate(180deg);
}
.tl-compact {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  margin-left: var(--space-2);
  color: var(--color-accent);
}
.tl-body {
  display: block;
  padding: var(--space-2) var(--space-4) var(--space-4);
}
.tl-step {
  position: relative;
  display: flex;
  gap: var(--space-3);
  padding: var(--space-2) 0;
}
.tl-step::before {
  content: '';
  position: absolute;
  left: 11px;
  top: 26px;
  bottom: -4px;
  width: 1px;
  background: var(--color-border);
}
.tl-step:last-child::before {
  display: none;
}
.tl-ico {
  width: 24px;
  height: 24px;
  flex: none;
  display: grid;
  place-items: center;
  z-index: 1;
  background: var(--color-bg-primary);
}
.tl-ico .ring {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  border: 1.5px solid var(--color-border-strong);
  color: var(--color-text-tertiary);
  font-size: var(--text-xs);
}
.tl-ico.done .ring {
  border-color: var(--color-success);
  color: var(--color-success);
}
.tl-ico.run .ring,
.tl-ico.running .ring {
  border-color: var(--color-accent);
  color: var(--color-accent);
  animation: pulse 1.4s ease-in-out infinite;
}
.tl-ico.failed .ring {
  border-color: var(--color-danger);
  color: var(--color-danger);
}
.tl-main {
  min-width: 0;
  flex: 1;
}
.tl-label {
  font-size: var(--text-base);
  color: var(--color-text-primary);
}
.tl-step.pending .tl-label {
  color: var(--color-text-faint);
}
.tl-step.failed .tl-label {
  color: var(--color-danger);
}
.tl-sub {
  margin-top: var(--space-1);
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
}
.agent-pills {
  display: inline-flex;
  gap: var(--space-1);
  margin-top: var(--space-1);
  flex-wrap: wrap;
}
.ap {
  font-size: var(--text-xs);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-full);
  padding: 1px 8px;
  color: var(--color-text-secondary);
}
.ap.ok {
  border-color: transparent;
  background: var(--color-success-bg);
  color: var(--color-success);
}
.ap.run {
  border-color: var(--color-accent-border);
  background: var(--color-accent-bg);
  color: var(--color-accent);
}
</style>
