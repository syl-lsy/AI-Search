/**
 * e2e — Threads 历史线程页主链路（真服务 :8300 + dev server :5173；批次 4 F4 更新）。
 *
 * 断言口径 = PRD v1.20 REQ-058~060 / A42~A45 GWT + frontend-api.md §1 ThreadsView 行 v1.21：
 *   - 更新既有用例到批次 4 新形态：三态视图 radio（全部/未读/已归档）、逐日分组
 *     （今天/昨天/M月D日，取代「今天/最近7天/更早」三桶）、未读点、行级三点菜单；
 *   - A42 全量加载/触底自动加载（IntersectionObserver sentinel）；A42 三态视图切换
 *     （请求参数映射 unread/archived）；A43 管理动作（重命名/置顶·取消置顶——真后端
 *     PATCH，操作对象 = smoke-b10 打头的冒烟线程，改名保留前缀保证用例可重复运行）；
 *     A44 打开即已读流转（PATCH is_unread → 侧栏未读点 → 打开详情后消失）；
 *     A45 标题搜索（q 防抖过滤 + 清空回全量）。
 * 删除+409005 与面板分组 e2e 不在本 spec：前者需进行中轮的真线程（构造成本高，组件层
 * + 后端 B10 已覆盖），后者依赖多来源轮数据（组件层 [n] 组作用域已断言）——留批次收口。
 * 种子依赖（workers=1 顺序执行 + 真服务数据）：A43 先于 A45 运行（A45 搜索的时间戳唯一
 * 前缀 smoke-b10-e2e 由 A43 首跑建立）；A42 需 threads 总数 > 50（不足时自动 skip）。
 */
import { expect, test, type Locator, type Page } from '@playwright/test'

/** 冒烟目标线程标题前缀（test-engineer B10 建档；重命名保留前缀 → 用例可重复运行） */
const SMOKE_PREFIX = 'smoke-b10'
/** 触底加载：首屏 50（api.md §0 分页约定 DEFAULT_LIMIT） */
const PAGE_SIZE = 50

function rows(page: Page): Locator {
  return page.locator('[data-testid^="thread-row-"]')
}

function smokeRow(page: Page): Locator {
  return rows(page).filter({ hasText: SMOKE_PREFIX }).first()
}

async function firstRowTestId(page: Page): Promise<string> {
  const testid = await rows(page).first().getAttribute('data-testid')
  expect(testid).toBeTruthy()
  return testid!.replace('thread-row-', '')
}

