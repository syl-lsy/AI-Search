/**
 * src/mocks/sseSequences.ts — §7.4 典型事件序列静态 fixture（api.md §7 线格镜像；
 * §9 fixture 优先——后端就绪前 SSE 管线对 fixture 开发）。
 *
 * 形态 = SseWireEvent（id=seq、event=类型名、data=§7.2 payload）。
 * 注意（§7.1）：心跳 `: ping` 为注释行，不占 seq，不出现在本 fixture。
 */
import type { SseWireEvent } from '@/types/api'
import { THREAD_ID_FAST, TURN_ID_DEEP, TURN_ID_DIRECT, TURN_ID_FAST } from './turnDetails'

/**
 * 快速模式序列（api.md §7.4）：
 * stage(rewrite) → stage(search,progress=x/3) → sources(batch) → images(可选)
 * → stage(write,label=综合中) → token…token → done(completed) → followups(可选，§7.1 保活窗口)。
 * images 条目仅 url（v1.12 键可省——上游无宽高/来源页/标题时不伪造）。
 */
export const fastSequence: SseWireEvent[] = [
  { id: 1, event: 'stage', data: { seq: 1, turn_id: TURN_ID_FAST, ts: '2026-11-01T08:12:00Z', stage_key: 'rewrite', status: 'running', label: '改写意图' } },
  { id: 2, event: 'stage', data: { seq: 2, turn_id: TURN_ID_FAST, ts: '2026-11-01T08:12:01Z', stage_key: 'rewrite', status: 'done' } },
  { id: 3, event: 'stage', data: { seq: 3, turn_id: TURN_ID_FAST, ts: '2026-11-01T08:12:01Z', stage_key: 'search', status: 'running', label: '并行搜索 3 个查询', progress: { current: 1, total: 3 }, detail: 'SearXNG 0/3 · BoCha 0/3' } },
  { id: 4, event: 'sources', data: { seq: 4, turn_id: TURN_ID_FAST, ts: '2026-11-01T08:12:03Z', batch: [{ source_id: 'f00d0001-0000-4000-8000-000000000001', ref_no: 1, source_type: 'web', title: 'LangGraph docs — Send API vs Router', url: 'https://docs.langchain.com/langgraph/send-vs-router', snippet: 'Send lets you fan out dynamically...', score: 0.9123, snapshot_available: false, kb_doc_id: null }], total: 1 } },
  { id: 5, event: 'images', data: { seq: 5, turn_id: TURN_ID_FAST, ts: '2026-11-01T08:12:03Z', images: [{ url: 'https://example.com/architecture.png' }] } },
  { id: 6, event: 'stage', data: { seq: 6, turn_id: TURN_ID_FAST, ts: '2026-11-01T08:12:04Z', stage_key: 'write', status: 'running', label: '综合中' } },
  { id: 7, event: 'token', data: { seq: 7, turn_id: TURN_ID_FAST, ts: '2026-11-01T08:12:05Z', delta: 'Send API 用于**动态扇出**，' } },
  { id: 8, event: 'token', data: { seq: 8, turn_id: TURN_ID_FAST, ts: '2026-11-01T08:12:06Z', delta: 'Router 用于静态分支 [1]。' } },
  { id: 9, event: 'done', data: { seq: 9, turn_id: TURN_ID_FAST, ts: '2026-11-01T08:12:11Z', finish: 'completed', degraded: false, degraded_reason: null, duration_ms: 7420, source_count: 1, usage: { total_tokens: 8953, cost_usd: 0.0091 } } },
  { id: 10, event: 'followups', data: { seq: 10, turn_id: TURN_ID_FAST, ts: '2026-11-01T08:12:15Z', items: ['Send API 的失败分支怎么处理？', 'Router 的条件边怎么写？', '两者能在一张图里混用吗？'] } },
]

/**
 * 深度模式序列（api.md §7.4 + v1.3 两新事件位置）：
 * stage(plan) → stage(search,extra.subagents) → sources×2 → stage(read) → sources(batch3)
 * → stage(reflect) → stage(search) → stage(reflect,done) → context_compact（仅 deep）
 * → stage(write) → thinking/token 交替 → stage(write,done,extra.sections) → images(可选) → done(completed)。
 */
