/**
 * src/stores/threadList.ts — threadListStore（frontend-api.md §4 第二行；setup store，§8.2）。
 *
 * 状态：items/total/modeFilter + suggestPool/suggestPoolLoaded（v1.10，REQ-047 热门池
 * 会话级缓存态）+ 分页态（v1.21 / REQ-058）：hasMore（items.length < total）/
 * loadingMore（防重入）/ loadMoreFailed（触底失败行内重试态）/ viewFilter（active|unread|
 * archived）/ searchQuery——items 为累积 append 形态（首屏替换、loadMore 追加、by thread_id
 * 去重）；recent = 侧栏「最近」独立投影（P2-2，2026-11-15：fetchRecent limit=10 缺省活跃集，
 * 不受 viewFilter/searchQuery 影响——items 是会话页当前视图列表，与侧栏语义分离）。
 * 动作：fetchThreads（缺省/offset=0=替换语义——首屏与重置）/ refresh（v1.21 语义钉正：
 * 重置分页态重拉首屏——新线程创建后走此口，防 unshift 与分页交叠重复）/ loadMore()
 * （offset=items.length 追加、去重、代数守卫防乱序覆盖——AnswerView loadSeq 同款）/
 * setViewFilter/setModeFilter/setSearchQuery（均重置分页+首屏拉取；视图映射 active→无
 * archived 参数、unread→unread=true、archived→archived=true；**首拉失败回滚状态并重新
 * 抛出**——调用方 toast，radio/chip 选中态随状态回滚保持一致，P2-4）/ 管理动作五件
 * renameThread/togglePin/markUnread/toggleArchive/deleteThread（v1.21 / REQ-059：
 * PATCH §2.10 / DELETE §2.11 经 src/api/threads.ts 唯一出口——乐观更新+失败回滚+toast
 * （错误信封 message）；成功以 §2.10 响应回填条目；toggleArchive/delete 在当前视图移除
 * 条目并 total-1；删除二次确认前置由菜单组件负责；**items 与 recent 双列表同步自愈**；
 * v1.24 增 markRead=markUnread 的 is_unread:false 反向——菜单未读项「标记为已读」落点，
 * 服务端 §2.10 同语义端点零后端改动）/
 * markReadLocally（打开线程详情后本地清未读点——服务端已随 GET 详情置已读，api.md §2.3
 * v1.18 注）/ fetchRecent（侧栏最近拉取）/ setRecentFilter + recentFilter/recentFiltered
 * （v1.24 侧栏区头筛选弹层：null=全部 / fast|deep 按 mode_summary 本地过滤 recent，
 * 独立于会话页 modeFilter）/ fetchSuggestPool（幂等拉取；经
 * src/api/suggestions.ts 唯一出口）。
 * 批量面（v1.23）：selectedIds（会话页勾选集，视图切换/搜索重置/重进页面清空）+
 * toggleSelect/setSelection/clearSelection + batchDelete/batchToggleArchive/batchMarkUnread
 * （逐个调用既有单线程动作——复用乐观自愈/回滚路径，部分失败汇总 toast「成功 X 失败 Y +
 * 失败明细」，成功清空选择；单动作增静默选项 silent/onError 供批量复用）。
 */
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { normalizeError } from '@/api/client'
import { fetchSuggestionPool } from '@/api/suggestions'
import { deleteThread, listThreads, patchThread } from '@/api/threads'
import { toast } from '@/composables/toast'
import { THREAD_TITLE_MAX_LENGTH } from '@/constants/ui'
import { i18n } from '@/i18n'
import type { ThreadListItem, ThreadModeSummary } from '@/types/api'

/** api.md §2.2：limit 默认 50（上限 200），offset 默认 0 */
const DEFAULT_LIMIT = 50
const DEFAULT_OFFSET = 0

/** 侧栏「最近」条数（frontend-api.md §1 侧栏行：GET /threads?limit=10） */
const RECENT_LIMIT = 10

/** api.md §2.2 v1.18：q 上限 200 字符（超长 400001——前端在收口处截断防御） */
const QUERY_MAX_LENGTH = 200

/** 三态视图（§5 ViewFilterRadio）：active=活跃集 / unread=未读 / archived=已归档 */
export type ThreadViewFilter = 'active' | 'unread' | 'archived'

