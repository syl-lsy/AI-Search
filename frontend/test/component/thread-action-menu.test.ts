/**
 * test/component/thread-action-menu.test.ts — ThreadActionMenu 组件层
 * （frontend-api.md §5 v1.21 / REQ-059/A43：三点菜单，侧栏最近项与会话页行级共用）。
 *
 * 断言面（契约 §5 ThreadActionMenu 行）：
 *   - 两 variant 菜单项集：active=置顶·取消置顶/标记为未读/重命名/归档/删除；
 *     archived=取消归档/重命名/删除（置顶与标记为未读隐藏，不是禁用）；
 *   - 动作分发至 threadListStore 动作面（组件零直接触网——经 @/api/threads mock 断言）；
 *   - 删除二次确认（轻确认弹层 threads.deleteConfirm；取消不发请求）；
 *   - 重命名弹层：输入 1~120 字符（前端先校验——空串禁提交、maxlength=120）；
 *   - a11y：menu/menuitem 语义、Esc 关闭、焦点返还触发钮。
 */
import { createPinia, setActivePinia } from 'pinia'
import { DOMWrapper, enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'
import type { Router } from 'vue-router'

import { deleteThread, listThreads, patchThread } from '@/api/threads'
import { toast } from '@/composables/toast'
import { i18n } from '@/i18n'
import ThreadActionMenu from '@/components/threads/ThreadActionMenu.vue'
import { useThreadListStore } from '@/stores/threadList'
import type { ThreadListItem } from '@/types/api'

vi.mock('@/api/threads', () => ({
  listThreads: vi.fn(),
  patchThread: vi.fn(),
  deleteThread: vi.fn(),
}))

vi.mock('@/composables/toast', () => ({ toast: vi.fn() }))

/** 弹层 Teleport 到 body（v1.25 裁剪修复：菜单脱离侧栏/列表裁剪容器，fixed 定位盖内容）
 *  ——wrapper.find 不穿 Teleport，菜单/菜单项断言与操作统一走 document 级查询 */
function menuEl(): HTMLElement {
  return document.querySelector('[role="menu"]') as HTMLElement
}
function menuItemsW() {
  return Array.from(document.querySelectorAll('[role="menuitem"]')).map(
    (el) => new DOMWrapper(el as HTMLElement),
  )
}

const mockedPatch = vi.mocked(patchThread)
const mockedDelete = vi.mocked(deleteThread)
const mockedList = vi.mocked(listThreads)

function makeThread(extra: Partial<ThreadListItem> = {}): ThreadListItem {
  return {
    thread_id: '11111111-1111-4111-8111-111111111111',
    title: 'LangGraph 调研',
    mode_summary: 'fast',
    turn_count: 1,
    pinned: false,
    is_unread: false,
    archived: false,
    created_at: '2026-11-01T08:00:00Z',
    updated_at: '2026-11-01T09:00:00Z',
    ...extra,
  }
}

/** 测试路由（memory history 实例隔离，不与共享单例 @/router 串历史）：
 *  删除跳转用例要求组件 setup 里 useRoute/useRouter 可注入——最小三路由骨架，
 *  route component 用 render 函数（运行时构建无模板编译器，禁 template 字符串）。 */
function makeTestRouter(initialPath = '/'): Router {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { render: () => null } },
      { path: '/threads', component: { render: () => null } },
      { path: '/thread/:threadId', component: { render: () => null } },
    ],
  })
  void router.push(initialPath)
  return router
}

async function mountMenu(thread: ThreadListItem, variant: 'active' | 'archived' = 'active', routePath = '/') {
  const pinia = createPinia()
  setActivePinia(pinia)
  mockedList.mockResolvedValue({ total: 1, items: [thread] })
  const store = useThreadListStore()
  await store.fetchThreads({ mode: null })
  const router = makeTestRouter(routePath)
  await router.isReady()
  // attachTo document.body：焦点返还与外点关闭须走真实 DOM 语义（theme-switcher.test.ts 同款）
  const w = mount(ThreadActionMenu, {
    props: { thread, variant },
    global: { plugins: [pinia, i18n, router] },
    attachTo: document.body,
  })
  await flushPromises()
  return { w, store, router }
}

