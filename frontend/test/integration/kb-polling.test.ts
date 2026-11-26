/**
 * test/integration/kb-polling.test.ts — LibraryView 挂载 → kbStore 单 tick 双条件轮询装配
 * （api.md §4.3「每 5s」/§4.8「沿列表节律」；plan-m3-b6-f6 §2.2/§5.2；fake timers）。
 * 覆盖：挂载双拉（文档+任务）/文档非终态 tick 续拉至停表/仅任务进行中 tick 只拉任务
 * （挂载竞态收口：FeishuImportJobs 先载任务后 pollUntilSettled 仍能排表）/触发导入后任务卡随节律刷新/卸载停表。
 */
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as kbApi from '@/api/kb'
import { i18n } from '@/i18n'
import LibraryView from '@/views/LibraryView.vue'
import type { KbDocument, KbImportJob } from '@/types/api'

vi.mock('@/api/kb', () => ({
  fetchDocuments: vi.fn(),
  uploadDocument: vi.fn(),
  collectUrl: vi.fn(),
  fetchDocument: vi.fn(),
  removeDocument: vi.fn(),
  fetchKbChunk: vi.fn(),
  triggerImport: vi.fn(),
  fetchImportJobs: vi.fn(),
  fetchImportJobDetail: vi.fn(),
}))

const mockedFetchDocuments = vi.mocked(kbApi.fetchDocuments)
const mockedTriggerImport = vi.mocked(kbApi.triggerImport)
const mockedFetchImportJobs = vi.mocked(kbApi.fetchImportJobs)

/** §4.3 查询参形态（P1-A：页请求 vs ready 计数条件请求） */
interface FetchDocsQuery {
  limit?: number
  offset?: number
  status?: KbDocument['status']
}

/** 页请求调用轮数（ready 条件请求不计入——断言语义=「拉了几轮列表」） */
function docPageCalls(): number {
  return mockedFetchDocuments.mock.calls.filter(([q]) => (q as FetchDocsQuery | undefined)?.status !== 'ready')
    .length
}

/** 页/计数双通道 mock：status=ready → 条件计数响应；其余 → 页响应 */
function mockDocsPage(page: { total: number; items: KbDocument[] }, readyTotal = 0): void {
  mockedFetchDocuments.mockImplementation(async (query?: FetchDocsQuery) =>
    query?.status === 'ready' ? { total: readyTotal, items: [] } : page,
  )
}

/** 一次性页响应（下一组调用按参数分流） */
function mockDocsPageOnce(page: { total: number; items: KbDocument[] }): void {
  mockedFetchDocuments.mockImplementationOnce(async (query?: FetchDocsQuery) =>
    query?.status === 'ready' ? { total: 0, items: [] } : page,
  )
}

function doc(partial: Partial<KbDocument> & { document_id: string }): KbDocument {
  return {
    doc_type: 'md',
    title: `doc-${partial.document_id}`,
    file_size_bytes: 1024,
    status: 'ready',
    fail_reason: null,
    chunk_count: 3,
    created_at: '2026-11-01T08:12:00Z',
    parse_meta: null,
    catalog_path: null,
    ...partial,
  }
}

