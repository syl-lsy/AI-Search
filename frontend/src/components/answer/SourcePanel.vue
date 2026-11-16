<script lang="ts">
/**
 * 组投影类型（frontend-api.md §5 SourcePanel 行 v1.21）：AnswerView 自 store.turns 聚合
 * （renderableTurns 中 sources 非空或该轮 collecting 的轮各成一组、按轮次序排列；direct
 * 轮不出现组——v1.15「无来源不渲染」口径）。独立导出供 AnswerView 类型化构造。
 */
import type { Source } from '@/types/api'

export interface SourcePanelGroup {
  turnId: string
  question: string
  sources: Source[]
  /** 「收集中」徽标只挂当前收集组的组头（live 轮且已进搜索阶段） */
  collecting: boolean
}
</script>

<script setup lang="ts">
/**
 * SourcePanel — 来源面板（frontend-api.md §5 SourcePanel/SourceCard 行；设计稿 sources-panel）。
 *
 * 【v1.21 线程级累计分组语义，REQ-061/A45】props 由 `sources: Source[]` 改
 * `groups: SourcePanelGroup[]`——按轮分组渲染；组标题=「『{question 截断 ~40 字符}』的
 * 搜索结果」（i18n answer.sourcesGroupTitle，截断长度 PANEL_GROUP_TITLE_MAX 前端常量建议值）；
 * 面板头总数 = Σ 各组 sources.length；「收集中」徽标只挂当前收集组的组头（不再挂面板头）；
 * 组容器 `data-turn-group="{turnId}"`——[n] 引用定位改组作用域（AnswerView onCite(turnId,
 * refNo) 在组容器内 querySelector [data-source-card]——修复全局 querySelector 命中首组
 * 同号卡缺陷：每轮 ref_no 从 1 起、跨组必然同号）。追问后新组追加、旧组保留、回看历史轮
 * 面板不重置（聚合态天然满足）；groups 为空整块不渲染（direct 轮无组——父级同口径）。
 * SSE per-turn sources 事件与 api.md §2.5 对账零变更（纯前端聚合消费）。
 *
 * 【设计说明 v1.5 多行卡形态保留】每条来源卡 = favicon 位（域名首字母圆标兜底）+ 域名
 * + 类型徽标行 + 标题 clamp + 摘要 clamp + 组内条目分隔线（相邻兄弟规则）；面板容器圆角
 * 一体（.src-frame max-height 限高内部滚动）+ 顶栏 sticky。
 * 高亮态（A40「来源」操作行联动）：highlightSeq prop 自增 → 根节点 panel-hit 2s 过渡。
 */
import { computed, nextTick, ref, watch, onUnmounted } from 'vue'

import UiIcon from '@/components/icons/UiIcon.vue'
import { PANEL_GROUP_TITLE_MAX, SOURCE_PILL_AVATARS_MAX } from '@/constants/ui'
import { isUuid } from '@/utils/guards'
import { useMediaQuery } from '@/composables/mediaQuery'

const props = withDefaults(
  defineProps<{
    /** 按轮分组的来源组（REQ-061；direct/无来源轮不产组） */
    groups: SourcePanelGroup[]
    /** 定位高亮序号（AnswerView「来源」点击自增；>0 变化触发 2s panel-hit 高亮态） */
    highlightSeq?: number
    /** 当前查看轮（AnswerView 锚点派生；变化时组容器 scrollIntoView + 组头 group-active 高亮；折叠胶囊态不跟随） */
    activeTurnId?: string | null
    /** 折叠态外部强制展开序号（AnswerView onCite/onLocateSources 联动；自增触发，与 highlightSeq 同构） */
    expandSeq?: number
  }>(),
  { highlightSeq: 0, activeTurnId: null, expandSeq: 0 },
)

const emit = defineEmits<{
  snapshot: [sourceId: string]
}>()

const open = ref(true)

/* ---------- A40 来源定位高亮态（2s CSS 过渡；重复触发重新计时） ---------- */
const panelHit = ref(false)
let highlightTimer: ReturnType<typeof setTimeout> | null = null
watch(
  () => props.highlightSeq,
  (seq) => {
    if (seq <= 0) return
    panelHit.value = true
    if (highlightTimer !== null) clearTimeout(highlightTimer)
    highlightTimer = setTimeout(() => {
      panelHit.value = false
      highlightTimer = null
    }, 2_000)
  },
)
onUnmounted(() => {
  if (highlightTimer !== null) clearTimeout(highlightTimer)
})

/** 面板头总数 = Σ 各组 sources.length（§5 v1.21；computed 派生——P3-2，watch+ref 收敛） */
const totalCount = computed(() => props.groups.reduce((sum, g) => sum + g.sources.length, 0))

