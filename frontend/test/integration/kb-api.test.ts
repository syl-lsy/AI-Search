/**
 * test/integration/kb-api.test.ts — src/api/kb.ts 九函数对 client.ts 唯一出口接线
 * （api.md §4.1~4.9；frontend-api.md §4 kbStore 行「经 src/api/kb.ts 唯一出口」）。
 * adapter 截获请求断言 URL/method/payload + 信封 roundtrip（沿 api-modules.test.ts 先例）；
 * multipart 形态（FormData 字段 file+title，勿手写 Content-Type——§1 LibraryView 行）；
 * §4.6 可选键缺省形态（node_token/node_id 缺则省略不置 null——api.md §4.6 v1.31）。
 */
import { AxiosHeaders } from 'axios'
import type { AxiosAdapter, InternalAxiosRequestConfig } from 'axios'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  collectUrl,
  fetchDocument,
  fetchDocuments,
  fetchImportJobDetail,
  fetchImportJobs,
  fetchKbChunk,
  removeDocument,
  triggerImport,
  uploadDocument,
} from '@/api/kb'
import { ApiError, client } from '@/api/client'
import { asEnvelope } from '@/mocks/envelope'
import type { KbChunkDetail, KbDocument, KbImportJob, KbImportJobDetail } from '@/types/api'

const captured: InternalAxiosRequestConfig[] = []

function okAdapter(data: unknown): AxiosAdapter {
  return vi.fn(async (config: InternalAxiosRequestConfig) => {
    captured.push(config)
    return { data, status: 200, statusText: 'OK', headers: new AxiosHeaders(), config }
  })
}

const originalAdapter = client.defaults.adapter

function doc(partial: Partial<KbDocument> & { document_id: string }): KbDocument {
  return {
    doc_type: 'md',
    title: `doc-${partial.document_id}`,
    file_size_bytes: 1024,
    status: 'ready',
    fail_reason: null,
    chunk_count: 3,
    created_at: '2026-11-01T08:12:00Z',
    ...partial,
  }
}

function job(partial: Partial<KbImportJob> & { job_id: string }): KbImportJob {
  return {
    connector: 'feishu',
    trigger: 'manual',
    status: 'running',
    total_docs: 931,
    processed_docs: 312,
    imported_docs: 306,
    failed_docs: 4,
    skipped_docs: 2,
    failure_summary: [{ category: 'block_fetch_failed', count: 3 }],
    started_at: '2026-11-22T08:00:00Z',
    finished_at: null,
    created_at: '2026-11-22T07:59:58Z',
    updated_at: '2026-11-22T08:12:30Z',
    ...partial,
  }
}

function chunk(partial: Partial<KbChunkDetail>): KbChunkDetail {
  return {
    chunk_id: '5c2e0001-0000-4000-8000-000000000001',
    document_id: '9a770001-0000-4000-8000-000000000002',
    document_title: '内部设计文档-检索融合.md',
    content: 'KB 候选与网络候选合并后统一 rerank...',
    heading_path: ['第三章 检索融合', '3.2 混合检索'],
    page: 12,
    paragraph: 4,
    ...partial,
  }
}

beforeEach(() => {
  captured.length = 0
})

afterEach(() => {
  client.defaults.adapter = originalAdapter
  vi.restoreAllMocks()
})

