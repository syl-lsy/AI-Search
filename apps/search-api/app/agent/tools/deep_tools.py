"""deep 工具面契约名集（M2 批次 1——DEV §9 子代理最小权限工具集）。

契约三工具（DEVELOPMENT-PROCESS §9 research 行）：`web_search`（既有搜索链消费面）、
`firecrawl_read`（url_reader 读取链工具，tools/url_reader.py）、`save_snapshot`
（快照工具，tools/save_snapshot.py）。批次 2 research（deepagents create_deep_agent）
按本名集装配暴露面（OQ-33 per-subagent 工具收窄探针后钉装配形态）。

**fast 隔离是物理性**（非约定）：SEARCH_SOURCE_NAMES 白名单物理不含读取链两源；
fast 图面模块（graphs/{fast,main,regenerate,registry}.py + nodes/*.py）零导入
save_snapshot/url_reader/deep_tools——AST 哨兵单测锁死（test_url_reader.py /
test_save_snapshot.py 哨兵节）。
"""

__all__ = ["DEEP_TOOL_NAMES"]

#: 子代理可见工具名集（批次 2 装配面以此为单一事实源；fast 面零交集）
DEEP_TOOL_NAMES = frozenset({"web_search", "firecrawl_read", "save_snapshot"})