/* ---------- 折叠胶囊（Perplexity 对齐小批）：胶囊圆标叠 = 跨组前 3 个不同来源 ---------- */

/** 胶囊圆标叠来源（source_id 去重、按组序取前 SOURCE_PILL_AVATARS_MAX 个；首字母复用 initialOf） */
const pillSources = computed(() => {
  const seen = new Set<string>()
  const out: Source[] = []
  for (const g of props.groups) {
    for (const src of g.sources) {
      if (seen.has(src.source_id)) continue
      seen.add(src.source_id)
      out.push(src)
      if (out.length === SOURCE_PILL_AVATARS_MAX) return out
    }
  }
  return out
})

/* ---------- 跟随当前查看轮（Perplexity 对齐小批）：activeTurnId 变化 → 组容器定位 + 组头高亮 ---------- */
const rootEl = ref<HTMLElement | null>(null)

/**
 * 仅在 activeTurnId 变化时触发（不连续追踪——避免与用户手动滚面板打架）；折叠态（胶囊）不跟随；
 * turnId 走 isUuid 守卫（选择器拼接前收窄，与 onCite 同款纵深防御）。组头高亮由
 * `g.turnId === activeTurnId` 声明式呈现（重新展开面板即时反映当前查看轮）。
 */
const isNarrowViewport = useMediaQuery('(max-width: 1200px)')

watch(
  () => props.activeTurnId,
  (turnId, prev) => {
    if (!turnId || turnId === prev || !open.value) return
    if (isNarrowViewport.value) return // 窄屏（≤1200px）面板置顶 static：跳过跟随，避免视口被拽离答案正文（P2-2）
    if (!isUuid(turnId)) return
    rootEl.value?.querySelector(`[data-turn-group="${turnId}"]`)?.scrollIntoView({ block: 'nearest' })
  },
  { flush: 'post' },
)

/** expandSeq 自增 → 外部强制展开（折叠态下 onCite/onLocateSources 先展开再定位；P1/P2-1） */
watch(
  () => props.expandSeq,
  (seq) => {
    if (seq > 0) open.value = true
  },
)

/* ---------- 双形态切换焦点返还（P3-3，沿 App paletteTriggerEl 先例） ---------- */
const headEl = ref<HTMLButtonElement | null>(null)
const pillEl = ref<HTMLButtonElement | null>(null)

/** 折叠：焦点返还胶囊触发钮（键盘 Tab 位置连续） */
async function collapseToPill(): Promise<void> {
  open.value = false
  await nextTick()
  pillEl.value?.focus()
}

/** 展开：焦点落面板头触发钮 */
async function expandFromPill(): Promise<void> {
  open.value = true
  await nextTick()
  headEl.value?.focus()
}

/** 组标题 question 截断（~40 字符建议值 + 省略号；§5-U7 前端常量） */
function groupTitle(question: string): string {
  return question.length > PANEL_GROUP_TITLE_MAX ? `${question.slice(0, PANEL_GROUP_TITLE_MAX)}…` : question
}

/** url 可得时展示域名（来源页链接）；KB 来源（url=null）展示 kb_doc_id 短标识 */
function domainOf(src: Source): string {
  if (src.url) {
    try {
      return new URL(src.url).hostname
    } catch {
      return src.url
    }
  }
  return src.kb_doc_id ? src.kb_doc_id.slice(0, 8) : ''
}

/** favicon 位首字母（域名首字符兜底；KB 来源取 kb_doc_id 短标识首字符；空回退 '?'） */
function initialOf(src: Source): string {
  const domain = domainOf(src)
  const head = (domain || src.title).trim().charAt(0)
  return head === '' ? '?' : head.toUpperCase()
}
</script>

