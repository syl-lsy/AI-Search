/**
 * src/main.ts — 应用入口：挂载 Pinia（状态）+ Router（懒加载路由）+ i18n（§7）。
 * 样式入口 = styles/index.css（tokens.css 原文 + Tailwind 三指令）。
 */
import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import { i18n } from './i18n'
import { router } from './router'
import './styles/index.css'

createApp(App).use(createPinia()).use(router).use(i18n).mount('#app')
