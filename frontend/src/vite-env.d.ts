/// <reference types="vite/client" />

/**
 * VITE_ 环境变量类型声明（frontend-api.md §2：前端只读 import.meta.env.VITE_*）。
 * 无类型边界纪律（§8.9③）：env 读取处经此声明获得字面类型，不做断言。
 */
interface ImportMetaEnv {
  /**
   * 后端 API origin（frontend/api/client.ts 唯一读取）。
   * 未设/空串 = 同源相对路径：REST/SSE 走 vite dev proxy `/api → http://127.0.0.1:8300`；
   * 直连形态填后端 origin（如 http://127.0.0.1:8300），CORS 白名单见 backend core/config.py。
   */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
