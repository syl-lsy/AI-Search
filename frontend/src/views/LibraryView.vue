<script setup lang="ts">
/**
 * LibraryView（§1 LibraryView 行：kb 文档列表/上传/URL 收藏/删除 + 有非终态条目时 5s 轮询）。
 * 设计稿 03-library：页头双动作 + 检索融合说明卡（REQ-021 开关图形）+ 上传拖拽区 + URL 收藏行 + 文档表。
 * 入口拒绝（413001/415001）直接 toast、不入表（§1）。
 * 留位：检索融合开关为设计稿图形（REQ-021 后端开关端点未在本批 §1 映射内——开关静态呈现，不接状态）。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import UiIcon from '@/components/icons/UiIcon.vue'
import { normalizeError } from '@/api/client'
import { toast } from '@/composables/toast'
import { useKbStore } from '@/stores/kb'
import type { KbDocStatus, KbDocType } from '@/types/api'

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

const readyCount = computed(() => store.documents.filter((d) => d.status === 'ready').length)

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
    case 'parsing':
      return t('library.statusParsing')
    case 'embedding':
      return t('library.statusEmbedding')
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
  const file = input.files?.[0]
  input.value = '' // 允许重选同一文件
  if (!file) return
  await doUpload(file)
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
  const file = e.dataTransfer?.files?.[0]
  if (file) void doUpload(file)
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

async function onRemove(id: string): Promise<void> {
  try {
    await store.remove(id)
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

      <!-- 上传拖拽区（REQ-019） -->
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
      <div class="doc-head">
        <h2>{{ $t('library.docTitle') }} <span class="doc-count">{{ store.documents.length }} · {{ readyCount }} {{ $t('library.readyCount') }}</span></h2>
      </div>
      <div class="table-wrap doc-table-wrap">
        <table class="tbl doc-table">
          <thead>
            <tr>
              <th style="width: 36%">{{ $t('library.colName') }}</th>
              <th>{{ $t('library.colSize') }}</th>
              <th>{{ $t('library.colChunks') }}</th>
              <th>{{ $t('library.colStatus') }}</th>
              <th>{{ $t('library.colTime') }}</th>
              <th style="width: 56px">{{ $t('library.colAction') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="store.documents.length === 0">
              <td colspan="6"><div class="empty-hint">{{ $t('library.empty') }}</div></td>
            </tr>
            <tr v-for="doc in store.documents" :key="doc.document_id" data-testid="kb-doc-row">
              <td>
                <div class="name-cell">
                  <span class="doc-ico" :class="docTypeClass(doc.doc_type)">
                    <UiIcon :name="docTypeIcon(doc.doc_type)" size="sm" />
                    <template v-if="doc.doc_type !== 'url'">{{ docTypeLabel(doc.doc_type) }}</template>
                  </span>
                  <div>
                    <div class="doc-name">{{ doc.title }}</div>
                    <div v-if="doc.doc_type === 'url' && doc.source_url" class="doc-sub">{{ doc.source_url }}</div>
                  </div>
                </div>
              </td>
              <td>{{ formatSize(doc.file_size_bytes) }}</td>
              <td>{{ doc.chunk_count > 0 ? doc.chunk_count : '—' }}</td>
              <td>
                <span class="badge" :class="statusBadgeClass(doc.status)">
                  <span v-if="doc.status === 'parsing' || doc.status === 'embedding'" class="dot dot-warning" style="width: 6px; height: 6px"></span>
                  <UiIcon v-else-if="doc.status === 'ready'" name="i-check" size="sm" />
                  <UiIcon v-else-if="doc.status === 'failed'" name="i-alert" size="sm" />
                  {{ statusText(doc.status) }}
                </span>
                <div v-if="doc.status === 'failed' && doc.fail_reason" class="status-reason">{{ doc.fail_reason }}</div>
              </td>
              <td>{{ formatTime(doc.created_at) }}</td>
              <td>
                <button
                  type="button"
                  class="icon-btn row-action"
                  :title="$t('library.delete')"
                  :aria-label="`${$t('library.delete')} ${doc.title}`"
                  data-testid="kb-doc-delete"
                  @click="onRemove(doc.document_id)"
                >
                  <UiIcon name="i-trash" size="sm" />
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="empty-hint">{{ $t('library.footHint') }}</div>
    </div>
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
  background: rgba(32, 184, 205, 0.04);
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
  background: rgba(229, 72, 77, 0.14);
  color: #ef7b7e;
}
.f-md {
  background: rgba(32, 184, 205, 0.14);
  color: var(--color-accent);
}
.f-txt {
  background: rgba(160, 166, 173, 0.14);
  color: var(--color-text-secondary);
}
.f-docx {
  background: rgba(63, 185, 80, 0.14);
  color: var(--color-success);
}
.f-url {
  background: rgba(143, 127, 240, 0.16);
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
.status-reason {
  margin-top: 2px;
  font-size: var(--text-xs);
  color: var(--color-danger);
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
  .doc-table-wrap {
    overflow-x: auto;
  }
  .doc-table {
    min-width: 720px;
  }
}
</style>
