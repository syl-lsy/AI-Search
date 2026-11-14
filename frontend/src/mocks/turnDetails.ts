/**
 * src/mocks/turnDetails.ts — 轮次终态静态 fixture（api.md §2.5 镜像形态；§9 fixture 优先）。
 * 覆盖：快速完成态（followups 恒 null=实验默认关闭）、达标 fast 轮（followups 非空，
 * §9 v1.9 fixture③ 回看渲染样例）、深度完成态（export_available=true）、排队轮
 * （§9 v1.5 queued 用例——仅 query 与状态已持久化）、direct 完成态（§9 v1.15 fixture②——
 * sources=[]/images=[]/stages 仅 rewrite+write 两节点）。
 */
import type { TurnDetail } from '@/types/api'

export const THREAD_ID_FAST = '7d2f0a1e-1c3b-4e5a-9d8f-0a1b2c3d4e5f'
export const TURN_ID_FAST = 'c31b9d74-2a41-4f6e-b3c2-5d6e7f809a1b'
export const TURN_ID_DEEP = 'd4e5f6a7-8b90-4cde-a123-4567890abcde'
export const TURN_ID_QUEUED = 'e5f6a7b8-9c0d-4e1f-b2a3-4c5d6e7f8a9b'
export const TURN_ID_DIRECT = 'f6a7b8c9-0d1e-4f2a-b3c4-5d6e7f809a2c'

const USAGE_ZERO = { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, cost_usd: 0 }

/** 快速完成态（followups 恒 null——实验默认关闭时的正常态，api.md §2.5） */
export const turnDetailFastCompleted: TurnDetail = {
  turn_id: TURN_ID_FAST,
  thread_id: THREAD_ID_FAST,
  mode: 'fast',
  status: 'completed',
  query: 'LangGraph 的 Send API 和 Router 的区别？',
  query_lang: 'zh',
  rewritten_queries: ['LangGraph Send API Router 区别'],
  parent_turn_id: null,
  answer_md: 'Send API 用于**动态扇出**，Router 用于静态分支。\n\n两者可组合使用 [1]。',
  sources: [
    {
      source_id: 'f00d0001-0000-4000-8000-000000000001',
      ref_no: 1,
      source_type: 'web',
      title: 'LangGraph docs — Send API vs Router',
      url: 'https://docs.langchain.com/langgraph/send-vs-router',
      snippet: 'Send lets you fan out dynamically; Router picks a static branch...',
      score: 0.9123,
      snapshot_available: false,
      kb_doc_id: null,
    },
  ],
  // §2.5 v1.12：images[] 自 turn_events 重建；条目键可省（本条仅 url——上游无宽高/来源页时不伪造）
  images: [{ url: 'https://example.com/architecture.png' }],
  stages: [
    { stage_key: 'rewrite', status: 'done', label: '改写意图', started_at: '2026-11-01T08:12:00Z', ended_at: '2026-11-01T08:12:01Z', duration_ms: 320, extra: undefined },
    { stage_key: 'search', status: 'done', label: '并行搜索 3 个查询', started_at: '2026-11-01T08:12:01Z', ended_at: '2026-11-01T08:12:04Z', duration_ms: 2870, extra: undefined },
    { stage_key: 'write', status: 'done', label: '综合中', started_at: '2026-11-01T08:12:04Z', ended_at: '2026-11-01T08:12:11Z', duration_ms: 7420, extra: undefined },
  ],
  degraded: false,
  degraded_reason: null,
  error: null,
  usage: { prompt_tokens: 8421, completion_tokens: 532, total_tokens: 8953, cost_usd: 0.0091 },
  duration_ms: 7420,
  export_available: false,
  created_at: '2026-11-01T08:12:00Z',
  finished_at: '2026-11-01T08:12:11Z',
  incognito: false,
  cancel_reason: null,
  followups: null,
  // memory_hits 字段 M6 起返回（M5 及之前无此字段）——fixture 按当前后端形态省略
}

/** 达标 fast 轮：followups 非空（§9 v1.9 fixture③——2.5 回看渲染样例，REST 为事实源） */
export const turnDetailFastWithFollowups: TurnDetail = {
  ...turnDetailFastCompleted,
  followups: [
    'Send API 的失败分支怎么处理？',
    'Router 的条件边怎么写？',
    '两者能在一张图里混用吗？',
  ],
}

