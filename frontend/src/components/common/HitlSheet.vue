<script setup lang="ts">
/**
 * HitlSheet — 通用 HITL 弹层壳（B22 批次 3 第二段组件架构：通用壳+业务皮，用户裁决
 * 2026-11-28——本壳业务无关，为通用 HITL 组件；未来 HITL 场景（M5 记忆审批等）直接复用；
 * ApprovalSheet 为首个业务实例（审批门），经 preview/choices/note 三 slot 注入内容）。
 *
 * 壳职责（全部业务无关）：
 *   - 底部 sheet 覆盖层：Teleport body（避 .app{overflow:hidden} 容器裁剪，ModelSelect
 *     v1.24 先例）+ fixed 定位：顶边紧贴 anchorEl 上沿、底部贴视口底——遮住输入卡且不改
 *     宿主布局（e2e A28 几何以 follow-bar 为基准不破坏）；anchorEl=null 退化贴视口底。
 *   - z-index 层级表扩展位：HITL_SHEET_Z_INDEX=45（constants/ui.ts——drawer(40) 之上、
 *     ToastHost(50) 之下：toast 恒浮于弹层之上；ThreadModal(60) 之下：下钻弹窗可叠）。
 *   - 倒计时机制：expiresAt prop 为准（服务端权威），前端仅展示——timeoutHintText 仅最后
 *     countdownLastSeconds 秒（缺省 15s）显示（role=status）；**零本地超时动作**（超点后
 *     等服务端收口帧，不 emit close、不倒计时计超时）。expiresAt=null 关停倒计时面。
 *   - 错误态机制：errorText prop 非空 → role=alert 区块（业务方注入文案；壳不管来源）。
 *   - 关闭确认机制：×（closeLabel 或 title 作 aria-label）与 cancelLabel 弱钮两入口统一
 *     → 确认区（closeConfirm* 四文案 prop）→「确认」emit close（业务方决定 close 语义，
 *     如取消收口）；「继续等待」撤回；确认态 Esc 仅撤回确认、非确认态 Esc
 *     零副作用（防误按丢轮——取消仅两个显式入口）；busy=true 冻结两入口（提交中防误触）。
 *   - a11y：role=dialog + aria-modal=false（非模态覆盖层——用户可继续浏览正文）+ aria-label
 *     =title；open 时焦点落面板（含挂载即 open 形态——immediate watch，tabindex=-1 承接）、
 *     关闭双相位焦点返还；确认区出现焦点移入、撤回焦点回面板；内容按键可达（slot 内
 *     原生 button/textarea 天然 Tab 序）。
 *   - busy 作用域透传：slot props { busy }——业务按钮/输入以单一来源禁用防重入。
 *
 * 壳内零业务语义：所有文案（title/确认四文案/提示文案/取消钮文案）纯 props 注入，壳内
 * 不内置任何业务词；样式全走 tokens.css 变量零裸值。
 */
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'

import UiIcon from '@/components/icons/UiIcon.vue'
import { HITL_SHEET_Z_INDEX } from '@/constants/ui'

const props = withDefaults(
  defineProps<{
    /** 可见性（业务方状态派生驱动——壳零状态机依赖） */
    open: boolean
    /** fixed 定位参照（宿主输入卡元素；null 退化 = 贴视口底） */
    anchorEl: HTMLElement | null
    /** 标题（业务文案注入；aria-label 同源） */
    title: string
    /** 标题左侧装饰图标（UiIcon 符号名——业务注入，纯视觉 aria-hidden；null=不渲染） */
    icon?: string | null
    /** 倒计时基准（服务端权威 expires_at；null=关停倒计时面） */
    expiresAt?: string | null
    /** 末 N 秒提示文案（业务注入；null=不渲染提示） */
    timeoutHintText?: string | null
    /** 末 N 秒提示阈值（秒；缺省 15——契约 §5「倒计时提示仅最后 15s 显示」值由业务传） */
    countdownLastSeconds?: number
    /** 错误态文案（业务注入；null=不渲染） */
    errorText?: string | null
    /** 提交中（业务按钮禁用防重入 + 壳关闭入口冻结；经 slot props { busy } 透传） */
    busy?: boolean
    /** 弱取消钮文案（业务注入；null=不渲染该钮——× 仍承载关闭入口） */
    cancelLabel?: string | null
    /** 关闭确认四文案（业务注入） */
    closeConfirmTitle: string
    closeConfirmBody: string
    closeConfirmOkLabel: string
    closeConfirmStayLabel: string
  }>(),
  {
    expiresAt: null,
    timeoutHintText: null,
    icon: null,
    countdownLastSeconds: 15,
    errorText: null,
    busy: false,
    cancelLabel: null,
  },
)

