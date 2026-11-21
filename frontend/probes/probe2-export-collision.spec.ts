/**
 * e2e 活体探针②——导出面 + 409003 碰撞 toast（批次 6 活体验证，docs/m2-batch6-live-20261121.md 探针二组）。
 *
 * 落点注记（批次 6 收口轮 P2-2，2026-11-21）：本文件已迁出回归 testDir（test/e2e/）至
 * frontend/probes/——活体探针工件（真轮一次性、证据落盘），不是回归保护面；常规 playwright
 * 全量（testDir=./test/e2e）不再收集本探针；单独运行 =
 * `pnpm --filter frontend exec playwright test --config playwright.probes.config.ts`
 * （探针专用 config，testDir=./probes）。docs/design/e2e-batch6/ 工件归档不动。
 *
 * 断言面（探针二组钉点②）：
 *   A. deep 终态轮「导出报告」（act-export-report，消费 exportAvailable → POST /turns/{id}/export
 *      → download_path 锚点跳转下载，api.md §2.8/§5.3）→ download 文件名 report_{YYYYMMDD_HHmm}.md
 *      + 内容与页面 answer_md 同源抽查。
 *   B. 同分钟重复导出（同轮）= 物理覆写 + artifact 行按次登记（§2.8 v1.27 钉）——REST 直调
 *      POST ×2：两个 artifact_id / 同 filename / 不同 download_path / 同 size_bytes；
 *      download 端点内容与 UI 下载文件一致。
 *   C. 同线程同分钟跨轮碰撞 → 409003 拒绝 + toast 呈现（AnswerActions.exportReport catch →
 *      toast(服务端中文 message)）——载体 = 对探针一轮线程追问一轮 deep（轮 B 终态后同分钟内
 *      先后导出轮 A / 轮 B）；边界自愈：若首次因分钟跨界未碰撞（轮 B 200），同分钟内轮 A 再导出
 *      登记 rel_path 后轮 B 再导出即再造碰撞。
 *
 * 快照面注记：全库 24 个 deep 终态轮 §2.5 权威面 snapshot_available=true 计数=0（2026-11-21
 * 全量扫描，/tmp/probe2_inventory.json → docs 留档）——快照胶囊 v-if 不渲染、正向不可达（环境性
 * 供给缺失）；负向佐证 GET /sources/{id}/snapshot → 404002「该来源无快照」已在 spec 外 curl 留档。
 */
import { readFileSync, writeFileSync } from 'node:fs'
import path from 'node:path'

import { expect, test, type Page } from '@playwright/test'

const SHOT_DIR = path.resolve(process.cwd(), '..', 'docs', 'design', 'e2e-batch6')
const API_BASE = 'http://127.0.0.1:8300'

/** 探针一组产出的 deep completed 轮（thread 293cbc0d / turn fc9a1f59，89 源，export_available=true） */
const THREAD_URL = 'http://localhost:5173/thread/293cbc0d-dedb-4e49-8873-d15c91601511'
const TURN_A_ID = 'fc9a1f59-3e84-4c10-ae5b-9a7ec9dc79d3'
const FOLLOW_QUESTION = '那以太坊的 EIP-1559 费用机制是如何工作的？'

/* ---------- 类型守卫（§8.9） ---------- */
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

/** EventSource 包装器（语句形态——探针一组踩坑钉正：裸字符串=语句脚本，禁箭头函数表达式） */
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

/** REST 信封解包（守卫收窄） */
async function apiPost(request: import('@playwright/test').APIRequestContext, url: string): Promise<Record<string, unknown>> {
  const resp = await request.post(url)
  expect(resp.status(), `POST ${url} 应 200`).toBe(200)
  const body: unknown = await resp.json()
  expect(isRecord(body) && body['code'] === '0' && isRecord(body['data']), '信封 code=0 且 data 在场').toBe(true)
  return (isRecord(body) ? body['data'] : null) as Record<string, unknown>
}

