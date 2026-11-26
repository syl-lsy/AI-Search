<script setup lang="ts">
/**
 * FeishuImportJobs — 飞书导入任务状态面（frontend-api.md §5 v1.33 FeishuImportJobs 行；
 * api.md §4.7~4.9 消费面；plan-m3-b6-f6 §1.1⑤/D7/D8）。
 *
 * 两分区（免页面裁决）：连接器状态卡（健康徽标 = 最近任务成败投影，D7——契约无独立连接器
 * 状态端点）+ 任务卡（进度五数 + failure_summary chips〔category 原样展示不解析，非封闭枚举〕
 * + failures 清单 details 展开→§4.9 详情请求〔展开态随轮询节律刷新——store tick 承载〕
 * + 上一次完成任务投影行；skipped 不静默，A53 GWT#3）。
 * 触发钮两态（409007 守卫前置）：idle=「飞书导入」可点 / 有 pending|running 任务或触发中
 * = disabled「导入进行中…」；触发走 kbStore.triggerImport()（api/kb.ts 唯一出口），
 * ApiError（409007 等）上抛 → toast 服务端 message（§4 kbStore 行）。
 * D8/OQ-2 清偿：feishu 双色值已合入 tokens.css（2026-11-26 用户裁决），消费全局 token。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import UiIcon from '@/components/icons/UiIcon.vue'
import { normalizeError } from '@/api/client'
import { toast } from '@/composables/toast'
import { useKbStore } from '@/stores/kb'
import type { KbImportJob } from '@/types/api'

const { t, locale } = useI18n()
const store = useKbStore()

onMounted(() => {
  store.fetchImportJobs().catch((err) => toast(normalizeError(err).message))
})

/** 最新任务 = 列表首条（§4.8 created_at DESC 新任务在前） */
const latestJob = computed<KbImportJob | null>(() => store.importJobs[0] ?? null)

/** 进行中任务（409007 同族判据：pending|running） */
const activeJob = computed<KbImportJob | null>(
  () => store.importJobs.find((j) => j.status === 'pending' || j.status === 'running') ?? null,
)

/** 任务卡展示对象：进行中优先（轮询对象），否则最新一条 */
const displayJob = computed<KbImportJob | null>(() => activeJob.value ?? latestJob.value)

/** 上一次完成任务投影（D7：无独立重试端点，恢复入口 = 触发钮重发） */
const lastCompleted = computed<KbImportJob | null>(
  () => store.importJobs.find((j) => j.status === 'completed') ?? null,
)

/** 健康徽标 = 最近任务成败投影（D7）：completed→健康 / failed→异常 / pending|running→进行中 / 无任务→未同步。
 * 圆点取色随徽标语义（CR-5/P3-3）：健康=dot-success 静态绿；进行中=dot-accent（accent 底 + pulse，
 * 与 LibraryView 处理中态同款先例）；danger/outline 无点。 */
const health = computed<{ class: string; text: string; dot: boolean; dotClass: string }>(() => {
  const status = latestJob.value?.status
  if (status === 'completed') return { class: 'badge-success', text: t('library.connectorHealthy'), dot: true, dotClass: 'dot-success' }
  if (status === 'failed') return { class: 'badge-danger', text: t('library.connectorUnhealthy'), dot: false, dotClass: '' }
  if (status === 'pending' || status === 'running') {
    return { class: 'badge-accent', text: t('library.connectorRunning'), dot: true, dotClass: 'dot-accent' }
  }
  return { class: 'badge-outline', text: t('library.connectorNotSynced'), dot: false, dotClass: '' }
})

function triggerLabel(job: KbImportJob): string {
  return job.trigger === 'scheduled' ? t('library.jobTriggerScheduled') : t('library.jobTriggerManual')
}

/** 触发：POST §4.7；409007 等上抛 toast（视图层）；进行中（disabled 态）点击为 no-op 守卫 */
async function onTrigger(): Promise<void> {
  if (activeJob.value !== null || store.importTriggering) return
  try {
    await store.triggerImport()
  } catch (err) {
    toast(normalizeError(err).message)
  }
}