beforeEach(() => {
  vi.clearAllMocks()
})

// attachTo body 的 wrapper 逐用例自动卸载后再清 body——防 pending 异步 patch 撞已被清空的
// 容器（coverage 插桩时序下偶发 unhandled insertBefore；@vue/test-utils 官方自动卸载钩子）
enableAutoUnmount(afterEach)

afterEach(() => {
  document.body.innerHTML = ''
})

describe('ThreadActionMenu 菜单项集（两 variant）', () => {
  it('active：置顶/标记为未读/重命名/归档/删除五项（role=menu/menuitem）', async () => {
    const { w } = await mountMenu(makeThread())
    await w.find('[data-testid="tam-trigger"]').trigger('click')
    expect(menuEl()).not.toBeNull()
    // 语义断言（Teleport 修复面）：菜单挂在 body 直下（脱离侧栏/列表裁剪容器——Perplexity
    // 形态=脱离容器盖在内容上）+ 定位坐标内联注入；position:fixed 属 CSS 静态面（jsdom
    // 不算样式，由 .tam-pop 实现钉正）
    expect(menuEl().parentElement).toBe(document.body)
    expect(menuEl().classList.contains('tam-pop')).toBe(true)
    expect(menuEl().style.top).not.toBe('')
    expect(menuEl().style.left).not.toBe('')
    const labels = menuItemsW().map((x) => x.text())
    expect(labels).toEqual(['置顶', '标记为未读', '重命名', '归档', '删除'])
  })

  it('active + pinned：置顶项翻转为「取消置顶」', async () => {
    const { w } = await mountMenu(makeThread({ pinned: true }))
    await w.find('[data-testid="tam-trigger"]').trigger('click')
    const labels = menuItemsW().map((x) => x.text())
    expect(labels[0]).toBe('取消置顶')
  })

  it('archived：取消归档/重命名/删除三项；置顶与标记为未读隐藏（不是禁用）', async () => {
    const { w } = await mountMenu(makeThread({ archived: true }), 'archived')
    await w.find('[data-testid="tam-trigger"]').trigger('click')
    const items = menuItemsW()
    expect(items.map((x) => x.text())).toEqual(['取消归档', '重命名', '删除'])
    expect(items.every((x) => x.attributes('disabled') === undefined)).toBe(true)
  })
})

describe('ThreadActionMenu 未读状态感知（v1.24：已读↔标记为未读 / 未读↔标记为已读 双向）', () => {
  it('未读项：菜单第二项显「标记为已读」（i18n threads.markRead 双语），点击 → PATCH {is_unread:false} + store 未读点清除', async () => {
    expect(i18n.global.t('threads.markRead')).toBe('标记为已读')
    const { w, store } = await mountMenu(makeThread({ is_unread: true }))
    await w.find('[data-testid="tam-trigger"]').trigger('click')
    const labels = menuItemsW().map((x) => x.text())
    expect(labels[1]).toBe('标记为已读')
    mockedPatch.mockResolvedValueOnce(makeThread({ is_unread: false }))
    await menuItemsW()[1]!.trigger('click')
    await flushPromises()
    expect(mockedPatch).toHaveBeenCalledWith('11111111-1111-4111-8111-111111111111', { is_unread: false })
    expect(store.items[0]!.is_unread).toBe(false)
  })

  it('已读项：菜单第二项显「标记为未读」（既有语义保留），点击 → PATCH {is_unread:true}', async () => {
    const { w } = await mountMenu(makeThread({ is_unread: false }))
    await w.find('[data-testid="tam-trigger"]').trigger('click')
    const labels = menuItemsW().map((x) => x.text())
    expect(labels[1]).toBe('标记为未读')
    mockedPatch.mockResolvedValueOnce(makeThread({ is_unread: true }))
    await menuItemsW()[1]!.trigger('click')
    await flushPromises()
    expect(mockedPatch).toHaveBeenLastCalledWith('11111111-1111-4111-8111-111111111111', { is_unread: true })
  })

  it('标记为已读失败：乐观翻转回滚（store 未读点保留）+ toast 服务端 message', async () => {
    const { w, store } = await mountMenu(makeThread({ is_unread: true }))
    await w.find('[data-testid="tam-trigger"]').trigger('click')
    mockedPatch.mockRejectedValueOnce(new Error('网络异常'))
    await menuItemsW()[1]!.trigger('click')
    await flushPromises()
    expect(store.items[0]!.is_unread).toBe(true)
    expect(vi.mocked(toast)).toHaveBeenCalled()
  })
})

