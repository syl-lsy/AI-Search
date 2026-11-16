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
