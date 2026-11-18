<script setup lang="ts">
/**
 * ThreadActionMenu — 线程管理三点菜单（frontend-api.md §5 v1.21 / REQ-059/A43；
 * 侧栏最近项与会话页行级共用一个组件——D-16）。
 *
 * props：thread 投影（ThreadListItem，含 pinned/is_unread/archived）+ variant=active|archived。
 * 菜单项集（props 驱动）：
 *   - active：置顶·取消置顶 / 标记为未读·标记为已读（v1.24 未读状态感知——按
 *     thread.is_unread 双向：已读项显「标记为未读」〔store.markUnread，PATCH is_unread:true〕、
 *     未读项显「标记为已读」〔store.markRead，PATCH is_unread:false；服务端 §2.10 同语义
 *     端点零后端改动〕；图标同枚举复用 i-alert/i-check）/ 重命名 / 归档 / 删除；
 *   - archived：取消归档 / 重命名 / 删除（置顶与标记为未读**隐藏**，不是禁用——D-16）。
 *   注：active 集含「归档」——PRD A43/A44 GWT「点『归档』」的入口侧载体（frontend-api §5
 *   D-16 枚举未列，随验收口径补，§1 ThreadsView 行「行级三点菜单 → PATCH」语义内）。
 * 动作全部经 threadListStore 动作面（§4——乐观更新+失败回滚+toast 在 store 内聚），
 * 组件零直接触网。删除二次确认（轻确认弹层 threads.deleteConfirm）；重命名 = 弹层输入
 * 1~120 字符（前端先校验：空串禁提交 + maxlength=120；服务端 400001 兜底）。
 * a11y：menu/menuitem 语义、打开即落首项、方向键移动焦点、Esc 关闭、焦点返还触发钮、
 * 外点（mousedown）关闭。
 */
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'

import UiIcon from '@/components/icons/UiIcon.vue'
import UiTooltip from '@/components/common/UiTooltip.vue'
import ThreadModal from '@/components/threads/ThreadModal.vue'
import { THREAD_TITLE_MAX_LENGTH as TITLE_MAX_LENGTH } from '@/constants/ui'
import { useThreadListStore } from '@/stores/threadList'
import type { ThreadListItem } from '@/types/api'

const props = withDefaults(
  defineProps<{
    thread: ThreadListItem
    variant?: 'active' | 'archived'
  }>(),
  { variant: 'active' },
)

const { t } = useI18n()
const store = useThreadListStore()
const route = useRoute()
const router = useRouter()

/* ---------- 层级状态机：closed → menu → confirm | rename（Esc 任意层关闭） ---------- */
const layer = ref<'closed' | 'menu' | 'confirm' | 'rename'>('closed')
const rootEl = ref<HTMLElement | null>(null)
const triggerEl = ref<HTMLButtonElement | null>(null)
const menuEl = ref<HTMLElement | null>(null)
const renameInputEl = ref<HTMLInputElement | null>(null)
const renameValue = ref('')

/* ---------- 弹层定位（v1.25 裁剪修复）：Teleport body + position:fixed ----------
 * 侧栏导航区（.sb-nav overflow-y:auto 滚动容器）与会话页行级列表容器都会裁剪 absolute 弹层（置顶区单行时
 * 菜单只剩首项可见）——脱离容器改为 fixed：读触发钮 rect，向下首选（贴底翻上）、右对齐
 * 触发钮右缘并左右钳制视口内；渲染后 nextTick 实测菜单宽高校正。window scroll/resize
 * 即隐（capture 捕获侧栏容器滚动；与预览卡「滚动即隐」口径一致——fixed 不随容器滚，
 * 顶飞不如关掉重开）。z-index 75 > 预览卡 65 / UiTooltip 70。 */
const MENU_GAP_PX = 4
const MENU_EDGE_GAP_PX = 8
const menuPos = ref<{ top: number; left: number } | null>(null)

function positionMenu(): void {
  const trigger = triggerEl.value
  if (!trigger) return
  const rect = trigger.getBoundingClientRect()
  const vw = window.innerWidth
  const vh = window.innerHeight
  const menu = menuEl.value
  const mw = menu?.offsetWidth || 160
  const mh = menu?.offsetHeight || 0
  // 垂直：向下首选；down 会越出视口底且上翻不越顶 → 改锚触发钮上缘
  const downTop = rect.bottom + MENU_GAP_PX
  const flip = downTop + mh > vh && rect.top - mh - MENU_GAP_PX >= 0
  const top = flip ? rect.top - mh - MENU_GAP_PX : downTop
  // 水平：右对齐触发钮右缘 + 视口内钳制（窄屏防溢出）
  const left = Math.min(Math.max(MENU_EDGE_GAP_PX, rect.right - mw), Math.max(MENU_EDGE_GAP_PX, vw - mw - MENU_EDGE_GAP_PX))
  menuPos.value = { top, left }
}