describe('ThreadActionMenu 动作分发（经 store，零直接触网）', () => {
  it('置顶 → PATCH {pinned:true}（乐观后由 store 处理）；未读 → PATCH {is_unread:true}', async () => {
    const { w } = await mountMenu(makeThread())
    await w.find('[data-testid="tam-trigger"]').trigger('click')
    const items = menuItemsW()
    mockedPatch.mockResolvedValueOnce(makeThread({ pinned: true }))
    await items[0]!.trigger('click')
    await flushPromises()
    expect(mockedPatch).toHaveBeenCalledWith('11111111-1111-4111-8111-111111111111', { pinned: true })

    await w.find('[data-testid="tam-trigger"]').trigger('click')
    const items2 = menuItemsW()
    mockedPatch.mockResolvedValueOnce(makeThread({ is_unread: true }))
    await items2[1]!.trigger('click')
    await flushPromises()
    expect(mockedPatch).toHaveBeenLastCalledWith('11111111-1111-4111-8111-111111111111', { is_unread: true })
  })

  it('archived：取消归档 → PATCH {archived:false}', async () => {
    const { w } = await mountMenu(makeThread({ archived: true }), 'archived')
    await w.find('[data-testid="tam-trigger"]').trigger('click')
    mockedPatch.mockResolvedValueOnce(makeThread())
    await menuItemsW()[0]!.trigger('click')
    await flushPromises()
    expect(mockedPatch).toHaveBeenCalledWith('11111111-1111-4111-8111-111111111111', { archived: false })
  })
})

describe('删除二次确认（v1.23：居中 modal——Teleport 到 body，document 查询）', () => {
  it('点删除 → 居中确认层（deleteConfirm 文案；role=alertdialog）；确认 → DELETE 调用且整组收起', async () => {
    const { w } = await mountMenu(makeThread())
    await w.find('[data-testid="tam-trigger"]').trigger('click')
    await menuItemsW()[4]!.trigger('click') // 删除
    const confirmLayer = () => document.querySelector('[data-testid="tam-confirm"]')
    expect(confirmLayer()).not.toBeNull()
    expect(confirmLayer()!.textContent).toContain('删除后不可恢复')
    // 居中 modal 容器语义：遮罩 + alertdialog 面板
    expect(document.querySelector('[data-testid="thread-modal-overlay"]')).not.toBeNull()
    expect(document.querySelector('.tm-panel')!.getAttribute('role')).toBe('alertdialog')
    // 取消 → 不发 DELETE
    ;(document.querySelector('[data-testid="tam-cancel"]') as HTMLElement).click()
    await flushPromises()
    expect(mockedDelete).not.toHaveBeenCalled()
    expect(confirmLayer()).toBeNull()
    // 再走一遍 → 确认
    await w.find('[data-testid="tam-trigger"]').trigger('click')
    await menuItemsW()[4]!.trigger('click')
    mockedDelete.mockResolvedValueOnce({ deleted: true })
    ;(document.querySelector('[data-testid="tam-confirm-delete"]') as HTMLElement).click()
    await flushPromises()
    expect(mockedDelete).toHaveBeenCalledWith('11111111-1111-4111-8111-111111111111')
    expect(confirmLayer()).toBeNull()
    expect(document.querySelector('[role="menu"]')).toBeNull()
  })

  it('确认层遮罩点击 = 取消（零请求收起）；× 钮同效', async () => {
    const { w } = await mountMenu(makeThread())
    await w.find('[data-testid="tam-trigger"]').trigger('click')
    await menuItemsW()[4]!.trigger('click')
    // 遮罩点击（面板外）
    ;(document.querySelector('[data-testid="thread-modal-overlay"]') as HTMLElement).click()
    await flushPromises()
    expect(mockedDelete).not.toHaveBeenCalled()
    expect(document.querySelector('[data-testid="tam-confirm"]')).toBeNull()
    // × 钮
    await w.find('[data-testid="tam-trigger"]').trigger('click')
    await menuItemsW()[4]!.trigger('click')
    ;(document.querySelector('[data-testid="thread-modal-close"]') as HTMLElement).click()
    await flushPromises()
    expect(mockedDelete).not.toHaveBeenCalled()
    expect(document.querySelector('[data-testid="tam-confirm"]')).toBeNull()
  })
})

