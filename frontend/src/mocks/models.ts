/**
 * src/mocks/models.ts — GET /api/v1/models 静态 fixture（api.md §3.1；§9 v1.7：三键、default=qwen）。
 */
import type { ModelsData } from '@/types/api'

export const modelsFixture: ModelsData = {
  options: [
    { key: 'qwen', label: 'Qwen' },
    { key: 'glm', label: 'GLM' },
    { key: 'deepseek', label: 'DeepSeek' },
  ],
  default: 'qwen',
}
