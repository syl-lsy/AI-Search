/**
 * src/utils/anchors.ts — 锚点工具（来源面板跟随态小批）。
 *
 * AnswerView 的 onScrollForAnchor 以锚点 id（`turn-{turnId}-q`，rebuildOutline 唯一构造点）
 * 维护 activeAnchorId；来源面板跟随态 prop（activeTurnId）由其派生——非锚点形态一律返回
 * null（跟随与组头高亮静默跳过）。锚点行为本身零变更（e2e A27 断言不回归）。
 */

/** 锚点 id → 轮 id：`turn-{turnId}-q` 形态解析 turnId（UUID 连字符原样保留），其余返回 null */
export function turnIdFromAnchor(anchorId: string | null): string | null {
  if (!anchorId) return null
  const match = /^turn-(.+)-q$/.exec(anchorId)
  return match ? match[1]! : null
}