const emit = defineEmits<{ (e: 'close'): void }>()

const sheetPanel = ref<HTMLElement | null>(null)

/* ---------- fixed 定位（锚点测量；resize 重算——覆盖层不改宿主布局） ---------- */

const anchorTop = ref<number | null>(null)

function measureAnchor(): void {
  const el = props.anchorEl
  if (el) {
    const top = el.getBoundingClientRect().top
    anchorTop.value = Number.isFinite(top) ? top : null
  } else {
    anchorTop.value = null
  }
}

watch(
  () => props.open,
  (o) => {
    if (o) {
      measureAnchor()
      window.addEventListener('resize', measureAnchor)
    } else {
      window.removeEventListener('resize', measureAnchor)
    }
  },
  { immediate: true }, // 挂载即 open 形态（状态先于挂载成立——SSE/对账先到）初始即测量锚点
)
// open 期间锚点元素替换（宿主重渲染换节点）→ 重测量
watch(
  () => props.anchorEl,
  () => {
    if (props.open) measureAnchor()
  },
)
onUnmounted(() => {
  window.removeEventListener('resize', measureAnchor)
})

/** inline 定位样式（position/left/right/bottom/z-index 恒 inline——jsdom 断言面；top=锚点上沿） */
const sheetStyle = computed<Record<string, string>>(() => ({
  position: 'fixed',
  left: '0',
  right: '0',
  bottom: '0',
  zIndex: String(HITL_SHEET_Z_INDEX),
  ...(anchorTop.value !== null ? { top: `${anchorTop.value}px` } : {}),
}))

/* ---------- 倒计时（服务端 expires_at 为准，前端仅展示、零本地超时动作） ---------- */

const nowMs = ref(Date.now())
let tickTimer: ReturnType<typeof setInterval> | null = null

const remainingSec = computed<number | null>(() => {
  if (props.expiresAt === null || props.expiresAt === undefined) return null
  const expMs = Date.parse(props.expiresAt)
  if (!Number.isFinite(expMs)) return null
  return Math.ceil((expMs - nowMs.value) / 1000)
})

/** 末 N 秒提示（>阈值 与 ≤0 均无 UI——超点后等服务端收口帧，不本地动作） */
const showTimeoutHint = computed(() => {
  if (props.timeoutHintText === null || props.timeoutHintText === undefined) return false
  const remaining = remainingSec.value
  return remaining !== null && remaining > 0 && remaining <= props.countdownLastSeconds
})

watch(
  () => props.open,
  (o) => {
    if (o && tickTimer === null) {
      nowMs.value = Date.now()
      tickTimer = setInterval(() => {
        nowMs.value = Date.now()
      }, 1000)
    } else if (!o && tickTimer !== null) {
      clearInterval(tickTimer)
      tickTimer = null
    }
  },
  { immediate: true },
)
onUnmounted(() => {
  if (tickTimer !== null) clearInterval(tickTimer)
})

/* ---------- 关闭确认（× 与 cancelLabel 弱钮两入口统一；Esc 仅撤回确认） ---------- */

const confirming = ref(false)
const confirmEl = ref<HTMLElement | null>(null)

function requestClose(): void {
  if (props.busy) return // 提交中冻结关闭入口（防误触丢轮）
  confirming.value = true
}

function confirmClose(): void {
  confirming.value = false
  emit('close') // close 语义由业务方决定（业务实例自行定收口——如取消轮不进历史）
}

