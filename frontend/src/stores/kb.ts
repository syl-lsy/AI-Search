/**
 * src/stores/kb.ts — kbStore（frontend-api.md §4 kbStore 行；setup store，§8.2；plan-m3-b6-f6 §2.2）。
 *
 * 状态：文档列表（页码分页形态——R-3 用户裁决：currentPage 当前页 + 整页替换语义）、
 *       docsTotal/readyTotal（计数投影：envelope total / status=ready 条件请求 total——api.md §4.3）、
 *       totalPages（ceil(docsTotal/pageSize)）/loadingPage（翻页/刷新防重入位）、
 *       loading（首屏主区加载位）、轮询定时器（store 内部句柄）、
 *       importJobs（§4.8 列表投影——FeishuImportJobs 数据源）、
 *       importJobDetail（§4.9 详情——failures 清单仅详情端点承载）、
 *       importTriggering（triggerImport 防重入位）。
 * 动作：upload(file)/collectUrl(url)/remove(id)/fetchDocuments()（回第 1 页）/goToPage(p)（页码翻页）/
 *       pollUntilSettled()/stopPolling()
 *       + triggerImport()/fetchImportJobs()/fetchImportJobDetail(jobId)/clearImportJobDetail()。
 * - REST 全部经 src/api/kb.ts 唯一出口（frontend-api.md §4 kbStore 行钉定）；ApiError 上抛由视图 toast（§1）；
 * - 删除二次确认归视图层（api.md §4.5「前端删除动作二次确认并提示级联范围」）——store 只管 DELETE + 当前页刷新；
 * - 单 tick 双条件轮询（plan §2.2 钉定形态）：api.md §4.3「每 5s 轮询」+ §4.8「进行中任务每 5s 轮询沿列表节律」
 *   同一节拍承载两条件，单 setTimeout 链式自排、幂等不重复排；tick 文档腿=当前页重拉（refreshCurrentPage）。
 */
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import * as kbApi from '@/api/kb'
import type { KbDeleteData, KbDocument, KbImportJob, KbImportJobDetail, KbImportTriggerData } from '@/types/api'

/**
 * 轮询间隔 5s —— api.md §4.3「前端对非终态条目每 5s 轮询本端点」契约钉定值
 * （§4.8「进行中任务每 5s 轮询沿列表节律」同节拍；非前端自由裁量）。
 */
const POLL_INTERVAL_MS = 5_000

/** §4.3 每页 50（`?limit=50&offset=0`——api.md §4.3 缺省形态；threads §2.2 每页 50 同款先例） */
const PAGE_SIZE = 50

/** §4.3 六态状态机（api.md v1.30 REQ-052）：pending/normalizing/chunking/indexing 非终态；ready/failed 终态 */
const NON_TERMINAL: readonly KbDocument['status'][] = ['pending', 'normalizing', 'chunking', 'indexing']

/** §4.8/§4.9 任务 status 枚举中的进行中值（§4.7 409007「已有进行中任务」同族判据口径） */
const ACTIVE_JOB_STATUSES: readonly KbImportJob['status'][] = ['pending', 'running']

