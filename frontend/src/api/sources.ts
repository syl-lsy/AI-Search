/**
 * src/api/sources.ts — 来源快照（api.md §5.1 GET /api/v1/sources/{source_id}/snapshot）。
 * 经 src/api/client.ts 唯一出口；URL 契约完整路径（§8.4）。AnswerView 散写 URL 收敛出口面
 * （评审留档 P2：视图层禁止散拼端点）。
 */
import { apiGet } from './client'
import type { SnapshotData } from '@/types/api'

export function fetchSnapshot(sourceId: string): Promise<SnapshotData> {
  return apiGet<SnapshotData>(`/api/v1/sources/${sourceId}/snapshot`)
}
