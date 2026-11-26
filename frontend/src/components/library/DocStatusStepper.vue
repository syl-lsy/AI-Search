<script setup lang="ts">
/**
 * DocStatusStepper — 四节点阶段步进条（plan-m3-b6-f6 §3.2；设计稿 .st-trail）。
 * normalizing→chunking→indexing→ready 四节点 done/cur/fail/todo 投影（stepperNodes 纯函数）；
 * failed 按 fail_reason 阶段前缀定位 fail 节点（parseFailStage；pending 期失败无前缀 → 全 todo）；
 * pending 态整条不渲染（由徽标承载，plan 钉定）。aria-hidden：语义由徽标承担（设计稿同口径）。
 */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

import { parseFailStage, stepperNodes } from '@/utils/kbDisplay'
import type { KbDocStatus } from '@/types/api'

const props = defineProps<{
  status: KbDocStatus
  failReason: string | null
}>()

const { t } = useI18n()

const nodes = computed(() => stepperNodes(props.status, parseFailStage(props.failReason ?? '').stage))

function labelOf(key: 'normalizing' | 'chunking' | 'indexing' | 'ready'): string {
  switch (key) {
    case 'normalizing':
      return t('library.stepperNormalizing')
    case 'chunking':
      return t('library.stepperChunking')
    case 'indexing':
      return t('library.stepperIndexing')
    case 'ready':
      return t('library.stepperReady')
  }
}
</script>

<template>
  <div v-if="props.status !== 'pending'" class="st-trail" aria-hidden="true" data-testid="kb-doc-stepper">
    <template v-for="(node, i) in nodes" :key="node.key">
      <i v-if="i > 0" class="st-gap"></i>
      <span class="st" :class="node.state"><i v-if="node.state === 'cur'" class="sdot"></i>{{ labelOf(node.key) }}</span>
    </template>
  </div>
</template>

<style scoped>
/* 阶段步进条（设计稿 214~233 行形态；取色全走 tokens 变量） */
.st-trail {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-top: var(--space-1);
}
.st {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 20px;
  padding: 0 7px;
  border-radius: var(--radius-full);
  font-size: var(--text-xs);
  white-space: nowrap;
  color: var(--color-text-faint);
  border: 1px solid var(--color-border-faint);
}
.st.done {
  color: var(--color-success);
  background: var(--color-success-bg);
  border-color: transparent;
}
.st.cur {
  color: var(--color-warning);
  background: var(--color-warning-bg);
  border-color: transparent;
}
.st.fail {
  color: var(--color-danger);
  background: var(--color-danger-bg);
  border-color: transparent;
}
.st-gap {
  width: 10px;
  height: 1px;
  background: var(--color-border-strong);
  flex: none;
}
.st .sdot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
  flex: none;
}
.st.cur .sdot {
  animation: st-pulse 1.4s ease-in-out infinite;
}
@keyframes st-pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.35;
  }
}
</style>
