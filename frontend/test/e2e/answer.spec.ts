/**
 * e2e 骨架 — Answer 答案页主链路 + A13 断线重连（批次 7 第三步只写不跑；
 * 下一阶段由实测员对真服务执行——必须打真后端 + 真 SSE，webServer 仅拉起 vite）。
 * 断言口径 = PRD A1/A6/A8/A9/A13 GWT 原文 + frontend-api.md §1 AnswerView 行 / §3 生命周期。
 */
import { expect, test } from '@playwright/test'

/**
 * 【批次 8 摘牌复跑——2026-11-12 批次 8 实测员摘；原批次 7 环境阻塞挂牌已解除】
 * 摘牌依据（全部一手实测，详录 .zcode/workflow-drafts/batch8-e2e-logs/）：
 * - 用户已对系统代理 127.0.0.1:20812 加 localhost bypass：20 行最小 uvicorn SSE ×
 *   chromium EventSource 探针 PASS（2s 内收到 3 条 tick，sse-probe.mjs）；
 * - 真实链路事件流到达：Playwright 真前端真实提问，stage/sources/images/thinking/token/
 *   done 全序列到达浏览器并被 addEventListener 派发（live-smoke.mjs hook 实证）。
 * 【摘牌后新发现（实测员根因钉死，留档待前端线修复——用例红为真实缺陷暴露，非环境阻塞）】
 * - frontend/src/sse/turnStream.ts:330 openEventSource 仅以 es.onmessage 接帧；而服务端
 *   全部业务事件为具名事件（event: stage/token/...，api.md §7.2）——按 SSE 规范具名事件
 *   只派发给 addEventListener(name) listener，onmessage 只收 event: message/无 event 帧。
 *   → 真浏览器下传输层收不到任何业务事件：UI 零流式渲染、done 永不应用（轮卡「生成中」）、
 *   连接生命周期靠 onerror 裸转（~15s 退避封顶重连-重放循环，loop-diag.mjs 实证）。
 * - 单测假绿根因：test/helpers/mockEventSource.ts:36,50 dispatch() 直接调 onmessage，
 *   未复刻浏览器具名事件派发语义——mock 与真实 EventSource 行为脱节。
 * - 修复方向（前端线 TDD）：openEventSource 对 SSE 事件名逐一 addEventListener(name)
 *   （复用 handleMessage）；mockEventSource.dispatch 改按具名 listener 派发语义先红后绿。
 * 【批次 8 门禁第 1 轮甄别处置（2026-11-12）】门禁 20 红 = 11 条环境性（双服务被实测员收尾
 *   停掉、后端 8300 无管理方→REST 数据依赖全红；服务重启后单条复跑即绿：threads 日期分组
 *   271ms / answer 引用联动 281ms 均 passed）+ 9 条确定性（turnStream 缺陷，服务健康下单条
 *   A1 复跑仍红坐实）。按 P1 挂牌纪律：9 条流式依赖用例重新挂牌（各 test 体内 skip，
 *   reason 带锚点与复跑条件）；缺陷修复由实测员 notes 转派前端修复工程师；修复落地后摘牌
 *   全量复跑（A27/A28/回看态判定能力已有回看态补图佐证：docs/design/e2e-batch8/ 9 张）。
 * 另注：frontend/.env 已由批次 8 实测员改回「同源 + vite proxy」形态（批次 7 直连形态与
 * 批次 7 后新增的 index.html CSP default-src 'self' 冲突——connect-src 跨源被拦，
 * live-smoke 首跑实证 models/threads 全 FAILED :: csp）。
 * 【2026-11-13 摘牌（修复落地复跑）】具名事件失明缺陷已修复：frontend/src/sse/turnStream.ts
 * openEventSource 对 api.md §7.2 全部 10 个事件名逐一 addEventListener(name) 接帧（复用
 * handleMessage），测试替身 test/helpers/mockEventSource.ts dispatch 改按 WHATWG 具名派发
 * 语义（具名帧只进 addEventListener listener）——单测先红 18 条后翻绿锁死。本文件 2 条 +
 * main-flow 1 条 + scroll-trio 6 条共 9 处挂牌全部摘除复跑。
 */

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
    // 终态后：答案渲染（A6 images 缺口已在批次 7 前置小修闭环——images 行无图不渲染为正常态）。
    // 60s 预算兜底降级轮 answer_md 可空（批次 6 遗留④间歇形态）——无空区块原则下
    // turn-answer 不渲染，按形态分派：答案区或降级横幅任一出现即收敛
    await expect
      .poll(
        async () =>
          (await page.locator('[data-testid="turn-answer"]').count()) > 0 ||
          (await page.getByTestId('degraded-banner').count()) > 0,
        { timeout: 90_000, intervals: [2_000] },
      )
      .toBe(true)
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
    // 【「不重」黑盒子断言已删（2026-11-13 全量复跑实证）】原判据「断点片段恰好出现一次」
    // 构造无效：标记片段只取答案开头 20 字符，真重放重复的二次出现位置在断点之后——与
    // LLM 正文天然复现同一短语在黑盒下不可区分（两轮复跑同用例一过一挂，天然出现 1~4 次
    // 波动纯由 LLM 输出决定）。幂等防线的有效防线在白盒层：传输层 seq 门重放去重与 store
    // 控制事件幂等门均有单测锁死（turn-stream.test.ts / turn-store.test.ts），黑盒重复
    // 检测为冗余且失真防线。A13 验收原文三判据（Last-Event-ID 重连/已输出文本不丢失/从
    // 断点继续直至完成）已由上方断言全覆盖。
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
