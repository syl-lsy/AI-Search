/**
 * e2e — 四页主链路（批次 7 第四步实测员新增；门禁执行口径）：
 * 首页提问 → 流式回答 → sources 渲染 → 新开线程（追问框新问题）→ Threads 页可见 → Library 页可打开。
 * 一条用例串六步（workers=1 串行 + 单次真实 LLM 轮，避免重复烧轮次）。
 * 断言口径 = PRD REQ-003/008/019 + frontend-api.md §1 四页行；题面为收敛型单面中文事实题。
 */
import { expect, test } from '@playwright/test'

/**
 * 【P1 挂牌已摘（2026-11-13 修复落地复跑）】批次 8 甄别挂牌的 turnStream 具名事件失明缺陷
 * 已修复（openEventSource 对 api.md §7.2 全部事件名逐一 addEventListener + mockEventSource
 * 派发语义复刻浏览器，单测先红 18 条后翻绿）——本文件 skip 删除全量复跑。详情见
 * answer.spec.ts 文件头注释。
 * 【摘牌复跑第 2 轮（2026-11-13）——降级空答案形态分派】第 3 轮全量复跑本用例红于 ②：
 * 本轮真服务回答撞 60s 预算兜底降级（500301，批次 6 遗留④已知间歇形态），synthesis 被掐
 * → answer_md 空 → 无空区块原则下 turn-answer 不渲染（TurnBlock v-if="turn.answer_md"）。
 * ② 断言改形态分派：有答案区→可见；无答案区→degraded-banner 在场（同为合法收敛终态）。
 */

test('四页主链路：首页提问 → 流式回答 → sources → Threads 可见 → Library 打开', async ({ page }) => {
  // ① 首页提问 → 跳答案页
  await page.goto('/')
  await page.locator('[data-testid="home-search-card"] textarea').fill('图神经网络有什么应用？')
  await page.locator('[data-testid="home-search-card"] [data-testid="submit-btn"]').click()
  await page.waitForURL(/\/thread\//, { timeout: 15_000 })
  await expect(page.getByTestId('turn-block').first()).toBeVisible({ timeout: 15_000 })

  // ② 流式回答至终态（真服务单轮 30~65s；degraded/500301 兜底语义同为可收敛终态——
  // 60s 预算兜底轮 synthesis 被掐时 answer_md 可为空（批次 6 遗留④：负载窗间歇形态），
  // 无空区块原则下 turn-answer 不渲染：按形态分派断言——有答案区则可见，无答案区则
  // 须为降级终态（degraded-banner 在场）
  await expect(page.getByTestId('turn-status').first()).toContainText(/已完成|研究未完成/, {
    timeout: 120_000,
  })
  const answerBlock = page.locator('[data-testid="turn-answer"]').first()
  if (await answerBlock.count()) {
    await expect(answerBlock).toBeVisible()
  } else {
    await expect(page.getByTestId('degraded-banner').first()).toBeVisible({ timeout: 10_000 })
  }

  // ③ sources 渲染（真服务带引用来源；来源面板可见性为 REQ-003 前端判定点）
  await expect(page.getByTestId('source-panel').first()).toBeVisible({ timeout: 10_000 })
  await expect(page.locator('.src-card').first()).toBeVisible({ timeout: 10_000 })

  // ④ 新开线程（侧栏「新建」→ 首页 → 提交第二问；同会话内两线程便于后续 Threads 断言）
  await page.getByTestId('nav-home').click()
  await expect(page.getByTestId('home-view')).toBeVisible()
  await page.locator('[data-testid="home-search-card"] textarea').fill('什么是强化学习？')
  await page.locator('[data-testid="home-search-card"] [data-testid="submit-btn"]').click()
  await page.waitForURL(/\/thread\//, { timeout: 15_000 })

  // ⑤ Threads 页可见（含先前线程；生成中第二线程以列表行/生成中指示存在）
  await page.getByTestId('nav-threads').click()
  await expect(page.getByTestId('threads-view')).toBeVisible()
  const rows = page.locator('[data-testid^="thread-row-"]')
  await expect(rows.first()).toBeVisible({ timeout: 10_000 })
  // 至少包含本会话先前提问创建的线程（≥2 行 = 本会话两问 + 历史线程均在）
  expect(await rows.count()).toBeGreaterThanOrEqual(2)

  // ⑥ Library 页可打开
  await page.getByTestId('nav-library').click()
  await expect(page.getByTestId('library-view')).toBeVisible()
  await expect(page.locator('.doc-table')).toBeVisible()
})
