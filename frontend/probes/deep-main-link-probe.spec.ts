/**
 * e2e 活体探针①——deep 主链路真浏览器真轮（批次 6 活体验证）。
 *
 * 归属：docs/m2-batch6-live-20261121.md「探针一组」。与实现面 deep-artifacts.spec.ts 分离：
 * 本 spec 是活体探针工件（真轮一次性、证据落盘），不是回归保护面；实现面套件的断言口径
 * 以其文件头注记为准。零 git 写操作由运行纪律保证（本文件只写 docs/design/e2e-batch6/ 工件）。
 *
 * 落点注记（批次 6 收口轮 P2-2，2026-11-21）：本文件已迁出回归 testDir（test/e2e/）至
 * frontend/probes/——常规 playwright 全量（testDir=./test/e2e）不再收集本探针；单独运行 =
 * `pnpm --filter frontend exec playwright test --config playwright.probes.config.ts`
 * （探针专用 config，testDir=./probes）。实现面回归套件 deep-artifacts.spec.ts 留在
 * testDir 不动；docs/design/e2e-batch6/ 工件归档不动。
 *
 * 断言面（探针一组钉点）：
 *   A. §7.4 深度序列渲染：stage(plan)→search→[read]→reflect≥1→write→done(completed)——
 *      经窗口内 EventSource 包装器捕获原始 SSE 帧（stage/token/sources/done 全量留痕），
 *      首次出现序断言 plan<search<reflect<write、reflect running≥1、done.finish=completed；
 *      read 为数据依赖事件面（§7.4 典型序列非恒有段）——在场则断 search<read<write，缺席如实注记。
 *   B. 分节先完先读：stage(write,running) 期间采样渲染正文前缀，终态后断言最终正文含该前缀
 *      （分节流式渐进渲染、非终态一次性倾倒）。
 *   C. TOC 锚点可跳转：①renderer 注入标题 id（h1~h6 全级，getElementById 可解析）；
 *      ②OutlineAnchor 右缘导航轨点击 → 滚动容器 scrollTop 变化（锚点跳转行为面）。
 *      注：OutlineAnchor 条目=问题块（2026-11-14 修复项③口径），与 frontend-api.md §5 行
 *      「深度报告按 h2 拆分」的旧措辞存在契约漂移——探针按实现现状断言，漂移另行留痕评审。
 *   D. [n]↔来源面板联动：正文 sup.cite[data-ref-no] 全集 ⊆ 来源卡 ref_no 全集（批次 5 修复回归）；
 *      点击 [n] → 组作用域定位卡 cite-hit 高亮（1.6s 窗口内断言）。
 *   E. done↔REST 对账：done 帧（finish/degraded/degraded_reason/source_count/duration_ms/usage）
 *      与 GET /api/v1/turns/{id}（§2.5）逐键一致；SSE sources ref_no 首现冻结无重复；
 *      REST ref_no 1..N 连续；export_available=true。
 *
 * 工件落盘：docs/design/e2e-batch6/p1-0*.png（关键态截图）+ p1-sse-summary.json +
 * p1-reconcile.json（SSE 序列摘要与对账明细——证据文档引用）。
 */
import { mkdirSync, writeFileSync } from 'node:fs'
import path from 'node:path'

import { expect, test, type Page } from '@playwright/test'

const SHOT_DIR = path.resolve(process.cwd(), '..', 'docs', 'design', 'e2e-batch6')
const API_BASE = 'http://127.0.0.1:8300'

/** 收敛型单面中文题（deep 轮题面——单面题避免多面压力熔断；与实现面套件题面错开） */
const DEEP_QUESTION = 'Ethereum 的账户模型与比特币的 UTXO 模型有什么区别？'

/* ---------- 类型守卫（§8.9 类型盲区纪律：守卫收窄，禁 as 双重断言） ---------- */

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null
}
function str(v: unknown): string | null {
  return typeof v === 'string' ? v : null
}
function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

interface ProbeEvent {
  event: string
  id: string | null
  data: Record<string, unknown> | null
}

function isProbeEventArray(v: unknown): v is ProbeEvent[] {
  return Array.isArray(v) && v.every((e) => isRecord(e) && typeof e['event'] === 'string')
}

