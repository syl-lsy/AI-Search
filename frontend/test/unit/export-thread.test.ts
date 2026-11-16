/**
 * test/unit/export-thread.test.ts — exportThread.ts 四函数直接单测（批次3评审-F2/F4）。
 *
 * 判定点：
 *   sanitizeFilename —— 非法字符集→空格 / 80 截断 / fallback 回落 / Windows 保留名
 *   （CON PRN AUX NUL COM1-9 LPT1-9，大小写不敏感加 "_" 后缀）/ 控制字符剥除（\r\n 除外，
 *   Unicode Cc：C0 其余 + DEL + C1）/ U+202E RTL 覆写符剥除（批次3评审-F4 消毒补强）；
 *   buildThreadMarkdown —— 正常（标题 + 问答 + 来源区，ref_no 排序 + kb 无 url 形态）/
 *   空 turns / 无标题线程回退（批次3评审-F2）；
 *   buildThreadLink —— origin + /thread/{threadId}；
 *   downloadMarkdown —— Blob（text/markdown;charset=utf-8）+ a.download + 同出同收
 *   （URL.revokeObjectURL 调用）。
 *
 * 纯函数零网络；downloadMarkdown 走 jsdom Blob/anchor 真装配（URL.* 与 click 桩）。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Source } from '@/types/api'
import { buildThreadLink, buildThreadMarkdown, downloadMarkdown, sanitizeFilename } from '@/utils/exportThread'
import type { ExportableTurn } from '@/utils/exportThread'

function src(refNo: number, url: string | null, title: string): Source {
  return {
    source_id: `00000000-0000-4000-8000-00000000000${refNo}`,
    ref_no: refNo,
    source_type: url === null ? 'kb' : 'web',
    title,
    url,
    snippet: '',
    score: 0,
    snapshot_available: false,
    kb_doc_id: null,
  }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('sanitizeFilename（文件名消毒：批次3评审-F2/F4）', () => {
  it('非法字符集（\\ / : * ? " < > |）替换为空格（词间分隔保持）', () => {
    expect(sanitizeFilename('a/b\\c:d*e?f"g<h>i|j', 'x')).toBe('a b c d e f g h i j')
  })

  it('换行（\\r\\n\\n）替换为空格（原行为不变）', () => {
    expect(sanitizeFilename('a\r\nb\nc', 'x')).toBe('a b c')
  })

  it('超长截断到 80 字符', () => {
    expect(sanitizeFilename('x'.repeat(100), 'x')).toBe('x'.repeat(80))
    expect(sanitizeFilename('一'.repeat(100), 'x')).toHaveLength(80)
  })

  it('空值/纯空白/纯非法字符回落 fallback', () => {
    expect(sanitizeFilename('', 'thread')).toBe('thread')
    expect(sanitizeFilename('   ', 'answer')).toBe('answer')
    expect(sanitizeFilename('///', 'thread')).toBe('thread')
  })

  it('Windows 保留名大小写不敏感加 "_" 后缀（CON PRN AUX NUL COM1-9 LPT1-9）', () => {
    expect(sanitizeFilename('con', 'x')).toBe('con_')
    expect(sanitizeFilename('CON', 'x')).toBe('CON_')
    expect(sanitizeFilename('Prn', 'x')).toBe('Prn_')
    expect(sanitizeFilename('aux', 'x')).toBe('aux_')
    expect(sanitizeFilename('NUL', 'x')).toBe('NUL_')
    expect(sanitizeFilename('com1', 'x')).toBe('com1_')
    expect(sanitizeFilename('Com9', 'x')).toBe('Com9_')
    expect(sanitizeFilename('lpt1', 'x')).toBe('lpt1_')
    expect(sanitizeFilename('LPT9', 'x')).toBe('LPT9_')
  })

  it('非裸名保留字不误伤（console/com10/computer/含保留字的短语）', () => {
    expect(sanitizeFilename('console', 'x')).toBe('console')
    expect(sanitizeFilename('com10', 'x')).toBe('com10')
    expect(sanitizeFilename('computer', 'x')).toBe('computer')
    expect(sanitizeFilename('my con', 'x')).toBe('my con')
  })

  it('控制字符剥除（\\r\\n 除外；C0 其余/TAB/DEL/C1），U+202E RTL 覆写符剥除（批次3评审-F4）', () => {
    // C0 其余 + TAB：NUL/BEL/TAB 直接剥除
    expect(sanitizeFilename('a\u0000b\u0007c\td', 'x')).toBe('abcd')
    // DEL + C1（如 U+0085 NEL）
    expect(sanitizeFilename('a\u007Fb\u0085c', 'x')).toBe('abc')
    // U+202E RTL 覆写符剥除（防扩展名方向视觉伪装）
    expect(sanitizeFilename('\u202Efdp.exe', 'x')).toBe('fdp.exe')
    expect(sanitizeFilename('report\u202Egnp', 'x')).toBe('reportgnp')
    // 纯控制字符 → 剥除后为空 → fallback
    expect(sanitizeFilename(' \u0007\t ', 'x')).toBe('x')
  })
})

describe('buildThreadMarkdown（整线程 Markdown 组装）', () => {
  it('正常：# 标题 + 每轮 ## 问：{question} + answer_md + 来源区（ref_no 升序；kb 无 url 仅 [n] 标题）', () => {
    const turns: ExportableTurn[] = [
      {
        question: '什么是 SSE 续传？',
        answer_md: '按 Last-Event-ID 续传。',
        sources: [src(2, 'https://b.example/2', 'B 文档'), src(1, 'https://a.example/1', 'A 文档')],
      },
      { question: '第二问', answer_md: '第二答', sources: [] },
    ]
    const md = buildThreadMarkdown('我的会话', turns)
    const lines = md.split('\n')
    expect(lines[0]).toBe('# 我的会话')
    expect(md).toContain('## 问：什么是 SSE 续传？')
    expect(md).toContain('按 Last-Event-ID 续传。')
    // 来源区 ref_no 升序（输入乱序 2→1）；kb 无 url 仅 [n] 标题
    const sourceLines = lines.filter((l) => /^\[\d+\]/.test(l))
    expect(sourceLines).toEqual([
      '[1] https://a.example/1 A 文档',
      '[2] https://b.example/2 B 文档',
    ])
    // 无来源轮整块省略来源区
    expect(md).toContain('## 问：第二问')
    expect(md).toContain('第二答')
    expect(md.indexOf('第二答')).toBeGreaterThan(md.indexOf('[2] https://b.example/2'))
  })

  it('空 turns：仅输出标题行', () => {
    expect(buildThreadMarkdown('只有标题', [])).toBe('# 只有标题')
  })

  it('无标题线程回退（批次3评审-F2）：空/纯空白标题 → 「未命名会话」', () => {
    const turns: ExportableTurn[] = [{ question: 'Q', answer_md: 'A', sources: [] }]
    expect(buildThreadMarkdown('', turns).startsWith('# 未命名会话')).toBe(true)
    expect(buildThreadMarkdown('   ', turns).startsWith('# 未命名会话')).toBe(true)
  })
})

describe('buildThreadLink（会话链接）', () => {
  it('origin + /thread/{threadId}', () => {
    expect(buildThreadLink('abc-123')).toBe(`${window.location.origin}/thread/abc-123`)
  })
})

describe('downloadMarkdown（Blob 下载装配）', () => {
  function stubUrlAndAnchor(): { createObjectURL: ReturnType<typeof vi.fn>; revokeObjectURL: ReturnType<typeof vi.fn>; clicked: { current: Partial<HTMLAnchorElement> | null } } {
    const createObjectURL = vi.fn<(blob: Blob) => string>(() => 'blob:mock-url')
    const revokeObjectURL = vi.fn<(url: string) => void>()
    Object.defineProperty(URL, 'createObjectURL', { value: createObjectURL, configurable: true, writable: true })
    Object.defineProperty(URL, 'revokeObjectURL', { value: revokeObjectURL, configurable: true, writable: true })
    const clicked: { current: Partial<HTMLAnchorElement> | null } = { current: null }
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      clicked.current = this
    })
    return { createObjectURL, revokeObjectURL, clicked }
  }

  it('Blob（text/markdown;charset=utf-8）+ a.download 同出同收（URL.revokeObjectURL 调用）', async () => {
    const { createObjectURL, revokeObjectURL, clicked } = stubUrlAndAnchor()
    downloadMarkdown('会话.md', '# 内容')
    expect(createObjectURL).toHaveBeenCalledTimes(1)
    const blob = createObjectURL.mock.calls[0]?.[0] as Blob
    expect(blob).toBeInstanceOf(Blob)
    expect(blob.type).toBe('text/markdown;charset=utf-8')
    expect(await blob.text()).toBe('# 内容')
    expect(clicked.current?.download).toBe('会话.md')
    expect(clicked.current?.href).toBe('blob:mock-url')
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock-url')
  })
})
