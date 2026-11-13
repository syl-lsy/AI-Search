/**
 * test/unit/render-throttle.test.ts — markdown 渲染限频批（streaming-buffer 规则 14/17）。
 *
 * 锁死口径：
 *   - 限频间隔 70ms（技能参数表值，规则 14 区间 60-80ms）；
 *   - leading：窗口外首变立即透传（首帧/静态回看零延迟）；
 *   - trailing：窗口内后续变化合批，定时器到期一次透传最新值（尾值不丢）；
 *   - 供 AnswerRenderer / ThinkingPanel 消费（每帧全量重解析 → ≤70ms 一批）。
 */
import { describe, expect, it, vi } from 'vitest'
import { nextTick, ref } from 'vue'

import {
  MARKDOWN_RENDER_INTERVAL_MS,
  useThrottledSource,
} from '@/components/markdown/renderThrottle'

describe('useThrottledSource（streaming-buffer 规则 14：markdown 限频批）', () => {
  it('常量锁死：70ms（参数表值，规则 14 的 60-80ms 区间）', () => {
    expect(MARKDOWN_RENDER_INTERVAL_MS).toBe(70)
  })

  it('窗口外首变立即透传（leading：流式首帧/静态更新零延迟）', async () => {
    vi.useFakeTimers()
    const src = ref('a')
    const throttled = useThrottledSource(() => src.value)
    expect(throttled.value).toBe('a')
    src.value = 'b'
    await nextTick()
    expect(throttled.value).toBe('b') // 首变（距上次 flush 无限窗）立即透传
    vi.useRealTimers()
  })

  it('窗口内后续变化合批：不透传，定时器到期一次透传最新值（trailing 尾值不丢）', async () => {
    vi.useFakeTimers()
    const src = ref('a')
    const throttled = useThrottledSource(() => src.value)
    src.value = 'b'
    await nextTick()
    expect(throttled.value).toBe('b') // leading 立即
    src.value = 'c'
    await nextTick()
    expect(throttled.value).toBe('b') // 窗口内：暂不透传
    src.value = 'd' // 合批期间源继续变
    await nextTick()
    await vi.advanceTimersByTimeAsync(MARKDOWN_RENDER_INTERVAL_MS + 20)
    expect(throttled.value).toBe('d') // 尾帧取最新值
    vi.useRealTimers()
  })

  it('合批窗过后再次变化 → 重新 leading 立即透传', async () => {
    vi.useFakeTimers()
    const src = ref('a')
    const throttled = useThrottledSource(() => src.value)
    src.value = 'b'
    await nextTick()
    expect(throttled.value).toBe('b')
    src.value = 'c'
    await nextTick()
    await vi.advanceTimersByTimeAsync(MARKDOWN_RENDER_INTERVAL_MS + 20)
    expect(throttled.value).toBe('c') // trailing 落地
    src.value = 'e' // 窗已过（再次推进时间）→ leading
    await vi.advanceTimersByTimeAsync(MARKDOWN_RENDER_INTERVAL_MS + 20)
    await nextTick()
    expect(throttled.value).toBe('e')
    vi.useRealTimers()
  })
})
