/**
 * test/unit/thread-display.test.ts — 线程条目展示共享纯函数（v1.23：会话页行 / 侧栏预览卡
 * 共用的模式徽标与相对时间；词条注入零 i18n 依赖，dateGroups 同款约定）。
 */
import { describe, expect, it, vi } from 'vitest'

import { modeBadgeClass, modeBadgeText, relativeTime } from '@/utils/threadDisplay'

const zhT = (key: string): string =>
  ({ 'mode.fast': '快速', 'mode.deep': '深度研究', 'threads.modeMixed': '混合' })[key] ?? key

describe('modeBadgeClass / modeBadgeText（会话页与预览卡同口径）', () => {
  it('样式类：deep=badge-outline；fast/mixed=badge-accent', () => {
    expect(modeBadgeClass('deep')).toBe('badge-outline')
    expect(modeBadgeClass('fast')).toBe('badge-accent')
    expect(modeBadgeClass('mixed')).toBe('badge-accent')
  })

  it('文案：fast/deep/mixed 词条化（t 注入）', () => {
    expect(modeBadgeText('fast', zhT)).toBe('快速')
    expect(modeBadgeText('deep', zhT)).toBe('深度研究')
    expect(modeBadgeText('mixed', zhT)).toBe('混合')
  })
})

describe('relativeTime（分钟/小时/天三段；locale 字符串注入）', () => {
  it('1 小时内：分钟相对（zh-CN）', () => {
    vi.setSystemTime(new Date(2026, 10, 15, 12, 0, 0))
    expect(relativeTime(new Date(2026, 10, 15, 11, 50, 0).toISOString(), 'zh-CN')).toBe('10分钟前')
    vi.useRealTimers()
  })

  it('24 小时内：小时相对；更早：天相对', () => {
    vi.setSystemTime(new Date(2026, 10, 15, 12, 0, 0))
    expect(relativeTime(new Date(2026, 10, 15, 9, 0, 0).toISOString(), 'zh-CN')).toBe('3小时前')
    expect(relativeTime(new Date(2026, 10, 12, 12, 0, 0).toISOString(), 'zh-CN')).toBe('3天前')
    vi.useRealTimers()
  })

  it('en-US 标签跟随 locale 字符串', () => {
    vi.setSystemTime(new Date(2026, 10, 15, 12, 0, 0))
    expect(relativeTime(new Date(2026, 10, 15, 11, 50, 0).toISOString(), 'en-US')).toContain('minutes ago')
    vi.useRealTimers()
  })

  it('无效时间戳回空串（Number.isFinite 守卫）', () => {
    expect(relativeTime('not-a-date', 'zh-CN')).toBe('')
  })
})
