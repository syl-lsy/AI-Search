/**
 * src/components/markdown/renderThrottle.ts — markdown 渲染限频批（streaming-buffer 规则 14/17）。
 *
 * 规则 14（markdown 增量渲染限频 60-80ms 一批）：源变更只置最新值，每 intervalMs 至多透传一次；
 *   leading（窗口外首变立即透传——流式首帧/静态回看零延迟）+ trailing（窗口内合批，尾帧取最新值
 *   ——终值不丢）。规则 17 面（避免逐字符触发 vdom diff 与 markdown 重解析）：AnswerRenderer /
 *   ThinkingPanel 以本限频源驱动 markdown 重解析，把「每帧全量重解析」收敛为「≤70ms 一批」。
 *
 * 取舍（M1 范围）：未引入进阶层「冻结前缀 + 只重解析尾部」分块管线——fast 答案 ≤3000 字硬上限、
 * thinking 面板 max-height 280px，全量重解析限频后单次成本有界；deep 长报告（M4 评测线）如实测
 * 卡顿再升级增量管线，届时本模块保持消费面不变。
 */
import { onScopeDispose, shallowRef, watch } from 'vue'
import type { Ref } from 'vue'

/** streaming-buffer 参数表「markdown 重解析间隔」标称值 70ms（规则 14 区间 60-80ms） */
export const MARKDOWN_RENDER_INTERVAL_MS = 70

/**
 * 限频透传源：`source` getter 变更时，窗口外立即透传（leading）、窗口内合批至定时器尾帧透传
 * 最新值（trailing）。输出为 shallowRef——仅在真透传时换引用，下游 computed 不因输入逐帧变化
 * 而重算（规则 17「累加器只在真变更时换引用」的消费面形态）。
 */
export function useThrottledSource<T>(source: () => T, intervalMs: number = MARKDOWN_RENDER_INTERVAL_MS): Readonly<Ref<T>> {
  const output = shallowRef(source()) as Ref<T>
  let latest = source()
  let lastFlushAt = 0
  let timer: ReturnType<typeof setTimeout> | null = null

  function flush(): void {
    timer = null
    lastFlushAt = Date.now()
    if (output.value !== latest) output.value = latest
  }

  const stop = watch(source, (value) => {
    latest = value
    if (timer !== null) return // 窗口内已有待尾帧：合批，尾帧统一取最新值
    const elapsed = Date.now() - lastFlushAt
    if (elapsed >= intervalMs) {
      flush()
      return
    }
    timer = setTimeout(flush, intervalMs - elapsed)
  })

  // 组件卸载清定时器（watch 随组件作用域自清；独立作用域外调用经 failSilently 免告警）
  onScopeDispose(
    () => {
      stop()
      if (timer !== null) clearTimeout(timer)
    },
    true,
  )
  return output
}
