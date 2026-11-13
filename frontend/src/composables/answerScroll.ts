/**
 * src/composables/answerScroll.ts — AnswerScrollController（frontend-api.md §5 v1.8，REQ-043 / A26）。
 *
 * 状态机（协议零变更：不新增 store 状态、不动 api.d.ts、不触网）：
 *   - following：流式输出期间新内容到达 → 自动贴底（距对话内容底部 < 40px 语义由「跟随态恒贴底」
 *     实现——跟随态只能经「用户上滚 >8px」退出，故跟随即贴底）；即时赋值 scrollTop（无平滑、无布局跳动）。
 *   - taken_over：用户上滚位移累计 > 8px（滚轮/触摸/拖滚动条任一——统一表现为 scrollTop 负向增量）
 *     → 立即接管；仅停止自动滚动，流式渲染照常（渲染节流管线独立于本控制器，REQ-043 工程约束）。
 *   - 恢复三途径：① 接管后用户主动下滚进入贴底区（距底 < 40px，以同一次 scroll 事件的
 *     正向位移 + 贴底联合判定——接管后视口停留或流式内容增长导致的距底变化不触发恢复，
 *     8px/40px 重叠带消歧）；② resumeViaFab（ScrollToBottomFab 点击，恢复 + 平滑滚底）；
 *     ③ notifyNewTurn（发送新消息 / 新一轮开始，恢复 + 即时贴底）。
 *   - 历史回看态（非流式）零自动滚动；ScrollToBottomFab 显隐（距底 > 约一视口高）与流式无关
 *     （REQ-045：流式生成中与回看历史均可用）。
 *   - scroll 监听 passive 化。
 *
 * 阈值为实现常量（REQ-043：40px / 8px 不进 Nacos）。
 */

/** 贴底判定阈值：距对话内容底部 < 40px（REQ-043，实现常量） */
export const FOLLOW_NEAR_BOTTOM_PX = 40
/** 用户接管阈值：向上滚动位移 > 8px（REQ-043，实现常量） */
export const TAKEOVER_UP_SCROLL_PX = 8

export type FollowPhase = 'following' | 'taken_over'

export interface AnswerScrollState {
  phase: FollowPhase
  showFab: boolean
}

/** 视口最小结构（HTMLElement 满足此形状；单测以纯字段替身驱动状态机） */
export interface ScrollViewportLike {
  scrollTop: number
  scrollHeight: number
  clientHeight: number
  addEventListener(
    type: string,
    listener: EventListenerOrEventListenerObject,
    options?: AddEventListenerOptions,
  ): void
  removeEventListener(
    type: string,
    listener: EventListenerOrEventListenerObject,
    options?: EventListenerOptions,
  ): void
  scrollTo(options?: { top?: number; behavior?: 'auto' | 'smooth' }): void
}

export class AnswerScrollController {
  phase: FollowPhase = 'following'
  showFab = false
  /** 供 Vue 层镜像 state 的回调（phase/showFab 变化时触发） */
  onStateChange: ((state: AnswerScrollState) => void) | null = null

  private readonly viewport: ScrollViewportLike
  private streaming = false
  private lastScrollTop = 0
  private upwardAccum = 0
  private readonly onScroll = (): void => this.handleScroll()

  constructor(viewport: ScrollViewportLike) {
    this.viewport = viewport
    this.lastScrollTop = viewport.scrollTop
    // REQ-043 工程约束：passive 化（不阻止滚动默认行为）
    viewport.addEventListener('scroll', this.onScroll, { passive: true })
  }

  /** 流式进行中（快速 token / 深度撰写阶段）= true；历史回看态 = false（零自动滚动） */
  setStreaming(active: boolean): void {
    this.streaming = active
  }

  /** 流式内容增长（answer_md/thinking_md 变更后由装配层在 nextTick 调用） */
  onContentGrew(): void {
    if (this.streaming && this.phase === 'following') {
      this.stickToBottom()
    } else {
      // 接管态/回看态：内容增长改变距底距离 → Fab 显隐复算（评审留档 P2：接管态内容增长
      // 原先只在 scroll 事件复算，增长导致的距底跨屏不显示 Fab——恢复途径②不可见）
      this.updateFab()
    }
  }

  /** 恢复途径②：ScrollToBottomFab 点击 → 恢复跟随 + 平滑滚底（REQ-045/REQ-043 联动） */
  resumeViaFab(): void {
    this.setFollowing()
    this.viewport.scrollTo({ top: this.viewport.scrollHeight, behavior: 'smooth' })
  }

  /** 恢复途径③：发送新消息 / 新一轮开始 → 恢复跟随 + 即时贴底 */
  notifyNewTurn(): void {
    this.setFollowing()
    this.stickToBottom()
  }

  /** 程序化赋值（等价用户拖滚动条）：不同步 lastScrollTop——用户滚动增量须经下一次
   *  scroll 事件被状态机观测（同步会使位移永远为 0）。 */
  scrollTopByUser(top: number): void {
    this.viewport.scrollTop = top
  }

  distanceFromBottom(): number {
    return Math.max(
      0,
      this.viewport.scrollHeight - this.viewport.scrollTop - this.viewport.clientHeight,
    )
  }

  detach(): void {
    this.viewport.removeEventListener('scroll', this.onScroll)
  }

  /** scroll 处理（passive）：用户位移判定 + Fab 显隐。接管态下的正向位移必为用户滚动
   *  （控制器仅在 following 态做程序化滚动），故「正向位移 && 贴底」即可判定恢复①。 */
  private handleScroll(): void {
    const top = this.viewport.scrollTop
    const delta = top - this.lastScrollTop
    this.lastScrollTop = top

    if (this.phase === 'following') {
      if (delta < 0) {
        this.upwardAccum += -delta
        if (this.upwardAccum > TAKEOVER_UP_SCROLL_PX) {
          this.phase = 'taken_over'
          this.upwardAccum = 0
          this.emit()
        }
      } else if (delta > 0) {
        // 向下位移（含程序化自动跟随）重置累计
        this.upwardAccum = 0
      }
    } else {
      // taken_over：仅认用户主动下滚进入贴底区（同一次 scroll 事件内联合判定）
      if (delta > 0 && this.distanceFromBottom() < FOLLOW_NEAR_BOTTOM_PX) {
        this.setFollowing()
      }
    }
    this.updateFab()
  }

  private setFollowing(): void {
    this.phase = 'following'
    this.upwardAccum = 0
    this.emit()
  }

  /** 自动跟随：直接赋值 scrollTop（即时、无平滑、无布局跳动；REQ-043 工程约束） */
  private stickToBottom(): void {
    this.viewport.scrollTop = this.viewport.scrollHeight
    this.lastScrollTop = this.viewport.scrollTop
    this.updateFab()
  }

  /** REQ-045：滚动位置距底部超过约一视口高度即显示（常驻），贴底消失 */
  private updateFab(): void {
    const next = this.distanceFromBottom() > this.viewport.clientHeight
    if (next !== this.showFab) {
      this.showFab = next
      this.emit()
    }
  }

  private emit(): void {
    this.onStateChange?.({ phase: this.phase, showFab: this.showFab })
  }
}