/** 进度百分比（total=0〔仅删除类定时任务〕→ 0% 防除零） */
function pctOf(job: KbImportJob): number {
  if (job.total_docs <= 0) return 0
  return Math.min(100, Math.round((job.processed_docs / job.total_docs) * 100))
}

/** failures 清单条目数 = 失败 + 跳过（A53 GWT#3：skip 类条目在清单内、不静默） */
function failureCountOf(job: KbImportJob): number {
  return job.failed_docs + job.skipped_docs
}

/** 展开中的详情归属当前任务才渲染（防串卡） */
function showDetail(job: KbImportJob): boolean {
  return store.importJobDetail !== null && store.importJobDetail.job_id === job.job_id
}

/**
 * details 展开/收起（CR-3/P3-1）：展开 → §4.9 详情请求（store tick 以 importJobDetail 非空为判据
 * 随节律刷新）；收起 → 走 store.clearImportJobDetail() 动作（R-4/F-7：置空 + 失配在途请求——
 * 在途响应落地不回写、tick 判据不被重新点亮）。
 */
function onDetailToggle(e: Event, job: KbImportJob): void {
  if (!(e.target instanceof HTMLDetailsElement)) return
  if (!e.target.open) {
    store.clearImportJobDetail()
    return
  }
  store.fetchImportJobDetail(job.job_id).catch(() => toast(t('library.jobDetailFailed')))
}

/** 相对时间（近期同步投影；非法/缺省 → 空串） */
function fmtRel(iso: string | null): string {
  if (iso === null) return ''
  const then = new Date(iso).getTime()
  if (!Number.isFinite(then)) return ''
  const diff = Date.now() - then
  const tag = locale.value === 'en-US' ? 'en' : 'zh-CN'
  const rtf = new Intl.RelativeTimeFormat(tag, { numeric: 'auto' })
  const minutes = Math.round(diff / 60_000)
  if (Math.abs(minutes) < 60) return rtf.format(-minutes, 'minute')
  const hours = Math.round(minutes / 60)
  if (Math.abs(hours) < 24) return rtf.format(-hours, 'hour')
  return rtf.format(-Math.round(hours / 24), 'day')
}

/** 最近同步只读行（D7 投影态，无独立状态端点） */
const latestSyncText = computed<string>(() => {
  const job = latestJob.value
  if (job === null) return t('library.connectorNoJob')
  const status =
    job.status === 'completed' || job.status === 'failed'
      ? fmtRel(job.finished_at)
      : t('library.connectorRunning')
  return `${triggerLabel(job)} · ${status}`
})

/** 变更摘要（最近任务五数投影） */
function summaryText(job: KbImportJob): string {
  return [
    t('library.importTotal', { n: job.total_docs }),
    t('library.importImported', { n: job.imported_docs }),
    t('library.importFailedCount', { n: job.failed_docs }),
    t('library.importSkippedCount', { n: job.skipped_docs }),
  ].join(' · ')
}

/** failures 清单 details 元素（R-3/F-2：displayJob 切换时程序化收起） */
const failuresDetailsEl = ref<HTMLDetailsElement | null>(null)

/**
 * displayJob 切换（R-3/F-2）：进行中新任务顶替/任务结束换投影时，展开态不跨任务继承——
 * 程序化收起 details + 清 importJobDetail（走 R-4 动作），杜绝 showDetail(新任务) 恒 false
 * 的「展开但空白」态（不自动发新任务详情请求——展开语义仍归用户点击）。
 */
watch(
  () => displayJob.value?.job_id,
  (jobId, prev) => {
    if (prev === undefined || jobId === prev) return
    if (failuresDetailsEl.value !== null) failuresDetailsEl.value.open = false
    store.clearImportJobDetail()
  },
)
</script>

