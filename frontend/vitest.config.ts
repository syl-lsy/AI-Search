import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { coverageConfigDefaults, defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  test: {
    environment: 'jsdom',
    include: [
      'test/unit/**/*.test.ts',
      'test/integration/**/*.test.ts',
      'test/component/**/*.test.ts',
    ],
    exclude: ['test/e2e/**', 'node_modules/**', 'dist/**'],
    coverage: {
      provider: 'v8',
      include: ['src/**'],
      // main.ts 为启动胶水（挂载副作用），渲染证明由 component/e2e 层承担——排除并在此声明理由；
      // css / *.d.ts / .gitkeep 无运行时代码（.gitkeep 为目录占位），一并排除避免报告噪音
      exclude: [
        'src/main.ts',
        'src/**/*.css',
        'src/**/*.d.ts',
        'src/**/*.gitkeep',
        ...coverageConfigDefaults.exclude,
      ],
      thresholds: {
        // frontend-api.md §9 / AGENTS.md：前端前三层合计 80% 门禁（e2e 不计入）
        statements: 80,
        branches: 80,
        functions: 80,
        lines: 80,
        perFile: false,
      },
    },
  },
})
