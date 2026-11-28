<script setup lang="ts">
/**
 * ApprovalSheet — B22 HITL 审批弹层·KB 门业务实例（frontend-api.md §5 ApprovalSheet 行 v1.40；
 * §3 生命周期 15；api.md §2.13/§2.5 approval/§7.2 v1.37 消费面；批次 3 第二段组件架构：
 * 通用壳 HitlSheet + 业务皮，用户裁决 2026-11-28——壳机制〔定位/倒计时/错误态/关闭确认/
 * 焦点〕全在 HitlSheet，本组件只组装 KB 门业务内容）。
 *
 * 业务内容（经壳三 slot 注入）：
 *   - #preview：命中预览 ≤3 条（title/heading_path 面包屑逐段插值/excerpt 服务端已截断前端零
 *     二次处理/query_index 对位 rewrittenQueries）。**KB 语料=外部内容：只文本插值、不走
 *     markdown、禁 v-html**（KbChunkDrilldown 直渲先例；B15 顺序锁正交）；不显示 rerank 分数。
 *     **纯展示不可点（2026-11-28 冲突裁决）**：无链接无按钮无点击面——看全文链路挂批次 5 前
 *     契约增量批（api.md §4.6 chunk 直查，previews 仅携 kb_doc_id+chunk_id、§4.6 现为
 *     source_id 键定）。
 *   - #choices：四选项按钮组（kb_only/web_only/both 点击即提交；custom 切换展开）。
 *   - #note：自定义展开——note 文本框（maxlength=500 硬截断 + 计数 + 截断提示；空串不携带键
 *     ——api.md §2.13 note 可选、服务端 400001 兜底）+ 范围三钮（customRange 真实所选——
 *     缺省 both 契约「范围选择缺省两者」，aria-pressed 随所选真实渲染、独立
 *     approval-custom-choice-* testid 命名空间与主选项钮不重复；点即落位提交携带 note）。
 *
 * 提交经 store.decideApproval（409008 → toast + reconcile 多标签页收敛；网络错 → toast、
 * 弹层保留可重试——组件层 submitting 防重入、失败复位；busy 经壳 slot 作用域单一来源禁用；
 * 批次 3 评审 L7：失败文案同步写入壳 errorText 弹层内错误条 role=alert，重开复位）。
 * 关闭=canceled(user)：壳关闭确认（×/弱取消钮两入口）→ emit close → store.cancelApproval
 * （本地立即收口 canceled(user) 不进历史，G 裁决；批次 3 评审 L6：取消在途与提交共用
 * submitting 门——取消 POST 未决期间选项钮与两关闭入口冻结防双动作）。
 *
 * 可见性由状态派生（契约「弹层可见性由状态派生」）：turn.status==='awaiting_approval' ∧
 * approval.status==='pending'——SSE approval_requested / reconcile 刷新恢复（2.5 准源）/
 * decideApproval 乐观置 running / cancelApproval 本地收口 / approval_resolved 四路径收敛
 * 全部自动开/关，组件零生命周期事件依赖。
 */
import { computed, ref, watch } from 'vue'

import HitlSheet from '@/components/common/HitlSheet.vue'
import { useTurnStore } from '@/stores/turn'
import type { ApprovalChoice } from '@/types/api'

/** api.md §2.13 冻结面：note ≤500 字（服务端 400001 兜底；前端先校验硬截断） */
const NOTE_MAX_CHARS = 500

const props = defineProps<{
  turnId: string
  /** fixed 定位参照（AnswerView .follow-bar 宿主元素；null 退化 = 贴视口底） */
  anchorEl: HTMLElement | null
  /** query_index 对位显示命中的子问题（§5 ApprovalSheet 行「命中的子问题」；缺省不渲染该行） */
  rewrittenQueries?: string[]
}>()

const store = useTurnStore()

const turn = computed(() => store.turns[props.turnId])
const approval = computed(() => turn.value?.approval ?? null)

/** 状态派生可见性：awaiting ∧ pending 才渲染（四路径收敛自动开/关） */
const open = computed(
  () => turn.value?.status === 'awaiting_approval' && approval.value?.status === 'pending',
)

/** pending 形态 previews（模板类型收窄面；open 派生已保证 pending，此处防御缺省空） */
const pendingPreviews = computed(() => (approval.value?.status === 'pending' ? approval.value.previews : []))

/* ---------- 交互（四选项/自定义展开/提交/取消——壳确认后取消；提交与取消共用 submitting 门） ---------- */

const customOpen = ref(false)
const note = ref('')
const submitting = ref(false)
/** 自定义面板范围所选（缺省 both——契约「范围选择（缺省两者）」；aria-pressed 真实状态源） */
const customRange = ref<ApprovalChoice>('both')
/** 弹层内错误条文案（批次 3 评审 L7：decideApproval 失败时与 toast 同步呈现——壳 errorText 死面接线） */
const sheetError = ref<string | null>(null)

