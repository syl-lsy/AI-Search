import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './test/e2e',
  // GUI 黑盒 e2e：不计入 vitest 覆盖率门禁（AGENTS.md：e2e 两端均不计入）
  // timeout 120s：真服务单轮 fast 回答 30~65s（批次 6 实测口径），30s 会掐断流式窗内断言；
  // 深度轮/A13 刷新恢复等长用例在 spec 内单独 test.setTimeout 放大。
  timeout: 120_000,
  // workers=1 强制串行：真实 LLM 轮并发互撞（fast.turn_timeout_s=60 预算）+ Threads 列表
  // 断言依赖「先提问后可见」的确定性顺序——并行只会制造非确定红。
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
