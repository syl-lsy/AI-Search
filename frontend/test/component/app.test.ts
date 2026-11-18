import { createPinia, getActivePinia, setActivePinia } from 'pinia'
import { DOMWrapper, flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { apiDelete, apiGet, apiPatch, apiPost } from '@/api/client'
import App from '@/App.vue'
import { useToasts } from '@/composables/toast'
import { i18n } from '@/i18n'
import { modelsFixture } from '@/mocks/models'
import { THREAD_ID_FAST, TURN_ID_FAST, turnDetailFastCompleted } from '@/mocks/turnDetails'
import { threadListFixture } from '@/mocks/threads'
import { router } from '@/router'
import { usePrefsStore } from '@/stores/prefs'
import { useTurnStore } from '@/stores/turn'

const VALID_THREAD_ID = THREAD_ID_FAST

/**
 * 组件层装配冒烟：真实 router + i18n 单例 + 独立 pinia（§4 store 依赖隔离）。
 * prefsStore.setLang 写的是 '@/i18n' 单例——挂载也必须用同一单例，切换才对界面响应式生效。
 * REST（@/api/client）mock：组件层不打真网络（fixture 驱动，§9）。
 */
vi.mock('@/api/client', () => ({
  API_BASE_URL: '',
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPut: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
  apiDownload: vi.fn(),
  // 视图层错误归一出口（toast 路径）；组件层不打真网络，兜底文案与真实实现一致
  normalizeError: () => ({ name: 'ApiError', code: 'network_error', message: '网络异常，请稍后重试' }),
}))

const mockedGet = vi.mocked(apiGet)
const mockedPatch = vi.mocked(apiPatch)
const mockedDelete = vi.mocked(apiDelete)
const mockedPost = vi.mocked(apiPost)

/** 按 URL 分发 fixture：threads 列表 / 线程详情 / 轮次详情（用例内 mockReset 后由 beforeEach 重装） */
function installDefaultGetMock(): void {
  mockedGet.mockReset()
  mockedGet.mockImplementation(async (url: string) => {
  if (url === '/api/v1/threads') return threadListFixture
  if (url === `/api/v1/threads/${VALID_THREAD_ID}`) {
    return {
      thread_id: VALID_THREAD_ID,
      title: 'LangGraph 的 Send API 和 Router 的区别？',
      mode_summary: 'fast',
      turn_count: 1,
      has_artifacts: false,
      created_at: '2026-11-01T08:12:00Z',
      updated_at: '2026-11-01T09:40:11Z',
      turns: [
        {
          turn_id: TURN_ID_FAST,
          mode: 'fast',
          status: 'completed',
          query: turnDetailFastCompleted.query,
          query_lang: 'zh',
          has_report: false,
          degraded: false,
          duration_ms: 7420,
          created_at: '2026-11-01T08:12:00Z',
        },
      ],
    }
  }
  if (url === `/api/v1/turns/${TURN_ID_FAST}`) return turnDetailFastCompleted
  throw new Error(`unexpected GET ${url}`)
})
}
installDefaultGetMock()

mockedPost.mockResolvedValue({})

let wrapper: VueWrapper | null = null

async function mountApp(): Promise<VueWrapper> {
  const pinia = createPinia()
  setActivePinia(pinia)
  wrapper = mount(App, {
    // attachTo：焦点断言（P3 面板关闭焦点返还）须走真实 document 焦点语义——游离树上
    // jsdom focus() 为 no-op；afterEach unmount 时随树摘除
    attachTo: document.body,
    global: { plugins: [pinia, router, i18n] },
  })
  await router.isReady()
  await flushPromises()
  return wrapper
}

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
})

