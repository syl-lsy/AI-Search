/**
 * src/utils/threadDisplay.ts — 线程条目展示共享纯函数（v1.23 抽取：会话页行 / 侧栏最近项
 * 预览卡两处共用模式徽标与相对时间；词条注入保持零 i18n 依赖可独立单测——dateGroups 同款约定）。
 */
import type { ThreadModeSummary } from '@/types/api'

/** 词条翻译函数形态（调用方注入 useI18n 的 t / i18n.global.t） */
export type Translate = (key: string) => string

/** 模式徽标样式类（deep=描边 / fast·mixed=强调淡底——会话页与预览卡同口径） */
export function modeBadgeClass(mode: ThreadModeSummary): string {
  return mode === 'deep' ? 'badge-outline' : 'badge-accent'
}

/** 模式徽标文案（fast/deep/mixed 词条化） */
export function modeBadgeText(mode: ThreadModeSummary, t: Translate): string {
  switch (mode) {
    case 'fast':
      return t('mode.fast')
    case 'deep':
      return t('mode.deep')
    case 'mixed':
      return t('threads.modeMixed')
  }
}

/** 相对时间（分钟/小时/天三段；locale 直接收字符串，OQ-5 与界面语言解耦口径不变） */
export function relativeTime(iso: string, locale: string): string {
  const then = new Date(iso).getTime()
  if (!Number.isFinite(then)) return ''
  const tag = locale === 'en-US' ? 'en' : 'zh-CN'
  const rtf = new Intl.RelativeTimeFormat(tag, { numeric: 'auto' })
  const minutes = Math.round((then - Date.now()) / 60_000)
  if (Math.abs(minutes) < 60) return rtf.format(minutes, 'minute')
  const hours = Math.round(minutes / 60)
  if (Math.abs(hours) < 24) return rtf.format(hours, 'hour')
  const days = Math.round(hours / 24)
  return rtf.format(days, 'day')
}
