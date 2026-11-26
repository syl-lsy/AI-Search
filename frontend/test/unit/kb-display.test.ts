/**
 * test/unit/kb-display.test.ts — KB 展示投影纯函数（plan-m3-b6-f6 §3.2/§3.4/§5.1）。
 * parseFailStage（api.md §4.3 阶段化 fail_reason）/ stepperNodes（四节点步进投影，
 * 设计稿 .st-trail done/cur/fail/todo）/ parseMetaSegments（parse_meta 结构化段，null 段不产段——
 * i18n 渲染归视图层，CR-4）/ feishuJumpUrl（api.md §4.6 v1.31 node_token 回跳 URL 构造，
 * 固定 https 基座 + isHttpUrl 纵深冗余守卫，SA-1）。
 */
import { describe, expect, it } from 'vitest'

import { feishuJumpUrl, parseFailStage, parseMetaSegments, stepperNodes } from '@/utils/kbDisplay'

describe('parseFailStage（api.md §4.3：非空值形如 {stage}: {detail}；pending 期失败无阶段前缀）', () => {
  it('三 stage 前缀命中 → 拆 stage + detail（detail 含冒号时贪婪归 detail）', () => {
    expect(parseFailStage('normalizing: 加密 PDF 无法解析，请解除密码后重新上传')).toEqual({
      stage: 'normalizing',
      detail: '加密 PDF 无法解析，请解除密码后重新上传',
    })
    expect(parseFailStage('chunking: 切分写入失败: RuntimeError')).toEqual({
      stage: 'chunking',
      detail: '切分写入失败: RuntimeError',
    })
    expect(parseFailStage('indexing: ES 写入失败: ConnectionError(attempts=3)')).toEqual({
      stage: 'indexing',
      detail: 'ES 写入失败: ConnectionError(attempts=3)',
    })
  })

  it('pending 期失败无前缀 → stage null、原文整体', () => {
    expect(parseFailStage('文件校验未通过')).toEqual({ stage: null, detail: '文件校验未通过' })
  })

  it('异形整体回退（含 stage 词但无「: 」前缀形态 → 防御不误拆）', () => {
    expect(parseFailStage('normalizing 异常终止（无冒号分隔）')).toEqual({
      stage: null,
      detail: 'normalizing 异常终止（无冒号分隔）',
    })
  })

  it('空串 → stage null、detail 空串', () => {
    expect(parseFailStage('')).toEqual({ stage: null, detail: '' })
  })
})

describe('stepperNodes（api.md §4.3 六态 → 四节点步进投影，设计稿 .st-trail）', () => {
  function statesOf(status: Parameters<typeof stepperNodes>[0], failStage: Parameters<typeof stepperNodes>[1]) {
    return stepperNodes(status, failStage).map((n) => n.state)
  }

  it('节点 key 序恒为 normalizing→chunking→indexing→ready（设计稿四节点）', () => {
    for (const status of ['pending', 'normalizing', 'chunking', 'indexing', 'ready', 'failed'] as const) {
      expect(stepperNodes(status, null).map((n) => n.key)).toEqual(['normalizing', 'chunking', 'indexing', 'ready'])
    }
  })

  it('normalizing → [cur, todo, todo, todo]', () => {
    // 逐态全量穷举断言（StepperNodeState 联合类型由编译期收窄，运行时不再重复子集校验——CR-6 删冗余）
    expect(statesOf('normalizing', null)).toEqual(['cur', 'todo', 'todo', 'todo'])
  })

  it('chunking → [done, cur, todo, todo]', () => {
    expect(statesOf('chunking', null)).toEqual(['done', 'cur', 'todo', 'todo'])
  })

  it('indexing → [done, done, cur, todo]', () => {
    expect(statesOf('indexing', null)).toEqual(['done', 'done', 'cur', 'todo'])
  })

  it('ready → 全 done', () => {
    expect(statesOf('ready', null)).toEqual(['done', 'done', 'done', 'done'])
  })

  it('failed × 3 stage：fail 定位失败节点，之前 done、之后 todo', () => {
    expect(statesOf('failed', 'normalizing')).toEqual(['fail', 'todo', 'todo', 'todo'])
    expect(statesOf('failed', 'chunking')).toEqual(['done', 'fail', 'todo', 'todo'])
    expect(statesOf('failed', 'indexing')).toEqual(['done', 'done', 'fail', 'todo'])
  })

  it('failed 无 stage（pending 期失败）→ 全 todo', () => {
    expect(statesOf('failed', null)).toEqual(['todo', 'todo', 'todo', 'todo'])
  })

  it('pending → 全 todo（视图层 v-if 整条不渲染，由徽标承载）', () => {
    expect(statesOf('pending', null)).toEqual(['todo', 'todo', 'todo', 'todo'])
  })
})

describe('parseMetaSegments（api.md §4.3 null 段不产段；结构化输出——i18n 渲染归视图层，CR-4）', () => {
  it('正常三段：pages/tables/code 结构化产出（段序恒 pages→tables→code）', () => {
    expect(parseMetaSegments({ pages: 24, tables: 3, code_blocks: 12 })).toEqual([
      { key: 'pages', n: 24 },
      { key: 'tables', n: 3 },
      { key: 'code', n: 12 },
    ])
  })

  it('pages null（非页式来源）→ 只产表/码两段', () => {
    expect(parseMetaSegments({ pages: null, tables: 0, code_blocks: 0 })).toEqual([
      { key: 'tables', n: 0 },
      { key: 'code', n: 0 },
    ])
    expect(parseMetaSegments({ pages: null, tables: 5, code_blocks: 2 })).toEqual([
      { key: 'tables', n: 5 },
      { key: 'code', n: 2 },
    ])
  })

  it('parse_meta null（normalizing 完成前）→ null（视图层渲染 —）', () => {
    expect(parseMetaSegments(null)).toBeNull()
    expect(parseMetaSegments(undefined)).toBeNull()
  })
})

describe('feishuJumpUrl（api.md §4.6 v1.31：回跳 URL = https://www.feishu.cn/wiki/{node_token}）', () => {
  it('node_token 存在 → 回跳 URL（安全边界=固定 https 基座：token 只落 path 段，SA-1）', () => {
    expect(feishuJumpUrl('wikcnKQ1k3pdoGgR1T2Uabckxy')).toBe(
      'https://www.feishu.cn/wiki/wikcnKQ1k3pdoGgR1T2Uabckxy',
    )
  })

  it('安全边界锁定：任意 token 无法改写 scheme/host（基座在拼接前已闭合；isHttpUrl 为纵深冗余恒真）', () => {
    // 对抗形态 token（伪协议/路径穿越）→ scheme+authority 仍恒为固定基座
    for (const hostile of ['https://evil.com', 'javascript:alert(1)', '../../evil', '//evil.com']) {
      expect(feishuJumpUrl(hostile)).toMatch(/^https:\/\/www\.feishu\.cn\/wiki\//)
    }
  })

  it('node_token 缺省（可选键缺则省略不置 null）→ null（外链钮数据面不渲染）', () => {
    expect(feishuJumpUrl(undefined)).toBeNull()
  })

  it('node_token 空串 → null', () => {
    expect(feishuJumpUrl('')).toBeNull()
  })
})
