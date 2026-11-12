import { expect, test } from '@playwright/test'

/**
 * e2e GUI 黑盒冒烟（AGENTS.md：e2e 不计入覆盖率门禁；必须打真服务/dev server）。
 * webServer 由 playwright.config.ts 自动拉起 vite dev（:5173）。
 */
test('首页骨架：标题与导航渲染（暗色基座）', async ({ page }) => {
  await page.goto('/')
  await expect(page).toHaveTitle(/AI Search/)
  await expect(page.getByTestId('app-shell')).toBeVisible()
  await expect(page.getByTestId('home-view')).toContainText('你想了解什么？')
})
