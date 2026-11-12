import { describe, expect, it } from 'vitest'

import { threadListFixture } from '@/mocks/threads'
import {
  TURN_ID_DEEP,
  TURN_ID_FAST,
  turnDetailDeepCompleted,
  turnDetailFastCompleted,
  turnDetailFastWithFollowups,
  turnDetailQueued,
} from '@/mocks/turnDetails'
import { deepSequence, fastSequence } from '@/mocks/sseSequences'
import { modelsFixture } from '@/mocks/models'
import { suggestionsFixture } from '@/mocks/suggestions'

const SSE_EVENT_NAMES = new Set([
  'stage',
  'token',
  'thinking',
  'sources',
  'images',
  'context_compact',
  'memory',
  'done',
  'followups',
  'error',
])

describe('SSE 序列 fixture（api.md §7.4 两种序列形态）', () => {
  it.each([
    ['fast', fastSequence],
    ['deep', deepSequence],
  ])('%s 序列：seq 线程内单调递增且与 id 同值、事件名合法、turn_id 一致', (_name, sequence) => {
    expect(sequence.length).toBeGreaterThan(0)
    const turnId = sequence[0]?.data.turn_id
    let prevSeq = 0
    for (const wire of sequence) {
      expect(SSE_EVENT_NAMES.has(wire.event)).toBe(true)
      expect(wire.data.seq).toBe(wire.id)
      expect(wire.data.seq).toBeGreaterThan(prevSeq)
      expect(wire.data.turn_id).toBe(turnId)
      prevSeq = wire.data.seq
    }
  })

  it('fast 序列主干符合 §7.4：rewrite 起、done 收尾、followups 尾随（v1.9 可选）', () => {
    const first = fastSequence[0]
    if (first?.event !== 'stage') {
      throw new Error('fast 序列应以 stage 事件开头')
    }
    expect(first.data.stage_key).toBe('rewrite') // 生命周期 9：fast 首个 stage = rewrite
    expect(fastSequence.at(-2)?.event).toBe('done')

    const last = fastSequence.at(-1)
    if (last?.event !== 'followups') {
      throw new Error('fast 序列应以 followups 尾随（§9 v1.9 样例）')
    }
    expect(last.data.items.length).toBeLessThanOrEqual(5)
  })

  it('deep 序列主干符合 §7.4：plan 起、context_compact 仅 deep、done 收尾', () => {
    const first = deepSequence[0]
    if (first?.event !== 'stage') {
      throw new Error('deep 序列应以 stage 事件开头')
    }
    expect(first.data.stage_key).toBe('plan')
    expect(deepSequence.at(-1)?.event).toBe('done')
    const compact = deepSequence.find((w) => w.event === 'context_compact')
    expect(compact).toBeDefined()
    const thinking = deepSequence.filter((w) => w.event === 'thinking')
    expect(thinking.length).toBeGreaterThan(0) // v1.3：token 流期间穿插 thinking
  })

  it('images 条目键可省口径（api.md v1.12 / batch7-handoff §1）：fast 仅 url、deep 全键', () => {
    const fastImages = fastSequence.find((w) => w.event === 'images')
    expect(Object.keys(fastImages?.data.images[0] ?? {})).toEqual(['url'])

    const deepImages = deepSequence.find((w) => w.event === 'images')
    expect(Object.keys(deepImages?.data.images[0] ?? {}).sort()).toEqual([
      'height',
      'source_url',
      'title',
      'url',
      'width',
    ])
  })
})

describe('轮次终态 fixture（api.md §2.5 镜像形态）', () => {
  it('快速完成态：followups 恒 null（实验默认关闭）、cancel_reason 恒 null', () => {
    expect(turnDetailFastCompleted.followups).toBeNull()
    expect(turnDetailFastCompleted.cancel_reason).toBeNull()
    expect(turnDetailFastCompleted.status).toBe('completed')
    expect(turnDetailFastCompleted.export_available).toBe(false) // fast 不计入 report
    expect('memory_hits' in turnDetailFastCompleted).toBe(false) // M6 起才返回
  })

  it('达标 fast 轮：followups 非空（§9 v1.9 fixture③ 回看渲染样例）', () => {
    expect(turnDetailFastWithFollowups.followups?.length).toBe(3)
    expect(turnDetailFastWithFollowups.turn_id).toBe(TURN_ID_FAST)
  })

  it('深度完成态：export_available=true（mode=deep 且 answer 非空且 completed）', () => {
    expect(turnDetailDeepCompleted.export_available).toBe(true)
    expect(turnDetailDeepCompleted.turn_id).toBe(TURN_ID_DEEP)
    const kbSource = turnDetailDeepCompleted.sources.find((s) => s.source_type === 'kb')
    expect(kbSource?.url).toBeNull() // kb 来源 url 为 null
  })

  it('排队轮：status=queued、仅 query 与状态已持久化（§9 v1.5）', () => {
    expect(turnDetailQueued.status).toBe('queued')
    expect(turnDetailQueued.stages).toEqual([])
    expect(turnDetailQueued.finished_at).toBeNull()
  })
})

describe('线程列表 / 模型清单 / 建议池 fixture', () => {
  it('线程列表：total 与 items 一致', () => {
    expect(threadListFixture.total).toBe(threadListFixture.items.length)
    for (const item of threadListFixture.items) {
      expect(item.mode_summary).toMatch(/^(fast|deep|mixed)$/)
    }
  })

  it('模型清单：三键且 default=qwen（§9 v1.7）', () => {
    expect(modelsFixture.options.map((o) => o.key)).toEqual(['qwen', 'glm', 'deepseek'])
    expect(modelsFixture.default).toBe('qwen')
  })

  it('建议池：0~8 条、单条非空且 ≤200 字符（api.md §3.2）', () => {
    expect(suggestionsFixture.items.length).toBeLessThanOrEqual(8)
    for (const item of suggestionsFixture.items) {
      expect(item.length).toBeGreaterThan(0)
      expect(item.length).toBeLessThanOrEqual(200)
    }
  })
})
