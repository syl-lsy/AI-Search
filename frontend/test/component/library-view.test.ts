/**
 * test/component/library-view.test.ts — LibraryView 组件层（§1 LibraryView 行）。
 * 文档表渲染（三态徽标/失败原因/URL 副行）/ URL 收集（协议白名单拒绝）/ 上传白名单 /
 * 删除 / 5s 轮询生命周期。
 */
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { apiDelete, apiGet, apiPost } from '@/api/client'
import { i18n } from '@/i18n'
import LibraryView from '@/views/LibraryView.vue'
import type { KbDocument } from '@/types/api'

vi.mock('@/api/client', () => ({
  API_BASE_URL: '',
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPut: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
  apiDownload: vi.fn(),
  normalizeError: () => ({ name: 'ApiError', code: 'network_error', message: '网络异常，请稍后重试' }),
}))

const mockedGet = vi.mocked(apiGet)
const mockedPost = vi.mocked(apiPost)
const mockedDelete = vi.mocked(apiDelete)

const DOCS: KbDocument[] = [
  {
    document_id: 'd-ready',
    doc_type: 'pdf',
    title: 'RAG-检索融合设计方案.pdf',
    file_size_bytes: 2_400_000,
    status: 'ready',
    fail_reason: null,
    chunk_count: 86,
    created_at: '2026-11-01T08:12:00Z',
  },
  {
    document_id: 'd-parsing',
    doc_type: 'docx',
    title: 'product-spec.docx',
    file_size_bytes: 1_100_000,
    status: 'parsing',
    fail_reason: null,
    chunk_count: 0,
    created_at: '2026-11-01T08:12:00Z',
  },
  {
    document_id: 'd-failed',
    doc_type: 'pdf',
    title: 'legacy-api-manual.pdf',
    file_size_bytes: 18_600_000,
    status: 'failed',
    fail_reason: '解析超时（扫描件无文本层），请重新上传',
    chunk_count: 0,
    created_at: '2026-11-01T08:12:00Z',
  },
  {
    document_id: 'd-url',
    doc_type: 'url',
    title: 'example.com/postgres-17-notes',
    source_url: 'https://example.com/postgres-17-notes',
    file_size_bytes: null,
    status: 'ready',
    fail_reason: null,
    chunk_count: 21,
    created_at: '2026-11-01T08:12:00Z',
  },
]

function mountLibrary() {
  return mount(LibraryView, { global: { plugins: [createPinia(), i18n] } })
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  vi.useFakeTimers()
  mockedGet.mockResolvedValue({ total: DOCS.length, items: DOCS })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('LibraryView（§1：kb 列表/上传/URL 收藏/删除/轮询）', () => {
  it('文档表渲染：三态徽标 + 失败原因副行 + URL 副行 + 大小/切片格式', async () => {
    const w = mountLibrary()
    await flushPromises()
    const rows = w.findAll('[data-testid="kb-doc-row"]')
    expect(rows).toHaveLength(4)
    expect(rows[0]?.text()).toContain('已入库')
    expect(rows[1]?.text()).toContain('处理中 · 解析')
    expect(rows[2]?.text()).toContain('失败')
    expect(rows[2]?.text()).toContain('解析超时（扫描件无文本层），请重新上传')
    expect(rows[3]?.text()).toContain('https://example.com/postgres-17-notes')
    expect(w.text()).toContain('2.3 MB') // 2.4MB 十进制 → 二进制 MiB 展示 2.3
    expect(w.text()).toContain('86')
    // 空大小（URL 收藏）显示占位
    expect(rows[3]?.text()).toContain('—')
  })

  it('URL 收藏：合法 http(s) → POST /api/v1/kb/urls + 重拉；非法协议前端拒绝不触网', async () => {
    const w = mountLibrary()
    await flushPromises()
    mockedPost.mockResolvedValueOnce(DOCS[3]!)
    await w.find('[data-testid="url-input"]').setValue('https://example.com/new-post')
    await w.find('[data-testid="url-collect-btn"]').trigger('click')
    await flushPromises()
    expect(mockedPost).toHaveBeenCalledWith('/api/v1/kb/urls', { url: 'https://example.com/new-post' })

    await w.find('[data-testid="url-input"]').setValue('javascript:alert(1)')
    await w.find('[data-testid="url-collect-btn"]').trigger('click')
    await flushPromises()
    expect(mockedPost).toHaveBeenCalledTimes(1) // 拒绝路径零网络（toast 由 App 层 ToastHost 承载）
  })

  it('上传白名单：非白名单扩展名前端拒绝（不入表不触网）', async () => {
    const w = mountLibrary()
    await flushPromises()
    const input = w.find('[data-testid="upload-input"]')
    const file = new File(['x'], 'evil.exe', { type: 'application/octet-stream' })
    Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
    await input.trigger('change')
    expect(mockedPost).not.toHaveBeenCalled()
  })

  it('上传超限（>20MB）前端拒绝', async () => {
    const w = mountLibrary()
    await flushPromises()
    const input = w.find('[data-testid="upload-input"]')
    const big = new File([new ArrayBuffer(21 * 1024 * 1024)], 'big.pdf', { type: 'application/pdf' })
    Object.defineProperty(input.element, 'files', { value: [big], configurable: true })
    await input.trigger('change')
    expect(mockedPost).not.toHaveBeenCalled()
  })

  it('合法上传走 store.upload（multipart POST）', async () => {
    const w = mountLibrary()
    await flushPromises()
    mockedPost.mockResolvedValueOnce(DOCS[0]!)
    const input = w.find('[data-testid="upload-input"]')
    const file = new File(['%PDF-'], 'ok.md', { type: 'text/markdown' })
    Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
    await input.trigger('change')
    await flushPromises()
    expect(mockedPost).toHaveBeenCalledTimes(1)
    expect(mockedPost.mock.calls[0]?.[0]).toBe('/api/v1/kb/documents')
  })

  it('删除：DELETE 后重拉（REQ-022）', async () => {
    const w = mountLibrary()
    await flushPromises()
    mockedDelete.mockResolvedValueOnce({ deleted: true })
    await w.findAll('[data-testid="kb-doc-delete"]')[0]!.trigger('click')
    await flushPromises()
    expect(mockedDelete).toHaveBeenCalledWith('/api/v1/kb/documents/d-ready')
  })

  it('挂载即拉取；非终态条目驱动 5s 轮询；卸载停表（§1）', async () => {
    const w = mountLibrary()
    await flushPromises()
    expect(mockedGet).toHaveBeenCalledWith('/api/v1/kb/documents')
    expect(mockedGet).toHaveBeenCalledTimes(1) // 含 parsing 条目 → 已排轮询（尚未到期）
    await vi.advanceTimersByTimeAsync(5_000)
    expect(mockedGet).toHaveBeenCalledTimes(2)
    w.unmount()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(mockedGet).toHaveBeenCalledTimes(2) // 停表
  })
})
