/**
 * test/unit/date-groups.test.ts — 逐日分组纯函数（frontend-api.md §5 v1.21 / D-18：
 * 今天/昨天/本年 M月D日/往年 YYYY年M月D日；时钟注入 mock——§3.2 F-3）。
 */
import { describe, expect, it } from 'vitest'

import { groupThreadsByDay } from '@/utils/dateGroups'
import type { ThreadListItem } from '@/types/api'

function item(id: string, updatedAt: string): ThreadListItem {
  return {
    thread_id: id,
    title: `问题 ${id}`,
    mode_summary: 'fast',
    turn_count: 1,
    pinned: false,
    is_unread: false,
    archived: false,
    created_at: updatedAt,
    updated_at: updatedAt,
  }
}

/** 固定「今天」= 2026-11-01 本地 12:00（分组键 = 本地时区日期） */
const NOW = new Date(2026, 10, 1, 12, 0, 0)

describe('groupThreadsByDay（四形态 + 组序 + 兜底）', () => {
  it('zh-CN：今天/昨天/本年（11月1日形态）/往年（2025年11月1日形态）四组', () => {
    const groups = groupThreadsByDay(
      [
        item('today', new Date(2026, 10, 1, 9, 0).toISOString()),
        item('yesterday', new Date(2026, 9, 31, 9, 0).toISOString()),
        item('this-year', new Date(2026, 4, 12, 9, 0).toISOString()),
        item('last-year', new Date(2025, 10, 1, 9, 0).toISOString()),
      ],
      NOW,
      { todayLabel: '今天', yesterdayLabel: '昨天', locale: 'zh-CN' },
    )
    expect(groups.map((g) => g.label)).toEqual(['今天', '昨天', '5月12日', '2025年11月1日'])
    expect(groups.map((g) => g.key)).toEqual(['2026-11-01', '2026-10-31', '2026-05-12', '2025-11-01'])
    expect(groups[0]!.items[0]!.thread_id).toBe('today')
  })

  it('en-US：Today / Yesterday / MMM D / MMM D, YYYY 本地化模板', () => {
    const groups = groupThreadsByDay(
      [
        item('today', new Date(2026, 10, 1, 9, 0).toISOString()),
        item('yesterday', new Date(2026, 9, 31, 9, 0).toISOString()),
        item('this-year', new Date(2026, 4, 12, 9, 0).toISOString()),
        item('last-year', new Date(2025, 10, 1, 9, 0).toISOString()),
      ],
      NOW,
      { todayLabel: 'Today', yesterdayLabel: 'Yesterday', locale: 'en-US' },
    )
    expect(groups.map((g) => g.label)).toEqual(['Today', 'Yesterday', 'May 12', 'Nov 1, 2025'])
  })

  it('组序 = 日期倒序（最新在前）；同组多条保持入参序（服务端排序原序）', () => {
    const groups = groupThreadsByDay(
      [
        item('older', new Date(2026, 9, 30, 9, 0).toISOString()),
        item('today-1', new Date(2026, 10, 1, 9, 0).toISOString()),
        item('today-2', new Date(2026, 10, 1, 8, 0).toISOString()),
        item('yesterday', new Date(2026, 9, 31, 9, 0).toISOString()),
      ],
      NOW,
      { todayLabel: '今天', yesterdayLabel: '昨天', locale: 'zh-CN' },
    )
    expect(groups.map((g) => g.label)).toEqual(['今天', '昨天', '10月30日'])
    expect(groups[0]!.items.map((x) => x.thread_id)).toEqual(['today-1', 'today-2'])
  })

  it('无效时间戳兜底桶（置末尾、label 空），不参与今天/昨天判定', () => {
    const groups = groupThreadsByDay(
      [item('bad', 'not-a-date'), item('today', new Date(2026, 10, 1, 9, 0).toISOString())],
      NOW,
      { todayLabel: '今天', yesterdayLabel: '昨天', locale: 'zh-CN' },
    )
    expect(groups.map((g) => g.key)).toEqual(['2026-11-01', 'invalid'])
    expect(groups[1]!.label).toBe('')
  })

  it('空列表 → 空分组数组', () => {
    expect(groupThreadsByDay([], NOW, { todayLabel: '今天', yesterdayLabel: '昨天', locale: 'zh-CN' })).toEqual([])
  })
})