<template>
  <aside
    v-if="groups.length > 0"
    ref="rootEl"
    class="sources-panel" :class="{ 'panel-hit': panelHit }" data-testid="source-panel">
    <!-- 折叠胶囊（Perplexity 对齐小批 v1.7）：open=false 时整面板收成一颗胶囊——来源名+圆标叠×3+Σ总数+›；
         双形态渲染（胶囊态不渲染框架，不再占 max-height）；点击展开回框架 -->
    <button
      v-if="!open"
      ref="pillEl"
      type="button"
      class="src-pill"
      data-testid="source-pill"
      aria-expanded="false"
      @click="expandFromPill"
    >
      <UiIcon name="i-link" size="sm" />
      <span class="src-pill-label">{{ $t('answer.sources') }}</span>
      <span class="src-pill-stack" aria-hidden="true">
        <span v-for="s in pillSources" :key="s.source_id" class="src-pill-chip">{{ initialOf(s) }}</span>
      </span>
      <span class="src-pill-count">{{ totalCount }}</span>
      <UiIcon name="i-chev-r" size="sm" class="src-pill-chev" />
    </button>
    <!-- 展开框架（open=true；v1.21 分组语义原样保留） -->
    <div v-else class="src-frame">
      <button
        ref="headEl"
        type="button"
        class="src-panel-head open"
        aria-expanded="true"
        @click="collapseToPill"
      >
        <UiIcon name="i-link" size="sm" />
        {{ $t('answer.sources') }}
        <span class="count">{{ totalCount }}</span>
        <UiIcon name="i-chev-d" size="sm" class="chev" />
      </button>
      <div class="src-body">
        <!-- 按轮分组（v1.21）：组容器 = [n] 组作用域定位锚点（data-turn-group） -->
        <section
          v-for="(g, gi) in groups"
          :key="g.turnId"
          class="src-group"
          :class="{ 'has-divider': gi > 0 }"
          :data-turn-group="g.turnId"
          :data-testid="`src-group-${g.turnId}`"
        >
          <div class="src-group-title" :class="{ 'group-active': g.turnId === activeTurnId }" :title="g.question">
            <span class="src-group-text">{{ $t('answer.sourcesGroupTitle', { question: groupTitle(g.question) }) }}</span>
            <span v-if="g.collecting" class="badge badge-accent src-group-collecting">{{ $t('answer.collecting') }}</span>
          </div>
          <div
            v-for="(src, i) in g.sources"
            :key="src.source_id"
            class="src-card"
            :class="{ 'has-divider': i > 0 }"
            :data-source-card="src.ref_no"
            :title="src.title"
          >
            <div class="src-line">
              <!-- favicon 位：域名首字母圆标（无真 favicon 兜底）+ [n] 序号角标 -->
              <span class="src-fav" aria-hidden="true">{{ initialOf(src) }}</span>
              <span class="src-num" aria-hidden="true">{{ src.ref_no }}</span>
              <span class="src-domain">{{ domainOf(src) }}</span>
              <span
                class="src-type"
                :class="{ kb: src.source_type === 'kb' }"
                role="img"
                :aria-label="src.source_type === 'kb' ? $t('answer.sourceKb') : $t('answer.sourceWeb')"
                :title="src.source_type === 'kb' ? $t('answer.sourceKb') : $t('answer.sourceWeb')"
              >
                <UiIcon :name="src.source_type === 'kb' ? 'i-book' : 'i-globe'" size="sm" />
              </span>
              <span class="src-spring"></span>
              <button
                v-if="src.snapshot_available"
                type="button"
                class="snapshot-icon"
                :data-testid="`snapshot-${src.ref_no}`"
                :title="$t('answer.snapshot')"
                :aria-label="$t('answer.snapshot')"
                @click="emit('snapshot', src.source_id)"
              >
                <UiIcon name="i-camera" size="sm" />
              </button>
            </div>
            <div class="src-title">{{ src.title }}</div>
            <div class="src-snip">{{ src.snippet }}</div>
          </div>
        </section>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.sources-panel {
  align-self: start;
  position: sticky;
  top: var(--space-4);
  min-width: 0;
  /* A40「来源」定位高亮态：accent 淡底 + 描边过渡（2s 由 highlightSeq 计时移除） */
  transition: box-shadow var(--duration-base) var(--ease-out), background-color var(--duration-base) var(--ease-out);
  border-radius: var(--radius-lg);
}
.sources-panel.panel-hit {
  box-shadow: 0 0 0 2px var(--color-accent-border);
  background: var(--color-accent-bg);
}
/* 圆角一体容器：max-height 限高 + 内部滚动（顶栏 sticky 保留） */
.src-frame {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  max-height: 560px;
  overflow-y: auto;
}
.src-panel-head {
  position: sticky;
  top: 0;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  padding: var(--space-3) var(--space-4);
  background: var(--color-surface);
  border-bottom: 1px solid var(--color-border-faint);
  width: 100%;
}
.src-panel-head .count {
  color: var(--color-text-primary);
  font-weight: var(--weight-medium);
}
.src-panel-head .chev {
  margin-left: auto;
  transition: transform var(--duration-base) var(--ease-out);
}
.src-panel-head.open .chev {
  transform: rotate(180deg);
}
.src-body {
  display: grid;
  grid-template-columns: 1fr;
}
/* 组容器（v1.21）：组间分隔（相邻兄弟规则）；组内条目分隔由卡片承担 */
.src-group {
  padding-bottom: var(--space-2);
}
.src-group.has-divider {
  border-top: 1px solid var(--color-border);
}
/* 组标题行：『question 截断』的搜索结果 + collecting 徽标（只挂当前收集组组头）；
   跟随态过渡（accent 淡底+左缘条，group-active）集中在本基础规则（P3-2 合并） */