export const deepSequence: SseWireEvent[] = [
  { id: 1, event: 'stage', data: { seq: 1, turn_id: TURN_ID_DEEP, ts: '2026-11-01T08:12:00Z', stage_key: 'plan', status: 'running', label: '规划 4 个子课题' } },
  { id: 2, event: 'stage', data: { seq: 2, turn_id: TURN_ID_DEEP, ts: '2026-11-01T08:12:04Z', stage_key: 'plan', status: 'done', extra: { subtopics: 4 } } },
  { id: 3, event: 'stage', data: { seq: 3, turn_id: TURN_ID_DEEP, ts: '2026-11-01T08:12:04Z', stage_key: 'search', status: 'running', extra: { subagents: [{ subtopic: 'A 方案生态', status: 'done' }, { subtopic: 'B 方案性能', status: 'running' }, { subtopic: 'C 方案成本', status: 'running' }] } } },
  { id: 4, event: 'sources', data: { seq: 4, turn_id: TURN_ID_DEEP, ts: '2026-11-01T08:12:10Z', batch: [{ source_id: 'f00d0001-0000-4000-8000-000000000001', ref_no: 1, source_type: 'web', title: 'LangGraph docs — Send API', url: 'https://docs.langchain.com/langgraph/send-api', snippet: 'Send lets you fan out...', score: 0.91, snapshot_available: false, kb_doc_id: null }], total: 2 } },
  { id: 5, event: 'sources', data: { seq: 5, turn_id: TURN_ID_DEEP, ts: '2026-11-01T08:12:14Z', batch: [{ source_id: 'aa310002-0000-4000-8000-000000000002', ref_no: 2, source_type: 'kb', title: '内部设计文档-检索融合.md', url: null, snippet: 'KB 候选与网络候选合并后统一 rerank...', score: 0.89, snapshot_available: false, kb_doc_id: '9a770003-0000-4000-8000-000000000003' }], total: 2 } },
  { id: 6, event: 'stage', data: { seq: 6, turn_id: TURN_ID_DEEP, ts: '2026-11-01T08:12:20Z', stage_key: 'read', status: 'running', label: '阅读第 1 篇' } },
  { id: 7, event: 'sources', data: { seq: 7, turn_id: TURN_ID_DEEP, ts: '2026-11-01T08:13:00Z', batch: [{ source_id: 'f00d0004-0000-4000-8000-000000000004', ref_no: 3, source_type: 'web', title: 'CrewOS 官方文档 — 架构', url: 'https://docs.crewos.example/architecture', snippet: 'CrewOS organizes agents...', score: 0.87, snapshot_available: true, kb_doc_id: null }], total: 3 } },
  { id: 8, event: 'stage', data: { seq: 8, turn_id: TURN_ID_DEEP, ts: '2026-11-01T08:13:10Z', stage_key: 'read', status: 'done', progress: { current: 6, total: 6 } } },
  { id: 9, event: 'stage', data: { seq: 9, turn_id: TURN_ID_DEEP, ts: '2026-11-01T08:13:12Z', stage_key: 'reflect', status: 'running', label: '反思第 1/2 轮', detail: '补搜方向：成本对比维度缺失' } },
  { id: 10, event: 'stage', data: { seq: 10, turn_id: TURN_ID_DEEP, ts: '2026-11-01T08:13:30Z', stage_key: 'search', status: 'running' } },
  { id: 11, event: 'stage', data: { seq: 11, turn_id: TURN_ID_DEEP, ts: '2026-11-01T08:13:45Z', stage_key: 'reflect', status: 'done' } },
  { id: 12, event: 'context_compact', data: { seq: 12, turn_id: TURN_ID_DEEP, ts: '2026-11-01T08:13:50Z', summarized_messages: 12, released_tokens: 58000, retained_tokens: 9000, offloaded_files: [{ ref: '/large_tool_results/abc123.md', url: 'https://example.com/long-page' }] } },
  { id: 13, event: 'stage', data: { seq: 13, turn_id: TURN_ID_DEEP, ts: '2026-11-01T08:13:50Z', stage_key: 'write', status: 'running', label: '撰写第 1 节' } },
  { id: 14, event: 'thinking', data: { seq: 14, turn_id: TURN_ID_DEEP, ts: '2026-11-01T08:13:52Z', delta: '先比较两框架的事件模型差异，再定位 Send 的扇出语义…' } },
  { id: 15, event: 'token', data: { seq: 15, turn_id: TURN_ID_DEEP, ts: '2026-11-01T08:13:53Z', delta: '# 调研报告\n\n' } },
  { id: 16, event: 'token', data: { seq: 16, turn_id: TURN_ID_DEEP, ts: '2026-11-01T08:13:55Z', delta: '## 摘要\n\n两框架的取舍集中在...' } },
  { id: 17, event: 'thinking', data: { seq: 17, turn_id: TURN_ID_DEEP, ts: '2026-11-01T08:13:56Z', delta: '引用编号需与 sources 面板对齐。' } },
  { id: 18, event: 'token', data: { seq: 18, turn_id: TURN_ID_DEEP, ts: '2026-11-01T08:13:58Z', delta: '\n\n## 取舍对比\n\n见 [1] [2]。' } },
  { id: 19, event: 'stage', data: { seq: 19, turn_id: TURN_ID_DEEP, ts: '2026-11-01T08:14:31Z', stage_key: 'write', status: 'done', extra: { sections: 5 } } },
  { id: 20, event: 'images', data: { seq: 20, turn_id: TURN_ID_DEEP, ts: '2026-11-01T08:14:31Z', images: [{ url: 'https://example.com/compare-chart.png', source_url: 'https://example.com/compare-page', title: '对比图表', width: 1200, height: 750 }] } },
  { id: 21, event: 'done', data: { seq: 21, turn_id: TURN_ID_DEEP, ts: '2026-11-01T08:14:32Z', finish: 'completed', degraded: false, degraded_reason: null, duration_ms: 152300, source_count: 3, usage: { total_tokens: 89530, cost_usd: 0.0812 } } },
]

