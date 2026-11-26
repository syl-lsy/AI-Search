/**
 * test/unit/kb-store.test.ts — kbStore（frontend-api.md §4 kbStore 行；plan-m3-b6-f6 §2.2）。
 * 文档列表/上传/URL 收藏/删除 + 单 tick 双条件 5s 轮询（非终态文档 ∥ 进行中导入任务）与停表 +
 * triggerImport/fetchImportJobs/fetchImportJobDetail（§4.7~4.9）。
 * api 全经 src/api/kb.ts 唯一出口（store 层 mock 断言走模块函数；URL/method 契约归 integration kb-api.test.ts）。
 */
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/client'
import * as kbApi from '@/api/kb'
import { useKbStore } from '@/stores/kb'
import type { KbDocument, KbImportJob, KbImportJobDetail, KbImportTriggerData } from '@/types/api'

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
const mockedUploadDocument = vi.mocked(kbApi.uploadDocument)
const mockedCollectUrl = vi.mocked(kbApi.collectUrl)
const mockedRemoveDocument = vi.mocked(kbApi.removeDocument)
const mockedTriggerImport = vi.mocked(kbApi.triggerImport)
const mockedFetchImportJobs = vi.mocked(kbApi.fetchImportJobs)
const mockedFetchImportJobDetail = vi.mocked(kbApi.fetchImportJobDetail)

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

function job(partial: Partial<KbImportJob> & { job_id: string }): KbImportJob {
  return {
    connector: 'feishu',
    trigger: 'manual',
    status: 'running',
    total_docs: 931,
    processed_docs: 312,
    imported_docs: 306,
    failed_docs: 4,
    skipped_docs: 2,
    failure_summary: [{ category: 'block_fetch_failed', count: 3 }],
    started_at: '2026-11-22T08:00:00Z',
    finished_at: null,
    created_at: '2026-11-22T07:59:58Z',
    updated_at: '2026-11-22T08:12:30Z',
    ...partial,
  }
}

function jobDetail(partial: Partial<KbImportJob> & { job_id: string }): KbImportJobDetail {
  return {
    ...job(partial),
    failures: [{ title: '调研 mindnote', category: 'unsupported_type', message: 'mindnote 无读取 API，已跳过', document_id: null }],
  }
}

function jobListData(items: KbImportJob[]): { total: number; items: KbImportJob[] } {
  return { total: items.length, items }
}

/** §4.3 查询参形态（P1-A：页请求 vs ready 计数条件请求——fetchDocuments 内部并发两调用） */
interface FetchDocsQuery {
  limit?: number
  offset?: number
  status?: KbDocument['status']
}

/** 页请求调用轮数（ready 条件请求不计入——断言语义=「拉了几轮列表」，P1-A 适配） */
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

