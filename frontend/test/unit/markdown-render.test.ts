/**
 * test/unit/markdown-render.test.ts — AnswerRenderer 渲染纯逻辑（frontend-api.md §5 AnswerRenderer 行）。
 *
 * 锁死口径：
 *   - markdown-it + DOMPurify（白名单 + §2 http/https 协议白名单）+ highlight.js；
 *   - [n] 引用标注 → 可点击 sup（data-ref-no），与 sources 面板 ref_no 联动（REQ-003）；
 *   - TOC 锚点：标题 id 由渲染器生成（OutlineAnchor 数据源，frontend-api.md §5）；
 *   - thinking 渲染通道不解析 [n]、不产标题 id（§3 thinking 行：不参与 [n] 引用解析）。
 */
import { describe, expect, it } from 'vitest'

import {
  isSameHeadingSet,
  renderAnswerMarkdown,
  renderPlainMarkdown,
  SANITIZE_ALLOWED_TAGS,
} from '@/components/markdown/renderer'

describe('renderAnswerMarkdown（answer 通道）', () => {
  it('基础 markdown → 安全 HTML（标题/段落/列表/表格）', () => {
    const { html } = renderAnswerMarkdown('# 标题\n\n正文段落\n\n- 甲\n- 乙\n\n| A | B |\n|---|---|\n| 1 | 2 |')
    expect(html).toContain('<h1')
    expect(html).toContain('正文段落')
    expect(html).toContain('<li>甲</li>')
    expect(html).toContain('<table>')
  })

  it('标题 id：headingPrefix + 序号（+ASCII slug）生成，中文纯标题无 ASCII 时仅前缀+序号（OutlineAnchor 数据源）', () => {
    const { html, headings } = renderAnswerMarkdown('## 分层关系\n\n正文\n\n## How to choose\n\n正文', {
      headingPrefix: 't1',
    })
    expect(headings).toHaveLength(2)
    expect(headings[0]).toMatchObject({ id: 't1-h-0', text: '分层关系' })
    expect(headings[1]?.id).toBe('t1-h-1-how-to-choose')
    expect(html).toContain(`id="${headings[0]?.id}"`)
    expect(html).toContain(`id="${headings[1]?.id}"`)
  })

  it('[n] 引用标注 → sup.cite[data-ref-no]（与 sources ref_no 联动；属性顺序无关断言）', () => {
    const { html } = renderAnswerMarkdown('动态扇出 [1] 与静态分支 [12] 组合。')
    expect((html.match(/<sup class="cite"[^>]*>\[1\]<\/sup>/g) ?? []).join('')).toMatch(
      /class="cite"/,
    )
    expect(html).toContain('data-ref-no="1"')
    expect(html).toContain('data-ref-no="12"')
    expect(html).toContain('tabindex="0"')
    expect(html).toContain('role="button"')
    expect(html).toContain('[1]</sup>')
    expect(html).toContain('[12]</sup>')
  })

  it('markdown 链接语法中的 [n] 不误伤（link_open/close 之间的文本不二次处理）', () => {
    const { html } = renderAnswerMarkdown('[1](https://example.com) 与 [文档](https://example.com/doc)')
    expect(html).toContain('href="https://example.com"')
    // 行内链接文本/URL 不产生 sup.cite
    expect(html).not.toContain('data-ref-no')
  })

  it('代码块：highlight.js 高亮（语言命中产出 hljs token span）', () => {
    const { html } = renderAnswerMarkdown('```ts\nconst a: number = 1\n```')
    expect(html).toContain('hljs-keyword')
  })

  it('代码块：未知语言不炸（回落转义文本）', () => {
    const { html } = renderAnswerMarkdown('```notalang\nconst a = 1\n```')
    expect(html).toContain('const a = 1')
  })

  it('XSS 防线：原始 HTML 被转义为文本（html:false 第一道防线），不可执行', () => {
    const { html } = renderAnswerMarkdown('<script>alert(1)</script>正文<img src=x onerror=alert(2)>')
    expect(html).toContain('&lt;script&gt;')
    expect(html).not.toContain('<script')
    expect(html).not.toContain('<img')
  })

  it('协议白名单（§2 ^https?://）：javascript:/data:text/html 被 markdown-it validateLink 拒渲染为链接（降级纯文本）', () => {
    const { html } = renderAnswerMarkdown(
      '[点我](javascript:alert(1)) 与 ![图](data:text/html;base64,xxx) 与 [好链](https://example.com)',
    )
    expect(html).not.toContain('href="javascript:')
    expect(html).not.toContain('<img')
    expect(html).toContain('href="https://example.com"')
  })

  it('协议白名单第二层：data:image/png 通过 markdown-it 但被 DOMPurify ^https?:// 剥除 href', () => {
    const { html } = renderAnswerMarkdown('[bad](data:image/png;base64,aaa)')
    expect(html).not.toContain('href="data:')
    expect(html).not.toContain('<a ')
  })

  it('正文内相对锚链接按协议白名单剥除 href（M1 口径从严；标题 id 保留）', () => {
    const { html } = renderAnswerMarkdown('## 章节\n\n[跳转](#section)')
    expect(html).toContain('id=')
    expect(html).not.toContain('href="#')
  })

  it('外链统一 rel="noopener noreferrer"（DOMPurify afterSanitizeAttributes hook，模块级注册一次）', () => {
    const { html } = renderAnswerMarkdown('[外链一](https://example.com/a) 与 [外链二](https://example.com/b)')
    expect((html.match(/rel="noopener noreferrer"/g) ?? []).length).toBe(2)
  })

  it('thinking 通道同样补 rel（同净化底线共用 hook）', () => {
    const { html } = renderPlainMarkdown('[链接](https://example.com)')
    expect(html).toContain('rel="noopener noreferrer"')
  })

  it('ALLOWED_TAGS 显式收敛：文本级标签白名单（markdown 输出无 svg/mathml 需求，剥除）', () => {
    expect(SANITIZE_ALLOWED_TAGS).toContain('a')
    expect(SANITIZE_ALLOWED_TAGS).toContain('sup')
    expect(SANITIZE_ALLOWED_TAGS).toContain('table')
    expect(SANITIZE_ALLOWED_TAGS).toContain('img')
    expect(SANITIZE_ALLOWED_TAGS).not.toContain('svg')
    expect(SANITIZE_ALLOWED_TAGS).not.toContain('math')
    expect(SANITIZE_ALLOWED_TAGS).not.toContain('script')
    expect(SANITIZE_ALLOWED_TAGS).not.toContain('iframe')
    expect(SANITIZE_ALLOWED_TAGS).not.toContain('form')
    expect(SANITIZE_ALLOWED_TAGS).not.toContain('style')
  })
})

