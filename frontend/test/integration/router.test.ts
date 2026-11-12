import { describe, expect, it } from 'vitest'

import { router, routes } from '@/router'
import { isUuid } from '@/utils/guards'

const VALID_THREAD_ID = '7d2f0a1e-1c3b-4e5a-9d8f-0a1b2c3d4e5f'

describe('路由联装（frontend-api.md §1 页面映射 / §8.3 懒加载）', () => {
  it('四页路由全部懒加载（component 为异步加载函数；§8.3）', () => {
    expect(routes).toHaveLength(4)
    for (const route of routes) {
      expect(typeof route.component).toBe('function')
    }
  })

  it('懒加载组件可解析（动态 import 链路通，.vue 默认导出存在）', async () => {
    for (const route of routes) {
      // 原始记录的 component 为懒加载函数；运行时 typeof 收窄后调用（单一 as 标注模块形状，§8.9①）
      const loader = route.component
      if (typeof loader !== 'function') {
        throw new Error('route.component 应为懒加载函数')
      }
      const module = await (loader as () => Promise<{ default: unknown }>)()
      expect(module.default).toBeTruthy()
    }
  })

  it('路径解析：/ /library /threads /thread/:threadId（§1 映射表）', async () => {
    expect(router.resolve('/').name).toBe('home')
    expect(router.resolve('/library').name).toBe('library')
    expect(router.resolve('/threads').name).toBe('threads')

    const answer = router.resolve(`/thread/${VALID_THREAD_ID}`)
    expect(answer.name).toBe('answer')
    expect(answer.params.threadId).toBe(VALID_THREAD_ID)
  })

  it('threadId 形态校验归视图层守卫（§8.3：非法参数不进 API 调用，路由不重定向）', () => {
    expect(isUuid(VALID_THREAD_ID)).toBe(true)
    expect(isUuid('not-a-uuid')).toBe(false)
  })
})
