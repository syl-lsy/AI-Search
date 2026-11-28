/**
 * src/constants/ui.ts — 纯前端 UI 阈值常量（配置三分法「代码常量」档：与环境无关的
 * 纯逻辑值，语义化命名收敛于此；有 Nacos 键的阈值不在此列，§配置三分法）。
 */

/** 长问题钳制阈值（修复项⑤，TurnBlock）：超此字符数或含换行符视为长问题，默认 3 行钳制 */
export const QUESTION_CLAMP_THRESHOLD = 120

/** 来源面板组标题截断长度（frontend-api.md §5 v1.21 / §5-U7：前端常量建议值 40） */
export const PANEL_GROUP_TITLE_MAX = 40

/** 触底自动加载提前量（px）：sentinel 进入视口前阈值即触发，减少等待感 */
export const INFINITE_SCROLL_ROOT_MARGIN = '160px'

/** 线程重命名长度上限（api.md §2.10 v1.18：title trim 后 1~120 字符——前端先校验） */
export const THREAD_TITLE_MAX_LENGTH = 120

/** 来源面板折叠胶囊圆标叠上限（Perplexity 对齐小批：跨组取前 3 个不同来源；设计说明 v1.7） */
export const SOURCE_PILL_AVATARS_MAX = 3

/**
 * z-index 层级表扩展位（B22 批次 3 通用 HITL 弹层壳 HitlSheet——组件架构用户裁决 2026-11-28）。
 * 既有层级：sb-backdrop/AnswerActions 菜单 30 < 抽屉 40 < **HITL 弹层壳 45** < ToastHost 50
 * < ThreadModal 60 < CommandPalette 65——弹层在抽屉之上（覆盖输入卡形态不与抽屉冲突），
 * 在 ToastHost 之下（409008/网络错 toast 恒浮于弹层之上可见）、ThreadModal 之下（将来
 * 预览点入下钻弹窗可叠）。
 */
export const HITL_SHEET_Z_INDEX = 45