describe('App 壳（组件层冒烟：装配 + 懒加载四页 + i18n）', () => {
  beforeEach(async () => {
    localStorage.clear()
    sessionStorage.clear()
    vi.clearAllMocks()
    installDefaultGetMock()
    i18n.global.locale.value = 'zh-CN'
    await router.push('/')
    await flushPromises()
  })

  it('根壳渲染：侧栏导航 + 图标 sprite + 首页（RouterView 懒加载解析）', async () => {
    const app = await mountApp()
    expect(app.find('[data-testid="app-shell"]').exists()).toBe(true)
    expect(app.find('[data-testid="sidebar"]').exists()).toBe(true)
    expect(app.find('[data-testid="nav-home"]').exists()).toBe(true)
    expect(app.find('[data-testid="home-view"]').text()).toContain('你想了解什么？')
  })

  it('路由切换：/threads 与 /library（懒加载视图就位）', async () => {
    const app = await mountApp()

    await router.push('/threads')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('threads')
    expect(app.find('[data-testid="threads-view"]').exists()).toBe(true)

    await router.push('/library')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('library')
    expect(app.find('[data-testid="library-view"]').exists()).toBe(true)
  })

  it('AnswerView：合法 threadId → 2.3+2.5 渲染轮次（§1 编排）；非法参数渲染空态不触 API（§8.3）', async () => {
    const app = await mountApp()

    await router.push(`/thread/${VALID_THREAD_ID}`)
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('answer')
    expect(mockedGet).toHaveBeenCalledWith(`/api/v1/threads/${VALID_THREAD_ID}`)
    // 轮次投影：问题气泡 + AnswerRenderer 内容（fixture answer_md 首句）
    expect(app.find('[data-testid="answer-view"]').text()).toContain(turnDetailFastCompleted.query)
    expect(app.find('[data-testid="turn-block"]').exists()).toBe(true)
    expect(app.find('[data-testid="answer-renderer"]').text()).toContain('Send API 用于')

    await router.push('/thread/not-a-uuid')
    await flushPromises()
    expect(app.find('[data-testid="answer-view"]').text()).toContain('线程不存在或链接无效')
  })

  it('语言切换即时生效：prefsStore.setLang → 界面文案响应式切 en（§7，不刷新）', async () => {
    const app = await mountApp()
    const prefs = usePrefsStore()
    prefs.setLang('en-US')
    await flushPromises()
    expect(app.find('[data-testid="nav-home"]').text()).toBe('New')
    expect(app.find('[data-testid="home-view"]').text()).toContain('What do you want to know?')
  })

  it('侧栏「最近」行：生成中线程带 i-refresh 旋转指示（title=threads.generating；与 ThreadsView 口径一致，P2）', async () => {
    const app = await mountApp()
    // 侧栏最近列表已拉取（fixture 第一条 = THREAD_ID_FAST）；初始无生成中指示
    expect(app.find(`[data-testid="sb-generating-${THREAD_ID_FAST}"]`).exists()).toBe(false)
    const turnStore = useTurnStore(getActivePinia())
    turnStore.generatingThreadIds.add(THREAD_ID_FAST)
    await flushPromises()
    const indicator = app.find(`[data-testid="sb-generating-${THREAD_ID_FAST}"]`)
    expect(indicator.exists()).toBe(true)
    expect(indicator.attributes('title')).toBe('生成中')
    expect(indicator.find('svg').classes().join(' ')).toContain('icon-sm')
    turnStore.generatingThreadIds.delete(THREAD_ID_FAST)
    await flushPromises()
    expect(app.find(`[data-testid="sb-generating-${THREAD_ID_FAST}"]`).exists()).toBe(false)
  })

  it('最近列表底部「查看全部」链接 → /threads（REQ-058：侧栏入口；v1.21）', async () => {
    const app = await mountApp()
    const viewAll = app.find('[data-testid="sb-view-all"]')
    expect(viewAll.exists()).toBe(true)
    await viewAll.trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/threads')
  })

  it('未读点指示：is_unread 驱动（aria-label=threads.unreadLabel）；已读项无点（REQ-060）', async () => {
    const items = [
      { ...threadListFixture.items[0]!, is_unread: true },
      threadListFixture.items[1]!,
    ]
    // 用例内全量接管（挂载首拍 models 与 threads 调用序不定——不分发次序，全 URL 收口）
    installDefaultGetMock()
    mockedGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/models') return modelsFixture
      if (url === '/api/v1/threads') return { total: items.length, items }
      throw new Error(`unexpected GET ${url}`)
    })
    const app = await mountApp()
    const dot = app.find(`[data-testid="sb-unread-${items[0]!.thread_id}"]`)
    expect(dot.exists()).toBe(true)
    expect(dot.attributes('aria-label')).toBe('未读')
    expect(app.find(`[data-testid="sb-unread-${items[1]!.thread_id}"]`).exists()).toBe(false)
  })

  it('归档线程不出现在侧栏最近列表（frontend-api §1 侧栏行 v1.21 注）', async () => {
    const items = [
      threadListFixture.items[0]!,
      { ...threadListFixture.items[1]!, archived: true },
    ]
    installDefaultGetMock()
    mockedGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/models') return modelsFixture
      if (url === '/api/v1/threads') return { total: items.length, items }
      throw new Error(`unexpected GET ${url}`)
    })
    const app = await mountApp()
    expect(app.find(`[data-testid="sb-recent-row-${items[0]!.thread_id}"]`).exists()).toBe(true)
    expect(app.find(`[data-testid="sb-recent-row-${items[1]!.thread_id}"]`).exists()).toBe(false)
  })

  it('侧栏导航收敛三项（Perplexity 对齐小批）：新建/知识库/历史；「会话」导航项移除', async () => {
    const app = await mountApp()
    const navItems = app.findAll('.sb-nav > .nav-item')
    expect(navItems.map((x) => x.attributes('data-testid'))).toEqual(['nav-home', 'nav-library', 'nav-threads'])
    expect(navItems.map((x) => x.text())).toEqual(['新建', '知识库', '历史'])
    expect(app.find('[data-testid="nav-chat"]').exists()).toBe(false)
  })
})

describe('侧栏最近项管理动作（REQ-059：ThreadActionMenu 共用组件；store 动作面自愈）', () => {
  beforeEach(async () => {
    localStorage.clear()
    sessionStorage.clear()
    vi.clearAllMocks()
    installDefaultGetMock()
    i18n.global.locale.value = 'zh-CN'
    await router.push('/')
    await flushPromises()
  })

  it('最近项三点菜单：五项（置顶/标记为未读/重命名/归档/删除）；点置顶 → PATCH 经唯一出口', async () => {
    const app = await mountApp()
    const row = app.find(`[data-testid="sb-recent-row-${THREAD_ID_FAST}"]`)
    expect(row.exists()).toBe(true)
    await row.find('[data-testid="tam-trigger"]').trigger('click')
    const items = Array.from(row.element.ownerDocument.querySelectorAll('[role="menuitem"]')).map((el) => new DOMWrapper(el as HTMLElement))
    expect(items.map((x) => x.text())).toEqual(['置顶', '标记为未读', '重命名', '归档', '删除'])
    mockedPatch.mockResolvedValueOnce({ ...threadListFixture.items[0]!, pinned: true })
    await items[0]!.trigger('click')
    await flushPromises()
    expect(mockedPatch).toHaveBeenCalledWith(`/api/v1/threads/${THREAD_ID_FAST}`, { pinned: true })
  })

  it('删除二次确认后 DELETE；菜单动作后侧栏随 store 自愈（行消失；v1.23 确认层迁居中 modal——Teleport 后 document 查询）', async () => {
    const app = await mountApp()
    const row = app.find(`[data-testid="sb-recent-row-${THREAD_ID_FAST}"]`)
    await row.find('[data-testid="tam-trigger"]').trigger('click')
    await new DOMWrapper(document.querySelectorAll('[role="menuitem"]')[4] as HTMLElement).trigger('click') // 删除（弹层 Teleport 到 body——document 级查询）
    // 居中 modal Teleport 到 body——document 查询（wrapper.find 不穿 Teleport）
    expect(document.querySelector('[data-testid="tam-confirm"]')).not.toBeNull()
    mockedDelete.mockResolvedValueOnce({ deleted: true })
    ;(document.querySelector('[data-testid="tam-confirm-delete"]') as HTMLElement).click()
    await flushPromises()
    expect(mockedDelete).toHaveBeenCalledWith(`/api/v1/threads/${THREAD_ID_FAST}`)
    // store 单一数据源自愈：行从侧栏消失
    expect(app.find(`[data-testid="sb-recent-row-${THREAD_ID_FAST}"]`).exists()).toBe(false)
  })
})

