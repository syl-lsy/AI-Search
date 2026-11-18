import { describe, expect, it } from 'vitest'

import { threadListFixture } from '@/mocks/threads'
import {
  POLITICS_REFUSAL_MESSAGE,
  TURN_ID_DEEP,
  TURN_ID_DIRECT,
  TURN_ID_FALLBACK_DIGEST,
  TURN_ID_FALLBACK_PARTIAL,
  TURN_ID_FAST,
  TURN_ID_POLITICS,
  turnDetailDeepCompleted,
  turnDetailDirectCompleted,
  turnDetailDirectWithFollowups,
  turnDetailFallbackDigestDegraded,
  turnDetailFallbackPartialDegraded,
  turnDetailFastCompleted,
  turnDetailFastHelpful,
  turnDetailFastWithFollowups,
  turnDetailPoliticsRefused,
  turnDetailQueued,
} from '@/mocks/turnDetails'
import {
  deepSequence,
  directFollowupsEvent,
  directSequence,
  fallbackDigestSequence,
  fallbackPartialSequence,
  fastSequence,
  politicsRefusalSequence,
} from '@/mocks/sseSequences'
import { modelsFixture } from '@/mocks/models'
import { suggestionsFixture } from '@/mocks/suggestions'
import type { SseWireEvent } from '@/types/api'

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

