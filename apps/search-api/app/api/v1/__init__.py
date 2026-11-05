"""API v1 路由包（占位，B2 起按 contract/api.md 落地）。

规划路由模块（ARCH §3.1）：threads / turns / kb / sources / artifacts / models /
suggestions / health；webhooks 挂 /api/webhooks 前缀（非 /api/v1，M4）。
api 层只做路由 + 参数校验 + 依赖注入，业务逻辑一律下沉 services/。
"""