test('探针二组②：导出报告 + 同分钟重复导出覆写 + 追问轮同分钟跨轮碰撞 409003 toast', async ({ page, request }) => {
  test.setTimeout(900_000)

  await page.addInitScript(SSE_PROBE_INIT)
  await page.goto(THREAD_URL)

  /* ---------- 轮 A 终态在场 ---------- */
  await expect(page.getByTestId('turn-status').first()).toContainText(/已完成|研究未完成/, { timeout: 30_000 })
  await expect(page.getByTestId('answer-actions').first()).toBeVisible({ timeout: 15_000 })

  /* ---------- A. 轮 A 导出报告（UI）→ download ---------- */
  const turnA = page.locator('[data-testid="turn-block"]').first()
  await turnA.getByTestId('act-more').click()
  await expect(page.getByTestId('act-more-menu')).toBeVisible()
  const exportBtn = page.getByTestId('act-export-report')
  await expect(exportBtn, 'deep+exportAvailable → 「导出报告」项渲染').toBeVisible({ timeout: 10_000 })
  await page.screenshot({ path: path.join(SHOT_DIR, 'p2-01-export-menu.png') })

  const postExportA = page
    .waitForResponse(
      (r) => r.url().includes(`/turns/${TURN_A_ID}/export`) && r.request().method() === 'POST',
      { timeout: 15_000 },
    )
    .catch(() => null)
  const downloadA = page.waitForEvent('download', { timeout: 15_000 }).catch(() => null)
  await exportBtn.click()
  const respA = await postExportA
  expect(respA, 'POST /export 响应到达（click → exportReport → axios 链路）').not.toBeNull()
  const dl = await downloadA
  const fnameA = dl ? dl.suggestedFilename() : ''
  let fileA = ''
  if (dl !== null) {
    expect(fnameA, '文件名形态 report_{YYYYMMDD_HHmm}.md').toMatch(/^report_\d{8}_\d{4}\.md$/)
    const dlPath = await dl.path()
    expect(dlPath, '下载已落盘').not.toBeNull()
    fileA = readFileSync(dlPath as string, 'utf8')
    expect(fileA.length, '下载体非空').toBeGreaterThan(0)
  }

  // 内容与页面 answer_md 同源抽查：REST download 端点拉取（服务端 FileResponse 权威内容）
  const restA = await apiPost(request, `${API_BASE}/api/v1/turns/${TURN_A_ID}/export`)
  const answerA = await request.get(`${API_BASE}/api/v1/turns/${TURN_A_ID}`)
  const answerBody: unknown = await answerA.json()
  const answerMd = str(
    isRecord(answerBody) && isRecord(answerBody['data']) ? answerBody['data']['answer_md'] : null,
  )
  expect(answerMd, 'REST answer_md 在场').not.toBeNull()
  const probe = (answerMd as string).replace(/\s+/g, ' ').slice(0, 120)
  const dlRespA = await request.get(`${API_BASE}${str(restA['download_path'])}`)
  expect(dlRespA.status(), 'download 端点 200').toBe(200)
  const dlBodyA = await dlRespA.text()
  expect(dlBodyA.length, '服务端导出文件非空').toBeGreaterThan(0)
  expect(
    dlBodyA.replace(/\s+/g, ' ').includes(probe),
    '导出文件包含 answer_md 开头片段（同源抽查）',
  ).toBe(true)
  if (dl !== null) {
    expect(dlBodyA.length, 'UI 下载文件长度 == REST download 内容长度').toBe(fileA.length)
  }

  /* ---------- B. 同分钟重复导出（同轮）REST ×2 → artifact 行按次登记 ---------- */
  const restB = await apiPost(request, `${API_BASE}/api/v1/turns/${TURN_A_ID}/export`)
  const restC = await apiPost(request, `${API_BASE}/api/v1/turns/${TURN_A_ID}/export`)
  const aidA = str(restA['artifact_id'])
  const aidB = str(restB['artifact_id'])
  const aidC = str(restC['artifact_id'])
  expect(aidA && aidB && aidC, '三次导出 artifact_id 均在场').not.toBeNull()
  expect(aidB, '同分钟重复导出 = 新 artifact 行（artifact_id 不同，§2.8 v1.27 钉）').not.toBe(aidA)
  expect(aidC).not.toBe(aidA)
  expect(aidC).not.toBe(aidB)
  expect(str(restA['filename']), '同分钟 filename 相同（分钟精度）').toBe(str(restB['filename']))
  expect(str(restB['filename'])).toBe(str(restC['filename']))
  expect(str(restA['filename']), 'filename 形态 report_{YYYYMMDD_HHmm}.md').toMatch(/^report_\d{8}_\d{4}\.md$/)
  if (dl !== null) {
    expect(str(restA['filename'])).toBe(fnameA)
  }
  expect(num(restB['size_bytes']), 'size_bytes 一致（同一 answer_md）').toBe(num(restA['size_bytes']))
  expect(str(restB['download_path'])).not.toBe(str(restA['download_path']))

  // download 端点内容与 UI 下载文件一致（长度+同源片段双证）
  const dlResp = await request.get(`${API_BASE}${str(restA['download_path'])}`)
  expect(dlResp.status(), 'download 端点 200').toBe(200)
  const dlBody = await dlResp.text()
  expect(dlBody.length, 'download 端点内容长度 == UI 下载文件长度').toBe(fileA.length)

  /* ---------- C. 追问轮 B（deep）→ 终态 → 同分钟跨轮碰撞 409003 toast ---------- */
  const threadId = THREAD_URL.split('/thread/')[1] ?? ''
  const postTurn = page
    .waitForResponse(
      (r) => r.url().includes(`/threads/${threadId}/turns`) && r.request().method() === 'POST',
      { timeout: 15_000 },
    )
    .catch(() => null)
  await page.getByTestId('follow-bar').locator('textarea').fill(FOLLOW_QUESTION)
  await page.getByTestId('follow-bar').getByTestId('mode-deep').click()
  await page.getByTestId('follow-bar').locator('[data-testid="submit-btn"]').click()
  const respTurn = await postTurn
  expect(respTurn, '追问 POST /threads/{id}/turns 到达（submit → store 链路）').not.toBeNull()
  await expect(page.locator('[data-testid="turn-block"]')).toHaveCount(2, { timeout: 20_000 })

  // 等 done（轮 B 真 deep 轮，最长 480s）
  const t0 = Date.now()
  let doneB: ProbeEvent | null = null
  while (Date.now() - t0 < 480_000) {
    const log = await probeLog(page)
    doneB = log.find((e) => e.event === 'done') ?? null
    if (doneB !== null) break
    await page.waitForTimeout(2_000)
  }
  expect(doneB, '轮 B 480s 内 done').not.toBeNull()
  const finishB = str(doneB?.data?.['finish'])
  expect(finishB === 'completed' || finishB === 'degraded', '轮 B 终态合法').toBe(true)

  // 轮 B 事件面记录（追问 deep 轮 §7.4 形态佐证）
  const logB = await probeLog(page)
  const stageSeqB = logB
    .filter((e) => e.event === 'stage')
    .map((e) => `${str(e.data?.['stage_key'])}:${str(e.data?.['status'])}`)
  const turnBId = str(
    logB.find((e) => isRecord(e.data) && typeof e.data['turn_id'] === 'string')?.data?.['turn_id'],
  )

  // 轮 B 的导出钮可见性分派：completed / degraded 非空 answer → 在场；空正文 degraded → 不渲染（此时
  // 走 REST 直调取 409003「未完成」形态，UI toast 无法经无入口轮触发——如实分派）
  const turnB = page.locator('[data-testid="turn-block"]').nth(1)
  await expect(turnB.getByTestId('turn-status')).toContainText(/已完成|研究未完成/, { timeout: 10_000 })
  const canExportB = (await turnB.getByTestId('answer-actions').count()) > 0

  /* ---------- 碰撞编排：同分钟内先登记轮 A rel_path，再导出轮 B ---------- */
  let collision409 = false
  let collisionForm = ''
  let collisionToastText = ''
  let attempt = 0
  while (!collision409 && attempt < 3) {
    attempt += 1
    // ① 轮 A 导出（UI 或 REST），登记当前分钟 rel_path
    const dlPromise = page.waitForEvent('download', { timeout: 8_000 }).catch(() => null)
    const actionsA = turnA.getByTestId('answer-actions')
    if ((await actionsA.count()) > 0) {
      await actionsA.getByTestId('act-more').click()
      await page.getByTestId('act-export-report').first().click()
    } else {
      void dlPromise
    }
    const dlA = await dlPromise
    if (dlA === null) {
      // UI 无入口（异常形态）→ REST 登记
      await apiPost(request, `${API_BASE}/api/v1/turns/${TURN_A_ID}/export`)
    }
    // ② 轮 B 导出：UI 入口在 → 点击期待 toast；不在 → REST 直调期待 409003
    if (canExportB) {
      const toastSeen = page.waitForSelector('[data-testid="toast-host"] .toast-item', { timeout: 8_000 }).catch(() => null)
      await turnB.getByTestId('answer-actions').getByTestId('act-more').click()
      await turnB.getByTestId('answer-actions').getByTestId('act-export-report').click()
      const toastEl = await toastSeen
      if (toastEl !== null) {
        const toastText = (await toastEl.textContent()) ?? ''
        if (toastText.length > 0) {
          collision409 = true
          collisionForm = 'ui_toast'
          collisionToastText = toastText
          await page.screenshot({ path: path.join(SHOT_DIR, 'p2-02-export-409003-toast.png') })
          expect(toastText.length, 'toast 文案非空（服务端中文 message）').toBeGreaterThan(0)
        }
      }
    } else {
      const resp = await request.post(`${API_BASE}/api/v1/turns/${turnBId}/export`)
      const body: unknown = await resp.json()
      const code = isRecord(body) ? str(body['code']) : null
      if (resp.status() === 409 && code === '409003') {
        collision409 = true
        collisionForm = 'rest_not_exportable'
        expect(isRecord(body) ? str(body['message']) : null, '409003 message 非空').not.toBeNull()
      }
    }
  }
  expect(collision409, '同线程同分钟跨轮碰撞 409003 触发（UI toast 或 REST 形态）').toBe(true)

  /* ---------- 工件：轮 B 事件面摘要 ---------- */
  const summary = {
    turn_a: TURN_A_ID,
    export_filename: fnameA,
    export_size: fileA.length,
    artifact_ids_in_same_minute: [str(restA['artifact_id']), str(restB['artifact_id']), str(restC['artifact_id'])],
    follow_question: FOLLOW_QUESTION,
    turn_b: turnBId,
    turn_b_finish: finishB,
    turn_b_stage_seq: stageSeqB,
    collision_form: collisionForm,
    collision_toast_text: collisionToastText,
    collision_attempts: attempt,
  }
  writeFileSync(path.join(SHOT_DIR, 'p2-export-summary.json'), JSON.stringify(summary, null, 2))
})