/** 深度完成态（export_available=true：mode=deep 且 answer_md 非空且 completed，§2.5 v1.10 判据） */
export const turnDetailDeepCompleted: TurnDetail = {
  turn_id: TURN_ID_DEEP,
  thread_id: THREAD_ID_FAST,
  mode: 'deep',
  status: 'completed',
  query: '对比 LangGraph 与 CrewOS 做调研 Agent 的取舍',
  query_lang: 'zh',
  rewritten_queries: ['LangGraph 调研 agent 架构', 'CrewOS multi-agent 框架对比'],
  parent_turn_id: null,
  answer_md: '# 调研报告\n\n## 摘要\n...\n\n## 取舍对比\n...\n\n## 引用\n[1] ...',
  sources: [
    {
      source_id: 'f00d0001-0000-4000-8000-000000000001',
      ref_no: 1,
      source_type: 'web',
      title: 'LangGraph docs — Send API',
      url: 'https://docs.langchain.com/langgraph/send-api',
      snippet: 'Send lets you fan out...',
      score: 0.9123,
      snapshot_available: true,
      kb_doc_id: null,
    },
    {
      source_id: 'aa310002-0000-4000-8000-000000000002',
      ref_no: 2,
      source_type: 'kb',
      title: '内部设计文档-检索融合.md',
      url: null,
      snippet: 'KB 候选与网络候选合并后统一 rerank...',
      score: 0.8871,
      snapshot_available: false,
      kb_doc_id: '9a770003-0000-4000-8000-000000000003',
    },
  ],
  images: [],
  stages: [
    { stage_key: 'plan', status: 'done', label: '规划 4 个子课题', started_at: '2026-11-01T08:12:01Z', ended_at: '2026-11-01T08:12:04Z', duration_ms: 3200, extra: { subtopics: 4 } },
    { stage_key: 'write', status: 'done', label: '撰写报告', started_at: '2026-11-01T08:13:50Z', ended_at: '2026-11-01T08:14:31Z', duration_ms: 41000, extra: { sections: 5 } },
  ],
  degraded: false,
  degraded_reason: null,
  error: null,
  usage: { prompt_tokens: 84210, completion_tokens: 5320, total_tokens: 89530, cost_usd: 0.0812 },
  duration_ms: 152300,
  export_available: true,
  created_at: '2026-11-01T08:12:00Z',
  finished_at: '2026-11-01T08:14:32Z',
  incognito: false,
  cancel_reason: null,
  followups: null,
}

/**
 * direct 完成态（§9 v1.15 fixture②——2.5 direct 轮回看渲染用例，api.md §7.4 v1.14 direct 形态 /
 * §2.5 v1.14：免检索直答轮 sources 恒空数组、stages 仅 rewrite/write 两节点、degraded 恒 false、
 * export_available=false（fast 不计入 report）、答案正文无 [n] 引用（零来源无悬挂引用）。
 *
 * stage(rewrite,done) 的 extra 携带 {"strategy":"direct"} 留痕（api.md §2.5 v1.14，
 * 评测/审计可判轮型；前端不消费不展示）——StageExtra 镜像已同步该键（2026-11-14
 * 调度方裁决 A 方案，镜像增量一行；sseSequences.ts directSequence 同步）。
 */
export const turnDetailDirectCompleted: TurnDetail = {
  turn_id: TURN_ID_DIRECT,
  thread_id: THREAD_ID_FAST,
  mode: 'fast',
  status: 'completed',
  query: '你好，请介绍一下你自己',
  query_lang: 'zh',
  rewritten_queries: ['你好，请介绍一下你自己'],
  parent_turn_id: null,
  answer_md: '你好！我是 AI 搜索助手，可以帮你检索网页、深度调研与管理个人知识库。',
  sources: [],
  images: [],
  stages: [
    { stage_key: 'rewrite', status: 'done', label: '改写意图', started_at: '2026-11-01T08:15:00Z', ended_at: '2026-11-01T08:15:00Z', duration_ms: 180, extra: { strategy: 'direct' } },
    { stage_key: 'write', status: 'done', label: '综合中', started_at: '2026-11-01T08:15:00Z', ended_at: '2026-11-01T08:15:02Z', duration_ms: 2220, extra: undefined },
  ],
  degraded: false,
  degraded_reason: null,
  error: null,
  usage: { prompt_tokens: 640, completion_tokens: 564, total_tokens: 1204, cost_usd: 0.0012 },
  duration_ms: 2400,
  export_available: false,
  created_at: '2026-11-01T08:15:00Z',
  finished_at: '2026-11-01T08:15:02Z',
  incognito: false,
  cancel_reason: null,
  followups: null,
}

/** direct 轮 + followups 非空（§9 v1.15 fixture③ 的 2.5 对账孪生形态——REST 为事实源，
 *  api.md §7.2 v1.14 注：轮型不排除 direct，闲聊/创作轮追问建议同为高价值目标） */
export const turnDetailDirectWithFollowups: TurnDetail = {
  ...turnDetailDirectCompleted,
  followups: ['帮我写一首关于秋天的诗', '你能联网搜索吗？', '把上面的介绍翻译成英文'],
}

/** 排队轮（§9 v1.5：status=queued——仅 query 与状态已持久化，等待期无业务事件） */
export const turnDetailQueued: TurnDetail = {
  turn_id: TURN_ID_QUEUED,
  thread_id: THREAD_ID_FAST,
  mode: 'fast',
  status: 'queued',
  query: '排队中的第二个问题',
  query_lang: 'zh',
  rewritten_queries: [],
  parent_turn_id: null,
  answer_md: '',
  sources: [],
  images: [],
  stages: [],
  degraded: false,
  degraded_reason: null,
  error: null,
  usage: USAGE_ZERO,
  duration_ms: 0,
  export_available: false,
  created_at: '2026-11-01T08:20:00Z',
  finished_at: null,
  incognito: false,
  cancel_reason: null,
  followups: null,
}
