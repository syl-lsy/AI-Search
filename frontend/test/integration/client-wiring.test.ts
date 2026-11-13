import { AxiosHeaders } from 'axios'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { asEnvelope } from '@/mocks/envelope'
import { modelsFixture } from '@/mocks/models'

describe('client 环境接线（frontend-api.md §8.4：baseURL 只读 VITE_API_BASE_URL）', () => {
  afterEach(() => {
    vi.unstubAllEnvs()
    vi.resetModules()
  })

  it('缺省（env 未设）= 同源相对路径：baseURL 空 + 契约完整路径 /api/v1/...（走 vite proxy）', async () => {
    // hermetic：显式 stub 置空（本地 frontend/.env 可能设了直连 origin——用例测「未设」形态
    // 必须与环境解耦；afterEach unstubAllEnvs + resetModules 已有）
    vi.stubEnv('VITE_API_BASE_URL', '')
    vi.resetModules()
    const { client, API_BASE_URL } = await import('@/api/client')
    expect(API_BASE_URL).toBe('')
    expect(client.defaults.baseURL).toBe('')

    const seen: string[] = []
    client.defaults.adapter = vi.fn(async (config) => {
      seen.push(config.url ?? '')
      return { data: asEnvelope(modelsFixture), status: 200, statusText: 'OK', headers: new AxiosHeaders(), config }
    })
    const { fetchModels } = await import('@/api/models')
    await fetchModels()
    // 默认形态下请求同源 /api/v1/models —— vite proxy 转发到 http://127.0.0.1:8300/api/v1/models
    expect(seen[0]).toBe('/api/v1/models')
  })

  it('直连形态：VITE_API_BASE_URL=http://127.0.0.1:8300（CORS 白名单 origin）→ baseURL 透传', async () => {
    vi.stubEnv('VITE_API_BASE_URL', 'http://127.0.0.1:8300')
    vi.resetModules()
    const { client, API_BASE_URL } = await import('@/api/client')
    expect(API_BASE_URL).toBe('http://127.0.0.1:8300')
    expect(client.defaults.baseURL).toBe('http://127.0.0.1:8300')

    client.defaults.adapter = vi.fn(async (config) => {
      // 直连形态下 axios 合成 origin + /api/v1/models（adapter 收到的相对 url 不变，合成在运行时发生）
      return { data: asEnvelope(modelsFixture), status: 200, statusText: 'OK', headers: new AxiosHeaders(), config }
    })
    const { fetchModels } = await import('@/api/models')
    await expect(fetchModels()).resolves.toEqual(modelsFixture)
  })
})
