/**
 * src/types/api.d.ts — api.md（v1.30）+ memory-api.md（v1.4；上游现行 v1.5，M5/M6 域增量
 * 未镜像——M5 开工前同步）字段/枚举的静态镜像（冻结）
 *
 * 镜像基准：
 *   - docs/contract/api.md v1.30（M3 知识库契约增量 2026-11-22：KbDocStatus 六态扩容
 *     〔旧 parsing/embedding 作废 → normalizing/chunking/indexing〕/Source.kb_chunk_id/
 *     KbChunkDetail 溯源下钻/KbImport* 导入任务面/KbParseMeta/KbDocument 增
 *     parse_meta·catalog 可选键/ErrorCode 409007——新增契约面已批准（2026-11-22 确认点②），
 *     实施随 M3 批次 1~6；【v1.31 镜像批注】KbChunkDetail 增可选键 node_token?/node_id?
 *     （api.md §4.6 A53 GWT#2 飞书原文回跳投影）
 *   - docs/contract/api.md v1.28（历史基准——REST + SSE 协议唯一事实源；批次 6 收口轮（2026-11-21，
 *     P2-3 用户裁决「当批收口全修」）随批钉正：**Artifact 镜像改 §2.8 export 八键线形**
 *     〔drop turn_id、补 thread_id——此前误按「与 §5.2 artifacts 条目同构」落型〕，详见
 *     Artifact docstring；两处消费面 fixture 同步：api-modules.test.ts / answer-actions.test.ts
 *     EXPORT_ARTIFACT；download_path 消费面增形态守卫〔AnswerActions P2-4，同源相对路径
 *     ∧ /api/ 前缀正则，非法 toast 不跳转〕）。M2 批次 6 主批随批镜像 = 注释级同步（零类型
 *     形状变更）——批次 6 消费面落地注记：第 8 钮可见性公式 mode∈{fast,deep}
 *     〔api.md §2.12 前置 3 v1.28，frontend-api.md v1.30 §3 生命周期 14 deep 形态注〕/ §2.8
 *     export 与 §5.1 snapshot 消费面（exportTurnReport→Artifact、fetchSnapshot→SnapshotData
 *     既有镜像直用，turnStore 增 exportAvailable 投影字段〔§2.5 export_available 对账回写〕））。
 *     M2 批次 5 批次注：§2.12 regenerate 前置放开
 *     mode∈{fast,deep}（deep 有源 degraded/failed 轮可重生成，api.md §7.4 v1.28 深度
 *     regenerate 形态）：RegenerateTurnData docstring 与 ErrorCode 409006 触发场景注释同步；
 *     §2.4 REQ-017 parented 轮消费面（TurnDetail.parent_turn_id 既有镜像不变）。B14 批已落
 *     镜像：§2.5/§7.2 stages[].extra 可选键 fallback——frontend-api.md §8.5 登记行）
 *   - docs/contract/memory-api.md v1.4 §4/§5（记忆域字段级契约——M2 镜像停留 v1.4 形态：
 *     v1.5 的 M5/M6 域增量〔2026-11-19 去噪/膨胀治理/冲突消解〕M2 零消费面、未镜像，
 *     M5 开工前同步镜像基准）
 *   - frontend-api.md §8.5（镜像类型清单）+ §8.9（类型盲区纪律）
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
  | '403002' // 内容安全拒绝——§0 域码（B13 拒答轮：轮终态 failed + turns.error 载荷携带本码）
  | '404001' // 资源不存在
  | '404002' // 该来源无快照 → 404
  | '404004' // 轮次不存在（反馈端点，api.md §0 v1.16；语义同 404001 turn 维度，独立码便于反馈面埋点区分）
  | '409001' // 线程已有进行中轮次
  | '409002' // 轮次已处于终态，不可取消
  | '409003' // 不满足导出条件
  | '409004' // 该线程已有进行中的记忆抽取任务
  | '409005' // 线程有进行中轮次，不可删除（api.md §0 v1.18，REQ-059）
  | '409006' // 轮次不支持重新生成（api.md §0 v1.28，REQ-063——非 {degraded,failed} 终态/零来源/regenerate 进行中重复点击单码覆盖；deep 有源 degraded/failed 轮 v1.28 起可重生成，原「deep 轮一律拒收」收窄为 deep 零源/进行中形态）
  | '409007' // 已有进行中的知识库导入任务（api.md §0 v1.30，REQ-067——已批准（2026-11-22 确认点②）；pending/running 导入任务存在时重复触发 POST /kb/import/jobs；409004 记忆抽取同族语义）
  | '409008' // 轮次不在待审批态或审批裁决冲突（api.md §0 v1.37，REQ-069——B22 HITL 审批门：非 awaiting_approval 轮调用 POST /turns/{id}/approval；或同轮已按不同 choice 裁决后再提交/超时收口后的人为裁决；同 choice 重复提交为幂等 200 不产生本码；定号注：计划草案 409007 已被 kb_import_jobs 占用顺延）
  | '413001' // 文件超过大小上限
  | '415001' // 不支持的文件类型
  | '422001' // URL 非法
  | '500001' // 内部错误（兜底）
  | '503001' // 配置中心不可达且无缓存

/**
 * SSE 专属错误码（仅出现在 error 事件，不用于 REST；api.md §7.2）
 */
