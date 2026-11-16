/**
 * test/component/threads-batch.test.ts — ThreadsView 批量操作栏组件层（frontend-api.md §5
 * v1.23 / 对标 Perplexity Library 批量形态；用户裁决动作集=删除/归档/标记为未读）。
 *
 * 断言面：行级复选框（hover 显形为 CSS，不属 jsdom 断言面）+ 表头全选（含 indeterminate）
 * + 批量栏替换筛选行（已选 N + 删除/归档/标记未读三钮 + 清除）+ Esc 退出 + 批量删除居中
 * 确认 modal（删除 N 个会话？不可撤销警示）+ 动作分发至 store 批量动作（部分失败汇总 toast）。
 */
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, apiDelete, apiGet, apiPatch } from '@/api/client'
import { useToasts } from '@/composables/toast'
import { i18n } from '@/i18n'
import { modelsFixture } from '@/mocks/models'
import { threadListFixture } from '@/mocks/threads'
import { router } from '@/router'
import ThreadsView from '@/views/ThreadsView.vue'

/** client mock 保真：normalizeError 透传 ApiError 的 code/message（store 部分失败汇总依赖服务端 message）；
 *  类定义收进 factory（vi.mock 提升至文件顶，factory 不得引用外部顶层变量） */
vi.mock('@/api/client', () => {
  class MockApiError extends Error {
    code: string
    constructor(code: string, message: string) {
      super(message)
      this.code = code
      this.name = 'ApiError'
    }
  }
  return {
    API_BASE_URL: '',
    apiGet: vi.fn(),
    apiPost: vi.fn(),
    apiPut: vi.fn(),
    apiPatch: vi.fn(),
    apiDelete: vi.fn(),
    apiDownload: vi.fn(),
    ApiError: MockApiError,
    normalizeError: (e: unknown) => {
      if (e instanceof MockApiError) return { name: 'ApiError', code: e.code, message: e.message }
      return { name: 'Error', code: 'network_error', message: e instanceof Error ? e.message : '网络异常，请稍后重试' }
    },
  }
})

const mockedGet = vi.mocked(apiGet)
const mockedPatch = vi.mocked(apiPatch)
const mockedDelete = vi.mocked(apiDelete)

class MockIntersectionObserver {
  static instances: MockIntersectionObserver[] = []
  cb: IntersectionObserverCallback
  observe = vi.fn()
  disconnect = vi.fn()
  unobserve = vi.fn()
  constructor(cb: IntersectionObserverCallback) {
    this.cb = cb
    MockIntersectionObserver.instances.push(this)
  }
}

const IDS = threadListFixture.items.map((i) => i.thread_id)

beforeEach(async () => {
  setActivePinia(createPinia())
  localStorage.clear()
  vi.clearAllMocks()
  i18n.global.locale.value = 'zh-CN'
  mockedGet.mockImplementation(async (url: string) => {
    if (url === '/api/v1/models') return modelsFixture
    if (url === '/api/v1/threads') return threadListFixture
    throw new Error(`unexpected GET ${url}`)
  })
  MockIntersectionObserver.instances = []
  vi.stubGlobal('IntersectionObserver', MockIntersectionObserver)
  await router.push('/')
  await router.isReady()
})

afterEach(async () => {
  mounted.splice(0).forEach((w) => w.unmount()) // onUnmounted 钩子（window keydown 移除/清空选择）必须真实触发
  document.body.innerHTML = ''
  vi.unstubAllGlobals()
  await router.push('/')
  await flushPromises()
})

/** attachTo：焦点返还断言（评审 P3-2）须走真实 document 焦点语义（游离树 jsdom focus 为 no-op） */
const mounted: VueWrapper[] = []

async function mountThreads(): Promise<VueWrapper> {
  const w = mount(ThreadsView, { global: { plugins: [createPinia(), router, i18n] }, attachTo: document.body })
  mounted.push(w)
  await flushPromises()
  return w
}

