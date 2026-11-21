import { defineConfig, devices } from '@playwright/test'

/**
 * 活体探针专用 config（批次 6 收口轮 P2-2）：探针 spec 已迁出回归 testDir（test/e2e/ →
 * frontend/probes/），常规 `playwright test`（testDir=./test/e2e，package.json test:e2e）
 * 不再收集探针；本 config 是探针的唯一运行入口：
 *   pnpm --filter frontend exec playwright test --config playwright.probes.config.ts
 * 探针是活体一次性工件（真轮、证据落盘 docs/design/），不是回归保护面——不计入常规全量、
 * 不计入 vitest 覆盖率门禁。超时/workers/webServer 与主 config 同口径（真服务真轮串行）。
 */
export default defineConfig({
  testDir: './probes',
  timeout: 120_000,
  workers: 1,
  retries: 0,
  use: {
    baseURL: 'http://localhost:5173',
    ...devices['Desktop Chrome'],
  },
  webServer: {
    command: 'pnpm dev',
    url: 'http://localhost:5173',
    reuseExistingServer: true,
    timeout: 60_000,
  },
})