export type SseErrorCode =
  | '403002' // 内容安全拒绝——§0 域码，允许出现于 error 帧（B13 拒答轮：无部分内容 → error 后直接关闭、无 done）
  | '500101'
  | '500201'
  | '500301'
  | '500401'

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

/** §2.5 status 枚举（v1.4 起 +queued；v1.37 起 +awaiting_approval——B22 HITL 审批门挂起态：占线/无协程/孤儿清理豁免；canceling 仅为 cancel 端点响应信号、非枚举值 §2.7） */
export type TurnStatus =
  | 'pending'
  | 'running'
  | 'queued'
  | 'awaiting_approval'
  | 'completed'
  | 'degraded'
  | 'failed'
  | 'canceled'

/** §2.5 cancel_reason 枚举（仅 status=canceled 时非空；v1.38 起 +timeout——B22 审批等待超时静默收口，hitl.on_timeout=cancel 裁决） */
export type CancelReason = 'user' | 'superseded' | 'rolled_back' | 'timeout'

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
  /** api.md §7.4 v1.14 direct / v1.22 recall 形态留痕：rewrite 输出 search_strategy=direct|recall 时携带（前端不消费不展示；类型保持宽松 string——B9 先例，枚举扩容零类型形状变更） */
  strategy?: string
  /** api.md §7.2/§2.5 v1.19 P1b 留痕：源名→同源延迟重试次数映射（stage(search,done) extra，形如 {"tavily": 1}；无重试零键，前端零渲染义务） */
  source_retries?: Record<string, number>
  /** api.md §7.4/§2.5 v1.23 B13 安全拒绝轮 stage failed extra 留痕（命中类别 danger|profanity|privacy——privacy=隐私侵害意图轮一律 reject，api.md §7.4 v1.24 值域扩容（pii 走 sanitize 不产生 failed stage，前端零消费不展示）；宽松 string 同 strategy 先例） */
  safety?: string
  /** api.md §7.4/§2.5 v1.23 B13 拦截层标注（pre_route|rewrite|planner，前端零消费不展示） */
  layer?: string
  /** api.md §7.4/§2.5 v1.25 B14 兜底收束轮留痕（synthesis_digest=来源速览兜底 | synthesis_partial=部分正文收束——stage(write,done) extra 携带，评测/审计判兜底轮型与 M4 采样排除依据〔digest 轮不计入 P50/P90〕；前端零消费不展示——degraded 渲染走 done/2.5 的 status/degraded 字段；宽松 string 同 strategy/safety 先例，值域扩容零类型形状变更） */
  fallback?: string
  /** api.md §7.2/§2.5 v1.26 决策模型判定源留痕（Decision Model 工单批次 2）：decision=决策模型判定生效 | light_fallback=decision 失败/超时/结构异常/低置信回退 light 全职责（不置 degraded） | shadow=shadow 双跑观测态（decision 只记录不生效）——仅 stage(rewrite, done) 携带；decision 未启用〔model.mapping.decision 缺省空串 kill switch〕与存量轮不携带该键（键可省略不置 null，沿 v1.12 images 先例）；前端零渲染义务（观测/评测判读键，extra.strategy v1.14 先例同款——宽松 string 同 strategy/safety/fallback 先例，值域扩容零类型形状变更） */
  decision_source?: string
  /** api.md §7.2/§2.5 v1.27 finalize 越界引用剔除计数留痕：deep 轮 stage(write,done) extra 携带剔除处数（**仅 >0 时携带**，零剔除零键——键可省略不置 null，沿 v1.12 images 先例）；观测/评测判读键、前端零渲染义务（宽松 number 类型；值本身即计数无值域扩容面） */
  citation_dropped?: number
  /** api.md v1.33 零面判定行（B19）：kb_route 三档路由留痕——值域 kb_only|kb_first|hybrid，仅 stage(rewrite,done) 携带且仅非缺省值（hybrid=灰度缺省键可省，沿 v1.12 images 先例不置 null）；前端零渲染义务（宽松 string 同 strategy/decision_source 先例，值域扩容零类型形状变更） */
  kb_route?: string
  /** api.md v1.33 零面判定行（B19）：fast 有界纠错轮标记——纠错第二趟的 stage(rewrite,done) extra 携带 correction:true（纠错轮=既有 stage 事件重复发射〔同 key 多次出现合法〕，零新事件类型）；前端零渲染义务（宽松 boolean） */
  correction?: boolean
  /** api.md v1.33 零面判定行（B19）：融合链路观测投影（A35 可观测留痕）——stage(search,done) extra 携带 {per_query_top_k, rrf_k, kb_fuse_top_k, es_hits, vec_hits, type_weights}（通道活跃副本才携带，键可省）；观测/评测判读键、前端零渲染义务（宽松 Record<string, unknown>） */
  kb_funnel?: Record<string, unknown>
  /** api.md §7.2/§2.5 v1.37 B22 HITL 门留痕（REQ-069）：仅门轮（含 shadow）的 stage(rewrite,done) 携带 {gate: triggered|shadow, verdict: has_hit|no_hit, subqueries: [{query_index, top_k_count, top1_score?}], gate_ms?}（评测/审计判读键——弹窗率分子=has_hit、误弹窗率=has_hit∧web_only；非门轮零键不置 null，前端零渲染义务；宽松 Record<string, unknown> 同 kb_funnel 先例） */
  hitl?: Record<string, unknown>
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
  /**
   * api.md §2.5/§7.2 v1.30（M3——已批准（2026-11-22 确认点②））：KB 来源恒携带——该来源对应知识库 chunk id（溯源下钻 §4.6 解析键）；
   * web 来源 null。可选键口径（就地修订 2026-11-22 门禁修复轮）：存量轮（M3 前落库）与缓冲重放的旧事件
   * 无此键——前端按缺省 null 消费，镜像落可选键。
   */
  kb_chunk_id?: Uuid | null
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
  /** v1.18：标题模糊搜索（ILIKE 包含、大小写不敏感；trim 后空串=不过滤；1~200 字符） */
  q?: string
  /** v1.18：缺省 false=活跃集；true=仅归档 */
  archived?: boolean
  /** v1.18：缺省 false；true=仅未读（read_at IS NULL）；与 archived 正交 */
  unread?: boolean
}

