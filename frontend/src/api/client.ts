/**
 * src/api/client.ts — REST 请求唯一出口（frontend-api.md §8.4）
 *
 * 铁律（§8.4/§2）：
 *   - baseURL 只读 import.meta.env.VITE_API_BASE_URL；组件与 store 禁止散写 URL、禁止裸 fetch。
 *   - 信封解包：拦截器统一剥 {"code","message","data"}；code !== "0" 抛业务错误，
 *     message 直接 toast（中文文案服务端已备好）。
 *   - SSE 端点不走本文件——走 src/sse/turnStream.ts（唯一出口之二，F2 落位）。
 *   - 无类型边界纪律（§8.9③）：信封先守卫收窄，再按调用方声明的镜像类型交付。
 */
import axios from 'axios'
import type { AxiosInstance, AxiosRequestConfig, AxiosResponse } from 'axios'

import { isRecord } from '@/utils/guards'

/**
 * baseURL 缺省（未设/空串）= 同源相对路径：REST/SSE 走 vite dev proxy `/api → http://127.0.0.1:8300`
 * （frontend/vite.config.ts）。直连形态填后端 origin（如 http://127.0.0.1:8300）——
 * 请求 URL = origin + 契约完整路径（/api/v1/...），后端 CORS 白名单已放行 :5173 两 origin。
 * REST 模块 URL 一律使用契约完整路径（/api/v1/...），与 SSE 路径书写约定一致（§3）。
 */
export const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL || ''

/** axios 网络层超时（纯前端常量；SSE 不经 axios，不受此限） */
export const AXIOS_TIMEOUT_MS = 30_000

/** 网络/协议层错误的统一兜底文案（服务端 message 缺席时使用；服务端 message 已是中文，§7） */
export const NETWORK_FALLBACK_MESSAGE = '网络异常，请稍后重试'
export const NETWORK_FALLBACK_CODE = 'network_error'

/** 业务错误：code !== "0" 的信封（frontend-api.md §2——message 直接供 toast） */
export class ApiError extends Error {
  readonly code: string
  constructor(code: string, message: string) {
    super(message)
    this.name = 'ApiError'
    this.code = code
  }
}

/**
 * 信封形态守卫（§8.9③：拦截器返回先收窄再用）。
 * 兼容两种契约形态（api.md §0）：成功信封 {"code","message","data"}、
 * 错误信封 {"code","message"}（无 data 键）——故仅要求 code/message 为 string。
 */
export function isEnvelope(value: unknown): value is { code: string; message: string; data?: unknown } {
  return isRecord(value) && typeof value.code === 'string' && typeof value.message === 'string'
}

/** 下载类端点（api.md §4.9 记忆导出 / §5.3 产物下载：FileResponse/302，不走信封） */
function isRawDownloadResponse(response: AxiosResponse): boolean {
  const responseType = response.config.responseType
  return responseType === 'blob' || responseType === 'arraybuffer'
}

/**
 * 信封校验 + 解包：成功返回 data；code !== "0" 抛 ApiError；信封畸形抛 500001 兜底。
 * 下载类响应原样放行（返回整个 AxiosResponse，由 apiDownload 消费）。
 */
export function unwrapEnvelope(response: AxiosResponse): unknown {
  if (isRawDownloadResponse(response)) {
    return response
  }
  const body: unknown = response.data
  if (!isEnvelope(body)) {
    throw new ApiError('500001', NETWORK_FALLBACK_MESSAGE)
  }
  if (body.code !== '0') {
    throw new ApiError(body.code, body.message)
  }
  return body.data
}

/** 任意异常归一为 ApiError（错误信封优先——服务端中文 message 直达 toast） */
export function normalizeError(error: unknown): ApiError {
  if (axios.isAxiosError(error)) {
    const body: unknown = error.response?.data
    if (isEnvelope(body)) {
      return new ApiError(body.code, body.message)
    }
    if (error.response) {
      return new ApiError('500001', NETWORK_FALLBACK_MESSAGE)
    }
    return new ApiError(NETWORK_FALLBACK_CODE, NETWORK_FALLBACK_MESSAGE)
  }
  return new ApiError(NETWORK_FALLBACK_CODE, NETWORK_FALLBACK_MESSAGE)
}

export const client: AxiosInstance = axios.create({
  baseURL: API_BASE_URL,
  timeout: AXIOS_TIMEOUT_MS,
})

/* 拦截器：仅做错误归一（HTTP 4xx/5xx 携带的错误信封 → ApiError）；解包在 request<T> 单点完成 */
client.interceptors.response.use(
  (response) => response,
  (error: unknown) => {
    throw normalizeError(error)
  },
)

/**
 * 单点解包出口：信封经 isEnvelope 守卫收窄后，按调用方声明的镜像类型（src/types/api.d.ts）交付。
 * 此处单次 `as T` 属 §8.9① 允许的收窄边界（信封形态已守卫验证；端点级形态防线 =
 * 镜像类型 + §9 fixture + 评审 diff，§8.9⑥——api.d.ts 非运行时校验器）。
 */
async function request<T>(config: AxiosRequestConfig): Promise<T> {
  const response = await client.request(config)
  const data: unknown = unwrapEnvelope(response)
  return data as T
}

export async function apiGet<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
  return request<T>({ ...config, method: 'GET', url })
}

export async function apiPost<T>(url: string, body?: unknown, config?: AxiosRequestConfig): Promise<T> {
  return request<T>({ ...config, method: 'POST', url, data: body })
}

export async function apiPut<T>(url: string, body?: unknown, config?: AxiosRequestConfig): Promise<T> {
  return request<T>({ ...config, method: 'PUT', url, data: body })
}

export async function apiPatch<T>(url: string, body?: unknown, config?: AxiosRequestConfig): Promise<T> {
  return request<T>({ ...config, method: 'PATCH', url, data: body })
}

export async function apiDelete<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
  return request<T>({ ...config, method: 'DELETE', url })
}

/** 文件下载（download_path / 记忆导出）：不走信封解包，直接取 Blob（api.md §5.3） */
export async function apiDownload(url: string, config?: AxiosRequestConfig): Promise<Blob> {
  const response = await client.get<Blob>(url, { ...config, responseType: 'blob' })
  return response.data
}
