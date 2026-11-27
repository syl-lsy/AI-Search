/**
 * test/component/kb-chunk-drilldown.test.ts — KbChunkDrilldown 溯源下钻弹窗
 * （frontend-api.md §5 KbChunkDrilldown 行 v1.33/v1.34；api.md §4.6 消费面；plan-m3-b6-f6 §1.3）。
 * ThreadModal 壳（role=dialog/aria-modal/Esc/×/遮罩）+ 文档面（§4.4 合流：catalog_path/parse_meta）
 * + 切片面（§4.6：heading_path 面包屑/page·paragraph pills/content 文本插值直渲〔类标签原文
 *   逐字保真，不经 DOMPurify——A36〕/meta-mono）
 * + 「打开飞书原文」外链（node_token 存在且过 isHttpUrl 守卫才渲染）+ 404001 占位卡降级 + 焦点双相位。
 */
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import KbChunkDrilldown from '@/components/answer/KbChunkDrilldown.vue'
import { i18n } from '@/i18n'
import type { KbChunkDetail, KbDocument } from '@/types/api'

const CHUNK: KbChunkDetail = {
  chunk_id: '5c2e0001-0000-4000-8000-000000000001',
  document_id: '9a770001-0000-4000-8000-000000000002',
  document_title: '内部设计文档-检索融合.md',
  content: '一致性哈希将整个哈希空间组织成一个环，节点与数据键都映射到环上。',
  heading_path: ['第三章 检索融合', '3.2 混合检索'],
  page: 31,
  paragraph: 2,
}

const DOC: KbDocument = {
  document_id: '9a770001-0000-4000-8000-000000000002',
  doc_type: 'pdf',
  title: '系统设计面试手册.pdf',
  file_size_bytes: 8_100_000,
  status: 'ready',
  fail_reason: null,
  chunk_count: 214,
  created_at: '2026-11-01T08:12:00Z',
  parse_meta: { pages: 46, tables: 3, code_blocks: 8 },
  catalog_path: '家庭知识库 / 大模型全套面试题 / 面试资料包',
}

function mountModal(props: {
  open: boolean
  chunk?: KbChunkDetail | null
  doc?: KbDocument | null
  unavailable?: boolean
}) {
  return mount(KbChunkDrilldown, {
    props: { open: props.open, chunk: props.chunk ?? null, doc: props.doc ?? null, unavailable: props.unavailable ?? false },
    global: { plugins: [i18n] },
    attachTo: document.body,
  })
}

const overlay = (): Element | null => document.querySelector('[data-testid="thread-modal-overlay"]')
const drill = (): Element | null => document.querySelector('[data-testid="kb-chunk-drilldown"]')

beforeEach(() => {
  document.body.innerHTML = ''
})

