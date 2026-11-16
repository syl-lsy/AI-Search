/**
 * test/component/sidebar-preview.test.ts — 侧栏最近项 hover 预览卡 + 截断标题 marquee
 * 组件层（frontend-api.md §1 侧栏行 v1.23；用户裁决双形态：预览卡 + 文字滚动）。
 *
 * 断言面：hover ≥300ms 浮出预览卡（完整标题不截断 + 相对时间 + 轮数 + 模式徽标）；
 * Esc/离开即隐（离开有 150ms 宽限供卡内 hover）；行 mousedown（三点菜单开启路径）预览
 * 立即隐藏（互斥）；预览卡内点击不导航；marquee 仅溢出触发（reduced-motion 不滚）。
 */
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { apiGet } from '@/api/client'
import { i18n } from '@/i18n'
import { modelsFixture } from '@/mocks/models'
import { threadListFixture } from '@/mocks/threads'
import { THREAD_ID_FAST } from '@/mocks/turnDetails'
import { router } from '@/router'

import App from '@/App.vue'

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

function installDefaultGetMock(): void {
  mockedGet.mockReset()
  mockedGet.mockImplementation(async (url: string) => {
    if (url === '/api/v1/models') return modelsFixture
    if (url === '/api/v1/threads') return threadListFixture
    throw new Error(`unexpected GET ${url}`)
  })
}

const previewCard = (): HTMLElement | null => document.querySelector('[data-testid="sb-preview-card"]')

/** 对最近行的包装层派发 mouseenter（mouseenter 不冒泡；hover 处理器绑在 .sb-recent-row 上） */
async function hoverRow(wrapper: ReturnType<typeof mount<typeof App>>, threadId: string): Promise<Element> {
  const row = wrapper.find(`[data-testid="sb-recent-row-${threadId}"]`)
  row.element.dispatchEvent(new MouseEvent('mouseenter'))
  await vi.advanceTimersByTimeAsync(300)
  await flushPromises()
  return row.element
}

beforeEach(async () => {
  setActivePinia(createPinia())
  localStorage.clear()
  vi.clearAllMocks()
  installDefaultGetMock()
  i18n.global.locale.value = 'zh-CN'
  vi.stubGlobal('EventSource', class {} as unknown as typeof EventSource)
  await router.push('/')
  await router.isReady()
})

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

