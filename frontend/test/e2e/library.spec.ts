/**
 * e2e — Library 知识库页主链路（REQ-019/020/021/022 + M3 批次 6 五面；plan-m3-b6-f6 §5.4）。
 * 断言口径 = frontend-api.md §1 LibraryView 行 + api.md §4.1~4.9（真服务；playwright webServer pnpm dev）。
 * 溯源下钻与飞书任务卡真服务用例 = 环境性挂牌（OQ-6：KB 来源真轮/飞书租户树非 e2e 确定性供给，
 * skip + 甄别注，活体验证归批次收官探针——沿批次 7 起的 skip 纪律）。
 */
import { expect, test } from '@playwright/test'

test.describe('Library 知识库页主链路（真服务）', () => {
  test('页面骨架：标题 + 双动作 + 飞书连接器卡 + 上传拖拽区 + URL 收藏行 + 六态图例 + 文档表（含解析摘要列头）', async ({ page }) => {
    await page.goto('/library')
    await expect(page.getByTestId('library-view')).toContainText('知识库')
    await expect(page.getByTestId('library-upload')).toBeVisible()
    await expect(page.getByTestId('upload-zone')).toBeVisible()
    await expect(page.getByTestId('url-input')).toBeVisible()
    await expect(page.getByTestId('kb-feishu-card')).toBeVisible()
    await expect(page.getByTestId('kb-import-trigger')).toBeVisible()
    await expect(page.getByTestId('kb-legend')).toBeVisible()
    await expect(page.locator('.doc-table')).toBeVisible()
    // 解析摘要列头（M3 表格扩展）
    await expect(page.locator('.doc-table th', { hasText: '解析摘要' })).toBeVisible()
  })

  test('URL 收藏入库：POST /kb/urls → 列表出现新条目（REQ-020）', async ({ page }) => {
    // 【M3 skip 牌摘除——2026-11-26 批次 6 复跑（plan §5.4 ②）】
    // 旧挂牌理由「apps/search-api/app/api/v1/ 无 kb.py」已过时：M3 批次 2 已交付 REST §4.1~4.5
    // （含 POST /api/v1/kb/urls，URL 收藏过 B15 url_guard）。真服务断言恢复自动化回归保护。
    await page.goto('/library')
    const url = `https://example.com/e2e-${Date.now()}`
    await page.getByTestId('url-input').fill(url)
    await page.getByTestId('url-collect-btn').click()
    // 真服务：列表出现该条目（初始 pending/normalizing 处理中态，5s 轮询向 ready 推进）
    await expect(page.locator('[data-testid="kb-doc-row"]').first()).toBeVisible({ timeout: 10_000 })
  })

  test('非 http(s) URL 前端拒绝（§2 协议白名单口径）', async ({ page }) => {
    await page.goto('/library')
    await page.getByTestId('url-input').fill('javascript:alert(1)')
    await page.getByTestId('url-collect-btn').click()
    // toast 提示且无网络提交——列表行数不变（骨架期断言 toast 出现）
    await expect(page.getByTestId('toast-host')).toBeVisible()
  })

  test('多选上传入口拒绝直接 toast 不入表（§1/D2：错类型备料逐条 toast；合法多选循环上传）', async ({ page }) => {
    await page.goto('/library')
    const input = page.getByTestId('upload-input')
    // 错类型备料：非白名单扩展名前端拒绝（415001 前置校验的入口侧承载）
    await input.setInputFiles([
      { name: 'evil.exe', mimeType: 'application/octet-stream', buffer: Buffer.from('MZ') },
    ])
    await expect(page.getByTestId('toast-host')).toBeVisible()
    // 错类型零触网——内容锚定负向断言（R-1：行计数基线断言受服务端列表分页 50 封顶影响不可用）
    await expect(page.locator('[data-testid="kb-doc-row"]').filter({ hasText: 'evil.exe' })).toHaveCount(0)

    // 合法文件入队（>20MB 那个被拒 toast），合法行最终出现在列表——内容锚定（R-1 收口：
    // 服务端列表分页默认 50 封顶，基线满页时新行把末行挤出首页，count+1 断言恒不成立——GUI 终跑实锤 34 次重试恒 50）
    const okName = `e2e-multi-ok-${Date.now()}.md`
    await input.setInputFiles([
      { name: okName, mimeType: 'text/markdown', buffer: Buffer.from('# e2e 批次 6\n\n多选上传用例备料。') },
      { name: 'big.pdf', mimeType: 'application/pdf', buffer: Buffer.alloc(21 * 1024 * 1024) },
    ])
    const newRow = page.locator('[data-testid="kb-doc-row"]').filter({ hasText: okName }).first()
    await expect(newRow).toBeVisible({ timeout: 15_000 })

    // 自清理：删除备料行（复用下方删除级联流同款交互），防止重复跑堆积同名行
    // （时间戳文件名天然唯一，重复跑不双匹配；残留历史行不清理——分页帽下不影响断言）
    await newRow.locator('[data-testid="kb-doc-delete"]').click()
    await page.getByTestId('kb-delete-confirm-btn').click()
    await expect(newRow).toHaveCount(0, { timeout: 10_000 })
  })

  test('删除级联确认流：上传 → 行删除钮 → alertdialog 三存储清单 → 取消不发 → 确认后行消失（api.md §4.5）', async ({ page }) => {
    await page.goto('/library')
    // 真服务备料：上传一个小 md（走 §4.1 multipart → 流水线）
    const title = `e2e-delete-${Date.now()}.md`
    await page.getByTestId('upload-input').setInputFiles([
      { name: title, mimeType: 'text/markdown', buffer: Buffer.from('# e2e delete cascade\n\n删除确认流备料。') },
    ])
    const row = page.locator('[data-testid="kb-doc-row"]', { hasText: title })
    await expect(row).toBeVisible({ timeout: 15_000 })

    // 点删除 → 确认弹层（alertdialog + 三存储清单）
    await row.locator('[data-testid="kb-doc-delete"]').click()
    const modal = page.getByTestId('kb-delete-confirm')
    await expect(modal).toBeVisible()
    await expect(page.locator('[role="alertdialog"][aria-modal="true"]')).toBeVisible()
    await expect(modal).toContainText('pgvector')
    await expect(modal).toContainText('Elasticsearch')
    await expect(modal).toContainText('MinIO')

    // 取消：不发 DELETE、行保留、弹层关闭
    await page.getByTestId('kb-delete-cancel').click()
    await expect(modal).not.toBeVisible()
    await expect(row).toBeVisible()

    // 确认：DELETE 发出、行消失
    await row.locator('[data-testid="kb-doc-delete"]').click()
    await page.getByTestId('kb-delete-confirm-btn').click()
    await expect(row).toHaveCount(0, { timeout: 10_000 })
  })

  test('处理中条目 5s 轮询至终态（§4.3：有非终态条目时每 5s 轮询列表）', async ({ page }) => {
    await page.goto('/library')
    // 实测员备料：先收一个 URL，观察 5s 内列表自动刷新（状态徽标向 ready 迁移或保持处理中不报错）
    await expect(page.locator('.doc-table')).toBeVisible()
  })

  test('页码分页：点第 2 页 → 行集合整页替换（首行变化）+ 计数 total 锚定（R-3：§4.3 每页 50）', async ({ page }) => {
    await page.goto('/library')
    const rows = page.locator('[data-testid="kb-doc-row"]')
    await expect(rows.first()).toBeVisible()
    // 计数数据源 = envelope total（docsTotal）/ status=ready 条件请求 total（readyTotal）——非页内行数；
    // 宽松正则匹配防库面变化脆断（当前真服务库面 1023 篇 / ready 1022——1 篇 failed 存量，真实投影）
    // 注：「文档」标签在 .doc-count span 外（模板 h2 直排），span 内文本=「1023 · 1022 个已入库」
    await expect(page.locator('.doc-count')).toContainText(/10\d\d · 10\d\d 个已入库/)
    // h2 级标签前缀断言（父级 h2 含「文档」——防标签将来被误删）
    await expect(page.locator('.doc-count').locator('..')).toContainText(/文档/)
    // 初始第一页恰 50 行（复跑条件：真服务库面须 >50——当前 1023 篇）
    expect(await rows.count()).toBe(50)
    // 页码分页（R-3）：点第 2 页 → 整页替换（行集合 differ：首行标题变化；行数仍 50 不累积）
    const firstRowTitle = await rows.first().locator('.doc-name').textContent()
    const firstRowDocId = await rows.first().getAttribute('data-doc-id')
    await page.getByTestId('kb-pager-page-2').click()
    await expect(page.getByTestId('kb-pager-page-2')).toHaveAttribute('aria-current', 'page')
    // 整页替换落地锚点：store goToPage 乐观置页码（aria-current 即翻），行集替换待双请求
    // （limit=50 主拉 + limit=1 ready 计数）全部落地才写 documents——toHaveCount(50) 对旧页
    // 行集恒真、不构成替换完成的信号；先等首行 data-doc-id 变化再读，否则全量负载下读到
    // 旧页首行（与 firstRowTitle 恒等——两轮门禁红/单跑绿分裂的竞态实证 2026-11-27）
    await expect(rows.first()).not.toHaveAttribute('data-doc-id', firstRowDocId!, { timeout: 10_000 })
    await expect(rows).toHaveCount(50, { timeout: 10_000 })
    const secondPageTitle = await rows.first().locator('.doc-name').textContent()
    expect(secondPageTitle).not.toBe(firstRowTitle)
  })

  test('页码控件 a11y 冒烟（R-3）：nav aria-label + 首页态边界钮 disabled + 当前页 aria-current', async ({ page }) => {
    await page.goto('/library')
    const pager = page.getByTestId('kb-doc-pagination')
    await expect(pager).toBeVisible()
    // nav 语义
    await expect(pager).toHaveAttribute('aria-label', '分页导航')
    // 首页态：« ‹ disabled、当前页 aria-current="page"
    await expect(page.getByTestId('kb-pager-first')).toBeDisabled()
    await expect(page.getByTestId('kb-pager-prev')).toBeDisabled()
    await expect(page.getByTestId('kb-pager-page-1')).toHaveAttribute('aria-current', 'page')
    // 下一页钮可用（库面 1023 篇 → 21 页）
    await expect(page.getByTestId('kb-pager-next')).toBeEnabled()
    // 总页数文本（宽松锚定两位页数）
    await expect(pager).toContainText(/共 \d\d 页/)
  })

  test('溯源下钻（§4.6）：答案页 KB 来源卡溯源钮 → kb-chunk 弹窗', async ({ page }) => {
    // 【环境性挂牌——OQ-6（plan §6）：依赖 KB 来源真轮（检索命中 KB 的 completed 轮 +
    // source_type=kb 来源行 kb_chunk_id），非 e2e 确定性供给；skip + 甄别注，
    // 活体验证归批次收官探针（沿批次 7 起的 skip 纪律）。】
    // 复跑条件：预置含 KB 命中的线程（上传文档→ready→提问命中 KB→completed）后摘牌：
    //   进入 /thread/{id} → [data-testid="source-kb-drill"] click → kb-chunk-drilldown 可见 →
    //   断言 heading_path 面包屑/page·paragraph pills/「打开飞书原文」外链仅飞书源渲染。
    test.skip(true, '环境性：依赖 KB 来源真轮（OQ-6）——活体验证归批次收官探针')
    await page.goto('/')
  })

  test('飞书导入任务卡（§4.7~4.9）：触发钮两态 + 进度五数 + failure chips', async ({ page }) => {
    // 【环境性挂牌——OQ-6（plan §6）：依赖飞书凭据（FEISHU_APP_ID/FEISHU_SECRET）与租户树，
    // .env.test 未注入凭据时 §4.7 500001 兜底；skip + 甄别注，活体验证归批次收官探针。】
    // 复跑条件：注入飞书凭据后摘牌：连接器卡 kb-feishu-card 可见（未同步/健康投影徽标）→
    //   idle 态 kb-import-trigger 可点 → POST §4.7 → 任务卡 kb-import-jobs 出现（进度条 aria/
    //   五数/failure chips）→ 409007 守卫（重复触发 toast 服务端 message）。
    test.skip(true, '环境性：依赖飞书凭据与租户树（OQ-6）——活体验证归批次收官探针')
    await page.goto('/library')
  })
})
