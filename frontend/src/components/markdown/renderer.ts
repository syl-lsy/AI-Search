/**
 * src/components/markdown/renderer.ts — AnswerRenderer 渲染纯逻辑（frontend-api.md §5 AnswerRenderer 行）。
 *
 * 通道分立：
 *   - renderAnswerMarkdown：answer_md 通道——markdown-it + DOMPurify（白名单 + §2 http/https
 *     协议白名单）+ highlight.js；[n] 引用标注 → 可点击 sup.cite（data-ref-no，与 sources
 *     ref_no 联动，REQ-003）；标题 id 由渲染器生成（OutlineAnchor 数据源，SSE/API 协议零变更）。
 *   - renderPlainMarkdown：thinking_md 通道——同一净化底线，但不解析 [n]（§3 thinking 行：
 *     不参与 [n] 引用解析）、不产标题 id。
 *
 * 安全：markdown-it html:false（原始 HTML 不进 token）+ DOMPurify 双层；图片/链接协议
 * 白名单 ^https?://（frontend-api.md §2，从严——页内锚 href 一并剥除，标题 id 不依赖链接）。
 */
import DOMPurify from 'dompurify'
import hljs from 'highlight.js/lib/common'
import MarkdownIt from 'markdown-it'
import type { MarkdownIt as MarkdownItType, StateCore, Token } from 'markdown-it'

export interface HeadingInfo {
  id: string
  text: string
}

export interface RenderedMarkdown {
  html: string
  headings: HeadingInfo[]
}

export interface RenderOptions {
  /** 标题 id 命名空间（多轮同页去重；如 turn id 短前缀） */
  headingPrefix?: string
}

/**
 * 净化白名单显式收敛（markdown 输出源无 svg/mathml 需求，剥除——评审留档 P3）：
 * 覆盖 CommonMark + GFM 在 html:false 下 markdown-it 可产出的全部标签（块级 p/h1-h6/
 * blockquote/pre/ul/ol/li/table 族/hr + 行内 a/img/br/strong/em/del/s/code/sup），
 * 外加少量无害文本级标签（mark/q/sub/abbr/kbd/small/span/u）作前向余量。
 * ALLOWED_ATTR 相应核对：维持 DOMPurify 默认（href/src/alt/title/class/role 等）+
 * ADD_ATTR——本渲染源的属性面即上述集合；rel 由 afterSanitizeAttributes hook 在净化后写入，
 * 不经白名单通道。
 */
export const SANITIZE_ALLOWED_TAGS: string[] = [
  'a', 'abbr', 'blockquote', 'br', 'code', 'del', 'em', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'hr', 'img', 'kbd', 'li', 'mark', 'ol', 'p', 'pre', 'q', 's', 'small', 'span', 'strong',
  'sub', 'sup', 'table', 'tbody', 'td', 'tfoot', 'th', 'thead', 'tr', 'u', 'ul',
]

const SANITIZE_OPTIONS = {
  // 评审留档 P3：显式 ALLOWED_TAGS 收敛（见 SANITIZE_ALLOWED_TAGS 注释）
  ALLOWED_TAGS: SANITIZE_ALLOWED_TAGS,
  // frontend-api.md §2：图片/链接协议白名单 ^https?://——替换默认 URI 正则后
  // javascript:/data:/相对协议/页内锚 href 全部剥除（DOMPurify 剥 URI 属性）
  ALLOWED_URI_REGEXP: /^https?:\/\//i,
  // [n] 角标所需的自定义属性（id 由净化后注入阶段写入）
  ADD_ATTR: ['data-ref-no', 'tabindex'],
}

/**
 * 外链加固 hook（评审留档 P2）：所有 a 标签统一 rel="noopener noreferrer"——
 * DOMPurify hook 全局注册，模块初始化时一次（布尔闸防 HMR 重复注册）；hook 在该节点
 * 净化完成后执行，写入的属性不再回流白名单，无注入面。
 */
let linkHardeningInstalled = false
function installLinkHardening(): void {
  if (linkHardeningInstalled) return
  linkHardeningInstalled = true
  DOMPurify.addHook('afterSanitizeAttributes', (node) => {
    // 仅带 href 的 a（超链接本体）补 rel——href 被协议白名单剥除的残壳 a 非超链，rel 无意义
    if (node instanceof Element && node.tagName === 'A' && node.hasAttribute('href')) {
      node.setAttribute('rel', 'noopener noreferrer')
    }
  })
}
installLinkHardening()

const CITE_PATTERN = /\[(\d{1,2})\]/
const CITE_SPLIT_PATTERN = /(\[\d{1,2}\])/

