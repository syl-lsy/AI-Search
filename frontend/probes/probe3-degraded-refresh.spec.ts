/**
 * e2e 活体探针③④——降级形态 + deep 刷新恢复（批次 6 活体验证，docs/m2-batch6-live-20261121.md 探针二组）。
 *
 * 落点注记（批次 6 收口轮 P2-2，2026-11-21）：本文件已迁出回归 testDir（test/e2e/）至
 * frontend/probes/——活体探针工件（真轮一次性、证据落盘），不是回归保护面；常规 playwright
 * 全量（testDir=./test/e2e）不再收集本探针；单独运行 =
 * `pnpm --filter frontend exec playwright test --config playwright.probes.config.ts`
 * （探针专用 config，testDir=./probes）。docs/design/e2e-batch6/ 工件归档不动。
 *
 * 前置：本 spec 必须在「L3 注入态」运行（.zcode/tmp/probe2_inject_degraded.py inject + dev 重启后）——
 * deep.turn_timeout_s=90 使探针③④ 合并轮（轮 C）在 ~90s 被 guards 熔断（done(degraded, reason=timeout)）。
 * 还原步骤（restore + 重启）由运行纪律在 spec 外执行，见证据档。
 *
 * 断言面：
 *   ③a 存量 degraded 回看面（注入无关）：thread 20df5c16 / turn e306f3b1（deep+72 源+answer 951 字+
 *      export_available=true，2026-11-21 全量扫描存量）→ DegradedBanner「研究未完成」在场 +
 *      act-regenerate（第 8 钮 v1.30 放开 deep：status=degraded ∧ sources>0 ∧ mode=deep 活体面）
 *      + act-export-report（exportAvailable 门控）——零点击（不烧 regenerate/导出轮）。
 *   ③b 注入触发（轮 C 终态）：done(degraded) → turn-status「研究未完成」+ degraded-banner 在场。
 *   ④ 刷新恢复（轮 C 进行中 reload）：REST 全量渲染已持久化部分（running 态 + 来源面板 + 时间线）
 *      + 续订阅不丢帧——OQ-35 v1.30 口径：轮 C 无 context_compact → sessionStorage 无 compactSeq
 *      记录 → resolveFromSeq 判定③ 返 0 = 放弃增量、服务端首连恒全量重放（turnStream.ts:299-300）
 *      → 断言：重放段 seq 1..M 连续无 gap 且 M ≥ 刷新前落盘游标（EventBus 重放缓冲覆盖刷新窗口）
 *      + 渲染幂等（turn-block 恒 1、来源卡数 == REST sources.length）。
 *
 * 工件：p3-01-degraded-lookback.png / p3-02-degraded-banner.png / p3-refresh-summary.json。
 */
import { writeFileSync } from 'node:fs'
import path from 'node:path'

import { expect, test, type Page } from '@playwright/test'

const SHOT_DIR = path.resolve(process.cwd(), '..', 'docs', 'design', 'e2e-batch6')
const API_BASE = 'http://127.0.0.1:8300'

/** ③a 存量 degraded-with-content 轮（2026-11-21 全量扫描权威面：72 源、answer 951 字、export=true） */
const DEGRADED_THREAD_ID = '20df5c16-a66f-4253-b9d8-887c4ca2f53a'

/** 轮 C 题面（注入态 90s 熔断载体） */
const QUESTION_C = '对比 Redis 与 Kafka 在实时数据管道中的角色差异与选型考量'

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
async function probeLog(page: Page): Promise<ProbeEvent[]> {
  const raw: unknown = await page.evaluate(() => (window as { __probeSSE?: unknown }).__probeSSE)
  return isProbeEventArray(raw) ? raw : []
}

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

test.describe.configure({ mode: 'serial' })

