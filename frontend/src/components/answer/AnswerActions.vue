<script setup lang="ts">
/**
 * AnswerActions — 回答底部操作行（REQ-056/A40 + REQ-057/A41 反馈入口，frontend-api.md §5 v1.18 落位）。
 *
 * 七钮常驻（流式中可见，操作对已产出内容生效）：
 *   1. 复制    = answer_md 全文入剪贴板（失败 console 留痕 + 一次性 toast 提示手动复制
 *      ——批次 2 评审 P3 闭合；成功零打扰）；
 *   2. 分享    = 复制会话链接（origin + /thread/{threadId}）入剪贴板，成功 toast
 *      (answer.linkCopied)、失败 toast(answer.copyFailed) + console 留痕（自用无公开分享
 *      后端——已定案，偏离 Perplexity 公开链接形态）；
 *   3. 有帮助 / 4. 没有帮助 = 互斥反馈 + 同钮再点即取消（store.sendFeedback 乐观 UI，
 *      REQ-057；api.md §2.9 v1.17——点已选中钮发 helpful:null 清除，点另一钮即改判；
 *      选中态 aria-pressed 按现值驱动）；
 *   5. 复刻    = emit('rerun', turn.question) → AnswerView 以原问题原模式走既有追问流
 *      （REQ-042 并发策略照常适用）；
 *   6. 来源    = emit('sources') → AnswerView 滚动定位来源面板并高亮（复用 [n] 联动机制）；
 *      标签带来源计数（Perplexity「10 Sources」形态）；
 *   7. 更多操作 = 轻菜单 →「导出 Markdown」前端本地生成 .md Blob 下载（内容 = answer_md，
 *      经 utils/exportThread.ts 共享实现——**不依赖也不复用深度专用 export 端点**——
 *      409003 非深度轮拒绝，A40 负向判定）；deep 轮另增「导出报告」服务端导出项（批次 6，
 *      REQ-016 消费面落地：api.md §2.8/§5.3——exportTurnReport POST → 合法形态 download_path
 *      锚点跳转（P2-4 形态守卫：§2.8 下载端点线形正则，非法 toast 不跳转），服务端 200
 *      FileResponse Content-Disposition 驱动下载；409003 同分钟碰撞/
 *      404001 → toast 服务端中文 message。门控 = mode === 'deep' ∧ turn.exportAvailable
 *      （§2.5 判据 store 对账回写，前端不本地推断）——fast 轮菜单项不渲染（A40 负向）；
 *   8. 重新生成（v1.26 增，REQ-063/A47，复刻钮旁；frontend-api.md §5 ⑧ / api.md §2.12）=
 *      emit('regenerate') → AnswerView 承接 store.regenerate（POST §2.12 → 状态置 running +
 *      清空重渲 + SSE 重订阅，§3 生命周期 14〔deep 形态注 v1.30〕）。可见性门控 = status ∈
 *      {degraded, failed}（终态——isLive 结构性排除）∧ sources.length > 0（§2.12 前置 2）∧
 *      mode ∈ {fast, deep}（§2.12 前置 3【v1.28 放开】——deep 有源 degraded/failed 轮可重新
 *      生成，原「deep 轮不渲染」随放开作废，frontend-api.md v1.30 §3 生命周期 14 / §5 ⑧）；
 *      i-replay 系图标。
 * 样式对标 Perplexity：图标行 + 文字型按钮，hover 显色；仅反馈/分享剪贴板触端，其余纯前端。
 * v1.24：七钮接入 UiTooltip（主标题替换原生 title；aria-label 保留——气泡 aria-hidden
 * 纯视觉，语义由触发钮承载，frontend-api.md §5 UiTooltip 行接入面补漏）。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import UiIcon from '@/components/icons/UiIcon.vue'
import UiTooltip from '@/components/common/UiTooltip.vue'
import { normalizeError } from '@/api/client'
import { exportTurnReport } from '@/api/turns'
import { toast } from '@/composables/toast'
import { useTurnStore } from '@/stores/turn'
import type { TurnState } from '@/stores/turn'
import { buildThreadLink, downloadMarkdown, sanitizeFilename } from '@/utils/exportThread'

const props = defineProps<{
  turn: TurnState
}>()

const emit = defineEmits<{
  rerun: [question: string]
  sources: []
  regenerate: []
}>()

const { t } = useI18n()
const store = useTurnStore()

/* ---------- 8. 重新生成（v1.26 REQ-063/A47：可见性门控，frontend-api.md §5 ⑧；v1.30 放开 deep） ---------- */
/**
 * 门控 = status ∈ {degraded, failed} ∧ sources.length > 0 ∧ mode ∈ {fast, deep}。
 * degraded/failed 均为终态枚举（isLive=running/queued 结构性排除，无需另判）；
 * 零来源 failed / direct-failed / completed / canceled 不渲染（api.md §2.12 前置 2/3
 * 【v1.28 放开 mode∈{fast,deep}】——服务端以同码 409006 兜底，前端门控仅为不渲染无效入口；
 * fast 行为零回退，deep 分支与 fast 同判据）。
 */