describe('api/kb.ts — KB 文档面（§4.1~4.5）', () => {
  it('fetchDocuments：GET /api/v1/kb/documents（§4.3）→ 列表信封 data roundtrip', async () => {
    const listFixture = { total: 1, items: [doc({ document_id: 'a' })] }
    client.defaults.adapter = okAdapter(asEnvelope(listFixture))
    await expect(fetchDocuments()).resolves.toEqual(listFixture)
    expect(captured[0]?.method).toBe('get')
    expect(captured[0]?.url).toBe('/api/v1/kb/documents')
  })

  it('uploadDocument：POST /api/v1/kb/documents multipart（FormData 字段 file+title；§4.1）', async () => {
    const uploadFixture = doc({ document_id: 'new', doc_type: 'pdf', status: 'pending', file_size_bytes: 1048576 })
    client.defaults.adapter = okAdapter(asEnvelope(uploadFixture))
    const file = new File(['%PDF-1.4'], 'spec.pdf', { type: 'application/pdf' })
    await expect(uploadDocument(file, 'spec.pdf')).resolves.toEqual(uploadFixture)
    expect(captured[0]?.method).toBe('post')
    expect(captured[0]?.url).toBe('/api/v1/kb/documents')
    const form = captured[0]?.data
    expect(form).toBeInstanceOf(FormData)
    expect((form as FormData).get('file')).toBeInstanceOf(File)
    expect((form as FormData).get('title')).toBe('spec.pdf')
  })

  it('collectUrl：POST /api/v1/kb/urls 携带 {url}（§4.2 REQ-020）→ duplicated 投影透传', async () => {
    const urlDoc = doc({ document_id: 'u1', doc_type: 'url', source_url: 'https://example.com/post', duplicated: true })
    client.defaults.adapter = okAdapter(asEnvelope(urlDoc))
    await expect(collectUrl('https://example.com/post')).resolves.toEqual(urlDoc)
    expect(captured[0]?.method).toBe('post')
    expect(captured[0]?.url).toBe('/api/v1/kb/urls')
    const raw = captured[0]?.data
    const body: unknown = typeof raw === 'string' ? JSON.parse(raw) : raw
    expect(body).toEqual({ url: 'https://example.com/post' })
  })

  it('fetchDocument：GET /api/v1/kb/documents/{id}（§4.4——下钻文档面 parse_meta/catalog_path 来源）', async () => {
    const detailFixture = doc({
      document_id: '9a770001-0000-4000-8000-000000000002',
      parse_meta: { pages: 46, tables: 3, code_blocks: 8 },
      catalog_path: '家庭知识库 / 大模型全套面试题',
    })
    client.defaults.adapter = okAdapter(asEnvelope(detailFixture))
    await expect(fetchDocument(detailFixture.document_id)).resolves.toEqual(detailFixture)
    expect(captured[0]?.method).toBe('get')
    expect(captured[0]?.url).toBe('/api/v1/kb/documents/9a770001-0000-4000-8000-000000000002')
  })

  it('removeDocument：DELETE /api/v1/kb/documents/{id}（§4.5）→ {"deleted":true}', async () => {
    client.defaults.adapter = okAdapter(asEnvelope({ deleted: true }))
    await expect(removeDocument('9a770001-0000-4000-8000-000000000002')).resolves.toEqual({ deleted: true })
    expect(captured[0]?.method).toBe('delete')
    expect(captured[0]?.url).toBe('/api/v1/kb/documents/9a770001-0000-4000-8000-000000000002')
  })
})

describe('api/kb.ts — 溯源下钻（§4.6）', () => {
  it('fetchKbChunk：GET /api/v1/sources/{source_id}/kb-chunk → 六必选键 roundtrip', async () => {
    const chunkFixture = chunk({})
    client.defaults.adapter = okAdapter(asEnvelope(chunkFixture))
    await expect(fetchKbChunk('f00d0001-0000-4000-8000-000000000001')).resolves.toEqual(chunkFixture)
    expect(captured[0]?.method).toBe('get')
    expect(captured[0]?.url).toBe('/api/v1/sources/f00d0001-0000-4000-8000-000000000001/kb-chunk')
  })

  it('可选键缺省形态（api.md §4.6 v1.31——本地/URL 来源形态）：client 层零伪造、不注默认键', async () => {
    // 行为化口径（CR-6）：fixture 不含可选键——断言的是 client 层不注入默认键（若实现改为补
    // node_token: null 之类默认值，此处即红）；「携带时不被剥离」由下一用例锁定。
    client.defaults.adapter = okAdapter(asEnvelope(chunk({})))
    const data = await fetchKbChunk('f00d0001-0000-4000-8000-000000000001')
    expect('node_token' in data).toBe(false)
    expect('node_id' in data).toBe(false)
  })

  it('可选键携带形态（CR-6 行为化）：node_token/node_id 经 client 层原样透传、键不被剥离（飞书来源形态）', async () => {
    const feishuChunk = chunk({ node_token: 'wikcnKQ1k3pdoGgR1T2Uabckxy', node_id: 'fs:node:wikcnKQ1k3pdoGgR1T2Uabckxy' })
    client.defaults.adapter = okAdapter(asEnvelope(feishuChunk))
    const data = await fetchKbChunk('f00d0001-0000-4000-8000-000000000001')
    expect('node_token' in data).toBe(true)
    expect('node_id' in data).toBe(true)
    expect(data).toEqual(feishuChunk) // 值原样透传
  })

  it('404001（来源不存在/越权/chunk 或文档已删除单码）→ ApiError 透传（前端优雅降级）', async () => {
    client.defaults.adapter = okAdapter({ code: '404001', message: '资源不存在' })
    await expect(fetchKbChunk('f00d0001-0000-4000-8000-000000000001')).rejects.toMatchObject({
      code: '404001',
      name: 'ApiError',
    })
  })
})

