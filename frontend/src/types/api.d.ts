/**
 * src/types/api.d.ts — api.md（v1.14）+ memory-api.md（v1.4）字段/枚举的静态镜像（冻结）
 *
 * 镜像基准：
 *   - docs/contract/api.md v1.14（REST + SSE 协议唯一事实源；§7.4 direct 形态留痕键 StageExtra.strategy 随本批镜像同步）
 *   - docs/contract/memory-api.md v1.4 §4/§5（记忆域字段级契约）
 *   - frontend-api.md v1.15 §8.5（镜像类型清单）+ §8.9（类型盲区纪律）
 *   - .zcode/workflow-drafts/batch7-handoff.md §1（A6 前置小修落型口径：images 条目键可省）
 *
 * 铁律：契约变更必须同步本文件（评审 diff 核对）；本文件是静态镜像、非运行时校验器——
 * 运行时形态防线 = 镜像同步 + src/mocks fixture 测试 + 评审 diff（§8.9⑥）。
 * 禁 as 双重断言与 @ts-ignore；无类型边界产物先守卫收窄（§8.9①②③）。
 */

/* =============================================================
 * 0. 通用约定（api.md §0）
 * =========================================================== */

/** api.md §0：时间戳一律 ISO-8601 UTC 带时区 */
export type IsoTimestamp = string

/** api.md §0：ID 一律 UUID v4 字符串 */
export type Uuid = string

/** api.md §0 错误码表（REST 错误信封 code；MRO 兜底 500001） */
export type ErrorCode =
  | '400001' // 请求参数错误
  | '403001' // 记忆功能已关闭
  | '404001' // 资源不存在
  | '404002' // 该来源无快照 → 404
  | '409001' // 线程已有进行中轮次
  | '409002' // 轮次已处于终态，不可取消
  | '409003' // 不满足导出条件
  | '409004' // 该线程已有进行中的记忆抽取任务
  | '413001' // 文件超过大小上限
  | '415001' // 不支持的文件类型
  | '422001' // URL 非法
  | '500001' // 内部错误（兜底）
  | '503001' // 配置中心不可达且无缓存

/** SSE 专属错误码（仅出现在 error 事件，不用于 REST；api.md §7.2） */
export type SseErrorCode = '500101' | '500201' | '500301' | '500401'

/** api.md §0 成功信封（/api/ 前缀自动包装） */
export interface ApiSuccessEnvelope<T> {
  code: '0'
  message: string
  data: T
}

/** api.md §0 错误信封（message 为用户可读中文，前端可直接 toast） */
export interface ApiErrorEnvelope {
  code: ErrorCode
  message: string
}

/** api.md §0 信封联合（成功/错误以 code==='0' 判别） */
export type ApiEnvelope<T> = ApiSuccessEnvelope<T> | ApiErrorEnvelope

/** 分页统一 limit（默认 50，上限 200）+ offset（默认 0）；列表响应含 total（api.md §0） */
export interface Paginated<T> {
  total: number
  items: T[]
}

/* =============================================================
 * 1. 健康检查（api.md §1；不走信封）
 * =========================================================== */

/** §1.2 组件状态枚举（v1.10 补冻结：ok|degraded|unavailable；sandbox 恒为 disabled） */
export type ReadinessComponentStatus = 'ok' | 'degraded' | 'unavailable'
export type ReadinessStatus = 'ok' | 'unavailable'

export interface ReadinessResponse {
  status: ReadinessStatus
  components: {
    db: ReadinessComponentStatus
    nacos: ReadinessComponentStatus
    minio: ReadinessComponentStatus
    litellm: ReadinessComponentStatus
    /** sandbox 恒为 'disabled'（§1.2 v1.10 冻结） */
    sandbox: 'disabled'
  }
}

/* =============================================================
 * 2. 线程与轮次（api.md §2）
 * =========================================================== */

/** §2.1 mode 枚举 */
export type AskMode = 'fast' | 'deep'

/** §2.1/§3.1 chat 模型键集合（v1.7：qwen|glm|deepseek；选项以 GET /models 返回为准） */
export type ChatModelKey = 'qwen' | 'glm' | 'deepseek'