const canRegenerate = computed(
  () =>
    (props.turn.status === 'degraded' || props.turn.status === 'failed') &&
    props.turn.sources.length > 0 &&
    (props.turn.mode === 'fast' || props.turn.mode === 'deep'),
)

/* ---------- ⑦b 服务端导出报告（批次 6 REQ-016 消费面落地：api.md §2.8/§5.3） ---------- */
/**
 * 门控 = mode === 'deep' ∧ turn.exportAvailable（§2.5 判据 store 对账回写——SSE 无对应
 * 事件、仅 2.5 双源；fast 轮恒不渲染，A40 负向判定「fast 不调端点」的菜单项侧承载）。
 */
const canExportReport = computed(() => props.turn.mode === 'deep' && props.turn.exportAvailable)

/**
 * download_path 消费面形态守卫（批次 6 收口轮 P2-4，frontend-api.md §8.9⑥ 盲区口径的
 * 运行时防线）：api.d.ts 是静态镜像、非运行时校验器——anchor.href 赋值前校验
 * download_path 至少为「同源相对路径且以 /api/ 开头」，钉死为 §2.8 report 工件下载
 * 端点线形（thread_id / artifact_id 各一段 UUID）；非法形态（javascript: 伪协议 /
 * 协议相对 //host/ / 非 /api/ 相对路径等契约回归）→ toast 不跳转（走既有 toast 链路）。
 */
const DOWNLOAD_PATH_PATTERN = /^\/api\/v1\/threads\/[0-9a-f-]{36}\/artifacts\/[0-9a-f-]{36}\/download$/

/**
 * 服务端导出报告：exportTurnReport（api.md §2.8 POST）→ 合法形态 download_path 锚点跳转
 * （api.md §5.3 口径——服务端 200 FileResponse + Content-Disposition attachment，
 * 文件名由服务端 filename 驱动，前端不经 blob/createObjectURL；形态守卫见
 * DOWNLOAD_PATH_PATTERN）。失败（409003 同线程同分钟跨轮碰撞 / 404001 / 网络）→
 * toast(normalizeError 服务端中文 message 直达)；菜单先行收起（与本地导出同交互）。
 */
async function exportReport(): Promise<void> {
  moreOpen.value = false
  try {
    const data = await exportTurnReport(props.turn.turnId)
    if (!DOWNLOAD_PATH_PATTERN.test(data.download_path)) {
      toast(t('answer.exportPathInvalid'))
      return
    }
    const anchor = document.createElement('a')
    anchor.href = data.download_path
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
  } catch (error) {
    toast(normalizeError(error).message)
  }
}

/* ---------- 1. 复制（失败 toast 一次性反馈，成功零打扰） ---------- */
async function copyAnswer(): Promise<void> {
  try {
    await navigator.clipboard.writeText(props.turn.answer_md)
  } catch (error) {
    console.warn('[AnswerActions] 复制失败', error)
    toast(t('answer.copyFailed'))
  }
}

/* ---------- 2. 分享 = 复制会话链接（origin + /thread/{threadId}；自用形态——已定案） ---------- */
async function shareThread(): Promise<void> {
  try {
    await navigator.clipboard.writeText(buildThreadLink(props.turn.threadId))
    toast(t('answer.linkCopied'))
  } catch (error) {
    console.warn('[AnswerActions] 分享链接复制失败', error)
    toast(t('answer.copyFailed'))
  }
}

