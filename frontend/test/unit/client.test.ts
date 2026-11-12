import { AxiosError, AxiosHeaders } from 'axios'
import type { AxiosResponse, InternalAxiosRequestConfig, ResponseType } from 'axios'
import { describe, expect, it } from 'vitest'

import {
  ApiError,
  NETWORK_FALLBACK_CODE,
  NETWORK_FALLBACK_MESSAGE,
  isEnvelope,
  normalizeError,
  unwrapEnvelope,
} from '@/api/client'

function makeResponse(data: unknown, responseType?: ResponseType): AxiosResponse {
  const config: InternalAxiosRequestConfig = { headers: new AxiosHeaders() }
  if (responseType !== undefined) {
    config.responseType = responseType
  }
  return {
    data,
    status: 200,
    statusText: 'OK',
    headers: new AxiosHeaders(),
    config,
  }
}

describe('client 信封守卫与解包（frontend-api.md §2 / §8.9③）', () => {
  it('isEnvelope：code/message 为 string 即信封（兼容成功含 data / 错误无 data 两形态）', () => {
    expect(isEnvelope({ code: '0', message: 'ok', data: {} })).toBe(true)
    expect(isEnvelope({ code: '409001', message: 'x', data: null })).toBe(true)
    expect(isEnvelope({ code: '409001', message: '线程不存在' })).toBe(true) // api.md §0 错误信封无 data 键
    expect(isEnvelope({ code: '0', message: 'ok' })).toBe(true) // 防御性容忍：成功信封缺 data
    expect(isEnvelope({ code: 0, message: 'ok', data: {} })).toBe(false)
    expect(isEnvelope({ code: '0', data: {} })).toBe(false)
    expect(isEnvelope('envelope')).toBe(false)
    expect(isEnvelope(null)).toBe(false)
  })

  it('unwrapEnvelope：成功信封剥壳返回 data', () => {
    const payload = { items: [1, 2] }
    expect(unwrapEnvelope(makeResponse({ code: '0', message: 'ok', data: payload }))).toEqual(payload)
  })

  it('unwrapEnvelope：code !== "0" 抛业务 ApiError（message 直达 toast）', () => {
    try {
      unwrapEnvelope(makeResponse({ code: '409001', message: '线程已有进行中轮次' }))
      expect.unreachable()
    } catch (error) {
      expect(error).toBeInstanceOf(ApiError)
      expect((error as ApiError).code).toBe('409001')
      expect((error as ApiError).message).toBe('线程已有进行中轮次')
    }
  })

  it('unwrapEnvelope：信封畸形抛 500001 兜底', () => {
    try {
      unwrapEnvelope(makeResponse({ unexpected: true }))
      expect.unreachable()
    } catch (error) {
      expect((error as ApiError).code).toBe('500001')
    }
  })

  it('unwrapEnvelope：下载类响应（blob）原样放行——不走信封（api.md §4.9/§5.3）', () => {
    const response = makeResponse(new Blob(['x']), 'blob')
    expect(unwrapEnvelope(response)).toBe(response)
  })
})

describe('normalizeError（错误归一）', () => {
  it('HTTP 错误信封 → ApiError(code, 服务端中文 message)', () => {
    // axios.isAxiosError 为鸭子判定（isAxiosError 标记）；此处用裸对象构造响应形态，
    // 避免在测试里拼装 AxiosResponse 的重型类型字面量。
    const duckError: unknown = {
      isAxiosError: true,
      response: { data: { code: '409001', message: '线程已有进行中轮次' }, status: 409 },
    }
    const apiError = normalizeError(duckError)
    expect(apiError.code).toBe('409001')
    expect(apiError.message).toBe('线程已有进行中轮次')
  })

  it('HTTP 错误但无信封体 → 500001 兜底', () => {
    const duckError: unknown = { isAxiosError: true, response: { data: 'oops', status: 500 } }
    expect(normalizeError(duckError).code).toBe('500001')
  })

  it('断连（无 response）→ 网络兜底文案', () => {
    const networkError = new AxiosError('Network Error', AxiosError.ERR_NETWORK)
    const apiError = normalizeError(networkError)
    expect(apiError.code).toBe(NETWORK_FALLBACK_CODE)
    expect(apiError.message).toBe(NETWORK_FALLBACK_MESSAGE)
  })

  it('非 axios 异常 → 网络兜底', () => {
    expect(normalizeError(new Error('boom')).code).toBe(NETWORK_FALLBACK_CODE)
  })
})