/** §0 on_conflict 四策略（对齐 LangGraph Agent Server multitask_strategy） */
export type OnConflictStrategy = 'reject' | 'enqueue' | 'interrupt' | 'rollback'

/** §2.5 status 枚举（v1.4 起 +queued；canceling 仅为 cancel 端点响应信号、非枚举值 §2.7） */
export type TurnStatus =
  | 'pending'
  | 'running'
  | 'queued'
  | 'completed'
  | 'degraded'
  | 'failed'
  | 'canceled'

/** §2.5 cancel_reason 枚举（仅 status=canceled 时非空） */
export type CancelReason = 'user' | 'superseded' | 'rolled_back'

/** §2.2 mode_summary（线程内轮次模式聚合，写入时计算） */
export type ThreadModeSummary = 'fast' | 'deep' | 'mixed'

/** §2.5 degraded_reason 枚举 */
export type DegradedReason = 'timeout' | 'budget' | 'source_failure'

/** §2.5/§7.2 stage_key 枚举 */
export type StageKey = 'rewrite' | 'search' | 'read' | 'reflect' | 'plan' | 'write' | 'code_exec'

/** §2.5 stages[].status / §7.2 stage 事件 status */
export type StageStatus = 'running' | 'done' | 'failed'

/** §7.2 stage 事件 extra.subagents[]（deep 的 search/read 阶段专用） */
export interface SubAgent {
  subtopic: string
  status: 'running' | 'done' | 'failed'
}

/** §7.2 stage 事件 extra（subagents/subtopics/sections 为已知键；其余上游自定键放行） */
export interface StageExtra {
  subagents?: SubAgent[]
  subtopics?: number
  sections?: number
  /** api.md §7.4 v1.14 direct 形态留痕：rewrite 输出 search_strategy=direct 时携带（前端不消费不展示） */
  strategy?: string
}

/** §2.5 usage（turns.usage jsonb：与 LiteLLM 用量列风格一致） */
export interface TokenUsage {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  cost_usd: number
}

/** §7.2 done 事件 usage（子集：total_tokens + cost_usd） */
export interface DoneUsage {
  total_tokens: number
  cost_usd: number
}

/** §2.5 sources[] / §7.2 sources 事件 batch[] 条目 */
export interface Source {
  source_id: Uuid
  ref_no: number
  source_type: 'web' | 'kb'
  title: string
  /** kb 来源 url 为 null（api.md §2.5 示例） */
  url: string | null
  snippet: string
  score: number
  snapshot_available: boolean
  kb_doc_id: Uuid | null
}

/**
 * §7.2 images 事件条目 / §2.5 images[] 条目（v1.12：键可省——仅 url 恒在（必填），
 * source_url/title/width/height 上游缺则省略该键（不置 null、不伪造）。
 * 渲染口径：缺键不渲染对应元数据（frontend-api.md v1.13 §3 派发表 images 行）。
 */
export interface TurnImageItem {
  url: string
  source_url?: string
  title?: string
  width?: number
  height?: number
}

/** §2.5 memory_hits / §7.2 memory 事件 items[] 条目（字段级契约 memory-api.md §5.2） */
export interface MemoryHitItem {
  memory_id: Uuid
  /** ≤80 字符 */
  text_head: string
  category: MemoryCategory
  channel: MemoryChannel
  score: number
}

/** §2.5 memory_hits（M6 起返回；无痕轮恒为空对象） */
export interface MemoryHits {
  profile_used: boolean
  items: MemoryHitItem[]
}

/* -------------------------------------------------------------
 * 2.1 POST /api/v1/threads —— 首问建线程并启动轮次
 * ----------------------------------------------------------- */

/** §2.1/§2.4 提问请求体（v1.2 +incognito、v1.4 +on_conflict、M4 预留 images） */
export interface CreateThreadRequest {
  /** 1~2000 字符 */
  query: string
  mode: AskMode
  /** 缺省走服务端缺省（Nacos chat.default）；无用户选择时不携带（frontend-api.md §2） */
  model?: ChatModelKey
  /** M4 预留：data URL，≤3 张（REQ-025） */
  images?: string[]
  /** 无痕轮（默认 false）：该轮记忆注入/写入全停（REQ-036） */
  incognito?: boolean
  /** 同线程并发提问策略（REQ-042）；null=不携带走服务端缺省（frontend-api.md §2） */
  on_conflict?: OnConflictStrategy
}