/** 弹层重开（状态派生 false→true）：交互态与错误条复位（重试失败态不残留进新等待期） */
watch(open, (o) => {
  if (o) {
    customOpen.value = false
    customRange.value = 'both'
    sheetError.value = null
  }
})

/** 计数与截断提示（maxlength 硬截断下 length==500 即达上限） */
const noteCount = computed(() => `${note.value.length}/${NOTE_MAX_CHARS}`)
const noteAtLimit = computed(() => note.value.length >= NOTE_MAX_CHARS)

/** 命中的子问题（query_index 对位 rewrittenQueries；越界/缺省 null 不渲染） */
function subQuery(queryIndex: number): string | null {
  const list = props.rewrittenQueries
  if (!list || queryIndex < 0 || queryIndex >= list.length) return null
  const text = list[queryIndex]
  return text !== undefined && text !== '' ? text : null
}

async function submit(choice: ApprovalChoice): Promise<void> {
  if (submitting.value) return
  submitting.value = true
  try {
    // note 空串不携带键（§2.13 note 可选）；maxlength 属性为浏览器硬截断，slice 为兜底
    // 双防线（jsdom 等无 maxlength 行为环境下仍保证 body ≤500 字），服务端 400001 再兜底
    const trimmedNote = customOpen.value ? note.value.trim().slice(0, 500) : ''
    const result = await store.decideApproval(
      props.turnId,
      choice,
      trimmedNote !== '' ? trimmedNote : undefined,
    )
    // 成功：store 乐观置 running → open 派生 false → 弹层随状态关闭（不等 SSE）；
    // 失败：错误条与 toast 同步呈现服务端/网络文案（评审 L7），弹层保留可重试
    sheetError.value = result.ok ? null : result.message
  } finally {
    submitting.value = false // 失败路径（409008/网络错）复位可重试——提示与 reconcile 在 store 内
  }
}

/** 自定义范围钮点击：真实所选先行落位（aria-pressed 与提交行为一致——评审 L5）再点即提交 */
function selectCustomRange(choice: ApprovalChoice): void {
  customRange.value = choice
  void submit(choice)
}

/**
 * 壳关闭确认后触发（emit close）→ canceled(user) 本地立即收口、不进历史（G 裁决）。
 * 批次 3 评审 L6：取消在途与提交共用 submitting 门——取消 POST 未决期间选项钮与两关闭入口
 * 冻结（busy 单一来源经壳透传），防「取消在途又提交裁决」双动作。
 */
async function cancelTurn(): Promise<void> {
  if (submitting.value) return
  submitting.value = true
  try {
    await store.cancelApproval(props.turnId)
  } finally {
    submitting.value = false
  }
}

const CHOICES: ReadonlyArray<{ choice: ApprovalChoice; label: string; testid: string }> = [
  { choice: 'kb_only', label: 'answer.approvalKbOnly', testid: 'approval-choice-kb_only' },
  { choice: 'web_only', label: 'answer.approvalWebOnly', testid: 'approval-choice-web_only' },
  { choice: 'both', label: 'answer.approvalBoth', testid: 'approval-choice-both' },
]
</script>