describe('删除当前浏览中的线程 → 跳转首页（删除成功且 route.params.threadId 命中被删线程）', () => {
  it('删除成功且当前路由=被删线程（/thread/{id} 答案页侧栏删除）→ 确认后跳转 /（新建）', async () => {
    const thread = makeThread()
    const { w, router } = await mountMenu(thread, 'active', `/thread/${thread.thread_id}`)
    expect(router.currentRoute.value.path).toBe(`/thread/${thread.thread_id}`)
    await w.find('[data-testid="tam-trigger"]').trigger('click')
    await menuItemsW()[4]!.trigger('click') // 删除 → 居中确认层
    mockedDelete.mockResolvedValueOnce({ deleted: true })
    ;(document.querySelector('[data-testid="tam-confirm-delete"]') as HTMLElement).click()
    await flushPromises()
    expect(mockedDelete).toHaveBeenCalledWith(thread.thread_id)
    // 跳转在删除确认成功之后：store.deleteThread 返回 true 才 push('/')
    expect(router.currentRoute.value.path).toBe('/')
  })

  it('删除失败（store 返回 false——行已回滚保留）不跳转（留在被删线程页）', async () => {
    const thread = makeThread()
    const { w, router } = await mountMenu(thread, 'active', `/thread/${thread.thread_id}`)
    await w.find('[data-testid="tam-trigger"]').trigger('click')
    await menuItemsW()[4]!.trigger('click')
    mockedDelete.mockRejectedValueOnce(new Error('网络异常'))
    ;(document.querySelector('[data-testid="tam-confirm-delete"]') as HTMLElement).click()
    await flushPromises()
    expect(mockedDelete).toHaveBeenCalledWith(thread.thread_id)
    expect(router.currentRoute.value.path).toBe(`/thread/${thread.thread_id}`)
  })

  it('当前路由非被删线程（如 /threads 会话页行级菜单）→ 删除成功也不跳转（route.params 不匹配 no-op）', async () => {
    const thread = makeThread()
    const { w, router } = await mountMenu(thread, 'active', '/threads')
    await w.find('[data-testid="tam-trigger"]').trigger('click')
    await menuItemsW()[4]!.trigger('click')
    mockedDelete.mockResolvedValueOnce({ deleted: true })
    ;(document.querySelector('[data-testid="tam-confirm-delete"]') as HTMLElement).click()
    await flushPromises()
    expect(mockedDelete).toHaveBeenCalledWith(thread.thread_id)
    expect(router.currentRoute.value.path).toBe('/threads')
  })
})