export interface FetchThreadsOptions {
  limit?: number
  offset?: number
  /** 显式传 null = 不带 mode 参数（全部）；缺省 = 当前筛选 */
  mode?: ThreadModeSummary | null
}

export const useThreadListStore = defineStore('threadList', () => {
  const items = ref<ThreadListItem[]>([])
  const total = ref(0)
  const modeFilter = ref<ThreadModeSummary | null>(null)
  const viewFilter = ref<ThreadViewFilter>('active')
  const searchQuery = ref('')
  const loadingMore = ref(false)
  const loadMoreFailed = ref(false)
  /** 首屏在途守卫（P2-1）：fetchThreads/refresh/三 setter 的首拉期间置位——loadMore 吞掉，
   *  防止「在途首屏被代数守卫丢弃后 items 停在旧视图」或「loadMore append 到旧 items」 */
  const fetchingFirstPage = ref(false)
  /** 侧栏「最近」独立投影（P2-2）：fetchRecent 写入；管理动作双列表同步自愈 */
  const recent = ref<ThreadListItem[]>([])
  /** 侧栏「最近」筛选（v1.24 区头筛选弹层）：null=全部 / fast|deep=按 mode_summary 本地过滤
   *  recentFiltered（store 态——预览卡等消费方共享；独立于会话页 modeFilter，互不干扰） */
  const recentFilter = ref<ThreadModeSummary | null>(null)
  const recentFiltered = computed(() =>
    recentFilter.value === null
      ? recent.value
      : recent.value.filter((item) => item.mode_summary === recentFilter.value),
  )
  const suggestPool = ref<string[]>([])
  const suggestPoolLoaded = ref(false)
  /** 批量勾选集（v1.23）：会话页行级复选框驱动；替换式更新（每次重建 Set 保响应式）；
   *  视图切换/搜索重置/重进页面由 fetchFirstPage 收口清空 */
  const selectedIds = ref<Set<string>>(new Set())

  const hasMore = computed(() => items.value.length < total.value)

  /** 装配代数（P1 stale-response 守卫）：任何会使列表换血的请求先自增；
   *  慢到的旧代数响应整体丢弃（不覆盖新视图/新搜索的列表） */
  let fetchSeq = 0

  /** 单一收口的列表页请求：参数由 store 状态组装（mode/q/archived/unread AND 叠加）。
   *  返回 true=响应已应用；false=代数过期被丢弃（P2-1：调用方据此不置派生失败态） */
  async function fetchPage(options: {
    limit: number
    offset: number
    append: boolean
    modeOverride?: ThreadModeSummary | null
  }): Promise<boolean> {
    const seq = ++fetchSeq
    const params: Parameters<typeof listThreads>[0] = {
      limit: options.limit,
      offset: options.offset,
    }
    const mode = options.modeOverride !== undefined ? options.modeOverride : modeFilter.value
    if (mode !== null) params.mode = mode
    const q = searchQuery.value.trim().slice(0, QUERY_MAX_LENGTH)
    if (q !== '') params.q = q
    if (viewFilter.value === 'unread') params.unread = true
    else if (viewFilter.value === 'archived') params.archived = true
    const data = await listThreads(params)
    if (seq !== fetchSeq) return false // 代数守卫：过期响应丢弃
    total.value = data.total
    // 响应数据快照化（v1.25 修复）：逐项浅拷贝与数据源隔离——共享引用上乐观更新做元素
    // 替换（recent.value[idx] = {...}）会原地污染数据源对象（fixture/调用方缓存），跨 store
    // 实例串数据（置顶分区场景首次显性化：污染 pinned 字段后行「跳分区」）
    if (options.append) {
      const known = new Set(items.value.map((item) => item.thread_id))
      items.value = [
        ...items.value,
        ...data.items.filter((item) => !known.has(item.thread_id)).map((item) => ({ ...item })),
      ]
    } else {
      items.value = data.items.map((item) => ({ ...item }))
    }
    return true
  }

  /** 首屏在途包装（P2-1）：fetchingFirstPage 期间 loadMore 被吞 */
  async function fetchFirstPage(modeOverride?: ThreadModeSummary | null): Promise<void> {
    // 批量勾选随列表换血失效（v1.23）：视图切换/搜索重置/重进页面统一在此收口清空
    clearSelection()
    loadingMore.value = false
    loadMoreFailed.value = false
    fetchingFirstPage.value = true
    try {
      await fetchPage({ limit: DEFAULT_LIMIT, offset: DEFAULT_OFFSET, append: false, modeOverride })
    } finally {
      fetchingFirstPage.value = false
    }
  }

  async function fetchThreads(opts: FetchThreadsOptions = {}): Promise<void> {
    const offset = opts.offset ?? DEFAULT_OFFSET
    if (offset > 0) {
      // offset>0 = 累积 append（编程式翻页路径，与触底 loadMore 同族）
      await fetchPage({ limit: opts.limit ?? DEFAULT_LIMIT, offset, append: true, modeOverride: opts.mode })
      return
    }
    // 缺省/offset=0 = 替换语义（首屏与重置）
    await fetchFirstPage(opts.mode)
  }

  /** v1.21 语义钉正：重置分页态重拉首屏（新线程创建后由页面编排调用）；
   *  同时 best-effort 自愈侧栏 recent（P2-2：新线程随 refresh 进入「最近」；失败静默——
   *  侧栏为辅助导航，不放大主链路失败） */
  async function refresh(): Promise<void> {
    await Promise.all([
      fetchFirstPage(),
      fetchRecent().catch(() => undefined),
    ])
  }

  /** 触底自动加载（§5 ThreadsView 行 / D-15）：offset=items.length 追加；失败行内重试态。
   *  P2-1 守卫：①首屏在途直接吞掉（不产生「append 到旧 items / 与在途首屏竞态」）；
   *  ②失败时若代数已推进（在途期间发生视图切换），失败态归属被替代的旧请求——不写入新视图 */
  async function loadMore(): Promise<void> {
    if (loadingMore.value || fetchingFirstPage.value || !hasMore.value) return
    loadingMore.value = true
    loadMoreFailed.value = false
    const genBefore = fetchSeq
    try {
      await fetchPage({ limit: DEFAULT_LIMIT, offset: items.value.length, append: true })
    } catch {
      // 行内「加载失败·重试」——不 toast（D-15）；代数过期（视图已切换）不置失败态
      if (fetchSeq === genBefore + 1) loadMoreFailed.value = true
    } finally {
      loadingMore.value = false
    }
  }

  /** 三态视图切换（重置分页+首拉；同值 no-op 防重复请求；失败回滚选中态并重新抛出——P2-4） */
  async function setViewFilter(filter: ThreadViewFilter): Promise<void> {
    if (viewFilter.value === filter) return
    const prev = viewFilter.value
    viewFilter.value = filter
    try {
      await fetchFirstPage()
    } catch (error) {
      viewFilter.value = prev
      throw error
    }
  }

  async function setModeFilter(mode: ThreadModeSummary | null): Promise<void> {
    if (modeFilter.value === mode) return
    const prev = modeFilter.value
    modeFilter.value = mode
    try {
      await fetchFirstPage(mode)
    } catch (error) {
      modeFilter.value = prev
      throw error
    }
  }

  async function setSearchQuery(query: string): Promise<void> {
    const next = query.trim().slice(0, QUERY_MAX_LENGTH)
    if (searchQuery.value === next) return
    const prev = searchQuery.value
    searchQuery.value = next
    try {
      await fetchFirstPage()
    } catch (error) {
      searchQuery.value = prev
      throw error
    }
  }

  /* ---------- 管理动作五件（REQ-059）：乐观更新 + 失败回滚 + toast；成功响应回填 ----------
   * P2-2：items（会话页当前视图）与 recent（侧栏最近）双列表同步——同一动作在两列表内
   * 各自乐观更新/回滚/回填，侧栏随 store 自愈且不受会话页视图切换影响。
   * v1.23：markUnread/toggleArchive/deleteThread 增静默选项（silent=失败不 toast、
   * onError 透传服务端 message）并返回布尔成功态——批量动作复用既有乐观自愈路径。 */

  /** 单线程动作静默选项（批量复用；缺省 undefined = 既有行为不变） */
  interface MutateOpts {
    /** 失败不 toast（批量场景由汇总 toast 统一反馈） */
    silent?: boolean
    /** 失败 message 透传通道（批量汇总「失败明细」收集用） */
    onError?: (message: string) => void
  }

  /* ---------- 批量选择（v1.23）：勾选集维护（替换式更新保响应式） ---------- */

  function toggleSelect(threadId: string): void {
    const next = new Set(selectedIds.value)
    if (next.has(threadId)) next.delete(threadId)
    else next.add(threadId)
    selectedIds.value = next
  }

  /** 表头全选：整组替换为传入 id 集（会话页传当前视图全量条目 id） */
  function setSelection(threadIds: string[]): void {
    selectedIds.value = new Set(threadIds)
  }

  function clearSelection(): void {
    selectedIds.value = new Set()
  }

  /** 单条目从勾选集剔除（替换式更新；评审 P2：乐观移除路径——单线程删除/归档与批量同源，
   *  否则勾选态下经菜单删除 → 批量计数虚高 → runBatch 对幽灵 id 静默计入失败） */
  function removeFromSelection(threadId: string): void {
    if (!selectedIds.value.has(threadId)) return
    const next = new Set(selectedIds.value)
    next.delete(threadId)
    selectedIds.value = next
  }

  /** 双列表快照（回滚单位；recent 无 total 语义） */
  interface ListSnapshot {
    items: ThreadListItem[]
    total: number
    recent: ThreadListItem[]
  }

  function snapshot(): ListSnapshot {
    return { items: [...items.value], total: total.value, recent: [...recent.value] }
  }

  function rollback(snap: ListSnapshot): void {
    items.value = snap.items
    total.value = snap.total
    recent.value = snap.recent
  }

  function indexOfItem(threadId: string): number {
    return items.value.findIndex((item) => item.thread_id === threadId)
  }

  function indexOfRecent(threadId: string): number {
    return recent.value.findIndex((item) => item.thread_id === threadId)
  }

  /** PATCH 响应（=更新后投影）回填条目（api.md §2.10；items 与 recent 双列表）。
   *  拷贝式元素替换（评审 P3 约定钉正）：同一响应对象不得写入两列表形成内部共享——
   *  本 store 所有列表变异一律「逐元素拷贝替换」（fetchPage/fetchRecent 同款），未来任何
   *  直接属性写（list[i].x = v）都会穿透共享引用，禁止。 */
  function applyPatchResponse(data: ThreadListItem): void {
    const idx = indexOfItem(data.thread_id)
    if (idx >= 0) items.value[idx] = { ...data }
    const ridx = indexOfRecent(data.thread_id)
    if (ridx >= 0) recent.value[ridx] = { ...data }
  }

  /** 活跃视图排序（api.md §2.2 v1.18：pinned DESC, updated_at DESC——服务端同口径）；
   *  Array.sort 稳定：同 pinned 同刻的条目保持原相对序 */
  function sortByActiveOrder(): void {
    items.value.sort((a, b) => {
      if (a.pinned !== b.pinned) return Number(b.pinned) - Number(a.pinned)
      const ta = new Date(a.updated_at).getTime()
      const tb = new Date(b.updated_at).getTime()
      if (tb !== ta) return tb - ta
      return 0
    })
  }

  function sortRecentByActiveOrder(): void {
    recent.value.sort((a, b) => {
      if (a.pinned !== b.pinned) return Number(b.pinned) - Number(a.pinned)
      const ta = new Date(a.updated_at).getTime()
      const tb = new Date(b.updated_at).getTime()
      if (tb !== ta) return tb - ta
      return 0
    })
  }

  async function renameThread(threadId: string, title: string): Promise<void> {
    const trimmed = title.trim()
    // 前端先校验（1~120 字符；服务端 400001 兜底，api.md §2.10）
    if (trimmed === '' || trimmed.length > THREAD_TITLE_MAX_LENGTH) return
    const idx = indexOfItem(threadId)
    const ridx = indexOfRecent(threadId)
    if (idx < 0 && ridx < 0) return
    const snap = snapshot()
    if (idx >= 0) items.value[idx] = { ...items.value[idx]!, title: trimmed }
    if (ridx >= 0) recent.value[ridx] = { ...recent.value[ridx]!, title: trimmed }
    try {
      applyPatchResponse(await patchThread(threadId, { title: trimmed }))
    } catch (error) {
      rollback(snap)
      toast(normalizeError(error).message)
    }
  }

  async function togglePin(threadId: string): Promise<void> {
    const idx = indexOfItem(threadId)
    const ridx = indexOfRecent(threadId)
    if (idx < 0 && ridx < 0) return
    const current = (idx >= 0 ? items.value[idx] : recent.value[ridx])!
    const next = !current.pinned
    const snap = snapshot()
    if (idx >= 0) items.value[idx] = { ...items.value[idx]!, pinned: next }
    if (ridx >= 0) recent.value[ridx] = { ...recent.value[ridx]!, pinned: next }
    if (viewFilter.value !== 'archived') sortByActiveOrder()
    sortRecentByActiveOrder()
    try {
      applyPatchResponse(await patchThread(threadId, { pinned: next }))
    } catch (error) {
      rollback(snap)
      toast(normalizeError(error).message)
    }
  }

  /** is_unread 双向落点（v1.24）：unread=true=标记为未读 / false=标记为已读（菜单未读项
   *  切回已读；服务端 §2.10 同语义端点，零后端改动）——乐观更新+回滚+toast 同族口径 */
  async function setUnread(threadId: string, unread: boolean, opts?: MutateOpts): Promise<boolean> {
    const idx = indexOfItem(threadId)
    const ridx = indexOfRecent(threadId)
    if (idx < 0 && ridx < 0) return false
    const snap = snapshot()
    if (idx >= 0) items.value[idx] = { ...items.value[idx]!, is_unread: unread }
    if (ridx >= 0) recent.value[ridx] = { ...recent.value[ridx]!, is_unread: unread }
    try {
      applyPatchResponse(await patchThread(threadId, { is_unread: unread }))
      return true
    } catch (error) {
      rollback(snap)
      const message = normalizeError(error).message
      opts?.onError?.(message)
      if (!opts?.silent) toast(message)
      return false
    }
  }

  function markUnread(threadId: string, opts?: MutateOpts): Promise<boolean> {
    return setUnread(threadId, true, opts)
  }

  /** 标记为已读（v1.24 ThreadActionMenu 未读状态感知）：PATCH is_unread:false */
  function markRead(threadId: string, opts?: MutateOpts): Promise<boolean> {
    return setUnread(threadId, false, opts)
  }

  /** 归档/取消归档：线程在互斥视图间移动——乐观移出当前视图 + 侧栏 recent；
   *  total-1 仅当条目在当前过滤 items 中（P3-1：目标只在 recent 时 hasMore 口径不漂移） */
  async function toggleArchive(threadId: string, opts?: MutateOpts): Promise<boolean> {
    const idx = indexOfItem(threadId)
    const ridx = indexOfRecent(threadId)
    if (idx < 0 && ridx < 0) return false
    const current = (idx >= 0 ? items.value[idx] : recent.value[ridx])!
    const next = !current.archived
    const snap = snapshot()
    items.value = items.value.filter((item) => item.thread_id !== threadId)
    recent.value = recent.value.filter((item) => item.thread_id !== threadId)
    if (idx >= 0) total.value = Math.max(0, total.value - 1)
    removeFromSelection(threadId)
    try {
      await patchThread(threadId, { archived: next })
      return true
    } catch (error) {
      rollback(snap)
      const message = normalizeError(error).message
      opts?.onError?.(message)
      if (!opts?.silent) toast(message)
      return false
    }
  }

  /** 物理删除（api.md §2.11；二次确认前置由菜单组件负责）：乐观移除（items+recent）；
   *  total-1 仅当条目在当前过滤 items 中（P3-1）；409005（有进行中轮次）→ toast 且行保留 */
  async function deleteThreadById(threadId: string, opts?: MutateOpts): Promise<boolean> {
    const idx = indexOfItem(threadId)
    const ridx = indexOfRecent(threadId)
    if (idx < 0 && ridx < 0) return false
    const snap = snapshot()
    items.value = items.value.filter((item) => item.thread_id !== threadId)
    recent.value = recent.value.filter((item) => item.thread_id !== threadId)
    if (idx >= 0) total.value = Math.max(0, total.value - 1)
    removeFromSelection(threadId)
    try {
      await deleteThread(threadId)
      return true
    } catch (error) {
      rollback(snap)
      const message = normalizeError(error).message
      opts?.onError?.(message)
      if (!opts?.silent) toast(message)
      return false
    }
  }

  /* ---------- 批量动作三件（v1.23）：循环既有单线程动作 + 部分失败汇总 toast ----------
   * 工单口径为「并发收敛 Promise.allSettled」，实现按**顺序逐个**执行（实现裁决）：单动作的
   * 乐观回滚单位是整表快照，同一 tick 并发时先发者的回滚会复活后发者的乐观删除/total 收口
   * （互相踩踏），顺序执行则每步都建立在前面已落定的状态上、复用既有自愈路径零改动。
   * 汇总语义与 allSettled 口径一致：逐项收集成功数与失败明细（去重拼接），部分失败时
   * toast「成功 X 失败 Y：明细」，全部成功静默；无论成败收尾清空勾选集。 */
  async function runBatch(
    ids: string[],
    run: (id: string, opts: MutateOpts) => Promise<boolean>,
  ): Promise<void> {
    let ok = 0
    const failures: string[] = []
    for (const id of ids) {
      const done = await run(id, { silent: true, onError: (message) => failures.push(message) })
      if (done) ok += 1
    }
    clearSelection()
    if (failures.length > 0) {
      const detail = [...new Set(failures)].join('；')
      toast(i18n.global.t('threads.batchResult', { ok, fail: ids.length - ok, detail }))
    }
  }

  function batchDelete(threadIds: string[]): Promise<void> {
    return runBatch(threadIds, deleteThreadById)
  }

  function batchToggleArchive(threadIds: string[]): Promise<void> {
    return runBatch(threadIds, toggleArchive)
  }

  function batchMarkUnread(threadIds: string[]): Promise<void> {
    return runBatch(threadIds, markUnread)
  }

  /** 打开线程详情后本地清未读点（api.md §2.3 v1.18：GET 详情幂等置已读——items+recent 双列表） */
  function markReadLocally(threadId: string): void {
    const idx = indexOfItem(threadId)
    if (idx >= 0) items.value[idx] = { ...items.value[idx]!, is_unread: false }
    const ridx = indexOfRecent(threadId)
    if (ridx >= 0) recent.value[ridx] = { ...recent.value[ridx]!, is_unread: false }
  }

  /** 侧栏「最近」拉取（§1 侧栏行：limit=10、archived 缺省 false=活跃集）——独立于
   *  items 分页态（viewFilter/searchQuery 不影响）；App 挂载时拉取一次。
   *  响应数据快照化（v1.25 修复，同 fetchPage——与数据源共享可变引用会被乐观元素替换污染） */
  async function fetchRecent(): Promise<void> {
    const data = await listThreads({ limit: RECENT_LIMIT, offset: DEFAULT_OFFSET })
    recent.value = data.items.map((item) => ({ ...item }))
  }

  /** 侧栏最近筛选（v1.24 弹层选项落点）：纯本地投影切换，零网络 */
  function setRecentFilter(mode: ThreadModeSummary | null): void {
    recentFilter.value = mode
  }

  /** 首页推荐池幂等拉取（v1.10）：已 loaded 直接返回；失败静默置空、不置 loaded、不 toast */
  async function fetchSuggestPool(): Promise<void> {
    if (suggestPoolLoaded.value) return
    try {
      const data = await fetchSuggestionPool()
      suggestPool.value = data.items
      suggestPoolLoaded.value = true
    } catch {
      suggestPool.value = []
      suggestPoolLoaded.value = false
    }
  }

  return {
    items,
    total,
    modeFilter,
    viewFilter,
    searchQuery,
    loadingMore,
    loadMoreFailed,
    fetchingFirstPage,
    hasMore,
    recent,
    recentFilter,
    recentFiltered,
    setRecentFilter,
    suggestPool,
    suggestPoolLoaded,
    selectedIds,
    fetchThreads,
    refresh,
    loadMore,
    setViewFilter,
    setModeFilter,
    setSearchQuery,
    renameThread,
    togglePin,
    markUnread,
    markRead,
    toggleArchive,
    deleteThread: deleteThreadById,
    toggleSelect,
    setSelection,
    clearSelection,
    batchDelete,
    batchToggleArchive,
    batchMarkUnread,
    markReadLocally,
    fetchRecent,
    fetchSuggestPool,
  }
})
