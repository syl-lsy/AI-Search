/**
 * e2e 骨架 — Answer 答案页主链路 + A13 断线重连（批次 7 第三步只写不跑；
 * 下一阶段由实测员对真服务执行——必须打真后端 + 真 SSE，webServer 仅拉起 vite）。
 * 断言口径 = PRD A1/A6/A8/A9/A13 GWT 原文 + frontend-api.md §1 AnswerView 行 / §3 生命周期。
 */
import { expect, test } from '@playwright/test'

/**
 * 【环境阻塞 skip——2026-11-12 批次 7 第四步实测员落；用户裁决 d) 维持现状挂牌】
 * 证据链（全部一手实测）：
 * - 本机 playwright chromium（chromium-1254 / headless-shell-158）与 channel:'chrome' 系统
 *   Chrome 的 EventSource 对本机 uvicorn SSE 恒定零消息（10+ 次：连接 OPEN 稳定、零 error、
 *   零 message；约 50s 后服务端关流进入重连循环）；
 * - 20 行最小 uvicorn SSE 服务（/tmp 独立进程，无任何项目中间件）同样复现——项目零关联；
 * - 同端点 fetch+ReadableStream 恒定正常（10ms 首字节、37KB 全量）、curl/urllib 实时段/
 *   回放段恒定正常（后端 SSE 与 A6 images 链路无回归）；
 * - 已排除：系统代理（--no-proxy-server 纯 loopback 直连仍复现）、GC/引用强度、socket
 *   复用/分池、headed/headless、CDP attach、页面可见性、NetworkServiceInProcess；
 * - 背景嫌疑（头号）：系统代理 127.0.0.1:20812 无 localhost bypass（scutil 证实、Chrome
 *   进程实挂其上、含 CLOSE_WAIT 僵尸连接），且可能有 TUN/增强模式层拦截。
 * 影响面：answer / scroll-trio / main-flow 三 spec 全部用例依赖 SSE 流式（token/stage/done
 * 事件驱动渲染与终态收敛）；threads/library/home 为 REST 路径不挂牌（已实测 10/10 通过）。
 * 复跑条件（可执行）：代理客户端对 127.0.0.1/localhost 加 bypass（或暂停代理客户端）后，
 * `pnpm --filter frontend exec playwright test` 全绿即摘牌（删除各 spec 顶部
 * `test.skip(true, '环境阻塞…')` 三处）；批次 8 前置动作。
 */
test.skip(true, '环境阻塞：本机 playwright chromium EventSource 对 uvicorn SSE 恒定零消息（20 行最小 uvicorn SSE 服务独立复现，项目零关联；同端点 fetch-stream/curl 恒定正常）——复跑条件：代理客户端对 127.0.0.1/localhost 加 bypass 后全量复跑，见文件头注释')

