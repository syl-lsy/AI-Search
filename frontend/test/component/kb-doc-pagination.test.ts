/**
 * test/component/kb-doc-pagination.test.ts — KbDocPagination 页码分页控件（R-3 用户裁决：
 * 可选页码分页，替代触底无限滚动）。
 * 页码窗口（首末恒显 + 当前 ±1 + 省略号折叠）/ aria-current="page" / nav aria-label /
 * 边界与加载中 disabled / change 事件载荷（«=1 ‹=p-1 页码=p ›=p+1 »=末页）。
 */
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import KbDocPagination from '@/components/library/KbDocPagination.vue'
import { i18n } from '@/i18n'

function mountPager(props: { page: number; totalPages: number; disabled?: boolean }) {
  return mount(KbDocPagination, {
    props: { page: props.page, totalPages: props.totalPages, disabled: props.disabled ?? false },
    global: { plugins: [i18n] },
  })
}

/** 页码钮文本序（窗口投影断言通道） */
function pageNums(w: ReturnType<typeof mountPager>): string[] {
  return w.findAll('.pager-num').map((n) => n.text())
}

describe('KbDocPagination — 页码窗口（首末恒显 + 当前 ±1 + 省略号）', () => {
  it('page=5 / totalPages=21 → 1 … 4 5 6 … 21（首末恒显 + 当前 ±1 窗口 + 双省略号）', () => {
    const w = mountPager({ page: 5, totalPages: 21 })
    expect(pageNums(w)).toEqual(['1', '4', '5', '6', '21'])
    expect(w.findAll('.pager-ellipsis')).toHaveLength(2)
    // 五钮齐备：« ‹ › » + 总页数文本
    expect(w.find('[data-testid="kb-pager-first"]').text()).toBe('«')
    expect(w.find('[data-testid="kb-pager-prev"]').text()).toBe('‹')
    expect(w.find('[data-testid="kb-pager-next"]').text()).toBe('›')
    expect(w.find('[data-testid="kb-pager-last"]').text()).toBe('»')
    expect(w.find('.kb-pager-total').text()).toBe('共 21 页')
  })

  it('page=2 / totalPages=6（≤7 全显无省略号）；page=1 窗口头部=1 2 3', () => {
    expect(pageNums(mountPager({ page: 2, totalPages: 6 }))).toEqual(['1', '2', '3', '4', '5', '6'])
    expect(mountPager({ page: 2, totalPages: 6 }).find('.pager-ellipsis').exists()).toBe(false)
    expect(pageNums(mountPager({ page: 1, totalPages: 21 }))).toEqual(['1', '2', '3', '21'])
    expect(pageNums(mountPager({ page: 21, totalPages: 21 }))).toEqual(['1', '19', '20', '21'])
  })

  it('nav aria-label=分页导航；当前页钮 aria-current="page" + disabled', () => {
    const w = mountPager({ page: 5, totalPages: 21 })
    expect(w.find('nav').attributes('aria-label')).toBe('分页导航')
    const cur = w.find('[data-testid="kb-pager-page-5"]')
    expect(cur.attributes('aria-current')).toBe('page')
    expect(cur.attributes('disabled')).toBeDefined()
    const other = w.find('[data-testid="kb-pager-page-4"]')
    expect(other.attributes('aria-current')).toBeUndefined()
    expect(other.attributes('disabled')).toBeUndefined()
  })

  it('边界 disabled：page=1 → « ‹ disabled；page=末页 → › » disabled', () => {
    const first = mountPager({ page: 1, totalPages: 21 })
    expect(first.find('[data-testid="kb-pager-first"]').attributes('disabled')).toBeDefined()
    expect(first.find('[data-testid="kb-pager-prev"]').attributes('disabled')).toBeDefined()
    expect(first.find('[data-testid="kb-pager-next"]').attributes('disabled')).toBeUndefined()
    const last = mountPager({ page: 21, totalPages: 21 })
    expect(last.find('[data-testid="kb-pager-next"]').attributes('disabled')).toBeDefined()
    expect(last.find('[data-testid="kb-pager-last"]').attributes('disabled')).toBeDefined()
  })

  it('loadingPage（disabled prop）→ 全部钮 disabled', () => {
    const w = mountPager({ page: 5, totalPages: 21, disabled: true })
    for (const btn of w.findAll('button')) {
      expect(btn.attributes('disabled')).toBeDefined()
    }
  })

  it('change 事件载荷：«=1 / ‹=p-1 / 页码=p / ›=p+1 / »=末页；同页/越界点击零事件', async () => {
    const w = mountPager({ page: 5, totalPages: 21 })
    await w.find('[data-testid="kb-pager-first"]').trigger('click')
    await w.find('[data-testid="kb-pager-prev"]').trigger('click')
    await w.find('[data-testid="kb-pager-page-4"]').trigger('click')
    await w.find('[data-testid="kb-pager-next"]').trigger('click')
    await w.find('[data-testid="kb-pager-last"]').trigger('click')
    expect(w.emitted('change')).toEqual([[1], [4], [4], [6], [21]])
    // 同页（当前页钮 disabled）与越界（守卫）零事件
    await w.find('[data-testid="kb-pager-page-5"]').trigger('click')
    expect(w.emitted('change')).toHaveLength(5)
  })
})
