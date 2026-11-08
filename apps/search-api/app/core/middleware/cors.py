"""CORS 中间件：读通用组 cors_origins 白名单（CORS_ORIGINS，逗号分隔已在配置层解析）。

类式中间件（kwargs 经 add_middleware 生效，注册表支持）。注册表里必须放最外层：
- 预检 OPTIONS 在此短路返回，不进信封/日志内层——/api/ 路径的预检响应若被
  统一信封改写（PlainText "OK" 经 json.loads 失败兜底 500001）会直接破坏 CORS 语义；
- 业务错误响应（异常处理器在更内层产出）也要带 CORS 头，前端才能读到错误体；
- allow_origins 为显式白名单（配置层不接受 * 通配），与 allow_credentials=True
  组合安全（星号 + 凭据是浏览器禁止的组合）。
"""
from typing import Any

from fastapi.middleware.cors import CORSMiddleware

from app.core.config import common_settings

MIDDLEWARE: tuple[Any, dict[str, Any]] = (
    CORSMiddleware,
    {
        "allow_origins": list(common_settings.cors_origins),
        "allow_credentials": True,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
    },
)
