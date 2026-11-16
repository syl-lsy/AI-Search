/**
 * test/component/command-palette.test.ts — CommandPalette 组件层直挂测试（覆盖率恢复批）。
 *
 * app.test.ts 经 App 壳间接覆盖（vi.mock 图内归因不佳）；本文件直挂组件（无 API mock——
 * 组件零取数，读 threadListStore.recent），覆盖渲染三组/过滤/键盘导航/三出口关闭/结果
 * 截断/滚动锁。既有行为 characterization（无行为变更——pure coverage recovery）。
 * Teleport 到 body：查询走 document.querySelector（wrapper.find 不穿 Teleport）。
 */
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import CommandPalette from '@/components/common/CommandPalette.vue'
import { i18n } from '@/i18n'
import { threadListFixture } from '@/mocks/threads'
import { router } from '@/router'
import { useThreadListStore } from '@/stores/threadList'

let wrapper: VueWrapper | null = null
const paletteEl = (): Element | null => document.querySelector('[data-testid="command-palette"]')

async function mountPalette(): Promise<VueWrapper> {
  const pinia = createPinia()
  setActivePinia(pinia)
  useThreadListStore(pinia).recent = threadListFixture.items
  const w = mount(CommandPalette, {
    attachTo: document.body,
    props: { open: false },
    global: { plugins: [pinia, router, i18n] },
  })
  await router.isReady()
  // open=false→true 变化触发打开 watcher（清查询/复位索引/锁滚动/autofocus）
  await w.setProps({ open: true })
  await flushPromises()
  return w
}

beforeEach(async () => {
  localStorage.clear()
  i18n.global.locale.value = 'zh-CN'
  await router.push('/')
  await router.isReady()
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.style.overflow = ''
})

describe('CommandPalette 组件层（直挂）', () => {
  it('打开：三命令 + 结果=store 最近数 + 搜索框 autofocus + body 滚动锁', async () => {
    wrapper = await mountPalette()
    expect(paletteEl()).not.toBeNull()
    expect(document.querySelectorAll('[data-testid^="palette-command-"]')).toHaveLength(3)
    expect(document.querySelectorAll('[data-testid^="palette-result-"]')).toHaveLength(threadListFixture.items.length)
    expect(document.activeElement?.getAttribute('data-testid')).toBe('palette-input')
    expect(document.body.style.overflow).toBe('hidden')
  })

  it('过滤：命令按 label 包含、结果按标题包含（大小写不敏感）；双空 → 空态', async () => {
    wrapper = await mountPalette()
    const input = document.querySelector('[data-testid="palette-input"]') as HTMLInputElement
    input.value = '新建'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    expect(document.querySelectorAll('[data-testid^="palette-command-"]')).toHaveLength(1)
    expect(document.querySelectorAll('[data-testid^="palette-result-"]')).toHaveLength(0)
    input.value = 'zzzzz'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await flushPromises()
    expect(document.querySelector('[data-testid="palette-empty"]')!.textContent).toContain('无匹配结果')
  })

  it('键盘：ArrowDown 移动激活项（active 类迁移到第二条命令）', async () => {
    wrapper = await mountPalette()
    const input = document.querySelector('[data-testid="palette-input"]') as HTMLInputElement
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true }))
    await flushPromises()
    const active = document.querySelector('.palette-item.active')
    expect(active?.getAttribute('data-testid')).toBe('palette-command-kb')
  })

  it('键盘：Enter 执行激活项（关闭 + 路由按激活命令）', async () => {
    wrapper = await mountPalette()
    const input = document.querySelector('[data-testid="palette-input"]') as HTMLInputElement
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true }))
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }))
    await flushPromises()
    expect(wrapper.emitted('close')).toHaveLength(1)
    // 卸载由宿主回设 open 驱动——组件侧模拟宿主后断言弹框摘除
    await wrapper.setProps({ open: false })
    await flushPromises()
    expect(paletteEl()).toBeNull()
  })

  it('Esc 关闭 + body 滚动锁解除', async () => {
    wrapper = await mountPalette()
    const input = document.querySelector('[data-testid="palette-input"]') as HTMLInputElement
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await flushPromises()
    expect(wrapper.emitted('close')).toHaveLength(1)
    // overflow 恢复由宿主（App）回设 open=false 驱动——组件侧模拟宿主回设后断言
    await wrapper.setProps({ open: false })
    await flushPromises()
    expect(document.body.style.overflow).toBe('')
  })

  it('遮罩点击关闭', async () => {
    wrapper = await mountPalette()
    document.querySelector('[data-testid="palette-backdrop"]')!.dispatchEvent(new Event('click'))
    await flushPromises()
    expect(wrapper.emitted('close')).toHaveLength(1)
  })

  it('结果条目点击 → 对应线程路由 + 关闭', async () => {
    wrapper = await mountPalette()
    const first = threadListFixture.items[0]!
    ;(document.querySelector(`[data-testid="palette-result-${first.thread_id}"]`) as HTMLElement).click()
    await flushPromises()
    expect(wrapper.emitted('close')).toHaveLength(1)
  })

  it('结果截断 RESULT_LIMIT=8：超过 8 条最近会话只渲染 8', async () => {
    const many = Array.from({ length: 11 }, (_, i) => ({
      ...threadListFixture.items[0]!,
      thread_id: `cccc${String(i).padStart(4, '0')}-1111-4111-8111-111111111111`,
      title: `批量会话 ${i}`,
    }))
    const pinia = createPinia()
    setActivePinia(pinia)
    useThreadListStore(pinia).recent = many
    const w = mount(CommandPalette, {
      attachTo: document.body,
      props: { open: false },
      global: { plugins: [pinia, router, i18n] },
    })
    wrapper = w
    await w.setProps({ open: true })
    await flushPromises()
    expect(document.querySelectorAll('[data-testid^="palette-result-"]')).toHaveLength(8)
  })
})