<template>
  <div class="feishu-zone">
    <!-- ⑤ 连接器状态卡（仅状态反馈面，无配置表单——凭据只进 .env，REQ-067 免页面裁决） -->
    <div class="conn-card" data-testid="kb-feishu-card">
      <span class="conn-ico"><UiIcon name="i-feishu" /></span>
      <div class="conn-txt">
        <h3>
          {{ $t('library.connectorTitle') }}
          <span class="badge" :class="health.class">
            <span v-if="health.dot" class="dot" :class="health.dotClass" style="width: 6px; height: 6px"></span>
            {{ health.text }}
          </span>
        </h3>
        <div class="conn-meta">
          <div>
            <span class="k">{{ $t('library.connectorMetaLatest') }}：</span>{{ latestSyncText }}
          </div>
          <div v-if="lastCompleted !== null">
            <span class="k">{{ $t('library.connectorMetaSummary') }}：</span>{{ summaryText(lastCompleted) }}
          </div>
          <div><span class="k">{{ $t('library.connectorMetaMode') }}</span></div>
        </div>
      </div>
      <div class="conn-side">
        <!-- 触发钮两态（409007 守卫前置）：idle 可点 / 进行中 disabled -->
        <button
          type="button"
          class="btn btn-bordered btn-sm"
          data-testid="kb-import-trigger"
          :disabled="activeJob !== null || store.importTriggering"
          @click="onTrigger"
        >
          <UiIcon :name="activeJob !== null || store.importTriggering ? 'i-refresh' : 'i-feishu'" size="sm" />
          {{ activeJob !== null || store.importTriggering ? $t('library.importRunning') : $t('library.importFeishu') }}
        </button>
      </div>
    </div>

    <!-- ⑤b 任务卡（进度五数 + 失败分类 chips + 失败清单 details + 上一次完成投影） -->
    <div v-if="displayJob !== null" class="job-card" data-testid="kb-import-jobs">
      <div class="job-head">
        <h4>{{ $t('library.importFeishu') }} · {{ triggerLabel(displayJob) }}</h4>
        <span class="badge badge-outline">{{ displayJob.status }}</span>
        <span v-if="activeJob !== null" class="badge badge-accent">
          <span class="dot dot-accent" style="width: 6px; height: 6px"></span>{{ $t('library.jobPollingBadge') }}
        </span>
      </div>
      <div class="pbar-row">
        <div
          class="pbar"
          role="progressbar"
          :aria-valuenow="displayJob.processed_docs"
          aria-valuemin="0"
          :aria-valuemax="displayJob.total_docs"
          :aria-label="$t('library.importFeishu')"
        >
          <i :style="{ width: `${pctOf(displayJob)}%` }"></i>
        </div>
        <span class="pbar-num">{{ displayJob.processed_docs }} / {{ displayJob.total_docs }} · {{ pctOf(displayJob) }}%</span>
      </div>
      <div class="job-nums">
        <span>{{ $t('library.importTotal', { n: displayJob.total_docs }) }}</span>
        <span>{{ $t('library.importProcessed', { n: displayJob.processed_docs }) }}</span>
        <span>{{ $t('library.importImported', { n: displayJob.imported_docs }) }}</span>
        <span>{{ $t('library.importFailedCount', { n: displayJob.failed_docs }) }}</span>
        <span>{{ $t('library.importSkippedCount', { n: displayJob.skipped_docs }) }}</span>
      </div>
      <div v-if="displayJob.failure_summary.length > 0" class="fchip-row">
        <span class="fchip-label">{{ $t('library.jobFailuresLabel') }}：</span>
        <span
          v-for="s in displayJob.failure_summary"
          :key="s.category"
          class="fchip"
          :class="{ skip: s.category === 'unsupported_type' }"
        >{{ s.category }} ×{{ s.count }}</span>
      </div>
      <details
        v-if="failureCountOf(displayJob) > 0"
        ref="failuresDetailsEl"
        class="failures"
        data-testid="kb-job-failures"
        @toggle="onDetailToggle($event, displayJob)"
      >
        <summary>
          <UiIcon name="i-chev-d" size="sm" />
          {{ $t('library.jobFailuresList', { n: failureCountOf(displayJob) }) }}
        </summary>
        <template v-if="showDetail(displayJob)">
          <div v-for="(f, i) in store.importJobDetail?.failures" :key="i" class="fail-row">
            <span class="fail-title">{{ f.title }}</span>
            <span class="fchip" :class="{ skip: f.category === 'unsupported_type' }">{{ f.category }}</span>
            <span class="fail-msg">{{ f.message }}</span>
          </div>
        </template>
      </details>
      <div v-if="lastCompleted !== null" class="job-prev" data-testid="kb-job-prev">
        <div class="job-prev-line">
          <UiIcon name="i-clock" size="sm" />
          {{ $t('library.jobPrev') }}：{{ triggerLabel(lastCompleted) }} · {{ summaryText(lastCompleted) }}
          <span class="badge badge-success"><UiIcon name="i-check" size="sm" />completed</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* D8/OQ-2 清偿：--color-feishu/--color-feishu-bg 双值已合入 tokens.css（2026-11-26 用户裁决——
   「以 tokens.css 全局 token 元素视觉为准」），本组件直接消费全局 token，零组件级色值。 */