.src-group-title {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-4) var(--space-1);
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  transition: background-color var(--duration-base) var(--ease-out), box-shadow var(--duration-base) var(--ease-out);
}
.src-group-text {
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.src-group-collecting {
  flex: none;
}
/* 多行卡（设计说明 v1.5）：条目间分隔线（相邻兄弟规则），非独立卡片描边 */
.src-card {
  padding: var(--space-3) var(--space-4);
  background: transparent;
  transition: background var(--duration-fast) var(--ease-out);
}
.src-card:hover {
  background: var(--color-surface-ghost);
}
.src-card.has-divider {
  border-top: 1px solid var(--color-border-faint);
}
/* [n] 引用联动高亮（AnswerView classList 注入；语义/锚点不变——定位已改组作用域） */
.src-card.cite-hit {
  background: var(--color-accent-bg);
}
/* 首行：favicon 圆标 + [n] 角标 + 域名 + 类型徽标 + 快照（行尾） */
.src-line {
  position: relative;
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.src-fav {
  position: relative;
  width: 32px;
  height: 32px;
  flex: none;
  display: grid;
  place-items: center;
  border-radius: var(--radius-full);
  background: var(--color-surface-active);
  border: 1px solid var(--color-border-strong);
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
}
/* [n] 序号角标（favicon 圆标右下角；[n]↔卡片联动契约的视觉保留面） */
.src-num {
  position: absolute;
  left: 20px;
  top: 16px;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  display: grid;
  place-items: center;
  border-radius: var(--radius-full);
  background: var(--color-accent-bg);
  border: 1px solid var(--color-accent-border);
  color: var(--color-accent);
  font-size: 10px;
  font-weight: var(--weight-medium);
}
.src-domain {
  min-width: 0;
  flex: 1 1 auto;
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.src-type {
  flex: none;
  display: inline-flex;
  align-items: center;
  color: var(--color-text-secondary);
}
/* 知识库类型：tokens 语义紫（--color-kb 徽标语义，§1 色板） */
.src-type.kb {
  color: var(--color-kb);
}
.src-spring {
  flex: none;
}
.snapshot-icon {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: var(--radius-full);
  color: var(--color-text-tertiary);
  transition: color var(--duration-fast) var(--ease-out), background var(--duration-fast) var(--ease-out);
}
.snapshot-icon:hover {
  color: var(--color-text-primary);
  background: var(--color-surface-ghost);
}
/* 标题 1~2 行 clamp */
.src-title {
  margin-top: var(--space-2);
  color: var(--color-text-primary);
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  line-height: 1.45;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
/* 摘要 2~3 行 clamp（v1.5 回归） */
.src-snip {
  margin-top: var(--space-1);
  color: var(--color-text-tertiary);
  font-size: var(--text-xs);
  line-height: 1.5;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
/* 折叠胶囊（Perplexity 对齐小批 v1.7）：收起态 = 单颗胶囊卡（来源名+圆标叠+Σ+›），高度自适应 ~40px */
.src-pill {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  width: 100%;
  padding: var(--space-2) var(--space-3);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-full);
  box-shadow: var(--shadow-card);
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  text-align: left;
  transition: background var(--duration-fast) var(--ease-out);
}
.src-pill:hover {
  background: var(--color-surface-ghost);
}
.src-pill-label {
  flex: none;
}
.src-pill-stack {
  display: inline-flex;
  align-items: center;
  flex: none;
}
.src-pill-chip {
  display: grid;
  place-items: center;
  /* 24px 圆标尺寸（布局值，无对应 token；与 -8px 重叠共同构成圆标叠视觉） */
  width: 24px;
  height: 24px;
  border-radius: var(--radius-full);
  background: var(--color-surface-active);
  border: 1px solid var(--color-border-strong);
  color: var(--color-text-secondary);
  font-size: var(--text-xs);
  font-weight: var(--weight-medium);
}
.src-pill-chip + .src-pill-chip {
  margin-left: -8px; /* 圆标叠：重叠 ~8px（布局值，无对应 token） */
}
.src-pill-count {
  flex: none;
  color: var(--color-text-primary);
  font-weight: var(--weight-medium);
  font-variant-numeric: tabular-nums;
}
.src-pill-chev {
  margin-left: auto;
  color: var(--color-text-tertiary);
}
/* 跟随态：当前查看轮的组头高亮（accent 淡底+左缘条；与 panel-hit 面板级高亮分元素不冲突） */
.src-group-title.group-active {
  background: var(--color-accent-bg);
  box-shadow: inset 2px 0 0 var(--color-accent);
}
@media (max-width: 1200px) {
  .sources-panel {
    position: static;
  }
  .src-body {
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: var(--space-2);
  }
  /* 多列形态下去掉行间分隔线（卡间以间距分层） */
  .src-card.has-divider {
    border-top: 0;
    background: var(--color-surface);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-md);
  }
}
</style>
