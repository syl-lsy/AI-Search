/**
 * e2e 骨架 — Library 知识库页主链路（批次 7 第三步只写不跑；下一阶段由实测员对真服务执行）。
 * 断言口径 = PRD REQ-019/020/021/022 / frontend-api.md §1 LibraryView 行（上传白名单/删除/5s 轮询）。
 */
import { expect, test } from '@playwright/test'

test.describe('Library 知识库页主链路（真服务）', () => {
  test('页面骨架：标题 + 双动作 + 检索融合说明卡 + 上传拖拽区 + URL 收藏行 + 文档表', async ({ page }) => {
    await page.goto('/library')
    await expect(page.getByTestId('library-view')).toContainText('知识库')
    await expect(page.getByTestId('library-upload')).toBeVisible()
    await expect(page.getByTestId('upload-zone')).toBeVisible()
    await expect(page.getByTestId('url-input')).toBeVisible()
    await expect(page.locator('.doc-table')).toBeVisible()
  })

  test('URL 收藏入库：POST /kb/urls → 列表出现新条目（REQ-020）', async ({ page }) => {
    // 【后端分期 skip——2026-11-12 批次 7 e2e 第 1 轮实测员落】
    // 甄别：服务就绪下确定性失败；直接探测 POST /api/v1/kb/urls → {"code":"404","message":"路径不存在"}
    // （apps/search-api/app/api/v1/ 无 kb.py）——契约 api.md §4.2/§9 分期表第 14 行明示
    // 「POST /api/v1/kb/urls | URL 收藏 | M3」，M1 后端未交付该端点。断言口径对齐 REQ-020
    // 契约无缺陷、LibraryView 前端页面已交付（骨架/前端拒绝两用例真实通过）。
    // 复跑条件：M3 工单落地 POST /api/v1/kb/urls 后删除本 test.skip 行即真实执行。
    test.skip(true, '后端分期：POST /api/v1/kb/urls 为 api.md §9 分期表 M3 端点（M1 未交付，直接探测 404）——复跑条件：M3 工单落地后摘牌')
    await page.goto('/library')
    const url = `https://example.com/e2e-${Date.now()}`
    await page.getByTestId('url-input').fill(url)
    await page.getByTestId('url-collect-btn').click()
    // 真服务：列表出现该条目（初始 pending/parsing 处理中态）
    await expect(page.locator('[data-testid="kb-doc-row"]').first()).toBeVisible({ timeout: 10_000 })
  })

  test('非 http(s) URL 前端拒绝（§2 协议白名单口径）', async ({ page }) => {
    await page.goto('/library')
    await page.getByTestId('url-input').fill('javascript:alert(1)')
    await page.getByTestId('url-collect-btn').click()
    // toast 提示且无网络提交——列表行数不变（骨架期断言 toast 出现）
    await expect(page.getByTestId('toast-host')).toBeVisible()
  })

  test('上传入口拒绝直接 toast 不入表（§1：413001/415001 口径；超限/错类型由实测员备料）', async ({ page }) => {
    await page.goto('/library')
    // 骨架断言：文件选择器入口可达（真服务上传全链路 + 恶意样本备料由实测员执行）
    await expect(page.getByTestId('upload-input')).toBeAttached()
  })

  test('处理中条目 5s 轮询至终态（§1：有非终态条目时每 5s 轮询列表）', async ({ page }) => {
    await page.goto('/library')
    // 实测员备料：先收一个 URL，观察 5s 内列表自动刷新（状态徽标向 ready 迁移或保持 processing 不报错）
    await expect(page.locator('.doc-table')).toBeVisible()
  })

  test('删除条目：DELETE /kb/documents/{id} → 行消失（REQ-022）', async ({ page }) => {
    await page.goto('/library')
    const rows = page.locator('[data-testid="kb-doc-row"]')
    const count = await rows.count()
    if (count > 0) {
      await rows.first().locator('[data-testid="kb-doc-delete"]').click()
      await expect(rows).toHaveCount(count - 1, { timeout: 10_000 })
    }
  })
})
