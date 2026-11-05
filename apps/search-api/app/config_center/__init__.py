"""Nacos 资产中心（占位包，B1 工单实装）。

规划（ARCH §3.1 / REQ-028）：客户端（nacos-sdk-python 配置监听，后台线程长轮询 →
线程安全换快照）/ 缓存 / 校验三层；资产管理五 dataId（提示词、模型映射、Agent Spec、
功能开关、热门池——键位见 docs/contract/nacos-config.md）；连接配置一律经 core/config
单例（NACOS_ 组）读取，不在本包内硬编码地址/凭据。密钥绝不进 Nacos（拒收疑似密钥字段）。
"""
