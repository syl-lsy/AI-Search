<script setup lang="ts">
/**
 * ApprovalRecall — 审批回看折叠条（B22 v1.40 批次 3 第三段；frontend-api.md §5 ApprovalSheet 行
 * 回看态句：已裁决轮在问题下方显示一行折叠条「已按『{choice}』回答 · 查看当时的知识库命中」——
 * **默认收起、不进导出**（hitl-OQ-04 裁决），展开显示预览与 note；**web 直达轮（no_hit）不显示
 * 任何条**（用户当时没被问过，事后提示只会制造困惑——判定留痕仅在 2.5 approval 对象供审计评测）；
 * shadow 形态零弹层零折叠条；canceled 轮 approval 键整体省略（v1.41）→ 无折叠条，组件侧
 * 再按轮级 status=canceled 对称排除（陈旧 approved 对象的幻影折叠条防御——见 approved 派生注）。
 *
 * 挂载点 = TurnBlock 问题下方（设计稿 06-approval-sheet.html 补节 D10 落位基准）。
 *
 * 数据源 = turn.approval（approved 形态唯一可见——decided_by user/timeout/system 均为已裁决轮）
 * + turn.approvalPreviews（流式域缓存——2.5 approved 无 previews 键，刷新后缓存未建立时展开面
 * 仅 note/等待时长）+ turn.awaitingMs（等待时长展示，非零才显示——实施新增键随批上报）。
 *
 * 无 previews 降级形态（批次 3 评审 M4）：刷新恢复的 approved 轮预览缓存为空——条文案降级为
 * 裁决摘要（不含「查看命中」承诺，approvalRecallChoiceBrief 词条）；有审计面（note/等待时长）
 * 仍可展开，三者皆缺退化为非交互摘要 strip（无 button/aria-expanded，零空展开区）。
 *
 * 安全面（与弹层同族）：预览文本为 KB 语料（外部内容）——只文本插值、不走 markdown、禁 v-html
 * （KbChunkDrilldown 直渲先例）；heading_path 面包屑逐段插值；展开面零链接零按钮（回看面无
 * 「查看全文」跳转义务——draft 补节注，点入链路挂批次 5 前契约增量批）。
 *
 * 导出（OQ-04）：本组件为纯 UI 面，exportThread.ts 投影面（ExportableTurn = question/answer_md/
 * sources）结构上不含审批数据——折叠条零进导出链（export-thread.test.ts OQ-04 用例锁定）。
 *
 * a11y：strip 为 button + aria-expanded + aria-controls；展开面 region 由 strip 关联。
 */
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import UiIcon from '@/components/icons/UiIcon.vue'
import type { TurnState } from '@/stores/turn'
import type { ApprovalChoice } from '@/types/api'

const props = defineProps<{ turn: TurnState }>()

const { t } = useI18n()

/**
 * approved 形态唯一可见（no_hit/shadow/pending/canceled 键省略 → 整块不渲染）。
 * P3 防御对称化（批次 3 残留清偿）：轮级 status=canceled 一并排除——审批通过后轮又被取消的
 * 内存窗口里（done(finish=canceled) 不触碰 approval，陈旧 approved 对象留存至 reconcile 按
 * 2.5「canceled 键整体省略」投影 null），仅凭 approval.status 判定会渲染幻影折叠条；双维判定
 * （轮 status ∧ approval.status）与弹层可见性派生（ApprovalSheet open）同构。
 */
const approved = computed(() =>
  props.turn.status !== 'canceled' && props.turn.approval?.status === 'approved' ? props.turn.approval : null,
)

const expanded = ref(false)

/** choice → 四选项词条复用（契约 §7 v1.40「choice 文本复用四选项词条」） */
const CHOICE_LABELS: ReadonlyArray<{ choice: ApprovalChoice; key: string }> = [
  { choice: 'kb_only', key: 'answer.approvalKbOnly' },
  { choice: 'web_only', key: 'answer.approvalWebOnly' },
  { choice: 'both', key: 'answer.approvalBoth' },
  { choice: 'custom', key: 'answer.approvalCustom' },
]

const choiceLabel = computed(() => {
  const choice = approved.value?.choice
  const hit = CHOICE_LABELS.find((c) => c.choice === choice)
  return hit !== undefined ? t(hit.key) : ''
})

/**
 * 无 previews 降级形态（批次 3 评审 M4）：刷新恢复的 approved 轮 2.5 不投影 previews（契约现状）
 * 且流式域缓存已被刷新清空——条文案不得再承诺「查看当时的知识库命中」，降级为裁决摘要；
 * 有 previews（流式缓存/重放帧回填）时维持契约钉字形态。
 */
const hasPreviews = computed(() => (props.turn.approvalPreviews?.length ?? 0) > 0)

const stripText = computed(() => {
  if (approved.value === null) return ''
  const params = { choice: choiceLabel.value }
  return hasPreviews.value
    ? t('answer.approvalRecallChoice', params)
    : t('answer.approvalRecallChoiceBrief', params)
})

/** note（2.5 approved 可选键——缺省/空串不渲染该行） */
const noteText = computed(() => {
  const note = approved.value?.note
  return note !== undefined && note !== '' ? note : null
})