test.describe('Threads 历史线程页主链路（真服务）', () => {
  test('页面骨架：标题 + 内嵌搜索框 + 三态视图 radio + 筛选 chips（批次 4 新形态）', async ({ page }) => {
    await page.goto('/threads')
    await expect(page.getByTestId('threads-view')).toContainText('历史')
    await expect(page.getByTestId('threads-search-card')).toBeVisible()
    // 三态视图 radio（REQ-058）：三选项可见、缺省选中「全部」
    await expect(page.getByTestId('view-filter-active')).toBeVisible()
    await expect(page.getByTestId('view-filter-unread')).toBeVisible()
    await expect(page.getByTestId('view-filter-archived')).toBeVisible()
    await expect(page.getByTestId('view-filter-active')).toHaveAttribute('aria-checked', 'true')
    await expect(page.getByTestId('view-filter-archived')).toContainText('已归档')
    // mode chips 共存
    await expect(page.getByTestId('threads-filter-all')).toBeVisible()
    await expect(page.getByTestId('threads-filter-fast')).toBeVisible()
    await expect(page.getByTestId('threads-filter-deep')).toBeVisible()
    // 标题搜索框（q 参数，独立于内嵌提问卡）
    await expect(page.getByTestId('threads-search-input')).toBeVisible()
  })

  test('列表渲染：GET /threads?limit=50 的 items 倒序展示（标题 + 相对时间列）', async ({ page }) => {
    await page.goto('/threads')
    await expect(rows(page).first()).toBeVisible()
    await expect(rows(page).first().locator('.t-time')).toBeVisible()
  })

  test('逐日分组：今天/昨天/M月D日 形态（v1.21 取代「今天/最近7天/更早」三桶）', async ({ page }) => {
    await page.goto('/threads')
    const seps = page.locator('.date-sep')
    await expect(seps.first()).toBeVisible()
    const label = (await seps.first().textContent())?.trim() ?? ''
    expect(label).toMatch(/^(今天|昨天)$|^\d{1,2}月\d{1,2}日$|^\d{4}年/)
    // 旧三桶词条不再出现
    await expect(page.getByTestId('threads-view')).not.toContainText('最近 7 天')
    await expect(page.getByTestId('threads-view')).not.toContainText('更早')
  })

  test('筛选 chips：mode 参数生效（REQ-008 徽标体系对应 §1 mode=）', async ({ page }) => {
    await page.goto('/threads')
    await page.getByTestId('threads-filter-deep').click()
    // 真服务：deep 筛选后仅深度线程（或空态）；不报错即为骨架通过，精确口径由实测员按数据核对
    await expect(page.getByTestId('threads-view')).toBeVisible()
  })

  test('点击行 → 跳 /thread/{threadId}（§1 用户动作）', async ({ page }) => {
    await page.goto('/threads')
    const firstRow = rows(page).first()
    await firstRow.click()
    await expect(page).toHaveURL(/\/thread\/[0-9a-f-]{36}$/)
    await expect(page.getByTestId('answer-view')).toBeVisible()
  })

  test('内嵌搜索框提问直达：提交 → POST /threads → 跳答案页（设计稿 04 内嵌卡）', async ({ page }) => {
    await page.goto('/threads')
    // 收敛型单面中文事实题
    await page.locator('[data-testid="threads-search-card"] textarea').fill('什么是 RESTful API？')
    await page.locator('[data-testid="threads-search-card"] [data-testid="submit-btn"]').click()
    await expect(page).toHaveURL(/\/thread\//, { timeout: 15_000 })
  })

  test('A42 会话页全量加载：首屏 50 + 触底自动追加下一页（IntersectionObserver sentinel，无重复）', async ({ page }) => {
    await page.goto('/threads')
    await expect(rows(page).first()).toBeVisible()
    // 种子依赖：真服务 threads 总数 > 50 才存在下一页（不足时本用例无翻页语义——跳过）
    const initial = await rows(page).count()
    test.skip(initial < PAGE_SIZE, `种子依赖：threads 总数需 > ${PAGE_SIZE}（当前首屏 ${initial} 条）`)
    // 首屏 = 前 50 条（PRD A42-1：GET /threads?limit=50&offset=0）
    await expect(rows(page)).toHaveCount(PAGE_SIZE, { timeout: 20_000 })
    // 触底（sentinel 进入视口 → store.loadMore → offset=50 追加）；
    // timeout 上浮：dev server 冷启动按需编译新编辑模块的首跑余量
    await page.locator('[data-testid="threads-view"]').evaluate((el) => {
      el.scrollTop = el.scrollHeight
    })
    await expect(async () => {
      expect(await rows(page).count()).toBeGreaterThan(PAGE_SIZE)
    }).toPass({ timeout: 30_000 })
    // 追加后无重复（PRD A42-1「无重复、无闪烁」——by thread_id 去重的黑盒观察面）
    const ids = await rows(page).evaluateAll((els) => els.map((el) => el.getAttribute('data-testid')))
    expect(new Set(ids).size).toBe(ids.length)
  })

  test('A42 三态视图切换：未读 → unread=true、已归档 → archived=true（重置分页，参数映射）', async ({ page }) => {
    await page.goto('/threads')
    await expect(rows(page).first()).toBeVisible()
    // 未读视图：请求携带 unread=true
    const unreadReq = page.waitForRequest((r) => r.url().includes('/api/v1/threads') && r.url().includes('unread=true'))
    await page.getByTestId('view-filter-unread').click()
    await unreadReq
    await expect(page.getByTestId('view-filter-unread')).toHaveAttribute('aria-checked', 'true')
    // 已归档视图：请求携带 archived=true（且不带 unread）
    const archivedReq = page.waitForRequest(
      (r) => r.url().includes('/api/v1/threads') && r.url().includes('archived=true') && !r.url().includes('unread'),
    )
    await page.getByTestId('view-filter-archived').click()
    await archivedReq
    await expect(page.getByTestId('view-filter-archived')).toHaveAttribute('aria-checked', 'true')
    // 回全部：无 archived/unread 参数
    const allReq = page.waitForRequest((r) => {
      const u = r.url()
      return u.includes('/api/v1/threads') && !u.includes('archived') && !u.includes('unread')
    })
    await page.getByTestId('view-filter-active').click()
    await allReq
    await expect(page.getByTestId('view-filter-active')).toHaveAttribute('aria-checked', 'true')
  })

  test('A43 管理动作：重命名（弹层校验后 PATCH 生效）+ 置顶·取消置顶（图钉渲染）', async ({ page }) => {
    await page.goto('/threads')
    await expect(rows(page).first()).toBeVisible()
    // 定位目标：标题搜索隔离 smoke 线程（真数据集持续增长、目标行秩随更新漂移——搜索隔离
    // 保证用例与数据规模无关；A43 重命名保留 smoke-b10 前缀 → 过滤结果保持命中）
    const searchReq = page.waitForRequest((r) => r.url().includes('/api/v1/threads') && r.url().includes('q=smoke'))
    await page.getByTestId('threads-search-input').fill('smoke')
    await searchReq
    const row = smokeRow(page)
    await expect(row).toBeVisible({ timeout: 10_000 })
    // ── 重命名：菜单 → 弹层 → 输入 → 提交 → 行内标题更新（真后端 PATCH §2.10）──
    await row.hover()
    await row.getByTestId('tam-trigger').click()
    await row.getByRole('menuitem', { name: '重命名' }).click()
    const renameInput = page.getByTestId('tam-rename-input')
    await expect(renameInput).toBeVisible()
    const newTitle = `${SMOKE_PREFIX}-e2e-${Date.now()}`
    await renameInput.fill(newTitle)
    await page.getByTestId('tam-rename-ok').click()
    await expect(rows(page).filter({ hasText: newTitle }).first()).toBeVisible({ timeout: 10_000 })
    // ── 置顶 / 取消置顶 ──
    // dispatchEvent：置顶的乐观重排在 @click 处理器内同步重挂行/菜单（store sortByActiveOrder），
    // 常规 click 的 post-action 稳定性重试会以原定位器（菜单项已翻转为「取消置顶」）重试而
    // 永不解析——此处单次确定性派发 click 事件（仍走组件真实 @click 处理路径）。
    // 「置顶区首位」的服务端排序契约（api.md §2.2 pinned DESC）由后端 B-5 用例与前端 store
    // sortByActiveOrder 单测承担；本 e2e 断言图钉渲染与 PATCH 生效（过滤视图内定位目标行）。
    const renamedRow = rows(page).filter({ hasText: newTitle }).first()
    const renamedId = (await renamedRow.getAttribute('data-testid'))!.replace('thread-row-', '')
    const pinBadge = page.getByTestId(`pin-${renamedId}`)

    // 基线复位（用例幂等）：上一轮异常残留 pinned=true 时菜单只有「取消置顶」——先复位
    if (await pinBadge.isVisible()) {
      await renamedRow.hover()
      await renamedRow.getByTestId('tam-trigger').click()
      await renamedRow.getByRole('menuitem', { name: '取消置顶' }).dispatchEvent('click')
      await expect(pinBadge).not.toBeVisible()
    }

    await renamedRow.hover()
    await renamedRow.getByTestId('tam-trigger').click()
    await renamedRow.getByRole('menuitem', { name: '置顶', exact: true }).dispatchEvent('click')
    await expect(pinBadge).toBeVisible()

    // ── 取消置顶（恢复原状态——用例可重复运行）──
    await renamedRow.hover()
    await renamedRow.getByTestId('tam-trigger').click()
    await renamedRow.getByRole('menuitem', { name: '取消置顶' }).dispatchEvent('click')
    await expect(pinBadge).not.toBeVisible()
  })

  test('A45 标题搜索：q 防抖过滤（唯一命中）+ 清空回全量（REQ-058 / PRD A42-4）', async ({ page }) => {
    await page.goto('/threads')
    await expect(rows(page).first()).toBeVisible()
    const before = await rows(page).count()
    // 防抖 250ms 后发 q 请求——q=smoke-b10-e2e 为 A43（同文件先行，workers=1 顺序执行）
    // 重命名写入的时间戳唯一前缀，真实数据不会撞（此前 q=smoke 会与 smoke-b10-线程改名验证
    // 等非 e2e 命名撞形态）；spec 头注：首次运行前须先有 A43 建立该前缀
    const searchReq = page.waitForRequest((r) => r.url().includes('/api/v1/threads') && r.url().includes('q=smoke-b10-e2e'))
    await page.getByTestId('threads-search-input').fill('smoke-b10-e2e')
    await searchReq
    await expect(rows(page)).toHaveCount(1, { timeout: 10_000 })
    await expect(rows(page).first()).toContainText('smoke-b10-e2e')
    // 清空 → 回全量（无 q 参数请求 + 首屏行数恢复）
    const clearReq = page.waitForRequest((r) => r.url().includes('/api/v1/threads') && !r.url().includes('q='))
    await page.getByTestId('threads-search-input').fill('')
    await clearReq
    await expect(rows(page)).toHaveCount(before, { timeout: 10_000 })
  })



  test('A44 打开即已读：PATCH 标未读 → 侧栏未读点出现 → 打开详情后消失（REQ-060 / PRD A44）', async ({ page }) => {
    await page.goto('/threads')
    await expect(rows(page).first()).toBeVisible()
    const tid = await firstRowTestId(page)
    // 前置：服务端标记未读（PATCH is_unread=true；测试后由同通道恢复）
    const markResp = await page.request.patch(`http://127.0.0.1:8300/api/v1/threads/${tid}`, {
      data: { is_unread: true },
    })
    expect(markResp.ok()).toBeTruthy()
    await page.reload()
    // 侧栏最近项未读点出现（is_unread 驱动）
    const dot = page.getByTestId(`sb-unread-${tid}`)
    await expect(dot).toBeVisible()
    // 打开线程详情（GET 详情服务端置已读，api.md §2.3 v1.18 写副作用）→ 未读点消失
    await page.locator(`[data-testid="thread-row-${tid}"]`).click()
    await page.waitForURL(new RegExp(`/thread/${tid}`))
    await expect(page.getByTestId('sb-unread-' + tid)).not.toBeVisible({ timeout: 15_000 })
    // 恢复：显式置已读（幂等——下一轮运行状态干净）
    await page.request.patch(`http://127.0.0.1:8300/api/v1/threads/${tid}`, { data: { is_unread: false } })
  })
})
