import { describe, expect, it } from 'vitest'

import {
  isAppLocale,
  isChatModelKey,
  isOnConflictStrategy,
  isRecord,
  isUuid,
  parseStoredFlag,
} from '@/utils/guards'

describe('guards（§8.9③ 无类型边界守卫）', () => {
  it('isRecord：对象形态判定', () => {
    expect(isRecord({ a: 1 })).toBe(true)
    expect(isRecord(null)).toBe(false)
    expect(isRecord([1])).toBe(false)
    expect(isRecord('x')).toBe(false)
  })

  it('isAppLocale：仅接受 zh-CN/en-US', () => {
    expect(isAppLocale('zh-CN')).toBe(true)
    expect(isAppLocale('en-US')).toBe(true)
    expect(isAppLocale('fr-FR')).toBe(false)
    expect(isAppLocale(null)).toBe(false)
    expect(isAppLocale(42)).toBe(false)
  })

  it('isChatModelKey：仅接受 qwen/glm/deepseek（api.md §2.1 v1.7 枚举）', () => {
    expect(isChatModelKey('qwen')).toBe(true)
    expect(isChatModelKey('glm')).toBe(true)
    expect(isChatModelKey('deepseek')).toBe(true)
    expect(isChatModelKey('gpt')).toBe(false)
    expect(isChatModelKey('')).toBe(false)
  })

  it('isOnConflictStrategy：四策略枚举（api.md §0 v1.4）', () => {
    for (const s of ['reject', 'enqueue', 'interrupt', 'rollback'] as const) {
      expect(isOnConflictStrategy(s)).toBe(true)
    }
    expect(isOnConflictStrategy('fallback')).toBe(false)
    expect(isOnConflictStrategy(undefined)).toBe(false)
  })

  it('parseStoredFlag：仅接受字符串 true/false', () => {
    expect(parseStoredFlag('true')).toBe(true)
    expect(parseStoredFlag('false')).toBe(false)
    expect(parseStoredFlag('1')).toBeNull()
    expect(parseStoredFlag(null)).toBeNull()
  })

  it('isUuid：UUID v4（api.md §0；§8.3 threadId 进 API 前校验）', () => {
    expect(isUuid('7d2f0a1e-1c3b-4e5a-9d8f-0a1b2c3d4e5f')).toBe(true)
    expect(isUuid('c31b9d74-2a41-4f6e-b3c2-5d6e7f809a1b')).toBe(true)
    expect(isUuid('7d2f0a1e-1c3b-9e5a-9d8f-0a1b2c3d4e5f')).toBe(false) // 非 v4 版本位
    expect(isUuid('not-a-uuid')).toBe(false)
    expect(isUuid(123)).toBe(false)
  })
})