/** 等待时长（awaiting_ms 非零才携带口径——0/null 不渲染该行；秒级展示） */
const waitedSeconds = computed(() => {
  const ms = props.turn.awaitingMs
  return ms !== null && ms > 0 ? Math.max(1, Math.round(ms / 1000)) : null
})

/**
 * 展开面有内容判定（评审 M4 同源）：previews / note / 等待时长任一在场才可展开；三者皆缺
 * （刷新恢复的裸 approved 形态）→ strip 退化为非交互摘要（无 button、无 aria-expanded，
 * 点击零展开面——不渲染空展开区）。
 */
const hasBody = computed(() => hasPreviews.value || noteText.value !== null || waitedSeconds.value !== null)

/** 展开面 id（评审 L8：带 turnId 后缀——多轮同屏 DOM id 唯一，aria-controls 关联不串轮） */
const bodyId = computed(() => `approval-recall-body-${props.turn.turnId}`)
</script>

<template>
  <div v-if="approved !== null" class="approval-recall" data-testid="approval-recall">
    <!-- 展开面有内容（previews/note/等待时长任一）→ 交互折叠条；皆缺 → 非交互摘要 strip -->
    <button
      v-if="hasBody"
      type="button"
      class="recall-strip"
      data-testid="approval-recall-strip"
      :aria-expanded="expanded ? 'true' : 'false'"
      :aria-controls="bodyId"
      @click="expanded = !expanded"
    >
      <UiIcon name="i-chev-d" size="sm" :class="{ flip: expanded }" />
      <span>{{ stripText }}</span>
    </button>
    <span v-else class="recall-strip recall-strip-static" data-testid="approval-recall-strip">
      {{ stripText }}
    </span>
    <div
      v-if="expanded && hasBody"
      :id="bodyId"
      class="recall-body"
      data-testid="approval-recall-body"
    >
      <!-- 预览回显：文本插值直渲（KB 语料=外部内容——禁 v-html/禁 markdown；零链接零按钮） -->
      <div
        v-for="(p, i) in turn.approvalPreviews ?? []"
        :key="p.chunk_id"
        class="recall-pv"
        role="list"
        :data-testid="`approval-recall-pv-${i}`"
      >
        <div class="recall-pv-title" data-testid="approval-recall-pv-title">{{ p.title }}</div>
        <div v-if="p.heading_path.length > 0" class="recall-pv-path">
          <template v-for="(seg, si) in p.heading_path" :key="`${si}-${seg}`">
            <span v-if="si > 0" class="ar-sep">›</span>
            <span>{{ seg }}</span>
          </template>
        </div>
        <div class="recall-pv-excerpt" data-testid="approval-recall-pv-excerpt">{{ p.excerpt }}</div>
      </div>
      <!-- note 回显（补充指令前缀 = 实施新增键，契约登记缺口随批上报——draft 补节 D8 同源） -->
      <p v-if="noteText !== null" class="recall-note" data-testid="approval-recall-note">
        <b>{{ $t('answer.approvalNoteLabel') }}：</b>{{ noteText }}
      </p>
      <!-- 等待时长（awaiting_ms 非零才显示；第三段 awaiting_ms 展示面） -->
      <p v-if="waitedSeconds !== null" class="recall-waited" data-testid="approval-recall-waited">
        {{ $t('answer.approvalWaitedSeconds', { n: waitedSeconds }) }}
      </p>
    </div>
  </div>
</template>

<style scoped>
/* 回看折叠条（设计稿 06-approval-sheet.html 补节基准——strip/展开面 tokens 化；预览卡纯展示
   不可点：零链接零按钮、不设 hover/cursor 可点击暗示） */
.approval-recall {
  display: grid;
  gap: var(--space-2);
  justify-items: start;
  margin-top: var(--space-2);
}
.recall-strip {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface-ghost);
  padding: var(--space-2) var(--space-3);
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  transition: background var(--duration-fast) var(--ease-out), color var(--duration-fast) var(--ease-out);
}
/* hover 可点击暗示仅交互形态（button）——非交互摘要 strip（span）不设 hover */
button.recall-strip:hover {
  background: var(--color-surface-hover);
  color: var(--color-text-primary);
}
.recall-strip .flip {
  transform: rotate(180deg);
}
.recall-body {
  border: 1px dashed var(--color-border-strong);
  border-radius: var(--radius-md);
  padding: var(--space-3);
  display: grid;
  gap: var(--space-2);
  width: 100%;
}
.recall-pv {
  display: grid;
  gap: var(--space-1);
}
.recall-pv-title {
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  color: var(--color-text-primary);
  word-break: break-word;
}
.recall-pv-path {
  display: flex;
  align-items: center;
  gap: var(--space-1-5);
  flex-wrap: wrap;
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}
.recall-pv-path .ar-sep {
  color: var(--color-text-faint);
}
.recall-pv-excerpt {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
}
.recall-note {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}
.recall-note b {
  color: var(--color-text-tertiary);
  font-weight: var(--weight-medium);
}
.recall-waited {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  font-variant-numeric: tabular-nums;
}
</style>
