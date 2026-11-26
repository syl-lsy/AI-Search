/**
 * test/component/source-panel.test.ts — SourcePanel 组件层（§5 SourcePanel 行 v1.21 分组语义）。
 *
 * 【v1.21 线程级累计分组语义，REQ-061/A45】props = groups: Array<{turnId, question,
 * sources, collecting}>——按轮分组渲染、组标题=『question 截断 ~40 字符』的搜索结果、
 * 面板头总数=Σ各组、collecting 徽标只挂当前收集组组头、组容器 data-turn-group={turnId}
 * （[n] 组作用域定位锚点——修复全局 querySelector 命中首组同号卡缺陷）。
 * 契约保留面：多行卡形态（v1.5）、data-source-card=ref_no、cite-hit 类、快照行尾小图标、
 * 【网络】/【知识库】徽标、highlightSeq panel-hit 2s 高亮。
 */
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import SourcePanel, { type SourcePanelGroup } from '@/components/answer/SourcePanel.vue'
import { i18n } from '@/i18n'
import type { Source } from '@/types/api'

function makeSource(partial: Partial<Source> & { source_id: string; ref_no: number }): Source {
  return {
    source_type: 'web',
    title: 'LangGraph docs',
    url: `https://docs.example.com/${partial.ref_no}`,
    snippet: 'Send lets you fan out dynamically; Router picks a static branch…',
    score: 0.9,
    snapshot_available: false,
    kb_doc_id: null,
    ...partial,
  }
}

const TURN_1 = '11111111-1111-4111-8111-111111111111'
const TURN_2 = '22222222-2222-4222-8222-222222222222'

function group(partial: Partial<SourcePanelGroup> & { turnId: string }): SourcePanelGroup {
  return {
    question: '第一轮的问题',
    sources: [],
    collecting: false,
    ...partial,
  }
}