function onWinScrollOrResize(): void {
  if (layer.value === 'menu') closeAll()
}

function closeAll(refocus = false): void {
  layer.value = 'closed'
  menuPos.value = null
  if (refocus) triggerEl.value?.focus()
}

function toggleMenu(): void {
  if (layer.value === 'closed') {
    menuPos.value = null
    layer.value = 'menu'
    void nextTick(() => {
      positionMenu()
      focusItemAt(0)
    })
  } else {
    closeAll(true)
  }
}

function openConfirm(): void {
  layer.value = 'confirm'
  void nextTick(() => wConfirmCancel.value?.focus())
}

function openRename(): void {
  renameValue.value = props.thread.title
  layer.value = 'rename'
  void nextTick(() => {
    renameInputEl.value?.focus()
    renameInputEl.value?.select()
  })
}

/* ---------- 菜单项集（props 驱动） ---------- */
interface MenuItem {
  key: string
  label: string
  icon: string
  run: () => void
}

const menuItems = computed<MenuItem[]>(() => {
  if (props.variant === 'archived') {
    return [
      { key: 'unarchive', label: t('threads.unarchive'), icon: 'i-archive', run: () => void store.toggleArchive(props.thread.thread_id) },
      { key: 'rename', label: t('threads.rename'), icon: 'i-pencil', run: openRename },
      { key: 'delete', label: t('threads.delete'), icon: 'i-trash', run: openConfirm },
    ]
  }
  /* v1.24 未读状态感知：已读项=「标记为未读」（is_unread:true）；未读项=「标记为已读」
   * （store.markRead → PATCH is_unread:false）——图标同枚举复用 i-alert/i-check */
  const markStateItem: MenuItem = props.thread.is_unread
    ? { key: 'markRead', label: t('threads.markRead'), icon: 'i-check', run: () => void store.markRead(props.thread.thread_id) }
    : { key: 'markUnread', label: t('threads.markUnread'), icon: 'i-alert', run: () => void store.markUnread(props.thread.thread_id) }
  return [
    {
      key: 'pin',
      label: props.thread.pinned ? t('threads.unpin') : t('threads.pin'),
      icon: 'i-pin',
      run: () => void store.togglePin(props.thread.thread_id),
    },
    markStateItem,
    { key: 'rename', label: t('threads.rename'), icon: 'i-pencil', run: openRename },
    { key: 'archive', label: t('threads.archive'), icon: 'i-archive', run: () => void store.toggleArchive(props.thread.thread_id) },
    { key: 'delete', label: t('threads.delete'), icon: 'i-trash', run: openConfirm },
  ]
})

function runItem(item: MenuItem): void {
  item.run()
  if (item.key !== 'rename' && item.key !== 'delete') closeAll(true)
}

/* ---------- 重命名（1~120 字符前端先校验；空串/纯空白禁提交） ---------- */
const renameValid = computed(() => {
  const v = renameValue.value.trim()
  return v !== '' && v.length <= TITLE_MAX_LENGTH
})

function submitRename(): void {
  if (!renameValid.value) return
  const value = renameValue.value.trim()
  closeAll()
  void store.renameThread(props.thread.thread_id, value)
}

/** Enter 提交（P2-2）：IME 组合期候选确认（isComposing=true）不提交——半成品输入直接
 *  关弹层会以未合成文本走 PATCH；组合结束后的正常 Enter 才提交 */
function onRenameEnter(e: KeyboardEvent): void {
  if (e.isComposing) return
  submitRename()
}

/* ---------- 删除二次确认 ----------
 * 删除成功且当前路由正是被删线程（route.params.threadId 命中）→ 跳回首页（新建）：
 * 单点覆盖侧栏（App.vue 置顶/最近两分区）与会话页（ThreadsView 行级）两处复用——
 * 非当前线程场景（会话页行级删除、侧栏删别的线程）params 不匹配自然 no-op。
 * 失败（store 返回 false：409005 有进行中轮次/网络异常等——store 已 toast 且行回滚保留）不跳转。 */
const wConfirmCancel = ref<HTMLButtonElement | null>(null)

async function confirmDelete(): Promise<void> {
  closeAll()
  const deleted = await store.deleteThread(props.thread.thread_id)
  if (deleted && route.params.threadId === props.thread.thread_id) {
    await router.push('/')
  }
}

/* ---------- 键盘：Esc 关闭+焦点返还；方向键在 menuitem 间移动 ----------
 * v1.25：菜单 Teleport 到 body——menuitem 查询走 document（rootEl 内已无菜单层） */