export interface ThreadListItem {
  thread_id: Uuid
  title: string
  mode_summary: ThreadModeSummary
  turn_count: number
  /** v1.18：置顶（活跃视图排序首位；归档视图不消费） */
  pinned: boolean
  /** v1.18：未读投影（read_at IS NULL） */
  is_unread: boolean
  /** v1.18：归档投影（archived_at IS NOT NULL） */
  archived: boolean
  created_at: IsoTimestamp
  updated_at: IsoTimestamp
}

export type ThreadListData = Paginated<ThreadListItem>

/* -------------------------------------------------------------
 * 2.10/2.11 —— 线程管理（PATCH 部分更新 / DELETE 物理删除，v1.18 增）
 * ----------------------------------------------------------- */

/** §2.10 PATCH 请求体（任意组合、至少一项——空对象 400001；title trim 后 1~120 字符） */
export interface ThreadPatchRequest {
  /** 1~120 字符（trim 后非空；空串/纯空白/超长 400001） */
  title?: string
  pinned?: boolean
  /** true=标记为未读（read_at 置 NULL）/ false=置已读 */
  is_unread?: boolean
  /** true=归档 / false=取消归档 */
  archived?: boolean
}

/** §2.10 响应 data：更新后的线程投影（与 §2.2 items[] 条目同构） */
export type ThreadPatchData = ThreadListItem