describe('侧栏最近项激活高亮（当前浏览中的会话行：RouterLink 自动 router-link-active 类挂载点）', () => {
  beforeEach(async () => {
    localStorage.clear()
    sessionStorage.clear()
    vi.clearAllMocks()
    installDefaultGetMock()
    i18n.global.locale.value = 'zh-CN'
    await router.push('/')
    await flushPromises()
  })

  it('停留在 /thread/{id}：对应最近行链接携带 router-link-active，其他行不带', async () => {
    const app = await mountApp()
    await router.push(`/thread/${THREAD_ID_FAST}`)
    await flushPromises()
    const linkOf = (id: string) => app.find(`[data-testid="sb-recent-row-${id}"] .sb-recent`)
    expect(linkOf(THREAD_ID_FAST).classes()).toContain('router-link-active')
    const otherId = threadListFixture.items[1]!.thread_id
    expect(linkOf(otherId).classes()).not.toContain('router-link-active')
    app.unmount()
  })

  it('首页（/）：无任何最近行携带激活类（激活=当前所处会话，非当前路由零误亮）', async () => {
    const app = await mountApp()
    expect(app.findAll('.sb-recent.router-link-active')).toHaveLength(0)
    app.unmount()
  })

  it('置顶分区行同样携带激活类（置顶/最近两分区共用 .sb-recent——高亮选择器挂载点通用）', async () => {
    mockedGet.mockReset()
    mockedGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/models') return modelsFixture
      if (url === '/api/v1/threads') {
        return {
          total: threadListFixture.items.length,
          items: [{ ...threadListFixture.items[0]!, pinned: true }, threadListFixture.items[1]!, threadListFixture.items[2]!],
        }
      }
      throw new Error(`unexpected GET ${url}`)
    })
    const app = await mountApp()
    await router.push(`/thread/${THREAD_ID_FAST}`)
    await flushPromises()
    // 置顶项（THREAD_ID_FAST）在置顶分区（sb-pinned-row）——同样命中 router-link-active
    expect(app.find(`[data-testid="sb-pinned-row-${THREAD_ID_FAST}"] .sb-recent`).classes()).toContain('router-link-active')
    const unpinnedId = threadListFixture.items[1]!.thread_id
    expect(app.find(`[data-testid="sb-recent-row-${unpinnedId}"] .sb-recent`).classes()).not.toContain('router-link-active')
    app.unmount()
  })
})

