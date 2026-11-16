/**
 * src/stores/prefs.ts — 偏好 store（frontend-api.md §4 prefsStore；setup store 写法，§8.2）。
 *
 * 状态：lang / model / incognito（v1.3）/ onConflict（v1.5）/ theme（v1.18，REQ-054），
 * localStorage 同步（§2）：
 *   - ai-search.lang：zh-CN | en-US
 *   - ai-search.model：qwen | glm | deepseek（初值不本地硬编码——无用户选择为 null，
 *     提交体不携带走服务端缺省，ModelSelect 落位时以 GET /models 的 default 兜底）
 *   - ai-search.incognito：'true' | 'false'（默认 false）
 *   - ai-search.onConflict：reject | enqueue | interrupt | rollback | null（默认 null=不携带字段）
 *   - ai-search.theme：light | dark | system（默认 system；resolvedTheme 经
 *     matchMedia prefers-color-scheme 解析，dataset 写入与 change 监听归 composables/theme.ts）
 * 语义：业务开关「对下一轮生效」；theme/lang 切换即时全页生效。
 */
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { setLocale } from '@/i18n'
import type { AppLocale } from '@/i18n'
import {
  isAppLocale,
  isChatModelKey,
  isOnConflictStrategy,
  isThemePreference,
  parseStoredFlag,
} from '@/utils/guards'
import type { ChatModelKey, OnConflictStrategy } from '@/types/api'
import type { ThemePreference } from '@/utils/guards'

const STORAGE_KEYS = {
  lang: 'ai-search.lang',
  model: 'ai-search.model',
  incognito: 'ai-search.incognito',
  onConflict: 'ai-search.onConflict',
  theme: 'ai-search.theme',
} as const

/** §8.9③：localStorage 无类型边界——读值先守卫收窄；存储不可用（隐私模式等）静默降级 */
function readStorage(key: string): unknown {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function writeStorage(key: string, value: string | null): void {
  try {
    if (value === null) {
      localStorage.removeItem(key)
    } else {
      localStorage.setItem(key, value)
    }
  } catch {
    // 存储不可用时静默：内存态照常生效，仅失去持久化
  }
}

function readStoredLocale(): AppLocale {
  const stored = readStorage(STORAGE_KEYS.lang)
  return isAppLocale(stored) ? stored : 'zh-CN'
}

function readStoredModel(): ChatModelKey | null {
  const stored = readStorage(STORAGE_KEYS.model)
  return isChatModelKey(stored) ? stored : null
}

function readStoredIncognito(): boolean {
  return parseStoredFlag(readStorage(STORAGE_KEYS.incognito)) ?? false
}

function readStoredOnConflict(): OnConflictStrategy | null {
  const stored = readStorage(STORAGE_KEYS.onConflict)
  return isOnConflictStrategy(stored) ? stored : null
}

function readStoredTheme(): ThemePreference {
  const stored = readStorage(STORAGE_KEYS.theme)
  return isThemePreference(stored) ? stored : 'system'
}

/** 当前系统偏好是否深色（jsdom/无 matchMedia 环境守卫为 false——解析回落浅色） */
function readSystemPrefersDark(): boolean {
  try {
    return typeof window.matchMedia === 'function' && window.matchMedia('(prefers-color-scheme: dark)').matches
  } catch {
    return false
  }
}

export const usePrefsStore = defineStore('prefs', () => {
  const lang = ref<AppLocale>(readStoredLocale())
  const model = ref<ChatModelKey | null>(readStoredModel())
  const incognito = ref<boolean>(readStoredIncognito())
  const onConflict = ref<OnConflictStrategy | null>(readStoredOnConflict())
  /** 界面主题三选（REQ-054，v1.18）：用户档；system=跟随系统（resolvedTheme 解析） */
  const theme = ref<ThemePreference>(readStoredTheme())
  /** 系统深色偏好镜像（useThemeEffect 经 matchMedia change 回调同步——数据面单一归属 store） */
  const systemPrefersDark = ref<boolean>(readSystemPrefersDark())
  /** 解析后生效档：system → 系统偏好；显式档原样（渲染层 dataset.theme 数据源） */
  const resolvedTheme = computed<'light' | 'dark'>(() => {
    if (theme.value === 'dark') return 'dark'
    if (theme.value === 'light') return 'light'
    return systemPrefersDark.value ? 'dark' : 'light'
  })

  /** 切换界面语言：即时生效（i18n 响应式）+ 持久化（§2/§7） */
  function setLang(next: AppLocale): void {
    lang.value = next
    setLocale(next)
    writeStorage(STORAGE_KEYS.lang, next)
  }

  /**
   * 记录用户模型选择（ModelSelect 数据源）；null = 清除用户选择——
   * 提交体不含 model 字段，走服务端缺省（api.md §2.1 / frontend-api.md §2 v1.7）。
   */
  function setModel(next: ChatModelKey | null): void {
    model.value = next
    writeStorage(STORAGE_KEYS.model, next)
  }

  /** 无痕开关（v1.3）：切换即时生效并持久化；激活时 TopBar 高亮 + IncognitoBanner 常驻 */
  function toggleIncognito(): void {
    incognito.value = !incognito.value
    writeStorage(STORAGE_KEYS.incognito, incognito.value ? 'true' : 'false')
  }

  /** 并发策略（v1.5）：null = 清除键、提交体不含该字段、走服务端缺省 */
  function setOnConflict(next: OnConflictStrategy | null): void {
    onConflict.value = next
    writeStorage(STORAGE_KEYS.onConflict, next)
  }

  /** 主题三选（REQ-054）：切换即时生效（dataset 写入归 useThemeEffect）+ 持久化 */
  function setTheme(next: ThemePreference): void {
    theme.value = next
    writeStorage(STORAGE_KEYS.theme, next)
  }

  /** 系统深色偏好同步（useThemeEffect 的 matchMedia change 回调；system 档实时跟随的数据面） */
  function syncSystemPrefersDark(matches: boolean): void {
    systemPrefersDark.value = matches
  }

  return {
    lang,
    model,
    incognito,
    onConflict,
    theme,
    resolvedTheme,
    setLang,
    setModel,
    toggleIncognito,
    setOnConflict,
    setTheme,
    syncSystemPrefersDark,
  }
})