describe('direct 轮 fixture（§9 v1.15 三用例：免检索直答，api.md §7.4 v1.14 direct 形态）', () => {
  it('① direct 序列：rewrite/write 两 stage、无 sources/images 事件、done(completed, source_count=0) 收尾', () => {
    expect(directSequence.length).toBeGreaterThan(0)
    // 序列基本不变量与其他序列同构：seq 单调、事件名合法、turn_id 一致
    const turnId = directSequence[0]?.data.turn_id
    let prevSeq = 0
    for (const wire of directSequence) {
      expect(SSE_EVENT_NAMES.has(wire.event)).toBe(true)
      expect(wire.data.seq).toBe(wire.id)
      expect(wire.data.seq).toBeGreaterThan(prevSeq)
      expect(wire.data.turn_id).toBe(turnId)
      prevSeq = wire.data.seq
    }
    expect(turnId).toBe(TURN_ID_DIRECT)
    // §7.4 direct：无 stage(search)/sources/images/thinking 事件
    const stageKeys = directSequence
      .filter((w): w is Extract<SseWireEvent, { event: 'stage' }> => w.event === 'stage')
      .map((w) => w.data.stage_key)
    expect(stageKeys).toEqual(['rewrite', 'rewrite', 'write', 'write'])
    expect(directSequence.some((w) => w.event === 'sources')).toBe(false)
    expect(directSequence.some((w) => w.event === 'images')).toBe(false)
    // §7.4 v1.14 留痕键：stage(rewrite,done) 携带 extra.strategy（前端不消费，镜像已同步）
    const rewriteDone = directSequence.find(
      (w): w is Extract<SseWireEvent, { event: 'stage' }> =>
        w.event === 'stage' && w.data.stage_key === 'rewrite' && w.data.status === 'done',
    )
    expect(rewriteDone?.data.extra).toEqual({ strategy: 'direct' })
    // done 收尾：completed、degraded=false、source_count=0（api.md §7.2 v1.14 对账口径）
    const done = directSequence.at(-1)
    if (done?.event !== 'done') throw new Error('direct 序列应以 done 收尾')
    expect(done.data.finish).toBe('completed')
    expect(done.data.degraded).toBe(false)
    expect(done.data.source_count).toBe(0)
    // token 拼接与 2.5 fixture answer_md 一致（fixture 互洽）
    const joined = directSequence
      .filter((w): w is Extract<SseWireEvent, { event: 'token' }> => w.event === 'token')
      .map((w) => w.data.delta)
      .join('')
    expect(joined).toBe(turnDetailDirectCompleted.answer_md)
  })

  it('② direct 回看 fixture（2.5）：sources/images 恒空、stages 仅两节点、答案无 [n] 悬挂引用', () => {
    expect(turnDetailDirectCompleted.status).toBe('completed')
    expect(turnDetailDirectCompleted.mode).toBe('fast')
    expect(turnDetailDirectCompleted.sources).toEqual([])
    expect(turnDetailDirectCompleted.images).toEqual([])
    expect(turnDetailDirectCompleted.stages.map((s) => [s.stage_key, s.status])).toEqual([
      ['rewrite', 'done'],
      ['write', 'done'],
    ])
    expect(turnDetailDirectCompleted.stages[0]?.extra).toEqual({ strategy: 'direct' }) // §2.5 v1.14 留痕重建
    expect(turnDetailDirectCompleted.degraded).toBe(false)
    expect(turnDetailDirectCompleted.export_available).toBe(false) // fast 不计入 report
    expect(turnDetailDirectCompleted.followups).toBeNull() // 默认关闭态（③ 另有非空孪生）
    expect(turnDetailDirectCompleted.answer_md).not.toMatch(/\[\d+\]/) // 零来源轮无引用标注
  })

  it('③ followups 追加于 direct 轮：事件样例与 2.5 孪生（api.md §7.2 v1.14 注轮型不排除 direct）', () => {
    if (directFollowupsEvent.event !== 'followups') {
      throw new Error('③ 样例应为 followups 事件')
    }
    const followups = directFollowupsEvent.data
    expect(followups.turn_id).toBe(TURN_ID_DIRECT)
    expect(followups.seq).toBeGreaterThan(directSequence.at(-1)?.data.seq ?? 0) // 必然晚于 done
    expect(followups.items.length).toBeLessThanOrEqual(5)
    for (const item of followups.items) {
      expect(item.length).toBeGreaterThan(0)
      expect(item.length).toBeLessThanOrEqual(200)
    }
    // 2.5 孪生（REST 为事实源）：followups 内容与 SSE 事件一致
    expect(turnDetailDirectWithFollowups.followups).toEqual(followups.items)
    expect(turnDetailDirectWithFollowups.sources).toEqual([])
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

  it('达标 fast 轮 + 已反馈：feedback_helpful=true（§9 反馈判定点 v1.17——A41 回看还原样例）；基线 fixture 字段缺席（M2 前端点）', () => {
    expect(turnDetailFastHelpful.feedback_helpful).toBe(true)
    expect(turnDetailFastHelpful.turn_id).toBe(TURN_ID_FAST)
    expect('feedback_helpful' in turnDetailFastCompleted).toBe(false) // 字段缺席 = 可选字段语义
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

describe('B14 兜底收束轮 fixture（api.md §7.4 v1.25 兜底收束形态 / PRD A51，§9 v1.29）', () => {
  /** 序列基本不变量（与其他序列同构）：seq 单调、事件名合法、turn_id 一致 */
  function assertSequenceInvariants(sequence: SseWireEvent[], expectedTurnId: string): void {
    expect(sequence.length).toBeGreaterThan(0)
    let prevSeq = 0
    for (const wire of sequence) {
      expect(SSE_EVENT_NAMES.has(wire.event)).toBe(true)
      expect(wire.data.seq).toBe(wire.id)
      expect(wire.data.seq).toBeGreaterThan(prevSeq)
      expect(wire.data.turn_id).toBe(expectedTurnId)
      prevSeq = wire.data.seq
    }
  }

  it('digest 序列：主干形态锁死——零 error 事件、token 单事件整块、write done extra.fallback、done(degraded, reason=null)', () => {
    assertSequenceInvariants(fallbackDigestSequence, TURN_ID_FALLBACK_DIGEST)
    // 零 error 事件（「超时不能报错收束，必须有兜底产出」2026-11-18 裁决①）
    expect(fallbackDigestSequence.some((w) => w.event === 'error')).toBe(false)
    // token 恰一个事件且整块 = 2.5 fixture answer_md（digest 机械装配全文，fixture 互洽）
    const tokens = fallbackDigestSequence.filter(
      (w): w is Extract<SseWireEvent, { event: 'token' }> => w.event === 'token',
    )
    expect(tokens).toHaveLength(1)
    expect(tokens[0]!.data.delta).toBe(turnDetailFallbackDigestDegraded.answer_md)
    // stage(write,done) 留痕 extra={"fallback":"synthesis_digest"}
    const writeDone = fallbackDigestSequence.find(
      (w): w is Extract<SseWireEvent, { event: 'stage' }> =>
        w.event === 'stage' && w.data.stage_key === 'write' && w.data.status === 'done',
    )
    expect(writeDone?.data.extra).toEqual({ fallback: 'synthesis_digest' })
    // done 收尾：degraded、degraded_reason=null（枚举零扩容——兜底轮不属三枚举任一语义）
    const done = fallbackDigestSequence.at(-1)
    if (done?.event !== 'done') throw new Error('digest 序列应以 done 收尾')
    expect(done.data.finish).toBe('degraded')
    expect(done.data.degraded).toBe(true)
    expect(done.data.degraded_reason).toBeNull()
    expect(done.data.source_count).toBe(6)
    // 主干次序：sources 批次先行、digest token 在 write running 之后（api.md §7.4 v1.25 序）
    const eventOrder = fallbackDigestSequence.map((w) => w.event)
    expect(eventOrder.indexOf('sources')).toBeGreaterThan(-1)
    expect(eventOrder.indexOf('token')).toBeGreaterThan(eventOrder.indexOf('sources'))
  })

  it('digest 全文形态：说明行 + 前 5 条 [n] 来源行与 sources 卡编号对齐 + 「其余见来源面板」尾注', () => {
    const md = turnDetailFallbackDigestDegraded.answer_md
    expect(md).toContain('综合回答未能完成')
    // [n] 1..5 与已发 sources 卡 ref_no 1..5 一一对齐（A51 GWT①「[n] 与来源卡编号对齐」）
    for (const refNo of [1, 2, 3, 4, 5]) {
      expect(md).toContain(`[${refNo}]`)
      const source = turnDetailFallbackDigestDegraded.sources.find((s) => s.ref_no === refNo)
      expect(source).toBeDefined()
      expect(md).toContain(source!.title)
    }
    // fused>5 条 → 「其余见来源面板」尾注（sources 共 6 条，digest 只列前 5）
    expect(turnDetailFallbackDigestDegraded.sources).toHaveLength(6)
    expect(md).toContain('其余 1 条来源见下方来源面板')
    expect(md).not.toContain('[6]') // N 上限 5：第 6 条不进 digest
  })

  it('partial 序列：token 流中断形态锁死——拼接=2.5 answer_md、句末标点截断、extra.fallback、done(degraded, reason=null)', () => {
    assertSequenceInvariants(fallbackPartialSequence, TURN_ID_FALLBACK_PARTIAL)
    expect(fallbackPartialSequence.some((w) => w.event === 'error')).toBe(false)
    // 多 token 部分流（与 digest 单块形态对偶）且拼接与 2.5 fixture 一致
    const tokens = fallbackPartialSequence.filter(
      (w): w is Extract<SseWireEvent, { event: 'token' }> => w.event === 'token',
    )
    expect(tokens.length).toBeGreaterThan(1)
    const joined = tokens.map((w) => w.data.delta).join('')
    expect(joined).toBe(turnDetailFallbackPartialDegraded.answer_md)
    // 截断到最近句末标点（。！？!?.）：句内残句丢弃、无伪造补全
    expect(joined).toMatch(/[。！？!?.]$/)
    const writeDone = fallbackPartialSequence.find(
      (w): w is Extract<SseWireEvent, { event: 'stage' }> =>
        w.event === 'stage' && w.data.stage_key === 'write' && w.data.status === 'done',
    )
    expect(writeDone?.data.extra).toEqual({ fallback: 'synthesis_partial' })
    const done = fallbackPartialSequence.at(-1)
    if (done?.event !== 'done') throw new Error('partial 序列应以 done 收尾')
    expect(done.data.finish).toBe('degraded')
    expect(done.data.degraded).toBe(true)
    expect(done.data.degraded_reason).toBeNull()
    expect(done.data.source_count).toBe(3)
  })

  it('politics 拒答序列：三帧形态锁死——stage failed extra(safety/layer)、error(403002)、无 done', () => {
    assertSequenceInvariants(politicsRefusalSequence, TURN_ID_POLITICS)
    expect(politicsRefusalSequence).toHaveLength(3)
    // stage(rewrite,failed) 留痕：safety=politics（v1.25 值域扩容）+ layer=pre_route（v1.23 B13 先例键）
    const failed = politicsRefusalSequence.find(
      (w): w is Extract<SseWireEvent, { event: 'stage' }> => w.event === 'stage' && w.data.status === 'failed',
    )
    expect(failed?.data.stage_key).toBe('rewrite')
    expect(failed?.data.extra).toEqual({ safety: 'politics', layer: 'pre_route' })
    // error(403002) + message=中性引导话术（SSE 与 2.5 双源同话术，fixture 互洽）
    const error = politicsRefusalSequence.at(-1)
    if (error?.event !== 'error') throw new Error('politics 序列应以 error 收尾（无 done）')
    expect(error.data.code).toBe('403002')
    expect(error.data.message).toBe(POLITICS_REFUSAL_MESSAGE)
    expect(error.data.stage_key).toBe('rewrite')
    expect(politicsRefusalSequence.some((w) => w.event === 'done')).toBe(false)
    expect(politicsRefusalSequence.some((w) => w.event === 'sources')).toBe(false) // 全程零搜索零综合
  })

  it('三个 2.5 终态孪生：digest/partial 降级态权威投影、politics failed 拒答投影', () => {
    // digest：degraded + reason=null + 零 error + 兜底留痕重建
    expect(turnDetailFallbackDigestDegraded.status).toBe('degraded')
    expect(turnDetailFallbackDigestDegraded.degraded).toBe(true)
    expect(turnDetailFallbackDigestDegraded.degraded_reason).toBeNull()
    expect(turnDetailFallbackDigestDegraded.error).toBeNull()
    expect(turnDetailFallbackDigestDegraded.stages.find((s) => s.stage_key === 'write')?.extra).toEqual({ fallback: 'synthesis_digest' })
    expect(turnDetailFallbackDigestDegraded.export_available).toBe(false) // fast 不计入 report
    // partial：同款降级投影 + answer_md 非空落库（A51 GWT②「刷新回看 partial 全文持久可见」读通道）
    expect(turnDetailFallbackPartialDegraded.status).toBe('degraded')
    expect(turnDetailFallbackPartialDegraded.degraded).toBe(true)
    expect(turnDetailFallbackPartialDegraded.degraded_reason).toBeNull()
    expect(turnDetailFallbackPartialDegraded.error).toBeNull()
    expect(turnDetailFallbackPartialDegraded.answer_md.length).toBeGreaterThan(0)
    expect(turnDetailFallbackPartialDegraded.stages.find((s) => s.stage_key === 'write')?.extra).toEqual({ fallback: 'synthesis_partial' })
    // politics：failed + error 载荷复用承载（data-model.md §1.3）+ 零内容零来源
    expect(turnDetailPoliticsRefused.status).toBe('failed')
    expect(turnDetailPoliticsRefused.answer_md).toBe('')
    expect(turnDetailPoliticsRefused.sources).toEqual([])
    expect(turnDetailPoliticsRefused.error).toEqual({ code: '403002', message: POLITICS_REFUSAL_MESSAGE, stage_key: 'rewrite' })
    expect(turnDetailPoliticsRefused.stages[0]?.extra).toEqual({ safety: 'politics', layer: 'pre_route' })
    expect(turnDetailPoliticsRefused.degraded).toBe(false)
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
