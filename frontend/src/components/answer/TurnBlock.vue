<script setup lang="ts">
/**
 * TurnBlock — 单轮对话块（bubble + 模式徽标 + 状态行 + Timeline + thinking + images + 答案渲染）。
 * 组合件（vue-best-practices：feature 拆分），数据 = turnStore 行投影（TurnState）。
 * 锚点 id 约定：问题块 `turn-{id}-q`、快答容器 `turn-{id}-a`、深度报告标题 id 由 AnswerRenderer 生成。
 * 批次 2「Perplexity 交互对齐」（frontend-api.md §5 v1.18 落位）：
 *   - 问题 hover 操作（REQ-055/A39）：QuestionHoverActions 于气泡侧，hover/focus 双通道显隐；
 *   - 回答底部操作行（REQ-056/A40）：AnswerActions 常驻答案区底部（流式中可见）；
 *   - followups（REQ-046）：FollowupChips 于操作行下方（followups 非空才渲染）。
 * 事件上抛：edit-question（预填追问框）/rerun（原问题原模式重发）/sources（面板定位高亮）/
 * follow（chip 追问）/regenerate（v1.26 REQ-063——degraded/failed-with-sources fast/deep 轮重新
 * 生成〔v1.30 放开 deep：api.md §2.12 前置 3 v1.28〕）→ AnswerView 编排层承接。
 */
import { computed, ref } from 'vue'

import UiIcon from '@/components/icons/UiIcon.vue'
import AnswerActions from '@/components/answer/AnswerActions.vue'
import FollowupChips from '@/components/answer/FollowupChips.vue'
import QuestionHoverActions from '@/components/answer/QuestionHoverActions.vue'
import AnswerRenderer from '@/components/markdown/AnswerRenderer.vue'
import ThinkingPanel from '@/components/markdown/ThinkingPanel.vue'
import ImageCardRow from '@/components/answer/ImageCardRow.vue'
import Timeline from '@/components/answer/Timeline.vue'
import { CONTENT_SAFETY_ERROR_CODE } from '@/constants/errors'
import { QUESTION_CLAMP_THRESHOLD } from '@/constants/ui'
import type { TurnState } from '@/stores/turn'

const props = defineProps<{ turn: TurnState; compactNote?: boolean }>()

const emit = defineEmits<{
  /** [n] 引用点击（REQ-003；v1.21 组作用域定位——携带来源轮 turnId，AnswerView 在组容器内定位） */
  cite: [turnId: string, refNo: number]
  'edit-question': [question: string]
  rerun: [question: string]
  sources: []
  follow: [question: string]
  /** 重新生成（v1.26 REQ-063/A47：AnswerActions 第 8 钮——AnswerView 承接 store.regenerate） */
  regenerate: []
}>()

const isLive = computed(() => props.turn.status === 'running' || props.turn.status === 'queued')
/**
 * B13 拒答轮：403002 内容安全拒绝 → 错误卡走中性温和样式分支（非强烈报错红）——
 * 标题走 i18n「内容安全提示」，正文渲染服务端 message 话术；复用既有 errorMsg 渲染通路
 * （SSE error.code / 2.5 error.code 双源）只加分支，其余错误码展示行为零变化。
 */
const isContentSafety = computed(() => props.turn.errorCode === CONTENT_SAFETY_ERROR_CODE)
const doneSeconds = computed(() =>
  props.turn.durationMs !== null && props.turn.durationMs > 0
    ? Math.max(1, Math.round(props.turn.durationMs / 1000))
    : null,
)
/**
 * 降级终态（done finish=degraded / 2.5 degraded 双源）：与 completed 严格区分（§3 done 行）。
 * 双条件判定（B14 修复）：兜底收束轮（api.md §7.4 v1.25）done(degraded, degraded_reason=null)
 * 经 done/2.5 双源投影后 degradedReason 恒 null——单键 degradedReason!==null 会漏判（banner
 * 不渲染 + 状态行误显绿点「已完成」）；status==='degraded'（done.finish / 2.5 status 直写，
 * 双路径均已覆盖）为主键，degradedReason!==null 为投影时序错位的防御性兜底。
 */
const isDegraded = computed(
  () => props.turn.status === 'degraded' || props.turn.degradedReason !== null,
)
const qId = computed(() => `turn-${props.turn.turnId}-q`)
const aId = computed(() => `turn-${props.turn.turnId}-a`)
const headingPrefix = computed(() => props.turn.turnId.slice(0, 8))

