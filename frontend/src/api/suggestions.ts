/**
 * src/api/suggestions.ts — GET /api/v1/suggestions（首页推荐查询池热门池，api.md §3.2 / REQ-047）。
 * frontend-api.md §8.4：推荐池端点经本文件唯一出口（§4 threadListStore.fetchSuggestPool 的底层通道）。
 */
import { apiGet } from './client'
import type { SuggestionPoolData } from '@/types/api'

export function fetchSuggestionPool(): Promise<SuggestionPoolData> {
  return apiGet<SuggestionPoolData>('/api/v1/suggestions')
}
