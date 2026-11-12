<script setup lang="ts">
/**
 * ImageCardRow — images 卡片行（frontend-api.md §5 ImageCardRow 行；REQ-004；设计稿 img-row）。
 *
 * - 无图不渲染区块（无空区块原则，父级 v-if + 组件内兜底）；
 * - 渲染前协议白名单校验 ^https?://（§2）；单图加载失败 onerror 静默隐藏该卡；
 * - 条目键可省（api.md §7.2 v1.12）：无 source_url 不渲染来源链接、无 title 不渲染标题占位、
 *   无 width/height 不按宽高预留布局（frontend-api.md §3 images 行 v1.13 口径）。
 */
import { ref } from 'vue'

import { isHttpUrl } from '@/utils/guards'
import type { TurnImageItem } from '@/types/api'

const props = defineProps<{ images: TurnImageItem[] }>()

/** 加载失败的图 URL → 静默隐藏该卡（§2：onerror 静默隐藏） */
const failedUrls = ref(new Set<string>())

function onImgError(url: string): void {
  failedUrls.value.add(url)
  // Set 内部变更需替换引用以触发响应式
  failedUrls.value = new Set(failedUrls.value)
}

/** 宽高键齐备时按宽高预留布局；缺任一键不预留（v1.13 缺键不渲染口径） */
function aspectStyle(img: TurnImageItem): Record<string, string> | null {
  if (typeof img.width === 'number' && typeof img.height === 'number' && img.height > 0) {
    return { aspectRatio: `${img.width} / ${img.height}` }
  }
  return null
}

const usable = (): TurnImageItem[] =>
  props.images.filter((img) => isHttpUrl(img.url) && !failedUrls.value.has(img.url))
</script>

<template>
  <div v-if="usable().length > 0" class="img-row" data-testid="image-row">
    <a
      v-for="img in usable()"
      :key="img.url"
      class="img-card"
      :href="img.source_url && isHttpUrl(img.source_url) ? img.source_url : undefined"
      :target="img.source_url && isHttpUrl(img.source_url) ? '_blank' : undefined"
      rel="noopener noreferrer"
    >
      <img
        class="img-el"
        :src="img.url"
        :alt="img.title ?? ''"
        :style="aspectStyle(img)"
        loading="lazy"
        @error="onImgError(img.url)"
      />
      <div v-if="img.title" class="img-src">{{ img.title }}</div>
    </a>
  </div>
</template>

<style scoped>
.img-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--space-4);
  margin-bottom: var(--space-8);
}
.img-card {
  min-width: 0;
  display: block;
}
.img-el {
  width: 100%;
  aspect-ratio: 16 / 10;
  object-fit: cover;
  border-radius: var(--radius-md);
  border: 1px solid var(--color-border);
  display: block;
  transition: transform var(--duration-base) var(--ease-out), border-color var(--duration-base) var(--ease-out);
}
.img-card:hover .img-el {
  transform: translateY(-2px);
  border-color: var(--color-border-strong);
}
.img-src {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: var(--space-2);
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
@media (max-width: 720px) {
  .img-row {
    grid-template-columns: repeat(2, 1fr);
  }
}
</style>
