/**
 * src/composables/theme.ts — 主题生效机制（REQ-054/A38，frontend-api.md §2 v1.18）。
 *
 * 职责（组件单一职责：store 只管数据面，DOM 副作用收敛在此）：
 *   - watch(prefs.resolvedTheme) → document.documentElement.dataset.theme 写具体值
 *     （'light' | 'dark'；CSS 变量切换即时全页生效、无刷新——tokens.css 浅色组以
 *     :root[data-theme='light'] 作用域，暗色保持 :root 缺省）；
 *   - system 档实时跟随：matchMedia('(prefers-color-scheme: dark)') change 监听 →
 *     prefs.syncSystemPrefersDark（数据面单一归属 store）→ resolvedTheme 重算；
 *   - 清理：onUnmounted 移除 change 监听（宿主=App 全局壳/ThemeSwitcher，随挂载面卸载）。
 */
import { onUnmounted, watch } from 'vue'

import { usePrefsStore } from '@/stores/prefs'

export function useThemeEffect(): void {
  const prefs = usePrefsStore()

  const apply = (resolved: 'light' | 'dark'): void => {
    document.documentElement.dataset.theme = resolved
  }

  // 无 matchMedia（jsdom 缺省/SSR）：跳过 change 监听，dataset 仍随用户显式档生效
  if (typeof window.matchMedia !== 'function') {
    const stop = watch(() => prefs.resolvedTheme, apply, { immediate: true })
    onUnmounted(() => stop())
    return
  }

  const mql = window.matchMedia('(prefers-color-scheme: dark)')
  const onChange = (e: MediaQueryListEvent): void => {
    prefs.syncSystemPrefersDark(e.matches)
  }
  mql.addEventListener('change', onChange)

  const stop = watch(() => prefs.resolvedTheme, apply, { immediate: true })

  onUnmounted(() => {
    mql.removeEventListener('change', onChange)
    stop()
  })
}
