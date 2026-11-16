/**
 * src/utils/exportThread.ts — 会话导出/分享共享工具（前端本地、零网络）。
 *
 * 抽取自 AnswerActions 现有导出实现（原单答案行为保持不变），供两处复用：
 *   - AnswerActions 更多操作菜单「导出 Markdown」= 单答案 .md（内容 = answer_md）；
 *   - App 顶栏「导出 Markdown」= 整线程 .md（# 线程标题 + 每轮 ## 问：{question} +
 *     answer_md + 来源区（[n] url 标题列表，无来源轮省略））。
 * 分享 = 复制会话链接（origin + /thread/{threadId}；自用无公开分享后端——已定案，
 * 偏离 Perplexity 公开链接形态），AnswerActions 与 App 顶栏同款接线。
 */
import type { Source } from '@/types/api'

/** 导出投影（TurnState 字段子集；单答案与整线程共用） */
export interface ExportableTurn {
  question: string
  answer_md: string
  sources: Source[]
}

const EXPORT_FILENAME_MAX = 80

/**
 * Windows 保留设备名（大小写不敏感，裸名精确命中）：CON / PRN / AUX / NUL / COM1-9 /
 * LPT1-9。命中即追加 "_" 逃离保留语义（批次3评审-F2/F4）。
 */
const WINDOWS_RESERVED_NAMES = /^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])$/i

/**
 * 文件名消毒（批次3评审-F2/F4 补强）：
 *   ① 文件系统非法字符与换行（\\ / : * ? " < > | \r \n）→ 空格（词间分隔保持，原行为）；
 *   ② 控制字符剥除（Unicode Cc：C0 其余含 TAB、DEL、C1）+ U+202E RTL 覆写符剥除——
 *      控制字符可致下载/文件系统行为异常，RTL 覆写符可视觉伪装扩展名方向；
 *   ③ trim → 空值回落 fallback（单答案 'answer' / 整线程 'thread' 由调用方指定）→ 80 截断；
 *   ④ Windows 保留名（大小写不敏感裸名）追加 "_"。
 */
export function sanitizeFilename(name: string, fallback: string): string {
  const cleaned = name
    .replace(/[\\/:*?"<>|\r\n]+/g, ' ')
    .replace(/[\u0000-\u0009\u000B\u000C\u000E-\u001F\u007F-\u009F\u202E]/g, '')
    .trim()
  const base = (cleaned || fallback).slice(0, EXPORT_FILENAME_MAX)
  return WINDOWS_RESERVED_NAMES.test(base) ? `${base}_` : base
}

/** 会话链接 = 当前页面 origin + /thread/{threadId}（调用方负责 threadId 合法性守卫） */
export function buildThreadLink(threadId: string): string {
  return `${window.location.origin}/thread/${threadId}`
}

/** 无标题线程的导出标题回落（纯工具零 i18n；导出正文为文档内容，与界面语言解耦） */
const UNTITLED_THREAD_TITLE = '未命名会话'

/**
 * 整线程 Markdown 组装：`# {线程标题}`（空/纯空白标题回落「未命名会话」）+ 每轮
 * `## 问：{question}` + answer_md + 来源区（[n] url 标题，kb 来源无 url 时仅 [n] 标题；
 * 无来源轮整块省略）。
 */
export function buildThreadMarkdown(title: string, turns: readonly ExportableTurn[]): string {
  const lines: string[] = [`# ${title.trim() || UNTITLED_THREAD_TITLE}`]
  for (const turn of turns) {
    lines.push('', `## 问：${turn.question}`, '')
    if (turn.answer_md) lines.push(turn.answer_md, '')
    const sources = [...turn.sources].sort((a, b) => a.ref_no - b.ref_no)
    if (sources.length > 0) {
      for (const s of sources) {
        lines.push(s.url ? `[${s.ref_no}] ${s.url} ${s.title}` : `[${s.ref_no}] ${s.title}`)
      }
      lines.push('')
    }
  }
  return lines.join('\n')
}

/** Markdown Blob（text/markdown;charset=utf-8）→ a.download 触发浏览器下载（同出同收） */
export function downloadMarkdown(filename: string, content: string): void {
  const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}
