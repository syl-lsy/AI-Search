/**
 * test/component/hitl-sheet.test.ts — HitlSheet 通用 HITL 弹层壳（B22 批次 3 第二段组件架构：
 * 通用壳+业务皮，用户 2026-11-28 裁决——本壳业务无关，未来 HITL 场景（M5 记忆审批等）直接复用；
 * ApprovalSheet 为 KB 门首个业务实例，经 slots 注入内容）。
 *
 * 壳职责全部业务无关：
 *   - 底部 sheet 覆盖层：Teleport body（避容器裁剪，ModelSelect v1.24 先例）+ fixed 定位
 *     （锚点元素上沿、遮住输入卡——锚点经 anchorEl prop 注入；覆盖层不改宿主布局）
 *   - z-index 层级表扩展位（constants/ui.ts HITL_SHEET_Z_INDEX=45：drawer(40) 之上、
 *     ToastHost(50) 之下——toast 恒浮于弹层之上）
 *   - 倒计时机制（expiresAt prop 为准、零本地超时动作——超点等服务端收口帧）
 *   - 错误态机制（errorText prop → role=alert 区块）
 *   - 关闭确认机制（×/cancelLabel 弱钮 → 确认区 → emit close；Esc 仅撤回确认不关弹层）
 *   - 内容经 preview/choices/note 三 slot 注入（壳内零业务语义——文案全部来自 props）
 *   - a11y：焦点管理（open 即落面板含挂载即 open 形态/关闭双相位返还）/aria/键盘
 */
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import HitlSheet from '@/components/common/HitlSheet.vue'
import HitlSheetSource from '@/components/common/HitlSheet.vue?raw'
import { HITL_SHEET_Z_INDEX } from '@/constants/ui'

const EXPIRES_AT = '2026-11-01T08:14:06Z' // 08:12:06 + 120s（seed 系统时间 08:12:06）

/** 壳测试夹具文案全部来自 props（零内置业务词——壳内零业务语义的负向断言依据） */
function mountSheet(overrides: Record<string, unknown> = {}, slots: Record<string, string> = {}) {
  return mount(HitlSheet, {
    props: {
      open: true,
      anchorEl: null,
      title: '测试弹层标题',
      icon: 'i-db',
      expiresAt: EXPIRES_AT,
      timeoutHintText: '即将按测试缺省继续',
      cancelLabel: '测试取消钮',
      closeConfirmTitle: '确认关闭？',
      closeConfirmBody: '关闭后内容不保留',
      closeConfirmOkLabel: '确认关闭',
      closeConfirmStayLabel: '继续等待',
      ...overrides,
    },
    slots: {
      preview: '<p data-testid="slot-preview">预览内容</p>',
      choices: '<button type="button" data-testid="slot-choice">选项</button>',
      note: '<p data-testid="slot-note">备注内容</p>',
      ...slots,
    },
  })
}

const panel = (): HTMLElement | null => document.querySelector<HTMLElement>('[data-testid="hitl-sheet"]')
const confirmBox = (): Element | null => document.querySelector('[data-testid="hitl-close-confirm"]')

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-11-01T08:12:06Z'))
  document.body.innerHTML = ''
})

afterEach(() => {
  vi.useRealTimers()
})