test('探针③a：存量 degraded-with-content 轮回看面——DegradedBanner + 第 8 钮 + 导出报告钮（零点击）', async ({ page }) => {
  test.setTimeout(120_000)
  await page.goto(`http://localhost:5173/thread/${DEGRADED_THREAD_ID}`)
  await expect(page.getByTestId('turn-status').first()).toContainText('研究未完成', { timeout: 20_000 })
  await expect(page.getByTestId('degraded-banner').first(), 'DegradedBanner 在场（degraded 终态渲染）').toBeVisible({ timeout: 10_000 })
  expect(
    ((await page.getByTestId('degraded-banner').first().textContent()) ?? '').includes('研究未完成'),
    'banner 文案含「研究未完成」',
  ).toBe(true)

  // v1.30 放开 deep 第 8 钮活体面：status=degraded ∧ sources>0 ∧ mode=deep → act-regenerate 在场
  await expect(page.getByTestId('act-regenerate').first(), '「重新生成」钮渲染（v1.30 deep 放开）').toBeVisible({ timeout: 10_000 })
  // export_available=true（§2.5 判据）→ 「导出报告」菜单项可达（展开菜单验证后收起，零点击执行）
  await page.getByTestId('act-more').first().click()
  await expect(page.getByTestId('act-export-report'), 'exportAvailable=true → 导出报告项在场').toBeVisible()
  await page.keyboard.press('Escape')
  await page.mouse.click(10, 10)
  // 来源面板（72 源回看渲染）
  await expect(page.getByTestId('source-panel').first()).toBeVisible({ timeout: 10_000 })
  await page.screenshot({ path: path.join(SHOT_DIR, 'p3-01-degraded-lookback.png') })
})

