/**
 * test/unit/kb-store.test.ts — kbStore（frontend-api.md §4 第四行；§1 LibraryView 行为口径）。
 * 文档列表/上传/URL 收藏/删除 + 非终态条目 5s 轮询与停表。
 */
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { apiDelete, apiGet, apiPost } from '@/api/client'
import { useKbStore } from '@/stores/kb'
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

function doc(partial: Partial<KbDocument> & { document_id: string }): KbDocument {
  return {
    doc_type: 'md',
    title: `doc-${partial.document_id}`,
    file_size_bytes: 1024,
    status: 'ready',
    fail_reason: null,
    chunk_count: 3,
    created_at: '2026-11-01T08:12:00Z',
    ...partial,
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('kbStore（§4：文档列表、轮询定时器）', () => {
  it('fetchDocuments：GET /api/v1/kb/documents → items 投影', async () => {
    mockedGet.mockResolvedValueOnce({ total: 2, items: [doc({ document_id: 'a' }), doc({ document_id: 'b', status: 'parsing' })] })
    const store = useKbStore()
    await store.fetchDocuments()
    expect(mockedGet).toHaveBeenCalledWith('/api/v1/kb/documents')
    expect(store.documents).toHaveLength(2)
    expect(store.loading).toBe(false)
  })

  it('fetchDocuments 失败上抛（视图层 toast、列表不落假数据）', async () => {
    mockedGet.mockRejectedValueOnce(new Error('boom'))
    const store = useKbStore()
    await expect(store.fetchDocuments()).rejects.toThrow('boom')
  })

  it('upload：multipart POST（file+title）→ 成功后重拉列表', async () => {
    mockedPost.mockResolvedValueOnce(doc({ document_id: 'new', status: 'parsing' }))
    mockedGet.mockResolvedValue({ total: 1, items: [doc({ document_id: 'new', status: 'parsing' })] })
    const store = useKbStore()
    const file = new File(['%PDF-1.4'], 'spec.pdf', { type: 'application/pdf' })
    const result = await store.upload(file, 'spec.pdf')
    expect(result.document_id).toBe('new')
    expect(mockedPost).toHaveBeenCalledTimes(1)
    const [url, body] = mockedPost.mock.calls[0]!
    expect(url).toBe('/api/v1/kb/documents')
    expect(body).toBeInstanceOf(FormData)
    const form = body as FormData
    expect(form.get('title')).toBe('spec.pdf')
    expect(form.get('file')).toBeInstanceOf(File)
    expect(store.documents.map((d) => d.document_id)).toEqual(['new'])
  })

  it('collectUrl：POST /api/v1/kb/urls 携带 {url}（REQ-020）', async () => {
    mockedPost.mockResolvedValueOnce(doc({ document_id: 'u1', doc_type: 'url' }))
    mockedGet.mockResolvedValue({ total: 1, items: [doc({ document_id: 'u1', doc_type: 'url' })] })
    const store = useKbStore()
    await store.collectUrl('https://example.com/post')
    expect(mockedPost).toHaveBeenCalledWith('/api/v1/kb/urls', { url: 'https://example.com/post' })
    expect(store.documents).toHaveLength(1)
  })

  it('remove：DELETE /api/v1/kb/documents/{id} → 重拉（REQ-022）', async () => {
    mockedDelete.mockResolvedValueOnce({ deleted: true })
    mockedGet.mockResolvedValue({ total: 0, items: [] })
    const store = useKbStore()
    await store.fetchDocuments()
    await store.remove('a')
    expect(mockedDelete).toHaveBeenCalledWith('/api/v1/kb/documents/a')
    expect(store.documents).toHaveLength(0)
  })

  it('pollUntilSettled：有非终态条目 → 5s 后自动重拉，直至全部终态（§1 每 5s 轮询）', async () => {
    // 第一次拉取：含 parsing 条目 → 排轮询；5s 后第二次拉取：已 ready → 停表
    mockedGet.mockResolvedValueOnce({ total: 1, items: [doc({ document_id: 'p', status: 'parsing' })] })
    const store = useKbStore()
    await store.pollUntilSettled()
    expect(mockedGet).toHaveBeenCalledTimes(1)
    mockedGet.mockResolvedValueOnce({ total: 1, items: [doc({ document_id: 'p', status: 'ready' })] })
    await vi.advanceTimersByTimeAsync(5_000)
    expect(mockedGet).toHaveBeenCalledTimes(2)
    // 全终态 → 不再排新轮询
    await vi.advanceTimersByTimeAsync(10_000)
    expect(mockedGet).toHaveBeenCalledTimes(2)
  })

  it('stopPolling：卸载路径停表（不再发起拉取）', async () => {
    mockedGet.mockResolvedValue({ total: 1, items: [doc({ document_id: 'p', status: 'embedding' })] })
    const store = useKbStore()
    await store.pollUntilSettled()
    store.stopPolling()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(mockedGet).toHaveBeenCalledTimes(1)
  })

  it('全部终态时 pollUntilSettled 不排轮询', async () => {
    mockedGet.mockResolvedValue({ total: 1, items: [doc({ document_id: 'r', status: 'ready' })] })
    const store = useKbStore()
    await store.pollUntilSettled()
    await vi.advanceTimersByTimeAsync(10_000)
    expect(mockedGet).toHaveBeenCalledTimes(1)
  })
})