/** 一次性页响应（下一组调用按参数分流——tick/触底的变体场景） */
function mockDocsPageOnce(page: { total: number; items: KbDocument[] }): void {
  mockedFetchDocuments.mockImplementationOnce(async (query?: FetchDocsQuery) =>
    query?.status === 'ready' ? { total: 0, items: [] } : page,
  )
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('kbStore — 既有文档面（§4.1~4.5 经 api/kb.ts）', () => {
  it('fetchDocuments：首屏 limit=50&offset=0 页请求 + status=ready 条件请求并发取计数（docsTotal/readyTotal 投影）', async () => {
    mockedFetchDocuments.mockImplementation(async (query?: FetchDocsQuery) =>
      query?.status === 'ready'
        ? { total: 1, items: [doc({ document_id: 'a' })] }
        : { total: 2, items: [doc({ document_id: 'a' }), doc({ document_id: 'b', status: 'normalizing' })] },
    )
    const store = useKbStore()
    await store.fetchDocuments()
    expect(mockedFetchDocuments).toHaveBeenCalledWith({ limit: 50, offset: 0 })
    expect(mockedFetchDocuments).toHaveBeenCalledWith({ limit: 1, status: 'ready' })
    expect(docPageCalls()).toBe(1)
    expect(store.documents).toHaveLength(2)
    expect(store.docsTotal).toBe(2)
    expect(store.readyTotal).toBe(1)
    expect(store.loading).toBe(false)
  })

  it('fetchDocuments 失败上抛（视图层 toast、列表不落假数据）', async () => {
    mockedFetchDocuments.mockRejectedValueOnce(new Error('boom'))
    const store = useKbStore()
    await expect(store.fetchDocuments()).rejects.toThrow('boom')
  })

  it('upload：经 api/kb.uploadDocument(file,title) → 成功后重拉列表', async () => {
    mockedUploadDocument.mockResolvedValueOnce(doc({ document_id: 'new', status: 'normalizing' }))
    mockedFetchDocuments.mockResolvedValue({ total: 1, items: [doc({ document_id: 'new', status: 'normalizing' })] })
    const store = useKbStore()
    const file = new File(['%PDF-1.4'], 'spec.pdf', { type: 'application/pdf' })
    const result = await store.upload(file, 'spec.pdf')
    expect(result.document_id).toBe('new')
    expect(mockedUploadDocument).toHaveBeenCalledTimes(1)
    expect(mockedUploadDocument).toHaveBeenCalledWith(file, 'spec.pdf')
    expect(store.documents.map((d) => d.document_id)).toEqual(['new'])
  })

  it('collectUrl：经 api/kb.collectUrl({url})（REQ-020）→ 重拉列表', async () => {
    mockedCollectUrl.mockResolvedValueOnce(doc({ document_id: 'u1', doc_type: 'url' }))
    mockedFetchDocuments.mockResolvedValue({ total: 1, items: [doc({ document_id: 'u1', doc_type: 'url' })] })
    const store = useKbStore()
    await store.collectUrl('https://example.com/post')
    expect(mockedCollectUrl).toHaveBeenCalledWith('https://example.com/post')
    expect(store.documents).toHaveLength(1)
  })

  it('remove：经 api/kb.removeDocument → DELETE 后重拉（REQ-022；二次确认归视图层）', async () => {
    mockedRemoveDocument.mockResolvedValueOnce({ deleted: true })
    mockedFetchDocuments.mockResolvedValue({ total: 0, items: [] })
    const store = useKbStore()
    await store.fetchDocuments()
    await store.remove('a')
    expect(mockedRemoveDocument).toHaveBeenCalledWith('a')
    expect(store.documents).toHaveLength(0)
  })

  it('pollUntilSettled：有非终态条目 → 5s 后自动重拉，直至全部终态（api.md §4.3 每 5s）', async () => {
    mockDocsPage({ total: 1, items: [doc({ document_id: 'p', status: 'normalizing' })] })
    const store = useKbStore()
    await store.pollUntilSettled()
    expect(docPageCalls()).toBe(1)
    mockDocsPageOnce({ total: 1, items: [doc({ document_id: 'p', status: 'ready' })] })
    await vi.advanceTimersByTimeAsync(5_000)
    expect(docPageCalls()).toBe(2)
    // 全终态 → 不再排新轮询
    await vi.advanceTimersByTimeAsync(10_000)
    expect(docPageCalls()).toBe(2)
  })

  it('stopPolling：卸载路径停表（不再发起拉取）', async () => {
    mockDocsPage({ total: 1, items: [doc({ document_id: 'p', status: 'indexing' })] })
    const store = useKbStore()
    await store.pollUntilSettled()
    store.stopPolling()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(docPageCalls()).toBe(1)
  })

  it('文档全部终态时 pollUntilSettled 不排轮询', async () => {
    mockDocsPage({ total: 1, items: [doc({ document_id: 'r', status: 'ready' })] })
    const store = useKbStore()
    await store.pollUntilSettled()
    await vi.advanceTimersByTimeAsync(10_000)
    expect(docPageCalls()).toBe(1)
  })
})

describe('kbStore — 导入任务面（api.md §4.7~4.9 经 api/kb.ts）', () => {
  it('fetchImportJobs：列表替换式更新 importJobs', async () => {
    mockedFetchImportJobs.mockResolvedValueOnce(jobListData([job({ job_id: 'j1' }), job({ job_id: 'j2', status: 'completed' })]))
    const store = useKbStore()
    await store.fetchImportJobs()
    expect(store.importJobs.map((j) => j.job_id)).toEqual(['j1', 'j2'])
    // 替换式：第二次拉取条目集整体覆盖，不累积
    mockedFetchImportJobs.mockResolvedValueOnce(jobListData([job({ job_id: 'j3' })]))
    await store.fetchImportJobs()
    expect(store.importJobs.map((j) => j.job_id)).toEqual(['j3'])
  })

  it('fetchImportJobDetail：detail 更新（failures 仅详情端点承载）', async () => {
    mockedFetchImportJobDetail.mockResolvedValueOnce(jobDetail({ job_id: 'j1' }))
    const store = useKbStore()
    await store.fetchImportJobDetail('j1')
    expect(mockedFetchImportJobDetail).toHaveBeenCalledWith('j1')
    expect(store.importJobDetail?.job_id).toBe('j1')
    expect(store.importJobDetail?.failures).toHaveLength(1)
  })

  it('triggerImport：POST 成功 → 立即拉任务列表 + 启动轮询；importTriggering 防重入位复原', async () => {
    mockedFetchImportJobs.mockResolvedValue(jobListData([job({ job_id: 'j1', status: 'pending' })]))
    const store = useKbStore()

    let resolveTrigger!: (v: KbImportTriggerData) => void
    mockedTriggerImport.mockImplementationOnce(() => new Promise<KbImportTriggerData>((r) => (resolveTrigger = r)))
    const pending = store.triggerImport()
    expect(store.importTriggering).toBe(true)
    resolveTrigger({ job_id: 'j9', status: 'pending' })
    await pending

    expect(store.importTriggering).toBe(false)
    expect(mockedFetchImportJobs).toHaveBeenCalledTimes(1)
    // 进行中任务存在 → 5s tick 沿列表节律续拉（api.md §4.8）
    await vi.advanceTimersByTimeAsync(5_000)
    expect(mockedFetchImportJobs).toHaveBeenCalledTimes(2)
  })

  it('triggerImport：ApiError（409007 等）上抛不吞——视图层 toast；不启动轮询', async () => {
    mockedTriggerImport.mockRejectedValueOnce(new ApiError('409007', '已有进行中的知识库导入任务'))
    const store = useKbStore()
    await expect(store.triggerImport()).rejects.toMatchObject({ code: '409007', name: 'ApiError' })
    expect(store.importTriggering).toBe(false)
    expect(mockedFetchImportJobs).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(mockedFetchImportJobs).not.toHaveBeenCalled()
  })
})

describe('kbStore — 单 tick 双条件轮询（plan §2.2：同一节拍承载两条件，无双定时器漂移）', () => {
  it('仅文档非终态 → tick 只拉文档，不拉任务', async () => {
    mockDocsPage({ total: 1, items: [doc({ document_id: 'p', status: 'normalizing' })] })
    const store = useKbStore()
    await store.pollUntilSettled()
    await vi.advanceTimersByTimeAsync(5_000)
    expect(docPageCalls()).toBe(2)
    expect(mockedFetchImportJobs).not.toHaveBeenCalled()
  })

  it('仅任务进行中 → tick 只拉任务，不拉文档', async () => {
    mockDocsPage({ total: 0, items: [] })
    mockedTriggerImport.mockResolvedValueOnce({ job_id: 'j1', status: 'pending' })
    mockedFetchImportJobs.mockResolvedValue(jobListData([job({ job_id: 'j1', status: 'running' })]))
    const store = useKbStore()
    await store.pollUntilSettled()
    await store.triggerImport()
    expect(docPageCalls()).toBe(1)
    await vi.advanceTimersByTimeAsync(5_000)
    expect(mockedFetchImportJobs).toHaveBeenCalledTimes(2)
    expect(docPageCalls()).toBe(1)
  })

  it('双条件皆命中 → 同一 tick 两请求齐发 + 展开中详情随节律刷新', async () => {
    mockDocsPage({ total: 1, items: [doc({ document_id: 'p', status: 'chunking' })] })
    mockedTriggerImport.mockResolvedValueOnce({ job_id: 'j1', status: 'pending' })
    mockedFetchImportJobs.mockResolvedValue(jobListData([job({ job_id: 'j1', status: 'running' })]))
    mockedFetchImportJobDetail.mockResolvedValue(jobDetail({ job_id: 'j1' }))
    const store = useKbStore()
    await store.pollUntilSettled()
    await store.triggerImport()
    await store.fetchImportJobDetail('j1')
    expect(docPageCalls()).toBe(1)
    expect(mockedFetchImportJobs).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(5_000)
    expect(docPageCalls()).toBe(2)
    expect(mockedFetchImportJobs).toHaveBeenCalledTimes(2)
    expect(mockedFetchImportJobDetail).toHaveBeenCalledTimes(2)
    expect(mockedFetchImportJobDetail).toHaveBeenLastCalledWith('j1')
  })

  it('双条件皆空（文档全终态且无进行中任务）→ 停表', async () => {
    mockDocsPage({ total: 0, items: [] })
    const store = useKbStore()
    await store.pollUntilSettled()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(docPageCalls()).toBe(1)
    expect(mockedFetchImportJobs).not.toHaveBeenCalled()
  })

  it('任务到终态后 → tick 只剩文档条件；全终态停表', async () => {
    mockDocsPage({ total: 1, items: [doc({ document_id: 'p', status: 'normalizing' })] })
    mockedTriggerImport.mockResolvedValueOnce({ job_id: 'j1', status: 'pending' })
    const store = useKbStore()
    await store.pollUntilSettled()
    // 第一次任务拉取：running；tick 后翻 completed → 下一 tick 任务条件消失
    mockedFetchImportJobs.mockResolvedValueOnce(jobListData([job({ job_id: 'j1', status: 'running' })]))
    await store.triggerImport()
    mockedFetchImportJobs.mockResolvedValueOnce(jobListData([job({ job_id: 'j1', status: 'completed' })]))
    mockDocsPageOnce({ total: 1, items: [doc({ document_id: 'p', status: 'indexing' })] })
    await vi.advanceTimersByTimeAsync(5_000)
    expect(mockedFetchImportJobs).toHaveBeenCalledTimes(2)
    expect(docPageCalls()).toBe(2)
    // 下一 tick：任务全终态 → 只拉文档；文档随后 ready → 停表
    mockDocsPageOnce({ total: 1, items: [doc({ document_id: 'p', status: 'ready' })] })
    await vi.advanceTimersByTimeAsync(5_000)
    expect(docPageCalls()).toBe(3)
    expect(mockedFetchImportJobs).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(15_000)
    expect(docPageCalls()).toBe(3)
  })

  it('stopPolling 停双条件链（任务进行中也不再有 tick）', async () => {
    mockedTriggerImport.mockResolvedValueOnce({ job_id: 'j1', status: 'pending' })
    mockedFetchImportJobs.mockResolvedValue(jobListData([job({ job_id: 'j1', status: 'running' })]))
    const store = useKbStore()
    await store.triggerImport()
    store.stopPolling()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(mockedFetchImportJobs).toHaveBeenCalledTimes(1)
  })

  it('CR-1 卸载竞态收口：stopPolling 后 tick 在途 fetch 落地不再重排（stopped 标志）', async () => {
    mockDocsPage({ total: 1, items: [doc({ document_id: 'p', status: 'chunking' })] })
    const store = useKbStore()
    await store.pollUntilSettled()
    // 首个 tick 的页请求挂住在途（落地时仍非终态——无标志形态会经 tick 尾部重排 5s 定时器）
    let resolveInFlight!: (v: { total: number; items: KbDocument[] }) => void
    mockedFetchDocuments.mockImplementationOnce((query?: FetchDocsQuery) => {
      if (query?.status === 'ready') return Promise.resolve({ total: 0, items: [] })
      return new Promise((resolve) => (resolveInFlight = resolve))
    })
    await vi.advanceTimersByTimeAsync(5_000)
    expect(docPageCalls()).toBe(2)
    store.stopPolling()
    resolveInFlight({ total: 1, items: [doc({ document_id: 'p', status: 'indexing' })] })
    await flushPromises() // 在途 promise 落地 → tick 尾部 schedulePollIfPending（须被标志拦下）
    await vi.advanceTimersByTimeAsync(30_000)
    expect(docPageCalls()).toBe(2) // 停表后零后续请求
  })

  it('CR-1 重进页面恢复：stopPolling 后 pollUntilSettled 复位停止标志、轮询重启', async () => {
    mockDocsPage({ total: 1, items: [doc({ document_id: 'p', status: 'indexing' })] })
    const store = useKbStore()
    await store.pollUntilSettled()
    store.stopPolling()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(docPageCalls()).toBe(1)
    // 重进页面（LibraryView onMounted → pollUntilSettled）：复位标志、续排轮询
    await store.pollUntilSettled()
    expect(docPageCalls()).toBe(2)
    mockDocsPageOnce({ total: 1, items: [doc({ document_id: 'p', status: 'ready' })] })
    await vi.advanceTimersByTimeAsync(5_000)
    expect(docPageCalls()).toBe(3)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(docPageCalls()).toBe(3) // 全终态停表
  })

  it('CR-3 收起清详情：importJobDetail 置空后 tick 不再续拉详情（判据=真展开态）；任务轮询照常', async () => {
    mockedFetchImportJobs.mockResolvedValue(jobListData([job({ job_id: 'j1', status: 'running' })]))
    mockedFetchImportJobDetail.mockResolvedValue(jobDetail({ job_id: 'j1' }))
    const store = useKbStore()
    await store.fetchImportJobs()
    await store.fetchImportJobDetail('j1')
    expect(store.importJobDetail?.job_id).toBe('j1')
    // 视图层收起（FeishuImportJobs onDetailToggle close 分支）走 R-4 动作
    store.clearImportJobDetail()
    await vi.advanceTimersByTimeAsync(5_000)
    expect(mockedFetchImportJobs).toHaveBeenCalledTimes(2) // 任务轮询照常
    expect(mockedFetchImportJobDetail).toHaveBeenCalledTimes(1) // 详情不再续拉
  })

  it('R-2/F-1 收起在途请求失配丢弃：展开→收起→在途详情落地不写回（tick 判据不被重新点亮）', async () => {
    mockedFetchImportJobs.mockResolvedValue(jobListData([job({ job_id: 'j1', status: 'running' })]))
    let resolveDetail!: (v: KbImportJobDetail) => void
    mockedFetchImportJobDetail.mockImplementationOnce(() => new Promise((resolve) => (resolveDetail = resolve)))
    const store = useKbStore()
    await store.fetchImportJobs() // 任务进行中 → 排表
    const inFlight = store.fetchImportJobDetail('j1') // 展开触发、响应在途
    store.clearImportJobDetail() // 在途时收起（R-4 动作）
    resolveDetail(jobDetail({ job_id: 'j1' }))
    await inFlight
    expect(store.importJobDetail).toBeNull() // 失配丢弃、不写回
    await vi.advanceTimersByTimeAsync(5_000)
    expect(mockedFetchImportJobDetail).toHaveBeenCalledTimes(1) // tick 判据未被回写点亮
    expect(mockedFetchImportJobs).toHaveBeenCalledTimes(2) // 任务轮询照常
  })

  it('R-2 串卡防护：旧任务在途响应晚于新任务详情落地 → 旧响应丢弃、新响应不被覆盖', async () => {
    mockedFetchImportJobs.mockResolvedValue(jobListData([job({ job_id: 'j1', status: 'running' })]))
    const store = useKbStore()
    await store.fetchImportJobs()
    let resolveOld!: (v: KbImportJobDetail) => void
    mockedFetchImportJobDetail.mockImplementationOnce(() => new Promise((resolve) => (resolveOld = resolve)))
    const oldReq = store.fetchImportJobDetail('j1') // 旧请求在途
    mockedFetchImportJobDetail.mockResolvedValue(jobDetail({ job_id: 'j2' }))
    await store.fetchImportJobDetail('j2') // 新任务详情先落地
    expect(store.importJobDetail?.job_id).toBe('j2')
    resolveOld(jobDetail({ job_id: 'j1' })) // 旧响应晚到
    await oldReq
    expect(store.importJobDetail?.job_id).toBe('j2') // 不被旧响应覆盖
  })

  it('tick 内单腿失败（allSettled）→ 链不断，下 tick 续拉', async () => {
    mockDocsPage({ total: 1, items: [doc({ document_id: 'p', status: 'normalizing' })] })
    const store = useKbStore()
    await store.pollUntilSettled()
    mockedFetchDocuments.mockRejectedValueOnce(new Error('transient'))
    await vi.advanceTimersByTimeAsync(5_000)
    mockDocsPageOnce({ total: 1, items: [doc({ document_id: 'p', status: 'ready' })] })
    await vi.advanceTimersByTimeAsync(5_000)
    expect(docPageCalls()).toBe(3)
  })
})

describe('kbStore — 页码分页（R-3 用户裁决：可选页码分页控件，页替换语义；api.md §4.3 limit/offset）', () => {
  /** 两页 fixture：第 1 页 offset=0 三行、第 2 页 offset=50 两行（total=5 → totalPages=1？——
   *  PAGE_SIZE=50，5 行仅 1 页；页码语义用例改用大 total 触发多页） */
  function twoPageMock(total: number, readyTotal = 0): void {
    const page1 = ['a', 'b', 'c'].map((id) => doc({ document_id: id }))
    const page2 = ['d', 'e'].map((id) => doc({ document_id: id }))
    mockedFetchDocuments.mockImplementation(async (query?: FetchDocsQuery) => {
      if (query?.status === 'ready') return { total: readyTotal, items: [] }
      return (query?.offset ?? 0) === 0 ? { total, items: page1 } : { total, items: page2 }
    })
  }

  it('fetchDocuments 首屏：currentPage=1 + limit=50&offset=0 页替换 + totalPages 投影', async () => {
    twoPageMock(120, 3)
    const store = useKbStore()
    await store.fetchDocuments()
    expect(store.currentPage).toBe(1)
    expect(mockedFetchDocuments).toHaveBeenCalledWith({ limit: 50, offset: 0 })
    expect(mockedFetchDocuments).toHaveBeenCalledWith({ limit: 1, status: 'ready' })
    expect(store.documents.map((d) => d.document_id)).toEqual(['a', 'b', 'c'])
    expect(store.docsTotal).toBe(120)
    expect(store.readyTotal).toBe(3)
    expect(store.totalPages).toBe(3) // ceil(120/50)
    expect(store.loadingPage).toBe(false)
  })

  it('goToPage：offset=(p-1)*pageSize 整页替换不追加；页码与请求参数一致', async () => {
    twoPageMock(120)
    const store = useKbStore()
    await store.fetchDocuments()
    await store.goToPage(2)
    expect(mockedFetchDocuments).toHaveBeenCalledWith({ limit: 50, offset: 50 })
    expect(store.currentPage).toBe(2)
    // 页替换语义：整页覆盖（2 行），不是累积追加（3+2=5 不成立）
    expect(store.documents.map((d) => d.document_id)).toEqual(['d', 'e'])
    expect(store.docsTotal).toBe(120)
    expect(store.loadingPage).toBe(false)
  })

  it('goToPage 边界钳制（0/负数/超出→1..totalPages）+ 同页 no-op 零请求', async () => {
    twoPageMock(120)
    const store = useKbStore()
    await store.fetchDocuments()
    const callsBefore = mockedFetchDocuments.mock.calls.length
    await store.goToPage(2)
    expect(store.currentPage).toBe(2)
    await store.goToPage(0) // 钳到 1
    await store.goToPage(-3) // 钳到 1
    expect(store.currentPage).toBe(1)
    await store.goToPage(999) // 钳到末页 3
    expect(store.currentPage).toBe(3)
    // 末页页请求 offset=100（页/ready 双通道并发——断言位置无关）
    expect(mockedFetchDocuments).toHaveBeenCalledWith({ limit: 50, offset: 100 })
    // 同页 no-op：回到当前页零请求
    const callsAtLast = mockedFetchDocuments.mock.calls.length
    await store.goToPage(3)
    expect(mockedFetchDocuments.mock.calls.length).toBe(callsAtLast)
    expect(callsAtLast).toBeGreaterThan(callsBefore)
  })

  it('goToPage 防重入：翻页在途期间再触发不发请求', async () => {
    const page1 = ['a', 'b'].map((id) => doc({ document_id: id }))
    let resolvePage2!: (v: { total: number; items: KbDocument[] }) => void
    let readySeq = 0
    mockedFetchDocuments.mockImplementation(async (query?: FetchDocsQuery) => {
      if (query?.status === 'ready') return { total: readySeq++ === 0 ? 1 : 2, items: [] }
      return (query?.offset ?? 0) === 0
        ? { total: 120, items: page1 }
        : new Promise((resolve) => (resolvePage2 = resolve))
    })
    const store = useKbStore()
    await store.fetchDocuments()
    const pending = store.goToPage(2)
    expect(store.loadingPage).toBe(true)
    void store.goToPage(3) // 在途吞——零新请求
    resolvePage2({ total: 120, items: [doc({ document_id: 'd' })] })
    await pending
    // 页请求 offset 序列恰为 [0, 50]（防重入收口——第二次 goToPage 未发请求）
    const pageOffsets = mockedFetchDocuments.mock.calls
      .filter(([q]) => (q as FetchDocsQuery | undefined)?.status !== 'ready')
      .map(([q]) => (q as FetchDocsQuery).offset)
    expect(pageOffsets).toEqual([0, 50])
    expect(store.currentPage).toBe(2)
    expect(store.readyTotal).toBe(2) // 翻页后 ready 计数腿随件刷新
  })

  it('上传成功后回第 1 页（新行置顶可见）：currentPage 复位 + 首页替换重拉', async () => {
    twoPageMock(120)
    mockedUploadDocument.mockResolvedValueOnce(doc({ document_id: 'new', status: 'normalizing' }))
    const store = useKbStore()
    await store.fetchDocuments()
    await store.goToPage(2)
    expect(store.currentPage).toBe(2)
    const file = new File(['x'], 'new.md', { type: 'text/markdown' })
    await store.upload(file, 'new.md')
    // fetchDocuments 内部 currentPage=1 → 重拉首页（新行置顶可见）
    expect(store.currentPage).toBe(1)
    expect(mockedFetchDocuments).toHaveBeenCalledWith({ limit: 50, offset: 0 })
  })

  it('tick 重拉当前页（limit=50&offset=(currentPage-1)*50）：非第 1 页的轮询推进可见行', async () => {
    twoPageMock(120)
    // 第 2 页含非终态行；第二次页请求（tick）返回 ready 态（轮询推进面）
    let page2Seq = 0
    mockedFetchDocuments.mockImplementation(async (query?: FetchDocsQuery) => {
      if (query?.status === 'ready') return { total: 1, items: [] }
      return (query?.offset ?? 0) === 0
        ? { total: 120, items: [doc({ document_id: 'a' })] }
        : {
            total: 120,
            items: [doc({ document_id: 'd', status: ++page2Seq >= 2 ? 'ready' : 'indexing' })],
          }
    })
    const store = useKbStore()
    await store.pollUntilSettled()
    await store.goToPage(2)
    await vi.advanceTimersByTimeAsync(5_000)
    // tick 文档腿 = 当前页重拉（offset=50），d 推进 ready
    expect(mockedFetchDocuments).toHaveBeenCalledWith({ limit: 50, offset: 50 })
    expect(store.documents.map((d) => `${d.document_id}:${d.status}`)).toEqual(['d:ready'])
    const callsAfterTick = docPageCalls() // 首拉 + 翻页 + tick = 3 轮
    await vi.advanceTimersByTimeAsync(15_000)
    expect(docPageCalls()).toBe(callsAfterTick) // 全终态停表
  })

  it('tick 在翻页在途时跳过（防 offset 竞态），下 tick 自愈', async () => {
    const page1 = [doc({ document_id: 'p', status: 'chunking' })]
    let resolvePage2!: (v: { total: number; items: KbDocument[] }) => void
    let armed = false
    mockedFetchDocuments.mockImplementation(async (query?: FetchDocsQuery) => {
      if (query?.status === 'ready') return { total: 1, items: [] }
      if ((query?.offset ?? 0) === 0) return { total: 120, items: page1 }
      if (armed) return new Promise((resolve) => (resolvePage2 = resolve))
      return { total: 120, items: [doc({ document_id: 'd', status: 'indexing' })] }
    })
    const store = useKbStore()
    await store.pollUntilSettled()
    // 翻页在途（挂住）——同窗口 tick 到点
    armed = true
    void store.goToPage(2)
    expect(store.loadingPage).toBe(true)
    await vi.advanceTimersByTimeAsync(5_000)
    // loadingPage 期间 tick 跳过文档刷新；p 非终态 → 下一 tick 正常续拉
    resolvePage2({ total: 120, items: [doc({ document_id: 'd', status: 'indexing' })] })
    await flushPromises()
    expect(store.documents.map((d) => d.document_id)).toEqual(['d'])
    mockDocsPage({ total: 120, items: [doc({ document_id: 'd' })] })
    await vi.advanceTimersByTimeAsync(5_000)
    expect(store.documents.map((d) => d.status)).toEqual(['ready']) // 下 tick 自愈
  })

  it('删除致当前页空 → 自愈回退末页重拉（末页必非空）', async () => {
    // 第 3 页（offset=100）仅 1 行 r；删除后服务端 total=51（仅 2 页）→ 拉第 3 页得空页 → 回退第 2 页
    let deleted = false
    mockedFetchDocuments.mockImplementation(async (query?: FetchDocsQuery) => {
      if (query?.status === 'ready') return { total: 0, items: [] }
      const offset = query?.offset ?? 0
      if (offset === 0) return { total: deleted ? 51 : 102, items: [doc({ document_id: 'a' })] }
      if (offset === 50) return { total: deleted ? 51 : 102, items: [doc({ document_id: 'b' })] }
      return deleted ? { total: 51, items: [] } : { total: 102, items: [doc({ document_id: 'r' })] }
    })
    mockedRemoveDocument.mockImplementationOnce(async () => {
      deleted = true
      return { deleted: true }
    })
    const store = useKbStore()
    await store.fetchDocuments()
    await store.goToPage(3)
    expect(store.currentPage).toBe(3)
    await store.remove('r')
    // 删除后重拉当前页得空页（total=51 → totalPages=2）→ 自愈回退第 2 页
    expect(store.currentPage).toBe(2)
    expect(store.documents.map((d) => d.document_id)).toEqual(['b'])
    expect(store.loadingPage).toBe(false)
  })
})
