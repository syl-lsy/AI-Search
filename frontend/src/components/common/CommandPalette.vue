<script setup lang="ts">
/**
 * CommandPalette — 全局命令面板（修复项⑧b，对标 Perplexity Cmd+K 形态/图 4）。
 *
 * - Teleport 到 body 的居中弹框：顶部搜索框（autofocus）+「命令」分组 +「结果」分组；
 * - 命令 = 本项目功能域三条：新建对话（'/'）/ 搜索知识库（'/library'）/ 查看历史（'/threads'）；
 * - 结果 = threadListStore.recent 最近会话（标题包含匹配，大小写不敏感；P2-2 起为侧栏同源独立投影），点击 → /thread/{id}；
 * - 输入过滤：命令按 label、结果按 title 包含匹配；命令在前结果在后，扁平索引供键盘导航；
 * - 键盘：↑↓ 移动高亮 / Enter 执行当前项 / Esc 关闭；打开时 body overflow hidden 防背景滚动；
 * - a11y：role=dialog + aria-modal + aria-label；遮罩点击关闭。
 */
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'

import UiIcon from '@/components/icons/UiIcon.vue'
import { useThreadListStore } from '@/stores/threadList'

const props = defineProps<{ open: boolean }>()

const emit = defineEmits<{
  close: []
}>()

const { t } = useI18n()
const router = useRouter()
const listStore = useThreadListStore()

/** 结果分组截断（最近会话，防长列表撑爆弹框） */
const RESULT_LIMIT = 8

const query = ref('')
const inputEl = ref<HTMLInputElement | null>(null)
const activeIndex = ref(0)

interface PaletteCommand {
  id: string
  icon: string
  label: string
  /** run 允许返回 router.push 的 Promise（NavigationFailure | void），调用方不消费返回值 */
  run: () => void | Promise<unknown>
}

const commands = computed<PaletteCommand[]>(() => [
  { id: 'new-chat', icon: 'i-plus', label: t('common.paletteNewChat'), run: () => router.push('/') },
  { id: 'kb', icon: 'i-book', label: t('common.paletteSearchKb'), run: () => router.push('/library') },
  { id: 'history', icon: 'i-clock', label: t('common.paletteViewHistory'), run: () => router.push('/threads') },
])

const filteredCommands = computed<PaletteCommand[]>(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return commands.value
  return commands.value.filter((c) => c.label.toLowerCase().includes(q))
})

const filteredResults = computed(() => {
  const q = query.value.trim().toLowerCase()
  const items = q
    ? listStore.recent.filter((it) => it.title.toLowerCase().includes(q))
    : listStore.recent
  return items.slice(0, RESULT_LIMIT)
})

/** 扁平键盘索引：命令在前、结果在后 */
const flatCount = computed(() => filteredCommands.value.length + filteredResults.value.length)

const hasAny = computed(() => flatCount.value > 0)

watch(
  () => props.open,
  async (open) => {
    if (open) {
      query.value = ''
      activeIndex.value = 0
      document.body.style.overflow = 'hidden'
      await nextTick()
      inputEl.value?.focus()
    } else {
      document.body.style.overflow = ''
    }
  },
)

watch(flatCount, (count) => {
  if (activeIndex.value >= count) activeIndex.value = 0
})

/** 关闭（Esc/遮罩/执行导航三出口同口径）：emit 后由宿主（App）把焦点返还触发按钮——
 *  键盘用户回到触发点，Tab 位置连续（P3 可访问性收口；焦点归还不属面板自身职责，
 *  宿主持有触发元素引用） */
function close(): void {
  emit('close')
}

async function runAt(index: number): Promise<void> {
  if (index < 0 || index >= flatCount.value) return
  const cmdCount = filteredCommands.value.length
  close()
  if (index < cmdCount) {
    await filteredCommands.value[index]!.run()
    return
  }
  const item = filteredResults.value[index - cmdCount]
  if (item) await router.push(`/thread/${item.thread_id}`)
}

function onInputKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape') {
    e.preventDefault()
    close()
    return
  }
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    if (flatCount.value > 0) activeIndex.value = (activeIndex.value + 1) % flatCount.value
    return
  }
  if (e.key === 'ArrowUp') {
    e.preventDefault()
    if (flatCount.value > 0) activeIndex.value = (activeIndex.value - 1 + flatCount.value) % flatCount.value
    return
  }
  if (e.key === 'Enter') {
    e.preventDefault()
    void runAt(activeIndex.value)
  }
}

