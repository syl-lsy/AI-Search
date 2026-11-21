/**
 * e2e — deep 产物面套件（批次 6 新增：deep 主链路 + 快照 modal + 导出下载）。
 * 断言口径 = PRD A2/A3 GWT + api.md §2.5/§2.8/§5.1/§5.3/§7.4 + frontend-api.md §5 组件清单；
 * 断结构不断文案（全部走 data-testid / 结构选择器 / 规则可判定正则）。
 *
 * 用例编排（serial + workers=1，单次真服务 deep 轮，②③④ 复用 ① 产出线程零重复烧轮）：
 *   ① deep 主链路：首页发起 deep 单面题 → §7.4 深度序列渲染（时间线/deep 徽标结构面）→
 *      终态（completed/degraded 兜底收束两形态分派）→ TOC 锚点（renderer 注入的标题 id 锚点，
 *      h1~h6 全级 level-agnostic）→ 来源面板 →
 *      终态刷新恢复复核（REST 全量渲染已持久化部分，§3 生命周期 5）；
 *      test.setTimeout 600_000（沿 answer.spec.ts:141 先例——deep 轮总时长不可控）。
 *   ② 快照正向：终态后断言快照胶囊 data-testid=snapshot-{ref_no} 存在（依赖真源
 *      snapshot_available=true——环境供给不可控，无快照源时条件软跳过，骨架②口径）。
 *   ③ 点击胶囊 → SnapshotModal 可见 → excerpt 非空 → 「打开原文」外链 href=http(s)（渲染时）。
 *   ④ 导出正向：操作行更多菜单「导出报告」（门控 = mode=deep ∧ exportAvailable，§2.5 判据
 *      store 对账回写）→ playwright download 事件断言 .md 文件名形态 report_{YYYYMMDD_HHmm}.md
 *      （api.md §2.8 filename 形态）且文件非空。
 *   ⑤ 降级面：DegradedBanner 真轮熔断难稳定构造——由 component 层 fixture 覆盖
 *      （turn-block.test.ts degraded 三枚举用例），e2e 不强设（骨架⑤口径）。
 *      终态分派中 degraded 兜底收束轮只压「banner 在场」结构面（分节锚点断言不适用 digest 形态）。
 *
 * 范围注记：§7.4 stage 顺序按 key 断言归 integration 层（Timeline DOM label 为服务端文案、
 * 不可规则判定）；deep 流式中刷新（A13 第二条）由 answer.spec.ts「深度研究进行中刷新页面」
 * 用例覆盖，本套件只压终态刷新恢复面（单次烧轮纪律）。
 */
import { readFileSync } from 'node:fs'

import { expect, test, type Page } from '@playwright/test'

test.describe.configure({ mode: 'serial' })

/** serial 共享态：① 产出的线程 URL（②③④ 复用同一 deep 轮；前置失败时空串触发 skip） */
let threadUrl = ''

/** 收敛型单面中文事实题（deep 轮题面——单面题避免多面压力熔断，批次 6 先例口径） */
const DEEP_QUESTION = 'Rust 的所有权机制是什么？'

/** 终态状态行正则（completed=已完成 / degraded=研究未完成——i18n zh 双形态均命中） */
const TERMINAL_STATUS = /已完成|研究未完成/

