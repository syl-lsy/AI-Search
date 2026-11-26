<script setup lang="ts">
/**
 * LibraryView（§1 LibraryView 行 + plan-m3-b6-f6 §1.2 五面接入；设计稿 05-library-m3）。
 * 页序：页头 → 飞书连接器卡/导入任务卡（FeishuImportJobs）→ 检索融合说明卡 → 多选上传拖拽区
 * → URL 收藏行 → 六态图例 → 文档表（六态徽标 + DocStatusStepper 步进条 + parse_meta 列 +
 * catalog_path 副行〔仅飞书〕+ failed stage-tag/retry-link）→ 表底注。
 * 入口拒绝（413001/415001）直接 toast、不入表（§1）；多选批量 = input multiple + 逐文件校验循环
 * （D2：端点本为单文件粒度，零契约增量；拒绝逐条 toast，队列卡不实现）；删除 = 级联确认流
 * （KbDeleteConfirmModal，api.md §4.5 二次确认 + 三存储提示，store 只管 DELETE + 刷新）；
 * failed「重新上传」= retry-link 打开文件选择器走 §4.1 新上传（OQ-5：新文档新 ID，旧行保留）。
 */
import { onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import DocStatusStepper from '@/components/library/DocStatusStepper.vue'
import FeishuImportJobs from '@/components/library/FeishuImportJobs.vue'
import KbDeleteConfirmModal from '@/components/library/KbDeleteConfirmModal.vue'
import KbDocPagination from '@/components/library/KbDocPagination.vue'
import UiIcon from '@/components/icons/UiIcon.vue'
import { normalizeError } from '@/api/client'
import { toast } from '@/composables/toast'
import { parseFailStage, parseMetaSegments } from '@/utils/kbDisplay'
import { useKbStore } from '@/stores/kb'
import type { KbDocStatus, KbDocType, KbDocument, KbParseMeta } from '@/types/api'

const { t, locale } = useI18n()
const store = useKbStore()

const MAX_UPLOAD_BYTES = 20 * 1024 * 1024
/** 上传类型白名单（安全铁律：PDF/MD/TXT/DOCX · ≤20MB · OCR 不支持——设计稿/安全口径） */
const ACCEPT_TYPES = '.pdf,.md,.txt,.docx'
const ALLOWED_EXT = ['pdf', 'md', 'txt', 'docx']

const fileInput = ref<HTMLInputElement | null>(null)
const urlInputEl = ref<HTMLInputElement | null>(null)
const dragOver = ref(false)
const urlInput = ref('')
const collecting = ref(false)
/** 删除级联确认流：待删文档（确认前只记录，不发 DELETE） */
const pendingDelete = ref<KbDocument | null>(null)

/** 表格头锚点（R-3：翻页后滚动回表格顶部——新页内容首屏可见） */
const docHeadEl = ref<HTMLElement | null>(null)

/**
 * 翻页（R-3 分页控件 @change 接线）：store.goToPage（边界钳制/防重入在 store；错误上抛视图
 * toast——Library 域惯例）；成功后滚动回表格头（新页内容首屏可见）。
 */
async function onPageChange(page: number): Promise<void> {
  try {
    await store.goToPage(page)
  } catch (err) {
    toast(normalizeError(err).message)
  }
  docHeadEl.value?.scrollIntoView({ block: 'start' })
}

/** 六态图例（KbDocStatus v1.33 全集，plan §1.2 ⑥） */
const LEGEND_STATUSES: readonly KbDocStatus[] = ['pending', 'normalizing', 'chunking', 'indexing', 'ready', 'failed']

function statusBadgeClass(status: KbDocStatus): string {
  if (status === 'ready') return 'badge-success'
  if (status === 'failed') return 'badge-danger'
  return 'badge-warning'
}

function statusText(status: KbDocStatus): string {
  switch (status) {
    case 'ready':
      return t('library.statusReady')
    case 'failed':
      return t('library.statusFailed')
    case 'pending':
      return t('library.statusPending')
    case 'normalizing':
      return t('library.statusNormalizing')
    case 'chunking':
      return t('library.statusChunking')
    case 'indexing':
      return t('library.statusIndexing')
  }
}

function docTypeIcon(docType: KbDocType): string {
  return docType === 'url' ? 'i-link' : 'i-file'
}

function docTypeLabel(docType: KbDocType): string {
  return docType.toUpperCase()
}

function docTypeClass(docType: KbDocType): string {
  return `f-${docType}`
}

/**
 * 解析摘要列文案（CR-4/P3-2 i18n 化）：parse_meta 结构化段 → 既有 answer.kbParse* 键渲染——
 * 与 KbChunkDrilldown chips 同键同语言（en-US 下不再出现硬编码中文「页/表/码」）；
 * null 段不渲染（pages=null 只产表/码两段）；parse_meta=null → 返回 null（视图层渲染 —）。
 */
const PARSE_META_I18N_KEYS = {
  pages: 'answer.kbParsePages',
  tables: 'answer.kbParseTables',
  code: 'answer.kbParseCode',
} as const

function parseMetaText(meta: KbParseMeta | null | undefined): string | null {
  const segments = parseMetaSegments(meta)
  if (segments === null) return null
  return segments.map((s) => t(PARSE_META_I18N_KEYS[s.key], { n: s.n })).join('·')
}

function formatSize(bytes: number | null): string {
  if (bytes === null) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function formatTime(iso: string): string {
  const then = new Date(iso).getTime()
  if (!Number.isFinite(then)) return ''
  const diff = Date.now() - then
  const tag = locale.value === 'en-US' ? 'en' : 'zh-CN'
  const rtf = new Intl.RelativeTimeFormat(tag, { numeric: 'auto' })
  const minutes = Math.round(diff / 60_000)
  if (Math.abs(minutes) < 60) return rtf.format(-minutes, 'minute')
  const hours = Math.round(minutes / 60)
  if (Math.abs(hours) < 24) return rtf.format(-hours, 'hour')
  const days = Math.round(hours / 24)
  return rtf.format(-days, 'day')
}

function pickFile(): void {
  fileInput.value?.click()
}

async function onFileChosen(e: Event): Promise<void> {
  const input = e.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  input.value = '' // 允许重选同一文件
  if (files.length === 0) return
  // 多选批量：逐文件校验循环（D2——拒绝逐条 toast，不中断合法文件批次）
  for (const file of files) {
    await doUpload(file)
  }
}

async function doUpload(file: File): Promise<void> {
  const ext = file.name.split('.').pop()?.toLowerCase() ?? ''
  if (!ALLOWED_EXT.includes(ext)) {
    toast(t('library.badType'))
    return
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    toast(t('library.badSize'))
    return
  }
  try {
    await store.upload(file, file.name)
  } catch (err) {
    // 入口拒绝（413001/415001）等：直接 toast，不入表（§1）
    toast(normalizeError(err).message)
  }
}

function onDrop(e: DragEvent): void {
  dragOver.value = false
  const files = Array.from(e.dataTransfer?.files ?? [])
  if (files.length === 0) return
  void (async () => {
    for (const file of files) {
      await doUpload(file)
    }
  })()
}

async function onCollectUrl(): Promise<void> {
  const url = urlInput.value.trim()
  if (!url || collecting.value) return
  // URL 收藏校验 ^https?://（§2 协议白名单口径同源）
  if (!/^https?:\/\//i.test(url)) {
    toast(t('library.badUrl'))
    return
  }
  collecting.value = true
  try {
    await store.collectUrl(url)
    urlInput.value = ''
  } catch (err) {
    toast(normalizeError(err).message)
  } finally {
    collecting.value = false
  }
}

/** 删除入口：只开确认弹层（api.md §4.5 二次确认；级联范围三存储由弹层提示） */
function onDeleteClick(doc: KbDocument): void {
  pendingDelete.value = doc
}

/** 确认删除：store.remove（DELETE + 列表刷新）；失败 toast、弹层关闭 */
async function onConfirmDelete(): Promise<void> {
  const doc = pendingDelete.value
  pendingDelete.value = null
  if (doc === null) return
  try {
    await store.remove(doc.document_id)
  } catch (err) {
    toast(normalizeError(err).message)
  }
}

onMounted(() => {
  store.pollUntilSettled().catch((err) => toast(normalizeError(err).message))
})

onUnmounted(() => {
  store.stopPolling()
})
</script>

<template>
  <section class="page" data-testid="library-view">
    <div class="page-inner">
      <div class="page-head">
        <div>
          <h1 class="page-title">{{ $t('library.title') }}</h1>
          <p class="page-sub">{{ $t('library.subtitle') }}</p>
        </div>
        <div class="page-actions">
          <button type="button" class="btn btn-bordered" @click="urlInputEl?.focus()">
            <UiIcon name="i-link" size="sm" />{{ $t('library.collectUrl') }}
          </button>
          <button type="button" class="btn btn-primary" data-testid="library-upload" @click="pickFile">
            <UiIcon name="i-upload" size="sm" />{{ $t('library.upload') }}
          </button>
        </div>
      </div>

      <!-- ⑤ 飞书连接器状态卡 + 导入任务卡（FeishuImportJobs，plan §1.2 页序②③） -->
      <FeishuImportJobs />

      <!-- 检索融合开关说明卡（REQ-021）：开关为设计稿图形，后端开关端点未落位——留位不接状态 -->
      <div class="fusion-card">
        <span class="fusion-ico"><UiIcon name="i-db" /></span>
        <div class="fusion-txt">
          <h3>
            {{ $t('library.fusionTitle') }}
            <span class="badge badge-accent">{{ $t('library.fusionBadge') }}</span>
          </h3>
          <p>{{ $t('library.fusionDesc') }}</p>
        </div>
        <div class="fusion-switch" :title="$t('library.fusionPending')">
          <span class="switch" aria-hidden="true"></span>
        </div>
      </div>

      <!-- ① 上传拖拽区（REQ-019；多选批量） -->
      <div
        class="upload-zone"
        :class="{ over: dragOver }"
        role="button"
        tabindex="0"
        data-testid="upload-zone"
        @click="pickFile"
        @keydown.enter="pickFile"
        @dragover.prevent="dragOver = true"
        @dragleave="dragOver = false"
        @drop.prevent="onDrop"
      >
        <span class="uz-ico"><UiIcon name="i-upload" /></span>
        <div class="uz-main">{{ $t('library.dropMain') }}</div>
        <div class="uz-hint">{{ $t('library.dropHint') }}</div>
        <input
          ref="fileInput"
          type="file"
          multiple
          :accept="ACCEPT_TYPES"
          hidden
          data-testid="upload-input"
          @change="onFileChosen"
        />
      </div>

      <!-- URL 收藏入库（REQ-020） -->
      <div class="url-row">
        <label class="url-input">
          <UiIcon name="i-link" size="sm" />
          <input
            ref="urlInputEl"
            v-model="urlInput"
            type="text"
            :placeholder="$t('library.urlPlaceholder')"
            data-testid="url-input"
            @keydown.enter="onCollectUrl"
          />
        </label>
        <button
          type="button"
          class="btn btn-bordered url-btn"
          :disabled="collecting"
          data-testid="url-collect-btn"
          @click="onCollectUrl"
        >
          {{ $t('library.collectBtn') }}
        </button>
      </div>

      <!-- 文档列表（REQ-022） -->
      <div ref="docHeadEl" class="doc-head">
        <h2>{{ $t('library.docTitle') }} <span class="doc-count">{{ store.docsTotal }} · {{ store.readyTotal }} {{ $t('library.readyCount') }}</span></h2>
      </div>
      <!-- 六态图例（KbDocStatus v1.33 全集，plan §1.2 ⑥） -->
      <div class="legend" data-testid="kb-legend">
        <span class="lg-lab">{{ $t('library.legendLabel') }}：</span>
        <span v-for="s in LEGEND_STATUSES" :key="s" class="badge" :class="statusBadgeClass(s)">
          <span v-if="s === 'normalizing' || s === 'chunking' || s === 'indexing'" class="dot dot-warning" style="width: 6px; height: 6px"></span>
          <UiIcon v-else-if="s === 'ready'" name="i-check" size="sm" />
          <UiIcon v-else-if="s === 'failed'" name="i-alert" size="sm" />
          {{ statusText(s) }}
        </span>
      </div>
      <div class="table-wrap doc-table-wrap">
        <table class="tbl doc-table">
          <thead>
            <tr>
              <th style="width: 26%">{{ $t('library.colName') }}</th>
              <th class="col-num">{{ $t('library.colSize') }}</th>
              <th class="col-num">{{ $t('library.colChunks') }}</th>
              <th style="width: 13%">{{ $t('library.colParseMeta') }}</th>
              <th class="col-status" style="width: 20%">{{ $t('library.colStatus') }}</th>
              <th class="col-time">{{ $t('library.colTime') }}</th>
              <th class="col-num" style="width: 56px">{{ $t('library.colAction') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="store.documents.length === 0">
              <td colspan="7"><div class="empty-hint">{{ $t('library.empty') }}</div></td>
            </tr>
            <tr v-for="doc in store.documents" :key="doc.document_id" :data-doc-id="doc.document_id" data-testid="kb-doc-row">
              <td>
                <div class="name-cell">
                  <span class="doc-ico" :class="docTypeClass(doc.doc_type)">
                    <UiIcon :name="docTypeIcon(doc.doc_type)" size="sm" />
                    <template v-if="doc.doc_type !== 'url'">{{ docTypeLabel(doc.doc_type) }}</template>
                  </span>
                  <div>
                    <div class="doc-name">{{ doc.title }}</div>
                    <div v-if="doc.doc_type === 'url' && doc.source_url" class="doc-sub">{{ doc.source_url }}</div>
                    <!-- catalog_path 目录链副行（仅飞书来源非空渲染；本地上传/URL 恒 null，api.md §4.3） -->
                    <div v-if="doc.catalog_path" class="doc-sub crumb">
                      <UiIcon name="i-feishu" size="sm" class="crumb-ico" />
                      <span>{{ doc.catalog_path }}</span>
                    </div>
                  </div>
                </div>
              </td>
              <td class="col-num">{{ formatSize(doc.file_size_bytes) }}</td>
              <td class="col-num">{{ doc.chunk_count > 0 ? doc.chunk_count : '—' }}</td>
              <!-- 解析摘要列（parse_meta：i18n 键拼段，null 段不渲染；normalizing 完成前 null → —） -->
              <td>
                <span v-if="parseMetaText(doc.parse_meta) !== null" class="pm">{{ parseMetaText(doc.parse_meta) }}</span>
                <span v-else class="pm-null">—</span>
              </td>
              <td class="status-cell col-status">
                <span class="badge" :class="statusBadgeClass(doc.status)">
                  <span v-if="doc.status === 'normalizing' || doc.status === 'chunking' || doc.status === 'indexing'" class="dot dot-warning" style="width: 6px; height: 6px"></span>
                  <UiIcon v-else-if="doc.status === 'ready'" name="i-check" size="sm" />
                  <UiIcon v-else-if="doc.status === 'failed'" name="i-alert" size="sm" />
                  {{ statusText(doc.status) }}
                </span>
                <!-- 阶段步进条（pending 由徽标承载不渲染） -->
                <DocStatusStepper :status="doc.status" :fail-reason="doc.fail_reason" />
                <!-- failed：fail_reason 阶段化常显（D4/OQ-1：A37 页面可见 + 读屏可达）+ 重新上传（OQ-5） -->
                <template v-if="doc.status === 'failed' && doc.fail_reason">
                  <div class="status-reason" data-testid="kb-doc-fail-reason">
                    <span v-if="parseFailStage(doc.fail_reason).stage" class="stage-tag">{{ parseFailStage(doc.fail_reason).stage }}</span>
                    {{ parseFailStage(doc.fail_reason).detail }}
                  </div>
                  <button type="button" class="retry-link" data-testid="kb-doc-retry" @click="pickFile">
                    <UiIcon name="i-refresh" size="sm" />{{ $t('library.retryUpload') }}
                  </button>
                </template>
              </td>
              <td class="col-time">{{ formatTime(doc.created_at) }}</td>
              <td class="col-num">
                <button
                  type="button"
                  class="icon-btn row-action"
                  :title="$t('library.delete')"
                  :aria-label="`${$t('library.delete')} ${doc.title}`"
                  data-testid="kb-doc-delete"
                  @click="onDeleteClick(doc)"
                >
                  <UiIcon name="i-trash" size="sm" />
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <!-- 页码分页控件（R-3 用户裁决：可选页码分页；totalPages>1 才渲染，翻页/加载中 disabled） -->
      <KbDocPagination
        v-if="store.totalPages > 1"
        :page="store.currentPage"
        :total-pages="store.totalPages"
        :disabled="store.loadingPage"
        @change="onPageChange"
      />
      <div class="empty-hint">{{ $t('library.footHint') }}</div>
    </div>

    <!-- 删除级联确认流（api.md §4.5；ThreadModal 壳 alertdialog + 三存储清单） -->
    <KbDeleteConfirmModal
      :open="pendingDelete !== null"
      :doc="pendingDelete"
      @close="pendingDelete = null"
      @confirm="onConfirmDelete"
    />
  </section>
</template>

<style scoped>
.page {
  flex: 1;
  overflow-y: auto;
  min-height: 0;
}
.page-inner {
  max-width: 960px;
  margin: 0 auto;
  padding: var(--space-8) var(--space-8) var(--space-16);
}
.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
  margin-bottom: var(--space-6);
}
.page-title {
  font-size: var(--text-2xl);
  line-height: var(--text-2xl--lh);
  font-weight: var(--weight-semibold);
}
.page-sub {
  margin-top: var(--space-1);
  color: var(--color-text-tertiary);
  font-size: var(--text-base);
}
.page-actions {
  display: flex;
  gap: var(--space-3);
  flex: none;
}
.fusion-card {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--space-4) var(--space-5);
  margin-bottom: var(--space-6);
}
.fusion-ico {
  width: 40px;
  height: 40px;
  flex: none;
  border-radius: var(--radius-md);
  display: grid;
  place-items: center;
  background: var(--color-accent-bg);
  color: var(--color-accent);
}
.fusion-txt h3 {
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.fusion-txt p {
  margin-top: 2px;
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
  line-height: 1.6;
}
.fusion-switch {
  margin-left: auto;
  flex: none;
}
.switch {
  width: 40px;
  height: 22px;
  border-radius: var(--radius-full);
  background: var(--color-accent);
  position: relative;
  display: inline-block;
}
.switch::after {
  content: '';
  position: absolute;
  top: 3px;
  right: 3px;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: #fff;
}
.upload-zone {
  border: 1.5px dashed var(--color-border-strong);
  border-radius: var(--radius-lg);
  background: var(--color-bg-primary);
  padding: var(--space-8) var(--space-6);
  display: grid;
  justify-items: center;
  gap: var(--space-2);
  text-align: center;
  cursor: pointer;
  transition: border-color var(--duration-base) var(--ease-out), background var(--duration-base) var(--ease-out);
  margin-bottom: var(--space-4);
}
.upload-zone:hover,
.upload-zone.over {
  border-color: var(--color-accent-border);
  background: var(--color-accent-bg);
}
.upload-zone .uz-ico {
  width: 44px;
  height: 44px;
  border-radius: var(--radius-full);
  display: grid;
  place-items: center;
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  color: var(--color-text-secondary);
  margin-bottom: var(--space-1);
}
.uz-main {
  font-size: var(--text-base);
  color: var(--color-text-primary);
}
.uz-hint {
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}
.url-row {
  display: flex;
  gap: var(--space-3);
  margin-bottom: var(--space-8);
}
.url-input {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-full);
  padding: 0 var(--space-4);
  height: 44px;
  color: var(--color-text-tertiary);
  font-size: var(--text-base);
}
.url-input input {
  flex: 1;
  min-width: 0;
  background: none;
  border: 0;
  outline: none;
  color: var(--color-text-primary);
}
.url-input input::placeholder {
  color: var(--color-text-faint);
}
.url-btn {
  height: 44px;
  min-width: 0;
}
.doc-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--space-3);
}
.doc-head h2 {
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
  color: var(--color-text-primary);
}
.doc-count {
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
}
/* 六态图例（设计稿 .legend） */
.legend {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
  margin-bottom: var(--space-3);
}
.legend .lg-lab {
  color: var(--color-text-tertiary);
  font-size: var(--text-xs);
  margin-right: var(--space-1);
}
.doc-table td .name-cell {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  min-width: 0;
}
.doc-ico {
  width: 32px;
  height: 32px;
  flex: none;
  border-radius: var(--radius-sm);
  display: grid;
  place-items: center;
  font-size: 10px;
  font-weight: 600;
  position: relative;
}
.f-pdf {
  background: var(--color-danger-bg);
  color: var(--color-danger);
}
.f-md {
  background: var(--color-accent-bg);
  color: var(--color-accent);
}
.f-txt {
  background: var(--color-surface-active);
  color: var(--color-text-secondary);
}
.f-docx {
  background: var(--color-success-bg);
  color: var(--color-success);
}
.f-url {
  background: var(--color-kb-bg);
  color: var(--color-kb);
}
.doc-name {
  font-weight: var(--weight-medium);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 280px;
}
.doc-sub {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  margin-top: 1px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 280px;
}
/* catalog_path 目录链副行（仅飞书来源；feishu 图标消费全局 token——D8/OQ-2 清偿：已合入 tokens.css 2026-11-26） */
.doc-sub.crumb {
  display: flex;
  align-items: center;
  gap: 4px;
}
.doc-sub.crumb .crumb-ico {
  color: var(--color-feishu);
  flex: none;
}
/* 解析摘要列（设计稿 .pm/.pm-null） */
.pm {
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.pm-null {
  color: var(--color-text-faint);
  font-size: var(--text-xs);
}
/* P1-B 列预算重排：时间/数字列不可压缩（防「11小时前」竖排截断）；
   状态列 20%（步进条双行堆叠）；名称列弹性 + ellipsis；溢出走 wrap 横向滚动兜底 */
.col-time,
.col-num {
  white-space: nowrap;
}
.status-cell {
  min-width: 170px;
}
/* failed fail_reason 阶段化常显（D4/OQ-1）：stage-tag mono 小标签 + detail */
.status-reason {
  margin-top: var(--space-1);
  font-size: var(--text-xs);
  color: var(--color-danger);
  line-height: 1.6;
  max-width: 240px;
}
.status-reason .stage-tag {
  font-family: var(--font-mono);
  font-size: 11px;
  background: var(--color-surface-active);
  border-radius: 4px;
  padding: 0 4px;
}
.retry-link {
  margin-top: var(--space-1);
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--color-accent);
  font-size: var(--text-xs);
}
.retry-link:hover {
  color: var(--color-accent-hover);
}
.row-action {
  color: var(--color-text-tertiary);
  width: 32px;
  height: 32px;
}
.row-action:hover {
  color: var(--color-danger);
}
.empty-hint {
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
  padding: var(--space-4);
  text-align: center;
}
/* P1-B 窄屏兜底推广到全宽：表格列总预算 > 容器宽时横向滚动（原仅 ≤880px 断点生效，
   用户截图实证桌面 832px 内容宽下操作列出视口）——min-width 保证列不被挤压出视口 */
.doc-table-wrap {
  overflow-x: auto;
}
.doc-table {
  min-width: 760px;
}
@media (max-width: 880px) {
  .page-inner {
    padding: var(--space-6) var(--space-4) var(--space-12);
  }
  .page-head {
    flex-direction: column;
  }
  .url-row {
    flex-direction: column;
  }
}
</style>