async function selectRows(w: VueWrapper, ids: string[]): Promise<void> {
  for (const id of ids) {
    await w.find(`[data-testid="thread-check-${id}"]`).setValue(true)
  }
  await flushPromises()
}

describe('批量选择（行级复选框 + 表头全选 indeterminate）', () => {
  it('勾选行复选框 → 批量栏替换筛选行（已选择 N 个）；取消勾选 → 还原筛选行', async () => {
    const w = await mountThreads()
    expect(w.find('[data-testid="batch-bar"]').exists()).toBe(false)
    expect(w.find('[data-testid="view-filter-radio"]').exists()).toBe(true)
    await selectRows(w, [IDS[0]!])
    expect(w.find('[data-testid="batch-bar"]').exists()).toBe(true)
    expect(w.find('[data-testid="batch-count"]').text()).toBe('已选择 1 个')
    await w.find(`[data-testid="thread-check-${IDS[0]!}"]`).setValue(false)
    await flushPromises()
    expect(w.find('[data-testid="batch-bar"]').exists()).toBe(false)
    expect(w.find('[data-testid="view-filter-radio"]').exists()).toBe(true)
  })

  it('表头全选：勾选 → 全部行勾选；再点 → 全清；部分选中态 indeterminate=true', async () => {
    const w = await mountThreads()
    await selectRows(w, [IDS[0]!])
    const all = w.find('[data-testid="batch-select-all"]')
    expect((all.element as HTMLInputElement).indeterminate).toBe(true) // 1/3 → 半选
    await all.setValue(true)
    await flushPromises()
    for (const id of IDS) {
      expect((w.find(`[data-testid="thread-check-${id}"]`).element as HTMLInputElement).checked).toBe(true)
    }
    expect(w.find('[data-testid="batch-count"]').text()).toBe('已选择 3 个')
    expect((all.element as HTMLInputElement).indeterminate).toBe(false)
    expect((all.element as HTMLInputElement).checked).toBe(true)
    // 再点全选 → 全清
    await all.setValue(false)
    await flushPromises()
    expect(w.find('[data-testid="batch-bar"]').exists()).toBe(false)
  })

  it('视图切换清空选择（批量栏消失、筛选行还原）；Esc 同效；清除钮同效', async () => {
    const w = await mountThreads()
    await selectRows(w, [IDS[0]!, IDS[1]!])
    expect(w.find('[data-testid="batch-bar"]').exists()).toBe(true)
    // 切换视图 → 首拉清空选择（选中态下筛选行已被批量栏替换——经仍可见的 mode chip 驱动切换）
    mockedGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/models') return modelsFixture
      if (url === '/api/v1/threads') return threadListFixture
      throw new Error(`unexpected GET ${url}`)
    })
    await w.find('[data-testid="threads-filter-deep"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="batch-bar"]').exists()).toBe(false)
    expect(w.find('[data-testid="view-filter-radio"]').exists()).toBe(true)
    // Esc 退出
    await selectRows(w, [IDS[0]!])
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await flushPromises()
    expect(w.find('[data-testid="batch-bar"]').exists()).toBe(false)
    // 清除钮
    await selectRows(w, [IDS[0]!])
    await w.find('[data-testid="batch-clear"]').trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="batch-bar"]').exists()).toBe(false)
  })
})