/** ① 通用段：首页发起 deep 轮并跳线程页 */
async function askDeepFromHome(page: Page): Promise<void> {
  await page.goto('/')
  await page.locator('[data-testid="home-search-card"] textarea').fill(DEEP_QUESTION)
  await page.getByTestId('mode-deep').click()
  await page.locator('[data-testid="home-search-card"] [data-testid="submit-btn"]').click()
  await page.waitForURL(/\/thread\//, { timeout: 15_000 })
}

test.describe('deep 产物面（真服务，serial 单烧轮）', () => {
  test('deep 主链路：§7.4 深度序列结构渲染 → 终态（TOC 锚点/分节）+ 终态刷新恢复复核', async ({ page }) => {
    // deep 轮总时长不可控（deepagents 子代理并行研究 + writer 120s 包络），单用例放宽到 10min
    test.setTimeout(600_000)
    await askDeepFromHome(page)
    threadUrl = page.url()

    // 流式进行中：轮块 + deep 模式徽标（badge-outline = deep 形态，TurnBlock :class 派生）+ 时间线
    await expect(page.getByTestId('turn-block').first()).toBeVisible({ timeout: 15_000 })
    await expect(page.locator('[data-testid="turn-block"] .badge-outline').first()).toBeVisible()
    // 时间线随首个 stage(plan,running) SSE 事件渲染（§7.4 深度序列首个事件——确定性存在但
    // 到达时刻受 pre_route 安全检查 LLM 调用延迟影响，二跑实证可 >15s）：60s 宽窗
    await expect(page.getByTestId('timeline').first()).toBeVisible({ timeout: 60_000 })

    // 终态（A2：报告产出；degraded 兜底收束同为合法收敛终态——形态分派沿 main-flow 先例）
    await expect(page.getByTestId('turn-status').first()).toContainText(TERMINAL_STATUS, {
      timeout: 480_000,
    })

    // 降级分派：degraded 兜底收束（digest/partial）显著标注「研究未完成」（A2 第三条），
    // digest 无分节 → 分节锚点断言不适用；completed 形态压 TOC 锚点（renderer.ts postProcess
    // 对 h1~h6 全级注入 id）与分节正文（A2「可跳转目录、分节正文」结构面）
    const degradedCount = await page.getByTestId('degraded-banner').count()
    if (degradedCount > 0) {
      await expect(page.getByTestId('degraded-banner').first()).toBeVisible()
    } else {
      const answer = page.locator('[data-testid="turn-answer"]').first()
      await expect(answer).toBeVisible()
      // TOC 锚点契约 level-agnostic（renderer.ts postProcess 对 h1~h6 全级注入 id）——
      // writer 输出的标题层级为模型行为（本套件首跑实证 h1 形态「# 第 N 节」），不钉 h2
      expect(await answer.locator('h1[id], h2[id], h3[id]').count()).toBeGreaterThanOrEqual(1)
    }

    // 来源面板（deep 轮 sources 非空——A2 快照入口与 [n] 引用的宿主）
    await expect(page.getByTestId('source-panel').first()).toBeVisible({ timeout: 10_000 })
    await expect(page.locator('.src-card').first()).toBeVisible({ timeout: 10_000 })

    // 终态刷新恢复（A13 口径·终态面）：reload → §2.5 REST 全量渲染已持久化部分（终态轮零重订阅），
    // 状态行回到终态且渲染面与刷新前同形态在场
    await page.reload()
    await expect(page.getByTestId('turn-status').first()).toContainText(TERMINAL_STATUS, { timeout: 60_000 })
    const refreshedAnswer = page.locator('[data-testid="turn-answer"]').first()
    await expect
      .poll(
        async () =>
          (await refreshedAnswer.count()) > 0 || (await page.getByTestId('degraded-banner').count()) > 0,
        { timeout: 30_000, intervals: [2_000] },
      )
      .toBe(true)
    await expect(page.getByTestId('source-panel').first()).toBeVisible({ timeout: 10_000 })
  })

  test('快照正向：来源卡快照胶囊在场（snapshot_available 供给条件软跳过）', async ({ page }) => {
    test.skip(threadUrl === '', '前置 deep 主链路用例未产出线程（serial 前置失败整组跳过）')
    await page.goto(threadUrl)
    await expect(page.getByTestId('source-panel').first()).toBeVisible({ timeout: 15_000 })
    await expect(page.locator('.src-card').first()).toBeVisible({ timeout: 15_000 })

    // 快照胶囊 = snapshot_available=true 来源卡行尾相机钮（SourcePanel data-testid=`snapshot-{ref_no}`；
    // 选择器限定 source-panel 子树——SnapshotModal 的 snapshot-modal-* testid 不在其内）
    const capsules = page.locator('[data-testid="source-panel"] [data-testid^="snapshot-"]')
    if ((await capsules.count()) === 0) {
      test.skip(true, '真源快照供给不可控：本轮来源 snapshot_available 均为 false（环境性，骨架②软跳过口径）')
    }
    await expect(capsules.first()).toBeVisible()
  })

  test('快照弹窗：胶囊点击 → SnapshotModal 摘录非空 + 「打开原文」外链 http(s) + 关闭通道', async ({ page }) => {
    test.skip(threadUrl === '', '前置 deep 主链路用例未产出线程（serial 前置失败整组跳过）')
    await page.goto(threadUrl)
    await expect(page.getByTestId('source-panel').first()).toBeVisible({ timeout: 15_000 })
    await expect(page.locator('.src-card').first()).toBeVisible({ timeout: 15_000 })

    const capsules = page.locator('[data-testid="source-panel"] [data-testid^="snapshot-"]')
    if ((await capsules.count()) === 0) {
      test.skip(true, '真源快照供给不可控：本轮来源 snapshot_available 均为 false（环境性，骨架②软跳过口径）')
    }

    // 胶囊点击 → fetchSnapshot（GET §5.1）→ SnapshotModal 预览（弹窗壳 Teleport 至 body）
    await capsules.first().click()
    const modalBody = page.getByTestId('snapshot-modal-body')
    await expect(modalBody).toBeVisible({ timeout: 15_000 })

    // 摘录非空（§5.1 excerpt = 原文开头 2000 字符，服务端截取）
    const excerpt = ((await page.getByTestId('snapshot-excerpt').textContent()) ?? '').trim()
    expect(excerpt.length).toBeGreaterThan(0)
    // 元数据行三键在场（fetched_at/size_bytes/content_type——api.md §5.1 响应字段渲染面）
    await expect(page.getByTestId('snapshot-meta')).toBeVisible()

    // 「打开原文」外链：download_url 过 isHttpUrl 守卫渲染（非 http 形态整钮不渲染）；
    // presigned URL 形态恒 http(s)（§5.1）——渲染时压契约 href 白名单
    const openBtn = page.getByTestId('snapshot-open-original')
    if ((await openBtn.count()) > 0) {
      expect(await openBtn.getAttribute('href')).toMatch(/^https?:\/\//)
    }

    // 关闭通道之一（按钮）→ 弹窗壳随 close 收起（Teleport 遮罩 Hidden）
    await page.getByTestId('snapshot-close').click()
    await expect(page.getByTestId('thread-modal-overlay')).toBeHidden({ timeout: 5_000 })
  })

  test('导出正向：deep 终态轮「导出报告」→ download 文件名 report_{YYYYMMDD_HHmm}.md 形态', async ({ page }) => {
    test.skip(threadUrl === '', '前置 deep 主链路用例未产出线程（serial 前置失败整组跳过）')
    await page.goto(threadUrl)
    await expect(page.getByTestId('turn-status').first()).toContainText(TERMINAL_STATUS, { timeout: 60_000 })

    // 形态分派（四跑实证：deep 终态存在「空正文 degraded」形态〔stages 止于 search、零 write
    // 段、degraded_reason=null、answer_md 空〕）：该形态下 AnswerActions 整行不渲染（TurnBlock
    // v-if answer_md）且 export_available=false（§2.5 判据 answer_md 非空前置）——压「banner
    // 在场 + 导出入口不渲染」结构面后软跳过下载半段（正向由 completed/digest 形态轮覆盖）
    if ((await page.getByTestId('answer-actions').count()) === 0) {
      await expect(page.getByTestId('degraded-banner').first()).toBeVisible()
      await expect(page.getByTestId('act-export-report')).toHaveCount(0)
      test.skip(true, '①轮为空正文 degraded 终态（环境性）：answer_md 空 → export_available=false（§2.5 判据）→ 导出入口不渲染为正确行为')
    }
    await expect(page.getByTestId('answer-actions').first()).toBeVisible({ timeout: 15_000 })
    await page.getByTestId('act-more').first().click()
    const menu = page.getByTestId('act-more-menu')
    await expect(menu).toBeVisible()

    // 本地「导出 Markdown」基础项在场；「导出报告」门控 = mode=deep ∧ exportAvailable
    // （§2.5 判据 store 对账回写——deep 终态非空答案轮恒渲染；A40 负向承载在 fast 轮组件测试）
    await expect(page.getByTestId('act-export-md')).toBeVisible()
    const exportReport = page.getByTestId('act-export-report')
    await expect(exportReport).toBeVisible({ timeout: 15_000 })

    // 点击 → POST §2.8 → download_path 锚点跳转（§5.3）→ 服务端 Content-Disposition 驱动下载
    const downloadPromise = page.waitForEvent('download')
    await exportReport.click()
    const download = await downloadPromise
    // 文件名形态 = report_{YYYYMMDD_HHmm}.md（api.md §2.8 filename 契约，分钟精度）
    expect(download.suggestedFilename()).toMatch(/^report_\d{8}_\d{4}\.md$/)
    // 下载体非空（报告 md 落盘成功；A3「下载文件包含目录、全部分节与引用列表」的
    // 结构面下限——内容一致性断言不断文案）
    expect(await download.failure()).toBeNull()
    const filePath = await download.path()
    if (filePath === null) throw new Error('download.path() 为 null——下载未落盘（§5.3 FileResponse 链路异常）')
    expect(readFileSync(filePath, 'utf8').length).toBeGreaterThan(0)
  })
})