describe('侧栏图标区三改（修复项⑧：删铃铛 / 放大镜=命令面板 / 折叠桌面语义）', () => {
  beforeEach(() => installDefaultGetMock())
  /** Teleport 到 body 的内容经 document 查询（wrapper.find 不穿 Teleport） */
  const paletteEl = (): Element | null => document.querySelector('[data-testid="command-palette"]')

  it('铃铛按钮移除；放大镜为命令面板入口（enabled，无 disabled 占位）', async () => {
    const app = await mountApp()
    expect(app.find('use[href="#i-bell"]').exists()).toBe(false)
    const headBtns = app.findAll('.sb-head-actions .icon-btn')
    expect(headBtns).toHaveLength(2) // 放大镜（命令面板）+ 折叠
    expect(headBtns[0]!.attributes('data-testid')).toBe('open-palette')
    expect(headBtns[0]!.attributes('disabled')).toBeUndefined()
  })

  it('放大镜点击 → 命令面板弹框：autofocus / 命令分组 3 条 / 结果分组=最近会话；Esc 关闭', async () => {
    const app = await mountApp()
    await app.find('[data-testid="open-palette"]').trigger('click')
    await flushPromises()
    expect(paletteEl()).not.toBeNull()
    // 打开即 autofocus 搜索框
    expect(document.activeElement?.getAttribute('data-testid')).toBe('palette-input')
    // 命令分组（新建对话 / 搜索知识库 / 查看历史）
    expect(document.querySelectorAll('[data-testid^="palette-command-"]')).toHaveLength(3)
    // 结果分组 = threadListStore 最近会话（fixture 3 条）
    expect(document.querySelectorAll('[data-testid^="palette-result-"]')).toHaveLength(3)
    // 遮罩点击关闭
    document.querySelector('[data-testid="palette-backdrop"]')!.dispatchEvent(new Event('click'))
    await flushPromises()
    expect(paletteEl()).toBeNull()
  })

  it('全局 Cmd/Ctrl+K 快捷键开合命令面板（input 聚焦时同样允许）', async () => {
    await mountApp()
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', metaKey: true }))
    await flushPromises()
    expect(paletteEl()).not.toBeNull()
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true }))
    await flushPromises()
    expect(paletteEl()).toBeNull()
  })

  it('Cmd/Ctrl+K 组合 shift/alt 时不触发（P3：不拦截浏览器 Cmd+Shift+K 等系统能力）', async () => {
    await mountApp()
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', metaKey: true, shiftKey: true }))
    await flushPromises()
    expect(paletteEl()).toBeNull()
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true, altKey: true }))
    await flushPromises()
    expect(paletteEl()).toBeNull()
    // 纯 Cmd+K / Ctrl+K 仍开合
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', metaKey: true }))
    await flushPromises()
    expect(paletteEl()).not.toBeNull()
  })

  it('命令面板关闭时焦点返还触发按钮（P3：键盘返回路径连续）', async () => {
    const app = await mountApp()
    const trigger = app.find('[data-testid="open-palette"]')
    await trigger.trigger('click')
    await flushPromises()
    // 打开时焦点在搜索框（autofocus）
    expect(document.activeElement?.getAttribute('data-testid')).toBe('palette-input')
    const input = document.querySelector('[data-testid="palette-input"]') as HTMLElement
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await flushPromises()
    expect(paletteEl()).toBeNull()
    // 关闭后焦点返还放大镜按钮（键盘用户回到触发点，Tab 位置连续）
    expect(document.activeElement).toBe(trigger.element)
  })

  it('命令面板导航：命令条目点击 → 路由跳转并关闭；结果条目点击 → 对应线程并关闭', async () => {
    const app = await mountApp()
    await app.find('[data-testid="open-palette"]').trigger('click')
    ;(document.querySelector('[data-testid="palette-command-history"]') as HTMLElement).click()
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/threads')
    expect(paletteEl()).toBeNull()

    await app.find('[data-testid="open-palette"]').trigger('click')
    ;(document.querySelector(`[data-testid="palette-result-${THREAD_ID_FAST}"]`) as HTMLElement).click()
    await flushPromises()
    expect(router.currentRoute.value.path).toBe(`/thread/${THREAD_ID_FAST}`)
    expect(paletteEl()).toBeNull()
  })

  it('命令面板输入过滤：命令按 label 包含、结果按标题包含（大小写不敏感）；无结果显示空态', async () => {
    const app = await mountApp()
    await app.find('[data-testid="open-palette"]').trigger('click')
    const input = document.querySelector('[data-testid="palette-input"]') as HTMLInputElement
    input.value = '新建' // 命中命令「新建对话」，不命中任何线程标题
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    expect(document.querySelectorAll('[data-testid^="palette-command-"]')).toHaveLength(1)
    expect(document.querySelectorAll('[data-testid^="palette-result-"]')).toHaveLength(0)
    input.value = 'langgraph' // 结果按标题包含命中（大小写不敏感），命令零命中
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    expect(document.querySelectorAll('[data-testid^="palette-command-"]')).toHaveLength(0)
    expect(document.querySelectorAll('[data-testid^="palette-result-"]')).toHaveLength(1)
    input.value = 'zzzzz' // 双列表全空 → 空态
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    expect(document.querySelector('[data-testid="palette-empty"]')!.textContent).toContain('无匹配结果')
  })

  it('折叠桌面语义：点击 → collapsed class + localStorage 持久化 + tooltip 动态标签；再点展开；初值恢复', async () => {
    const app = await mountApp()
    const sidebar = () => app.find('[data-testid="sidebar"]')
    expect(sidebar().classes()).not.toContain('collapsed')
    await app.find('[data-testid="sidebar-toggle"]').trigger('click')
    expect(sidebar().classes()).toContain('collapsed')
    expect(localStorage.getItem('ai-search.sidebar-collapsed')).toBe('1')
    // v1.23：原生 title 换 UiTooltip——hover 300ms 后气泡文案随折叠态翻转（打开侧栏）
    // mouseenter 不冒泡：直接对触发钮的包装层（.ui-tip）派发
    vi.useFakeTimers()
    const toggleWrap = app.find('[data-testid="sidebar-toggle"]').element.closest('.ui-tip')!
    toggleWrap.dispatchEvent(new MouseEvent('mouseenter'))
    await vi.advanceTimersByTimeAsync(300)
    let bubble = document.querySelector('[data-testid="ui-tip-bubble"]')
    expect(bubble?.textContent).toBe('打开侧栏')
    toggleWrap.dispatchEvent(new MouseEvent('mouseleave'))
    await flushPromises()
    vi.useRealTimers()
    await app.find('[data-testid="sidebar-toggle"]').trigger('click')
    expect(sidebar().classes()).not.toContain('collapsed')
    expect(localStorage.getItem('ai-search.sidebar-collapsed')).toBe('0')
    // 初始值从 localStorage 恢复（try/catch 读值守卫）
    localStorage.setItem('ai-search.sidebar-collapsed', '1')
    const app2 = await mountApp()
    expect(app2.find('[data-testid="sidebar"]').classes()).toContain('collapsed')
    app.unmount()
    app2.unmount()
  })
})