describe('筛选行常显全选框（v1.24：对标 Perplexity——常规态即可全选当前视图）', () => {
  it('常规态筛选行最左侧常显全选框（未选中空框）；勾选 → 当前视图全量条目入 selection（批量栏接管显示已选 N）', async () => {
    const w = await mountThreads()
    expect(w.find('[data-testid="batch-bar"]').exists()).toBe(false)
    const all = w.find('[data-testid="filter-select-all"]')
    expect(all.exists()).toBe(true)
    expect((all.element as HTMLInputElement).checked).toBe(false)
    await all.setValue(true)
    await flushPromises()
    expect(w.find('[data-testid="batch-bar"]').exists()).toBe(true)
    expect(w.find('[data-testid="batch-count"]').text()).toBe('已选择 3 个')
    for (const id of IDS) {
      expect((w.find(`[data-testid="thread-check-${id}"]`).element as HTMLInputElement).checked).toBe(true)
    }
  })

  it('DOM 序语义：全选框位于视图 radio 左侧（筛选行首个子元素）', async () => {
    const w = await mountThreads()
    const row = w.find('.filter-row')
    const first = row.element.children[0] as HTMLElement | undefined
    expect(first?.querySelector('[data-testid="filter-select-all"]')).not.toBeNull()
    expect(first?.contains(row.element.querySelector('[data-testid="view-filter-radio"]') ?? null)).toBe(false)
  })

  it('批量栏取消全选 → 筛选行还原且常显全选框复位（勾选/清空循环可用）', async () => {
    const w = await mountThreads()
    await w.find('[data-testid="filter-select-all"]').setValue(true)
    await flushPromises()
    await w.find('[data-testid="batch-select-all"]').setValue(false)
    await flushPromises()
    expect(w.find('[data-testid="batch-bar"]').exists()).toBe(false)
    const all = w.find('[data-testid="filter-select-all"]')
    expect(all.exists()).toBe(true)
    expect((all.element as HTMLInputElement).checked).toBe(false)
    // 再次勾选仍可用（循环）
    await all.setValue(true)
    await flushPromises()
    expect(w.find('[data-testid="batch-bar"]').exists()).toBe(true)
  })
})