describe('api/kb.ts — 飞书导入任务面（§4.7~4.9）', () => {
  it('triggerImport：POST /api/v1/kb/import/jobs 携带 {connector:"feishu"}（§4.7 REQ-067）', async () => {
    const triggerFixture = { job_id: '3d810001-0000-4000-8000-000000000001', status: 'pending' as const }
    client.defaults.adapter = okAdapter(asEnvelope(triggerFixture))
    await expect(triggerImport()).resolves.toEqual(triggerFixture)
    expect(captured[0]?.method).toBe('post')
    expect(captured[0]?.url).toBe('/api/v1/kb/import/jobs')
    const raw = captured[0]?.data
    const body: unknown = typeof raw === 'string' ? JSON.parse(raw) : raw
    expect(body).toEqual({ connector: 'feishu' })
  })

  it('fetchImportJobs：GET /api/v1/kb/import/jobs（§4.8）→ 列表条目 extra 键（failures）原样透传不剥离', async () => {
    // §4.8 契约面「条目不含 failures——清单随 §4.9 详情」；CR-6 行为化：fixture 注入 failures 锁
    // client 层透传语义（响应多余键不被 api/kb.ts 剥离——旧断言 in-false 是 fixture 自身形状、恒假不约束行为）。
    const item = {
      ...job({ job_id: '3d810001-0000-4000-8000-000000000001' }),
      failures: [{ title: '调研 mindnote', category: 'unsupported_type', message: 'mindnote 无读取 API，已跳过', document_id: null }],
    }
    const listFixture = { total: 1, items: [item] }
    client.defaults.adapter = okAdapter(asEnvelope(listFixture))
    const data = await fetchImportJobs()
    expect(data).toEqual(listFixture)
    expect('failures' in data.items[0]!).toBe(true)
    expect(captured[0]?.method).toBe('get')
    expect(captured[0]?.url).toBe('/api/v1/kb/import/jobs')
  })

  it('fetchImportJobDetail：GET /api/v1/kb/import/jobs/{job_id}（§4.9）→ 详情含 failures 清单', async () => {
    const detailFixture: KbImportJobDetail = {
      ...job({ job_id: '3d810001-0000-4000-8000-000000000001' }),
      failures: [
        { title: '调研 mindnote', category: 'unsupported_type', message: 'mindnote 无读取 API，已跳过', document_id: null },
      ],
    }
    client.defaults.adapter = okAdapter(asEnvelope(detailFixture))
    await expect(fetchImportJobDetail('3d810001-0000-4000-8000-000000000001')).resolves.toEqual(detailFixture)
    expect(captured[0]?.method).toBe('get')
    expect(captured[0]?.url).toBe('/api/v1/kb/import/jobs/3d810001-0000-4000-8000-000000000001')
  })

  it('409007（进行中重复触发）→ ApiError 透传服务端 message（前端 toast 直达）', async () => {
    client.defaults.adapter = okAdapter({ code: '409007', message: '已有进行中的知识库导入任务' })
    await expect(triggerImport()).rejects.toBeInstanceOf(ApiError)
    await expect(triggerImport()).rejects.toMatchObject({ code: '409007', message: '已有进行中的知识库导入任务' })
  })
})