describe('侧栏最近项 hover 预览卡（v1.23：≥300ms 延迟浮出）', () => {
  it('hover 满 300ms → 预览卡出现：完整标题 + 相对时间 + 轮数 + 模式徽标（数据零网络）', async () => {
    vi.useFakeTimers()
    const app = mount(App, { global: { plugins: [createPinia(), router, i18n] }, attachTo: document.body })
    await flushPromises()
    expect(previewCard()).toBeNull()
    const item = threadListFixture.items[0]!
    await hoverRow(app, item.thread_id)
    const card = previewCard()
    expect(card).not.toBeNull()
    // 完整标题不截断（textContent 全量）
    expect(card!.textContent).toContain(item.title)
    // 轮数徽标 + 相对时间（updated_at 驱动；格式不钉死，断言非空元信息区）
    expect(card!.textContent).toContain('轮')
    expect(card!.querySelector('.sb-preview-meta')!.textContent!.trim().length).toBeGreaterThan(0)
    app.unmount()
    vi.useRealTimers()
  })

  it('300ms 内离开不出现（防误触）；离开（宽限后）即隐', async () => {
    vi.useFakeTimers()
    const app = mount(App, { global: { plugins: [createPinia(), router, i18n] }, attachTo: document.body })
    await flushPromises()
    const row = app.find(`[data-testid="sb-recent-row-${THREAD_ID_FAST}"]`)
    row.element.dispatchEvent(new MouseEvent('mouseenter'))
    await vi.advanceTimersByTimeAsync(100)
    row.element.dispatchEvent(new MouseEvent('mouseleave'))
    await vi.advanceTimersByTimeAsync(400)
    await flushPromises()
    expect(previewCard()).toBeNull()
    // 再 hover 满延迟 → 出现；离开 150ms 宽限内仍在、宽限后消失
    await hoverRow(app, THREAD_ID_FAST)
    expect(previewCard()).not.toBeNull()
    const rowEl = app.find(`[data-testid="sb-recent-row-${THREAD_ID_FAST}"]`).element
    rowEl.dispatchEvent(new MouseEvent('mouseleave'))
    await vi.advanceTimersByTimeAsync(100)
    expect(previewCard()).not.toBeNull() // 宽限期
    await vi.advanceTimersByTimeAsync(100)
    await flushPromises()
    expect(previewCard()).toBeNull()
    app.unmount()
    vi.useRealTimers()
  })

  it('Esc 立即隐藏预览卡', async () => {
    vi.useFakeTimers()
    const app = mount(App, { global: { plugins: [createPinia(), router, i18n] }, attachTo: document.body })
    await flushPromises()
    await hoverRow(app, THREAD_ID_FAST)
    expect(previewCard()).not.toBeNull()
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await flushPromises()
    expect(previewCard()).toBeNull()
    app.unmount()
    vi.useRealTimers()
  })

  it('行 mousedown（三点菜单开启路径）预览立即隐藏——预览卡与菜单互斥', async () => {
    vi.useFakeTimers()
    const app = mount(App, { global: { plugins: [createPinia(), router, i18n] }, attachTo: document.body })
    await flushPromises()
    const rowEl = await hoverRow(app, THREAD_ID_FAST)
    expect(previewCard()).not.toBeNull()
    rowEl.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
    await flushPromises()
    expect(previewCard()).toBeNull()
    app.unmount()
    vi.useRealTimers()
  })

  it('预览卡内点击不导航（纯预览；路由不变）', async () => {
    vi.useFakeTimers()
    const app = mount(App, { global: { plugins: [createPinia(), router, i18n] }, attachTo: document.body })
    await flushPromises()
    await hoverRow(app, THREAD_ID_FAST)
    const card = previewCard()!
    card.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
    card.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/')
    app.unmount()
    vi.useRealTimers()
  })

  it('离开宽限期内回行同一行：取消隐藏排程、预览卡保持（clearPreviewHideTimer 路径）', async () => {
    vi.useFakeTimers()
    const app = mount(App, { global: { plugins: [createPinia(), router, i18n] }, attachTo: document.body })
    await flushPromises()
    const rowEl = await hoverRow(app, THREAD_ID_FAST)
    expect(previewCard()).not.toBeNull()
    rowEl.dispatchEvent(new MouseEvent('mouseleave'))
    // 宽限期内（<150ms）回行同一行 → 取消隐藏排程 + onRecentEnter 同行早退
    rowEl.dispatchEvent(new MouseEvent('mouseenter'))
    await vi.advanceTimersByTimeAsync(100)
    expect(previewCard()).not.toBeNull()
    await vi.advanceTimersByTimeAsync(400)
    await flushPromises()
    expect(previewCard()).not.toBeNull() // 排程已取消，卡保持
    app.unmount()
    vi.useRealTimers()
  })

  it('超下视口上翻：行位于视口下部时预览卡钳进视口（maxTop 上翻分支）', async () => {
    vi.useFakeTimers()
    const app = mount(App, { global: { plugins: [createPinia(), router, i18n] }, attachTo: document.body })
    await flushPromises()
    const rowEl = app.find(`[data-testid="sb-recent-row-${THREAD_ID_FAST}"]`).element
    // jsdom 无布局——mock 行矩形（视口下部）与视口高度（卡片实测高度 0）
    rowEl.getBoundingClientRect = () =>
      ({ top: 500, right: 300, bottom: 530, left: 0, width: 300, height: 30 }) as DOMRect
    const prevInnerHeight = window.innerHeight
    Object.defineProperty(window, 'innerHeight', { value: 200, configurable: true })
    try {
      rowEl.dispatchEvent(new MouseEvent('mouseenter'))
      await vi.advanceTimersByTimeAsync(300)
      await flushPromises()
      const card = previewCard()
      expect(card).not.toBeNull()
      // maxTop = 200(innerHeight) - 0(offsetHeight) - 8(gap) = 192；top=500 > 192 → 上翻钳到 192
      expect(card!.style.top).toBe('192px')
    } finally {
      Object.defineProperty(window, 'innerHeight', { value: prevInnerHeight, configurable: true })
    }
    app.unmount()
    vi.useRealTimers()
  })
})