/**
 * 快速 direct 形态序列（§9 v1.15 fixture①，api.md §7.4 v1.14 direct 形态）：
 * 免检索直答轮（rewrite 输出 search_strategy=direct——闲聊/寒暄/能力询问/创作/翻译/纯计算），
 * stage(rewrite,running) → stage(rewrite,done) → stage(write,running) → token…token
 * → stage(write,done) → done(completed, source_count=0)——**无 stage(search)/sources/images 事件**。
 * 时间线仅 rewrite/write 两节点（frontend-api.md §3 stage 行 v1.15 注：按 stage_key 驱动，
 * 缺 key 即缺节点）；§3 sources 行 v1.15 注：无 sources 事件且 2.5 sources=[] → 不渲染来源面板
 * 与「收集中」徽标（无空态、无「来源 0」占位）。
 * seq 2 事件 extra={"strategy":"direct"} 为留痕字段（api.md §7.4 v1.14；前端不消费不展示，
 * §3 v1.15 注）——StageExtra 镜像已同步该键（2026-11-14 调度方裁决 A 方案，镜像增量一行）。
 */
export const directSequence: SseWireEvent[] = [
  { id: 1, event: 'stage', data: { seq: 1, turn_id: TURN_ID_DIRECT, ts: '2026-11-01T08:15:00Z', stage_key: 'rewrite', status: 'running', label: '改写意图' } },
  { id: 2, event: 'stage', data: { seq: 2, turn_id: TURN_ID_DIRECT, ts: '2026-11-01T08:15:00Z', stage_key: 'rewrite', status: 'done', extra: { strategy: 'direct' } } },
  { id: 3, event: 'stage', data: { seq: 3, turn_id: TURN_ID_DIRECT, ts: '2026-11-01T08:15:00Z', stage_key: 'write', status: 'running', label: '综合中' } },
  { id: 4, event: 'token', data: { seq: 4, turn_id: TURN_ID_DIRECT, ts: '2026-11-01T08:15:01Z', delta: '你好！我是 AI 搜索助手，' } },
  { id: 5, event: 'token', data: { seq: 5, turn_id: TURN_ID_DIRECT, ts: '2026-11-01T08:15:01Z', delta: '可以帮你检索网页、深度调研与管理个人知识库。' } },
  { id: 6, event: 'stage', data: { seq: 6, turn_id: TURN_ID_DIRECT, ts: '2026-11-01T08:15:02Z', stage_key: 'write', status: 'done' } },
  { id: 7, event: 'done', data: { seq: 7, turn_id: TURN_ID_DIRECT, ts: '2026-11-01T08:15:02Z', finish: 'completed', degraded: false, degraded_reason: null, duration_ms: 2400, source_count: 0, usage: { total_tokens: 1204, cost_usd: 0.0012 } } },
]

/**
 * followups 追加于 direct 轮（§9 v1.15 fixture③——实验开关开启时 done 后经保活窗口下发，
 * api.md §7.4 v1.14 注「direct 轮同样适用」+ §7.2 v1.14 注轮型不排除 direct）。
 * 独立导出为「追加样例」：与 directSequence 尾部 done 拼接即完整 ③ 形态
 * （`… → done(completed, source_count=0) → followups`，L13 收口①：交付后 close）。
 */
export const directFollowupsEvent: SseWireEvent = {
  id: 8,
  event: 'followups',
  data: { seq: 8, turn_id: TURN_ID_DIRECT, ts: '2026-11-01T08:15:06Z', items: ['帮我写一首关于秋天的诗', '你能联网搜索吗？', '把上面的介绍翻译成英文'] },
}

/** 序列归属线程（fixture 自描述：THREAD_ID_FAST 也承载 deep/direct 轮的 thread_id） */
export const SSE_SEQUENCE_THREAD_ID = THREAD_ID_FAST