/** §2.1/§2.4 响应 data：status 为 running（立即执行）或 queued（enqueue 排队轮） */
export interface StartTurnData {
  thread_id: Uuid
  turn_id: Uuid
  status: 'running' | 'queued'
}

/* -------------------------------------------------------------
 * 2.2 GET /api/v1/threads —— 线程列表
 * ----------------------------------------------------------- */

export interface ThreadListQuery {
  /** 默认 50，上限 200 */
  limit?: number
  /** 默认 0 */
  offset?: number
  /** mode 缺省=全部 */
  mode?: ThreadModeSummary
}

export interface ThreadListItem {
  thread_id: Uuid
  title: string
  mode_summary: ThreadModeSummary
  turn_count: number
  created_at: IsoTimestamp
  updated_at: IsoTimestamp
}

export type ThreadListData = Paginated<ThreadListItem>

/* -------------------------------------------------------------
 * 2.3 GET /api/v1/threads/{thread_id} —— 线程详情（答案页骨架）
 * ----------------------------------------------------------- */

/** 2.3 turns[] 条目（turns 按 created_at 升序；has_report = mode=deep 且 answer_md 非空，§2.3 v1.10） */
export interface TurnSummary {
  turn_id: Uuid
  mode: AskMode
  status: TurnStatus
  query: string
  /** 检测语言（api.md §2.3 示例 "zh"；枚举未冻结，跟随上游） */
  query_lang: string
  has_report: boolean
  degraded: boolean
  duration_ms: number
  created_at: IsoTimestamp
}

export interface ThreadDetail {
  thread_id: Uuid
  title: string
  mode_summary: ThreadModeSummary
  turn_count: number
  has_artifacts: boolean
  created_at: IsoTimestamp
  updated_at: IsoTimestamp
  turns: TurnSummary[]
}

/* -------------------------------------------------------------
 * 2.5 GET /api/v1/turns/{turn_id} —— 轮次终态详情（回看/终态渲染唯一数据源）
 * ----------------------------------------------------------- */

/** §2.5 stages[] 条目 */
export interface StageRecord {
  stage_key: StageKey
  status: StageStatus
  label: string
  started_at: IsoTimestamp | null
  ended_at: IsoTimestamp | null
  duration_ms: number | null
  extra?: StageExtra
}

/** §2.5 响应 data（进行中调用同样合法：返回已持久化部分） */
export interface TurnDetail {
  turn_id: Uuid
  thread_id: Uuid
  mode: AskMode
  status: TurnStatus
  query: string
  query_lang: string
  rewritten_queries: string[]
  parent_turn_id: Uuid | null
  /** 快速=综合回答 Markdown（≤3000 字上限、含 [n]）；深度=完整报告 Markdown */
  answer_md: string
  sources: Source[]
  /** 自该轮 turn_events 的 images 事件 payload 重建（轮内至多一次；零行→空数组；键可省口径同 §7.2） */
  images: TurnImageItem[]
  stages: StageRecord[]
  degraded: boolean
  degraded_reason: DegradedReason | null
  /** 失败信息（示例恒 null；failed 终态经本字段对账） */
  error: string | null
  usage: TokenUsage
  duration_ms: number
  /** mode=deep 且 answer_md 非空 且 status ∈ {completed, degraded}（v1.10 同源化判据） */
  export_available: boolean
  created_at: IsoTimestamp
  finished_at: IsoTimestamp | null
  /** v1.2：所有轮返回（回看态解释「该轮为何无记忆提示」） */
  incognito: boolean
  /** v1.4：恒出现、仅 status=canceled 时非空 */
  cancel_reason: CancelReason | null
  /**
   * v1.7 实验功能：恒出现可空；仅达标 fast 轮非空、异步回填（done 时刻可能仍为 null）。
   * REST 为事实源（SSE followups 事件仅为通知通道）。
   */
  followups: string[] | null
  /** M6（记忆 P1）起返回——M5 及之前无此字段，故可省（memory-api.md 镜像） */
  memory_hits?: MemoryHits
}

/* -------------------------------------------------------------
 * 2.7/2.8 —— cancel / export
 * ----------------------------------------------------------- */

