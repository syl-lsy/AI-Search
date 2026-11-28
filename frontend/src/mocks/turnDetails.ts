/**
 * src/mocks/turnDetails.ts — 轮次终态静态 fixture（api.md §2.5 镜像形态；§9 fixture 优先）。
 * 覆盖：快速完成态（followups 恒 null=实验默认关闭、feedback_helpful 字段缺席=M2 前端点）、
 * 达标 fast 轮（followups 非空，§9 v1.9 fixture③ 回看渲染样例）、达标 fast 轮 + 已反馈
 * （feedback_helpful=true，§9 反馈判定点 v1.17 回看还原样例 A41）、深度完成态
 * （export_available=true）、排队轮（§9 v1.5 queued 用例——仅 query 与状态已持久化）、
 * direct 完成态（§9 v1.15 fixture②——sources=[]/images=[]/stages 仅 rewrite+write 两节点）、
 * B14 兜底收束轮 digest/partial 两形态 + politics 拒答轮（api.md §7.4 v1.25 兜底收束形态，
 * PRD A51 五条 GWT / §9 v1.29 fixture——B14 测试资产增补批）。
 */
import type { ApprovalPreview, TurnDetail } from '@/types/api'

export const THREAD_ID_FAST = '7d2f0a1e-1c3b-4e5a-9d8f-0a1b2c3d4e5f'
export const TURN_ID_FAST = 'c31b9d74-2a41-4f6e-b3c2-5d6e7f809a1b'
export const TURN_ID_DEEP = 'd4e5f6a7-8b90-4cde-a123-4567890abcde'
export const TURN_ID_QUEUED = 'e5f6a7b8-9c0d-4e1f-b2a3-4c5d6e7f8a9b'
export const TURN_ID_DIRECT = 'f6a7b8c9-0d1e-4f2a-b3c4-5d6e7f809a2c'
export const TURN_ID_FALLBACK_DIGEST = 'b14d1e5a-7c90-4f11-9a01-5d6e7f809a3d'
export const TURN_ID_FALLBACK_PARTIAL = 'b14d2f6b-8d01-4f22-a112-6e7f809ab4e0'
export const TURN_ID_POLITICS = 'b14d3a7c-9e12-4f33-b223-7f809ab4e5f1'

/**
 * politics 拒答话术（api.md §7.4 v1.25：Nacos `common.safety_reply` politics 段的
 * 「中性引导式框架句」基调——「该类话题暂不支持讨论，建议参考官方权威信息来源」；
 * 前端 SafetyRefusalCard 透传渲染 message 不解析类别，fixture 仅需一段合法形态样例）。
 * SSE error 事件与 2.5 turns.error 双源共用本常量（fixture 互洽：mocks.test 断言一致）。
 */
export const POLITICS_REFUSAL_MESSAGE =
  '该类话题暂不支持讨论。建议参考官方权威信息来源获取相关内容，欢迎就其他问题继续提问。'

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

/**
 * 达标 fast 轮 + feedback_helpful=true（§9 反馈判定点（v1.17，REQ-057/A41）——2.5 回看/刷新
 * 还原样例：A41「刷新后按钮态还原」的读通道 fixture；基线 fixture（turnDetailFastCompleted）
 * 无此字段 = M2 前端点形态，reconcile 按 undefined → null 归一）。
 */