function stayInSheet(): void {
  confirming.value = false
  void focusPanel()
}

function onKeydown(ev: KeyboardEvent): void {
  // Esc 策略：确认态仅撤回确认（焦点回面板）；非确认态零副作用——防误按丢轮，
  // 取消仅 ×/弱取消钮两个显式入口经确认区承接
  if (ev.key === 'Escape' && confirming.value) {
    ev.stopPropagation()
    stayInSheet()
  }
}

watch(confirming, async (c) => {
  if (c) {
    await nextTick()
    confirmEl.value?.focus()
  }
})

/* ---------- 焦点管理（open 落面板含挂载即 open 形态 / 关闭双相位返还） ---------- */

let previouslyFocused: HTMLElement | null = null

async function focusPanel(): Promise<void> {
  await nextTick()
  sheetPanel.value?.focus()
}

watch(
  () => props.open,
  async (o) => {
    if (o) {
      previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null
      await focusPanel()
    } else {
      confirming.value = false // 重开不复原确认态
      if (previouslyFocused !== null) {
        previouslyFocused.focus()
        previouslyFocused = null
      }
    }
  },
  { immediate: true },
)
onUnmounted(() => {
  if (previouslyFocused !== null) {
    previouslyFocused.focus()
    previouslyFocused = null
  }
})
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      ref="sheetPanel"
      class="hitl-sheet"
      data-testid="hitl-sheet"
      role="dialog"
      aria-modal="false"
      :aria-label="title"
      :style="sheetStyle"
      tabindex="-1"
      @keydown="onKeydown"
    >
      <!-- 标题行 + 关闭钮（关闭入口①——确认机制承接） -->
      <div class="hs-head">
        <span v-if="icon !== null && icon !== undefined" class="hs-ico" aria-hidden="true">
          <UiIcon :name="icon" />
        </span>
        <span class="hs-title">{{ title }}</span>
        <button
          type="button"
          class="hs-close"
          data-testid="hitl-close"
          :aria-label="cancelLabel ?? title"
          :disabled="busy"
          @click="requestClose"
        >
          ×
        </button>
      </div>

      <!-- 错误态（errorText prop——业务方注入文案，壳不管来源） -->
      <div v-if="errorText !== null && errorText !== undefined" class="hs-error" data-testid="hitl-error" role="alert">
        {{ errorText }}
      </div>

      <!-- 业务内容三 slot（preview/choices/note——壳内零业务语义；busy 单一来源透传） -->
      <div class="hs-body">
        <slot name="preview" />
        <slot name="choices" :busy="busy" />
        <slot name="note" :busy="busy" />
      </div>

      <!-- 关闭确认区（两入口统一承接；确认后 emit close——close 语义归业务方） -->
      <div
        v-if="confirming"
        ref="confirmEl"
        class="hs-confirm"
        data-testid="hitl-close-confirm"
        role="group"
        :aria-label="closeConfirmTitle"
        tabindex="-1"
      >
        <p class="hs-confirm-title">{{ closeConfirmTitle }}</p>
        <p class="hs-confirm-body">{{ closeConfirmBody }}</p>
        <div class="hs-confirm-actions">
          <button type="button" class="hs-confirm-stay" data-testid="hitl-confirm-stay" @click="stayInSheet">
            {{ closeConfirmStayLabel }}
          </button>
          <button type="button" class="hs-confirm-ok" data-testid="hitl-confirm-close" @click="confirmClose">
            {{ closeConfirmOkLabel }}
          </button>
        </div>
      </div>

      <!-- 弱取消钮（关闭入口②——文案业务注入；cancelLabel=null 不渲染） -->
      <div v-if="cancelLabel !== null && cancelLabel !== undefined" class="hs-actions">
        <button type="button" class="hs-cancel" data-testid="hitl-cancel" :disabled="busy" @click="requestClose">
          {{ cancelLabel }}
        </button>
      </div>

      <!-- 倒计时提示：仅最后 N 秒显示（消费 expiresAt 纯展示、零本地超时动作） -->
      <div v-if="showTimeoutHint" class="hs-timeout" data-testid="hitl-timeout-hint" role="status">
        {{ timeoutHintText }}
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
/* 底部 sheet 覆盖层：fixed 定位 inline 承载（position/left/right/bottom/z-index）；
   z-index 45 = 抽屉(40) 之上 / ToastHost(50) 之下 / ThreadModal(60) 之下——层级表扩展位
   见 constants/ui.ts HITL_SHEET_Z_INDEX */