describe('侧栏最近区头：折叠开关 + 筛选弹层（v1.24）', () => {
  /** fixture 三条：mixed（THREAD_ID_FAST）/ fast / deep */
  const FAST_ID = threadListFixture.items[1]!.thread_id
  const DEEP_ID = threadListFixture.items[2]!.thread_id

  beforeEach(async () => {
    localStorage.clear()
    sessionStorage.clear()
    vi.clearAllMocks()
    installDefaultGetMock()
    i18n.global.locale.value = 'zh-CN'
    await router.push('/')
    await flushPromises()
  })

  it('区头为按钮语义：点击折叠/展开（aria-expanded + 列表 v-show）+ localStorage 持久化 + chevron 翻转类', async () => {
    const app = await mountApp()
    const toggle = app.find('[data-testid="sb-recent-toggle"]')
    expect(toggle.exists()).toBe(true)
    expect(toggle.attributes('aria-expanded')).toBe('true')
    const list = () => app.find('[data-testid="sb-recent-list"]')
    expect(list().isVisible()).toBe(true)
    await toggle.trigger('click')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(list().isVisible()).toBe(false)
    expect(localStorage.getItem('ai-search.recent-collapsed')).toBe('1')
    // chevron 旋转过渡（折叠态翻转类）
    expect(app.find('[data-testid="sb-recent-chev"]').classes()).toContain('flip')
    // 再点展开复位
    await toggle.trigger('click')
    expect(toggle.attributes('aria-expanded')).toBe('true')
    expect(list().isVisible()).toBe(true)
    expect(localStorage.getItem('ai-search.recent-collapsed')).toBe('0')
    expect(app.find('[data-testid="sb-recent-chev"]').classes()).not.toContain('flip')
    app.unmount()
  })

  it('折叠态重挂载恢复（ai-search.recent-collapsed 初值守卫，读写 try/catch 同 sidebarCollapsed 模式）', async () => {
    localStorage.setItem('ai-search.recent-collapsed', '1')
    const app = await mountApp()
    expect(app.find('[data-testid="sb-recent-list"]').isVisible()).toBe(false)
    app.unmount()
  })

  it('筛选钮 hover-reveal 类挂载（sb-section-tools 显形为 CSS 面）+ UiTooltip（筛选会话/快速与深度副题）', async () => {
    const app = await mountApp()
    // hover 显形：CSS 类断言（jsdom 不算样式——断言类挂载与层级结构）
    const tools = app.find('.sb-section-tools')
    expect(tools.exists()).toBe(true)
    const filterBtn = app.find('[data-testid="sb-recent-filter"]')
    expect(filterBtn.exists()).toBe(true)
    expect((filterBtn.element as HTMLElement).closest('.sb-section-tools')).not.toBeNull()
    // tooltip：主标题「筛选会话」+ 副题「快速与深度」
    vi.useFakeTimers()
    const wrap = filterBtn.element.closest('.ui-tip')!
    wrap.dispatchEvent(new MouseEvent('mouseenter'))
    await vi.advanceTimersByTimeAsync(300)
    const bubble = document.querySelector('[data-testid="ui-tip-bubble"]')
    expect(bubble).not.toBeNull()
    expect(bubble!.querySelector('.ui-tip-label')!.textContent).toBe('筛选会话')
    expect(bubble!.querySelector('.ui-tip-sub')!.textContent).toBe('快速与深度')
    wrap.dispatchEvent(new MouseEvent('mouseleave'))
    await flushPromises()
    vi.useRealTimers()
    app.unmount()
  })

  it('筛选弹层：三选项 全部/快速/深度；选快速 → 最近列表仅 fast 行 + active 标注 + store.recentFilter；选全部恢复；Esc 关闭弹层保留筛选', async () => {
    const app = await mountApp()
    expect(app.find('[data-testid="sb-recent-filter-pop"]').exists()).toBe(false)
    await app.find('[data-testid="sb-recent-filter"]').trigger('click')
    const pop = app.find('[data-testid="sb-recent-filter-pop"]')
    expect(pop.exists()).toBe(true)
    expect(pop.findAll('[data-testid^="sb-filter-"]').map((x) => x.text())).toEqual(['全部', '快速', '深度'])
    // 选「快速」→ 仅 fast 行渲染（mixed/deep 隐藏）
    await pop.find('[data-testid="sb-filter-fast"]').trigger('click')
    await flushPromises()
    expect(app.find('[data-testid="sb-recent-filter-pop"]').exists()).toBe(false) // 选后收起
    expect(app.find(`[data-testid="sb-recent-row-${FAST_ID}"]`).exists()).toBe(true)
    expect(app.find(`[data-testid="sb-recent-row-${DEEP_ID}"]`).exists()).toBe(false)
    expect(app.find(`[data-testid="sb-recent-row-${THREAD_ID_FAST}"]`).exists()).toBe(false)
    // store 态（共享面）：recentFilter=fast；再开弹层 active 标注在 fast
    const { useThreadListStore } = await import('@/stores/threadList')
    const store = useThreadListStore()
    expect(store.recentFilter).toBe('fast')
    await app.find('[data-testid="sb-recent-filter"]').trigger('click')
    expect(app.find('[data-testid="sb-filter-fast"]').classes()).toContain('active')
    expect(app.find('[data-testid="sb-filter-all"]').classes()).not.toContain('active')
    // Esc 关闭弹层且筛选保留
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await flushPromises()
    expect(app.find('[data-testid="sb-recent-filter-pop"]').exists()).toBe(false)
    expect(store.recentFilter).toBe('fast')
    // 选「全部」→ 三行恢复
    await app.find('[data-testid="sb-recent-filter"]').trigger('click')
    await app.find('[data-testid="sb-filter-all"]').trigger('click')
    await flushPromises()
    expect(app.find(`[data-testid="sb-recent-row-${DEEP_ID}"]`).exists()).toBe(true)
    expect(store.recentFilter).toBeNull()
    app.unmount()
  })

  it('外点关闭（ThreadActionMenu 同族语义）：区头内 mousedown 不关；区头外 mousedown 关闭且筛选态保留', async () => {
    const app = await mountApp()
    await app.find('[data-testid="sb-recent-filter"]').trigger('click')
    expect(app.find('[data-testid="sb-recent-filter-pop"]').exists()).toBe(true)
    // 区头内 mousedown（冒泡；如弹层内文字选择）不关闭
    app.find('[data-testid="sb-recent-filter-pop"]').element.dispatchEvent(
      new MouseEvent('mousedown', { bubbles: true }),
    )
    await flushPromises()
    expect(app.find('[data-testid="sb-recent-filter-pop"]').exists()).toBe(true)
    // 区头外 mousedown → 关闭
    document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
    await flushPromises()
    expect(app.find('[data-testid="sb-recent-filter-pop"]').exists()).toBe(false)
    app.unmount()
  })

  it('存储不可用守卫：localStorage 读/写抛错 → 默认展开且折叠开关会话内仍生效（try/catch 同 sidebarCollapsed 模式）', async () => {
    const getItemSpy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('storage denied')
    })
    const setItemSpy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('storage denied')
    })
    try {
      const app = await mountApp()
      // 读失败 → 默认展开（readInitialRecentCollapsed catch 兜底）
      expect(app.find('[data-testid="sb-recent-list"]').isVisible()).toBe(true)
      // 写失败 → 会话内状态仍生效（watch catch 静默）
      await app.find('[data-testid="sb-recent-toggle"]').trigger('click')
      expect(app.find('[data-testid="sb-recent-list"]').isVisible()).toBe(false)
      expect(app.find('[data-testid="sb-recent-toggle"]').attributes('aria-expanded')).toBe('false')
      app.unmount()
    } finally {
      getItemSpy.mockRestore()
      setItemSpy.mockRestore()
    }
  })
})

