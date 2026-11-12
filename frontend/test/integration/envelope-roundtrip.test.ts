import { AxiosHeaders } from 'axios'
import type { AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import { describe, expect, it } from 'vitest'

import { isEnvelope, unwrapEnvelope } from '@/api/client'
import { asEnvelope } from '@/mocks/envelope'
import { modelsFixture } from '@/mocks/models'
import { suggestionsFixture } from '@/mocks/suggestions'
import { threadListFixture } from '@/mocks/threads'

/** 信封回环联装：fixture → 服务端信封形态 → client 守卫收窄 → data 还原（§8.9③⑥ 防线） */
describe('信封回环（mocks × client 联装）', () => {
  it.each([
    ['models', modelsFixture],
    ['suggestions', suggestionsFixture],
    ['threads', threadListFixture],
  ])('%s fixture 经信封包装后可通过守卫并完整还原', (_name, data) => {
    const envelope = asEnvelope(data)
    expect(isEnvelope(envelope)).toBe(true)

    const response: AxiosResponse = {
      data: envelope,
      status: 200,
      statusText: 'OK',
      headers: new AxiosHeaders(),
      config: { headers: new AxiosHeaders() } as InternalAxiosRequestConfig,
    }
    expect(unwrapEnvelope(response)).toEqual(data)
  })

  it('错误信封形态可被守卫识别（供 normalizeError 提取 code/message）', () => {
    const errorEnvelope = { code: '409001', message: '线程已有进行中轮次' }
    expect(isEnvelope(errorEnvelope)).toBe(true)
  })
})
