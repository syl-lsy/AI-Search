/**
 * src/constants/errors.ts — 错误码分支常量（配置三分法「代码常量」档：协议枚举值的
 * 前端消费面，语义化命名收敛于此；码表唯一事实源 = docs/contract/api.md §0 +
 * src/types/api.d.ts 镜像，本文件不得新增码值——只收敛前端分支判定用的具名引用）。
 */

/**
 * 内容安全拒绝（§0 域码 403002，B13 拒答轮收口）：
 *   - 传输层（turnStream）：error 事件携带本码 → 置「等待关闭」标记——流随后关闭且未见 done
 *     → 按终态收口（不重连；A13 断线重连逻辑在该情形豁免）；
 *   - store（turn）：错误条/卡温和分支 + 关闭收口时立即移出生成中集合；
 *   - 渲染（TurnBlock）：错误卡中性温和样式判定值。
 */
export const CONTENT_SAFETY_ERROR_CODE = '403002'