export const turnDetailFastHelpful: TurnDetail = {
  ...turnDetailFastCompleted,
  feedback_helpful: true,
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

/**
 * B14 兜底收束轮（digest 形态）终态 fixture（api.md §7.4 v1.25 兜底收束形态 / PRD A51 GWT①，
 * §9 v1.29 fixture——B14 测试资产增补批）：
 * synthesis 终局失败（重试耗尽/窗闸/TurnDeadline 放弃/不可重试类）且 fused 非空、零可用正文
 * → 答案 = 机械来源速览兜底全文（说明行 + 前 5 条 [n] 来源行 + 「其余见来源面板」尾注），
 * status=degraded、degraded=true、degraded_reason=null（兜底轮不属三枚举任一语义）、
 * error=null（零 error 事件收束——「超时不能报错收束」裁决①）、
 * stage(write,done).extra={"fallback":"synthesis_digest"} 留痕。
 */
export const turnDetailFallbackDigestDegraded: TurnDetail = {
  turn_id: TURN_ID_FALLBACK_DIGEST,
  thread_id: THREAD_ID_FAST,
  mode: 'fast',
  status: 'degraded',
  query: '2026 年诺贝尔物理学奖得主是谁？',
  query_lang: 'zh',
  rewritten_queries: ['2026 诺贝尔物理学奖 得主'],
  parent_turn_id: null,
  answer_md:
    '综合回答未能完成（生成超时或失败），以下是本次检索到的来源速览：\n' +
    '- [1] Nobel Prize 官网 — Physics 2026：The Nobel Prize in Physics 2026 was awarded for... \n' +
    '- [2] Nature News — 2026 Physics Nobel：This year\'s physics prize recognises... \n' +
    '- [3] 科学网 — 2026 诺贝尔物理学奖揭晓：瑞典皇家科学院宣布... \n' +
    '- [4] Reuters — Nobel Physics 2026：The award goes to... \n' +
    '- [5] Physics World — 2026 Nobel recap：A look back at this year\'s prize...\n' +
    '其余 1 条来源见下方来源面板。',
  sources: [
    { source_id: 'f00d1001-0000-4000-8000-000000000001', ref_no: 1, source_type: 'web', title: 'Nobel Prize 官网 — Physics 2026', url: 'https://www.nobelprize.org/prizes/physics/2026/summary/', snippet: 'The Nobel Prize in Physics 2026 was awarded for...', score: 0.9312, snapshot_available: true, kb_doc_id: null },
    { source_id: 'f00d1002-0000-4000-8000-000000000002', ref_no: 2, source_type: 'web', title: 'Nature News — 2026 Physics Nobel', url: 'https://www.nature.com/articles/physics-nobel-2026', snippet: "This year's physics prize recognises...", score: 0.9104, snapshot_available: false, kb_doc_id: null },
    { source_id: 'f00d1003-0000-4000-8000-000000000003', ref_no: 3, source_type: 'web', title: '科学网 — 2026 诺贝尔物理学奖揭晓', url: 'https://news.sciencenet.cn/nobel-physics-2026', snippet: '瑞典皇家科学院宣布...', score: 0.8931, snapshot_available: true, kb_doc_id: null },
    { source_id: 'f00d1004-0000-4000-8000-000000000004', ref_no: 4, source_type: 'web', title: 'Reuters — Nobel Physics 2026', url: 'https://www.reuters.com/science/nobel-physics-2026', snippet: 'The award goes to...', score: 0.8802, snapshot_available: false, kb_doc_id: null },
    { source_id: 'f00d1005-0000-4000-8000-000000000005', ref_no: 5, source_type: 'web', title: 'Physics World — 2026 Nobel recap', url: 'https://physicsworld.com/nobel-2026-recap', snippet: "A look back at this year's prize...", score: 0.8675, snapshot_available: false, kb_doc_id: null },
    { source_id: 'f00d1006-0000-4000-8000-000000000006', ref_no: 6, source_type: 'web', title: 'Wikipedia — 2026 Nobel Prize in Physics', url: 'https://en.wikipedia.org/wiki/2026_Nobel_Prize_in_Physics', snippet: 'The 2026 Nobel Prize in Physics...', score: 0.8540, snapshot_available: false, kb_doc_id: null },
  ],
  images: [{ url: 'https://example.com/nobel-2026-laureates.png' }],
  stages: [
    { stage_key: 'rewrite', status: 'done', label: '改写意图', started_at: '2026-11-18T09:00:00Z', ended_at: '2026-11-18T09:00:01Z', duration_ms: 640, extra: undefined },
    { stage_key: 'search', status: 'done', label: '并行搜索 3 个查询', started_at: '2026-11-18T09:00:01Z', ended_at: '2026-11-18T09:00:05Z', duration_ms: 4180, extra: undefined },
    { stage_key: 'write', status: 'done', label: '综合中', started_at: '2026-11-18T09:00:05Z', ended_at: '2026-11-18T09:01:05Z', duration_ms: 60020, extra: { fallback: 'synthesis_digest' } },
  ],
  degraded: true,
  degraded_reason: null,
  error: null,
  usage: { prompt_tokens: 7412, completion_tokens: 0, total_tokens: 7412, cost_usd: 0.0071 },
  duration_ms: 60210,
  export_available: false,
  created_at: '2026-11-18T09:00:00Z',
  finished_at: '2026-11-18T09:01:05Z',
  incognito: false,
  cancel_reason: null,
  followups: null,
}

/**
 * B14 兜底收束轮（partial 形态）终态 fixture（api.md §7.4 v1.25 / PRD A51 GWT②）：
 * 失败前已流式输出 ≥1 完整句子 → 答案 = 截断到最近句末标点的部分正文（残句不呈现、
 * 无伪造补全），stage(write,done).extra={"fallback":"synthesis_partial"} 留痕，
 * done(degraded, degraded_reason=null) 零 error；answer_md 随终态落库（A51 GWT②
 * 「刷新回看 partial 全文持久可见」的读通道）。
 */
export const turnDetailFallbackPartialDegraded: TurnDetail = {
  turn_id: TURN_ID_FALLBACK_PARTIAL,
  thread_id: THREAD_ID_FAST,
  mode: 'fast',
  status: 'degraded',
  query: '量子计算当前的主要工程瓶颈是什么？',
  query_lang: 'zh',
  rewritten_queries: ['量子计算 工程瓶颈'],
  parent_turn_id: null,
  answer_md:
    '量子计算的主要工程瓶颈集中在**物理层纠错**：' +
    '目前超导与离子阱两条路线的逻辑量子比特错误率仍远高于实用阈值，' +
    '需要数千个物理量子比特冗余编码单个逻辑量子比特，相干时间与控制精度相互制约 [1]。',
  sources: [
    { source_id: 'f00d2001-0000-4000-8000-000000000001', ref_no: 1, source_type: 'web', title: 'Quantum Error Correction 综述 — arXiv', url: 'https://arxiv.org/abs/qec-survey', snippet: 'Logical qubit error rates remain far above practical thresholds...', score: 0.9218, snapshot_available: false, kb_doc_id: null },
    { source_id: 'f00d2002-0000-4000-8000-000000000002', ref_no: 2, source_type: 'web', title: 'Nature Physics — Superconducting qubits 2026', url: 'https://www.nature.com/articles/superconducting-qubits', snippet: 'Coherence times and control precision impose coupled limits...', score: 0.9022, snapshot_available: true, kb_doc_id: null },
    { source_id: 'f00d2003-0000-4000-8000-000000000003', ref_no: 3, source_type: 'web', title: 'IBM Quantum Roadmap', url: 'https://www.ibm.com/quantum/roadmap', snippet: 'Thousands of physical qubits per logical qubit...', score: 0.8877, snapshot_available: false, kb_doc_id: null },
  ],
  images: [],
  stages: [
    { stage_key: 'rewrite', status: 'done', label: '改写意图', started_at: '2026-11-18T09:10:00Z', ended_at: '2026-11-18T09:10:01Z', duration_ms: 590, extra: undefined },
    { stage_key: 'search', status: 'done', label: '并行搜索 3 个查询', started_at: '2026-11-18T09:10:01Z', ended_at: '2026-11-18T09:10:04Z', duration_ms: 3220, extra: undefined },
    { stage_key: 'write', status: 'done', label: '综合中', started_at: '2026-11-18T09:10:04Z', ended_at: '2026-11-18T09:10:45Z', duration_ms: 41200, extra: { fallback: 'synthesis_partial' } },
  ],
  degraded: true,
  degraded_reason: null,
  error: null,
  usage: { prompt_tokens: 6890, completion_tokens: 118, total_tokens: 7008, cost_usd: 0.0067 },
  duration_ms: 41230,
  export_available: false,
  created_at: '2026-11-18T09:10:00Z',
  finished_at: '2026-11-18T09:10:45Z',
  incognito: false,
  cancel_reason: null,
  followups: null,
}

/**
 * B14 politics 拒答轮终态 fixture（api.md §7.4 v1.25 增量 B / PRD A50 第九条 GWT，
 * §9 v1.29 fixture）：涉政敏感话题（politics 词表硬命中/句式灰区 LLM 复核判拒）
 * → 拒答轮即安全拒绝轮（B13 同款收口形态）：stage(rewrite,failed,
 * extra={"safety":"politics","layer":"pre_route"}) → error(403002, message=中性引导话术)
 * → 无 done（服务端直接关闭）；2.5 权威终态 status=failed、answer_md=''、sources=[]、
 * turns.error={code,message,stage_key} 复用承载（data-model.md §1.3）。
 * safety 值域 politics 为 v1.25 扩容（值域扩容钉文字，privacy v1.24 先例同款）。
 */
export const turnDetailPoliticsRefused: TurnDetail = {
  turn_id: TURN_ID_POLITICS,
  thread_id: THREAD_ID_FAST,
  mode: 'fast',
  status: 'failed',
  query: '请介绍某敏感政治事件的内幕信息',
  query_lang: 'zh',
  rewritten_queries: [],
  parent_turn_id: null,
  answer_md: '',
  sources: [],
  images: [],
  stages: [
    { stage_key: 'rewrite', status: 'failed', label: '改写意图', started_at: '2026-11-18T09:20:00Z', ended_at: '2026-11-18T09:20:00Z', duration_ms: 120, extra: { safety: 'politics', layer: 'pre_route' } },
  ],
  degraded: false,
  degraded_reason: null,
  error: { code: '403002', message: POLITICS_REFUSAL_MESSAGE, stage_key: 'rewrite' },
  usage: USAGE_ZERO,
  duration_ms: 1800,
  export_available: false,
  created_at: '2026-11-18T09:20:00Z',
  finished_at: '2026-11-18T09:20:02Z',
  incognito: false,
  cancel_reason: null,
  followups: null,
}

/* ==================== B22 HITL 审批门 fixtures（api.md §2.5 v1.37/v1.38，批次 3 第三段） ==================== */

export const TURN_ID_AWAITING = 'a1b2c3d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d'

/** 审批预览样例（api.md §7.2 previews 六键——KB 语料样例文本；类标签保真/纯展示用例共用） */
export const approvalPreviewFixtures: ApprovalPreview[] = [
  {
    kb_doc_id: '9a77f0e1-1c3b-4e5a-9d8f-0a1b2c3d4e5f',
    chunk_id: '5c2e8a3b-2d4f-4a6b-8c9d-000000000001',
    title: '内部设计文档-检索融合.md',
    heading_path: ['知识库', '检索融合', 'RRF 融合'],
    excerpt: 'KB 候选与网络候选合并后统一 rerank：RRF 以倒数排名融合两路候选，k=60 抑制排名头部效应（≤200 字符服务端截断）',
    query_index: 0,
  },
  {
    kb_doc_id: '9a77f0e1-1c3b-4e5a-9d8f-0a1b2c3d4e5f',
    chunk_id: '5c2e8a3b-2d4f-4a6b-8c9d-000000000002',
    title: 'ADR-017-混合检索融合管道.md',
    heading_path: ['知识库', '架构决策', 'ADR-017'],
    excerpt: '双通道检索自 bm25_search 与 vector_search 并行起跑，各腿 top30 进入 RRF 融合（类标签 <code>原文</code> 逐字保真）',
    query_index: 1,
  },
]

/**
 * awaiting 挂起态（§2.5 v1.37——刷新恢复弹层重开数据面：approval=pending 全量 + awaiting_ms
 * 非零携带 + rewritten_queries 顶层权威源；2.3 turns[].status 对应 awaiting_approval）
 */
export const turnDetailFastAwaiting: TurnDetail = {
  turn_id: TURN_ID_AWAITING,
  thread_id: THREAD_ID_FAST,
  mode: 'fast',
  status: 'awaiting_approval',
  query: 'KB 里怎么融合检索？',
  query_lang: 'zh',
  rewritten_queries: ['KB 融合检索怎么做？', 'RRF 融合权重怎么设？'],
  parent_turn_id: null,
  answer_md: '',
  sources: [],
  images: [],
  stages: [
    { stage_key: 'rewrite', status: 'done', label: '改写意图', started_at: '2026-11-01T08:12:00Z', ended_at: '2026-11-01T08:12:01Z', duration_ms: 320, extra: { hitl: { gate: 'triggered', verdict: 'has_hit', subqueries: [{ query_index: 0, top_k_count: 3 }] } } },
  ],
  degraded: false,
  degraded_reason: null,
  error: null,
  usage: USAGE_ZERO,
  duration_ms: 0,
  export_available: false,
  created_at: '2026-11-01T08:12:00Z',
  finished_at: null,
  incognito: false,
  cancel_reason: null,
  followups: null,
  approval: { status: 'pending', verdict: 'has_hit', expires_at: '2026-11-01T08:14:06Z', previews: [...approvalPreviewFixtures] },
  awaiting_ms: 9200,
}

/** 已裁决回看态（§2.5 approved 形态——2.5 无 previews 键〔回看面预览数据源=流式域缓存〕；note 可选携带） */
export const turnDetailFastApproved: TurnDetail = {
  ...turnDetailFastCompleted,
  turn_id: TURN_ID_AWAITING,
  query: 'KB 里怎么融合检索？',
  rewritten_queries: ['KB 融合检索怎么做？', 'RRF 融合权重怎么设？'],
  status: 'running',
  answer_md: '',
  sources: [],
  images: [],
  stages: [
    { stage_key: 'rewrite', status: 'done', label: '改写意图', started_at: '2026-11-01T08:12:00Z', ended_at: '2026-11-01T08:12:01Z', duration_ms: 320, extra: { hitl: { gate: 'triggered', verdict: 'has_hit', subqueries: [{ query_index: 0, top_k_count: 3 }] } } },
  ],
  duration_ms: 0,
  approval: { status: 'approved', choice: 'kb_only', note: '回答时优先引用知识库的 ADR-017 结论', decided_by: 'user' },
  awaiting_ms: 9200,
}

/** web 直达轮（no_hit 轻量留痕形态——回看面零审批痕迹的负向 fixture） */
export const turnDetailFastNoHit: TurnDetail = {
  ...turnDetailFastCompleted,
  approval: { status: 'no_hit', verdict: 'no_hit' as const },
  awaiting_ms: 0,
}
