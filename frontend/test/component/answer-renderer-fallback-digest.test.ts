/**
 * test/component/answer-renderer-fallback-digest.test.ts — B14 digest 兜底轮「单块大 delta
 * 渲染完整性」（architect 提示点；api.md §7.4 v1.25 兜底收束形态 digest 单事件整块 token +
 * frontend-api.md §3 v1.29 钉注②「既有 answer_md += delta 拼接路径零改动」；PRD A51 GWT①）。
 *
 * 合帧幂等断言链（digest 全文以单事件整块 token 到达，经两级合并后必须一次完整落地）：
 *   1. 传输级合帧：turnStream ChannelBuffer 对一次性大块 delta 按预算追赶分帧（budget =
 *      max(2, pending×0.12)）逐帧 flush——尾帧取走全部剩余积压（尾值不丢，streaming-buffer
 *      规则 9/10），分帧拼接与整块逐字节一致、终态缓冲零残留；
 *   2. 渲染级限频：AnswerRenderer 经 useThrottledSource 70ms 限频批（leading + trailing 合批
 *      ——规则 14/17），分帧 prop 增长与整块 prop 替换两形态最终渲染一致且完整；
 *   3. 终态完整性：渲染 DOM 含 digest 全文语义单元——说明行、[n] 角标 1..5（sup.cite 与
 *      sources 卡联动形态）、五条来源 title、「其余见来源面板」尾注（A51 GWT①）。
 * 联装形态：真 ChannelBuffer（帧调度以 stub 驱动）+ 真 AnswerRenderer（经 setProps 驱动——
 * store answer_md 逐帧增长 → :markdown prop 的组件层真实投递形态）。
 */
import { mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import AnswerRenderer from '@/components/markdown/AnswerRenderer.vue'
import { turnDetailFallbackDigestDegraded } from '@/mocks/turnDetails'
import { ChannelBuffer } from '@/sse/turnStream'

const DIGEST_MD = turnDetailFallbackDigestDegraded.answer_md

/** digest 全文语义单元（DOM 完整性断言面——缺一即「大 delta 被截断」） */
function expectDigestComplete(wrapper: VueWrapper): void {
  const text = wrapper.find('[data-testid="answer-renderer"]').text()
  expect(text).toContain('综合回答未能完成') // 说明行
  for (const source of turnDetailFallbackDigestDegraded.sources.slice(0, 5)) {
    expect(text).toContain(source.title) // 前 5 条来源 title
  }
  expect(text).toContain('其余 1 条来源见下方来源面板') // 尾注
  // [n] 角标 1..5 与 sources 卡编号对齐（sup.cite——A51 GWT①「可点击联动」渲染形态）
  const cites = wrapper.findAll('sup.cite').map((s) => Number(s.attributes('data-ref-no')))
  expect(cites).toEqual([1, 2, 3, 4, 5])
}

beforeEach(() => {
  vi.useFakeTimers()
  // rAF → setTimeout(16)（streaming-buffer 规则 8 的测试等价物，fake timers 可推进）
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => setTimeout(() => cb(0), 16))
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('digest 单块大 delta 渲染完整性（ChannelBuffer + renderThrottle 两级合帧）', () => {
  it('传输级合帧：单块整 delta 入 ChannelBuffer → 预算追赶分帧 flush → 终态缓冲零残留、拼接无重无漏', () => {
    const chunks: string[] = []
    const buffer = new ChannelBuffer((merged) => chunks.push(merged), (fn) => setTimeout(fn, 16))
    buffer.push(DIGEST_MD, 8) // api.md §7.4：digest 以单事件整块 token 发射（seq 8 形态）
    // 追赶消费直至积压清空（budget = max(2, pending×0.12)——数百字符需数帧）
    for (let i = 0; i < 60 && buffer.pendingChars > 0; i++) {
      vi.advanceTimersByTime(32)
    }
    expect(buffer.pendingChars).toBe(0) // 尾帧取走全部剩余（尾值不丢）
    expect(chunks.join('')).toBe(DIGEST_MD) // 分帧拼接幂等：无重无漏、与整块逐字节一致
    expect(chunks.length).toBeGreaterThan(1) // 大块确实经历多帧追赶（分帧路径非退化单帧）
    // 终态同步冲刷路径（L4 done 先冲刷——flushAll）与追赶路径终值一致
    const singleShot: string[] = []
    const buffer2 = new ChannelBuffer((merged) => singleShot.push(merged), (fn) => setTimeout(fn, 16))
    buffer2.push(DIGEST_MD, 8)
    buffer2.flushAll()
    expect(singleShot.join('')).toBe(chunks.join(''))
  })

  it('渲染级限频：分帧 prop 增长 → 70ms 限频批 → 终态 DOM 完整（与传输级分帧同源，尾帧透传最新全文）', async () => {
    // 真实分帧：单块 delta 经 ChannelBuffer 预算追赶产生的 chunk 序列（传输级真实输出）
    const chunks: string[] = []
    const buffer = new ChannelBuffer((merged) => chunks.push(merged), (fn) => setTimeout(fn, 16))
    buffer.push(DIGEST_MD, 8)
    for (let i = 0; i < 60 && buffer.pendingChars > 0; i++) {
      vi.advanceTimersByTime(32)
    }
    expect(buffer.pendingChars).toBe(0)

    // 组件层按分帧序列驱动 :markdown prop（store answer_md 逐帧增长的真实投递形态）
    const w = mount(AnswerRenderer, { props: { markdown: '', headingPrefix: 'fb1' } })
    expect(w.find('[data-testid="answer-renderer"]').text()).not.toContain('综合回答未能完成')
    let acc = ''
    for (const chunk of chunks) {
      acc += chunk
      await w.setProps({ markdown: acc })
    }
    await vi.advanceTimersByTimeAsync(200) // renderThrottle 70ms 尾帧透传（trailing 尾值不丢）
    expectDigestComplete(w) // 终态完整性：说明行、五条 title、尾注、[n] 角标 1..5
  })

  it('一次性大 delta 直达（跳过分帧的极端形态）：整块 prop 替换 → DOM 完整、同值重复投递渲染幂等不增不减', async () => {
    const w = mount(AnswerRenderer, { props: { markdown: '', headingPrefix: 'fb2' } })
    await w.setProps({ markdown: DIGEST_MD }) // 单帧整块直达（ChannelBuffer 单帧 flushAll 等价形态）
    await vi.advanceTimersByTimeAsync(200) // 限频窗过后透传
    expectDigestComplete(w)
    // 幂等：同值重复投递（store 同值重复投影 / props 引用替换）渲染结果不增不减
    const firstHtml = w.find('[data-testid="answer-renderer"]').html()
    await w.setProps({ markdown: DIGEST_MD })
    await vi.advanceTimersByTimeAsync(200)
    expect(w.find('[data-testid="answer-renderer"]').html()).toBe(firstHtml)
    expect(w.findAll('sup.cite')).toHaveLength(5)
  })
})