describe('重命名（v1.23：居中 modal——预填全选输入框 + 单个保存钮）', () => {
  /** Teleport 后经 document 查询（wrapper.find 不穿 Teleport） */
  function renameLayer(): HTMLElement | null {
    return document.querySelector('[data-testid="tam-rename"]')
  }
  function renameInput(): HTMLInputElement {
    return document.querySelector('[data-testid="tam-rename-input"]') as HTMLInputElement
  }

  it('点重命名 → 居中 modal（renameTitle 标题）；输入预填全选聚焦；空串禁提交；合法值提交 PATCH {title}', async () => {
    const { w } = await mountMenu(makeThread())
    await w.find('[data-testid="tam-trigger"]').trigger('click')
    await menuItemsW()[2]!.trigger('click') // 重命名
    const layer = renameLayer()
    expect(layer).not.toBeNull()
    // 标题在 modal 头部（ThreadModal .tm-title），正文槽 = 输入 + 保存钮
    expect(document.querySelector('.tm-panel')!.textContent).toContain('重命名会话')
    const input = renameInput()
    expect(input.value).toBe('LangGraph 调研')
    expect(input.getAttribute('maxlength')).toBe('120')
    // 预填全选 + 聚焦（Perplexity 形态：打开即全选可直接覆盖输入）
    expect(document.activeElement).toBe(input)
    expect(input.selectionStart).toBe(0)
    expect(input.selectionEnd).toBe('LangGraph 调研'.length)
    // 单个保存钮（无取消钮——取消=Esc/遮罩/×）
    const buttons = layer!.querySelectorAll('button')
    expect(buttons).toHaveLength(1)
    expect(buttons[0]!.textContent!.trim()).toBe('保存')
    // 空串 → 保存钮 disabled
    input.value = '   '
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    expect(document.querySelector('[data-testid="tam-rename-ok"]')!.hasAttribute('disabled')).toBe(true)
    // 合法值 → PATCH {title}（trim 后）
    input.value = '  新标题  '
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    mockedPatch.mockResolvedValueOnce(makeThread({ title: '新标题' }))
    ;(document.querySelector('[data-testid="tam-rename-ok"]') as HTMLElement).click()
    await flushPromises()
    expect(mockedPatch).toHaveBeenCalledWith('11111111-1111-4111-8111-111111111111', { title: '新标题' })
    expect(renameLayer()).toBeNull()
  })

  it('IME 组合期 Enter（isComposing=true）不提交（P2-2：拼音候选确认不触发 PATCH）；正常 Enter 提交', async () => {
    const { w } = await mountMenu(makeThread())
    await w.find('[data-testid="tam-trigger"]').trigger('click')
    await menuItemsW()[2]!.trigger('click') // 重命名
    const input = renameInput()
    input.value = 'IME 半成品输入'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    // jsdom 不经构造器暴露 isComposing——原生派发 + defineProperty 模拟组合期事件
    const composing = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })
    Object.defineProperty(composing, 'isComposing', { value: true })
    input.dispatchEvent(composing)
    await flushPromises()
    expect(mockedPatch).not.toHaveBeenCalled()
    expect(renameLayer()).not.toBeNull() // modal 未关
    // 正常 Enter（组合已结束）→ 提交
    mockedPatch.mockResolvedValueOnce(makeThread({ title: 'IME 半成品输入' }))
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }))
    await flushPromises()
    expect(mockedPatch).toHaveBeenCalledWith('11111111-1111-4111-8111-111111111111', { title: 'IME 半成品输入' })
    expect(renameLayer()).toBeNull()
  })

  it('Esc 关闭重命名 modal 且焦点返还触发钮；遮罩点击取消零请求', async () => {
    const { w } = await mountMenu(makeThread())
    const trigger = w.find('[data-testid="tam-trigger"]')
    await trigger.trigger('click')
    await menuItemsW()[2]!.trigger('click')
    expect(renameLayer()).not.toBeNull()
    // Esc（input 上派发冒泡到 document）→ 关闭 + 焦点返还
    renameInput().dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await flushPromises()
    expect(renameLayer()).toBeNull()
    expect(document.activeElement).toBe(trigger.element)
    expect(mockedPatch).not.toHaveBeenCalled()
    // 遮罩点击 → 取消零请求
    await trigger.trigger('click')
    await menuItemsW()[2]!.trigger('click')
    ;(document.querySelector('[data-testid="thread-modal-overlay"]') as HTMLElement).click()
    await flushPromises()
    expect(renameLayer()).toBeNull()
    expect(mockedPatch).not.toHaveBeenCalled()
  })

  it('IME 组合期 Esc（isComposing=true）不关闭 modal（评审 P3：与 Enter isComposing 守卫同族对称——取消候选不得连带丢半成品输入）', async () => {
    const { w } = await mountMenu(makeThread())
    await w.find('[data-testid="tam-trigger"]').trigger('click')
    await menuItemsW()[2]!.trigger('click')
    const input = renameInput()
    input.value = '拼音半成品'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    // 组合期 Esc（jsdom 不经构造器暴露 isComposing——defineProperty 模拟，同 Enter 守卫测试口径）
    const composingEsc = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    Object.defineProperty(composingEsc, 'isComposing', { value: true })
    document.dispatchEvent(composingEsc)
    await flushPromises()
    expect(renameLayer()).not.toBeNull() // modal 未关
    expect(renameInput().value).toBe('拼音半成品') // 半成品输入保留
    expect(mockedPatch).not.toHaveBeenCalled()
    // 正常 Esc（组合已结束）→ 关闭 + 焦点返还
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await flushPromises()
    expect(renameLayer()).toBeNull()
    expect(document.activeElement).toBe(w.find('[data-testid="tam-trigger"]').element)
  })
})

