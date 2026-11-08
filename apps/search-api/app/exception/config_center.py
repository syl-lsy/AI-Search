"""配置中心不可达异常：production 启动期降级到 L4（无任何缓存）时拒绝启动。"""
from app.exception.base import BusinessException


class ConfigCenterException(BusinessException):
    """配置中心不可达且无缓存：映射 HTTP 503 + 业务错误码 503001（api.md §0；nacos-config.md §7 L4）。

    仅启动期可能发生——运行期内存快照一旦建立便始终可服务（§7 L2）。
    """
