import { AxiosError, AxiosHeaders } from 'axios'
import type { AxiosAdapter, InternalAxiosRequestConfig } from 'axios'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, client } from '@/api/client'
import { fetchModels } from '@/api/models'
import { fetchSnapshot } from '@/api/sources'
import { fetchSuggestionPool } from '@/api/suggestions'
import { asEnvelope } from '@/mocks/envelope'
import { modelsFixture } from '@/mocks/models'
import { suggestionsFixture } from '@/mocks/suggestions'
import type { SnapshotData } from '@/types/api'

/**
 * unit 层 LLM/网络全 mock（AGENTS.md 测试门禁）：以自定义 adapter 截获请求，
 * 断言 URL/方法契约与信封解包链路，不触网。
 */
const captured: InternalAxiosRequestConfig[] = []

function okAdapter(data: unknown): AxiosAdapter {
  return vi.fn(async (config: InternalAxiosRequestConfig) => {
    captured.push(config)
    return { data, status: 200, statusText: 'OK', headers: new AxiosHeaders(), config }
  })
}

const originalAdapter = client.defaults.adapter

beforeEach(() => {
  captured.length = 0
})

afterEach(() => {
  client.defaults.adapter = originalAdapter
  vi.restoreAllMocks()
})

describe('api 模块（请求唯一出口 client.ts 之上）', () => {
  it('fetchModels：GET /api/v1/models（api.md §3.1），返回镜像类型 data', async () => {
    client.defaults.adapter = okAdapter(asEnvelope(modelsFixture))
    await expect(fetchModels()).resolves.toEqual(modelsFixture)
    expect(captured[0]?.method).toBe('get')
    expect(captured[0]?.url).toBe('/api/v1/models')
  })

  it('fetchSuggestionPool：GET /api/v1/suggestions（api.md §3.2）', async () => {
    client.defaults.adapter = okAdapter(asEnvelope(suggestionsFixture))
    await expect(fetchSuggestionPool()).resolves.toEqual(suggestionsFixture)
    expect(captured[0]?.method).toBe('get')
    expect(captured[0]?.url).toBe('/api/v1/suggestions')
  })

  it('fetchSnapshot：GET /api/v1/sources/{source_id}/snapshot（api.md §5.1；P2：AnswerView 散写 URL 收敛出口面）', async () => {
    const snapshotFixture: SnapshotData = {
      snapshot_id: 'f00d0001-0000-4000-8000-000000000001',
      url: 'https://example.com/article',
      content_type: 'text/html',
      size_bytes: 20480,
      fetched_at: '2026-11-01T08:12:03Z',
      download_url: 'https://minio.local/signed-url',
      excerpt: '开头摘录',
    }
    client.defaults.adapter = okAdapter(asEnvelope(snapshotFixture))
    await expect(fetchSnapshot('f00d0001-0000-4000-8000-000000000001')).resolves.toEqual(snapshotFixture)
    expect(captured[0]?.method).toBe('get')
    expect(captured[0]?.url).toBe('/api/v1/sources/f00d0001-0000-4000-8000-000000000001/snapshot')
  })

  it('业务错误经拦截器归一为 ApiError（code/message 透传）', async () => {
    // adapter 以 resolve 返回 409 信封 → unwrapEnvelope 抛 ApiError（错误信封形态契约）
    client.defaults.adapter = okAdapter({ code: '409001', message: '线程已有进行中轮次' })
    await expect(fetchModels()).rejects.toMatchObject({
      code: '409001',
      message: '线程已有进行中轮次',
      name: 'ApiError',
    })
  })

  it('apiPut/apiPatch/apiDelete：方法与 URL 透传（记忆域 M6 端点的出口面）', async () => {
    const { apiPut, apiPatch, apiDelete } = await import('@/api/client')
    client.defaults.adapter = okAdapter(asEnvelope({ deleted: true }))

    await apiPut('/api/v1/memory/settings', { memory_enabled: false })
    expect(captured[0]?.method).toBe('put')
    expect(captured[0]?.url).toBe('/api/v1/memory/settings')

    await apiPatch('/api/v1/memory/items/7d2f0a1e-1c3b-4e5a-9d8f-0a1b2c3d4e5f', { text: '新文本' })
    expect(captured[1]?.method).toBe('patch')

    await apiDelete('/api/v1/kb/documents/7d2f0a1e-1c3b-4e5a-9d8f-0a1b2c3d4e5f')
    expect(captured[2]?.method).toBe('delete')
  })

  it('apiDownload：blob 响应直取（不走信封；api.md §5.3 download_path 用法）', async () => {
    const blobAdapter: AxiosAdapter = vi.fn(async (config: InternalAxiosRequestConfig) => {
      captured.push(config)
      return {
        data: new Blob(['report-bytes'], { type: 'text/markdown' }),
        status: 200,
        statusText: 'OK',
        headers: new AxiosHeaders(),
        config,
      }
    })
    client.defaults.adapter = blobAdapter
    const blob = await import('@/api/client').then((m) => m.apiDownload('/api/v1/memory/export'))
    expect(blob).toBeInstanceOf(Blob)
    expect(captured[0]?.responseType).toBe('blob')
  })

  it('网络层异常经拒绝拦截器归一（断连 → 网络兜底文案）', async () => {
    client.defaults.adapter = vi.fn(async () => {
      throw new AxiosError('Network Error', AxiosError.ERR_NETWORK)
    })
    await expect(fetchModels()).rejects.toBeInstanceOf(ApiError)
    await expect(fetchModels()).rejects.toMatchObject({ code: 'network_error' })
  })
})