/** §2.7 cancel 响应 data（canceling 仅为响应信号，非 turns.status 枚举值） */
export interface CancelTurnData {
  turn_id: Uuid
  status: 'canceling'
}

/** §2.8 export 响应 data（与 §5.2 artifacts 条目同构） */
export interface Artifact {
  artifact_id: Uuid
  artifact_type: 'report' | 'code_output'
  filename: string
  size_bytes: number
  content_type: string
  turn_id: Uuid
  created_at: IsoTimestamp
  /** 原样请求/跳转（§5.3：前端只用 download_path） */
  download_path: string
}

/* =============================================================
 * 3. 模型清单与建议池（api.md §3）
 * =========================================================== */

/** §3.1 GET /api/v1/models 响应 data（label 为服务端常量，前端不拼接不回退） */
export interface ModelsData {
  options: { key: ChatModelKey; label: string }[]
  default: ChatModelKey
}

/** §3.2 GET /api/v1/suggestions 响应 data（0~8 条、单条非空且 ≤200 字符；空数组合法） */
export interface SuggestionPoolData {
  items: string[]
}

/* =============================================================
 * 4. 知识库（api.md §4）
 * =========================================================== */

export type KbDocType = 'pdf' | 'md' | 'txt' | 'docx' | 'url'
export type KbDocStatus = 'pending' | 'parsing' | 'embedding' | 'ready' | 'failed'

/**
 * §4.1/4.2/4.3/4.4 KB 文档条目。
 * 字段出现性按端点：上传（4.1）无 source_url/updated_at/ready_at/duplicated；
 * URL 收藏（4.2）多 duplicated；列表（4.3）多 updated_at/ready_at；单条（4.4）同列表项。
 */
export interface KbDocument {
  document_id: Uuid
  doc_type: KbDocType
  title: string
  /** 4.2 起出现（URL 收藏） */
  source_url?: string
  /** URL 收藏无原始文件 → null */
  file_size_bytes: number | null
  status: KbDocStatus
  fail_reason: string | null
  chunk_count: number
  created_at: IsoTimestamp
  /** 4.3/4.4 出现 */
  updated_at?: IsoTimestamp
  ready_at?: IsoTimestamp | null
  /** 4.2 出现（重复收藏返回既有条目时 false） */
  duplicated?: boolean
}

/** §4.2 请求体 */
export interface CollectUrlRequest {
  url: string
}

/** §4.5 响应 data */
export interface KbDeleteData {
  deleted: boolean
}

/* =============================================================
 * 5. 快照与产物（api.md §5）
 * =========================================================== */

/** §5.1 GET /api/v1/sources/{source_id}/snapshot 响应 data */
export interface SnapshotData {
  snapshot_id: Uuid
  url: string
  content_type: string
  size_bytes: number
  fetched_at: IsoTimestamp
  /** 10 分钟有效 MinIO 预签名 URL */
  download_url: string
  /** 开头 2000 字符摘录（弹窗预览用） */
  excerpt: string
}

/* =============================================================
 * 6. 记忆域（memory-api.md v1.4 §4/§5 镜像；api.md §8 指认其为字段级唯一事实源）
 * =========================================================== */

/** memory-data-model.md §1（category 枚举，注入分流依据） */
export type MemoryCategory = 'style' | 'source' | 'language' | 'region' | 'interest' | 'fact' | 'other'

/** memory-data-model.md §1（origin 枚举） */
export type MemoryOrigin = 'explicit' | 'extracted' | 'user_manual' | 'user_edit'

/** memory-data-model.md §1（validity 软失效状态机） */
export type MemoryValidity = 'current' | 'superseded' | 'stale' | 'expired'

/** memory-api.md §2（channel 分流通道） */
export type MemoryChannel = 'rewrite' | 'prompt' | 'profile'

/** §4.1/4.2 记忆开关 */
export interface MemorySettings {
  memory_enabled: boolean
  implicit_learning_enabled: boolean
  updated_at: IsoTimestamp | null
}

/** §4.2 请求体（至少一个字段） */
export interface UpdateMemorySettingsRequest {
  memory_enabled?: boolean
  implicit_learning_enabled?: boolean
}

