<script setup lang="ts">
/**
 * KbChunkDrilldown — KB 来源溯源下钻弹窗（frontend-api.md §5 KbChunkDrilldown 行 v1.33/v1.34；
 * api.md §4.6/§4.4 消费面；plan-m3-b6-f6 §1.3）。
 *
 * 壳复用 ThreadModal（role=dialog，Teleport/遮罩/×/Esc 三关闭通道——焦点返还本组件内聚，
 * SnapshotModal 双相位先例）。内容两段：
 *  - 文档面（§4.4 合流源）：文档标题 + 【知识库】徽标 + catalog_path 目录链（非空才渲染）+
 *    parse_meta chips（null 段不渲染）——doc prop 为 null（获取失败）整块降级隐藏，不遮蔽切片面；
 *  - 切片面（§4.6）：heading_path 面包屑（空数组不渲染）→ page/paragraph 定位 pills（null 不渲染
 *    对应段）→ content 原文（纯文本 pre-wrap、max-height 200px 内滚、文本插值直渲——
 *    Vue 文本插值已转义、安全只增不减，不经 DOMPurify 以保类标签原文逐字保真〔A36 溯源
 *    原文预览；契约已同口径钉正：frontend-api.md §5 v1.38 / api.md §4.6 v1.35。
 *    SnapshotModal 快照摘录仍走 DOMPurify 消毒层，两路线分叉各自成立〕；溯源内容仅进
 *    UI 不进 LLM）
 *    → meta-mono 行（chunk_id/document_id
 *    恒在；node_id/node_token 可选键缺则不渲染）。
 * 「打开飞书原文」外链（v1.34）：node_token 存在 且 isHttpUrl 守卫通过才渲染（feishuJumpUrl），
 * href = https://www.feishu.cn/wiki/{node_token}、target=_blank + rel="noopener noreferrer"。
 * 404001 降级（unavailable prop）：占位卡 answer.kbChunkUnavailable——不报错红、不 toast、
 * 占位卡即弹窗体（中性提示，api.md §4.6 单码不泄漏存在性）。
 */
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import ThreadModal from '@/components/threads/ThreadModal.vue'
import UiIcon from '@/components/icons/UiIcon.vue'
import { feishuJumpUrl } from '@/utils/kbDisplay'
import type { KbChunkDetail, KbDocument } from '@/types/api'

const props = defineProps<{
  open: boolean
  /** §4.6 切片面（AnswerView fetchKbChunk 结果；404001 时为 null + unavailable=true） */
  chunk: KbChunkDetail | null
  /** §4.4 文档面合流源（fetchDocument 结果；失败为 null → 文档面整块降级隐藏） */
  doc: KbDocument | null
  /** 404001 优雅降级态（来源不存在/越权/chunk 或文档已删除单码） */
  unavailable: boolean
}>()

const emit = defineEmits<{ close: [] }>()

const { t } = useI18n()

const contentEl = ref<HTMLElement | null>(null)
let previouslyFocused: HTMLElement | null = null

/** 目录链段（catalog_path 按「 / 」分段展示——纯展示拆分，root→leaf 末位 leaf 高亮） */
const catalogSegments = computed<string[]>(() => {
  const path = props.doc?.catalog_path
  if (!path) return []
  return path.split('/').map((s) => s.trim()).filter((s) => s !== '')
})

/** 飞书外链（node_token 存在 + isHttpUrl 守卫；缺省 → null 整钮不渲染） */
const feishuUrl = computed<string | null>(() => feishuJumpUrl(props.chunk?.node_token))