function focusItemAt(index: number): void {
  const nodes = document.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')
  nodes?.[Math.max(0, Math.min(index, (nodes?.length ?? 1) - 1))]?.focus()
}

function onTriggerKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape') {
    e.stopPropagation()
    closeAll(true)
    return
  }
  if (layer.value !== 'menu') return
  const count = menuItems.value.length
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    focusItemAt(0)
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    focusItemAt(count - 1)
  }
}

function onItemKeydown(e: KeyboardEvent, index: number): void {
  if (e.key === 'Escape') {
    e.stopPropagation()
    closeAll(true)
    return
  }
  const count = menuItems.value.length
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    focusItemAt((index + 1) % count)
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    focusItemAt((index - 1 + count) % count)
  }
}

/* ---------- 外点关闭（mousedown 在根外且在菜单层外；仅菜单层——confirm/rename 已迁居中
 *  modal，遮罩点击关闭由 ThreadModal 承载，避免双通道竞争）。
 *  v1.25 回归修复：菜单 Teleport 到 body 后不在 rootEl 内——豁免必须包含 menuEl，否则
 *  按下菜单项的 mousedown 被判「外部」→ closeAll 先卸载菜单 → click 永远到不了 runItem
 *  （用户实测：侧栏三点菜单所有项点击无效） ---------- */
function onDocMousedown(e: MouseEvent): void {
  if (layer.value === 'closed' || layer.value === 'confirm' || layer.value === 'rename') return
  const target = e.target
  if (target instanceof Node && (rootEl.value?.contains(target) || menuEl.value?.contains(target))) return
  closeAll()
}
onMounted(() => {
  document.addEventListener('mousedown', onDocMousedown)
  // capture=true：侧栏 .sb-nav / 会话页列表等容器滚动不冒泡，捕获阶段在 window 截获
  window.addEventListener('scroll', onWinScrollOrResize, true)
  window.addEventListener('resize', onWinScrollOrResize)
})
onUnmounted(() => {
  document.removeEventListener('mousedown', onDocMousedown)
  window.removeEventListener('scroll', onWinScrollOrResize, true)
  window.removeEventListener('resize', onWinScrollOrResize)
})
</script>

<template>
  <!-- v1.25：菜单层 Teleport 后，keydown 语义收敛于触发钮（onTriggerKeydown）与
       menuitem（onItemKeydown，Esc/箭头均 stopPropagation）——rootEl 层无 keydown 路径 -->
  <div ref="rootEl" class="tam-root" data-testid="thread-action-menu">
    <UiTooltip :label="$t('answer.moreActions')">
      <button
        ref="triggerEl"
        type="button"
        class="tam-trigger"
        :aria-label="$t('answer.moreActions')"
        :aria-haspopup="layer === 'menu' ? 'menu' : undefined"
        :aria-expanded="layer === 'menu' ? 'true' : 'false'"
        data-testid="tam-trigger"
        @click="toggleMenu"
        @keydown="onTriggerKeydown"
      >
        <UiIcon name="i-more" size="sm" />
      </button>
    </UiTooltip>

    <!-- 菜单层（role=menu；打开即落首项；方向键移动焦点）。
        v1.25 裁剪修复：Teleport body + fixed 定位——脱离侧栏导航区滚动容器（.sb-nav overflow-y:auto）与
        会话页行级容器的 overflow 裁剪，完整盖在内容上（Perplexity 形态）；
        定位=触发钮 rect 驱动（positionMenu：向下首选/贴底翻上/右对齐+视口钳制） -->
    <Teleport to="body">
      <div
        v-if="layer === 'menu'"
        ref="menuEl"
        class="tam-pop tam-menu"
        role="menu"
        :aria-label="$t('answer.moreActions')"
        :style="menuPos ? { top: `${menuPos.top}px`, left: `${menuPos.left}px` } : undefined"
      >
        <button
          v-for="(item, i) in menuItems"
          :key="item.key"
          type="button"
          role="menuitem"
          class="tam-item"
          :class="{ danger: item.key === 'delete' }"
          :data-testid="`tam-item-${item.key}`"
          @click="runItem(item)"
          @keydown="onItemKeydown($event, i)"
        >
          <UiIcon :name="item.icon" size="sm" />
          {{ item.label }}
        </button>
      </div>
    </Teleport>

    <!-- 删除二次确认（v1.23：居中 modal；取消不发请求；role=alertdialog 语义）。
        三层互斥由 layer 状态机保证（Teleport 打断 v-if/else 链后改独立 v-if，语义等价） -->
    <ThreadModal
      v-if="layer === 'confirm'"
      :open="true"
      :title="$t('threads.delete')"
      role="alertdialog"
      @close="closeAll(true)"
    >
      <div class="tam-confirm" data-testid="tam-confirm">
        <p class="tam-confirm-text">{{ $t('threads.deleteConfirm') }}</p>
        <div class="tam-confirm-actions">
          <button type="button" ref="wConfirmCancel" class="tam-btn" data-testid="tam-cancel" @click="closeAll(true)">
            {{ $t('common.cancel') }}
          </button>
          <button type="button" class="tam-btn danger" data-testid="tam-confirm-delete" @click="confirmDelete">
            {{ $t('threads.delete') }}
          </button>
        </div>
      </div>
    </ThreadModal>

    <!-- 重命名（v1.23：居中 modal——预填全选输入框 + 单个深色保存钮；取消=Esc/遮罩/×） -->
    <ThreadModal
      v-if="layer === 'rename'"
      :open="true"
      :title="$t('threads.renameTitle')"
      @close="closeAll(true)"
    >
      <div class="tam-rename" data-testid="tam-rename">
        <input
          ref="renameInputEl"
          v-model="renameValue"
          class="tam-input"
          type="text"
          :maxlength="TITLE_MAX_LENGTH"
          :aria-label="$t('threads.renameTitle')"
          data-testid="tam-rename-input"
          @keydown.enter.prevent="onRenameEnter"
        />
        <div class="tam-confirm-actions">
          <button
            type="button"
            class="tam-btn save"
            data-testid="tam-rename-ok"
            :disabled="!renameValid"
            @click="submitRename"
          >
            {{ $t('threads.save') }}
          </button>
        </div>
      </div>
    </ThreadModal>
  </div>
