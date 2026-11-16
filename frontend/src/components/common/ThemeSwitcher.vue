<script setup lang="ts">
/**
 * ThemeSwitcher — 左下用户区主题三选弹层（REQ-054/A38，frontend-api.md §5 v1.18 落位）。
 *
 * 形态：侧栏底部「默认用户」区整体为触发钮，点击弹出轻弹层——「外观」行 radio 三选
 * （浅色/深色/跟随系统，对标 Perplexity 头像菜单外观子菜单）。
 * a11y（A38 硬性）：role=radiogroup/radio、方向键可切换（roving tabindex）、Tab 可达、
 * 选中态 aria-checked；关闭（选择/Esc/外点）后焦点返还触发钮。
 * 生效机制：setup 内装配 useThemeEffect（dataset 写入 + system 档 matchMedia change
 * 实时跟随 + 卸载清理——App 壳挂载一次，生命周期随本组件）。
 */
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import UiTooltip from '@/components/common/UiTooltip.vue'
import { useThemeEffect } from '@/composables/theme'
import { usePrefsStore } from '@/stores/prefs'
import type { ThemePreference } from '@/utils/guards'

const { t } = useI18n()
const prefs = usePrefsStore()
useThemeEffect()

const OPTIONS: { value: ThemePreference; labelKey: string }[] = [
  { value: 'light', labelKey: 'theme.light' },
  { value: 'dark', labelKey: 'theme.dark' },
  { value: 'system', labelKey: 'theme.system' },
]

const open = ref(false)
const triggerEl = ref<HTMLButtonElement | null>(null)
const rootEl = ref<HTMLElement | null>(null)

function toggle(): void {
  if (open.value) close()
  else show()
}

function show(): void {
  open.value = true
  // 焦点移入弹层：当前选中项即焦点位（radio 组语义——方向键即刻切换，Tab 关闭路径连续）
  void nextTick(() => {
    rootEl.value?.querySelector<HTMLButtonElement>('button[role="radio"][aria-checked="true"]')?.focus()
  })
}

/** 关闭并返还焦点（A38：关闭后焦点返还触发点，键盘 Tab 位置连续） */
function close(): void {
  if (!open.value) return
  open.value = false
  triggerEl.value?.focus()
}

function onTriggerKeydown(e: KeyboardEvent): void {
  // 触发钮上箭头/Enter 直接展开并进入 radio 组（Tab 序最短路径）
  if (open.value) return
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault()
    show()
  }
}

/** 选择即应用 + 关闭（Perplexity 头像菜单同款单选行为；即时生效由 useThemeEffect 承接） */
function select(value: ThemePreference): void {
  prefs.setTheme(value)
  close()
}

/** 方向键在 radio 组内移动选择（radio 语义：箭头即切换并应用，A38） */
function onGroupKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape') {
    e.stopPropagation()
    close()
    return
  }
  const idx = OPTIONS.findIndex((o) => o.value === prefs.theme)
  if (e.key === 'ArrowDown' || e.key === 'ArrowRight') {
    e.preventDefault()
    select(OPTIONS[(idx + 1) % OPTIONS.length]!.value)
  } else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') {
    e.preventDefault()
    select(OPTIONS[(idx - 1 + OPTIONS.length) % OPTIONS.length]!.value)
  }
}

/** 外点关闭（mousedown 于弹层/触发钮之外） */
function onDocMousedown(e: MouseEvent): void {
  if (!open.value) return
  const target = e.target
  if (target instanceof Node && rootEl.value?.contains(target)) return
  open.value = false
}

onMounted(() => document.addEventListener('mousedown', onDocMousedown))
onUnmounted(() => document.removeEventListener('mousedown', onDocMousedown))
</script>

<template>
  <div ref="rootEl" class="theme-switcher">
    <UiTooltip :label="$t('theme.trigger')">
      <button
        ref="triggerEl"
        type="button"
        class="sb-user theme-trigger"
        data-testid="theme-trigger"
        aria-haspopup="true"
        :aria-expanded="open ? 'true' : 'false'"
        :aria-label="$t('theme.trigger')"
        @click="toggle"
        @keydown="onTriggerKeydown"
      >
        <span class="avatar" aria-hidden="true">{{ $t('common.userInitial') }}</span>
        <span class="theme-trigger-text">
          <span class="u-name">{{ $t('common.userName') }}</span>
          <span class="u-sub">{{ $t('common.planLabel') }}</span>
        </span>
      </button>
    </UiTooltip>

    <div v-if="open" class="theme-pop" role="dialog" :aria-label="$t('theme.title')" data-testid="theme-pop">
      <div class="theme-pop-title">{{ $t('theme.appearance') }}</div>
      <div
        role="radiogroup"
        :aria-label="$t('theme.appearance')"
        data-testid="theme-radiogroup"
        @keydown="onGroupKeydown"
      >
        <button
          v-for="opt in OPTIONS"
          :key="opt.value"
          type="button"
          role="radio"
          class="theme-opt"
          :class="{ on: prefs.theme === opt.value }"
          :data-testid="`theme-opt-${opt.value}`"
          :aria-checked="prefs.theme === opt.value ? 'true' : 'false'"
          :tabindex="prefs.theme === opt.value ? 0 : -1"
          @click="select(opt.value)"
        >
          <span class="theme-opt-check" aria-hidden="true"></span>
          {{ t(opt.labelKey) }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.theme-switcher {
  position: relative;
  min-width: 0;
}
/* v1.23：触发钮外包 UiTooltip（子组件根 .ui-tip）——恢复块级占满用户区宽度 */
.theme-switcher > .ui-tip {
  display: block;
  width: 100%;
}
/* 触发钮 = 原 .sb-user 用户区形态（tokens.css 布局骨架复用 + button 化） */
.theme-trigger {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  width: 100%;
  min-height: 40px;
  padding: var(--space-1) var(--space-2);
  border-radius: var(--radius-md);
  text-align: left;
  transition: background var(--duration-fast) var(--ease-out);
}
.theme-trigger:hover {
  background: var(--color-surface-ghost);
}
.theme-trigger-text {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.theme-trigger-text .u-name,
.theme-trigger-text .u-sub {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.theme-pop {
  position: absolute;
  left: 0;
  bottom: calc(100% + var(--space-2));
  z-index: 50;
  min-width: 200px;
  padding: var(--space-2);
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-floating);
}
.theme-pop-title {
  padding: var(--space-1) var(--space-2) var(--space-2);
  color: var(--color-text-tertiary);
  font-size: var(--text-xs);
}
.theme-opt {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  width: 100%;
  min-height: 32px;
  padding: 0 var(--space-2);
  border-radius: var(--radius-sm);
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  text-align: left;
  transition: background var(--duration-fast) var(--ease-out), color var(--duration-fast) var(--ease-out);
}
.theme-opt:hover {
  background: var(--color-surface-ghost);
  color: var(--color-text-primary);
}
.theme-opt.on {
  background: var(--color-accent-bg);
  color: var(--color-accent);
}
/* 选中指示（radio 圆点；非选中为空心） */
.theme-opt-check {
  width: 12px;
  height: 12px;
  flex: none;
  border-radius: var(--radius-full);
  border: 1px solid var(--color-border-strong);
  position: relative;
}
.theme-opt.on .theme-opt-check {
  border-color: var(--color-accent);
}
.theme-opt.on .theme-opt-check::after {
  content: '';
  position: absolute;
  inset: 2px;
  border-radius: var(--radius-full);
  background: var(--color-accent);
}
</style>
