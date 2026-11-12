/**
 * src/composables/mediaQuery.ts — 响应式断点（REQ-044：<720px 锚点隐藏；REQ-045：箭头保留）。
 * matchMedia 变更监听（addEventListener 优先，旧实现回落 addListener）；无 window 时恒 false。
 */
import { onUnmounted, readonly, shallowRef } from 'vue'
import type { Ref } from 'vue'

export function useMediaQuery(query: string): Readonly<Ref<boolean>> {
  const matches = shallowRef(false)
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
    return readonly(matches)
  }
  const mql = window.matchMedia(query)
  matches.value = mql.matches
  const update = (): void => {
    matches.value = mql.matches
  }
  const legacy = mql as MediaQueryList & { addListener?: (cb: () => void) => void; removeListener?: (cb: () => void) => void }
  if (typeof mql.addEventListener === 'function') {
    mql.addEventListener('change', update)
    onUnmounted(() => mql.removeEventListener('change', update))
  } else if (typeof legacy.addListener === 'function') {
    legacy.addListener(update)
    onUnmounted(() => legacy.removeListener?.(update))
  }
  return readonly(matches)
}
