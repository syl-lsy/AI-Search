/**
 * e2e 骨架 — Threads 历史线程页主链路（批次 7 第三步只写不跑；下一阶段由实测员对真服务执行）。
 * 断言口径 = PRD REQ-008 / frontend-api.md §1 ThreadsView 行；后端数据依赖真服务。
 */
import { expect, test } from '@playwright/test'

test.describe('Threads 历史线程页主链路（真服务）', () => {
  test('页面骨架：标题/副标题 + 内嵌搜索框 + 筛选 chips（已归档 chip 一期隐藏，§1）', async ({ page }) => {
    await page.goto('/threads')
    await expect(page.getByTestId('threads-view')).toContainText('历史')
    await expect(page.getByTestId('threads-search-card')).toBeVisible()
    await expect(page.getByTestId('threads-filter-all')).toBeVisible()
    await expect(page.getByTestId('threads-filter-fast')).toBeVisible()
    await expect(page.getByTestId('threads-filter-deep')).toBeVisible()
    // 「已归档」chip 一期隐藏（无归档数据，frontend-api.md §1 ThreadsView 行备注）
    await expect(page.getByText('已归档')).toHaveCount(0)
  })

  test('列表渲染：GET /threads?limit=50 的 items 倒序展示（标题 + 模式徽标 + 轮次 + 相对时间）', async ({ page }) => {
    await page.goto('/threads')
    // 真服务返回 2.2 items；断言口径 REQ-008：标题/模式徽标/轮次徽标/更新时间
    const rows = page.locator('[data-testid^="thread-row-"]')
    await expect(rows.first()).toBeVisible()
    // 相对时间列可见
    await expect(rows.first().locator('.t-time')).toBeVisible()
  })

  test('筛选 chips：mode 参数生效（REQ-008 徽标体系对应 §1 mode=）', async ({ page }) => {
    await page.goto('/threads')
    await page.getByTestId('threads-filter-deep').click()
    // 真服务：deep 筛选后仅深度线程（或空态）；不报错即为骨架通过，精确口径由实测员按数据核对
    await expect(page.getByTestId('threads-view')).toBeVisible()
  })

  test('点击行 → 跳 /thread/{threadId}（§1 用户动作）', async ({ page }) => {
    await page.goto('/threads')
    const firstRow = page.locator('[data-testid^="thread-row-"]').first()
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

  test('日期分组：今天 / 最近 7 天 / 更早（纯前端分组，§1 备注）', async ({ page }) => {
    await page.goto('/threads')
    // 分组标题至少出现一个（真服务数据足够时三组可见；口径 = 分组标签存在性）
    await expect(page.getByText('今天').or(page.getByText('最近 7 天')).or(page.getByText('更早')).first()).toBeVisible()
  })
})