/** §4.3/4.4 记忆条目（列表/单条/添加/编辑响应同构） */
export interface MemoryItem {
  memory_id: Uuid
  /** 1~500 字符 */
  text: string
  category: MemoryCategory
  origin: MemoryOrigin
  confidence: number
  validity: MemoryValidity
  superseded_by: Uuid | null
  /** 手工添加（4.5）无对话来源 → null */
  thread_id: Uuid | null
  turn_id: Uuid | null
  effective_at: IsoTimestamp | null
  expires_at: IsoTimestamp | null
  usage_count: number
  last_accessed_at: IsoTimestamp | null
  created_at: IsoTimestamp
  updated_at: IsoTimestamp
}

/** §4.5 请求体（text 1~500 字符；category 非法枚举 400001） */
export interface CreateMemoryItemRequest {
  text: string
  category: MemoryCategory
}

/** §4.6 请求体（均可选、至少一个；text 不可为空串） */
export interface PatchMemoryItemRequest {
  text?: string
  category?: MemoryCategory
}

/** §4.7 响应 data */
export interface MemoryDeleteData {
  deleted: boolean
}

/** §4.8 请求体（scope：all=全部（默认）| auto=仅 origin=extracted） */
export interface ClearMemoryRequest {
  scope: 'all' | 'auto'
}

/** §4.9 导出文件（200 FileResponse，不走信封） */
export interface MemoryExportFile {
  schema_version: number
  exported_at: IsoTimestamp
  user_id: Uuid
  items: MemoryItem[]
  profile: { markdown: string }
}

/** §4.10/4.11 profile 卡（markdown='' 且 updated_at=null = 尚未生成） */
export interface MemoryProfile {
  markdown: string
  updated_at: IsoTimestamp | null
}

/** §4.11 请求体（≤2000 字符） */
export interface SaveMemoryProfileRequest {
  markdown: string
}

/** §4.12 query 参数 */
export interface ExtractionJobsQuery {
  limit?: number
  offset?: number
  status?: ExtractionJobStatus
  thread_id?: Uuid
}

/** memory-data-model.md §2（trigger_type 枚举；列名不用 trigger：PG 保留字） */
export type ExtractionTriggerType = 'thread_idle' | 'new_thread' | 'manual' | 'startup_recover'

/** memory-data-model.md §2（job status 枚举；skipped=无新内容，增量输入全为无痕轮时亦 skipped） */
export type ExtractionJobStatus = 'pending' | 'running' | 'completed' | 'failed' | 'skipped'

/** §4.12/4.13 抽取任务条目 */
export interface ExtractionJob {
  job_id: Uuid
  thread_id: Uuid
  trigger_type: ExtractionTriggerType
  status: ExtractionJobStatus
  /** 只计非无痕轮（v1.2） */
  input_turn_count: number
  applied_count: number
  fail_reason: string | null
  usage: TokenUsage | null
  started_at: IsoTimestamp | null
  finished_at: IsoTimestamp | null
  created_at: IsoTimestamp
}

/** §4.13 任务详情 = 列表项 + applied/input_turn_ids */
export interface ExtractionJobDetail extends ExtractionJob {
  applied: ExtractionAppliedOp[] | null
  input_turn_ids: Uuid[]
}

/** applied 条目（memory-data-model.md §2：AUDN 决策摘要） */
export interface ExtractionAppliedOp {
  op: 'add' | 'update' | 'delete' | 'invalidate'
  key: string
  reason: string
}

/** §4.14 请求体 */
export interface TriggerExtractionRequest {
  thread_id: Uuid
}

/** §4.14 响应 data */
export interface TriggerExtractionData {
  job_id: Uuid
  status: 'pending'
}

/* =============================================================
 * 7. SSE 事件协议（api.md §7 独立冻结节）
 * =========================================================== */

/**
 * §7.1 data 通用信封：{"seq","turn_id","ts",...类型字段}。
 * seq 为线程内单调递增整数（SSE id 行同值）。
 */
export interface SseBase {
  seq: number
  turn_id: Uuid
  ts: IsoTimestamp
}

/** §7.2 stage 事件 */
export interface SseStageEvent extends SseBase {
  stage_key: StageKey
  status: StageStatus
  label?: string
  /** progress.total 缺省时前端不渲染计数 */
  progress?: { current: number; total?: number }
  detail?: string
  extra?: StageExtra
}