describe('渲染（Teleport body + 底部覆盖层 + 通用层级）', () => {
  it('open=true：Teleport 到 body 渲染 role=dialog 面板（aria-label=title、aria-modal=false 非模态、position:fixed）+ z-index=HITL_SHEET_Z_INDEX 层级表扩展位（drawer(40) 之上、ToastHost(50) 之下）', async () => {
    const w = mountSheet()
    await flushPromises()
    const p = panel()
    expect(p).not.toBeNull()
    expect(w.find('[data-testid="hitl-sheet"]').exists()).toBe(false) // Teleport body：不在 wrapper 内
    expect(document.body.contains(p!)).toBe(true)
    expect(p!.getAttribute('role')).toBe('dialog')
    expect(p!.getAttribute('aria-label')).toBe('测试弹层标题')
    expect(p!.getAttribute('aria-modal')).toBe('false')
    expect(p!.querySelector('.hs-ico')).not.toBeNull() // 装饰图标（aria-hidden，业务注入）
    expect(p!.style.position).toBe('fixed')
    expect(p!.style.zIndex).toBe(String(HITL_SHEET_Z_INDEX))
    expect(p!.style.bottom).toBe('0px') // DOM 内联样式规范化补 px
    w.unmount()
  })

  it('open=false：整块不渲染（Teleport 目标零节点）——可见性由业务方状态派生驱动', async () => {
    const w = mountSheet({ open: false })
    await flushPromises()
    expect(panel()).toBeNull()
    w.unmount()
  })

  it('anchorEl 注入：面板顶边=锚点元素 getBoundingClientRect().top（遮住输入卡形态）；anchorEl=null 退化为贴视口底（无 top 内联样式）', async () => {
    const anchor = document.createElement('div')
    anchor.id = 'test-anchor'
    document.body.appendChild(anchor)
    vi.spyOn(anchor, 'getBoundingClientRect').mockReturnValue({
      top: 480, bottom: 600, left: 0, right: 800, width: 800, height: 120,
      x: 0, y: 480, toJSON: () => ({}),
    } as DOMRect)
    const w = mountSheet({ anchorEl: anchor })
    await flushPromises()
    expect(panel()!.style.top).toBe('480px')
    w.unmount()

    const w2 = mountSheet({ anchorEl: null })
    await flushPromises()
    expect(panel()!.style.top).toBe('')
    w2.unmount()
  })

  it('三 slot 注入渲染（preview/choices/note——壳内零业务语义，内容全由业务方组装）', async () => {
    const w = mountSheet()
    await flushPromises()
    expect(document.querySelector('[data-testid="slot-preview"]')).not.toBeNull()
    expect(document.querySelector('[data-testid="slot-choice"]')).not.toBeNull()
    expect(document.querySelector('[data-testid="slot-note"]')).not.toBeNull()
    w.unmount()
  })

  it('壳内零业务语义：不传业务 props（cancelLabel=null → 弱取消钮不渲染；title 为唯一业务文案且纯 props 驱动）', async () => {
    const w = mountSheet({ cancelLabel: null })
    await flushPromises()
    expect(document.querySelector('[data-testid="hitl-cancel"]')).toBeNull()
    // 壳内除 title/确认文案/提示文案外无任何内置业务文本（负向：面板不含「知识库」等 KB 词——业务词只可能来自 slot/props）
    expect(panel()!.textContent).not.toContain('知识库')
    w.unmount()
  })
})

describe('倒计时（expiresAt prop 为准——零本地超时动作）', () => {
  it('剩余 >15s 无提示；推进至剩 14s 显示 timeoutHintText（role=status）；超过点提示隐藏且零本地动作（零 emit close、open 不变）', async () => {
    const w = mountSheet()
    await flushPromises()
    expect(document.querySelector('[data-testid="hitl-timeout-hint"]')).toBeNull() // 剩 120s >15
    vi.setSystemTime(new Date('2026-11-01T08:13:52Z')) // 剩 14s
    await flushPromises()
    vi.advanceTimersByTime(1100) // 驱动 tick interval
    await flushPromises()
    const hint = document.querySelector('[data-testid="hitl-timeout-hint"]')
    expect(hint).not.toBeNull()
    expect(hint!.getAttribute('role')).toBe('status')
    expect(hint!.textContent).toContain('即将按测试缺省继续')
    vi.setSystemTime(new Date('2026-11-01T08:14:10Z')) // 超过点 4s
    await flushPromises()
    vi.advanceTimersByTime(1100)
    await flushPromises()
    expect(document.querySelector('[data-testid="hitl-timeout-hint"]')).toBeNull()
    // 零本地超时动作：不 emit close（判定权在服务端）——w.emitted 全程记录
    expect(w.emitted('close')).toBeUndefined()
    expect(panel()).not.toBeNull()
    w.unmount()
  })

  it('expiresAt=null：任何时刻无倒计时提示（业务方可关停倒计时面）', async () => {
    const w = mountSheet({ expiresAt: null })
    await flushPromises()
    vi.setSystemTime(new Date('2026-11-01T08:14:00Z')) // 虚拟「剩 6s」窗
    await flushPromises()
    vi.advanceTimersByTime(1100)
    await flushPromises()
    expect(document.querySelector('[data-testid="hitl-timeout-hint"]')).toBeNull()
    w.unmount()
  })
})

