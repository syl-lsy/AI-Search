/**
 * theme-init.js — 首帧主题预置（REQ-054/A38 防主题闪变；评审修复项 P2-1，2026-11-15）。
 *
 * 背景：index.html 原内联 `<style>html{background:#191a1a}</style>` 硬编码暗底 + data-theme
 * 要等应用 JS 加载后才写入——浅色用户每次刷新经历暗→白闪变；且 CSP script-src 'self'
 * 禁内联脚本，预置逻辑必须走外部文件（'self' 允许）。
 *
 * 装配：index.html head 内 `<script src="/theme-init.js"></script>` 同步阻塞、置于样式表
 * 与内联基座色 <style> 之前——首帧前写好 document.documentElement.dataset.theme，
 * tokens.css 的 `:root[data-theme='light']` 浅色组与 index.html 双值基座色首帧即生效。
 *
 * 语义与 src/stores/prefs.ts（readStoredTheme/readSystemPrefersDark）及
 * src/composables/theme.ts 同构：显式 light/dark 原样；'system'/缺省/脏值 → matchMedia
 * 系统偏好解析（无 matchMedia 视同 false，与 store 同口径）；localStorage 不可用 try/catch
 * 静默走 system 档。应用接管后 composables/theme.ts 承接后续响应式切换，本脚本不重复监听。
 * 纯 IIFE 零依赖、ES5 语法（不参与 vite 构建管线，须在全部目标浏览器直接可执行）。
 */
(function () {
  'use strict'
  var stored = null
  try {
    stored = window.localStorage.getItem('ai-search.theme')
  } catch (e) {
    stored = null // 隐私模式等存储不可用：静默走 system 档
  }
  var resolved
  if (stored === 'light' || stored === 'dark') {
    resolved = stored
  } else {
    // 'system' / 缺省 / 脏值（非三档枚举）→ 系统偏好解析（与 prefs store 初始化收窄同口径）
    var prefersDark =
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-color-scheme: dark)').matches
    resolved = prefersDark ? 'dark' : 'light'
  }
  document.documentElement.dataset.theme = resolved
})()