/* ---------- 长问题钳制（修复项⑤：每轮独立实例状态；锚点 qId 在外层 div，展开不影响定位） ---------- */
const expanded = ref(false)
const isLongQuestion = computed(
  () => props.turn.question.length > QUESTION_CLAMP_THRESHOLD || props.turn.question.includes('\n'),
)

/* ---------- 问题 hover/focus 显隐（REQ-055：P1 hover+focus 双触发） ---------- */
const qActionsVisible = ref(false)
</script>

<template>
  <article class="turn-block" data-testid="turn-block">
    <div
      :id="qId"
      class="bubble-row"
      data-testid="turn-question"
      @mouseenter="qActionsVisible = true"
      @mouseleave="qActionsVisible = false"
      @focusin="qActionsVisible = true"
      @focusout="qActionsVisible = false"
    >
      <span class="badge" :class="turn.mode === 'deep' ? 'badge-outline' : 'badge-accent'">
        <UiIcon :name="turn.mode === 'deep' ? 'i-wave' : 'i-zap'" size="sm" />
        {{ turn.mode === 'deep' ? $t('mode.deep') : $t('mode.fast') }}
      </span>
      <!-- 问题 hover 操作（REQ-055/A39）：气泡侧两图标；hover+focus 双通道显隐 -->
      <QuestionHoverActions
        :question="turn.question"
        :visible="qActionsVisible"
        @edit="(q) => emit('edit-question', q)"
      />
      <div class="bubble-wrap">
        <div class="bubble" :class="{ clamped: isLongQuestion && !expanded }">{{ turn.question }}</div>
        <button
          v-if="isLongQuestion"
          type="button"
          class="bubble-toggle"
          data-testid="bubble-toggle"
          :aria-expanded="expanded ? 'true' : 'false'"
          :title="expanded ? $t('answer.collapseQuestion') : $t('answer.expandQuestion')"
          @click="expanded = !expanded"
        >
          {{ expanded ? $t('answer.collapseQuestion') : $t('answer.expandQuestion') }}
          <UiIcon name="i-chev-d" size="sm" :class="{ flip: expanded }" />
        </button>
      </div>
    </div>

    <div class="status-row" data-testid="turn-status">
      <span
        class="dot"
        :class="
          isLive
            ? 'dot-accent'
            : turn.status === 'canceled'
              ? 'dot-neutral'
              : turn.status === 'failed'
                ? 'dot-danger'
                : isDegraded
                  ? 'dot-warning'
                  : 'dot-success'
        "
      ></span>
      <template v-if="isLive">{{ $t('answer.statusRunning') }}</template>
      <!-- canceled（v1.24 修复：canceled 原无分支掉进 doneSeconds 默认分支误显绿点「已完成」
           ——用户取消非故障非完成，中性点 + 「已取消 Ns」；部分产出照常渲染） -->
      <template v-else-if="turn.status === 'canceled'">
        {{ $t('answer.statusCanceled') }}<template v-if="doneSeconds !== null"> <b>{{ doneSeconds }}s</b></template>
      </template>
      <template v-else-if="isDegraded">
        {{ $t('answer.statusDegraded') }}<template v-if="doneSeconds !== null"> <b>{{ doneSeconds }}s</b></template>
      </template>
      <template v-else-if="doneSeconds !== null">
        {{ $t('answer.statusDone') }} <b>{{ doneSeconds }}s</b>
      </template>
      <template v-else>{{ $t('answer.statusDone') }}</template>
      <template v-if="turn.sources.length > 0">· {{ turn.sources.length }} {{ $t('answer.sourceCount') }}</template>
      <!-- B13：403002 温和卡分支接管错误展示（状态行不再内联错误文本，防双份呈现） -->
      <template v-if="turn.errorMsg && !isContentSafety">· {{ turn.errorMsg }}</template>
    </div>

    <!-- 内容安全温和卡（B13 拒答轮 403002）：中性温和样式（非强烈报错红）；标题走 i18n，
         正文 = 服务端 message 话术；时间线 stage(rewrite|plan, failed) 由既有 Timeline failed
         渲染承担，本卡不重复节点语义 -->
    <div v-if="isContentSafety && turn.errorMsg" class="safety-card" role="status" data-testid="safety-card">
      <UiIcon name="i-shield" size="sm" class="safety-ico" />
      <div class="safety-text">
        <span class="safety-title">{{ $t('answer.contentSafetyTitle') }}</span>
        <span class="safety-msg">{{ turn.errorMsg }}</span>
      </div>
    </div>

    <Timeline :stages="turn.stages" :status="turn.status" :show-compact-note="compactNote === true" />

    <ThinkingPanel :markdown="turn.thinking_md" :streaming="turn.status === 'running'" />

    <ImageCardRow :images="turn.images" />

    <!-- DegradedBanner（frontend-api.md §3 done 行 / §5 组件清单；api.md §2.5：研究未完成显著标注）：
         数据源 turn.status === 'degraded' ∨ turn.degradedReason（SSE done + 2.5 对账双源，stores/turn.ts），
         reason 三枚举分立文案；兜底收束轮（api.md §7.4 v1.25）degraded_reason=null → 'none' 空态词条 -->
    <div v-if="isDegraded" class="degraded-banner" role="status" data-testid="degraded-banner">
      <UiIcon name="i-alert" size="sm" class="degraded-ico" />
      <span class="degraded-main">{{ $t('answer.degradedBanner') }}</span>
      <span class="degraded-reason">{{ $t(`answer.degradedReason.${turn.degradedReason ?? 'none'}`) }}</span>
    </div>

    <div v-if="turn.answer_md" :id="aId" data-testid="turn-answer">
      <AnswerRenderer
        :markdown="turn.answer_md"
        :heading-prefix="headingPrefix"
        @cite="(n) => emit('cite', turn.turnId, n)"
      />
    </div>

    <!-- 回答底部操作行（REQ-056/A40）：随答案区常驻（流式中可见，操作对已产出内容生效）；
         rerun = 原问题原模式重发（emit 上抛 AnswerView 编排）、sources = 面板定位高亮；
         regenerate = 第 8 钮（v1.26 REQ-063，同轮原地重渲——不新增轮） -->
    <AnswerActions
      v-if="turn.answer_md"
      :turn="turn"
      @rerun="(q) => emit('rerun', q)"
      @sources="emit('sources')"
      @regenerate="emit('regenerate')"
    />

    <!-- followups（REQ-046）：操作行下方行列表（非空才渲染，FollowupChips 内部兜底过滤）；
         点击 chip = 以该文本走既有追问流（emit('follow') → AnswerView） -->
    <FollowupChips
      v-if="turn.followups !== null && turn.followups.length > 0"
      :items="turn.followups"
      @follow="(q) => emit('follow', q)"
    />
  </article>