describe('关闭确认（关闭=canceled(user) 确认机制——业务无关）', () => {
  it('点 × → 确认区出现（零 emit close）；「继续等待」撤回（确认消失、零 emit）；「确认关闭」→ emit close 一次', async () => {
    const w = mountSheet()
    await flushPromises()
    document.querySelector<HTMLButtonElement>('[data-testid="hitl-close"]')!.click()
    await flushPromises()
    expect(confirmBox()).not.toBeNull() // 确认区出现
    expect(panel()!.textContent).toContain('关闭后内容不保留')
    expect(w.emitted('close')).toBeUndefined() // 确认前零 emit

    document.querySelector<HTMLButtonElement>('[data-testid="hitl-confirm-stay"]')!.click()
    await flushPromises()
    expect(confirmBox()).toBeNull()
    expect(w.emitted('close')).toBeUndefined()

    document.querySelector<HTMLButtonElement>('[data-testid="hitl-close"]')!.click()
    await flushPromises()
    document.querySelector<HTMLButtonElement>('[data-testid="hitl-confirm-close"]')!.click()
    await flushPromises()
    expect(w.emitted('close')).toHaveLength(1)
    expect(confirmBox()).toBeNull() // 确认态复位
    w.unmount()
  })

  it('cancelLabel 弱钮同入口：点击 → 确认区出现 → 确认 → emit close（两关闭入口统一走确认机制）', async () => {
    const w = mountSheet()
    await flushPromises()
    document.querySelector<HTMLButtonElement>('[data-testid="hitl-cancel"]')!.click()
    await flushPromises()
    expect(confirmBox()).not.toBeNull()
    document.querySelector<HTMLButtonElement>('[data-testid="hitl-confirm-close"]')!.click()
    await flushPromises()
    expect(w.emitted('close')).toHaveLength(1)
    w.unmount()
  })

  it('确认态 Esc → 仅撤回确认（确认消失、零 emit close）；非确认态 Esc → 零副作用（零 emit、面板保留）', async () => {
    const w = mountSheet()
    await flushPromises()
    const p = panel()!
    p.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()
    expect(confirmBox()).toBeNull()
    expect(w.emitted('close')).toBeUndefined()

    document.querySelector<HTMLButtonElement>('[data-testid="hitl-close"]')!.click()
    await flushPromises()
    expect(confirmBox()).not.toBeNull()
    p.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()
    expect(confirmBox()).toBeNull() // Esc 仅撤回确认
    expect(w.emitted('close')).toBeUndefined() // 不关弹层
    expect(panel()).not.toBeNull()
    w.unmount()
  })

  it('busy=true：× 与弱取消钮 disabled（提交中关闭入口冻结防误触）', async () => {
    const w = mountSheet({ busy: true })
    await flushPromises()
    expect(document.querySelector<HTMLButtonElement>('[data-testid="hitl-close"]')!.disabled).toBe(true)
    expect(document.querySelector<HTMLButtonElement>('[data-testid="hitl-cancel"]')!.disabled).toBe(true)
    w.unmount()
  })
})

describe('错误态（errorText prop——业务无关机制）', () => {
  it('errorText 非空 → role=alert 区块渲染文本；null → 不渲染', async () => {
    const w = mountSheet({ errorText: '该轮已不在待审批状态' })
    await flushPromises()
    const err = document.querySelector('[data-testid="hitl-error"]')
    expect(err).not.toBeNull()
    expect(err!.getAttribute('role')).toBe('alert')
    expect(err!.textContent).toContain('该轮已不在待审批状态')
    w.unmount()

    const w2 = mountSheet({ errorText: null })
    await flushPromises()
    expect(document.querySelector('[data-testid="hitl-error"]')).toBeNull()
    w2.unmount()
  })
})

describe('壳纯度负向断言（批次 3 评审 M3 用户点名专审项：壳内零业务语义——未来 HITL 场景直接复用）', () => {
  /** SFC 源文（Vite ?raw 导入——模板/脚本块逐字检查，样式块单独查 token 引用） */
  const source: string = HitlSheetSource
  const scriptBlock = source.slice(source.indexOf('<script'), source.indexOf('</script>') + '</script>'.length)
  const templateBlock = source.slice(source.indexOf('<template>'), source.indexOf('</template>') + '</template>'.length)

  it('脚本与模板零 kb 字样业务引用（大小写不敏感——业务词只允许经 props/slot 注入，注释亦不豁免）', () => {
    expect(scriptBlock).not.toMatch(/kb/i)
    expect(templateBlock).not.toMatch(/kb/i)
  })

  it('图标底色/前景走通用 token：.hs-ico 用 --color-accent-bg/--color-accent（禁 --color-kb* 业务色渗入通用壳）', () => {
    const icoRule = source.match(/\.hs-ico\s*{[^}]*}/)![0]!
    expect(icoRule).toContain('var(--color-accent-bg)')
    expect(icoRule).toContain('var(--color-accent)')
    expect(source).not.toContain('--color-kb')
  })

  it('面板 ref 中性命名（sheetPanel——非业务轮次词，壳可复用于非轮次场景）', () => {
    expect(source).not.toContain('turnPanel')
    expect(source).toContain('sheetPanel')
  })
})

describe('可访问性（焦点管理——挂载即 open 形态与关闭返还）', () => {
  it('挂载即 open：焦点落面板（tabindex=-1 承接）；open 翻 false：焦点双相位返还此前聚焦元素', async () => {
    const trigger = document.createElement('button')
    trigger.id = 'focus-origin'
    document.body.appendChild(trigger)
    trigger.focus()
    expect(document.activeElement).toBe(trigger)

    const w = mountSheet()
    await flushPromises()
    expect(document.activeElement).toBe(panel()) // open 即落面板（含挂载时已 open——immediate watch）

    await w.setProps({ open: false })
    await flushPromises()
    expect(document.activeElement).toBe(trigger) // 双相位返还
    expect(panel()).toBeNull()
    w.unmount()
  })
})