/* ---------- 3/4. 反馈（互斥 + 同钮再点即取消——api.md §2.9 v1.17；aria-pressed 现值驱动） ---------- */
function feedback(helpful: boolean): void {
  // toggle：当前现值 === 本次点击值 → 发 null 清除（取消）；否则 upsert 改判
  const next = props.turn.feedbackHelpful === helpful ? null : helpful
  void store.sendFeedback(props.turn.turnId, next)
}
const helpfulPressed = computed(() => props.turn.feedbackHelpful === true)
const notHelpfulPressed = computed(() => props.turn.feedbackHelpful === false)

/* ---------- 7. 更多操作轻菜单（外点关闭） ---------- */
const moreOpen = ref(false)
const moreRootEl = ref<HTMLElement | null>(null)

function toggleMore(): void {
  moreOpen.value = !moreOpen.value
}

function onDocMousedown(e: MouseEvent): void {
  if (!moreOpen.value) return
  const target = e.target
  if (target instanceof Node && moreRootEl.value?.contains(target)) return
  moreOpen.value = false
}
onMounted(() => document.addEventListener('mousedown', onDocMousedown))
onUnmounted(() => document.removeEventListener('mousedown', onDocMousedown))

/**
 * 导出 Markdown（前端本地）：answer_md → Blob（text/markdown）→ a.download 触发下载
 * （实现抽取至 utils/exportThread.ts 共享——App 顶栏整线程导出同款；单答案行为不变）。
 * 文件名 = 问题原文（剔除文件系统非法字符）+.md；全程零网络（A40：不调深度专用 export 端点）。
 */
function exportMarkdown(): void {
  moreOpen.value = false
  downloadMarkdown(`${sanitizeFilename(props.turn.question, 'answer')}.md`, props.turn.answer_md)
}
</script>