</template>

<style scoped>
.turn-block {
  margin-bottom: var(--space-10);
}
.bubble-row {
  display: flex;
  justify-content: flex-end;
  align-items: flex-start;
  gap: var(--space-2);
  margin-bottom: var(--space-6);
}
.bubble-wrap {
  max-width: 72%;
  min-width: 0;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: var(--space-1);
}
.bubble {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--space-3) var(--space-4);
  font-size: var(--text-md);
  line-height: var(--text-md--lh);
  white-space: pre-wrap;
  word-break: break-word;
}
/* 长问题钳制（修复项⑤）：3 行截断；展开按钮切 clamped class */
.bubble.clamped {
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.bubble-toggle {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  color: var(--color-text-tertiary);
  font-size: var(--text-xs);
  padding: 2px var(--space-2);
  border-radius: var(--radius-full);
  transition: color var(--duration-fast) var(--ease-out), background var(--duration-fast) var(--ease-out);
}
.bubble-toggle:hover {
  color: var(--color-text-primary);
  background: var(--color-surface-ghost);
}
.bubble-toggle .flip {
  transform: rotate(180deg);
}
.status-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
  margin-bottom: var(--space-5);
}
/* 内容安全温和卡（B13 拒答轮 403002）：中性表面色 + 常规边框（danger 语义色不用），§8.6 不自创样式 */
.safety-card {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--space-3) var(--space-4);
  margin-bottom: var(--space-4);
  font-size: var(--text-sm);
}
.safety-card .safety-ico {
  color: var(--color-text-tertiary);
  margin-top: 2px;
  flex-shrink: 0;
}
.safety-card .safety-text {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  min-width: 0;
}
.safety-card .safety-title {
  color: var(--color-text-primary);
  font-weight: var(--weight-medium);
}
.safety-card .safety-msg {
  color: var(--color-text-tertiary);
  white-space: pre-wrap;
  word-break: break-word;
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