describe('侧栏已置顶分区（v1.25：对标 Perplexity——置顶项独立成区+图钉+折叠持久化）', () => {
  /** 注入 pinned 投影（fixture 全 unpinned；置顶第一条 + 第二条不动） */
  function installPinnedGetMock(): void {
    mockedGet.mockReset()
    mockedGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/models') return modelsFixture
      if (url === '/api/v1/threads') {
        return {
          total: threadListFixture.items.length,
          items: [
            { ...threadListFixture.items[0]!, pinned: true },
            threadListFixture.items[1]!,
            threadListFixture.items[2]!,
          ],
        }
      }
      throw new Error(`unexpected GET ${url}`)
    })
  }

  beforeEach(async () => {
    localStorage.clear()
    sessionStorage.clear()
    vi.clearAllMocks()
    installDefaultGetMock()
    i18n.global.locale.value = 'zh-CN'
    await router.push('/')
    await flushPromises()
  })

  it('无置顶项：整个分区不渲染（对标 Perplexity——没置顶就没有该区）；最近区照常', async () => {
    const app = await mountApp()
    expect(app.find('[data-testid="sb-pinned-section"]').exists()).toBe(false)
    expect(app.find('[data-testid="sb-pinned-toggle"]').exists()).toBe(false)
    expect(app.find('[data-testid="sb-pinned-list"]').exists()).toBe(false)
    expect(app.find('[data-testid="sb-recent-list"]').exists()).toBe(true)
    app.unmount()
  })

  it('有置顶项：分区在最近区上方渲染（区头「已置顶」+ chevron）+ 置顶行带 i-pin 图钉；最近区只含未置顶行', async () => {
    installPinnedGetMock()
    const app = await mountApp()
    const section = app.find('[data-testid="sb-pinned-section"]')
    expect(section.exists()).toBe(true)
    // 分区位置语义：在最近区头之前（DOM 序）
    const nav = app.find('.sb-nav')
    const children = nav.element.children
    const pinnedIdx = Array.from(children).findIndex((el) => el.getAttribute('data-testid') === 'sb-pinned-section')
    const recentIdx = Array.from(children).findIndex((el) => el.classList.contains('sb-section') && el.getAttribute('data-testid') !== 'sb-pinned-section')
    expect(pinnedIdx).toBeGreaterThanOrEqual(0)
    expect(pinnedIdx).toBeLessThan(recentIdx)
    // 区头文案 + aria
    const toggle = app.find('[data-testid="sb-pinned-toggle"]')
    expect(toggle.text()).toContain('已置顶')
    expect(toggle.attributes('aria-expanded')).toBe('true')
    expect(toggle.attributes('aria-controls')).toBe('sb-pinned-list')
    // 置顶行：图钉 i-pin + 可访问名称（title/aria-label=threads.pinnedLabel，评审 P3 对齐
    // ThreadsView 同语义图钉）；最近区只含未置顶行
    const pinnedRow = app.find(`[data-testid="sb-pinned-row-${THREAD_ID_FAST}"]`)
    expect(pinnedRow.exists()).toBe(true)
    const pin = pinnedRow.find('.sb-pin-ind')
    expect(pin.find('use[href="#i-pin"]').exists()).toBe(true)
    expect(pin.attributes('title')).toBe('已置顶')
    expect(pin.attributes('aria-label')).toBe('已置顶')
    const unpinnedId = threadListFixture.items[1]!.thread_id
    expect(app.find(`[data-testid="sb-pinned-row-${unpinnedId}"]`).exists()).toBe(false)
    expect(app.find(`[data-testid="sb-recent-list"] [data-testid="sb-recent-row-${THREAD_ID_FAST}"]`).exists()).toBe(false)
    expect(app.find(`[data-testid="sb-recent-row-${unpinnedId}"]`).exists()).toBe(true)
    app.unmount()
  })

  it('折叠持久化：点击区头 → 列表隐藏 + localStorage ai-search.pinned-collapsed + chevron 翻转；与「最近」折叠互不影响；重挂载恢复', async () => {
    installPinnedGetMock()
    const app = await mountApp()
    await app.find('[data-testid="sb-pinned-toggle"]').trigger('click')
    expect(app.find('[data-testid="sb-pinned-toggle"]').attributes('aria-expanded')).toBe('false')
    expect(app.find('[data-testid="sb-pinned-list"]').isVisible()).toBe(false)
    expect(localStorage.getItem('ai-search.pinned-collapsed')).toBe('1')
    expect(app.find('[data-testid="sb-pinned-chev"]').classes()).toContain('flip')
    // 「最近」区开关不受影响（两把开关互不影响）
    expect(app.find('[data-testid="sb-recent-toggle"]').attributes('aria-expanded')).toBe('true')
    await app.find('[data-testid="sb-recent-toggle"]').trigger('click')
    expect(app.find('[data-testid="sb-recent-list"]').isVisible()).toBe(false)
    expect(app.find('[data-testid="sb-pinned-list"]').isVisible()).toBe(false)
    await app.find('[data-testid="sb-recent-toggle"]').trigger('click')
    expect(app.find('[data-testid="sb-recent-list"]').isVisible()).toBe(true)
    expect(app.find('[data-testid="sb-pinned-list"]').isVisible()).toBe(false) // 置顶区仍折叠
    // 再点展开复位
    await app.find('[data-testid="sb-pinned-toggle"]').trigger('click')
    expect(app.find('[data-testid="sb-pinned-list"]').isVisible()).toBe(true)
    expect(localStorage.getItem('ai-search.pinned-collapsed')).toBe('0')
    // 重挂载恢复折叠态
    const saved = localStorage.getItem('ai-search.pinned-collapsed')
    localStorage.setItem('ai-search.pinned-collapsed', '1')
    const app2 = await mountApp()
    expect(app2.find('[data-testid="sb-pinned-list"]').isVisible()).toBe(false)
    localStorage.setItem('ai-search.pinned-collapsed', saved ?? '0')
    app.unmount()
    app2.unmount()
  })

  it('取消置顶自愈：置顶行三点菜单「取消置顶」→ PATCH {pinned:false} → 分区消失、行落入最近区', async () => {
    installPinnedGetMock()
    const app = await mountApp()
    const pinnedRow = app.find(`[data-testid="sb-pinned-row-${THREAD_ID_FAST}"]`)
    await pinnedRow.find('[data-testid="tam-trigger"]').trigger('click')
    const items = Array.from(document.querySelectorAll('[role="menuitem"]')).map((el) => new DOMWrapper(el as HTMLElement))
    expect(items[0]!.text()).toBe('取消置顶')
    mockedPatch.mockResolvedValueOnce({ ...threadListFixture.items[0]!, pinned: false })
    await items[0]!.trigger('click')
    await flushPromises()
    expect(mockedPatch).toHaveBeenCalledWith(`/api/v1/threads/${THREAD_ID_FAST}`, { pinned: false })
    // 分区随 recent 自愈消失（无置顶项不渲染）；行落入最近区
    expect(app.find('[data-testid="sb-pinned-section"]').exists()).toBe(false)
    expect(app.find(`[data-testid="sb-recent-row-${THREAD_ID_FAST}"]`).exists()).toBe(true)
    app.unmount()
  })

  it('置顶行预览卡照常（openRecentPreview 的 rowEl 查询在两分区通用）', async () => {
    installPinnedGetMock()
    const app = await mountApp()
    vi.useFakeTimers()
    try {
      const row = app.find(`[data-testid="sb-pinned-row-${THREAD_ID_FAST}"]`)
      row.element.dispatchEvent(new MouseEvent('mouseenter'))
      await vi.advanceTimersByTimeAsync(300)
      await flushPromises()
      expect(document.querySelector('[data-testid="sb-preview-card"]')).not.toBeNull()
      row.element.dispatchEvent(new MouseEvent('mouseleave'))
      // 排空离开宽限（150ms）定时器再退 fake timers——防 unmount 后残余异步 patch 撞窗
      // （coverage 插桩时序下曾偶发 unhandled insertBefore，与本用例预览卡异步链同窗）
      await vi.advanceTimersByTimeAsync(200)
      await flushPromises()
    } finally {
      vi.useRealTimers()
    }
    app.unmount()
  })

  it('置顶折叠存储不可用守卫：localStorage 读/写抛错 → 默认展开且开关会话内仍生效（try/catch 同「最近」区模式）', async () => {
    installPinnedGetMock()
    const getItemSpy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('storage denied')
    })
    const setItemSpy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('storage denied')
    })
    try {
      const app = await mountApp()
      // 读失败 → 默认展开（readInitialPinnedCollapsed catch 兜底）
      expect(app.find('[data-testid="sb-pinned-list"]').isVisible()).toBe(true)
      // 写失败 → 会话内状态仍生效（watch catch 静默）
      await app.find('[data-testid="sb-pinned-toggle"]').trigger('click')
      expect(app.find('[data-testid="sb-pinned-list"]').isVisible()).toBe(false)
      expect(app.find('[data-testid="sb-pinned-toggle"]').attributes('aria-expanded')).toBe('false')
      // 「最近」区开关不受影响
      expect(app.find('[data-testid="sb-recent-list"]').isVisible()).toBe(true)
      app.unmount()
    } finally {
      getItemSpy.mockRestore()
      setItemSpy.mockRestore()
    }
  })
})

