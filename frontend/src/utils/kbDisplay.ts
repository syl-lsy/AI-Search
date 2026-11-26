/**
 * src/utils/kbDisplay.ts — KB 六态展示投影纯函数（plan-m3-b6-f6 §3.2/§3.4）。
 * 数据判据全部来自 api.md §4.3/§4.6 契约钉定形态；零网络、零组件依赖（unit 可测）。
 */
import type { KbDocStatus, KbParseMeta } from '@/types/api'

import { isHttpUrl } from './guards'

/** §4.3 阶段化 fail_reason 的阶段前缀值域（pending 期失败无前缀） */
const FAIL_STAGES = ['normalizing', 'chunking', 'indexing'] as const
export type FailStage = (typeof FAIL_STAGES)[number]

export interface ParsedFailReason {
  stage: FailStage | null
  detail: string
}

function isFailStage(value: string | undefined): value is FailStage {
  return value !== undefined && (FAIL_STAGES as readonly string[]).includes(value)
}

/**
 * §4.3 阶段化 fail_reason 解析：非空值形如 `{stage}: {detail}`（stage ∈ normalizing|chunking|indexing）；
 * pending 期失败无前缀 → `{ stage: null, detail: 原文整体 }`（防御回退，不解析 category 语义）。
 */
export function parseFailStage(failReason: string): ParsedFailReason {
  const matched = /^(normalizing|chunking|indexing): (.+)$/.exec(failReason)
  if (matched && isFailStage(matched[1])) {
    return { stage: matched[1], detail: matched[2] ?? '' }
  }
  return { stage: null, detail: failReason }
}

/** 设计稿 .st-trail 节点态：已过=done / 进行中=cur / 失败点=fail / 未达=todo */
export type StepperNodeState = 'done' | 'cur' | 'fail' | 'todo'

export interface StepperNode {
  key: (typeof STEPPER_KEYS)[number]
  state: StepperNodeState
}

/** §4.3 状态机主链四节点（pending 不入步进条——视图层 v-if 整条不渲染，由徽标承载） */
const STEPPER_KEYS = ['normalizing', 'chunking', 'indexing', 'ready'] as const

/**
 * 步进条节点投影（设计稿 st-trail：done/cur/fail/todo × 四节点）：
 * normalizing→cur；chunking→[done,cur]；indexing→[done,done,cur]；ready→全 done；
 * failed→按 failStage 定位 fail 节点（failStage=null〔pending 期失败〕→ 全 todo）；pending→全 todo（视图层不渲染）。
 */
export function stepperNodes(status: KbDocStatus, failStage: FailStage | null): StepperNode[] {
  const indexOf = (key: (typeof STEPPER_KEYS)[number]): number => STEPPER_KEYS.indexOf(key)
  let failIdx = -1
  let curIdx = -1
  if (status === 'ready') {
    return STEPPER_KEYS.map((key) => ({ key, state: 'done' as const }))
  }
  if (status === 'failed') {
    if (failStage === null) {
      return STEPPER_KEYS.map((key) => ({ key, state: 'todo' as const }))
    }
    failIdx = indexOf(failStage)
  } else if (status !== 'pending') {
    // normalizing/chunking/indexing 进行中：当前阶段节点=cur
    curIdx = indexOf(status)
  }
  return STEPPER_KEYS.map((key, i) => ({
    key,
    state: failIdx === i ? ('fail' as const) : curIdx === i ? ('cur' as const) : i < Math.max(failIdx, curIdx) ? ('done' as const) : ('todo' as const),
  }))
}

/** 解析摘要段键位（CR-4/P3-2：视图层经既有 i18n 键渲染——answer.kbParsePages/kbParseTables/kbParseCode） */
export type ParseMetaSegmentKey = 'pages' | 'tables' | 'code'

export interface ParseMetaSegment {
  key: ParseMetaSegmentKey
  n: number
}

/**
 * §4.3 解析摘要结构化段（api.md「null 段不渲染」：pages=null〔非页式〕只产表/码两段）；
 * parse_meta=null（normalizing 完成前）→ null（视图层渲染 —，设计稿 .pm-null）。
 * 纯结构零文案（util 层零语言依赖）——i18n 键映射归视图层（CR-4：LibraryView 解析摘要列与
 * KbChunkDrilldown chips 统一走既有 answer.kbParse* 键，en-US 下不再出现硬编码中文）。
 */
export function parseMetaSegments(meta: KbParseMeta | null | undefined): ParseMetaSegment[] | null {
  if (!meta) return null
  const segments: ParseMetaSegment[] = []
  if (meta.pages !== null) segments.push({ key: 'pages', n: meta.pages })
  segments.push({ key: 'tables', n: meta.tables })
  segments.push({ key: 'code', n: meta.code_blocks })
  return segments
}

/** §4.6 v1.31 回跳 URL 契约形态（api.md §4.6：「打开飞书原文」外链） */
const FEISHU_WIKI_BASE = 'https://www.feishu.cn/wiki/'

/**
 * 飞书回跳 URL 构造：node_token 存在 → URL；缺省/空串 → null（外链钮数据面不渲染——frontend-api.md §5 KbChunkDrilldown 行）。
 *
 * 安全边界如实注记（SA-1/P3-5）：本函数的安全边界 = **固定 https 基座**——scheme 与 authority
 * （https://www.feishu.cn）在 token 拼接前已闭合，node_token 只落入 path 段、无法改写 host/scheme。
 * 尾部 isHttpUrl 校验因此**恒真**，仅为纵深冗余保留（防后续重构误依赖——若有人改为可变基座拼接，
 * 该守卫才成为活防线）；不对 token 做字符集白名单（path 段拼接形态下无注入面）。
 */
export function feishuJumpUrl(nodeToken: string | undefined): string | null {
  if (!nodeToken) return null
  const url = FEISHU_WIKI_BASE + nodeToken
  return isHttpUrl(url) ? url : null
}