onUnmounted(() => {
  // 关闭态卸载时恢复 body 滚动（打开 watcher 不再触发）
  document.body.style.overflow = ''
})
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="palette-layer">
      <div class="palette-backdrop" data-testid="palette-backdrop" @click="close"></div>
      <div
        class="palette"
        role="dialog"
        aria-modal="true"
        :aria-label="$t('common.paletteLabel')"
        data-testid="command-palette"
      >
        <div class="palette-search">
          <UiIcon name="i-search" size="sm" />
          <input
            ref="inputEl"
            v-model="query"
            type="text"
            :placeholder="$t('common.palettePlaceholder')"
            :aria-label="$t('common.palettePlaceholder')"
            data-testid="palette-input"
            @keydown="onInputKeydown"
          />
        </div>

        <div v-if="filteredCommands.length > 0" class="palette-group">
          <div class="palette-group-title">{{ $t('common.paletteCommands') }}</div>
          <button
            v-for="(cmd, i) in filteredCommands"
            :key="cmd.id"
            type="button"
            class="palette-item"
            :class="{ active: activeIndex === i }"
            :data-testid="`palette-command-${cmd.id}`"
            @mouseenter="activeIndex = i"
            @click="runAt(i)"
          >
            <UiIcon :name="cmd.icon" size="sm" class="item-ico" />
            <span class="item-label">{{ cmd.label }}</span>
          </button>
        </div>

        <div v-if="filteredResults.length > 0" class="palette-group">
          <div class="palette-group-title">{{ $t('common.paletteResults') }}</div>
          <button
            v-for="(item, i) in filteredResults"
            :key="item.thread_id"
            type="button"
            class="palette-item"
            :class="{ active: activeIndex === filteredCommands.length + i }"
            :data-testid="`palette-result-${item.thread_id}`"
            @mouseenter="activeIndex = filteredCommands.length + i"
            @click="runAt(filteredCommands.length + i)"
          >
            <UiIcon name="i-chat" size="sm" class="item-ico" />
            <span class="item-label">{{ item.title }}</span>
            <span class="item-action">{{ $t('common.paletteOpenThread') }}</span>
          </button>
        </div>

        <div v-if="!hasAny" class="palette-empty" role="status" data-testid="palette-empty">
          {{ $t('common.paletteEmpty') }}
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.palette-layer {
  position: fixed;
  inset: 0;
  z-index: 100;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding-top: 12vh;
}
.palette-backdrop {
  position: absolute;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
}
.palette {
  position: relative;
  width: min(560px, calc(100% - var(--space-8)));
  max-height: 60vh;
  display: flex;
  flex-direction: column;
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-floating);
  overflow: hidden;
}
.palette-search {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-4);
  border-bottom: 1px solid var(--color-border);
  color: var(--color-text-tertiary);
}
.palette-search input {
  flex: 1;
  background: none;
  border: 0;
  outline: none;
  color: var(--color-text-primary);
  font-size: var(--text-md);
}
.palette-search input::placeholder {
  color: var(--color-text-faint);
}
.palette-search input:focus-visible {
  box-shadow: none;
}
.palette-group {
  overflow-y: auto;
  padding: var(--space-2) 0;
}
.palette-group + .palette-group {
  border-top: 1px solid var(--color-border-faint);
}
.palette-group-title {
  padding: var(--space-2) var(--space-4);
  color: var(--color-text-tertiary);
  font-size: var(--text-xs);
}
.palette-item {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  width: 100%;
  min-height: 40px;
  padding: var(--space-1) var(--space-4);
  text-align: left;
  color: var(--color-text-primary);
  font-size: var(--text-base);
  transition: background var(--duration-fast) var(--ease-out);
}
.palette-item .item-ico {
  color: var(--color-text-tertiary);
  flex: none;
}
.palette-item .item-label {
  flex: 1 1 auto;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.palette-item .item-action {
  flex: none;
  color: var(--color-text-faint);
  font-size: var(--text-xs);
}
.palette-item:hover,
.palette-item.active {
  background: var(--color-surface-ghost);
}
.palette-item.active .item-action {
  color: var(--color-text-secondary);
}
.palette-empty {
  padding: var(--space-6);
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
  text-align: center;
}
</style>
