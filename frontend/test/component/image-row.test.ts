/**
 * test/component/image-row.test.ts — ImageCardRow 组件层（REQ-004；api.md §7.2 v1.12 键可省）。
 * 缺键不渲染对应元数据（无来源链接/无标题占位/不按宽高预留布局）+ 协议白名单 + onerror 静默隐藏。
 */
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ImageCardRow from '@/components/answer/ImageCardRow.vue'
import type { TurnImageItem } from '@/types/api'

function mountRow(images: TurnImageItem[]) {
  return mount(ImageCardRow, { props: { images } })
}

describe('ImageCardRow（REQ-004 / v1.12 键可省口径）', () => {
  it('无图不渲染区块（无空区块原则）', () => {
    const w = mountRow([])
    expect(w.find('[data-testid="image-row"]').exists()).toBe(false)
  })

  it('仅 url 键条目：渲染图片，但无来源链接（缺 source_url 不渲染）、无标题（缺 title 不渲染）', () => {
    const w = mountRow([{ url: 'https://example.com/a.png' }])
    expect(w.find('[data-testid="image-row"]').exists()).toBe(true)
    const card = w.find('a.img-card')
    expect(card.attributes('href')).toBeUndefined()
    expect(w.find('.img-src').exists()).toBe(false)
    const img = w.find('img.img-el')
    expect(img.attributes('src')).toBe('https://example.com/a.png')
    // 缺 width/height 不按宽高预留布局（无 aspect-ratio 内联样式）
    expect(img.attributes('style')).toBeUndefined()
  })

  it('全键条目：来源链接 + 标题 + 按宽高预留布局（aspect-ratio）', () => {
    const w = mountRow([
      {
        url: 'https://example.com/chart.png',
        source_url: 'https://example.com/page',
        title: '对比图表',
        width: 1200,
        height: 750,
      },
    ])
    const card = w.find('a.img-card')
    expect(card.attributes('href')).toBe('https://example.com/page')
    expect(card.attributes('target')).toBe('_blank')
    expect(w.find('.img-src').text()).toContain('对比图表')
    expect(w.find('img.img-el').attributes('style')).toContain('aspect-ratio: 1200 / 750')
  })

  it('协议白名单（§2 ^https?://）：非 http(s) 图片 URL 整卡不渲染', () => {
    const w = mountRow([
      { url: 'data:text/html;base64,xxx' },
      { url: 'javascript:alert(1)' },
      { url: 'https://example.com/ok.png' },
    ])
    const imgs = w.findAll('img.img-el')
    expect(imgs).toHaveLength(1)
    expect(imgs[0]?.attributes('src')).toBe('https://example.com/ok.png')
  })

  it('单图加载失败 onerror → 静默隐藏该卡（其余保留）', async () => {
    const w = mountRow([
      { url: 'https://example.com/broken.png' },
      { url: 'https://example.com/good.png' },
    ])
    expect(w.findAll('img.img-el')).toHaveLength(2)
    await w.findAll('img.img-el')[0]!.trigger('error')
    const imgs = w.findAll('img.img-el')
    expect(imgs).toHaveLength(1)
    expect(imgs[0]?.attributes('src')).toBe('https://example.com/good.png')
  })

  it('source_url 非 http(s) 时不渲染来源链接（协议白名单）', () => {
    const w = mountRow([{ url: 'https://example.com/x.png', source_url: 'javascript:alert(1)', title: 't' }])
    expect(w.find('a.img-card').attributes('href')).toBeUndefined()
  })
})