export const useKbStore = defineStore('kb', () => {
  const documents = ref<KbDocument[]>([])
  const docsTotal = ref(0)
  const readyTotal = ref(0)
  const loading = ref(false)
  const loadingPage = ref(false)
  const currentPage = ref(1)
  const importJobs = ref<KbImportJob[]>([])
  const importJobDetail = ref<KbImportJobDetail | null>(null)
  const importTriggering = ref(false)
  /** 总页数（页码分页控件数据源）：ceil(docsTotal/pageSize)；空库=1 页（钳制下界） */
  const totalPages = computed(() => Math.max(1, Math.ceil(docsTotal.value / PAGE_SIZE)))
  let pollTimer: ReturnType<typeof setTimeout> | null = null
  /**
   * 停止标志（CR-1/P2-1 卸载竞态收口）：stopPolling 置位后，一切在途 fetch 落地触发的
   * schedulePollIfPending（fetchImportJobs 完成回调 / tick 尾部自排）一律不再重排定时器——
   * 「页面卸载停表」钉定语义；重进页面 pollUntilSettled 首行复位恢复调度。
   */
  let pollStopped = false

  function hasPendingDocs(): boolean {
    return documents.value.some((d) => NON_TERMINAL.includes(d.status))
  }

  function hasActiveImportJobs(): boolean {
    return importJobs.value.some((j) => ACTIVE_JOB_STATUSES.includes(j.status))
  }

  /**
   * 当前页拉取内核（R-3 页替换语义）：limit=50&offset=(currentPage-1)*50 整页替换 +
   * docsTotal/readyTotal 计数并发投影；loadingPage 防重入（tick 在翻页在途时跳过，下 tick 自愈）；
   * 删除致当前页超出末页（空页）→ 自愈回退末页重拉一次（末页必非空）。
   */
  async function refreshCurrentPage(): Promise<void> {
    if (loadingPage.value) return
    loadingPage.value = true
    try {
      const offset = (currentPage.value - 1) * PAGE_SIZE
      const [data, readyData] = await Promise.all([
        kbApi.fetchDocuments({ limit: PAGE_SIZE, offset }),
        kbApi.fetchDocuments({ limit: 1, status: 'ready' }),
      ])
      documents.value = data.items
      docsTotal.value = data.total
      readyTotal.value = readyData.total
      if (data.items.length === 0 && docsTotal.value > 0 && currentPage.value > 1) {
        currentPage.value = totalPages.value
        loadingPage.value = false
        await refreshCurrentPage()
        return
      }
    } finally {
      loadingPage.value = false
    }
  }

  /** 首屏/重置：回第 1 页（新内容置顶可见）+ 计数投影 */
  async function fetchDocuments(): Promise<void> {
    currentPage.value = 1
    loading.value = true
    try {
      await refreshCurrentPage()
    } finally {
      loading.value = false
    }
  }

  /** 页码翻页（R-3 分页控件动作）：边界钳制 1..totalPages、同页 no-op、防重入；错误上抛视图 toast。
   *  落地后按需排表（幂等，fetchImportJobs 同款收口）——翻入含非终态行的页时轮询须在跑。 */
  async function goToPage(page: number): Promise<void> {
    const target = Math.min(Math.max(1, Math.floor(page)), totalPages.value)
    if (loadingPage.value || target === currentPage.value) return
    currentPage.value = target
    await refreshCurrentPage()
    schedulePollIfPending()
  }

  /**
   * §4.8 任务列表（替换式更新——FeishuImportJobs 数据源 + 进行中任务投影）。
   * 载入后按需排表（幂等）：挂载竞态收口——FeishuImportJobs onMounted 先于
   * LibraryView pollUntilSettled 载入进行中任务时，任务条件不因轮询器已判空而漏排。
   */
  async function fetchImportJobs(): Promise<void> {
    const data = await kbApi.fetchImportJobs()
    importJobs.value = data.items
    schedulePollIfPending()
  }

  /** §4.9 详情请求序号（R-2/F-1 失配丢弃）：发起 +1；收起（clearImportJobDetail）亦 +1 使在途请求全部失配——
   *  落地时序号非最新 → 不写回（否则：收起后响应回写会重新点亮 tick 判据每 5s 续拉已收起 job；
   *  旧任务响应晚到会覆盖新任务详情串卡）。 */
  let detailReqSeq = 0

  /** §4.9 详情（failures 清单展开时调用；展开态随轮询节律刷新——tick 按 importJobDetail 存否判断；
   *  响应落地按序号失配丢弃，收起/更新请求后的在途响应不写回） */
  async function fetchImportJobDetail(jobId: string): Promise<void> {
    const reqId = ++detailReqSeq
    const data = await kbApi.fetchImportJobDetail(jobId)
    if (reqId !== detailReqSeq) return
    importJobDetail.value = data
  }

  /** 收起详情（R-4/F-7 内聚动作）：置空 + 序号 +1 失配全部在途详情请求；组件不直写 store 字段 */
  function clearImportJobDetail(): void {
    detailReqSeq++
    importJobDetail.value = null
  }

  async function upload(file: File, title: string): Promise<KbDocument> {
    const doc = await kbApi.uploadDocument(file, title)
    await fetchDocuments()
    schedulePollIfPending()
    return doc
  }

  async function collectUrl(url: string): Promise<KbDocument> {
    const doc = await kbApi.collectUrl(url)
    await fetchDocuments()
    schedulePollIfPending()
    return doc
  }

  async function remove(id: string): Promise<KbDeleteData> {
    const data = await kbApi.removeDocument(id)
    // 留当前页刷新（页码分页语义——跨页删除不把用户拽回第 1 页）；当前页删空时
    // refreshCurrentPage 内部自愈回退末页
    await refreshCurrentPage()
    return data
  }

  /** §4.7 手动触发：成功 → 立即拉任务列表 + 启动轮询；ApiError（409007 等）上抛由视图 toast（不吞） */
  async function triggerImport(): Promise<KbImportTriggerData> {
    importTriggering.value = true
    try {
      const data = await kbApi.triggerImport()
      await fetchImportJobs()
      schedulePollIfPending()
      return data
    } finally {
      importTriggering.value = false
    }
  }

  /**
   * 单 tick 双条件（plan §2.2 钉定）：文档有非终态 → refreshCurrentPage()（R-3：当前页重拉推进可见行）；
   * 任务有进行中 → fetchImportJobs() + 展开中详情同节拍刷新（importJobDetail 非空 → §4.9 同 job_id）；
   * 双条件皆空 → 停表。单 setTimeout 链式自排（幂等：已有定时器不重复排）；腿间失败互不遮蔽（allSettled——单腿瞬态错误不杀链）。
   */
  function schedulePollIfPending(): void {
    if (pollStopped || pollTimer !== null || !(hasPendingDocs() || hasActiveImportJobs())) return
    pollTimer = setTimeout(async () => {
      pollTimer = null
      const tickJobs: Array<Promise<unknown>> = []
      if (hasPendingDocs()) tickJobs.push(refreshCurrentPage())
      if (hasActiveImportJobs()) {
        tickJobs.push(fetchImportJobs())
        const expanded = importJobDetail.value
        if (expanded !== null) tickJobs.push(fetchImportJobDetail(expanded.job_id))
      }
      await Promise.allSettled(tickJobs)
      schedulePollIfPending()
    }, POLL_INTERVAL_MS)
  }

  /** 视图挂载后调用：首拉 + 按需启动轮询（重进页面重拉重启——既有语义；首行复位 CR-1 停止标志） */
  async function pollUntilSettled(): Promise<void> {
    pollStopped = false
    await fetchDocuments()
    schedulePollIfPending()
  }

  /** 视图卸载时停表（轮询器为 store 内部句柄，不随组件卸载自动清理；置标志拦在途 fetch 落地后的重排） */
  function stopPolling(): void {
    pollStopped = true
    if (pollTimer !== null) {
      clearTimeout(pollTimer)
      pollTimer = null
    }
  }

  return {
    documents,
    docsTotal,
    readyTotal,
    loading,
    loadingPage,
    currentPage,
    totalPages,
    importJobs,
    importJobDetail,
    importTriggering,
    fetchDocuments,
    goToPage,
    upload,
    collectUrl,
    remove,
    triggerImport,
    fetchImportJobs,
    fetchImportJobDetail,
    clearImportJobDetail,
    pollUntilSettled,
    stopPolling,
  }
})