</template>

<style scoped>
.tam-root {
  position: relative;
  flex: none;
  display: inline-flex;
}
/* 触发钮：行 hover 显现（父容器 .thread-row:hover / .sb-recent-row:hover 控制 opacity） */
.tam-trigger {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border-radius: var(--radius-full);
  color: var(--color-text-tertiary);
  opacity: 0;
  transition: opacity var(--duration-fast) var(--ease-out), color var(--duration-fast) var(--ease-out),
    background var(--duration-fast) var(--ease-out);
}
.tam-root:hover .tam-trigger,
.tam-trigger:focus-visible,
.tam-root:focus-within .tam-trigger {
  opacity: 1;
}
.tam-trigger:hover {
  color: var(--color-text-primary);
  background: var(--color-surface-ghost);
}
/* 弹层（v1.25 裁剪修复）：Teleport body + position:fixed——定位坐标由 positionMenu 注入
   （内联 top/left），脱离侧栏列表与会话页行级容器的 overflow 裁剪；
   z-index 75 > 预览卡 65 / UiTooltip 70（盖在全部浮层内容上） */
.tam-pop {
  position: fixed;
  z-index: 75;
  min-width: 160px;
  background: var(--color-surface);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-floating);
  padding: var(--space-1);
}
.tam-menu {
  display: grid;
}
.tam-item {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-sm);
  font-size: var(--text-sm);
  color: var(--color-text-primary);
  text-align: left;
  white-space: nowrap;
  transition: background var(--duration-fast) var(--ease-out);
}
.tam-item:hover,
.tam-item:focus-visible {
  background: var(--color-surface-ghost);
}
.tam-item.danger {
  color: var(--color-danger);
}
.tam-confirm-text {
  font-size: var(--text-sm);
  color: var(--color-text-primary);
  white-space: normal;
}
.tam-input {
  display: block;
  width: 100%;
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-sm);
  background: var(--color-surface);
  color: var(--color-text-primary);
  font-size: var(--text-sm);
}
.tam-input:focus {
  outline: none;
  border-color: var(--color-accent-border);
}
.tam-confirm-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
  margin-top: var(--space-4);
}
.tam-btn {
  padding: var(--space-1) var(--space-3);
  border-radius: var(--radius-sm);
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  border: 1px solid var(--color-border);
  transition: background var(--duration-fast) var(--ease-out), color var(--duration-fast) var(--ease-out);
}
.tam-btn:hover {
  background: var(--color-surface-ghost);
  color: var(--color-text-primary);
}
.tam-btn.danger {
  background: var(--color-danger);
  border-color: transparent;
  color: var(--color-text-inverse);
}
.tam-btn.danger:hover {
  filter: brightness(1.08);
}
/* 重命名保存钮（v1.23）：单个深色钮（Perplexity 形态）——主题反转高对比（tokens .btn-primary 同款取值） */
.tam-btn.save {
  min-width: 72px;
  padding: var(--space-2) var(--space-4);
  background: var(--color-text-primary);
  border-color: transparent;
  color: var(--color-text-inverse);
}
.tam-btn.save:hover {
  filter: brightness(1.08);
  background: var(--color-text-primary);
  color: var(--color-text-inverse);
}
.tam-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
</style>
