<script setup lang="ts">
/**
 * SnapshotModal — 来源快照预览弹窗（批次 6，api.md §5.1 GET /sources/{id}/snapshot 消费面）。
 *
 * 形态对标：复用 ThreadModal 居中 modal 壳（Teleport/遮罩变暗/×/Esc 三条关闭通道 + role/
 * aria-modal 语义）——本组件承载快照域内容与焦点返还（ThreadModal 契约「焦点返还由调用方
 * 处理」在快照域内聚实现）：
 *   - excerpt 摘录：原文开头 2000 字符（服务端截取），纯文本 + 换行保留渲染（white-space:
 *     pre-wrap，不走 markdown 渲染）；先过 DOMPurify 消毒再以文本插值呈现（纵深防御——
 *     文本插值本身已转义，消毒层兜上游内容异常形态，与答案渲染同源卫生链）；
 *   - 元数据行：fetched_at（ISO T→空格显示）/ size_bytes（B/KB/MB 人类可读）/ content_type；
 *   - 「打开原文」外链钮：download_url 过 isHttpUrl 协议白名单守卫（§2 渲染前校验 ^https?://），
 *     target=_blank + rel="noopener noreferrer"；非 http 形态整钮不渲染（AnswerView 侧对
 *     不可用形态另行 toast answer.snapshotUnavailable——摘录预览为主面，外链不可用不遮蔽）；
 *   - 焦点返还：open 置位时记录此前焦点元素并把焦点落弹窗内容体（tabindex=-1 承接），
 *     close 置位（或卸载）时归还——批次先例 ThreadModal 调用方焦点管理口径。
 * 取色全走 tokens.css 变量（设计 tokens 强制，零自创样式值）。
 */
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import DOMPurify from 'dompurify'

import ThreadModal from '@/components/threads/ThreadModal.vue'
import { isHttpUrl } from '@/utils/guards'
import type { SnapshotData } from '@/types/api'

const props = defineProps<{
  open: boolean
  data: SnapshotData | null
}>()

const emit = defineEmits<{ close: [] }>()

const { t } = useI18n()

const contentEl = ref<HTMLElement | null>(null)
let previouslyFocused: HTMLElement | null = null

/** 摘录纵深消毒（先 DOMPurify 再文本插值——双层卫生，见 docstring） */
const safeExcerpt = computed(() => (props.data !== null ? DOMPurify.sanitize(props.data.excerpt) : ''))

/** 外链钮渲染守卫（download_url 协议白名单；非 http 形态整钮不渲染） */
const canOpenOriginal = computed(() => props.data !== null && isHttpUrl(props.data.download_url))

/** 元数据展示格式化（纯展示，无 i18n 依赖——数值/时间形态稳定可测） */
function formatFetchedAt(ts: string): string {
  return ts.replace('T', ' ').replace(/(\.\d+)?Z$/, '')
}
function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

/** 焦点返还（open/close 双相位；组件卸载兜底归还） */
watch(
  () => props.open,
  async (open) => {
    if (open) {
      previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null
      await nextTick()
      contentEl.value?.focus()
    } else if (previouslyFocused !== null) {
      previouslyFocused.focus()
      previouslyFocused = null
    }
  },
)
onUnmounted(() => {
  if (previouslyFocused !== null) {
    previouslyFocused.focus()
    previouslyFocused = null
  }
})
</script>

<template>
  <ThreadModal :open="open" :title="t('answer.snapshotTitle')" @close="emit('close')">
    <div ref="contentEl" class="snap-body" data-testid="snapshot-modal-body" tabindex="-1">
      <!-- 元数据行（api.md §5.1：snapshot 元数据随预签名 URL 与摘录同响应） -->
      <dl class="snap-meta" data-testid="snapshot-meta">
        <div class="snap-meta-item">
          <dt>{{ $t('answer.snapshotMetaFetched') }}</dt>
          <dd>{{ data !== null ? formatFetchedAt(data.fetched_at) : '' }}</dd>
        </div>
        <div class="snap-meta-item">
          <dt>{{ $t('answer.snapshotMetaSize') }}</dt>
          <dd>{{ data !== null ? formatBytes(data.size_bytes) : '' }}</dd>
        </div>
        <div class="snap-meta-item">
          <dt>{{ $t('answer.snapshotMetaType') }}</dt>
          <dd>{{ data !== null ? data.content_type : '' }}</dd>
        </div>
      </dl>

      <!-- 摘录预览（原文开头 2000 字符；纯文本 + 换行保留，文本插值已转义） -->
      <div class="snap-excerpt" data-testid="snapshot-excerpt">{{ safeExcerpt }}</div>

      <!-- 动作行：外链（协议白名单守卫）+ 关闭 -->
      <div class="snap-actions">
        <a
          v-if="canOpenOriginal"
          class="snap-open"
          data-testid="snapshot-open-original"
          :href="data?.download_url"
          target="_blank"
          rel="noopener noreferrer"
        >{{ $t('answer.snapshotOpenOriginal') }}</a>
        <button
          type="button"
          class="snap-close"
          data-testid="snapshot-close"
          @click="emit('close')"
        >{{ $t('answer.snapshotClose') }}</button>
      </div>
    </div>
  </ThreadModal>
</template>

<style scoped>
.snap-body {
  outline: none;
}
.snap-meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2) var(--space-5);
  margin: 0 0 var(--space-3);
  padding: 0;
}
.snap-meta-item {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  min-width: 0;
}
.snap-meta-item dt {
  color: var(--color-text-tertiary);
  font-size: var(--text-xs);
}
.snap-meta-item dd {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--text-xs);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.snap-excerpt {
  max-height: 320px;
  overflow-y: auto;
  padding: var(--space-3);
  background: var(--color-surface-ghost);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text-primary);
  font-size: var(--text-sm);
  line-height: var(--text-sm--lh);
  white-space: pre-wrap;
  word-break: break-word;
}
.snap-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
  margin-top: var(--space-4);
}
.snap-open,
.snap-close {
  display: inline-flex;
  align-items: center;
  min-height: 32px;
  padding: 0 var(--space-4);
  border-radius: var(--radius-full);
  font-size: var(--text-sm);
  transition: background var(--duration-fast) var(--ease-out), color var(--duration-fast) var(--ease-out);
}
.snap-open {
  color: var(--color-accent);
  background: var(--color-accent-bg);
}
.snap-open:hover {
  color: var(--color-accent);
  background: var(--color-accent-bg);
}
.snap-close {
  color: var(--color-text-secondary);
  border: 1px solid var(--color-border-strong);
}
.snap-close:hover {
  background: var(--color-surface-ghost);
  color: var(--color-text-primary);
}
</style>