/** §7.2 token 事件（答案增量，打字机；delta 为 Markdown 增量片段直接拼接） */
export interface SseTokenEvent extends SseBase {
  delta: string
}

/** §7.2 thinking 事件（v1.3：模型思维链增量；不入 answer_md，独立缓冲，§8.7 双缓冲） */
export interface SseThinkingEvent extends SseBase {
  delta: string
}

/** §7.2 sources 事件（分批追加；ref_no 首次出现即冻结、后续批次只增不改） */
export interface SseSourcesEvent extends SseBase {
  batch: Source[]
  total: number
}

/** §7.2 images 事件（至多一次、≤3 张；无图不发本事件） */
export interface SseImagesEvent extends SseBase {
  images: TurnImageItem[]
}

/** §7.2 context_compact 事件（v1.3，仅 deep；重放边界——本地 lastSeq 早于本 seq 则放弃增量续传） */
export interface SseContextCompactEvent extends SseBase {
  summarized_messages: number
  released_tokens: number
  retained_tokens: number
  /** 被卸载正文的文件引用与原 URL（可恢复压缩——丢正文保引用），可为空数组 */
  offloaded_files: { ref: string; url: string }[]
}

/** §7.2 memory 事件（M6；无命中且未用 profile 不发；无痕轮绝不发送） */
export interface SseMemoryEvent extends SseBase {
  items: MemoryHitItem[]
  profile_used: boolean
  truncated: boolean
}

/** §7.2 done 事件（终态；收到后对账 GET /turns/{id}） */
export interface SseDoneEvent extends SseBase {
  finish: 'completed' | 'degraded' | 'canceled'
  degraded: boolean
  degraded_reason: DegradedReason | null
  duration_ms: number
  source_count: number
  usage?: DoneUsage
  /** 可选出现、仅 finish=canceled 时携带（v1.4；与 2.5 同名字段同源，出现性契约不同——勿合并） */
  cancel_reason?: CancelReason
  /** 仅 >0 时出现（M5；无痕轮恒不含） */
  memory_saved?: number
}

/** §7.2 followups 事件（v1.7 实验默认关闭；必然晚于 done、至多一次、仅 fast+completed+answer 非空） */
export interface SseFollowupsEvent extends SseBase {
  /** string[]（长度 1~5，单条非空且 ≤200 字符） */
  items: string[]
}

/** §7.2 error 事件（error 不是终态；有部分内容时后跟 done(degraded)，无部分内容时直接关闭无 done） */
export interface SseErrorEvent extends SseBase {
  code: SseErrorCode
  message: string
  /** 定位失败时间线节点 */
  stage_key?: StageKey
}

/** SSE 事件类型枚举（§7.2 全集：stage/token/thinking/sources/images/context_compact/memory/done/followups/error） */
export type SseEventName =
  | 'stage'
  | 'token'
  | 'thinking'
  | 'sources'
  | 'images'
  | 'context_compact'
  | 'memory'
  | 'done'
  | 'followups'
  | 'error'

/** SSE data payload 按事件名的映射（turnStream.ts 解析时按 event 收窄的依据） */
export interface SseDataMap {
  stage: SseStageEvent
  token: SseTokenEvent
  thinking: SseThinkingEvent
  sources: SseSourcesEvent
  images: SseImagesEvent
  context_compact: SseContextCompactEvent
  memory: SseMemoryEvent
  done: SseDoneEvent
  followups: SseFollowupsEvent
  error: SseErrorEvent
}

/**
 * SSE 线格式事件（§7.1 三行式 id/event/data 的解析形态）：
 * id=seq、event=类型名、data=对应 payload。心跳 `: ping` 为注释行，不占 seq、不产事件。
 * 映射联合形态：event 与 data 按 SseDataMap 键关联——按 event 判别即可收窄 data。
 */
export type SseWireEvent = {
  [K in SseEventName]: { id: number; event: K; data: SseDataMap[K] }
}[SseEventName]

/** 单事件名形态的线格事件（turnStream 解析处按名字面量实例化时使用） */
export interface SseTypedWireEvent<T extends SseEventName = SseEventName> {
  id: number
  event: T
  data: SseDataMap[T]
}