test('探针③b+④：deep 轮进行中刷新（REST 全量渲染 + 全量重放零丢失）→ 90s 熔断 DegradedBanner', async ({ page, request }) => {
  test.setTimeout(600_000)
  await page.addInitScript(SSE_PROBE_INIT)
  await page.goto('/')
  await page.locator('[data-testid="home-search-card"] textarea').fill(QUESTION_C)
  await page.getByTestId('mode-deep').click()
  await page.locator('[data-testid="home-search-card"] [data-testid="submit-btn"]').click()
  await page.waitForURL(/\/thread\//, { timeout: 15_000 })
  const threadId = page.url().split('/thread/')[1] ?? ''
  expect(threadId).not.toBe('')

  /* ---------- 流式进行中：等 sources 首批（刷新窗口起点） ---------- */
  await expect(page.getByTestId('turn-block').first()).toBeVisible({ timeout: 15_000 })
  await expect(page.getByTestId('timeline').first()).toBeVisible({ timeout: 60_000 })
  let lastSeqBefore = 0
  const t0 = Date.now()
  while (Date.now() - t0 < 120_000) {
    const log = await probeLog(page)
    const seqs = log
      .map((e) => (isRecord(e.data) ? num(e.data['seq']) : null))
      .filter((n): n is number => n !== null)
    lastSeqBefore = seqs.length > 0 ? Math.max(...seqs) : 0
    if (log.some((e) => e.event === 'sources')) break
    await page.waitForTimeout(1_000)
  }
  const logBefore = await probeLog(page)
  const sourcesBatchesBefore = logBefore.filter((e) => e.event === 'sources').length
  expect(sourcesBatchesBefore, '刷新前 sources 首批已到（§7.4 sources 事件）').toBeGreaterThanOrEqual(1)
  expect(lastSeqBefore, '刷新前游标在场').toBeGreaterThan(0)

  /* ---------- ④ reload：REST 全量渲染已持久化部分 ---------- */
  const cursorBefore: unknown = await page.evaluate((tid: string) =>
    sessionStorage.getItem(`ai-search.seq.${tid}`),
  threadId)
  await page.reload()
  await expect(page.getByTestId('turn-status').first()).toContainText(/进行中|生成中|研究|撰写|排队/, {
    timeout: 30_000,
  })
  await expect(page.locator('[data-testid="turn-block"]'), '渲染幂等：轮块恒 1').toHaveCount(1)
  await expect(page.getByTestId('source-panel').first()).toBeVisible({ timeout: 20_000 })
  await page.screenshot({ path: path.join(SHOT_DIR, 'p3-03-refresh-recovery-midround.png') })

  // sessionStorage 游标（v1.30 OQ-35 单键 JSON 双值形态）——键族=turnId 维度（turn.ts:55）
  const restTurnResp = await request.get(`${API_BASE}/api/v1/threads/${threadId}`)
  const restTurnBody: unknown = await restTurnResp.json()
  const restTurnsArr =
    isRecord(restTurnBody) && isRecord(restTurnBody['data']) && Array.isArray(restTurnBody['data']['turns'])
      ? (restTurnBody['data']['turns'] as unknown[])
      : []
  const turnCIdEarly =
    restTurnsArr.length > 0 && isRecord(restTurnsArr[0]) ? str(restTurnsArr[0]['turn_id']) : null
  expect(turnCIdEarly, 'REST 线程详情取 turnId').not.toBeNull()
  const cursorAfter: unknown = await page.evaluate((tid: string) =>
    sessionStorage.getItem(`ai-search.seq.${tid}`),
  turnCIdEarly as string)
  expect(typeof cursorAfter, '刷新后 sessionStorage 游标在位').toBe('string')
  let cursorObj: { lastSeq: number; compactSeq: number | null } | null = null
  if (typeof cursorAfter === 'string') {
    try {
      const parsed: unknown = JSON.parse(cursorAfter)
      if (
        isRecord(parsed) &&
        typeof parsed['lastSeq'] === 'number' &&
        (parsed['compactSeq'] === null || typeof parsed['compactSeq'] === 'number')
      ) {
        cursorObj = { lastSeq: parsed['lastSeq'], compactSeq: parsed['compactSeq'] as number | null }
      }
    } catch {
      cursorObj = null
    }
  }
  expect(cursorObj, '游标形态 {lastSeq, compactSeq}（v1.30 双值 JSON）').not.toBeNull()
  expect(cursorObj?.compactSeq, '轮 C 无 context_compact → compactSeq 无记录（null/0）').toBeFalsy()
  // 注：不另断「落盘 ≤ 采样末帧」——reload 编排窗口内轮继续流，采样时序天然滞后（实测 10>9）；
  // 零丢失由下方「重放覆盖」断言承担（重放 max ≥ 落盘游标=EventBus 缓冲覆盖刷新窗口）。

  /* ---------- ④ 续订阅形态：全量重放（fromSeq=0 判定③）+ 零丢失 ---------- */
  // 重放完成判定：重放段（seq 1..M）连续无 gap，且 M ≥ 落盘游标（覆盖刷新窗口）
  await expect.poll(
    async () => {
      const log = await probeLog(page)
      const seqs = log
        .map((e) => (isRecord(e.data) ? num(e.data['seq']) : null))
        .filter((n): n is number => n !== null)
      if (seqs.length === 0) return 0
      const m = Math.max(...seqs)
      return m >= (cursorObj?.lastSeq ?? 0) ? m : 0
    },
    { timeout: 60_000, intervals: [1_000] },
  ).toBeGreaterThan(0)

  const logAfter = await probeLog(page)
  const seqsAfter = logAfter
    .map((e) => (isRecord(e.data) ? num(e.data['seq']) : null))
    .filter((n): n is number => n !== null)
  const gaps: string[] = []
  for (let i = 1; i < seqsAfter.length; i += 1) {
    if (seqsAfter[i] !== seqsAfter[i - 1] + 1) gaps.push(`${seqsAfter[i - 1]}->${seqsAfter[i]}`)
  }
  const replayMax = Math.max(...seqsAfter)
  const replayCovered = replayMax >= (cursorObj?.lastSeq ?? 0)
  const firstSeqAfter = seqsAfter[0] ?? null
  expect(firstSeqAfter, '刷新后 wrapper 首帧 = 全量重放起点 seq=1（fromSeq=0 判定③口径）').toBe(1)
  expect(gaps, '重放段 seq 连续无 gap（EventBus 重放缓冲完整）').toEqual([])
  expect(replayCovered, `重放覆盖刷新窗口（M=${replayMax} ≥ 落盘游标=${cursorObj?.lastSeq}）`).toBe(true)

  // 来源卡渲染恢复（§2.5 完整面——thread detail turns[] 为摘要形态不含 sources，勿混用）：
  // 流式期 DOM 只增不减、REST 为 t1 时刻快照 → 断言 DOM 卡数 ≥ REST sources.length（对账渲染面下限）
  const restTurnResp2 = await request.get(`${API_BASE}/api/v1/turns/${turnCIdEarly}`)
  expect(restTurnResp2.status()).toBe(200)
  const restTurnBody2: unknown = await restTurnResp2.json()
  const restSources =
    isRecord(restTurnBody2) && isRecord(restTurnBody2['data']) && Array.isArray(restTurnBody2['data']['sources'])
      ? (restTurnBody2['data']['sources'] as unknown[])
      : []
  const cardCount = await page.locator('[data-testid="source-panel"] [data-source-card]').count()
  expect(cardCount, '刷新后来源卡渲染恢复（>0）').toBeGreaterThan(0)
  expect(cardCount, '渲染卡数 ≥ REST §2.5 快照 sources.length（对账下限，流式期竞态容忍）').toBeGreaterThanOrEqual(
    restSources.length,
  )

  /* ---------- ③b 注入触发：90s 熔断 → done(degraded) → DegradedBanner ---------- */
  await expect(page.getByTestId('turn-status').first()).toContainText('研究未完成', { timeout: 300_000 })
  await expect(page.getByTestId('degraded-banner').first()).toBeVisible()
  expect(
    ((await page.getByTestId('degraded-banner').first().textContent()) ?? '').includes('研究未完成'),
    'banner 文案含「研究未完成」',
  ).toBe(true)
  await page.screenshot({ path: path.join(SHOT_DIR, 'p3-02-degraded-banner.png') })

  // 终态对账：REST degraded + reason=timeout（注入死线形态）+ error 500301（批次 5 击杀形态注）
  const turnCId = str(
    logAfter.find((e) => isRecord(e.data) && typeof e.data['turn_id'] === 'string')?.data?.['turn_id'],
  )
  const doneEv = (await probeLog(page)).filter((e) => e.event === 'done')
  const doneC = doneEv[doneEv.length - 1]?.data ?? null
  const finishC = str(doneC?.['finish'])
  const reasonC = doneC?.['degraded_reason']
  expect(finishC, 'done(finish=degraded)').toBe('degraded')
  expect(reasonC, 'degraded_reason=timeout（turn_timeout_s=90 注入死线）').toBe('timeout')

  const turnDetail = await request.get(`${API_BASE}/api/v1/turns/${turnCId}`)
  const turnBody: unknown = await turnDetail.json()
  const turnData = isRecord(turnBody) && isRecord(turnBody['data']) ? turnBody['data'] : null
  expect(str(turnData?.['status']), 'REST status=degraded').toBe('degraded')
  expect(turnData?.['degraded_reason'], 'REST degraded_reason=timeout').toBe('timeout')
  const restSourcesFinal = Array.isArray(turnData?.['sources']) ? (turnData?.['sources'] as unknown[]) : []
  expect(
    restSourcesFinal.length,
    '终态 REST sources.length == done.source_count（终态严格对账）',
  ).toBe(num(doneC?.['source_count']))
  const errEvents = (await probeLog(page)).filter((e) => e.event === 'error')
  const errCode = errEvents.length > 0 ? str(errEvents[0].data?.['code']) : null

  const summary = {
    thread_id: threadId,
    turn_id: turnCId,
    question: QUESTION_C,
    last_seq_before_reload: lastSeqBefore,
    cursor_before_reload: typeof cursorBefore === 'string' ? JSON.parse(cursorBefore) : null,
    cursor_after_reload: cursorObj,
    replay: { first_seq: firstSeqAfter, max_seq: replayMax, gaps, covered_cursor: replayCovered },
    rest_sources_at_refresh_check: restSources.length,
    source_cards_rendered: cardCount,
    done: { finish: finishC, degraded_reason: reasonC },
    error_event_code: errCode,
    note: 'error 500301 = TurnRunner 整轮硬超时兜底收束（§7.2 v1.21 注专属形态，批次 5 探针③同款击杀窗口）',
  }
  writeFileSync(path.join(SHOT_DIR, 'p3-refresh-summary.json'), JSON.stringify(summary, null, 2))
})