<template>
  <HitlSheet
    :open="open"
    :anchor-el="anchorEl"
    :title="$t('answer.approvalTitle')"
    icon="i-db"
    :expires-at="approval?.status === 'pending' ? approval.expires_at : null"
    :timeout-hint-text="$t('answer.approvalTimeoutHint')"
    :busy="submitting"
    :error-text="sheetError"
    :cancel-label="$t('answer.approvalCancelTurn')"
    :close-confirm-title="$t('answer.approvalCloseConfirmTitle')"
    :close-confirm-body="$t('answer.approvalCloseConfirmBody')"
    :close-confirm-ok-label="$t('answer.approvalCloseConfirmOk')"
    :close-confirm-stay-label="$t('answer.approvalCloseConfirmStay')"
    @close="cancelTurn"
  >
    <!-- 预览区：文本插值直渲（禁 v-html/禁 markdown）；纯展示不可点（role=list 语义，零链接零按钮） -->
    <template #preview>
      <div class="as-previews" data-testid="approval-previews" role="list">
        <div
          v-for="(p, i) in pendingPreviews"
          :key="p.chunk_id"
          class="as-preview"
          role="listitem"
          :data-testid="`approval-preview-${i}`"
        >
          <div class="as-pv-title" data-testid="approval-pv-title">{{ p.title }}</div>
          <div
            v-if="p.heading_path.length > 0"
            class="as-pv-path"
            data-testid="approval-pv-path"
          >
            <template v-for="(seg, si) in p.heading_path" :key="`${si}-${seg}`">
              <span v-if="si > 0" class="as-sep">›</span>
              <span>{{ seg }}</span>
            </template>
          </div>
          <div class="as-pv-excerpt" data-testid="approval-pv-excerpt">{{ p.excerpt }}</div>
          <div v-if="subQuery(p.query_index)" class="as-pv-query" data-testid="approval-pv-query">{{ subQuery(p.query_index) }}</div>
        </div>
      </div>
    </template>

    <!-- 四选项（kb_only/web_only/both 点击即提交；custom 切换展开）——busy 经壳 slot 作用域禁用 -->
    <template #choices="{ busy }">
      <div class="as-choices" role="group" :aria-label="$t('answer.approvalTitle')">
        <button
          v-for="c in CHOICES"
          :key="c.choice"
          type="button"
          class="as-choice"
          :data-testid="c.testid"
          :disabled="busy"
          @click="submit(c.choice)"
        >
          {{ $t(c.label) }}
        </button>
        <button
          type="button"
          class="as-choice as-choice-custom"
          data-testid="approval-custom-toggle"
          :aria-expanded="customOpen"
          :disabled="busy"
          @click="customOpen = !customOpen"
        >
          {{ $t('answer.approvalCustom') }}
        </button>
      </div>
    </template>

    <!-- 自定义展开：note 文本框（≤500 计数与截断提示）+ 范围三钮（缺省两者预选，点即提交携带 note） -->
    <template #note="{ busy }">
      <div v-if="customOpen" class="as-custom" data-testid="approval-custom">
        <textarea
          v-model="note"
          class="as-note"
          data-testid="approval-note"
          :maxlength="500"
          :aria-label="$t('answer.approvalCustom')"
          :disabled="busy"
          rows="3"
        ></textarea>
        <div class="as-note-meta">
          <span class="as-note-count" data-testid="approval-note-count">{{ noteCount }}</span>
          <span v-if="noteAtLimit" class="as-note-limit" data-testid="approval-note-limit">
            {{ $t('answer.approvalNoteLimit') }}
          </span>
        </div>
        <div class="as-choices as-custom-choices" role="group" :aria-label="$t('answer.approvalCustom')">
          <!-- 范围钮（自定义面板独立 testid 命名空间——customOpen 时与主选项钮不重复，评审 L5）；
               aria-pressed 反映真实所选（customRange——缺省 both，点击即翻转落位再提交） -->
          <button
            v-for="c in CHOICES"
            :key="c.choice"
            type="button"
            class="as-choice"
            :data-testid="`approval-custom-choice-${c.choice}`"
            :aria-pressed="customRange === c.choice ? 'true' : 'false'"
            :disabled="busy"
            @click="selectCustomRange(c.choice)"
          >
            {{ $t(c.label) }}
          </button>
        </div>
      </div>
    </template>
  </HitlSheet>
</template>

<style scoped>
/* 业务皮样式（壳容器/标题/确认/倒计时样式归 HitlSheet）——全 tokens.css 变量零裸值。
   预览卡纯展示不可点（2026-11-28 冲突裁决）：零链接零按钮、不设 hover/cursor 可点击暗示 */
.as-previews {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.as-preview {
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface-ghost);
  padding: var(--space-3);
  display: grid;
  gap: var(--space-1);
}
.as-pv-title {
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  color: var(--color-text-primary);
  word-break: break-word;
}
.as-pv-path {
  display: flex;
  align-items: center;
  gap: var(--space-1-5);
  flex-wrap: wrap;
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
}
.as-pv-path .as-sep {
  color: var(--color-text-faint);
}
.as-pv-excerpt {
  font-size: var(--text-sm);
  line-height: 1.7;
  color: var(--color-text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
}
.as-pv-query {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  word-break: break-word;
}
.as-choices {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.as-choice {
  display: inline-flex;
  align-items: center;
  min-height: 36px;
  padding: 0 var(--space-4);
  border-radius: var(--radius-full);
  border: 1px solid var(--color-border-strong);
  color: var(--color-text-primary);
  font-size: var(--text-sm);
  transition: background var(--duration-fast) var(--ease-out), border-color var(--duration-fast) var(--ease-out);
}
.as-choice:hover:not(:disabled) {
  background: var(--color-accent-bg);
  border-color: var(--color-accent-border);
  color: var(--color-accent);
}
/* 自定义面板范围所选视觉态（aria-pressed=true——视觉与语义一致，评审 L5 同源） */
.as-choice[aria-pressed='true'] {
  background: var(--color-accent-bg);
  border-color: var(--color-accent-border);
  color: var(--color-accent);
}
.as-choice:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.as-choice-custom {
  color: var(--color-text-secondary);
}
.as-custom {
  border: 1px dashed var(--color-border-strong);
  border-radius: var(--radius-md);
  padding: var(--space-3);
  display: grid;
  gap: var(--space-2);
}
.as-note {
  width: 100%;
  resize: vertical;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-bg-primary);
  color: var(--color-text-primary);
  padding: var(--space-2) var(--space-3);
  font-size: var(--text-sm);
  line-height: 1.6;
}
.as-note:focus-visible {
  outline: none;
  border-color: var(--color-accent-border);
  box-shadow: var(--shadow-focus);
}
.as-note-meta {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  justify-content: flex-end;
}
.as-note-count {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  font-variant-numeric: tabular-nums;
}
.as-note-limit {
  font-size: var(--text-xs);
  color: var(--color-warning);
}
.as-custom-choices .as-choice {
  min-height: 32px;
}
</style>