/** §2.11 DELETE 响应 data（§4.5 KbDeleteData 同构） */
export interface ThreadDeleteData {
  deleted: boolean
}

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

/**
 * §2.5 error 字段载荷（镜像勘误 2026-11-15，随审计修复落地）——data-model.md §1.3 冻结口径
 * `turns.error: jsonb {code, message, stage_key} 终态错误；成功为 null`（后端 app/schemas/
 * turn.py TurnError 同构：code/message 必填、stage_key 可空可省）；api.md §2.5 仅给 null 示例
 * 未钉型——本镜像此前误落 `string | null`，failed/进程重启中断轮直写会渲染 [object Object]。
 */
export interface TurnError {
  code: string
  message: string
  /** 可空可省（pydantic 序列化形态两种均兼容；前端不消费不展示——失败定位在 SSE error 事件） */
  stage_key?: string | null
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
  /** 失败信息（TurnError 对象载荷或 null；示例恒 null；failed/进程重启中断轮经本字段对账） */
  error: TurnError | null
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
  /**
   * v1.16（REQ-057/A41）：该轮用户反馈现值（true=有帮助 / false=没有帮助 / null=未反馈）。
   * M2 起返回、此前无此字段（可选字段语义，前端按可空处理）——A41 刷新/回看还原读通道。
   */
  feedback_helpful?: boolean | null
  /** M6（记忆 P1）起返回——M5 及之前无此字段，故可省（memory-api.md 镜像） */
  memory_hits?: MemoryHits
  /** v1.37（B22/REQ-069）审批门投影四形态（非门轮键整体省略不置 null；pending 即轮 status=awaiting_approval——刷新恢复以本字段为准源重新打开弹层；v1.38 钉：status=canceled 的审批行 approval 键整体省略——审计留痕在 turn_approvals 表，回看面无审批折叠条） */
  approval?: TurnApproval
  /** v1.37（B22）awaiting_approval 累计时长毫秒——非零才携带（键可省口径；duration_ms 同步扣除 awaiting 时长〔hitl-OQ-03〕） */
  awaiting_ms?: number
}

/* -------------------------------------------------------------
 * 2.9 POST /api/v1/turns/{turn_id}/feedback —— 轮次有帮助反馈
 * ----------------------------------------------------------- */

/** §2.9 请求体（v1.17：helpful 为 boolean | null 必填——true/false=upsert 改判语义不变；
 * null = 清除反馈（删除该轮 turn_feedback 行）；缺失/非 boolean/null 一律 400001，strict 保留） */
export interface TurnFeedbackRequest {
  helpful: boolean | null
}

/**
 * §2.9 响应 data（v1.16 / REQ-057；v1.17 取消语义——true/false=upsert 改判后的现值；
 * null=已清除（反馈行已删除，updated_at 同为 null）。每轮一条 upsert 改判，
 * 两按钮互斥单击即改判/同钮再点即取消）。
 */
export interface FeedbackResponse {
  turn_id: Uuid
  helpful: boolean | null
  updated_at: IsoTimestamp | null
}

/* -------------------------------------------------------------
 * 2.13 POST /api/v1/turns/{turn_id}/approval —— 审批裁决（v1.37 增，B22/REQ-069/A54）
 * ----------------------------------------------------------- */

/** §2.5/§7.2 v1.37 审批裁决四选项（hitl-OQ 裁决值域） */
export type ApprovalChoice = 'kb_only' | 'web_only' | 'both' | 'custom'