/** 焦点返还（open/close 双相位；组件卸载兜底归还——SnapshotModal 先例） */
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
  <ThreadModal :open="open" :title="t('answer.kbChunkTitle')" @close="emit('close')">
    <!-- 404001 降级：占位卡即弹窗体（不报错红、不 toast、不关闭） -->
    <div
      v-if="unavailable"
      ref="contentEl"
      class="unavail-card"
      data-testid="kb-chunk-drilldown"
      tabindex="-1"
    >
      <UiIcon name="i-alert" />
      <div>{{ $t('answer.kbChunkUnavailable') }}</div>
      <div class="unavail-actions">
        <button type="button" class="kb-close" data-testid="kb-chunk-close" @click="emit('close')">
          {{ $t('answer.kbChunkClose') }}
        </button>
      </div>
    </div>

    <div v-else-if="chunk !== null" ref="contentEl" class="chunk-body" data-testid="kb-chunk-drilldown" tabindex="-1">
      <!-- 文档面（§4.4 合流源；doc=null 降级隐藏，不遮蔽切片面） -->
      <div v-if="doc !== null" class="kb-doc-box" data-testid="kb-chunk-doc">
        <div class="kb-doc-line">
          <span class="kb-doc-title">{{ doc.title }}</span>
          <span class="badge badge-kb">{{ $t('answer.kbChunkDocBadge') }}</span>
        </div>
        <div v-if="catalogSegments.length > 0" class="path-line" :aria-label="$t('answer.kbChunkCatalog')">
          <template v-for="(seg, i) in catalogSegments" :key="`${seg}-${i}`">
            <span v-if="i > 0" class="sep">›</span>
            <span :class="{ leaf: i === catalogSegments.length - 1 }">{{ seg }}</span>
          </template>
        </div>
        <div v-if="doc.parse_meta" class="pm-chips" data-testid="kb-chunk-parse" :aria-label="$t('answer.kbChunkParseMeta')">
          <span v-if="doc.parse_meta.pages !== null" class="pm-chip">{{ $t('answer.kbParsePages', { n: doc.parse_meta.pages }) }}</span>
          <span class="pm-chip">{{ $t('answer.kbParseTables', { n: doc.parse_meta.tables }) }}</span>
          <span class="pm-chip">{{ $t('answer.kbParseCode', { n: doc.parse_meta.code_blocks }) }}</span>
        </div>
      </div>

      <!-- 切片面（§4.6） -->
      <div class="chunk-face">
        <div v-if="chunk.heading_path.length > 0" class="path-line" data-testid="kb-chunk-heading" :aria-label="$t('answer.kbChunkHeading')">
          <template v-for="(seg, i) in chunk.heading_path" :key="`${seg}-${i}`">
            <span v-if="i > 0" class="sep">›</span>
            <span class="crumb-seg" :class="{ leaf: i === chunk.heading_path.length - 1 }">{{ seg }}</span>
          </template>
        </div>
        <div v-if="chunk.page !== null || chunk.paragraph !== null" class="loc-row" data-testid="kb-chunk-loc">
          <span class="kb-sec-label">{{ $t('answer.kbChunkLocate') }}：</span>
          <span v-if="chunk.page !== null" class="loc-pill">{{ $t('answer.kbChunkPage', { n: chunk.page }) }}</span>
          <span v-if="chunk.paragraph !== null" class="loc-pill">{{ $t('answer.kbChunkParagraph', { n: chunk.paragraph }) }}</span>
        </div>
        <span class="kb-sec-label">{{ $t('answer.kbChunkContentLabel') }}</span>
        <div class="chunk-content" data-testid="kb-chunk-content">{{ chunk.content }}</div>
        <div class="meta-mono" data-testid="kb-chunk-meta">
          chunk_id {{ chunk.chunk_id }} · document_id {{ chunk.document_id }}
          <template v-if="chunk.node_id"> · node_id {{ chunk.node_id }}</template>
          <template v-if="chunk.node_token"> · node_token {{ chunk.node_token }}</template>
        </div>
      </div>

      <!-- 动作行：关闭 + 打开飞书原文（node_token 存在且守卫通过才渲染） -->
      <div class="chunk-actions">
        <button type="button" class="kb-close" data-testid="kb-chunk-close" @click="emit('close')">
          {{ $t('answer.kbChunkClose') }}
        </button>
        <a
          v-if="feishuUrl !== null"
          class="kb-open-feishu"
          data-testid="kb-chunk-feishu"
          :href="feishuUrl"
          target="_blank"
          rel="noopener noreferrer"
        >
          <UiIcon name="i-feishu" size="sm" />{{ $t('answer.kbChunkOpenFeishu') }}<UiIcon name="i-ext" size="sm" />
        </a>
      </div>
    </div>
  </ThreadModal>
</template>

<style scoped>
.chunk-body,
.unavail-card {
  outline: none;
}
.unavail-card {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
  border: 1px dashed var(--color-border-strong);
  border-radius: var(--radius-md);
  padding: var(--space-4);
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
}
.unavail-card :first-child {
  color: var(--color-text-tertiary);
  flex: none;
}
.unavail-actions {
  margin-left: auto;
  flex: none;
}
/* 文档面（设计稿 .kb-doc-box） */
.kb-doc-box {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--space-3) var(--space-4);
  display: grid;
  gap: var(--space-2);
  background: var(--color-surface-ghost);
  margin-bottom: var(--space-3);
}
.kb-doc-line {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.kb-doc-title {
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
  color: var(--color-text-primary);
}
/* 目录链/标题链（设计稿 .path-line） */
.path-line {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}
.path-line .sep {
  color: var(--color-text-faint);
}
.path-line .leaf,
.path-line .crumb-seg.leaf {
  color: var(--color-text-primary);
}
.pm-chips {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.pm-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 22px;
  padding: 0 8px;
  border-radius: var(--radius-full);
  background: var(--color-surface-active);
  color: var(--color-text-secondary);
  font-size: var(--text-xs);
  font-variant-numeric: tabular-nums;
}
.kb-sec-label {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  letter-spacing: 0.02em;
}
.chunk-face {
  display: grid;
  gap: var(--space-2);
}
/* 定位 pills（设计稿 .loc-row） */
.loc-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}
.loc-row .loc-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 24px;
  padding: 0 10px;
  border-radius: var(--radius-full);
  border: 1px solid var(--color-border-strong);
  font-size: var(--text-xs);
}
/* content 原文：pre-wrap + max-height 200px 内滚（plan §1.3/D6 钉定） */
.chunk-content {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface-ghost);
  padding: var(--space-4);
  font-size: var(--text-sm);
  line-height: 1.8;
  color: var(--color-text-primary);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 200px;
  overflow-y: auto;
}
.meta-mono {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--color-text-faint);
  word-break: break-all;
}
.chunk-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
  margin-top: var(--space-4);
}
.kb-close,
.kb-open-feishu {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  min-height: 32px;
  padding: 0 var(--space-4);
  border-radius: var(--radius-full);
  font-size: var(--text-sm);
  transition: background var(--duration-fast) var(--ease-out), color var(--duration-fast) var(--ease-out);
}
.kb-close {
  color: var(--color-text-secondary);
  border: 1px solid var(--color-border-strong);
}
.kb-close:hover {
  background: var(--color-surface-ghost);
  color: var(--color-text-primary);
}
.kb-open-feishu {
  color: var(--color-accent);
  background: var(--color-accent-bg);
}
.kb-open-feishu:hover {
  color: var(--color-accent);
  background: var(--color-accent-bg);
}
</style>
