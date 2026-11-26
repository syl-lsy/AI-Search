/**
 * test/component/library-view.test.ts — LibraryView 组件层（§1 LibraryView 行；plan-m3-b6-f6 §1.2/§3/§5.3）。
 * M3 管理面：六态徽标 + DocStatusStepper 步进条 + failed stage-tag/retry-link + parse_meta 列 +
 * catalog_path 副行（仅飞书）+ 六态图例 + 删除级联确认流（alertdialog 三存储清单）+ 多选上传循环 +
 * URL 收集（协议白名单拒绝）+ 5s 轮询生命周期。api 经 src/api/kb.ts 唯一出口（模块 mock）。
 */
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as kbApi from '@/api/kb'
import { i18n } from '@/i18n'
import LibraryView from '@/views/LibraryView.vue'
import type { KbDocument } from '@/types/api'

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
const mockedFetchImportJobs = vi.mocked(kbApi.fetchImportJobs)

/** §4.3 查询参形态（P1-A：页请求 vs ready 计数条件请求） */
interface FetchDocsQuery {
  limit?: number
  offset?: number
  status?: KbDocument['status']
}

/** 页请求调用轮数（ready 条件请求不计入） */
function docPageCalls(): number {
  return mockedFetchDocuments.mock.calls.filter(([q]) => (q as FetchDocsQuery | undefined)?.status !== 'ready')
    .length
}

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
    parse_meta: { pages: 24, tables: 3, code_blocks: 12 },
    catalog_path: null,
  },
  {
    document_id: 'd-normalizing',
    doc_type: 'docx',
    title: 'product-spec.docx',
    file_size_bytes: 1_100_000,
    status: 'normalizing',
    fail_reason: null,
    chunk_count: 0,
    created_at: '2026-11-01T08:12:00Z',
    parse_meta: null,
    catalog_path: null,
  },
  {
    document_id: 'd-failed',
    doc_type: 'pdf',
    title: 'legacy-api-manual.pdf',
    file_size_bytes: 18_600_000,
    status: 'failed',
    fail_reason: 'normalizing: 加密 PDF 无法解析，请解除密码后重新上传',
    chunk_count: 0,
    created_at: '2026-11-01T08:12:00Z',
    parse_meta: null,
    catalog_path: null,
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
    parse_meta: { pages: null, tables: 0, code_blocks: 0 },
    catalog_path: null,
  },
  {
    document_id: 'd-feishu',
    doc_type: 'pdf',
    title: '系统设计面试手册.pdf',
    file_size_bytes: 8_100_000,
    status: 'chunking',
    fail_reason: null,
    chunk_count: 0,
    created_at: '2026-11-01T08:12:00Z',
    parse_meta: { pages: 46, tables: 3, code_blocks: 8 },
    catalog_path: '家庭知识库 / 大模型全套面试题 / 面试资料包',
  },
]

function mountLibrary() {
  return mount(LibraryView, { global: { plugins: [createPinia(), i18n] } })
}

function rowOf(w: ReturnType<typeof mountLibrary>, documentId: string) {
  return w.findAll('[data-testid="kb-doc-row"]').find((r) => r.attributes('data-doc-id') === documentId)!
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  vi.useFakeTimers()
  // §4.3 页/计数双通道（R-3 页码分页：首屏第 1 页 + ready 条件请求——DOCS 内 ready 2 行）
  mockedFetchDocuments.mockImplementation(async (query?: FetchDocsQuery) =>
    query?.status === 'ready'
      ? { total: 2, items: DOCS.filter((d) => d.status === 'ready') }
      : { total: DOCS.length, items: DOCS },
  )
  mockedFetchImportJobs.mockResolvedValue({ total: 0, items: [] })
})