test.describe('Answer 答案页主链路（真服务）', () => {
  test('首页提问 → 跳答案页 → 流式渲染 → 终态对账（A1 前端判定点：两段式 + 事件驱动渲染）', async ({ page }) => {
    await page.goto('/')
    // 收敛型单面中文事实题（批次 6 实证：多面压力题撞 fast.turn_timeout_s=60 预算熔断，避开）
    await page.locator('[data-testid="home-search-card"] textarea').fill('LangGraph 是什么？')
    await page.locator('[data-testid="home-search-card"] [data-testid="submit-btn"]').click()
    // §1：POST /threads → 跳 /thread/{thread_id}（ask 内已订阅 SSE）
    await page.waitForURL(/\/thread\//, { timeout: 15_000 })
    // 流式进行中：问题气泡 + 时间线可见（stage 事件驱动）；骨架期不硬断言时长（P50 归 API 预检口径）
    await expect(page.getByTestId('turn-block').first()).toBeVisible({ timeout: 15_000 })
    // 终态后：答案渲染（A6 images 缺口已在批次 7 前置小修闭环——images 行无图不渲染为正常态）
    await expect(page.locator('[data-testid="turn-answer"]').first()).toBeVisible({ timeout: 90_000 })
  })

  test('快速轮 sources/引用联动：[n] 点击 → 来源卡定位高亮（REQ-003 前端判定点）', async ({ page }) => {
    await page.goto('/')
    // 收敛型单面中文事实题：带网络引用来源，[n] 角标产出概率高
    await page.locator('[data-testid="home-search-card"] textarea').fill('量子纠缠是什么？')
    await page.locator('[data-testid="home-search-card"] [data-testid="submit-btn"]').click()
    await page.waitForURL(/\/thread\//)
    // 真服务产出 [n] 与 sources 后：点击角标 → 来源卡 scrollIntoView + .cite-hit 高亮
    const cite = page.locator('sup.cite').first()
    const hasCite = await cite.count()
    if (hasCite > 0) {
      await cite.click()
      await expect(page.locator('.src-card.cite-hit').first()).toBeVisible()
    }
  })

  test('停止：运行中提交钮=停止（§2）→ 点击 → cancel（REQ-006 前端判定点）', async ({ page }) => {
    await page.goto('/')
    // 深度轮运行窗口长，点停时机充裕；题面为收敛型单面事实题
    await page.locator('[data-testid="home-search-card"] textarea').fill('光合作用的过程是什么？')
    await page.getByTestId('mode-deep').click()
    await page.locator('[data-testid="home-search-card"] [data-testid="submit-btn"]').click()
    await page.waitForURL(/\/thread\//)
    // 运行中：追问框提交钮变停止
    const stop = page.getByTestId('stop-btn')
    if (await stop.count()) {
      await stop.click()
      // cancel 请求已发（终态由服务端 done/cancel 链路收敛；骨架断言停止钮随终态消失）
      await expect(stop).toBeHidden({ timeout: 30_000 })
    }
  })
})

test.describe('A13 SSE 断线续传（REQ-005，非功能核心项）', () => {
  // 实测员执行方式：真服务流式输出期间断网 2s（浏览器侧 offline 模拟）→ 恢复；
  // 断言口径 = PRD A13 原文：经 Last-Event-ID 重连、已完成步骤与已输出文本不丢失、答案从断点继续直至完成。
  test('Given 快速模式流式输出进行中，When 断网 2s 后恢复，Then Last-Event-ID 重连且文本从断点继续', async ({ page }) => {
    await page.goto('/')
    // 收敛型单面中文事实题：token 流足够长（断线窗口内有增量可断言）
    await page.locator('[data-testid="home-search-card"] textarea').fill('HTTP 状态码 301 和 302 有什么区别？')
    await page.locator('[data-testid="home-search-card"] [data-testid="submit-btn"]').click()
    await page.waitForURL(/\/thread\//)

    // 等到流式产出非空文本再断线（断点内容存在，断言才有意义）
    await expect(page.locator('[data-testid="turn-block"]').first()).toBeVisible({ timeout: 15_000 })
    await expect
      .poll(async () => (await page.locator('[data-testid="answer-renderer"]').first().textContent()) ?? '', {
        timeout: 90_000,
        intervals: [1_000],
      })
      .not.toBe('')
    const before = ((await page.locator('[data-testid="answer-renderer"]').first().textContent()) ?? '').trim()
    expect(before.length).toBeGreaterThan(0)

    // 断网 2s → 恢复（EventSource 原生重连携带 Last-Event-ID，补帧由传输层 seq 门去重）
    await page.context().setOffline(true)
    await page.waitForTimeout(2_000)
    await page.context().setOffline(false)

    // 不丢：断点前文本仍在（A13「已输出文本不丢失」）
    await expect(page.locator('[data-testid="answer-renderer"]').first()).toContainText(
      before.slice(0, 20),
      { timeout: 60_000 },
    )
    // 从断点继续直至终态（A13「答案从断点继续直至完成」）
    await expect(page.getByTestId('turn-status').first()).toContainText(/已完成|研究未完成/, {
      timeout: 90_000,
    })
    // 不重：断点片段在全文恰好出现一次（补帧重复会造成 ≥2；streaming-buffer 规则 24 幂等的黑盒观测）
    const finalText = ((await page.locator('[data-testid="answer-renderer"]').first().textContent()) ?? '').trim()
    const occurrences = finalText.split(before.slice(0, 20)).length - 1
    expect(occurrences).toBe(1)
  })

  test('Given 深度研究进行中刷新页面，When 重新打开线程，Then 已完成步骤可见且剩余进度继续（A13 第二条）', async ({ page }) => {
    // 深度轮总时长不可控（deepagents 子代理并行研究），单用例放宽到 10min
    test.setTimeout(600_000)
    await page.goto('/')
    // 收敛型单面中文事实题（深度模式）
    await page.locator('[data-testid="home-search-card"] textarea').fill('PostgreSQL 的 MVCC 机制是什么？')
    await page.getByTestId('mode-deep').click()
    await page.locator('[data-testid="home-search-card"] [data-testid="submit-btn"]').click()
    await page.waitForURL(/\/thread\//)
    const url = page.url()

    // 运行中刷新 → 2.3+2.5 渲染已持久化部分 → subscribe 续传（§3 生命周期 5）
    await page.reload()
    await expect(page.getByTestId('turn-block').first()).toBeVisible({ timeout: 15_000 })
    await page.goto(url)
    // 剩余进度继续推进至终态
    await expect(page.getByTestId('turn-status').first()).toContainText(/已完成|研究未完成/, { timeout: 300_000 })
  })
})