.conn-card {
  display: flex;
  align-items: flex-start;
  gap: var(--space-4);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--space-4) var(--space-5);
  margin-bottom: var(--space-4);
}
.conn-ico {
  width: 40px;
  height: 40px;
  flex: none;
  border-radius: var(--radius-md);
  display: grid;
  place-items: center;
  background: var(--color-feishu-bg);
  color: var(--color-feishu);
}
.conn-txt {
  min-width: 0;
  flex: 1;
}
.conn-txt h3 {
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.conn-meta {
  margin-top: var(--space-1);
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
  line-height: 1.7;
}
.conn-meta .k {
  color: var(--color-text-secondary);
}
.conn-side {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: var(--space-2);
  flex: none;
}
.job-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--space-4) var(--space-5);
  margin-bottom: var(--space-8);
}
.job-head {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
}
.job-head h4 {
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
}
.pbar-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  margin-top: var(--space-3);
}
.pbar {
  flex: 1;
  height: 6px;
  border-radius: var(--radius-full);
  background: var(--color-surface-active);
  overflow: hidden;
}
.pbar > i {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: var(--color-accent);
  transition: width var(--duration-base) var(--ease-out);
}
.pbar-num {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.job-nums {
  display: flex;
  gap: var(--space-4);
  flex-wrap: wrap;
  margin-top: var(--space-2);
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
}
.fchip-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
  margin-top: var(--space-3);
}
.fchip-label {
  color: var(--color-text-tertiary);
  font-size: var(--text-xs);
}
.fchip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 22px;
  padding: 0 8px;
  border-radius: var(--radius-full);
  background: var(--color-danger-bg);
  color: var(--color-danger);
  font-size: var(--text-xs);
  font-family: var(--font-mono);
}
.fchip.skip {
  background: var(--color-warning-bg);
  color: var(--color-warning);
}
.failures {
  margin-top: var(--space-3);
  border-top: 1px solid var(--color-border-faint);
  padding-top: var(--space-2);
}
.failures summary {
  cursor: pointer;
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  list-style: none;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-height: 32px;
}
.failures summary::-webkit-details-marker {
  display: none;
}
.fail-row {
  display: flex;
  align-items: baseline;
  gap: var(--space-3);
  padding: var(--space-2) 0;
  border-bottom: 1px solid var(--color-border-faint);
  font-size: var(--text-sm);
}
.fail-row:last-child {
  border-bottom: 0;
}
.fail-title {
  color: var(--color-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 260px;
}
.fail-msg {
  color: var(--color-text-tertiary);
  font-size: var(--text-xs);
  flex: 1;
  min-width: 0;
}
.job-prev {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding-top: var(--space-3);
  margin-top: var(--space-1);
  border-top: 1px dashed var(--color-border);
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
}
.job-prev-line {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}
</style>
