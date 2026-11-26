"""ES 客户端惰性单例工厂（plan B18 §4.5——连接参数全自 es_settings 单例）。

形态沿 `get_engine` / `get_kb_minio_client` 先例：首次调用惰性装配，进程级单例。
transport 层 max_retries=1 兜连接抖动；业务级重试归 pipeline（两层不混淆）。
`basic_auth` 仅 username 非空时携带（compose 侧 security=false 零鉴权形态；
未来开 security 单侧改 compose，应用零改码）。
"""

from typing import Any

from pydantic import SecretStr

from app.core.config import es_settings

#: 传输层参数（集中工厂——连接池/重试参数不散落调用点）
_TRANSPORT_MAX_RETRIES = 1
_REQUEST_TIMEOUT_S = 10.0

_es_client_instance: Any | None = None


def build_client_kwargs(
    host: str, port: int, username: str, password: SecretStr
) -> dict[str, Any]:
    """AsyncElasticsearch 构造参数（纯函数——auth 仅 username 非空，unit 直驱）。"""
    kwargs: dict[str, Any] = {
        "hosts": [{"host": host, "port": port, "scheme": "http"}],
        "max_retries": _TRANSPORT_MAX_RETRIES,
        "request_timeout": _REQUEST_TIMEOUT_S,
    }
    if username:
        kwargs["basic_auth"] = (username, password.get_secret_value())
    return kwargs


def get_es_client() -> Any:
    """进程级 AsyncElasticsearch 单例（惰性；密钥仅经 get_secret_value 出口消费）。"""
    global _es_client_instance
    if _es_client_instance is None:
        from elasticsearch import AsyncElasticsearch

        _es_client_instance = AsyncElasticsearch(
            **build_client_kwargs(
                es_settings.host,
                es_settings.port,
                es_settings.username,
                es_settings.password,
            )
        )
    return _es_client_instance
