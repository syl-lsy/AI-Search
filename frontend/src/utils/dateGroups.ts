/**
 * src/utils/dateGroups.ts — 逐日分组纯函数（frontend-api.md §5 v1.21 / D-18：
 * 今天 / 昨天 / 本年 M月D日 / 往年 YYYY年M月D日；en-US：Today / Yesterday / MMM D /
 * MMM D, YYYY）。分组键 = updated_at 的前端本地时区日期；today/yesterday 走 i18n 词条
 * （词条由调用方注入，保持本函数零 i18n 依赖、可独立单测），其余走 Intl.DateTimeFormat
 * 本地化模板。取代旧「今天/最近 7 天/更早」三桶。
 */
import type { ThreadListItem } from '@/types/api'

export interface ThreadDateGroup {
  /** 分组键 = 本地时区日期 YYYY-MM-DD（稳定 :key） */
  key: string
  /** 组标题（today/yesterday 由调用方传入词条；其余为本地化日期文本） */
  label: string
  items: ThreadListItem[]
}

/** 本地时区日期键（YYYY-MM-DD；epoch 无效回退 'invalid' 兜底桶，置于末尾） */
function localDateKey(date: Date): string {
  const y = date.getFullYear()
  const m = String(date.getMonth() + 1).padStart(2, '0')
  const d = String(date.getDate()).padStart(2, '0')
  return `${y}-${m}-${d}`
}

export interface GroupThreadsByDayOptions {
  /** 今天/昨天词条（i18n threads.group_today / threads.group_yesterday） */
  todayLabel: string
  yesterdayLabel: string
  /** Intl tag（'zh-CN' | 'en-US'——跟随界面 locale，OQ-5 解耦口径） */
  locale: string
}

export function groupThreadsByDay(
  items: ThreadListItem[],
  now: Date,
  options: GroupThreadsByDayOptions,
): ThreadDateGroup[] {
  const todayKey = localDateKey(now)
  const yesterday = new Date(now)
  yesterday.setDate(yesterday.getDate() - 1)
  const yesterdayKey = localDateKey(yesterday)
  const nowYear = now.getFullYear()

  // 本地化模板（D-18）：zh=「11月1日」/「2025年11月1日」；en=「Nov 1」/「Nov 1, 2025」
  const isZh = options.locale.toLowerCase().startsWith('zh')
  const thisYearFormatter = new Intl.DateTimeFormat(
    options.locale,
    isZh ? { month: 'long', day: 'numeric' } : { month: 'short', day: 'numeric' },
  )
  const yearFormatter = new Intl.DateTimeFormat(options.locale, { dateStyle: 'medium' })

  const buckets = new Map<string, { label: string; items: ThreadListItem[] }>()

  for (const item of items) {
    const date = new Date(item.updated_at)
    let key: string
    let label: string
    if (!Number.isFinite(date.getTime())) {
      // 无效时间戳兜底桶（不参与 today/yesterday 判定），置末尾
      key = 'invalid'
      label = ''
    } else {
      key = localDateKey(date)
      if (key === todayKey) {
        label = options.todayLabel
      } else if (key === yesterdayKey) {
        label = options.yesterdayLabel
      } else if (date.getFullYear() === nowYear) {
        label = thisYearFormatter.format(date)
      } else {
        label = yearFormatter.format(date)
      }
    }
    const bucket = buckets.get(key)
    if (bucket) bucket.items.push(item)
    else buckets.set(key, { label, items: [item] })
  }

  // 组序：时间倒序（最新日期在前）；invalid 兜底桶恒末尾；组内保持服务端排序原序
  return [...buckets.entries()]
    .sort(([a], [b]) => {
      if (a === 'invalid') return 1
      if (b === 'invalid') return -1
      return a < b ? 1 : a > b ? -1 : 0
    })
    .map(([key, bucket]) => ({ key, label: bucket.label, items: bucket.items }))
}