describe('KbChunkDrilldown — 壳与切片面（§4.6）', () => {
  it('open=false 整块不渲染；open=true：role=dialog + aria-modal + 标题「知识库来源」', async () => {
    const w = mountModal({ open: false, chunk: CHUNK, doc: DOC })
    await nextTick()
    expect(overlay()).toBeNull()
    w.unmount()

    const w2 = mountModal({ open: true, chunk: CHUNK, doc: DOC })
    await nextTick()
    const panel = document.querySelector('[role="dialog"][aria-modal="true"]')
    expect(panel).not.toBeNull()
    expect(panel!.getAttribute('aria-label')).toBe('知识库来源')
    expect(drill()).not.toBeNull()
    w2.unmount()
  })

  it('heading_path 面包屑 root→leaf（末位 leaf 高亮）；空数组不渲染面包屑', async () => {
    const w = mountModal({ open: true, chunk: CHUNK, doc: DOC })
    await nextTick()
    const crumbs = document.querySelectorAll('[data-testid="kb-chunk-heading"] .crumb-seg')
    expect(crumbs).toHaveLength(2)
    expect(crumbs[0]!.textContent).toBe('第三章 检索融合')
    expect(crumbs[1]!.textContent).toBe('3.2 混合检索')
    expect(crumbs[1]!.classList.contains('leaf')).toBe(true)
    w.unmount()

    document.body.innerHTML = ''
    const w2 = mountModal({ open: true, chunk: { ...CHUNK, heading_path: [] }, doc: DOC })
    await nextTick()
    expect(document.querySelector('[data-testid="kb-chunk-heading"]')).toBeNull()
    w2.unmount()
  })

  it('page/paragraph 定位 pills；null 不渲染对应段（键恒出现值可空）', async () => {
    const w = mountModal({ open: true, chunk: CHUNK, doc: DOC })
    await nextTick()
    const pills = document.querySelectorAll('[data-testid="kb-chunk-loc"] .loc-pill')
    expect(pills).toHaveLength(2)
    expect(pills[0]!.textContent).toContain('第 31 页')
    expect(pills[1]!.textContent).toContain('段落 2')
    w.unmount()

    document.body.innerHTML = ''
    const w2 = mountModal({ open: true, chunk: { ...CHUNK, page: null, paragraph: null }, doc: DOC })
    await nextTick()
    expect(document.querySelector('[data-testid="kb-chunk-loc"]')).toBeNull()
    w2.unmount()
  })

  it('content 类标签纯文本保真（A36 溯源原文预览）：不经 DOMPurify 逐字渲染，不剥失不变形', async () => {
    const tagged = {
      ...CHUNK,
      content: '代码示例：#include <stdio.h> 后 return 0\n比较表达式 a<b 且 c>d',
    }
    const w = mountModal({ open: true, chunk: tagged, doc: DOC })
    await nextTick()
    const contentEl = document.querySelector('[data-testid="kb-chunk-content"]')
    expect(contentEl).not.toBeNull()
    // 类标签形态原文逐字保真（DOMPurify 会剥失 <stdio.h>、把「且 c」吞进虚构 <b> 结构）
    expect(contentEl!.textContent).toContain('#include <stdio.h> 后 return 0')
    expect(contentEl!.textContent).toContain('a<b 且 c>d')
    w.unmount()
  })

  it('content 文本插值转义：`<script>` 注入不执行、无脚本元素、pre-wrap 保留', async () => {
    const hostile = { ...CHUNK, content: '<script>window.__pwned = true</script>正文第一行\n正文第二行' }
    const w = mountModal({ open: true, chunk: hostile, doc: DOC })
    await nextTick()
    await flushPromises()
    const contentEl = document.querySelector('[data-testid="kb-chunk-content"]')
    expect(contentEl).not.toBeNull()
    expect((window as unknown as { __pwned?: boolean }).__pwned).toBeUndefined()
    expect(contentEl!.querySelector('script')).toBeNull()
    // 文本插值：消毒后残余以纯文本呈现（转义），换行保留 pre-wrap
    expect(contentEl!.textContent).toContain('正文第一行\n正文第二行')
    w.unmount()
  })

  it('meta-mono 行：chunk_id/document_id 恒在；node_id/node_token 可选键缺则不渲染', async () => {
    const w = mountModal({ open: true, chunk: CHUNK, doc: DOC })
    await nextTick()
    const meta = document.querySelector('[data-testid="kb-chunk-meta"]')!.textContent ?? ''
    expect(meta).toContain(CHUNK.chunk_id)
    expect(meta).toContain(CHUNK.document_id)
    expect(meta).not.toContain('node_id')
    expect(meta).not.toContain('node_token')
    w.unmount()

    document.body.innerHTML = ''
    const w2 = mountModal({
      open: true,
      chunk: { ...CHUNK, node_id: 'fs:node:WTp2x', node_token: 'WTp2dAqBposqlFxMmmhcnLsCnmf' },
      doc: DOC,
    })
    await nextTick()
    const meta2 = document.querySelector('[data-testid="kb-chunk-meta"]')!.textContent ?? ''
    expect(meta2).toContain('node_id')
    expect(meta2).toContain('fs:node:WTp2x')
    w2.unmount()
  })
})