function makeMarkdownIt(withCites: boolean): MarkdownItType {
  const mdit: MarkdownItType = new MarkdownIt({
    html: false, // 原始 HTML 不进 token（DOMPurify 之外的第一个防线）
    linkify: false,
    breaks: false,
    highlight(code: string, lang: string): string {
      if (lang && hljs.getLanguage(lang)) {
        try {
          return hljs.highlight(code, { language: lang, ignoreIllegals: true }).value
        } catch {
          // 高亮失败回落 markdown-it 默认转义
        }
      }
      return '' // 非字符串返回值语义 = 「未处理」→ markdown-it 走默认 escapeHtml
    },
  })

  if (withCites) {
    // [n] → 可点击角标：仅处理纯文本 token；markdown 链接语法已被 inline 切词拆为
    // link_open/text/link_close 结构，链接文案中的 [n] 落在 link 边界内——为免误伤，
    // 仅对「游离于链接结构之外」的 text token 替换（split 后逐段判定，命中段替换为
    // html_inline，其余段原样保留）。
    mdit.core.ruler.after('inline', 'cite_badges', (state: StateCore) => {
      for (const token of state.tokens) {
        if (token.type !== 'inline' || !token.children) continue
        if (!token.children.some((c: Token) => c.type === 'text' && CITE_PATTERN.test(c.content))) continue
        const children: Token[] = []
        for (const child of token.children) {
          if (child.type === 'text' && CITE_PATTERN.test(child.content)) {
            for (const part of child.content.split(CITE_SPLIT_PATTERN)) {
              const m = part.match(/^\[(\d{1,2})\]$/)
              if (m) {
                const sup = new state.Token('html_inline', '', 0)
                sup.content =
                  `<sup class="cite" data-ref-no="${m[1]}" tabindex="0" role="button">[${m[1]}]</sup>`
                children.push(sup)
              } else if (part) {
                const text = new state.Token('text', '', 0)
                text.content = part
                children.push(text)
              }
            }
          } else {
            children.push(child)
          }
        }
        token.children = children
      }
    })
  }

  return mdit
}

const answerMd = makeMarkdownIt(true)
const plainMd = makeMarkdownIt(false)

/** 标题 slug：仅保留 ASCII 字母数字（CJK 无 ASCII 时返回空串，id 由序号保唯一） */
function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 32)
}

interface HeadingHost {
  html: string
  headings: HeadingInfo[]
}

/**
 * 净化后处理（临时 DOM 不挂载文档）：
 *   1. 标题 id 注入（OutlineAnchor 数据源；只加 id 属性，不引入新内容）；
 *   2. sup.cite 补 tabindex="0"——DOMPurify 3.4.x 在自定义 ALLOWED_URI_REGEXP 时连带丢弃
 *      ADD_ATTR 中的 tabindex（隔离+组合双探针复现）；该属性值为本模块自产常量，补写无注入面。
 */
function postProcessSanitized(sanitizedHtml: string, prefix: string): HeadingHost {
  if (typeof document === 'undefined') return { html: sanitizedHtml, headings: [] }
  const host = document.createElement('div')
  host.innerHTML = sanitizedHtml
  const headings: HeadingInfo[] = []
  let index = 0
  for (const el of Array.from(host.querySelectorAll('h1,h2,h3,h4,h5,h6'))) {
    const text = (el.textContent ?? '').trim()
    const slug = slugify(text)
    const id = slug ? `${prefix}-h-${index}-${slug}` : `${prefix}-h-${index}`
    el.setAttribute('id', id)
    headings.push({ id, text })
    index += 1
  }
  for (const el of Array.from(host.querySelectorAll('sup.cite[data-ref-no]'))) {
    el.setAttribute('tabindex', '0')
  }
  const html = host.innerHTML
  host.remove()
  return { html, headings }
}

/**
 * 标题集相等判定（AnswerView outline 重建节流依据）：id+text 逐项全等且顺序一致才视为
 * 同一集合——流式限频批之间标题未变时上抛值相同，消费面据此跳过 outlineItems 重建。
 */
export function isSameHeadingSet(a: HeadingInfo[], b: HeadingInfo[]): boolean {
  if (a.length !== b.length) return false
  return a.every((item, i) => item.id === b[i]?.id && item.text === b[i]?.text)
}

/** answer 通道：markdown-it 渲染 → DOMPurify 净化 → 标题 id 注入（OutlineAnchor 数据源） */
export function renderAnswerMarkdown(source: string, options: RenderOptions = {}): RenderedMarkdown {
  const prefix = options.headingPrefix ?? 'a'
  // DOMPurify 3.4 返回类型 string | TrustedHTML（TrustedTypes 环境）——统一归一为 string
  const sanitized = String(DOMPurify.sanitize(answerMd.render(source), SANITIZE_OPTIONS))
  return postProcessSanitized(sanitized, prefix)
}

/** thinking 通道：同净化底线；不解析 [n]、不产标题 id（§3 thinking 行口径） */
export function renderPlainMarkdown(source: string): RenderedMarkdown {
  const sanitized = String(DOMPurify.sanitize(plainMd.render(source), SANITIZE_OPTIONS))
  return { html: sanitized, headings: [] }
}
