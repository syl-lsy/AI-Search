/**
 * test/component/feishu-import-jobs.test.ts — FeishuImportJobs 组件层
 * （frontend-api.md §5 FeishuImportJobs 行；api.md §4.7~4.9 消费面；plan-m3-b6-f6 §1.1⑤）。
 * 连接器状态卡（健康徽标=最近任务成败投影，D7）+ 触发钮两态（idle/进行中 disabled，409007 守卫前置）
 * + 任务卡（进度条 aria/五数/failure_summary chips 原样 category/failures details 展开→详情请求/skipped 不静默）。
 */
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as kbApi from '@/api/kb'
import FeishuImportJobs from '@/components/library/FeishuImportJobs.vue'
import { i18n } from '@/i18n'
import { useKbStore } from '@/stores/kb'
import type { KbImportJob } from '@/types/api'

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

const mockedTriggerImport = vi.mocked(kbApi.triggerImport)
const mockedFetchImportJobs = vi.mocked(kbApi.fetchImportJobs)
const mockedFetchImportJobDetail = vi.mocked(kbApi.fetchImportJobDetail)

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
    failure_summary: [{ category: 'block_fetch_failed', count: 1 }],
    started_at: '2026-11-22T08:00:00Z',
    finished_at: null,
    created_at: '2026-11-22T07:59:58Z',
    updated_at: '2026-11-22T08:12:30Z',
    ...partial,
  }
}

