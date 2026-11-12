import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { i18n } from '@/i18n'
import { usePrefsStore } from '@/stores/prefs'

describe('prefsStore（frontend-api.md §4 / §2 localStorage 约定）', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    i18n.global.locale.value = 'zh-CN'
  })

  it('默认值：zh-CN / model=null（不携带走服务端缺省）/ incognito=false / onConflict=null', () => {
    const prefs = usePrefsStore()
    expect(prefs.lang).toBe('zh-CN')
    expect(prefs.model).toBeNull()
    expect(prefs.incognito).toBe(false)
    expect(prefs.onConflict).toBeNull()
  })

  it('setLang：即时生效（i18n 响应式切换）+ 持久化 ai-search.lang', () => {
    const prefs = usePrefsStore()
    prefs.setLang('en-US')
    expect(prefs.lang).toBe('en-US')
    expect(i18n.global.locale.value).toBe('en-US')
    expect(localStorage.getItem('ai-search.lang')).toBe('en-US')
  })

  it('setModel：持久化；null 清除键（提交体不含 model，走服务端缺省）', () => {
    const prefs = usePrefsStore()
    prefs.setModel('glm')
    expect(prefs.model).toBe('glm')
    expect(localStorage.getItem('ai-search.model')).toBe('glm')
    prefs.setModel(null)
    expect(prefs.model).toBeNull()
    expect(localStorage.getItem('ai-search.model')).toBeNull()
  })

  it('toggleIncognito：切换并持久化（v1.3）', () => {
    const prefs = usePrefsStore()
    prefs.toggleIncognito()
    expect(prefs.incognito).toBe(true)
    expect(localStorage.getItem('ai-search.incognito')).toBe('true')
    prefs.toggleIncognito()
    expect(prefs.incognito).toBe(false)
    expect(localStorage.getItem('ai-search.incognito')).toBe('false')
  })

  it('setOnConflict：策略持久化；null 清除键（v1.5）', () => {
    const prefs = usePrefsStore()
    prefs.setOnConflict('enqueue')
    expect(prefs.onConflict).toBe('enqueue')
    expect(localStorage.getItem('ai-search.onConflict')).toBe('enqueue')
    prefs.setOnConflict(null)
    expect(localStorage.getItem('ai-search.onConflict')).toBeNull()
  })

  it('初始化收窄：localStorage 脏值回退默认（§8.9③）', () => {
    localStorage.setItem('ai-search.lang', 'fr-FR')
    localStorage.setItem('ai-search.model', 'chatgpt')
    localStorage.setItem('ai-search.incognito', '1')
    localStorage.setItem('ai-search.onConflict', 'fallback')
    const prefs = usePrefsStore()
    expect(prefs.lang).toBe('zh-CN')
    expect(prefs.model).toBeNull()
    expect(prefs.incognito).toBe(false)
    expect(prefs.onConflict).toBeNull()
  })

  it('存储不可用时静默降级：内存态照常生效（读/写两 catch 分支）', () => {
    const readSpy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    const writeSpy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    const prefs = usePrefsStore()
    expect(prefs.lang).toBe('zh-CN') // 读失败 → 默认值
    prefs.setLang('en-US') // 写失败 → 仅内存态
    expect(prefs.lang).toBe('en-US')
    readSpy.mockRestore()
    writeSpy.mockRestore()
  })
})