/** §2.13 请求体：choice 必填枚举（缺失/非法→400001）；note 可选 ≤500 字（任意 choice 可附带——协议面无 choice 限制；服务端过 query 同款 sanitize 管道〔R35〕后落库，不改检索范围） */
export interface ApprovalDecisionRequest {
  choice: ApprovalChoice
  note?: string
}

/** §2.13 响应 data：202=首次成功（阶段 2 已调度）/ 200=幂等重复（返回已记录裁决，不重复触发阶段 2）；信封 code 恒 "0"、status 恒 "running" */
export interface ApprovalDecisionData {
  turn_id: Uuid
  status: 'running'
  choice: ApprovalChoice
}

/** §7.2 approval_requested previews 条目（≤hitl.preview_count 条、excerpt 服务端截断 ≤preview_chars、按门内 rerank 分跨子问题全局去重排序；分数不下发） */
export interface ApprovalPreview {
  kb_doc_id: Uuid
  chunk_id: Uuid
  title: string
  heading_path: string[]
  excerpt: string
  query_index: number
}

/** §2.5 v1.37 approval 投影四形态（非门轮键整体省略不置 null；v1.38 钉：status=canceled 的审批行 approval 键整体省略——审计留痕在 turn_approvals 表）：pending=待裁决（轮 status=awaiting_approval）/ approved=已裁决（含超时/系统收口，decided_by 区分）/ no_hit=web 直达轻量留痕 / shadow=shadow 观测态（previews 不投影） */
export type TurnApproval =
  | { status: 'pending'; verdict: 'has_hit'; expires_at: IsoTimestamp; previews: ApprovalPreview[] }
  | { status: 'approved'; choice: ApprovalChoice; note?: string; decided_by: 'user' | 'timeout' | 'system' }
  | { status: 'no_hit'; verdict: 'no_hit' }
  | { status: 'shadow'; verdict: 'has_hit' | 'no_hit' }

/** §7.2 approval_requested（审批请求，B22 HITL 门——仅 enforce 弹窗形态；阶段 1 门判定落库后发布，入 turn_events 落库子集可重放；受 seq 幂等门保护） */
export interface SseApprovalRequestedEvent extends SseBase {
  verdict: 'has_hit'
  /** 等待窗截止（服务端超时判据 expires_at；前端倒计时仅消费做展示，不做本地超时动作） */
  expires_at: IsoTimestamp
  previews: ApprovalPreview[]
}

/** §7.2 approval_resolved（审批收口——裁决提交/超时收口/用户取消/interrupt·rollback 四路径；与 approval_requested 成对可重放） */
export interface SseApprovalResolvedEvent extends SseBase {
  /** timeout 收口=缺省策略值 hybrid 对应 both；cancel 收口无 choice——键省略 */
  choice?: ApprovalChoice
  decided_by: 'user' | 'timeout' | 'system'
  /** 收口路径留痕：submit=裁决提交 | timeout=超时收口 | cancel=用户取消 | conflict=interrupt/rollback 直接收尾 */
  resolved_by: 'submit' | 'timeout' | 'cancel' | 'conflict'
  next_stage?: 'search'
}

/* -------------------------------------------------------------
 * 2.7/2.8 —— cancel / export
 * ----------------------------------------------------------- */

/** §2.7 cancel 响应 data（canceling 仅为响应信号，非 turns.status 枚举值） */
export interface CancelTurnData {
  turn_id: Uuid
  status: 'canceling'
}

/* -------------------------------------------------------------
 * 2.12 POST /api/v1/turns/{turn_id}/regenerate —— degraded 轮仅重新生成（v1.19 增）
 * ----------------------------------------------------------- */

/**
 * §2.12 响应 data（v1.19 增，REQ-063/A47；v1.28 前置放开 mode∈{fast,deep}）：degraded/
 * failed-with-sources fast/deep 轮「仅重新生成」——复用既有 sources 只重跑 synthesis
 * （sources/[n] 映射稳定、images 不动不重发），请求体空、无参数。deep 分支重放起点=write
 * （无 plan/search/read/reflect 段，api.md §7.4 v1.28 深度 regenerate 形态）。轮已置 running、
 * SSE 续发同一 turn 既有通道（前端重订阅 + 清空重渲约定，frontend-api.md §3 生命周期 14）；
 * 错误 404001/409006/500001。
 */