/** 读取窗口内 SSE 探针日志（addInitScript 包装 EventSource 落下的帧） */
async function probeLog(page: Page): Promise<ProbeEvent[]> {
  const raw: unknown = await page.evaluate(() => (window as { __probeSSE?: unknown }).__probeSSE)
  return isProbeEventArray(raw) ? raw : []
}

/**
 * EventSource 包装器：具名帧全量捕获进 window.__probeSSE（token/thinking 截断 delta 防爆内存）。
 * 形态注记：addInitScript 裸字符串 = 按语句脚本执行（非回调）——内容必须是语句序列，
 * 不得写成箭头函数表达式（表达式只求值不调用、静默不装——首跑实证的空日志根因）。
 */
const SSE_PROBE_INIT = `
  const log = []
  window.__probeSSE = log
  const Native = window.EventSource
  const NAMES = ['stage','token','thinking','sources','images','done','followups','error','context_compact','memory']
  class ProbeES extends Native {
    constructor(url, opts) {
      super(url, opts)
      for (const name of NAMES) {
        this.addEventListener(name, (e) => {
          let data = null
          try { data = JSON.parse(e.data) } catch { data = e.data }
          if ((name === 'token' || name === 'thinking') && data && typeof data.delta === 'string') {
            data = { seq: data.seq, delta_len: data.delta.length, delta_head: data.delta.slice(0, 60) }
          }
          log.push({ event: name, id: e.lastEventId ?? null, data })
        })
      }
    }
  }
  window.EventSource = ProbeES
`