function job(partial: Partial<KbImportJob> & { job_id: string }): KbImportJob {
  return {
    connector: 'feishu',
    trigger: 'manual',
    status: 'running',
    total_docs: 37,
    processed_docs: 12,
    imported_docs: 11,
    failed_docs: 1,
    skipped_docs: 0,
    failure_summary: [],
    started_at: '2026-11-22T08:00:00Z',
    finished_at: null,
    created_at: '2026-11-22T07:59:58Z',
    updated_at: '2026-11-22T08:12:30Z',
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

describe('kb-polling（LibraryView 挂载 → store 双条件轮询装配）', () => {
  it('挂载双拉（文档+任务）；文档非终态 → 5s tick 续拉文档；全终态停表；卸载停表', async () => {
    mockDocsPage({ total: 1, items: [doc({ document_id: 'p', status: 'chunking' })] })
    mockedFetchImportJobs.mockResolvedValue({ total: 0, items: [] })
    const w = mount(LibraryView, { global: { plugins: [createPinia(), i18n] } })
    await flushPromises()
    expect(docPageCalls()).toBe(1)
    expect(mockedFetchImportJobs).toHaveBeenCalledTimes(1)

    mockDocsPageOnce({ total: 1, items: [doc({ document_id: 'p', status: 'ready' })] })
    await vi.advanceTimersByTimeAsync(5_000)
    expect(docPageCalls()).toBe(2)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(docPageCalls()).toBe(2) // 全终态停表

    w.unmount()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(docPageCalls()).toBe(2)
  })

  it('仅任务进行中（文档全终态）：挂载先载任务 → pollUntilSettled 仍能按任务条件排表（挂载竞态收口）', async () => {
    mockDocsPage({ total: 0, items: [] })
    mockedFetchImportJobs.mockResolvedValue({ total: 1, items: [job({ job_id: 'j1', status: 'running' })] })
    const w = mount(LibraryView, { global: { plugins: [createPinia(), i18n] } })
    await flushPromises()
    expect(mockedFetchImportJobs).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(5_000)
    expect(mockedFetchImportJobs).toHaveBeenCalledTimes(2)
    expect(docPageCalls()).toBe(1) // 文档全终态不拉
    w.unmount()
  })

  it('触发导入（§4.7）→ 任务列表立即拉取 + 之后随 5s 节律刷新', async () => {
    mockedFetchDocuments.mockResolvedValue({ total: 0, items: [] })
    mockedFetchImportJobs
      .mockResolvedValueOnce({ total: 0, items: [] }) // 挂载首拉：无任务
      .mockResolvedValue({ total: 1, items: [job({ job_id: 'j-new', status: 'pending' })] })
    mockedTriggerImport.mockResolvedValue({ job_id: 'j-new', status: 'pending' })
    const w = mount(LibraryView, { global: { plugins: [createPinia(), i18n] } })
    await flushPromises()
    expect(mockedFetchImportJobs).toHaveBeenCalledTimes(1)

    await w.find('[data-testid="kb-import-trigger"]').trigger('click')
    await flushPromises()
    expect(mockedTriggerImport).toHaveBeenCalledTimes(1)
    expect(mockedFetchImportJobs).toHaveBeenCalledTimes(2) // 成功后立即拉

    await vi.advanceTimersByTimeAsync(5_000)
    expect(mockedFetchImportJobs).toHaveBeenCalledTimes(3) // 节律续拉
    await vi.advanceTimersByTimeAsync(5_000)
    expect(mockedFetchImportJobs).toHaveBeenCalledTimes(4)
    w.unmount()
  })

  it('CR-1 卸载竞态收口：tick 在途 fetchImportJobs 落地不再重排（零后续 §4.3/§4.8 请求）', async () => {
    mockDocsPage({ total: 0, items: [] })
    mockedFetchImportJobs.mockResolvedValueOnce({ total: 1, items: [job({ job_id: 'j1', status: 'running' })] })
    const w = mount(LibraryView, { global: { plugins: [createPinia(), i18n] } })
    await flushPromises()
    expect(mockedFetchImportJobs).toHaveBeenCalledTimes(1)
    // 首个 tick 的 fetchImportJobs 挂住在途（fetch 在途时离开页面）
    let resolveJobs!: (v: { total: number; items: KbImportJob[] }) => void
    mockedFetchImportJobs.mockImplementationOnce(() => new Promise((resolve) => (resolveJobs = resolve)))
    await vi.advanceTimersByTimeAsync(5_000)
    expect(mockedFetchImportJobs).toHaveBeenCalledTimes(2)
    // 在途时卸载（unmount → onUnmounted stopPolling）
    w.unmount()
    // 在途 promise 落地（仍 running——fetchImportJobs 完成回调会尝试重排 5s 定时器）
    resolveJobs({ total: 1, items: [job({ job_id: 'j1', status: 'running' })] })
    await flushPromises()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(mockedFetchImportJobs).toHaveBeenCalledTimes(2) // 零后续 §4.8
    expect(docPageCalls()).toBe(1) // 零后续 §4.3
  })
})
