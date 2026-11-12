/**
 * src/router/index.ts — 路由定义（frontend-api.md §8.3）。
 *
 * - 全部懒加载 () => import('@/views/XxxView.vue')。
 * - 页面映射（§1）：HomeView / AnswerView / LibraryView / ThreadsView；
 *   MemoryView（M5/M6 增）随里程碑落位（§1 映射表里程碑注）。
 * - 路由参数 threadId 经 string 校验后才进 API 调用（§8.3——校验在 AnswerView 内经
 *   utils/guards.isUuid 完成，路由层不做重定向语义）。
 */
import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'

export const routes: RouteRecordRaw[] = [
  {
    path: '/',
    name: 'home',
    component: () => import('@/views/HomeView.vue'),
  },
  {
    path: '/thread/:threadId',
    name: 'answer',
    component: () => import('@/views/AnswerView.vue'),
  },
  {
    path: '/library',
    name: 'library',
    component: () => import('@/views/LibraryView.vue'),
  },
  {
    path: '/threads',
    name: 'threads',
    component: () => import('@/views/ThreadsView.vue'),
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})