function mountPanel(
  groups: SourcePanelGroup[],
  highlightSeq = 0,
  activeTurnId: string | null = null,
  expandSeq = 0,
) {
  return mount(SourcePanel, {
    props: { groups, highlightSeq, activeTurnId, expandSeq },
    global: { plugins: [i18n] },
  })
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('SourcePanel 分组语义（v1.21 REQ-061/A45）', () => {
  it('两轮各带来源 → 两组渲染（data-turn-group 容器 + 组标题=『question』的搜索结果）', () => {
    const w = mountPanel([
      group({ turnId: TURN_1, question: 'Send API 的用法', sources: [makeSource({ source_id: 'a', ref_no: 1 })] }),
      group({ turnId: TURN_2, question: 'Router 的取舍', sources: [makeSource({ source_id: 'b', ref_no: 1 })] }),
    ])
    const groupsEls = w.findAll('.src-group')
    expect(groupsEls).toHaveLength(2)
    expect(groupsEls[0]!.attributes('data-turn-group')).toBe(TURN_1)
    expect(groupsEls[1]!.attributes('data-turn-group')).toBe(TURN_2)
    expect(groupsEls[0]!.find('.src-group-title').text()).toContain('『Send API 的用法』的搜索结果')
    expect(groupsEls[1]!.find('.src-group-title').text()).toContain('『Router 的取舍』的搜索结果')
  })

  it('组标题截断：超 40 字符的 question 截断加省略号（PANEL_GROUP_TITLE_MAX=40）', () => {
    const long = '这是一个非常长的提问'.repeat(5) // 50 字符
    const w = mountPanel([group({ turnId: TURN_1, question: long, sources: [makeSource({ source_id: 'a', ref_no: 1 })] })])
    const title = w.find('.src-group-title').text()
    expect(title).toContain(long.slice(0, 40))
    expect(title).toContain('…')
    expect(title).not.toContain(long)
  })

  it('面板头总数 = Σ 各组 sources.length', () => {
    const w = mountPanel([
      group({ turnId: TURN_1, sources: [makeSource({ source_id: 'a', ref_no: 1 }), makeSource({ source_id: 'b', ref_no: 2 })] }),
      group({ turnId: TURN_2, sources: [makeSource({ source_id: 'c', ref_no: 1 })] }),
    ])
    expect(w.find('.count').text()).toBe('3')
  })

  it('「收集中」徽标只挂当前收集组的组头（历史组无徽标）', () => {
    const w = mountPanel([
      group({ turnId: TURN_1, sources: [makeSource({ source_id: 'a', ref_no: 1 })] }),
      group({ turnId: TURN_2, sources: [makeSource({ source_id: 'b', ref_no: 1 })], collecting: true }),
    ])
    const heads = w.findAll('.src-group-title')
    expect(heads[0]!.text()).not.toContain('收集中')
    expect(heads[1]!.text()).toContain('收集中')
    // 面板头不挂徽标（v1.21：徽标从面板头迁移到组头）
    expect(w.find('.src-panel-head').text()).not.toContain('收集中')
  })

  it('[n] 组作用域：两轮各含 ref_no=1 卡——各自组容器内独立锚点（跨组同号不冲突）', () => {
    const w = mountPanel([
      group({ turnId: TURN_1, sources: [makeSource({ source_id: 'a', ref_no: 1 }), makeSource({ source_id: 'a2', ref_no: 2 })] }),
      group({ turnId: TURN_2, sources: [makeSource({ source_id: 'b', ref_no: 1 })] }),
    ])
    // 组作用域选择器各命中自己的 1 号卡（AnswerView onCite(turnId, refNo) 的查询形态）
    const g1 = w.find(`[data-turn-group="${TURN_1}"]`)
    const g2 = w.find(`[data-turn-group="${TURN_2}"]`)
    expect(g1.find('[data-source-card="1"]').exists()).toBe(true)
    expect(g2.find('[data-source-card="1"]').exists()).toBe(true)
    expect(g1.findAll('.src-card')).toHaveLength(2)
    expect(g2.findAll('.src-card')).toHaveLength(1)
  })

  it('追问后新组追加、旧组保留（聚合态：三组时序渲染）', () => {
    const w = mountPanel([
      group({ turnId: TURN_1, sources: [makeSource({ source_id: 'a', ref_no: 1 })] }),
      group({ turnId: '33333333-3333-4333-8333-333333333333', sources: [makeSource({ source_id: 'b', ref_no: 1 })] }),
      group({ turnId: TURN_2, sources: [makeSource({ source_id: 'c', ref_no: 1 })], collecting: true }),
    ])
    expect(w.findAll('.src-group')).toHaveLength(3)
    // DOM 序 = 轮次序
    expect(w.findAll('.src-group').map((g) => g.attributes('data-turn-group'))).toEqual([
      TURN_1,
      '33333333-3333-4333-8333-333333333333',
      TURN_2,
    ])
  })

  it('direct 轮无组：groups 为空数组时整块不渲染（无空组占位——父级同口径）', () => {
    const w = mountPanel([])
    expect(w.find('[data-testid="source-panel"]').exists()).toBe(false)
  })
})

describe('SourcePanel 契约保留面（多行卡形态 v1.5 / [n] 锚点 / 快照 / 高亮）', () => {
  it('多行卡结构：favicon 首字母圆标 + 域名 + [n] 序号 + 标题 + 摘要块（组内）', () => {
    const w = mountPanel([
      group({
        turnId: TURN_1,
        sources: [makeSource({ source_id: 'a', ref_no: 1, title: 'LangGraph 官方文档 — Send API 指南' })],
      }),
    ])
    const card = w.find('.src-card')
    expect(card.find('.src-fav').text()).toBe('D')
    expect(card.find('.src-num').text()).toBe('1')
    expect(card.find('.src-domain').text()).toContain('docs.example.com')
    expect(card.find('.src-title').text()).toContain('LangGraph 官方文档 — Send API 指南')
    expect(card.find('.src-snip').text()).toContain('Send lets you fan out')
  })

  it('KB 来源 favicon 兜底 = kb_doc_id 短标识首字符；类型徽标 aria-label 语义', () => {
    const w = mountPanel([
      group({
        turnId: TURN_1,
        sources: [
          makeSource({ source_id: 'web1', ref_no: 1, source_type: 'web' }),
          makeSource({ source_id: 'kb1', ref_no: 2, source_type: 'kb', url: null, kb_doc_id: '9a770003-0000-4000-8000-000000000003' }),
        ],
      }),
    ])
    const cards = w.findAll('.src-card')
    expect(cards[1]!.find('.src-fav').text()).toBe('9')
    expect(cards[0]!.find('.src-type').attributes('aria-label')).toBe('网络')
    expect(cards[1]!.find('.src-type').attributes('aria-label')).toBe('知识库')
    expect(cards[1]!.find('.src-type').classes()).toContain('kb')
  })

  it('快照 = 行尾小图标：仅 snapshot_available 渲染；点击上抛 snapshot(source_id)', async () => {
    const w = mountPanel([
      group({
        turnId: TURN_1,
        sources: [
          makeSource({ source_id: 'plain', ref_no: 1, snapshot_available: false }),
          makeSource({ source_id: 'snap', ref_no: 2, snapshot_available: true }),
        ],
      }),
    ])
    expect(w.find('[data-testid="snapshot-1"]').exists()).toBe(false)
    const snapIcon = w.find('[data-testid="snapshot-2"]')
    expect(snapIcon.exists()).toBe(true)
    await snapIcon.trigger('click')
    expect(w.emitted('snapshot')).toEqual([['snap']])
  })

  it('组内分隔线（相邻兄弟规则）：组内第 2 卡起 has-divider；组间分隔由组容器承担', () => {
    const w = mountPanel([
      group({ turnId: TURN_1, sources: [makeSource({ source_id: 'a', ref_no: 1 }), makeSource({ source_id: 'b', ref_no: 2 })] }),
      group({ turnId: TURN_2, sources: [makeSource({ source_id: 'c', ref_no: 1 })] }),
    ])
    const cards = w.findAll('.src-card')
    expect(cards[0]!.classes()).not.toContain('has-divider')
    expect(cards[1]!.classes()).toContain('has-divider')
    // 组间分隔：非首组容器带 has-divider
    expect(w.findAll('.src-group')[1]!.classes()).toContain('has-divider')
  })

  it('卡片携带 data-source-card=ref_no + cite-hit 类注入锚点保留', () => {
    const w = mountPanel([group({ turnId: TURN_1, sources: [makeSource({ source_id: 'a', ref_no: 3 })] })])
    const card = w.find('[data-source-card="3"]')
    expect(card.exists()).toBe(true)
    card.element.classList.add('cite-hit')
    expect(card.classes()).toContain('cite-hit')
  })

  it('面板折叠切换（双形态）+ 来源定位高亮态（highlightSeq → panel-hit 2s）', async () => {
    const w = mountPanel([group({ turnId: TURN_1, sources: [makeSource({ source_id: 'a', ref_no: 1 })] })])
    const head = w.find('.src-panel-head')
    expect(head.attributes('aria-expanded')).toBe('true')
    await head.trigger('click')
    // 双形态：折叠后框架整体替换为胶囊（不再是 v-show 藏体）——控制钮=胶囊，aria-expanded=false
    expect(w.find('.src-panel-head').exists()).toBe(false)
    const pill = w.find('[data-testid="source-pill"]')
    expect(pill.exists()).toBe(true)
    expect(pill.attributes('aria-expanded')).toBe('false')
    await pill.trigger('click')
    expect(w.find('.src-panel-head').attributes('aria-expanded')).toBe('true')
    // highlightSeq 高亮态（根 aside 不随形态切换卸载）
    expect(w.find('[data-testid="source-panel"]').classes()).not.toContain('panel-hit')
    await w.setProps({ highlightSeq: 1 })
    expect(w.find('[data-testid="source-panel"]').classes()).toContain('panel-hit')
    vi.advanceTimersByTime(2_000)
    await nextTick()
    expect(w.find('[data-testid="source-panel"]').classes()).not.toContain('panel-hit')
  })
})

describe('SourcePanel 折叠胶囊（Perplexity 对齐小批：设计说明 v1.7）', () => {
  /** 两轮五源：前 3 个不同来源（source_id 去重）= a/a2/b，首字母 A/A/B——证明去重键=source_id 而非首字母 */
  function pillFix(): SourcePanelGroup[] {
    return [
      group({
        turnId: TURN_1,
        sources: [
          makeSource({ source_id: 'a', ref_no: 1, url: 'https://alpha.example.com/1' }),
          makeSource({ source_id: 'a2', ref_no: 2, url: 'https://alpha2.example.com/2' }),
        ],
      }),
      group({
        turnId: TURN_2,
        sources: [
          makeSource({ source_id: 'b', ref_no: 1, url: 'https://beta.example.com/1' }),
          makeSource({ source_id: 'c', ref_no: 2, url: 'https://gamma.example.com/2' }),
          makeSource({ source_id: 'd', ref_no: 3, url: 'https://delta.example.com/3' }),
        ],
      }),
    ]
  }

  it('默认展开：open 初始=true 渲染面板框架、不渲染胶囊（对标 Perplexity 默认态）', () => {
    const w = mountPanel(pillFix())
    expect(w.find('.src-frame').exists()).toBe(true)
    expect(w.find('[data-testid="source-pill"]').exists()).toBe(false)
  })

  it('胶囊内容：来源名 + 圆标叠（跨组前 3 个不同来源，source_id 去重）+ Σ 总数 + › 展开箭头', async () => {
    const w = mountPanel(pillFix())
    await w.find('.src-panel-head').trigger('click')
    const pillBtn = w.find('[data-testid="source-pill"]')
    expect(pillBtn.exists()).toBe(true)
    // 框架被替换（双形态渲染——折叠态不再占 max-height）
    expect(w.find('.src-frame').exists()).toBe(false)
    expect(pillBtn.text()).toContain('来源')
    // 圆标叠=3：source_id 去重取前 3（a/a2/b——首字母同为 A 不再去重）
    const chips = pillBtn.findAll('.src-pill-chip')
    expect(chips).toHaveLength(3)
    expect(chips.map((c) => c.text())).toEqual(['A', 'A', 'B'])
    // Σ 总数与面板头同口径（=Σ 各组 sources.length=5）
    expect(pillBtn.find('.src-pill-count').text()).toBe('5')
    // › 展开箭头（i-chev-r）——与面板头 i-chev-d 区分
    expect(pillBtn.find('use[href="#i-chev-r"]').exists()).toBe(true)
  })

  it('来源不足 3 个：圆标叠取全部（单来源=1 圆标，Σ 同步）', async () => {
    const w = mountPanel([
      group({ turnId: TURN_1, sources: [makeSource({ source_id: 'only', ref_no: 1, url: 'https://only.example.com/1' })] }),
    ])
    await w.find('.src-panel-head').trigger('click')
    const chips = w.findAll('[data-testid="source-pill"] .src-pill-chip')
    expect(chips).toHaveLength(1)
    expect(chips[0]!.text()).toBe('O')
    expect(w.find('.src-pill-count').text()).toBe('1')
  })

  it('点胶囊 → 回面板框架（往返闭环）', async () => {
    const w = mountPanel(pillFix())
    await w.find('.src-panel-head').trigger('click')
    await w.find('[data-testid="source-pill"]').trigger('click')
    expect(w.find('.src-frame').exists()).toBe(true)
    expect(w.find('[data-testid="source-pill"]').exists()).toBe(false)
    expect(w.find('.src-panel-head').attributes('aria-expanded')).toBe('true')
  })
})

describe('SourcePanel 跟随当前查看轮（activeTurnId prop；Perplexity 对齐小批）', () => {
  /** jsdom 无 scrollIntoView：原型补桩记录调用目标（面板内组容器定位断言面） */
  let targets: Element[]
  function stubScroll(): void {
    targets = []
    const stub = vi.fn(function (this: Element) {
      targets.push(this)
    })
    Element.prototype.scrollIntoView = stub as typeof Element.prototype.scrollIntoView
  }

  function followFix(): SourcePanelGroup[] {
    return [
      group({ turnId: TURN_1, sources: [makeSource({ source_id: 'a', ref_no: 1 })] }),
      group({ turnId: TURN_2, sources: [makeSource({ source_id: 'b', ref_no: 1 })] }),
    ]
  }

  afterEach(() => {
    Reflect.deleteProperty(Element.prototype, 'scrollIntoView')
  })

  it('activeTurnId 变化 → 该轮组容器 scrollIntoView({block:nearest}) + 组头 group-active 高亮（仅变化触发）', async () => {
    stubScroll()
    const w = mountPanel(followFix())
    expect(w.findAll('.group-active')).toHaveLength(0)
    await w.setProps({ activeTurnId: TURN_2 })
    const titles = w.findAll('.src-group-title')
    expect(titles[0]!.classes()).not.toContain('group-active')
    expect(titles[1]!.classes()).toContain('group-active')
    expect(targets).toHaveLength(1)
    expect(targets[0]!.getAttribute('data-turn-group')).toBe(TURN_2)
    expect(vi.mocked(Element.prototype.scrollIntoView).mock.calls[0]![0]).toEqual({ block: 'nearest' })
    // 同值重复 setProps 不再滚动（仅变化时触发——不与用户手动滚面板打架）
    await w.setProps({ activeTurnId: TURN_2 })
    expect(targets).toHaveLength(1)
  })

  it('回看旧轮再切换：第 2 次变化仍触发定位与高亮迁移（TURN_1）', async () => {
    stubScroll()
    const w = mountPanel(followFix())
    await w.setProps({ activeTurnId: TURN_2 })
    await w.setProps({ activeTurnId: TURN_1 })
    expect(targets).toHaveLength(2)
    expect(targets[1]!.getAttribute('data-turn-group')).toBe(TURN_1)
    expect(w.findAll('.src-group-title')[0]!.classes()).toContain('group-active')
    expect(w.findAll('.src-group-title')[1]!.classes()).not.toContain('group-active')
  })

  it('折叠态（胶囊）不跟随：折叠后 activeTurnId 变化零 scrollIntoView；展开回框架高亮按 prop 声明式呈现', async () => {
    stubScroll()
    const w = mountPanel(followFix())
    await w.find('.src-panel-head').trigger('click')
    expect(w.find('[data-testid="source-pill"]').exists()).toBe(true)
    await w.setProps({ activeTurnId: TURN_2 })
    expect(targets).toHaveLength(0)
    // 展开回框架：高亮按当前 prop 声明式呈现；变化已消费、无新增滚动
    await w.find('[data-testid="source-pill"]').trigger('click')
    expect(w.findAll('.src-group-title')[1]!.classes()).toContain('group-active')
    expect(targets).toHaveLength(0)
  })

  it('非法 turnId（非 UUID）：守卫静默——零 scrollIntoView、无高亮（选择器拼接前收窄，与 onCite 同款纵深防御）', async () => {
    stubScroll()
    const w = mountPanel(followFix())
    await w.setProps({ activeTurnId: 'not-a-uuid' })
    expect(targets).toHaveLength(0)
    expect(w.findAll('.group-active')).toHaveLength(0)
  })
})


describe('折叠态联动与焦点返还（评审 P1/P2-1/P2-2/P3-3：折叠胶囊不回退 REQ-003/A40 契约）', () => {
  /** 本 describe 自带 fixture/桩（上一 describe 的 followFix/stubScroll 为其闭包私有） */
  let targets2: Element[]
  function stubScroll2(): void {
    targets2 = []
    const stub = vi.fn(function (this: Element) {
      targets2.push(this)
    })
    Element.prototype.scrollIntoView = stub as typeof Element.prototype.scrollIntoView
  }
  function followFix2(): SourcePanelGroup[] {
    return [
      group({ turnId: TURN_1, sources: [makeSource({ source_id: 'a', ref_no: 1 })] }),
      group({ turnId: TURN_2, sources: [makeSource({ source_id: 'b', ref_no: 1 })] }),
    ]
  }

  afterEach(() => {
    Reflect.deleteProperty(Element.prototype, 'scrollIntoView')
  })

  it('expandSeq 自增 → 折叠胶囊回面板框架（外部强制展开，与 highlightSeq 同构；初始 0 不误触）', async () => {
    const w = mountPanel(followFix2())
    expect(w.find('.src-frame').exists()).toBe(true)
    await w.find('.src-panel-head').trigger('click')
    expect(w.find('[data-testid="source-pill"]').exists()).toBe(true)
    await w.setProps({ expandSeq: 1 })
    expect(w.find('.src-frame').exists()).toBe(true)
    expect(w.find('[data-testid="source-pill"]').exists()).toBe(false)
  })

  it('窄屏（≤1200px）跳过跟随滚动：scrollIntoView 零调用、组头高亮仍声明式呈现（P2-2）', async () => {
    vi.stubGlobal(
      'matchMedia',
      vi.fn().mockImplementation((query: string) => ({
        matches: query.includes('max-width: 1200px'),
        media: query,
        onchange: null,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
        addListener: () => undefined,
        removeListener: () => undefined,
      })),
    )
    stubScroll2()
    const w = mountPanel(followFix2())
    await w.setProps({ activeTurnId: TURN_2 })
    expect(targets2).toHaveLength(0)
    expect(w.findAll('.src-group-title')[1]!.classes()).toContain('group-active')
    vi.unstubAllGlobals()
  })

  it('双形态切换焦点返还（P3-3）：折到胶囊焦点落胶囊钮，展回框架焦点落面板头', async () => {
    const w = mount(SourcePanel, {
      attachTo: document.body,
      props: { groups: followFix2() },
      global: { plugins: [i18n] },
    })
    try {
      await w.find('.src-panel-head').trigger('click')
      expect(document.activeElement).toBe(w.find('[data-testid="source-pill"]').element)
      await w.find('[data-testid="source-pill"]').trigger('click')
      expect(document.activeElement).toBe(w.find('.src-panel-head').element)
    } finally {
      w.unmount()
    }
  })
})

describe('SourcePanel KB 来源卡溯源钮（v1.33 增，REQ-053；frontend-api §5 SourcePanel/plan §1.1）', () => {
  function kbSource(partial: Partial<Source> & { source_id: string; ref_no: number }): Source {
    return makeSource({ source_type: 'kb', url: null, kb_doc_id: '9a770003-0000-4000-8000-000000000003', ...partial })
  }

  it('KB 卡（source_type=kb 且 kb_chunk_id 存在）渲染溯源钮；点击 emit kbChunk(source_id, kb_chunk_id) 载荷', async () => {
    const w = mountPanel([
      group({
        turnId: TURN_1,
        sources: [kbSource({ source_id: 'kb-1', ref_no: 2, kb_chunk_id: '5c2e0001-0000-4000-8000-000000000001' })],
      }),
    ])
    const btn = w.find('[data-testid="source-kb-drill"]')
    expect(btn.exists()).toBe(true)
    await btn.trigger('click')
    expect(w.emitted('kbChunk')).toEqual([['kb-1', '5c2e0001-0000-4000-8000-000000000001']])
  })

  it('web 卡无溯源钮（400001 防御面——前端仅对 KB 卡渲染入口）', () => {
    const w = mountPanel([group({ turnId: TURN_1, sources: [makeSource({ source_id: 'w1', ref_no: 1 })] })])
    expect(w.find('[data-testid="source-kb-drill"]').exists()).toBe(false)
  })

  it('kb_chunk_id 缺省 null（存量轮形态）→ 不渲染溯源钮', () => {
    const w = mountPanel([group({ turnId: TURN_1, sources: [kbSource({ source_id: 'kb-legacy', ref_no: 1, kb_chunk_id: null })] })])
    expect(w.find('[data-testid="source-kb-drill"]').exists()).toBe(false)
  })

  it('KB 卡快照胶囊位共存：snapshot_available=false 无胶囊、溯源钮与快照位同位不重叠', () => {
    const w = mountPanel([
      group({
        turnId: TURN_1,
        sources: [
          makeSource({ source_id: 'w-snap', ref_no: 1, snapshot_available: true }),
          kbSource({ source_id: 'kb-1', ref_no: 2, kb_chunk_id: '5c2e0001-0000-4000-8000-000000000001', snapshot_available: false }),
        ],
      }),
    ])
    const cards = w.findAll('.src-card')
    expect(cards[0]!.find('[data-testid="snapshot-1"]').exists()).toBe(true)
    expect(cards[1]!.find('[data-testid="snapshot-2"]').exists()).toBe(false) // KB 来源无快照胶囊（api.md §5.1 语义）
    expect(cards[1]!.find('[data-testid="source-kb-drill"]').exists()).toBe(true)
  })

  it('溯源钮 a11y：icon-only 按钮带 aria-label（溯源）', () => {
    const w = mountPanel([
      group({ turnId: TURN_1, sources: [kbSource({ source_id: 'kb-1', ref_no: 1, kb_chunk_id: '5c2e0001-0000-4000-8000-000000000001' })] }),
    ])
    expect(w.find('[data-testid="source-kb-drill"]').attributes('aria-label')).toBe('溯源')
  })
})

describe('SourcePanel 分支收尾（覆盖率恢复批：domainOf 兜底/initialOf 空回落/highlightSeq 早退）', () => {
  it('url 非法（URL 解析抛错）→ 域名位回落展示原始串（domainOf catch 分支）', () => {
    const w = mountPanel([
      group({ turnId: TURN_1, sources: [makeSource({ source_id: 'bad', ref_no: 9, url: '::not-a-url::' })] }),
    ])
    expect(w.find('.src-domain').text()).toBe('::not-a-url::')
  })

  it('url/kb_doc_id 双缺 + 标题空白 → favicon 位回落问号（initialOf 空回落分支）', () => {
    const w = mountPanel([
      group({ turnId: TURN_1, sources: [makeSource({ source_id: 'ghost', ref_no: 8, url: null, kb_doc_id: null, title: '  ' })] }),
    ])
    expect(w.find('.src-fav').text()).toBe('?')
  })

  it('highlightSeq 归零 → 早退不触发高亮（watch 首行守卫分支）', async () => {
    const w = mountPanel([group({ turnId: TURN_1, sources: [makeSource({ source_id: 'a', ref_no: 1 })] })])
    expect(w.find('[data-testid="source-panel"]').classes()).not.toContain('panel-hit')
    await w.setProps({ highlightSeq: 0 })
    await nextTick()
    expect(w.find('[data-testid="source-panel"]').classes()).not.toContain('panel-hit')
  })
})