describe('侧栏最近项标题 marquee（v1.23：溢出才滚，hover 起 / 离停；v1.24 经典无缝左滚走马灯）', () => {
  it('文本溢出：hover 后标题获得 marquee class + 双拷贝（第二份 aria-hidden，仅激活行渲染）+ 无缝距离/匀速时长变量；离开移除并恢复单份', async () => {
    vi.useFakeTimers()
    const app = mount(App, { global: { plugins: [createPinia(), router, i18n] }, attachTo: document.body })
    await flushPromises()
    const textEl = app
      .find(`[data-testid="sb-recent-row-${THREAD_ID_FAST}"] .sb-recent-text`)
      .element as HTMLElement
    // jsdom 无布局——注入溢出量（scrollWidth > clientWidth；单份态 scrollWidth=拷贝宽 500）
    Object.defineProperty(textEl, 'scrollWidth', { value: 500, configurable: true })
    Object.defineProperty(textEl, 'clientWidth', { value: 150, configurable: true })
    await hoverRow(app, THREAD_ID_FAST)
    expect(textEl.classList.contains('marquee')).toBe(true)
    // 双拷贝结构：仅激活行渲染第二份（aria-hidden 纯视觉；非激活行保持单份截断）
    const copies = textEl.querySelectorAll('.sb-recent-copy')
    expect(copies).toHaveLength(2)
    expect(copies[0]!.getAttribute('aria-hidden')).toBeNull()
    expect(copies[1]!.getAttribute('aria-hidden')).toBe('true')
    expect(copies[1]!.textContent).toBe(copies[0]!.textContent)
    // 无缝距离 = min(单份宽 500 + 接缝 gap 42, 400) = 400（钳制生效）；匀速时长 = distance/30px/s
    // （linear infinite 单方向为 CSS 静态面——jsdom 不算样式，由 App.vue keyframes 实现钉正）
    expect(textEl.style.getPropertyValue('--mq-distance')).toBe('400px')
    expect(textEl.style.getPropertyValue('--mq-duration')).toBe(`${400 / 30}s`)
    // 离开（宽限后隐藏预览）→ marquee 移除（animation 停止+复位=class 移除机制）+ 单份恢复
    const rowEl = app.find(`[data-testid="sb-recent-row-${THREAD_ID_FAST}"]`).element
    rowEl.dispatchEvent(new MouseEvent('mouseleave'))
    await vi.advanceTimersByTimeAsync(200)
    await flushPromises()
    expect(textEl.classList.contains('marquee')).toBe(false)
    expect(textEl.querySelectorAll('.sb-recent-copy')).toHaveLength(1)
    app.unmount()
    vi.useRealTimers()
  })

  it('常规溢出（未触钳制）：distance = 单份拷贝宽 + 接缝 gap（300+42=342）——to 点=第二份拷贝左缘对齐原起点（无缝）', async () => {
    vi.useFakeTimers()
    const app = mount(App, { global: { plugins: [createPinia(), router, i18n] }, attachTo: document.body })
    await flushPromises()
    const textEl = app
      .find(`[data-testid="sb-recent-row-${THREAD_ID_FAST}"] .sb-recent-text`)
      .element as HTMLElement
    Object.defineProperty(textEl, 'scrollWidth', { value: 300, configurable: true })
    Object.defineProperty(textEl, 'clientWidth', { value: 150, configurable: true })
    await hoverRow(app, THREAD_ID_FAST)
    expect(textEl.classList.contains('marquee')).toBe(true)
    expect(textEl.style.getPropertyValue('--mq-distance')).toBe('342px')
    expect(textEl.style.getPropertyValue('--mq-duration')).toBe(`${342 / 30}s`)
    app.unmount()
    vi.useRealTimers()
  })

  it('超长标题：distance 钳制 ≤400px（测量异常/超长拷贝的视觉速度上限；连续循环下=更快匀速，无节奏畸变）', async () => {
    vi.useFakeTimers()
    const app = mount(App, { global: { plugins: [createPinia(), router, i18n] }, attachTo: document.body })
    await flushPromises()
    const textEl = app
      .find(`[data-testid="sb-recent-row-${THREAD_ID_FAST}"] .sb-recent-text`)
      .element as HTMLElement
    Object.defineProperty(textEl, 'scrollWidth', { value: 900, configurable: true })
    Object.defineProperty(textEl, 'clientWidth', { value: 150, configurable: true })
    await hoverRow(app, THREAD_ID_FAST)
    expect(textEl.classList.contains('marquee')).toBe(true)
    expect(textEl.style.getPropertyValue('--mq-distance')).toBe('400px') // min(900+42, 400)
    expect(textEl.style.getPropertyValue('--mq-duration')).toBe(`${400 / 30}s`)
    app.unmount()
    vi.useRealTimers()
  })

  it('不溢出：hover 后不触发 marquee（截断态维持）', async () => {
    vi.useFakeTimers()
    const app = mount(App, { global: { plugins: [createPinia(), router, i18n] }, attachTo: document.body })
    await flushPromises()
    const textEl = app
      .find(`[data-testid="sb-recent-row-${THREAD_ID_FAST}"] .sb-recent-text`)
      .element as HTMLElement
    Object.defineProperty(textEl, 'scrollWidth', { value: 120, configurable: true })
    Object.defineProperty(textEl, 'clientWidth', { value: 150, configurable: true })
    await hoverRow(app, THREAD_ID_FAST)
    expect(textEl.classList.contains('marquee')).toBe(false)
    expect(previewCard()).not.toBeNull() // 预览卡不受影响
    app.unmount()
    vi.useRealTimers()
  })

  it('prefers-reduced-motion：溢出也不滚动（直接显示完整态，预览卡照常）', async () => {
    // mediaQuery composable 在 setup 读一次 matchMedia——挂载前桩入
    vi.stubGlobal('matchMedia', vi.fn().mockImplementation((query: string) => ({
      matches: query === '(prefers-reduced-motion: reduce)',
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })))
    vi.useFakeTimers()
    const app = mount(App, { global: { plugins: [createPinia(), router, i18n] }, attachTo: document.body })
    await flushPromises()
    const textEl = app
      .find(`[data-testid="sb-recent-row-${THREAD_ID_FAST}"] .sb-recent-text`)
      .element as HTMLElement
    Object.defineProperty(textEl, 'scrollWidth', { value: 500, configurable: true })
    Object.defineProperty(textEl, 'clientWidth', { value: 150, configurable: true })
    await hoverRow(app, THREAD_ID_FAST)
    expect(textEl.classList.contains('marquee')).toBe(false)
    expect(previewCard()).not.toBeNull()
    app.unmount()
    vi.useRealTimers()
  })
})
