/**
 * src/composables/infiniteScroll.ts — 触底自动加载（frontend-api.md §1 ThreadsView 行 v1.21 /
 * D-15：IntersectionObserver sentinel，无独立「加载更多」按钮）。
 *
 * 目标元素进入视口（rootMargin 提前量）即回调 onIntersect（调用方负责防重入——store
 * loadingMore 已兜底）；环境无 IntersectionObserver 时静默降级为 no-op（现代浏览器均支持，
 * jsdom 测试经 stub 注入）。回调经 onMounted 装配、onUnmounted 断开；target 置 null
 * （v-if 卸载 sentinel）时自动断开、重挂时自动重连。
 */
import { onUnmounted, watch, type Ref } from 'vue'

import { INFINITE_SCROLL_ROOT_MARGIN } from '@/constants/ui'

export function useInfiniteScroll(
  target: Ref<HTMLElement | null>,
  onIntersect: () => void,
  options: { enabled?: () => boolean } = {},
): void {
  let observer: IntersectionObserver | null = null

  function disconnect(): void {
    observer?.disconnect()
    observer = null
  }

  function connect(): void {
    if (typeof IntersectionObserver === 'undefined') return
    disconnect()
    observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting && (options.enabled?.() ?? true)) onIntersect()
        }
      },
      { rootMargin: INFINITE_SCROLL_ROOT_MARGIN },
    )
    const el = target.value
    if (el) observer.observe(el)
  }

  watch(target, (el) => {
    if (el) connect()
    else disconnect()
  })

  onUnmounted(disconnect)
}