describe('App 顶栏导出/分享（answer 页：整线程 .md 导出 + 复制会话链接）', () => {
  beforeEach(() => installDefaultGetMock())
  const clipboardWrite = vi.fn<(text: string) => Promise<void>>()

  async function mountOnAnswer(): Promise<VueWrapper> {
    const app = await mountApp()
    await router.push(`/thread/${VALID_THREAD_ID}`)
    await flushPromises()
    return app
  }

  it('顶栏导出/分享按钮启用（去除 disabled 占位）', async () => {
    const app = await mountOnAnswer()
    expect(app.find('[data-testid="topbar-export"]').attributes('disabled')).toBeUndefined()
    expect(app.find('[data-testid="topbar-share"]').attributes('disabled')).toBeUndefined()
  })

  it('导出 Markdown：整线程 .md 触发下载（文件名=线程标题消毒；Blob 含标题 + 问答结构 + 来源区）', async () => {
    const createObjectURL = vi.fn<(blob: Blob) => string>(() => 'blob:mock-url')
    const revokeObjectURL = vi.fn<(url: string) => void>()
    Object.defineProperty(URL, 'createObjectURL', { value: createObjectURL, configurable: true, writable: true })
    Object.defineProperty(URL, 'revokeObjectURL', { value: revokeObjectURL, configurable: true, writable: true })
    const clickedAnchor: { current: Partial<HTMLAnchorElement> | null } = { current: null }
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      clickedAnchor.current = this
    })
    try {
      const app = await mountOnAnswer()
      await app.find('[data-testid="topbar-export"]').trigger('click')
      await flushPromises()
      expect(createObjectURL).toHaveBeenCalledTimes(1)
      const blob = createObjectURL.mock.calls[0]?.[0]
      expect(blob).toBeInstanceOf(Blob)
      const text = await blob!.text()
      expect(text).toContain('# LangGraph 的 Send API 和 Router 的区别？')
      expect(text).toContain('## 问：LangGraph 的 Send API 和 Router 的区别？')
      expect(text).toContain('Send API 用于**动态扇出**')
      expect(text).toContain('[1] https://docs.langchain.com/langgraph/send-vs-router LangGraph docs — Send API vs Router')
      expect(clickedAnchor.current?.download).toBe('LangGraph 的 Send API 和 Router 的区别？.md')
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock-url')
    } finally {
      vi.restoreAllMocks()
    }
  })

  /** 导出防静默用例共用：下载桩 + 断言「屏上存在 exportNotReady toast 且零下载」。 */
  async function expectExportBlocked(mutate: (turnStore: ReturnType<typeof useTurnStore>) => void): Promise<void> {
    const createObjectURL = vi.fn<(blob: Blob) => string>(() => 'blob:mock-url')
    Object.defineProperty(URL, 'createObjectURL', { value: createObjectURL, configurable: true, writable: true })
    try {
      const app = await mountOnAnswer()
      const ts = useTurnStore(getActivePinia())
      mutate(ts)
      await app.find('[data-testid="topbar-export"]').trigger('click')
      await flushPromises()
      expect(createObjectURL).not.toHaveBeenCalled() // 不产出空文档下载
      const notReady = i18n.global.t('answer.exportNotReady')
      expect(useToasts().value.some((x) => x.message === notReady)).toBe(true) // toast 明示而非无声无息
    } finally {
      vi.restoreAllMocks()
    }
  }

  it('导出防静默（批次3评审-F3）：threadMetas 无当前线程（装配快照未就绪）→ toast(exportNotReady) 且不触发下载', async () => {
    await expectExportBlocked((ts) => {
      delete ts.threadMetas[VALID_THREAD_ID]
    })
  })

  it('导出防静默（批次3评审-F3）：store.turns 空（轮次投影未就绪）→ toast(exportNotReady) 且不触发下载', async () => {
    await expectExportBlocked((ts) => {
      ts.turns = {}
    })
  })

  it('分享：会话链接（origin + /thread/{threadId}）写入剪贴板 + toast(answer.linkCopied)', async () => {
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: clipboardWrite.mockResolvedValue(undefined) },
      configurable: true,
    })
    const app = await mountOnAnswer()
    const before = useToasts().value.length
    await app.find('[data-testid="topbar-share"]').trigger('click')
    await flushPromises()
    expect(clipboardWrite).toHaveBeenCalledWith(`${window.location.origin}/thread/${VALID_THREAD_ID}`)
    const items = useToasts().value
    expect(items.length).toBe(before + 1)
    expect(items[items.length - 1]!.message).toBe(i18n.global.t('answer.linkCopied'))
  })

  it('分享失败：console.warn 留痕 + toast(answer.copyFailed)', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: clipboardWrite.mockRejectedValue(new Error('denied')) },
      configurable: true,
    })
    try {
      const app = await mountOnAnswer()
      const before = useToasts().value.length
      await app.find('[data-testid="topbar-share"]').trigger('click')
      await flushPromises()
      expect(warnSpy).toHaveBeenCalled()
      const items = useToasts().value
      expect(items.length).toBe(before + 1)
      expect(items[items.length - 1]!.message).toBe(i18n.global.t('answer.copyFailed'))
    } finally {
      warnSpy.mockRestore()
    }
  })
})