test('探针一组：deep 主链路真浏览器真轮——§7.4 序列 + 分节先完先读 + TOC 锚点 + [n] 联动 + done↔REST 对账', async ({ page, request }) => {
  test.setTimeout(600_000)
  mkdirSync(SHOT_DIR, { recursive: true })

  const consoleErrors: string[] = []
  page.on('console', (m) => {
    if (m.type() === 'error') consoleErrors.push(m.text())
  })

  await page.addInitScript(SSE_PROBE_INIT)

  /* ---------- 建轮：首页发起 deep 单面题 ---------- */
  await page.goto('/')
  await page.locator('[data-testid="home-search-card"] textarea').fill(DEEP_QUESTION)
  await page.getByTestId('mode-deep').click()
  await page.locator('[data-testid="home-search-card"] [data-testid="submit-btn"]').click()
  await page.waitForURL(/\/thread\//, { timeout: 15_000 })
  const threadUrl = page.url()
  const threadId = threadUrl.split('/thread/')[1] ?? ''
  expect(threadId, '线程 URL 应含 thread id').not.toBe('')

  // 哨兵：包装器必须在（2s 内显式失败，避免空日志盲等 480s——首跑实证根因防线）
  const sentinelInstalled = await page.evaluate(() => Array.isArray((window as { __probeSSE?: unknown }).__probeSSE))
  expect(sentinelInstalled, 'SSE 包装器未安装（__probeSSE 缺失）——addInitScript 内容形态错误').toBe(true)

  /* ---------- 流式进行中：deep 徽标 + 时间线 ---------- */
  await expect(page.getByTestId('turn-block').first()).toBeVisible({ timeout: 15_000 })
  await expect(page.locator('[data-testid="turn-block"] .badge-outline').first()).toBeVisible()
  await expect(page.getByTestId('timeline').first()).toBeVisible({ timeout: 60_000 })

  /* ---------- 等真轮推进：分段截图 + write 期正文前缀采样（480s 宽窗） ---------- */
  const answerCol = page.locator('[data-testid="turn-answer"]').first()
  let sample: { head: string; len: number } | null = null
  let doneEv: ProbeEvent | null = null
  const shotEarly = { done: false }
  const shotReflect = { done: false }
  const shotWrite = { done: false }
  const t0 = Date.now()

  while (Date.now() - t0 < 480_000) {
    const log = await probeLog(page)
    doneEv = log.find((e) => e.event === 'done') ?? null

    // p1-01：search 段进入后（时间线在渲染、来源面板可能出现）
    if (!shotEarly.done && log.some((e) => e.event === 'stage' && e.data?.['stage_key'] === 'search')) {
      shotEarly.done = true
      await page.screenshot({ path: path.join(SHOT_DIR, 'p1-01-deep-streaming-search.png') })
    }
    // p1-02：reflect 段出现瞬间（时间线含反思节点）
    if (!shotReflect.done && log.some((e) => e.event === 'stage' && e.data?.['stage_key'] === 'reflect')) {
      shotReflect.done = true
      await page.screenshot({ path: path.join(SHOT_DIR, 'p1-02-deep-reflect-stage.png') })
    }
    // p1-03：write 流式期正文渐进渲染采样（分节先完先读证据）
    if (!shotWrite.done && sample === null) {
      const writeRunning = log.some(
        (e) => e.event === 'stage' && e.data?.['stage_key'] === 'write' && e.data?.['status'] === 'running',
      )
      const tokenCount = log.filter((e) => e.event === 'token').length
      if (writeRunning && tokenCount > 20 && (await answerCol.count()) > 0) {
        const text = ((await answerCol.textContent()) ?? '').trim()
        if (text.length >= 150) {
          sample = { head: text.slice(0, 100), len: text.length }
          shotWrite.done = true
          await page.screenshot({ path: path.join(SHOT_DIR, 'p1-03-deep-write-streaming.png') })
        }
      }
    }
    if (doneEv !== null) break
    // 第二道哨兵：45s 仍零帧=捕获链路失效（非轮慢）——显式失败，防空日志盲等 480s
    if (Date.now() - t0 > 45_000 && log.length === 0) {
      throw new Error('45s 仍零 SSE 帧——EventSource 包装器未捕获（检查 addInitScript 安装与订阅链路）')
    }
    await page.waitForTimeout(2_000)
  }
  expect(doneEv, '480s 内未收到 done 帧——deep 轮未收敛').not.toBeNull()

  /* ---------- A. §7.4 深度序列断言（首次出现序 + reflect≥1 + done 终态） ---------- */
  const log = await probeLog(page)
  const stageSeq = log
    .filter((e) => e.event === 'stage')
    .map((e) => ({
      key: str(e.data?.['stage_key']),
      status: str(e.data?.['status']),
      extra: isRecord(e.data?.['extra']) ? (e.data?.['extra'] as Record<string, unknown>) : null,
    }))
    .filter((s): s is { key: string; status: string | null; extra: Record<string, unknown> | null } => s.key !== null)

  const firstIdx = (key: string): number => stageSeq.findIndex((s) => s.key === key)
  const idxPlan = firstIdx('plan')
  const idxSearch = firstIdx('search')
  const idxRead = firstIdx('read')
  const idxReflect = firstIdx('reflect')
  const idxWrite = firstIdx('write')
  expect(idxPlan, 'plan 段应在场（§7.4 首段）').toBeGreaterThanOrEqual(0)
  expect(idxSearch, 'search 段应在场').toBeGreaterThan(idxPlan)
  expect(idxReflect, 'reflect 段应在场').toBeGreaterThan(idxSearch)
  expect(idxWrite, 'write 段应在场且晚于 reflect').toBeGreaterThan(idxReflect)
  if (idxRead >= 0) {
    expect(idxRead, 'read 在场时应位于 search 与 write 之间（数据依赖段）').toBeGreaterThan(idxSearch)
    expect(idxRead).toBeLessThan(idxWrite)
  }
  const reflectRunningCount = stageSeq.filter((s) => s.key === 'reflect' && s.status === 'running').length
  expect(reflectRunningCount, 'reflect running ≥1（深度反思回路）').toBeGreaterThanOrEqual(1)

  // done 帧：finish=completed（探针一组钉点=等真 completed）+ 最后事件
  const doneData = doneEv?.data ?? null
  expect(isRecord(doneData), 'done 帧应有 payload').toBe(true)
  const finish = str(doneData?.['finish'])
  expect(finish, 'done.finish 应为 completed（真 completed 轮）').toBe('completed')
  expect(log[log.length - 1]?.event, 'done 应为通道最后一个事件（deep 无 followups）').toBe('done')
  expect(log.some((e) => e.event === 'error'), '零 error 事件').toBe(false)

  // plan done extra.subtopics（§7.4 典型序列第二段）
  const planDone = stageSeq.find((s) => s.key === 'plan' && s.status === 'done')
  const subtopics = num(planDone?.extra?.['subtopics'])
  expect(subtopics, 'stage(plan,done).extra.subtopics ≥1').not.toBeNull()
  expect(subtopics).toBeGreaterThanOrEqual(1)

  // write done extra.sections（§7.4 分节数留痕）
  const writeDone = stageSeq.find((s) => s.key === 'write' && s.status === 'done')
  const sections = num(writeDone?.extra?.['sections'])

  // turn_id（REST 对账键，取自首个 SSE 帧载荷）
  const turnId = str(log.find((e) => isRecord(e.data) && typeof e.data['turn_id'] === 'string')?.data?.['turn_id'])
  expect(turnId, 'SSE 帧应携带 turn_id').not.toBeNull()

  /* ---------- B. 分节先完先读：终态正文含 write 期采样前缀 ---------- */
  if (sample === null) {
    throw new Error('write 流式期未完成正文前缀采样（write running 且 token>20 时正文未达 150 字符）')
  }
  const finalText = ((await answerCol.textContent()) ?? '').trim()
  expect(finalText.length, '终态正文非空').toBeGreaterThan(0)
  expect(finalText.includes(sample.head), '终态正文包含 write 期采样前缀（分节渐进渲染，非终态倾倒）').toBe(true)

  /* ---------- 终态 DOM：状态行=已完成、无降级横幅 ---------- */
  await expect(page.getByTestId('turn-status').first()).toContainText('已完成', { timeout: 10_000 })
  expect(await page.getByTestId('degraded-banner').count(), 'completed 轮零降级横幅').toBe(0)

  /* ---------- C. TOC 锚点：标题 id 注入 + 右缘导航轨点击跳转 ---------- */
  const headingIds = await answerCol.locator('h1[id], h2[id], h3[id], h4[id]').evaluateAll((els) =>
    els.map((el) => el.getAttribute('id')).filter((v): v is string => typeof v === 'string'),
  )
  expect(headingIds.length, '深度报告标题 id 注入 ≥2（renderer TOC 标题 id 机制）').toBeGreaterThanOrEqual(2)
  const allResolvable = await page.evaluate(
    (ids: string[]) => ids.every((id) => document.getElementById(id) !== null),
    headingIds,
  )
  expect(allResolvable, '全部标题 id 可被 getElementById 解析（锚点目标在文档中）').toBe(true)

  const rail = page.getByTestId('outline-anchor')
  await expect(rail, '右缘导航轨在场（内容超一屏）').toBeVisible({ timeout: 10_000 })
  const scrollBox = page.getByTestId('answer-scroll')
  // 先滚到底，点击导航轨条目后应回到问题锚点附近（scrollTop 显著回落=跳转发生）
  await scrollBox.evaluate((el) => {
    el.scrollTop = el.scrollHeight
  })
  await page.waitForTimeout(300)
  const bottomScrollTop = await scrollBox.evaluate((el) => el.scrollTop)
  expect(bottomScrollTop, '前置滚动到底应生效').toBeGreaterThan(0)
  await rail.locator('button').first().click()
  await expect
    .poll(async () => await scrollBox.evaluate((el) => el.scrollTop), {
      timeout: 6_000,
      intervals: [300],
    })
    .toBeLessThan(Math.max(bottomScrollTop - 200, 1))
  const questionAnchorExists = await page.evaluate(
    (tid: string) => document.getElementById(`turn-${tid}-q`) !== null,
    turnId as string,
  )
  expect(questionAnchorExists, '问题块锚点 turn-{id}-q 在文档中').toBe(true)
  await page.screenshot({ path: path.join(SHOT_DIR, 'p1-04-toc-jump.png') })

  /* ---------- D. [n]↔来源面板联动 ---------- */
  await expect(page.getByTestId('source-panel').first()).toBeVisible({ timeout: 10_000 })
  const groupSel = `[data-turn-group="${turnId}"]`
  const cardSel = `${groupSel} [data-source-card]`
  await expect(page.locator(cardSel).first()).toBeVisible({ timeout: 10_000 })

  const cites = answerCol.locator('sup.cite[data-ref-no]')
  const citeCount = await cites.count()
  expect(citeCount, '正文引用角标 ≥1').toBeGreaterThanOrEqual(1)
  const citeNos = await cites.evaluateAll((els) =>
    els.map((el) => Number(el.getAttribute('data-ref-no'))).filter((n) => Number.isFinite(n) && n > 0),
  )
  const cardNos = await page
    .locator(cardSel)
    .evaluateAll((els) => els.map((el) => Number(el.getAttribute('data-source-card'))))
  const missing = [...new Set(citeNos)].filter((n) => !cardNos.includes(n))
  expect(missing, '[n] 全集 ⊆ 来源卡 ref_no 全集（批次 5 修复回归：零悬空引用）').toEqual([])

  // 点击首个 [n] → 组作用域定位 + cite-hit 高亮（1.6s 窗口）
  const firstRef = citeNos[0]
  const hitSel = `${groupSel} [data-source-card="${firstRef}"]`
  await cites.first().click()
  let hitSeen = false
  try {
    await page.waitForFunction(
      (sel: string) => document.querySelector(sel)?.classList.contains('cite-hit') === true,
      hitSel,
      { timeout: 4_000, polling: 100 },
    )
    hitSeen = true
    await page.screenshot({ path: path.join(SHOT_DIR, 'p1-05-cite-linkage.png') })
  } finally {
    expect(hitSeen, `点击 [${firstRef}] 应命中来源卡 cite-hit 高亮（组作用域定位）`).toBe(true)
  }

  /* ---------- E. done↔REST 对账（§2.5）+ 工件落盘 ---------- */
  const resp = await request.get(`${API_BASE}/api/v1/turns/${turnId}`)
  expect(resp.status(), 'REST GET /turns/{id} 200').toBe(200)
  const body: unknown = await resp.json()
  expect(isRecord(body) && body['code'] === '0' && isRecord(body['data']), '信封 code=0 且 data 在场').toBe(true)
  const rest = (isRecord(body) ? body['data'] : null) as Record<string, unknown> | null

  const restStatus = str(rest?.['status'])
  const restDegraded = rest?.['degraded']
  const restDegradedReason = rest?.['degraded_reason']
  const restSources = Array.isArray(rest?.['sources']) ? (rest?.['sources'] as unknown[]) : []
  const restDuration = num(rest?.['duration_ms'])
  const restUsage = isRecord(rest?.['usage']) ? (rest?.['usage'] as Record<string, unknown>) : null
  const restAnswer = str(rest?.['answer_md'])
  const restExport = rest?.['export_available']
  const restStages = Array.isArray(rest?.['stages']) ? (rest?.['stages'] as unknown[]) : []

  const doneFinish = finish
  const doneDegraded = doneData?.['degraded']
  const doneDegradedReason = doneData?.['degraded_reason']
  const doneSourceCount = num(doneData?.['source_count'])
  const doneDuration = num(doneData?.['duration_ms'])
  const doneUsage = isRecord(doneData?.['usage']) ? (doneData?.['usage'] as Record<string, unknown>) : null

  expect(restStatus, 'REST status == done.finish').toBe(doneFinish)
  expect(restDegraded, 'REST degraded == done.degraded').toBe(doneDegraded)
  expect(restDegradedReason, 'REST degraded_reason == done.degraded_reason').toEqual(doneDegradedReason)
  expect(doneSourceCount, 'done.source_count 在场').not.toBeNull()
  expect(restSources.length, 'REST sources.length == done.source_count').toBe(doneSourceCount)
  expect(restDuration, 'REST duration_ms == done.duration_ms').toBe(doneDuration)
  for (const key of ['prompt_tokens', 'completion_tokens', 'total_tokens']) {
    expect(restUsage?.[key], `usage.${key} done↔REST 一致`).toEqual(doneUsage?.[key])
  }
  expect(restAnswer, 'REST answer_md 非空（报告持久化）').not.toBeNull()
  expect((restAnswer as string).length, 'REST answer_md 长度 >0').toBeGreaterThan(0)
  expect(restExport, 'export_available=true（deep+completed+非空）').toBe(true)
  const restStageKeys = restStages
    .map((s) => (isRecord(s) ? str(s['stage_key']) : null))
    .filter((k): k is string => k !== null)
  for (const key of ['plan', 'search', 'reflect', 'write']) {
    expect(restStageKeys, `REST stages[] 含 ${key}（落库重建面）`).toContain(key)
  }
  const restRefNos = restSources
    .map((s) => (isRecord(s) ? num(s['ref_no']) : null))
    .filter((n): n is number => n !== null)
    .sort((a, b) => a - b)
  const contiguous = restRefNos.length > 0 && restRefNos.every((n, i) => n === i + 1)
  expect(contiguous, 'REST ref_no 排序后 1..N 连续无缺').toBe(true)

  // SSE sources ref_no 首现冻结：跨批无重复
  const sseRefNos: number[] = []
  for (const e of log) {
    if (e.event !== 'sources' || !Array.isArray(e.data?.['batch'])) continue
    for (const item of e.data['batch'] as unknown[]) {
      const n = isRecord(item) ? num(item['ref_no']) : null
      if (n !== null) sseRefNos.push(n)
    }
  }
  const sseDistinct = [...new Set(sseRefNos)]
  expect(sseRefNos.length, 'SSE sources 批次条目在场').toBeGreaterThan(0)
  expect(sseDistinct.length, 'SSE ref_no 首现冻结（跨批零重复）').toBe(sseRefNos.length)
  expect(sseDistinct.length, 'SSE distinct ref_no == done.source_count').toBe(doneSourceCount)

  /* ---------- 工件：SSE 摘要 + 对账明细 JSON ---------- */
  const eventCounts: Record<string, number> = {}
  for (const e of log) eventCounts[e.event] = (eventCounts[e.event] ?? 0) + 1
  const seqs = log
    .map((e) => (isRecord(e.data) ? num(e.data['seq']) : null))
    .filter((n): n is number => n !== null)
  const seqGaps: string[] = []
  for (let i = 1; i < seqs.length; i += 1) {
    if (seqs[i] !== seqs[i - 1] + 1) seqGaps.push(`${seqs[i - 1]}->${seqs[i]}`)
  }
  let snapshotAvailableTrue = 0
  for (const e of log) {
    if (e.event !== 'sources' || !Array.isArray(e.data?.['batch'])) continue
    for (const item of e.data['batch'] as unknown[]) {
      if (isRecord(item) && item['snapshot_available'] === true) snapshotAvailableTrue += 1
    }
  }

  const sseSummary = {
    question: DEEP_QUESTION,
    thread_url: threadUrl,
    thread_id: threadId,
    turn_id: turnId,
    stage_sequence: stageSeq.map((s) => ({ key: s.key, status: s.status, extra: s.extra })),
    event_counts: eventCounts,
    seq_first: seqs[0] ?? null,
    seq_last: seqs[seqs.length - 1] ?? null,
    seq_gaps: seqGaps,
    reflect_running_count: reflectRunningCount,
    read_present: idxRead >= 0,
    plan_subtopics: subtopics,
    write_sections: sections,
    write_sample_prefix: sample?.head ?? null,
    write_sample_len: sample?.len ?? null,
    final_answer_len: finalText.length,
    heading_ids: headingIds,
    toc_jump: { bottom_scroll_top: bottomScrollTop, question_anchor_exists: questionAnchorExists },
    cite_linkage: { cite_count: citeCount, distinct_cite_nos: [...new Set(citeNos)].sort((a, b) => a - b), first_ref_hit: hitSeen },
    sources: { batches: eventCounts['sources'] ?? 0, distinct_ref_no: sseDistinct.length, snapshot_available_true: snapshotAvailableTrue },
    console_errors: consoleErrors,
  }
  writeFileSync(path.join(SHOT_DIR, 'p1-sse-summary.json'), JSON.stringify(sseSummary, null, 2))

  const reconcile = {
    turn_id: turnId,
    done: {
      finish: doneFinish,
      degraded: doneDegraded,
      degraded_reason: doneDegradedReason,
      source_count: doneSourceCount,
      duration_ms: doneDuration,
      usage: doneUsage,
    },
    rest: {
      status: restStatus,
      degraded: restDegraded,
      degraded_reason: restDegradedReason,
      sources_len: restSources.length,
      duration_ms: restDuration,
      usage: restUsage,
      answer_md_len: (restAnswer as string).length,
      export_available: restExport,
      stage_keys: restStageKeys,
      ref_no_contiguous: contiguous,
    },
    all_match: true,
  }
  writeFileSync(path.join(SHOT_DIR, 'p1-reconcile.json'), JSON.stringify(reconcile, null, 2))

  /* ---------- 终态全页截图 ---------- */
  await page.screenshot({ path: path.join(SHOT_DIR, 'p1-06-deep-terminal-report.png'), fullPage: true })
})