export interface RegenerateTurnData {
  turn_id: Uuid
  status: 'running'
}

/**
 * §2.8 export 响应 data（八键线形，批次 6 收口轮 P2-3 钉正——此前误按「与 §5.2 artifacts
 * 条目同构」落型：§2.8 响应含 thread_id、不含 turn_id；§5.2 artifacts 列表条目则带 turn_id、
 * 无 thread_id，两者不同构。如需承载 §5.2 产物列表应另立 ArtifactListItem 类型，不共用本型）。
 */
export interface Artifact {
  artifact_id: Uuid
  thread_id: Uuid
  artifact_type: 'report' | 'code_output'
  filename: string
  size_bytes: number
  content_type: string
  created_at: IsoTimestamp
  /** 原样请求/跳转（§5.3：前端只用 download_path；消费面过形态守卫——AnswerActions P2-4） */
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

/**
 * §4.3 状态机（api.md v1.30，REQ-052 多阶段——M3 起，已批准（2026-11-22 确认点②））：
 * pending → normalizing → chunking → indexing → ready；任一阶段失败 failed（fail_reason 阶段化）。
 * 旧 parsing/embedding 值作废（取代口径——M3 未上线无存量语义负担，迁移随 data-model.md 同批）。
 */
export type KbDocStatus = 'pending' | 'normalizing' | 'chunking' | 'indexing' | 'ready' | 'failed'

/**
 * §4.1/4.2/4.3/4.4 KB 文档条目。
 * 字段出现性按端点：上传（4.1）无 source_url/updated_at/ready_at/duplicated/parse_meta/catalog_ids/catalog_path；
 * URL 收藏（4.2）多 duplicated；列表（4.3）多 updated_at/ready_at/parse_meta/catalog_ids/catalog_path
 * 〔api.md §4.3 v1.30——已批准（2026-11-22 确认点②）〕；单条（4.4）同列表项。
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
  /** api.md §4.3 v1.30（M3——已批准（2026-11-22 确认点②））：归一化产物摘要——normalizing 完成前 null；页式来源 pages 有值、非页式 null */
  parse_meta?: KbParseMeta | null
  /** api.md §4.3 v1.30（M3——已批准（2026-11-22 确认点②））：飞书目录链节点 id 数组（root→leaf，连接器外部 token 非 UUID）——本地上传/URL 为 null */
  catalog_ids?: string[] | null
  /** api.md §4.3 v1.30（M3——已批准（2026-11-22 确认点②））：目录显示路径（目录层标题串联）——本地上传/URL 为 null */
  catalog_path?: string | null
}

/** §4.2 请求体 */
export interface CollectUrlRequest {
  url: string
}

/** §4.5 响应 data */
export interface KbDeleteData {
  deleted: boolean
}

/** §4.3 v1.30（M3——已批准（2026-11-22 确认点②））：归一化产物摘要（页数/表格数/代码块数——页式来源 pages 有值、非页式 null） */
export interface KbParseMeta {
  pages: number | null
  tables: number
  code_blocks: number
}

/** §4.3 响应 data：items 字段同 4.1 响应对象（另含 updated_at/ready_at/parse_meta/catalog_ids/catalog_path）+ total（limit/offset 分页约定同 §0） */
export type KbDocumentListData = Paginated<KbDocument>

/**
 * §4.3 文档列表查询参数（`?limit=50&offset=0&status=&doc_type=`——api.md §4.3；
 * P1-A 分页消费面：触底加载 limit/offset + ready 已入库计数条件请求 status）
 */
export interface KbDocumentListQuery {
  limit?: number
  offset?: number
  status?: KbDocStatus
  doc_type?: KbDocType
}

/* -------------------------------------------------------------
 * 4.6 GET /api/v1/sources/{source_id}/kb-chunk —— KB 来源溯源下钻（api.md v1.30 增，REQ-053——
 * 已批准（2026-11-22 确认点②）；v1.31 增列 node_token?/node_id? 两可选键）
 * ----------------------------------------------------------- */

