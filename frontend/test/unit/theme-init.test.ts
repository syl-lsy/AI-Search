/**
 * test/unit/theme-init.test.ts — theme-init.js 首帧主题预置（评审 P2-1，REQ-054/A38 闪变修复）。
 *
 * 背景：原 index.html 内联 `<style>html{background:#191a1a}</style>` 硬编码暗底 + data-theme
 * 要等 JS 应用加载后才写入——浅色用户每次刷新经历暗→白闪变，且 CSP script-src 'self'
 * 禁内联预置脚本。修复形态：
 *   - `public/theme-init.js` 外部小脚本（'self' 允许），head 内同步阻塞置于样式前——首帧前
 *     读 localStorage['ai-search.theme']（try/catch）+ matchMedia 系统偏好，写
 *     document.documentElement.dataset.theme；
 *   - index.html 基座色改双值 CSS（:root 暗缺省 + html[data-theme='light'] 白底）。
 * 本文件两类断言：① 脚本语义（jsdom 内按生产形态原样 eval，存值/系统/脏值/存储不可用四路）；
 * ② index.html 静态契约（脚本先于样式、双值基座色——首帧次序是本修复的承重结构）。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// vitest root = frontend/（pnpm --filter frontend exec 运行面）；以 cwd 解析静态资产路径
// （import.meta.url 经 vite 变换后非 file:// scheme，URL 解析不可用）
const scriptSource = readFileSync(resolve(process.cwd(), 'public/theme-init.js'), 'utf8')
const indexHtml = readFileSync(resolve(process.cwd(), 'index.html'), 'utf8')

/** 按生产形态原样执行（静态自研 IIFE；jsdom 提供 document/localStorage，matchMedia 由用例 stub） */
function runThemeInit(): void {
  delete document.documentElement.dataset.theme
  // 静态自研脚本原样执行（非用户输入拼接），eval 无注入面
  // eslint-disable-next-line no-eval
  eval(scriptSource)
}

const mediaListeners = new Set<(e: MediaQueryListEvent) => void>()
let systemDark = false

function stubMatchMedia(matches: boolean): void {
  systemDark = matches
  vi.stubGlobal(
    'matchMedia',
    vi.fn().mockImplementation((query: string) => ({
      matches: query.includes('prefers-color-scheme: dark') ? systemDark : false,
      media: query,
      onchange: null,
      addEventListener: (_type: string, cb: (e: MediaQueryListEvent) => void) => {
        mediaListeners.add(cb)
      },
      removeEventListener: (_type: string, cb: (e: MediaQueryListEvent) => void) => {
        mediaListeners.delete(cb)
      },
      addListener: vi.fn(),
      removeListener: vi.fn(),
    })),
  )
}

beforeEach(() => {
  localStorage.clear()
  mediaListeners.clear()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('theme-init.js 脚本语义（首帧 data-theme 预置，与 prefs store 解析同构）', () => {
  it('显式 light/dark：直接按存值写 dataset', () => {
    stubMatchMedia(false)
    localStorage.setItem('ai-search.theme', 'light')
    runThemeInit()
    expect(document.documentElement.dataset.theme).toBe('light')
    localStorage.setItem('ai-search.theme', 'dark')
    runThemeInit()
    expect(document.documentElement.dataset.theme).toBe('dark')
  })

  it('system / 缺省：按 matchMedia 系统偏好解析', () => {
    localStorage.setItem('ai-search.theme', 'system')
    stubMatchMedia(true)
    runThemeInit()
    expect(document.documentElement.dataset.theme).toBe('dark')
    stubMatchMedia(false)
    runThemeInit()
    expect(document.documentElement.dataset.theme).toBe('light')

    localStorage.removeItem('ai-search.theme')
    stubMatchMedia(true)
    runThemeInit()
    expect(document.documentElement.dataset.theme).toBe('dark')
  })

  it('脏值（非三档枚举）：按 system 档解析（与 prefs store 初始化收窄同口径）', () => {
    localStorage.setItem('ai-search.theme', 'sepia')
    stubMatchMedia(true)
    runThemeInit()
    expect(document.documentElement.dataset.theme).toBe('dark')
  })

  it('localStorage 不可用（隐私模式）：try/catch 静默，按 system 档解析不抛错', () => {
    stubMatchMedia(false)
    const readSpy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    expect(() => runThemeInit()).not.toThrow()
    expect(document.documentElement.dataset.theme).toBe('light')
    readSpy.mockRestore()
  })
})

describe('index.html 静态契约（首帧次序 + 双值基座色，评审 P2-1 承重结构）', () => {
  it('theme-init.js 以同步外部脚本置于内联样式之前（写 data-theme 先于基座色渲染）', () => {
    // 行首锚定匹配真实标签（CSP 注释文本中的 <style> 字样不参与判定）
    const scriptIdx = indexHtml.search(/^\s*<script src="\/theme-init\.js">/m)
    const styleIdx = indexHtml.search(/^\s*<style>/m)
    expect(scriptIdx).toBeGreaterThan(-1)
    expect(styleIdx).toBeGreaterThan(scriptIdx)
  })

  it('基座色双值：暗色 :root 缺省保留 + 浅色 html[data-theme=\'light\'] 白底（无单值暗底残留）', () => {
    expect(indexHtml).toContain('html { background: #191a1a; }')
    expect(indexHtml).toContain("html[data-theme='light'] { background: #ffffff; }")
  })
})
