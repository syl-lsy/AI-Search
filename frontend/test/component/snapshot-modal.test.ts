/**
 * test/component/snapshot-modal.test.ts — SnapshotModal 来源快照预览弹窗（批次 6，api.md §5.1）。
 *
 * 组件形态：复用 ThreadModal 居中 modal 壳（Teleport/遮罩/×/Esc 三关闭通道 + aria-modal），
 * 本组件承载快照域内容——excerpt 摘录（纯文本+换行保留渲染、先经 DOMPurify 纵深消毒）、
 * 元数据行（fetched_at/size_bytes/content_type）、「打开原文」外链（download_url 过
 * isHttpUrl 协议白名单守卫，target=_blank noopener noreferrer）+ 焦点返还（open 态记录
 * 之前焦点元素，close 态归还——ThreadModal 契约「焦点返还由调用方处理」的本组件内实现）。
 * 取色全走 tokens.css 变量（设计 tokens 强制）。
 */
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { beforeEach, describe, expect, it } from 'vitest'

import SnapshotModal from '@/components/answer/SnapshotModal.vue'
import { i18n } from '@/i18n'
import type { SnapshotData } from '@/types/api'

const SNAPSHOT: SnapshotData = {
  snapshot_id: 'e5cc1111-1111-4111-8111-111111111111',
  url: 'https://example.com/original-page',
  content_type: 'text/html',
  size_bytes: 20480,
  fetched_at: '2026-11-01T08:12:03Z',
  download_url: 'http://127.0.0.1:9000/ai-search-snapshots/e5cc1111?X-Amz-Signature=abc',
  excerpt: '# 原文标题\n\n第一段正文。\n第二段正文。',
}

function mountModal(open: boolean, data: SnapshotData | null = SNAPSHOT) {
  return mount(SnapshotModal, {
    props: { open, data },
    global: { plugins: [i18n] },
  })
}

const overlay = (): Element | null => document.querySelector('[data-testid="thread-modal-overlay"]')
const excerptEl = (): Element | null => document.querySelector('[data-testid="snapshot-excerpt"]')
const openOriginalEl = (): HTMLAnchorElement | null =>
  document.querySelector<HTMLAnchorElement>('[data-testid="snapshot-open-original"]')

beforeEach(() => {
  document.body.innerHTML = ''
})

describe('SnapshotModal（批次 6：api.md §5.1 弹窗预览面）', () => {
  it('open=false：整块不渲染（无 overlay 无面板）', () => {
    const w = mountModal(false)
    expect(overlay()).toBeNull()
    expect(excerptEl()).toBeNull()
    w.unmount()
  })

  it('open=true：overlay + role=dialog + aria-modal + 摘录换行保留渲染（pre-wrap 纯文本，两段正文各占行）', async () => {
    const w = mountModal(true)
    await nextTick()
    const panel = document.querySelector('[role="dialog"][aria-modal="true"]')
    expect(panel).not.toBeNull()
    expect(panel!.getAttribute('aria-label')).toBe('快照预览')
    const el = excerptEl()
    expect(el).not.toBeNull()
    // 换行保留：textContent 含两段正文（white-space: pre-wrap 由类承载——快照域纯文本，不走 markdown 渲染）
    expect(el!.textContent).toContain('# 原文标题')
    expect(el!.textContent).toContain('第一段正文。')
    expect(el!.textContent).toContain('第二段正文。')
    w.unmount()
  })

  it('元数据行：抓取时间（ISO T→空格）/ 大小（20480B→20.0 KB）/ 类型（content_type 原样）', async () => {
    const w = mountModal(true)
    await nextTick()
    const meta = document.querySelector('[data-testid="snapshot-meta"]')
    expect(meta).not.toBeNull()
    expect(meta!.textContent).toContain('抓取时间')
    expect(meta!.textContent).toContain('2026-11-01 08:12:03')
    expect(meta!.textContent).toContain('大小')
    expect(meta!.textContent).toContain('20.0 KB')
    expect(meta!.textContent).toContain('类型')
    expect(meta!.textContent).toContain('text/html')
    w.unmount()
  })

  it('「打开原文」外链钮：href=download_url 原样 + target=_blank + rel 含 noopener noreferrer', async () => {
    const w = mountModal(true)
    await nextTick()
    const a = openOriginalEl()
    expect(a).not.toBeNull()
    expect(a!.getAttribute('href')).toBe(SNAPSHOT.download_url)
    expect(a!.getAttribute('target')).toBe('_blank')
    expect(a!.getAttribute('rel')).toContain('noopener')
    expect(a!.getAttribute('rel')).toContain('noreferrer')
    expect(a!.textContent).toContain('打开原文')
    w.unmount()
  })

  it('download_url 非 http（协议白名单守卫）：外链钮不渲染，摘录/元数据照常（AnswerView 侧另行 toast 快照地址不可用）', async () => {
    const w = mount(SnapshotModal, {
      props: { open: true, data: { ...SNAPSHOT, download_url: 'javascript:alert(1)' } },
      global: { plugins: [i18n] },
    })
    await nextTick()
    expect(openOriginalEl()).toBeNull()
    expect(excerptEl()).not.toBeNull()
    expect(document.querySelector('[data-testid="snapshot-meta"]')).not.toBeNull()
    w.unmount()
  })

  it('excerpt 注入形态纵深消毒：DOMPurify 剥 <script>——无 script 元素、无 alert 文本外泄', async () => {
    const w = mount(SnapshotModal, {
      props: { open: true, data: { ...SNAPSHOT, excerpt: '<script>alert(1)</script>正文' } },
      global: { plugins: [i18n] },
    })
    await nextTick()
    expect(document.querySelector('script')).toBeNull()
    expect(excerptEl()!.textContent).not.toContain('alert')
    expect(excerptEl()!.textContent).toContain('正文')
    w.unmount()
  })

  it('Esc 关闭（ThreadModal 承载通道）→ emit close', async () => {
    const w = mountModal(true)
    await nextTick()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await flushPromises()
    expect(w.emitted('close')).toEqual([[]])
    w.unmount()
  })

  it('遮罩点击 / × 钮两条关闭通道 → emit close', async () => {
    const w = mountModal(true)
    await nextTick()
    overlay()!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flushPromises()
    expect(w.emitted('close')).toEqual([[]])

    document.querySelector('[data-testid="thread-modal-close"]')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flushPromises()
    expect(w.emitted('close')).toHaveLength(2)
    w.unmount()
  })

  it('焦点返还（ThreadModal 契约的本组件内实现）：open 记录前焦点并落弹窗体，close 归还', async () => {
    const outside = document.createElement('button')
    document.body.appendChild(outside)
    outside.focus()
    expect(document.activeElement).toBe(outside)

    const w = mountModal(false)
    await w.setProps({ open: true })
    await nextTick()
    // 打开态：焦点落弹窗内容体（tabindex=-1 承接——避免焦点残留 body）
    expect(document.activeElement).toBe(document.querySelector('[data-testid="snapshot-modal-body"]'))

    await w.setProps({ open: false })
    await nextTick()
    // 关闭态：焦点归还此前聚焦元素
    expect(document.activeElement).toBe(outside)
    w.unmount()
  })
})
