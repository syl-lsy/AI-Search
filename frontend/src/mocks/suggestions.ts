/**
 * src/mocks/suggestions.ts — GET /api/v1/suggestions 静态 fixture（api.md §3.2；§9 v1.10：
 * 6 条示意（0~8 上限、单条 ≤200 字符），不硬编码语义断言——文案以 Nacos home.suggest_pool 为准）。
 */
import type { SuggestionPoolData } from '@/types/api'

export const suggestionsFixture: SuggestionPoolData = {
  items: [
    'LangGraph 和 deepagents 是什么关系？',
    '对比 pgvector 与 Milvus，个人知识库场景怎么选？',
    'Vue3 的 watchEffect 和 computed 有什么区别？',
    'RAG 检索增强生成有哪些主流优化手段？',
    'LiteLLM 网关如何配置多模型故障切换？',
    'MCP 协议适合什么样的 Agent 应用场景？',
  ],
}
