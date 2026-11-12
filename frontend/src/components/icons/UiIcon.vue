<script setup lang="ts">
// UiIcon — symbol 引用助手（tokens.css .icon/.icon-sm/.icon-lg 三档，§8.6 内联 SVG stroke 体系）。
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{ name: string; size?: 'sm' | 'md' | 'lg' }>(),
  { size: 'md' },
)

const sizeClass = computed(() =>
  props.size === 'sm' ? 'icon-sm' : props.size === 'lg' ? 'icon-lg' : 'icon',
)
</script>

<template>
  <svg :class="sizeClass" aria-hidden="true" focusable="false"><use :href="`#${name}`" /></svg>
</template>
