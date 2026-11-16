/**
 * test/unit/turn-anchor.test.ts — turnIdFromAnchor（来源面板跟随态小批）。
 *
 * AnswerView 的 onScrollForAnchor 以锚点 id（`turn-{turnId}-q`，rebuildOutline 唯一构造点）
 * 维护 activeAnchorId；跟随态 prop（SourcePanel activeTurnId）由其派生：锚点形态解析出轮 id，
 * 非锚点形态/null 一律返回 null（跟随与组头高亮静默跳过）。锚点行为本身零变更（e2e A27 不回归）。
 */
import { describe, expect, it } from 'vitest'

import { turnIdFromAnchor } from '@/utils/anchors'

describe('turnIdFromAnchor（锚点 id → 轮 id 派生）', () => {
  it('turn-{turnId}-q 形态 → turnId（UUID 连字符原样保留）', () => {
    const uuid = '11111111-1111-4111-8111-111111111111'
    expect(turnIdFromAnchor(`turn-${uuid}-q`)).toBe(uuid)
  })

  it('null → null（无激活锚点态）', () => {
    expect(turnIdFromAnchor(null)).toBe(null)
  })

  it('非锚点形态 → null（不带 -q 后缀 / 前缀不符 / 轮段为空）', () => {
    expect(turnIdFromAnchor('turn-abc')).toBe(null)
    expect(turnIdFromAnchor('outline-item-1')).toBe(null)
    expect(turnIdFromAnchor('turn--q')).toBe(null)
    expect(turnIdFromAnchor('')).toBe(null)
  })
})
