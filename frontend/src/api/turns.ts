/**
 * src/api/turns.ts — 轮次域 REST 出口面（api.md §2.8/§2.9/§2.12；经 src/api/client.ts 唯一出口）。
 *
 * v1.16（REQ-057/A41，frontend-api.md §1 v1.17）：POST /api/v1/turns/{turn_id}/feedback
 * ——每轮一条 upsert 改判；v1.17 增取消语义：helpful=null = 清除反馈（删除该轮反馈行，
 * 响应 helpful=null 且 updated_at=null）；错误 400001/404004/500001。
 * v1.19（REQ-063/A47，frontend-api.md §1 v1.26）：POST /api/v1/turns/{turn_id}/regenerate
 * ——degraded/failed-with-sources fast/deep 轮「仅重新生成」（复用既有 sources 只重跑 synthesis；
 * v1.30 放开 deep——api.md §2.12 前置第 3 条 v1.28，frontend-api.md §3 生命周期 14 deep 形态注）；
 * 请求体空、无参数，响应 {turn_id, status:'running'}；错误 404001/409006/500001
 * （toast 链路与编排归 turnStore.regenerate，§3 生命周期 14）。
 * 批次 6（REQ-016 导出链消费面落地）：POST /api/v1/turns/{turn_id}/export
 * ——深度报告服务端导出；错误 404001/409003（非深度轮或未完成 / 同线程同分钟跨轮碰撞，
 * 服务端中文 message 直达 toast）。
 */
import { apiPost } from './client'
import type { Artifact, FeedbackResponse, RegenerateTurnData } from '@/types/api'

/** 轮反馈（点按钮即发；乐观 UI/静默回滚/取消语义归 turnStore.sendFeedback） */
export function sendTurnFeedback(turnId: string, helpful: boolean | null): Promise<FeedbackResponse> {
  return apiPost<FeedbackResponse>(`/api/v1/turns/${turnId}/feedback`, { helpful })
}

/**
 * 轮次重新生成（api.md §2.12 v1.19）：服务端条件 UPDATE（WHERE status IN degraded,failed AND
 * EXISTS sources）天然防并发双击——进行中重复点击同码 409006。编排（状态置 running/清空重渲/
 * SSE 重订阅/错误 toast）归 turnStore.regenerate。
 */
export function regenerateTurn(turnId: string): Promise<RegenerateTurnData> {
  return apiPost<RegenerateTurnData>(`/api/v1/turns/${turnId}/regenerate`)
}

/**
 * 深度报告服务端导出（批次 6 REQ-016 消费面落地；api.md §2.8）：POST /api/v1/turns/{turn_id}/export
 * ——报告 md 写入线程产物目录（threads/{thread_id}/report_{YYYYMMDD_HHmm}.md）并登记 artifact，
 * 响应 data = Artifact 镜像（artifact_id/thread_id/artifact_type/filename/size_bytes/content_type/
 * created_at/download_path）。前置 = §2.5 export_available 判据（mode=deep ∧ answer_md 非空 ∧
 * status∈{completed,degraded}）——store 对账回写 exportAvailable、组件层门控渲染，前端不本地推断。
 * download_path 原样请求/跳转归消费面（§5.3：服务端 200 FileResponse + Content-Disposition，
 * 前端只用 download_path）。错误：404001（轮不存在）/ 409003（非深度轮或未完成 / 同线程同分钟
 * 跨轮导出碰撞【v1.27 钉】——normalizeError 直通服务端中文 message，toast 呈现）。
 */
export function exportTurnReport(turnId: string): Promise<Artifact> {
  return apiPost<Artifact>(`/api/v1/turns/${turnId}/export`)
}