describe('弹层定位（v1.25 裁剪修复：Teleport body + fixed——向下首选、贴底翻上、滚动即隐）', () => {
  it('触发钮贴视口底：菜单向上翻（top 语义=触发钮上缘之上，而非向下越出视口）；正常位向下弹出', async () => {
    const { w } = await mountMenu(makeThread())
    const trigger = w.find('[data-testid="tam-trigger"]').element as HTMLElement
    // 正常态：jsdom rect 全 0 → 向下（top ≥ 4px 起步）
    await w.find('[data-testid="tam-trigger"]').trigger('click')
    const downTop = Number.parseInt(menuEl().style.top, 10)
    expect(downTop).toBeGreaterThanOrEqual(4)
    await w.find('[data-testid="tam-trigger"]').trigger('click') // 关
    // 贴底态：触发钮下缘=视口底（768）→ 向下越出视口，翻上（top < 触发钮 rect.bottom）
    const rectBottom = 768
    trigger.getBoundingClientRect = () =>
      ({ top: rectBottom - 30, bottom: rectBottom, left: 20, right: 60, width: 40, height: 30 }) as DOMRect
    await w.find('[data-testid="tam-trigger"]').trigger('click')
    const flippedTop = Number.parseInt(menuEl().style.top, 10)
    expect(flippedTop).toBeLessThan(rectBottom) // 语义级：菜单整体位于触发钮上缘之上（不再向下越出）
    expect(flippedTop).toBeGreaterThan(0) // 且未溢出视口顶
    await w.find('[data-testid="tam-trigger"]').trigger('click')
    expect(document.querySelector('[role="menu"]')).toBeNull()
  })

  it('窗口滚动/resize 即隐（与预览卡滚动即隐口径一致——裁剪容器滚动不顶飞 fixed 菜单）', async () => {
    const { w } = await mountMenu(makeThread())
    await w.find('[data-testid="tam-trigger"]').trigger('click')
    expect(menuEl()).not.toBeNull()
    window.dispatchEvent(new Event('scroll'))
    await flushPromises()
    expect(document.querySelector('[role="menu"]')).toBeNull()
    // resize 同口径
    await w.find('[data-testid="tam-trigger"]').trigger('click')
    expect(menuEl()).not.toBeNull()
    window.dispatchEvent(new Event('resize'))
    await flushPromises()
    expect(document.querySelector('[role="menu"]')).toBeNull()
  })
})