.hitl-sheet {
  max-height: 70vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-4);
  background: var(--color-surface);
  border-top: 1px solid var(--color-border-strong);
  box-shadow: var(--shadow-floating);
  outline: none;
}
.hs-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.hs-ico {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  flex: none;
  border-radius: var(--radius-md);
  background: var(--color-accent-bg);
  color: var(--color-accent);
}
.hs-title {
  font-size: var(--text-md);
  font-weight: var(--weight-medium);
  color: var(--color-text-primary);
  flex: 1;
}
.hs-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: var(--radius-full);
  color: var(--color-text-secondary);
  font-size: var(--text-lg);
  transition: background var(--duration-fast) var(--ease-out), color var(--duration-fast) var(--ease-out);
}
.hs-close:hover:not(:disabled) {
  background: var(--color-surface-ghost);
  color: var(--color-text-primary);
}
.hs-close:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.hs-error {
  border: 1px solid var(--color-danger);
  border-radius: var(--radius-md);
  padding: var(--space-2) var(--space-3);
  background: var(--color-surface-ghost);
  color: var(--color-danger);
  font-size: var(--text-sm);
}
.hs-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.hs-confirm {
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  padding: var(--space-3);
  display: grid;
  gap: var(--space-2);
  background: var(--color-surface-ghost);
  outline: none;
}
.hs-confirm-title {
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  color: var(--color-text-primary);
}
.hs-confirm-body {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}
.hs-confirm-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
}
.hs-confirm-stay {
  display: inline-flex;
  align-items: center;
  min-height: 32px;
  padding: 0 var(--space-4);
  border-radius: var(--radius-full);
  border: 1px solid var(--color-border-strong);
  color: var(--color-text-primary);
  font-size: var(--text-sm);
  transition: background var(--duration-fast) var(--ease-out), border-color var(--duration-fast) var(--ease-out);
}
.hs-confirm-stay:hover {
  background: var(--color-surface-ghost);
  border-color: var(--color-text-faint);
}
.hs-confirm-ok {
  display: inline-flex;
  align-items: center;
  min-height: 32px;
  padding: 0 var(--space-4);
  border-radius: var(--radius-full);
  border: 1px solid var(--color-danger);
  color: var(--color-danger);
  font-size: var(--text-sm);
  transition: background var(--duration-fast) var(--ease-out), border-color var(--duration-fast) var(--ease-out);
}
.hs-confirm-ok:hover {
  background: var(--color-surface-ghost);
  border-color: var(--color-danger-hover);
}
.hs-actions {
  display: flex;
  justify-content: flex-end;
}
.hs-cancel {
  display: inline-flex;
  align-items: center;
  min-height: 32px;
  padding: 0 var(--space-4);
  border-radius: var(--radius-full);
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  transition: background var(--duration-fast) var(--ease-out), color var(--duration-fast) var(--ease-out);
}
.hs-cancel:hover:not(:disabled) {
  background: var(--color-surface-ghost);
  color: var(--color-text-primary);
}
.hs-cancel:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.hs-timeout {
  /* 设计稿 06-approval-sheet.html ④ 态视觉语言（契约钉定行为+稿内 warning 色）：末 15s 提示条
     warning 淡底 + warning 字色（tokens 变量，无裸值） */
  display: flex;
  align-items: center;
  gap: var(--space-2);
  border-radius: var(--radius-md);
  padding: var(--space-2) var(--space-3);
  background: var(--color-warning-bg);
  color: var(--color-warning);
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
}
</style>