/**
 * §4.6 响应 data（chunk 原文与位置物化字段；溯源内容仅进 UI 不进 LLM、前端渲染过 DOMPurify——
 * frontend-api.md §5 v1.33/v1.34 KbChunkDrilldown）。
 */
export interface KbChunkDetail {
  chunk_id: Uuid
  document_id: Uuid
  document_title: string
  /** chunk 原文（纯文本/Markdown 形态随来源格式） */
  content: string
  /** 标题链 root→leaf（物化自 chunk.meta.heading_path）；无标题链来源可为空数组 */
  heading_path: string[]
  /** 页码/段落定位（键恒出现、值可空——页式来源有页码、结构化来源有段落序） */
  page: number | null
  paragraph: number | null
  /** 飞书来源才有（可选键，缺则省略不置 null，api.md §4.6 v1.31）——「回跳飞书原文」外链 URL = https://www.feishu.cn/wiki/{node_token}（KbChunkDrilldown 消费）；本地/URL 来源无此键 */
  node_token?: string
  /** 统一寻址可选键（仅飞书源，缺则省略，api.md §4.6 v1.31）——载荷形态 fs:node:{node_token} / fs:blk:{block_id}，对齐 data-model.md §1.8 meta.node_id */
  node_id?: string
}

/* -------------------------------------------------------------
 * 4.7~4.9 /api/v1/kb/import/jobs —— 飞书导入任务（api.md v1.30 增，REQ-067/A53——已批准（2026-11-22 确认点②））
 * ----------------------------------------------------------- */

/** §4.7 connector 枚举（M3 仅 feishu——连接器扩展沿此键位） */
export type KbImportConnector = 'feishu'

/** §4.7 请求体（connector 非法 400001；进行中重复触发 409007） */
export interface KbImportTriggerRequest {
  connector: KbImportConnector
}

/** §4.7 响应 data */
export interface KbImportTriggerData {
  job_id: Uuid
  status: 'pending'
}

/** §4.8/4.9 任务 status 枚举 */
export type KbImportJobStatus = 'pending' | 'running' | 'completed' | 'failed'

/** §4.9 触发方式（手动触发/定时全量 diff 同表） */
export type KbImportTriggerKind = 'manual' | 'scheduled'

/** §4.9 失败清单条目（category 非封闭枚举——state errors[] 先例，宽松 string；document_id=null=文档未建成） */
export interface KbImportFailure {
  title: string
  category: string
  message: string
  document_id: Uuid | null
}

/** §4.9 失败分类计数（category 非封闭枚举，宽松 string） */
export interface KbImportFailureSummary {
  category: string
  count: number
}

/** §4.8 列表条目 / §4.9 详情条目基形（进度五数 + failure_summary；列表条目不含 failures） */
export interface KbImportJob {
  job_id: Uuid
  connector: KbImportConnector
  trigger: KbImportTriggerKind
  status: KbImportJobStatus
  /** 本批次待处理文档单元总数（触发时快照计数） */
  total_docs: number
  /** 已推进数 = imported + failed + skipped */
  processed_docs: number
  imported_docs: number
  failed_docs: number
  /** mindnote/slides 等无读取 API 能力类型跳过留痕（A53 第 3 GWT 不静默） */
  skipped_docs: number
  failure_summary: KbImportFailureSummary[]
  started_at: IsoTimestamp | null
  finished_at: IsoTimestamp | null
  created_at: IsoTimestamp
  updated_at: IsoTimestamp
}

/** §4.8 响应 data：items 为任务条目（同 4.9 详情基形、不含 failures，created_at DESC 新任务在前）+ total */
export type KbImportJobListData = Paginated<KbImportJob>

/** §4.9 详情 = 列表条目 + failures 失败清单（仅详情端点返回；自用 931+ 量级一次返回不设分页） */
export interface KbImportJobDetail extends KbImportJob {
  failures: KbImportFailure[]
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

/** SSE 事件类型枚举（§7.2 全集：stage/token/thinking/sources/images/context_compact/memory/done/followups/approval_requested/approval_resolved/error） */
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
  | 'approval_requested'
  | 'approval_resolved'
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
  approval_requested: SseApprovalRequestedEvent
  approval_resolved: SseApprovalResolvedEvent
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