describe('外点豁免菜单层（v1.25 回归修复：Teleport 后菜单在 body——按下菜单项不得判外点卸载）', () => {
  it('菜单打开后菜单项 mousedown → 菜单保持打开（不被外点关闭误卸载）', async () => {
    const { w } = await mountMenu(makeThread())
    await w.find('[data-testid="tam-trigger"]').trigger('click')
    expect(menuEl()).not.toBeNull()
    const item = menuItemsW()[0]!
    item.element.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
    await flushPromises()
    expect(document.querySelector('[role="menu"]')).not.toBeNull()
  })

  it('完整点击序列（mousedown+click 于菜单项）→ runItem 动作真实执行（置顶 PATCH 调用）', async () => {
    const { w } = await mountMenu(makeThread())
    await w.find('[data-testid="tam-trigger"]').trigger('click')
    mockedPatch.mockResolvedValueOnce(makeThread({ pinned: true }))
    const item = menuItemsW()[0]!
    item.element.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
    // nextTick 排空（模拟真实帧间隔：浏览器 mousedown→click 间卸载已 flush）——
    // 修复前菜单此刻已被误卸载、元素 detached，click 到不了 runItem（本用例红）
    await w.vm.$nextTick()
    await item.trigger('click')
    await flushPromises()
    expect(mockedPatch).toHaveBeenCalledWith('11111111-1111-4111-8111-111111111111', { pinned: true })
  })

  it('根外真外部 mousedown → 照常关闭（外点关闭回归护栏）', async () => {
    const { w } = await mountMenu(makeThread())
    await w.find('[data-testid="tam-trigger"]').trigger('click')
    expect(menuEl()).not.toBeNull()
    document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
    await flushPromises()
    expect(document.querySelector('[role="menu"]')).toBeNull()
  })
})

describe('a11y：Esc 关闭与焦点返还触发钮', () => {
  it('菜单打开时 Esc → 菜单关闭且焦点返还触发钮', async () => {
    const { w } = await mountMenu(makeThread())
    const trigger = w.find('[data-testid="tam-trigger"]')
    await trigger.trigger('click')
    expect(menuEl()).not.toBeNull()
    await trigger.trigger('keydown', { key: 'Escape' })
    expect(document.querySelector('[role="menu"]')).toBeNull()
    expect(document.activeElement).toBe(trigger.element)
  })

  it('确认层 Esc 同样关闭整组并返还焦点（v1.23 居中 modal：Esc 冒泡至 document 监听）', async () => {
    const { w } = await mountMenu(makeThread())
    const trigger = w.find('[data-testid="tam-trigger"]')
    await trigger.trigger('click')
    await menuItemsW()[4]!.trigger('click')
    expect(document.querySelector('[data-testid="tam-confirm"]')).not.toBeNull()
    await trigger.trigger('keydown', { key: 'Escape' })
    expect(document.querySelector('[data-testid="tam-confirm"]')).toBeNull()
    expect(document.activeElement).toBe(trigger.element)
  })

  it('方向键在 menuitem 间移动焦点（ArrowDown/ArrowUp）', async () => {
    const { w } = await mountMenu(makeThread())
    await w.find('[data-testid="tam-trigger"]').trigger('click')
    const items = menuItemsW()
    expect(document.activeElement).toBe(items[0]!.element) // 打开即落首项
    await items[0]!.trigger('keydown', { key: 'ArrowDown' })
    expect(document.activeElement).toBe(items[1]!.element)
    await items[1]!.trigger('keydown', { key: 'ArrowUp' })
    expect(document.activeElement).toBe(items[0]!.element)
  })
})