describe('isSameHeadingSet（AnswerView outline 重建节流依据：仅标题集变化才重建）', () => {
  it('同集（同 id 同 text）相等；顺序敏感；空集相等；任一差异不等', () => {
    expect(isSameHeadingSet([{ id: 'a', text: 'A' }], [{ id: 'a', text: 'A' }])).toBe(true)
    expect(isSameHeadingSet([], [])).toBe(true)
    expect(isSameHeadingSet([{ id: 'a', text: 'A' }, { id: 'b', text: 'B' }], [{ id: 'a', text: 'A' }, { id: 'b', text: 'B' }])).toBe(true)
    expect(isSameHeadingSet([{ id: 'a', text: 'A' }, { id: 'b', text: 'B' }], [{ id: 'b', text: 'B' }, { id: 'a', text: 'A' }])).toBe(false)
    expect(isSameHeadingSet([{ id: 'a', text: 'A' }], [{ id: 'a', text: 'A2' }])).toBe(false)
    expect(isSameHeadingSet([{ id: 'a', text: 'A' }], [{ id: 'a2', text: 'A' }])).toBe(false)
    expect(isSameHeadingSet([{ id: 'a', text: 'A' }], [])).toBe(false)
  })
})

describe('renderPlainMarkdown（thinking 通道）', () => {
  it('不解析 [n]、不产标题 id（§3：thinking 不参与 [n] 引用解析）', () => {
    const { html, headings } = renderPlainMarkdown('## 思考点\n\n先看 [1] 再看 [2]')
    expect(headings).toHaveLength(0)
    expect(html).not.toContain('data-ref-no')
    expect(html).not.toContain('<sup')
    expect(html).toContain('[1]')
    expect(html).toContain('[2]')
  })

  it('同样过 DOMPurify 白名单', () => {
    const { html } = renderPlainMarkdown('<script>alert(1)</script>思考内容')
    expect(html).not.toContain('<script')
    expect(html).toContain('思考内容')
  })
})