<template>
  <div class="answer-actions" data-testid="answer-actions">
    <!-- 1. 复制 -->
    <UiTooltip :label="$t('answer.copyAnswer')">
      <button
        type="button"
        class="aa-btn"
        data-testid="act-copy"
        :aria-label="$t('answer.copyAnswer')"
        @click="copyAnswer"
      >
        <UiIcon name="i-copy" size="sm" />
      </button>
    </UiTooltip>

    <!-- 2. 分享 = 复制会话链接（自用无公开分享后端——已定案；成功/失败 toast 反馈） -->
    <UiTooltip :label="$t('answer.share')">
      <button
        type="button"
        class="aa-btn"
        data-testid="act-share"
        :aria-label="$t('answer.share')"
        @click="shareThread"
      >
        <UiIcon name="i-share" size="sm" />
      </button>
    </UiTooltip>

    <!-- 3/4. 有帮助 · 没有帮助（互斥；REQ-057 点按钮即发、乐观 UI） -->
    <UiTooltip :label="$t('answer.helpful')">
      <button
        type="button"
        class="aa-btn aa-labeled"
        :class="{ on: helpfulPressed }"
        data-testid="act-helpful"
        :aria-pressed="helpfulPressed ? 'true' : 'false'"
        :aria-label="$t('answer.helpful')"
        @click="feedback(true)"
      >
        <UiIcon name="i-thumb-up" size="sm" />
        <span class="aa-label">{{ $t('answer.helpful') }}</span>
      </button>
    </UiTooltip>
    <UiTooltip :label="$t('answer.notHelpful')">
      <button
        type="button"
        class="aa-btn aa-labeled"
        :class="{ on: notHelpfulPressed }"
        data-testid="act-not-helpful"
        :aria-pressed="notHelpfulPressed ? 'true' : 'false'"
        :aria-label="$t('answer.notHelpful')"
        @click="feedback(false)"
      >
        <UiIcon name="i-thumb-down" size="sm" />
        <span class="aa-label">{{ $t('answer.notHelpful') }}</span>
      </button>
    </UiTooltip>

    <!-- 5. 复刻（原问题原模式走既有追问流） -->
    <UiTooltip :label="$t('answer.rerun')">
      <button
        type="button"
        class="aa-btn aa-labeled"
        data-testid="act-rerun"
        :aria-label="$t('answer.rerun')"
        @click="emit('rerun', turn.question)"
      >
        <UiIcon name="i-replay" size="sm" />
        <span class="aa-label">{{ $t('answer.rerun') }}</span>
      </button>
    </UiTooltip>

    <!-- 8. 重新生成（v1.26 REQ-063/A47：复刻钮旁——degraded/failed-with-sources fast/deep 轮
         〔v1.30 放开 deep，api.md §2.12 前置 3 v1.28〕；点击 emit → AnswerView 承接
         store.regenerate，§3 生命周期 14 清空重渲约定〔deep 形态注 v1.30〕） -->
    <UiTooltip v-if="canRegenerate" :label="$t('answer.regenerate')">
      <button
        type="button"
        class="aa-btn aa-labeled"
        data-testid="act-regenerate"
        :aria-label="$t('answer.regenerate')"
        @click="emit('regenerate')"
      >
        <UiIcon name="i-replay" size="sm" />
        <span class="aa-label">{{ $t('answer.regenerate') }}</span>
      </button>
    </UiTooltip>

    <!-- 6. 来源（滚动定位 + 高亮来源面板；带计数） -->
    <UiTooltip :label="$t('answer.sources')">
      <button
        type="button"
        class="aa-btn aa-labeled"
        data-testid="act-sources"
        :aria-label="$t('answer.sources')"
        @click="emit('sources')"
      >
        <UiIcon name="i-source" size="sm" />
        <span v-if="turn.sources.length > 0" class="aa-label">{{ turn.sources.length }} {{ $t('answer.sourceCount') }}</span>
        <span v-else class="aa-label">{{ $t('answer.sources') }}</span>
      </button>
    </UiTooltip>

    <!-- 7. 更多操作（轻菜单） -->
    <div ref="moreRootEl" class="aa-more-host">
      <UiTooltip :label="$t('answer.moreActions')">
        <button
          type="button"
          class="aa-btn"
          data-testid="act-more"
          :aria-label="$t('answer.moreActions')"
          :aria-haspopup="moreOpen ? 'true' : undefined"
          :aria-expanded="moreOpen ? 'true' : 'false'"
          @click="toggleMore"
        >
          <UiIcon name="i-more" size="sm" />
        </button>
      </UiTooltip>
      <div v-if="moreOpen" class="aa-menu" data-testid="act-more-menu" role="menu" :aria-label="$t('answer.moreActions')">
        <button
          type="button"
          class="aa-menu-item"
          role="menuitem"
          data-testid="act-export-md"
          @click="exportMarkdown"
        >
          <UiIcon name="i-download" size="sm" />
          {{ $t('answer.exportMd') }}
        </button>
        <!-- ⑦b 服务端导出报告（批次 6：deep 轮 + exportAvailable 门控——A40 负向：fast 不渲染本项） -->
        <button
          v-if="canExportReport"
          type="button"
          class="aa-menu-item"
          role="menuitem"
          data-testid="act-export-report"
          @click="exportReport"
        >
          <UiIcon name="i-download" size="sm" />
          {{ $t('answer.exportReport') }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.answer-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-1);
  margin: var(--space-4) 0 var(--space-5);
}
.aa-btn {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  min-height: 32px;
  padding: 0 var(--space-2);
  border-radius: var(--radius-full);
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
  white-space: nowrap;
  transition: background var(--duration-fast) var(--ease-out), color var(--duration-fast) var(--ease-out);
}
.aa-btn:hover:not(:disabled) {
  background: var(--color-surface-ghost);
  color: var(--color-text-primary);
}
.aa-btn:disabled {
  cursor: default;
  color: var(--color-text-faint);
}
/* 选中态（反馈互斥；tokens accent 语义） */
.aa-btn.on {
  background: var(--color-accent-bg);
  color: var(--color-accent);
}
.aa-label {
  line-height: 1;
}
.aa-more-host {
  position: relative;
  display: inline-flex;
}
.aa-menu {
  position: absolute;
  top: calc(100% + var(--space-1));
  left: 0;
  z-index: 30;
  min-width: 180px;
  padding: var(--space-1);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-floating);
}
.aa-menu-item {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  width: 100%;
  min-height: 34px;
  padding: 0 var(--space-3);
  border-radius: var(--radius-sm);
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  text-align: left;
  transition: background var(--duration-fast) var(--ease-out), color var(--duration-fast) var(--ease-out);
}
.aa-menu-item:hover {
  background: var(--color-surface-ghost);
  color: var(--color-text-primary);
}
</style>
