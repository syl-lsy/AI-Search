import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    // 项目铁律：全部 host 端口绑 127.0.0.1——vite 默认仅监听 [::1]（IPv6 loopback），
    // 显式钉死 IPv4 回环地址（批次 7 e2e 实测：默认形态 curl 127.0.0.1 连不上）。
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      // frontend-api.md 联调约定：/api → http://127.0.0.1:8300。
      // SSE（text/event-stream）经此 proxy 直通：Vite dev server 无压缩中间件、
      // server.proxy 亦无 compress 配置项（官方 server-options 文档核对，2026-11-12），
      // 根 Makefile 注释中的「compress:false」为 webpack-dev-server 语汇，Vite 下无需任何额外配置。
      '/api': {
        target: 'http://127.0.0.1:8300',
        changeOrigin: true,
      },
    },
  },
})
