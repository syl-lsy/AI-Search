/**
 * src/mocks/envelope.ts — fixture 信封包装助手。
 * mock 均为「信封内 data」形态（api.md §0）；需要模拟完整响应时用本助手包装，
 * 供 client 信封守卫（isEnvelope/unwrapEnvelope）测试与集成联装使用。
 * 仅开发期与测试使用——e2e 必须打真服务（AGENTS.md：fixture 仅开发期用）。
 */
import type { ApiSuccessEnvelope } from '@/types/api'

export function asEnvelope<T>(data: T): ApiSuccessEnvelope<T> {
  return { code: '0', message: 'ok', data }
}