afterEach(() => {
  vi.useRealTimers()
  document.body.innerHTML = ''
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('LibraryView — 文档表 M3 管理面（六态/步进条/parse_meta/catalog_path）', () => {
  it('六态徽标 + 失败原因 + URL 副行 + 大小/切片格式', async () => {
    const w = mountLibrary()
    await flushPromises()
    expect(w.findAll('[data-testid="kb-doc-row"]')).toHaveLength(5)
    expect(rowOf(w, 'd-ready').text()).toContain('已入库')
    expect(rowOf(w, 'd-normalizing').text()).toContain('归一化中')
    expect(rowOf(w, 'd-failed').text()).toContain('失败')
    expect(rowOf(w, 'd-url').text()).toContain('https://example.com/postgres-17-notes')
    expect(w.text()).toContain('2.3 MB')
  })

  it('解析摘要列：parse_meta 非空 → i18n 键拼段（null 段不渲染）；null → —（CR-4：与下钻 chips 同键）', async () => {
    const w = mountLibrary()
    await flushPromises()
    expect(rowOf(w, 'd-ready').text()).toContain('页 24·表格 3·代码块 12')
    // 非页式（pages null）→ 只渲染表/码两段
    expect(rowOf(w, 'd-url').text()).toContain('表格 0·代码块 0')
    expect(rowOf(w, 'd-url').text()).not.toContain('页 ')
    // normalizing 完成前 parse_meta=null → —
    expect(rowOf(w, 'd-normalizing').text()).toContain('—')
  })

  it('catalog_path 副行：仅飞书来源（非空）渲染目录链；本地上传/URL 恒 null 不渲染', async () => {
    const w = mountLibrary()
    await flushPromises()
    expect(rowOf(w, 'd-feishu').text()).toContain('家庭知识库 / 大模型全套面试题 / 面试资料包')
    expect(rowOf(w, 'd-ready').find('.crumb').exists()).toBe(false)
    expect(rowOf(w, 'd-url').find('.crumb').exists()).toBe(false)
  })

  it('DocStatusStepper：进行中/终态行渲染四节点步进条（cur 定位当前阶段）；pending 行不渲染', async () => {
    mockedFetchDocuments.mockResolvedValue({
      total: 2,
      items: [
        { ...DOCS[0]!, document_id: 'd-pending', status: 'pending' as const, parse_meta: null },
        DOCS[1]!,
      ],
    })
    const w = mountLibrary()
    await flushPromises()
    expect(rowOf(w, 'd-pending').find('[data-testid="kb-doc-stepper"]').exists()).toBe(false)
    const stepper = rowOf(w, 'd-normalizing').find('[data-testid="kb-doc-stepper"]')
    expect(stepper.exists()).toBe(true)
    expect(stepper.findAll('.st')).toHaveLength(4)
    expect(stepper.find('.st.cur').text()).toContain('归一化')
  })

  it('failed 行：步进条 fail 定位 + stage-tag 阶段标签 + detail + retry-link（点击触发文件选择器）', async () => {
    const w = mountLibrary()
    await flushPromises()
    const row = rowOf(w, 'd-failed')
    const stepper = row.find('[data-testid="kb-doc-stepper"]')
    expect(stepper.exists()).toBe(true)
    expect(stepper.find('.st.fail').text()).toContain('归一化')
    const reason = row.find('[data-testid="kb-doc-fail-reason"]')
    expect(reason.exists()).toBe(true)
    expect(reason.find('.stage-tag').text()).toBe('normalizing')
    expect(reason.text()).toContain('加密 PDF 无法解析，请解除密码后重新上传')
    const clickSpy = vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(() => undefined)
    await row.find('[data-testid="kb-doc-retry"]').trigger('click')
    expect(clickSpy).toHaveBeenCalledTimes(1)
  })
})

describe('LibraryView — 解析摘要 en locale（CR-4：走既有 i18n 键，en 下不再出现中文）', () => {
  afterEach(() => {
    i18n.global.locale.value = 'zh-CN'
  })

  it('en-US：解析摘要列 = answer.kbParse* 键拼段（Pages/Tables/Code blocks）', async () => {
    i18n.global.locale.value = 'en-US'
    const w = mountLibrary()
    await flushPromises()
    expect(rowOf(w, 'd-ready').text()).toContain('Pages 24·Tables 3·Code blocks 12')
    expect(rowOf(w, 'd-url').text()).toContain('Tables 0·Code blocks 0')
    expect(rowOf(w, 'd-url').text()).not.toContain('Pages ')
    expect(w.text()).not.toContain('页 ')
    expect(w.text()).not.toContain('表格 ')
    w.unmount()
  })
})

describe('LibraryView — 删除级联确认流（api.md §4.5 二次确认 + 三存储提示）', () => {
  it('点删除 → alertdialog 三存储清单可见；取消不发 DELETE；确认才发并重拉', async () => {
    const w = mountLibrary()
    await flushPromises()
    await rowOf(w, 'd-ready').find('[data-testid="kb-doc-delete"]').trigger('click')
    await flushPromises()

    const modal = document.querySelector('[data-testid="kb-delete-confirm"]')
    expect(modal).not.toBeNull()
    // role=alertdialog + aria-modal（不可逆危险确认语义，ThreadModal 壳）
    const dialog = document.querySelector('[role="alertdialog"][aria-modal="true"]')
    expect(dialog).not.toBeNull()
    expect(dialog!.textContent).toContain('RAG-检索融合设计方案.pdf')
    // 三存储清单（kb_chunks ‖ ES ‖ MinIO）
    expect(modal!.querySelectorAll('.store-row')).toHaveLength(3)
    expect(modal!.textContent).toContain('pgvector')
    expect(modal!.textContent).toContain('Elasticsearch')
    expect(modal!.textContent).toContain('MinIO')

    // 取消：不发 DELETE、弹窗关闭
    document.querySelector<HTMLButtonElement>('[data-testid="kb-delete-cancel"]')!.click()
    await flushPromises()
    expect(mockedRemoveDocument).not.toHaveBeenCalled()
    expect(document.querySelector('[data-testid="kb-delete-confirm"]')).toBeNull()

    // 再开 → 确认：DELETE 发出、列表重拉、弹窗关闭
    await rowOf(w, 'd-ready').find('[data-testid="kb-doc-delete"]').trigger('click')
    await flushPromises()
    mockedRemoveDocument.mockResolvedValueOnce({ deleted: true })
    document.querySelector<HTMLButtonElement>('[data-testid="kb-delete-confirm-btn"]')!.click()
    await flushPromises()
    expect(mockedRemoveDocument).toHaveBeenCalledWith('d-ready')
    expect(document.querySelector('[data-testid="kb-delete-confirm"]')).toBeNull()
  })
})

describe('LibraryView — 六态图例与多选上传', () => {
  it('六态图例渲染（kb-legend：六个状态徽标）', async () => {
    const w = mountLibrary()
    await flushPromises()
    const legend = w.find('[data-testid="kb-legend"]')
    expect(legend.exists()).toBe(true)
    expect(legend.findAll('.badge')).toHaveLength(6)
    expect(legend.text()).toContain('归一化中')
    expect(legend.text()).toContain('已入库')
    expect(legend.text()).toContain('失败')
  })

  it('多选批量上传：多文件 input → 循环逐文件 uploadDocument（§4.1 单文件粒度端点）', async () => {
    const w = mountLibrary()
    await flushPromises()
    mockedUploadDocument.mockResolvedValue(DOCS[0]!)
    const input = w.find('[data-testid="upload-input"]')
    const f1 = new File(['a'], 'one.md', { type: 'text/markdown' })
    const f2 = new File(['b'], 'two.pdf', { type: 'application/pdf' })
    Object.defineProperty(input.element, 'files', { value: [f1, f2], configurable: true })
    await input.trigger('change')
    await flushPromises()
    expect(mockedUploadDocument).toHaveBeenCalledTimes(2)
    expect(mockedUploadDocument).toHaveBeenNthCalledWith(1, f1, 'one.md')
    expect(mockedUploadDocument).toHaveBeenNthCalledWith(2, f2, 'two.pdf')
  })

  it('逐文件校验循环：合法与非法混合 → 仅合法文件上传、非法逐条 toast 不中断批次', async () => {
    const w = mountLibrary()
    await flushPromises()
    mockedUploadDocument.mockResolvedValue(DOCS[0]!)
    const input = w.find('[data-testid="upload-input"]')
    const bad = new File(['x'], 'evil.exe', { type: 'application/octet-stream' })
    const good = new File(['a'], 'ok.md', { type: 'text/markdown' })
    Object.defineProperty(input.element, 'files', { value: [bad, good], configurable: true })
    await input.trigger('change')
    await flushPromises()
    expect(mockedUploadDocument).toHaveBeenCalledTimes(1)
    expect(mockedUploadDocument).toHaveBeenCalledWith(good, 'ok.md')
  })

  it('单文件超限（>20MB）前端拒绝', async () => {
    const w = mountLibrary()
    await flushPromises()
    const input = w.find('[data-testid="upload-input"]')
    const big = new File([new ArrayBuffer(21 * 1024 * 1024)], 'big.pdf', { type: 'application/pdf' })
    Object.defineProperty(input.element, 'files', { value: [big], configurable: true })
    await input.trigger('change')
    expect(mockedUploadDocument).not.toHaveBeenCalled()
  })
})

describe('LibraryView — URL 收集与轮询生命周期', () => {
  it('URL 收藏：合法 http(s) → collectUrl + 重拉；非法协议前端拒绝不触网', async () => {
    const w = mountLibrary()
    await flushPromises()
    mockedCollectUrl.mockResolvedValueOnce(DOCS[3]!)
    await w.find('[data-testid="url-input"]').setValue('https://example.com/new-post')
    await w.find('[data-testid="url-collect-btn"]').trigger('click')
    await flushPromises()
    expect(mockedCollectUrl).toHaveBeenCalledWith('https://example.com/new-post')

    await w.find('[data-testid="url-input"]').setValue('javascript:alert(1)')
    await w.find('[data-testid="url-collect-btn"]').trigger('click')
    await flushPromises()
    expect(mockedCollectUrl).toHaveBeenCalledTimes(1) // 拒绝路径零网络
  })

  it('挂载即拉取（文档+任务）；非终态条目驱动 5s 轮询；卸载停表', async () => {
    const w = mountLibrary()
    await flushPromises()
    expect(docPageCalls()).toBe(1)
    expect(mockedFetchImportJobs).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(5_000)
    expect(docPageCalls()).toBe(2)
    w.unmount()
    await vi.advanceTimersByTimeAsync(30_000)
    expect(docPageCalls()).toBe(2)
  })
})

describe('LibraryView — 计数投影与页码分页（R-3：api.md §4.3 分页控件交互）', () => {
  it('计数行数据源投影：文档 {docsTotal} · {readyTotal} 个已入库（非页内行数）', async () => {
    const w = mountLibrary()
    await flushPromises()
    expect(w.find('.doc-count').text()).toBe(`5 · 2 个已入库`)
    w.unmount()
  })

  it('分页控件渲染（totalPages>1）；点第 2 页 → goPage 请求 offset=50 + 行集合整页替换 + 滚动回表格头', async () => {
    const scrollSpy = vi.fn()
    Element.prototype.scrollIntoView = scrollSpy as typeof Element.prototype.scrollIntoView
    mockedFetchDocuments.mockImplementation(async (query?: FetchDocsQuery) => {
      if (query?.status === 'ready') return { total: 120, items: [] }
      return (query?.offset ?? 0) === 0
        ? { total: 120, items: DOCS }
        : { total: 120, items: [{ ...DOCS[0]!, document_id: 'p2-first', title: '第二页首行-unique-marker' }] }
    })
    const w = mountLibrary()
    await flushPromises()
    // 首屏第 1 页（total=120 → totalPages=3 → 控件渲染）
    const pager = w.find('[data-testid="kb-doc-pagination"]')
    expect(pager.exists()).toBe(true)
    expect(pager.attributes('aria-label')).toBe('分页导航')
    expect(w.find('[data-testid="kb-pager-page-1"]').attributes('aria-current')).toBe('page')
    expect(rowOf(w, 'd-ready').exists()).toBe(true)
    // 点第 2 页页码 → 整页替换（非追加）：原行退场、第二页行进场
    await w.find('[data-testid="kb-pager-page-2"]').trigger('click')
    await flushPromises()
    expect(mockedFetchDocuments).toHaveBeenCalledWith({ limit: 50, offset: 50 })
    expect(w.text()).toContain('第二页首行-unique-marker')
    expect(w.find('[data-doc-id="d-ready"]').exists()).toBe(false) // 原页行随整页替换退场
    expect(w.find('[data-testid="kb-pager-page-2"]').attributes('aria-current')).toBe('page')
    // 翻页后滚动回表格头（docHead scrollIntoView）
    expect(scrollSpy).toHaveBeenCalled()
    w.unmount()
    delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView
  })

  it('单页库（totalPages<=1）不渲染分页控件', async () => {
    const w = mountLibrary()
    await flushPromises()
    expect(w.find('[data-testid="kb-doc-pagination"]').exists()).toBe(false) // beforeEach mock total=5 → 单页
    w.unmount()
  })
})

describe('LibraryView — 表格列布局（P1-B：时间/数字列 nowrap、操作列保位）', () => {
  it('入库时间列与数字列 nowrap class（防竖排截断/挤压出视口）；操作列删除钮可达', async () => {
    const w = mountLibrary()
    await flushPromises()
    const row = rowOf(w, 'd-ready')
    expect(row.find('td.col-time').exists()).toBe(true)
    expect(row.findAll('td.col-num').length).toBeGreaterThanOrEqual(2) // 大小 + 切片数
    expect(w.find('th.col-time').exists()).toBe(true)
    expect(w.find('th.col-num').exists()).toBe(true)
    // 操作列（行内删除钮）保位可达
    expect(row.find('[data-testid="kb-doc-delete"]').exists()).toBe(true)
    // 状态列收窄 class（列预算重排锚点）
    expect(w.find('th.col-status').exists()).toBe(true)
    w.unmount()
  })
})
