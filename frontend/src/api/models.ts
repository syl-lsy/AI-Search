/**
 * src/api/models.ts — GET /api/v1/models（chat 模型切换器数据，api.md §3.1 / REQ-010）。
 * 经 src/api/client.ts 唯一出口；URL 使用契约完整路径。
 */
import { apiGet } from './client'
import type { ModelsData } from '@/types/api'

export function fetchModels(): Promise<ModelsData> {
  return apiGet<ModelsData>('/api/v1/models')
}
