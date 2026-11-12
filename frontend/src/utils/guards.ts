/**
 * src/utils/guards.ts — 无类型边界产物的类型守卫（frontend-api.md §8.9③）
 *
 * 适用边界：localStorage 读值、JSON.parse、拦截器返回等 unknown 形态——
 * 一律先经本模块守卫收窄再使用；API 数据的具体形态收窄依赖 src/types/api.d.ts 镜像（§8.5）。
 */
import type { ChatModelKey, OnConflictStrategy } from '@/types/api'
import type { AppLocale } from '@/i18n'

/** §8.9③ 基础守卫：对象形态 */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

const APP_LOCALES: readonly AppLocale[] = ['zh-CN', 'en-US']

/** localStorage `ai-search.lang` 守卫（frontend-api.md §2） */
export function isAppLocale(value: unknown): value is AppLocale {
  return typeof value === 'string' && APP_LOCALES.some((l) => l === value)
}

const CHAT_MODEL_KEYS: readonly ChatModelKey[] = ['qwen', 'glm', 'deepseek']

/** localStorage `ai-search.model` 守卫（api.md §2.1 v1.7 枚举；运行时权威以 GET /models 为准） */
export function isChatModelKey(value: unknown): value is ChatModelKey {
  return typeof value === 'string' && CHAT_MODEL_KEYS.some((k) => k === value)
}

const ON_CONFLICT_STRATEGIES: readonly OnConflictStrategy[] = ['reject', 'enqueue', 'interrupt', 'rollback']

/** localStorage `ai-search.onConflict` 守卫（api.md §0 v1.4 枚举） */
export function isOnConflictStrategy(value: unknown): value is OnConflictStrategy {
  return typeof value === 'string' && ON_CONFLICT_STRATEGIES.some((s) => s === value)
}

/**
 * localStorage 布尔键读值守卫（`ai-search.incognito`）：仅接受字符串 'true'/'false'，
 * 其余（null/任意他值）返回 null = 无有效值，由调用方落默认值。
 */
export function parseStoredFlag(value: unknown): boolean | null {
  if (value === 'true') return true
  if (value === 'false') return false
  return null
}

/**
 * UUID v4 校验（api.md §0：ID 一律 UUID v4）。
 * frontend-api.md §8.3：路由参数 threadId 经 string 校验后才进 API 调用。
 */
const UUID_V4_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

export function isUuid(value: unknown): value is string {
  return typeof value === 'string' && UUID_V4_RE.test(value)
}

/** 图片/链接协议白名单（frontend-api.md §2：渲染前校验 ^https?://） */
export function isHttpUrl(value: unknown): value is string {
  return typeof value === 'string' && /^https?:\/\//i.test(value)
}