describe('批量动作（分发至 store batch 三动作；删除走居中确认 modal）', () => {
  it('批量删除：确认 modal（「删除选中的 2 个会话？」+ 不可撤销警示）；确认 → DELETE ×2、行消失、批量栏收起；取消零请求', async () => {
    const w = await mountThreads()
    await selectRows(w, [IDS[0]!, IDS[1]!])
    await w.find('[data-testid="batch-delete"]').trigger('click')
    // 居中 modal（Teleport → document 查询）
    const panel = document.querySelector('.tm-panel')
    expect(panel).not.toBeNull()
    expect(panel!.getAttribute('role')).toBe('alertdialog')
    expect(panel!.textContent).toContain('删除选中的 2 个会话？')
    expect(panel!.textContent).toContain('不可恢复')
    // 取消 → 零请求
    ;(document.querySelector('[data-testid="batch-cancel"]') as HTMLElement).click()
    await flushPromises()
    expect(mockedDelete).not.toHaveBeenCalled()
    // 再开 → 确认
    await w.find('[data-testid="batch-delete"]').trigger('click')
    mockedDelete.mockResolvedValue({ deleted: true })
    ;(document.querySelector('[data-testid="batch-confirm-delete"]') as HTMLElement).click()
    await flushPromises()
    expect(mockedDelete).toHaveBeenCalledWith(`/api/v1/threads/${IDS[0]}`)
    expect(mockedDelete).toHaveBeenCalledWith(`/api/v1/threads/${IDS[1]}`)
    expect(w.find(`[data-testid="thread-row-${IDS[0]!}"]`).exists()).toBe(false)
    expect(w.find('[data-testid="batch-bar"]').exists()).toBe(false) // 成功后收起
  })

  it('批量归档（活跃视图）：PATCH {archived:true} 逐个发送；行移出列表', async () => {
    const w = await mountThreads()
    await selectRows(w, [IDS[0]!])
    mockedPatch.mockResolvedValue({ ...threadListFixture.items[0]!, archived: true })
    await w.find('[data-testid="batch-archive"]').trigger('click')
    await flushPromises()
    expect(mockedPatch).toHaveBeenCalledWith(`/api/v1/threads/${IDS[0]}`, { archived: true })
    expect(w.find(`[data-testid="thread-row-${IDS[0]!}"]`).exists()).toBe(false)
  })

  it('归档视图：批量钮语义反转=取消归档（PATCH {archived:false}）；标记为未读钮隐藏', async () => {
    const w = await mountThreads()
    // 归档视图提供 archived:true 投影（toggleArchive 语义随条目 archived 翻转）
    const archivedItems = threadListFixture.items.map((i) => ({ ...i, archived: true }))
    mockedGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/models') return modelsFixture
      if (url === '/api/v1/threads') return { total: archivedItems.length, items: archivedItems }
      throw new Error(`unexpected GET ${url}`)
    })
    await w.find('[data-testid="view-filter-archived"]').trigger('click')
    await flushPromises()
    await selectRows(w, [IDS[0]!])
    expect(w.find('[data-testid="batch-unread"]').exists()).toBe(false)
    // 归档语义反转：aria-label（与 tooltip label 同源）
    expect(w.find('[data-testid="batch-archive"]').attributes('aria-label')).toBe('取消归档')
    mockedPatch.mockResolvedValue({ ...archivedItems[0]!, archived: false })
    await w.find('[data-testid="batch-archive"]').trigger('click')
    await flushPromises()
    expect(mockedPatch).toHaveBeenCalledWith(`/api/v1/threads/${IDS[0]}`, { archived: false })
  })

  it('批量标记为未读：PATCH {is_unread:true}；未读点出现', async () => {
    const w = await mountThreads()
    await selectRows(w, [IDS[0]!])
    mockedPatch.mockResolvedValue({ ...threadListFixture.items[0]!, is_unread: true })
    await w.find('[data-testid="batch-unread"]').trigger('click')
    await flushPromises()
    expect(mockedPatch).toHaveBeenCalledWith(`/api/v1/threads/${IDS[0]}`, { is_unread: true })
    expect(w.find(`[data-testid="unread-dot-${IDS[0]!}"]`).exists()).toBe(true)
  })

  it('批量删除确认 modal 取消/Esc 后焦点返还批量删除钮（评审 P3-2：与 ThreadActionMenu closeAll(true) 对称）', async () => {
    const w = await mountThreads()
    await selectRows(w, [IDS[0]!])
    const delBtn = w.find('[data-testid="batch-delete"]')
    // 模拟真实流：用户焦点在删除钮上（打开后焦点被引到取消钮）
    ;(delBtn.element as HTMLElement).focus()
    expect(document.activeElement).toBe(delBtn.element)
    await delBtn.trigger('click')
    expect(document.activeElement).toBe(document.querySelector('[data-testid="batch-cancel"]'))
    // Esc 关闭 → 焦点返还删除钮
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await flushPromises()
    expect(document.querySelector('.tm-panel')).toBeNull()
    expect(document.activeElement).toBe(delBtn.element)
    // 取消钮路径同效
    await delBtn.trigger('click')
    ;(document.querySelector('[data-testid="batch-cancel"]') as HTMLElement).click()
    await flushPromises()
    expect(document.querySelector('.tm-panel')).toBeNull()
    expect(document.activeElement).toBe(delBtn.element)
  })

  it('部分失败汇总 toast（409005）：「成功 1 个，失败 1 个」+ 服务端 message；失败行保留', async () => {
    const w = await mountThreads()
    await selectRows(w, [IDS[0]!, IDS[1]!])
    mockedDelete.mockImplementation(async (url: string) => {
      if (url.endsWith(IDS[1]!)) throw new ApiError('409005', '线程有进行中轮次，不可删除')
      return { deleted: true }
    })
    await w.find('[data-testid="batch-delete"]').trigger('click')
    ;(document.querySelector('[data-testid="batch-confirm-delete"]') as HTMLElement).click()
    await flushPromises()
    const messages = useToasts().value.map((t) => t.message)
    expect(messages.some((m) => m.includes('成功 1 个') && m.includes('失败 1 个') && m.includes('线程有进行中轮次，不可删除'))).toBe(true)
    expect(w.find(`[data-testid="thread-row-${IDS[1]!}"]`).exists()).toBe(true) // 失败行保留
    expect(w.find('[data-testid="batch-bar"]').exists()).toBe(false) // 选择仍收尾清空
  })
})