describe('KbChunkDrilldown — 文档面（§4.4 合流）与飞书外链（v1.34）', () => {
  it('文档面：标题 + 【知识库】徽标 + catalog_path 目录链 + parse_meta chips（null 段不渲染）', async () => {
    const w = mountModal({ open: true, chunk: CHUNK, doc: DOC })
    await nextTick()
    const docFace = document.querySelector('[data-testid="kb-chunk-doc"]')!
    expect(docFace.textContent).toContain('系统设计面试手册.pdf')
    expect(docFace.textContent).toContain('知识库')
    expect(docFace.textContent).toContain('家庭知识库')
    expect(docFace.textContent).toContain('面试资料包')
    const chips = docFace.querySelectorAll('[data-testid="kb-chunk-parse"] .pm-chip')
    expect(chips).toHaveLength(3)
    expect(chips[0]!.textContent).toContain('页 46')
    expect(chips[1]!.textContent).toContain('表格 3')
    expect(chips[2]!.textContent).toContain('代码块 8')
    w.unmount()
  })

  it('parse_meta pages=null（非页式）→ 页 chip 不渲染；doc=null → 文档面整块不渲染（降级隐藏）', async () => {
    const w = mountModal({
      open: true,
      chunk: CHUNK,
      doc: { ...DOC, parse_meta: { pages: null, tables: 0, code_blocks: 0 } },
    })
    await nextTick()
    const chips = document.querySelectorAll('[data-testid="kb-chunk-parse"] .pm-chip')
    expect(chips).toHaveLength(2)
    w.unmount()

    document.body.innerHTML = ''
    const w2 = mountModal({ open: true, chunk: CHUNK, doc: null })
    await nextTick()
    expect(document.querySelector('[data-testid="kb-chunk-doc"]')).toBeNull()
    // 文档面缺失不遮蔽切片面
    expect(drill()!.textContent).toContain('一致性哈希')
    w2.unmount()
  })

  it('node_token 存在 →「打开飞书原文」外链（href=https://www.feishu.cn/wiki/{token} + target=_blank + rel noopener noreferrer）', async () => {
    const w = mountModal({
      open: true,
      chunk: { ...CHUNK, node_token: 'WTp2dAqBposqlFxMmmhcnLsCnmf' },
      doc: DOC,
    })
    await nextTick()
    const link = document.querySelector<HTMLAnchorElement>('[data-testid="kb-chunk-feishu"]')
    expect(link).not.toBeNull()
    expect(link!.getAttribute('href')).toBe('https://www.feishu.cn/wiki/WTp2dAqBposqlFxMmmhcnLsCnmf')
    expect(link!.getAttribute('target')).toBe('_blank')
    expect(link!.getAttribute('rel')).toContain('noopener')
    expect(link!.getAttribute('rel')).toContain('noreferrer')
    w.unmount()
  })

  it('node_token 缺省（本地/URL 来源形态）→ 外链整钮不渲染', async () => {
    const w = mountModal({ open: true, chunk: CHUNK, doc: DOC })
    await nextTick()
    expect(document.querySelector('[data-testid="kb-chunk-feishu"]')).toBeNull()
    w.unmount()
  })

  it('关闭钮 emit close（×/Esc/遮罩通道由 ThreadModal 壳承担）', async () => {
    const w = mountModal({ open: true, chunk: CHUNK, doc: DOC })
    await nextTick()
    document.querySelector<HTMLButtonElement>('[data-testid="kb-chunk-close"]')!.click()
    await nextTick()
    expect(w.emitted('close')).toHaveLength(1)
    w.unmount()
  })
})

describe('KbChunkDrilldown — 404001 降级占位卡（不报错红、不 toast、占位卡即弹窗体）', () => {
  afterEach(() => {
    i18n.global.locale.value = 'zh-CN'
  })

  it('unavailable=true：占位卡渲染 kbChunkUnavailable 文案；切片面不渲染；无 danger 红', async () => {
    const w = mountModal({ open: true, chunk: null, doc: null, unavailable: true })
    await nextTick()
    const panel = document.querySelector('[role="dialog"][aria-modal="true"]')!
    expect(panel.textContent).toContain('该引用对应的知识库内容已不可查看')
    expect(drill()).not.toBeNull()
    expect(document.querySelector('[data-testid="kb-chunk-content"]')).toBeNull()
    // 中性占位：非 danger 红系（badge-danger/status-reason 类不存在于面板）
    expect(panel.querySelector('.badge-danger')).toBeNull()
    expect(panel.querySelector('.status-reason')).toBeNull()
    w.unmount()
  })

  it('CR-2 en locale：kbChunkUnavailable 词条逐字对齐（占位卡英文形态防漂）', async () => {
    i18n.global.locale.value = 'en-US'
    const w = mountModal({ open: true, chunk: null, doc: null, unavailable: true })
    await nextTick()
    const panel = document.querySelector('[role="dialog"][aria-modal="true"]')!
    expect(panel.textContent).toContain('This source content is no longer available')
    // 旧文案逐字不回退
    expect(panel.textContent).not.toContain('no longer viewable')
    w.unmount()
  })
})

describe('KbChunkDrilldown — 焦点管理（SnapshotModal 双相位先例）', () => {
  it('open 记录先前焦点落内容体（tabindex=-1）；close 归还', async () => {
    const trigger = document.createElement('button')
    trigger.textContent = '溯源'
    document.body.appendChild(trigger)
    trigger.focus()
    expect(document.activeElement).toBe(trigger)

    const w = mountModal({ open: false, chunk: CHUNK, doc: DOC })
    await w.setProps({ open: true })
    await nextTick()
    const body = document.querySelector<HTMLElement>('[data-testid="kb-chunk-drilldown"]')!
    expect(document.activeElement).toBe(body)

    await w.setProps({ open: false })
    await nextTick()
    expect(document.activeElement).toBe(trigger)
    w.unmount()
  })
})