function mountCard() {
  return mount(FeishuImportJobs, { global: { plugins: [createPinia(), i18n] } })
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('FeishuImportJobs — 连接器状态卡（D7 健康徽标=最近任务成败投影）', () => {
  it('无任务：连接器卡渲染（kb-feishu-card）+「未同步」中性徽标；任务卡不渲染', async () => {
    mockedFetchImportJobs.mockResolvedValue({ total: 0, items: [] })
    const w = mountCard()
    await flushPromises()
    expect(w.find('[data-testid="kb-feishu-card"]').exists()).toBe(true)
    expect(w.text()).toContain('未同步')
    expect(w.find('[data-testid="kb-import-jobs"]').exists()).toBe(false)
  })

  it('最新任务 completed → 健康徽标；failed → 异常徽标（created_at DESC 首条投影）', async () => {
    mockedFetchImportJobs.mockResolvedValue({
      total: 2,
      items: [job({ job_id: 'j-done', status: 'completed', finished_at: '2026-11-22T09:00:00Z' })],
    })
    let w = mountCard()
    await flushPromises()
    expect(w.text()).toContain('已连接 · 健康')
    w.unmount()

    mockedFetchImportJobs.mockResolvedValue({
      total: 1,
      items: [job({ job_id: 'j-fail', status: 'failed', finished_at: '2026-11-22T09:00:00Z' })],
    })
    setActivePinia(createPinia())
    w = mountCard()
    await flushPromises()
    expect(w.text()).toContain('异常')
    w.unmount()
  })

  it('最新任务 running → 进行中徽标（无需时间修饰的投影态）', async () => {
    mockedFetchImportJobs.mockResolvedValue({ total: 1, items: [job({ job_id: 'j-run' })] })
    const w = mountCard()
    await flushPromises()
    expect(w.text()).toContain('同步进行中')
  })
})

describe('FeishuImportJobs — 触发钮两态（409007 守卫前置）', () => {
  it('idle 态可点：click → store.triggerImport（经 api/kb.ts POST §4.7）', async () => {
    mockedFetchImportJobs.mockResolvedValue({ total: 0, items: [] })
    mockedTriggerImport.mockResolvedValue({ job_id: 'j-new', status: 'pending' })
    const w = mountCard()
    await flushPromises()
    const btn = w.find('[data-testid="kb-import-trigger"]')
    expect(btn.attributes('disabled')).toBeUndefined()
    await btn.trigger('click')
    await flushPromises()
    expect(mockedTriggerImport).toHaveBeenCalledTimes(1)
    // 成功后立即拉任务列表（plan §2.2：成功后立即 fetchJobs 启动轮询）
    expect(mockedFetchImportJobs.mock.calls.length).toBeGreaterThanOrEqual(2)
  })

  it('有 pending/running 任务 → disabled「导入进行中…」（点击不触发 POST）', async () => {
    mockedFetchImportJobs.mockResolvedValue({ total: 1, items: [job({ job_id: 'j1', status: 'running' })] })
    const w = mountCard()
    await flushPromises()
    const btn = w.find('[data-testid="kb-import-trigger"]')
    expect(btn.attributes('disabled')).toBeDefined()
    expect(btn.text()).toContain('导入进行中')
    await btn.trigger('click')
    expect(mockedTriggerImport).not.toHaveBeenCalled()
  })
})

describe('FeishuImportJobs — 任务卡（§4.8/§4.9 消费面）', () => {
  it('进度条 aria（aria-valuenow=processed/aria-valuemax=total）+ 五数 + failure chips category 原样', async () => {
    mockedFetchImportJobs.mockResolvedValue({
      total: 1,
      items: [
        job({
          job_id: 'j1',
          status: 'running',
          total_docs: 37,
          processed_docs: 12,
          imported_docs: 11,
          failed_docs: 1,
          skipped_docs: 2,
          failure_summary: [
            { category: 'block_fetch_failed', count: 1 },
            { category: 'unsupported_type', count: 2 },
          ],
        }),
      ],
    })
    const w = mountCard()
    await flushPromises()
    const card = w.find('[data-testid="kb-import-jobs"]')
    expect(card.exists()).toBe(true)
    const pbar = card.find('[role="progressbar"]')
    expect(pbar.attributes('aria-valuenow')).toBe('12')
    expect(pbar.attributes('aria-valuemax')).toBe('37')
    expect(pbar.attributes('aria-valuemin')).toBe('0')
    // 进度五数（total/processed/imported/failed/skipped）
    expect(card.text()).toContain('12')
    expect(card.text()).toContain('11')
    expect(card.text()).toContain('1')
    expect(card.text()).toContain('2')
    // failure_summary category 原样展示不解析（非封闭枚举）
    expect(card.text()).toContain('block_fetch_failed')
    expect(card.text()).toContain('unsupported_type')
    // skipped 不静默（A53 GWT#3）：skipped 五数 + skip 类 chip 可见
    expect(card.text()).toContain('跳过')
  })

  it('failures details 展开 → fetchImportJobDetail(job_id)；清单行 title/category/message 渲染', async () => {
    mockedFetchImportJobs.mockResolvedValue({ total: 1, items: [job({ job_id: 'j1', status: 'running' })] })
    mockedFetchImportJobDetail.mockResolvedValue({
      ...job({ job_id: 'j1' }),
      failures: [
        { title: '调研 mindnote', category: 'unsupported_type', message: 'mindnote 无读取 API，已跳过', document_id: null },
        { title: '2026 预算表', category: 'block_fetch_failed', message: 'blocks 拉取 429 限流，重试耗尽', document_id: null },
      ],
    })
    const w = mountCard()
    await flushPromises()
    expect(mockedFetchImportJobDetail).not.toHaveBeenCalled()
    await w.find('[data-testid="kb-job-failures"] summary').trigger('click')
    // <details> toggle 事件在 jsdom 经 trigger('toggle') 派发
    await w.find('[data-testid="kb-job-failures"]').trigger('toggle')
    await flushPromises()
    expect(mockedFetchImportJobDetail).toHaveBeenCalledWith('j1')
    expect(w.find('[data-testid="kb-job-failures"]').text()).toContain('调研 mindnote')
    expect(w.find('[data-testid="kb-job-failures"]').text()).toContain('mindnote 无读取 API，已跳过')
    expect(w.find('[data-testid="kb-job-failures"]').text()).toContain('2026 预算表')
  })

  it('上一次完成任务投影行：completed 任务五数摘要渲染（最新 completed 投影）', async () => {
    mockedFetchImportJobs.mockResolvedValue({
      total: 2,
      items: [
        job({ job_id: 'j-run', status: 'running', created_at: '2026-11-23T08:00:00Z' }),
        job({
          job_id: 'j-done',
          status: 'completed',
          total_docs: 931,
          processed_docs: 931,
          imported_docs: 925,
          failed_docs: 4,
          skipped_docs: 2,
          finished_at: '2026-11-22T09:06:00Z',
          created_at: '2026-11-22T07:59:58Z',
        }),
      ],
    })
    const w = mountCard()
    await flushPromises()
    const prev = w.find('[data-testid="kb-job-prev"]')
    expect(prev.exists()).toBe(true)
    expect(prev.text()).toContain('925')
    expect(prev.text()).toContain('4')
    expect(prev.text()).toContain('2')
  })

  it('任务列表拉取失败 → 上抛 toast 链路（不落假数据、任务卡不渲染）', async () => {
    mockedFetchImportJobs.mockRejectedValue(new Error('boom'))
    const w = mountCard()
    await flushPromises()
    expect(w.find('[data-testid="kb-import-jobs"]').exists()).toBe(false)
    expect(w.find('[data-testid="kb-feishu-card"]').exists()).toBe(true)
  })

  it('CR-3：details 收起 → 清空 store.importJobDetail（tick 判据收敛为真展开态，不再续拉旧 job）', async () => {
    const pinia = createPinia()
    mockedFetchImportJobs.mockResolvedValue({ total: 1, items: [job({ job_id: 'j1', status: 'running' })] })
    mockedFetchImportJobDetail.mockResolvedValue({
      ...job({ job_id: 'j1' }),
      failures: [{ title: '调研 mindnote', category: 'unsupported_type', message: 'mindnote 无读取 API，已跳过', document_id: null }],
    })
    const w = mount(FeishuImportJobs, { global: { plugins: [pinia, i18n] } })
    const store = useKbStore(pinia)
    await flushPromises()
    const details = w.find('[data-testid="kb-job-failures"]')
    // 展开 → §4.9 详情请求 + store 落详情
    ;(details.element as HTMLDetailsElement).open = true
    await details.trigger('toggle')
    await flushPromises()
    expect(mockedFetchImportJobDetail).toHaveBeenCalledWith('j1')
    expect(store.importJobDetail?.job_id).toBe('j1')
    // 收起 → store 详情清空（旧实现仅 early-return、详情滞留 → tick 持续请求旧 job_id）
    ;(details.element as HTMLDetailsElement).open = false
    await details.trigger('toggle')
    await flushPromises()
    expect(store.importJobDetail).toBeNull()
    w.unmount()
  })

  it('R-3/F-2：displayJob 切换（新任务 B 顶替）→ details 收起 + 清 importJobDetail（展开态不跨任务继承）', async () => {
    const pinia = createPinia()
    mockedFetchImportJobs.mockResolvedValue({ total: 1, items: [job({ job_id: 'j-a', status: 'running' })] })
    mockedFetchImportJobDetail.mockResolvedValue({
      ...job({ job_id: 'j-a' }),
      failures: [{ title: '调研 mindnote', category: 'unsupported_type', message: 'mindnote 无读取 API，已跳过', document_id: null }],
    })
    const w = mount(FeishuImportJobs, { global: { plugins: [pinia, i18n] } })
    const store = useKbStore(pinia)
    await flushPromises()
    // 任务 A 展开中：详情请求 + store 落详情
    const details = w.find('[data-testid="kb-job-failures"]')
    ;(details.element as HTMLDetailsElement).open = true
    await details.trigger('toggle')
    await flushPromises()
    expect(store.importJobDetail?.job_id).toBe('j-a')
    // 进行中触发新任务 B → 列表投影换成 B（直写 store 模拟轮询结果）
    store.importJobs = [job({ job_id: 'j-b', status: 'pending' })]
    await flushPromises()
    // details DOM 收起 + 详情清空：showDetail(B) 恒 false 的「展开但空白」态不再出现
    expect((w.find('[data-testid="kb-job-failures"]').element as HTMLDetailsElement).open).toBe(false)
    expect(store.importJobDetail).toBeNull()
    w.unmount()
  })
})

describe('FeishuImportJobs — en locale 词条契约对齐（CR-2：frontend-api §7 逐字防漂）', () => {
  afterEach(() => {
    i18n.global.locale.value = 'zh-CN'
  })

  it('en-US：触发钮 importRunning / 任务卡 importFeishu·importFailedCount·importSkippedCount 逐字', async () => {
    i18n.global.locale.value = 'en-US'
    mockedFetchImportJobs.mockResolvedValue({
      total: 1,
      items: [job({ job_id: 'j1', status: 'running', failed_docs: 4, skipped_docs: 2 })],
    })
    const w = mountCard()
    await flushPromises()
    expect(w.find('[data-testid="kb-import-trigger"]').text()).toContain('Import in progress')
    const card = w.find('[data-testid="kb-import-jobs"]')
    expect(card.text()).toContain('Import from Feishu')
    expect(card.text()).toContain('4 failed')
    expect(card.text()).toContain('2 skipped')
    // 旧文案逐字不回退（防再漂）
    expect(w.text()).not.toContain('Feishu import')
    expect(w.text()).not.toContain('Import running')
    expect(w.text()).not.toContain('Failed 4')
    expect(w.text()).not.toContain('Skipped 2')
    w.unmount()
  })
})

describe('FeishuImportJobs — 健康徽标圆点取色（CR-5：语义对齐 tokens.dot-*）', () => {
  it('running → dot-accent（accent pulse，与处理中态同款）；completed → dot-success', async () => {
    mockedFetchImportJobs.mockResolvedValue({ total: 1, items: [job({ job_id: 'j-run', status: 'running' })] })
    let w = mountCard()
    await flushPromises()
    const runningDot = w.find('.conn-card .badge .dot')
    expect(runningDot.classes()).toContain('dot-accent')
    expect(runningDot.classes()).not.toContain('dot-success')
    w.unmount()

    mockedFetchImportJobs.mockResolvedValue({
      total: 1,
      items: [job({ job_id: 'j-done', status: 'completed', finished_at: '2026-11-22T09:00:00Z' })],
    })
    w = mountCard()
    await flushPromises()
    expect(w.find('.conn-card .badge .dot').classes()).toContain('dot-success')
    w.unmount()
  })
})
