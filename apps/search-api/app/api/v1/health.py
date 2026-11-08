"""双健康探针路由（api.md §1——健康检查不走信封，不挂 /api/ 前缀故不经成功信封包装）。

- GET /health（§1.1 存活）：只证明进程能响应请求，零外部依赖——依赖抖动会误杀
  健康进程，这里绝不能加数据库/外部服务检查；
- GET /health/ready（§1.2 就绪）：四组件并行探活（db / nacos / minio / litellm）+
  sandbox 静态位（M4 预留，契约示例字段）；全部可用 200，任一组件 unavailable 503；
  nacos 降级（缓存快照接替实时值）≠ 不可用——组件状态 degraded、整体仍 200
  （503001「不可达且无缓存」仅启动期，nacos-config.md §7 四级降级阶梯是设计内
  运行态，降级不摘流量）。

探针纪律：
- 相互独立：asyncio.gather 并发调度，各探针独立限时（_PROBE_TIMEOUT_S），单探针
  超时/失败不阻塞其余探针；
- 失败语义：超时/任意异常统一落组件 unavailable，端点自身永不抛错（就绪探针只答
  200/503，绝不出 500）；
- 出站安全：探活目标仅取自 core/config settings（MINIO_/LITELLM_ 组），发请求前
  scheme 必须 http/https（_validate_outbound_url 闸口）；第一方基建端点绑 127.0.0.1
  属部署设计（ARCH §7），不做内网地址拒绝；严禁以请求参数/用户输入为 URL 来源。

失败组件明细由响应体 components 携带（请求日志中间件每请求一条 503 留痕；
探针不另起日志出口——运维面要的「哪个组件、什么状态」在响应体里）。
"""
import asyncio
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.config_center import DATA_IDS, DegradationLevel
from app.core.config import litellm_settings, minio_settings
from app.db.session import get_engine
from app.dependencies import get_asset_center

# 探针超时为与环境无关的纯逻辑值（配置三分法 → 代码常量，语义化命名收敛于此）：
# 单探针独立限时，gather 并发下 /health/ready 总耗时上限 ≈ 本值。键位冻结的五
# dataId 无此键（热调诉求出现前先按常量落，nacos-config.md §1「键位即契约」）。
_PROBE_TIMEOUT_S: float = 2.0

# 组件状态值（api.md §1.2 示例字段值）：ok 可用 / degraded 降级可用（仅 nacos）/
# unavailable 不可用（触发整体 503）/ sandbox 恒 disabled（M4 预留，禁探活）。
_STATUS_OK = "ok"
_STATUS_DEGRADED = "degraded"
_STATUS_UNAVAILABLE = "unavailable"
_SANDBOX_STATUS = "disabled"


def _validate_outbound_url(url: str) -> str:
    """出站 URL scheme 闸口：仅放行 http/https（不归一化——保持 settings 原值供路径拼接）。

    探活目标只来自部署配置（settings 单例），本函数是发请求前的最后一道校验；
    URL 结构异常立即失败，不让畸形目标进入 httpx。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"出站探活 URL 必须 http/https，得到：{url!r}")
    return url


def _http_client() -> httpx.AsyncClient:
    """出站探活 HTTP 客户端工厂（超时与探针限时同源；单测经 MockTransport 注入假体）。"""
    return httpx.AsyncClient(timeout=httpx.Timeout(_PROBE_TIMEOUT_S))


# ---- 四组件探针（各自只做判定，超时/异常统一交 _run_probe 包装）----


async def _probe_db() -> str:
    """DB 探活：从连接池借一条连接执行 SELECT 1（表达式层构造，非原始 SQL 字符串）。"""
    engine = get_engine()
    async with engine.connect() as conn:
        await conn.scalar(select(1))
    return _STATUS_OK


async def _probe_nacos() -> str:
    """Nacos 探活：判定依据 = AssetCenter.level（config_center 组装层的 degraded 判定锚点）。

    五 dataId 全部 L1（实时拉取/推送生效）→ ok；任一降至 L2/L3/L4 → degraded——
    缓存快照接替实时值是 §7 设计内运行态（服务照常供数），不是不可用，不落 503；
    资产中心未启动（get_asset_center fail-fast）或其他异常上抛，由 _run_probe 统一落
    unavailable。
    """
    center = get_asset_center()
    if all(center.level(data_id) is DegradationLevel.L1 for data_id in DATA_IDS):
        return _STATUS_OK
    return _STATUS_DEGRADED


async def _probe_minio() -> str:
    """MinIO 探活：GET {scheme}://{endpoint}/minio/health/live，200 才算可用。

    scheme 由 MINIO_SECURE 派生（endpoint 是 host:port 形态，minio SDK 口径无 scheme）；
    存活端点语义以 MinIO 官方为准：200 = live，其余一律不可用。
    """
    scheme = "https" if minio_settings.secure else "http"
    url = _validate_outbound_url(
        f"{scheme}://{minio_settings.endpoint}/minio/health/live"
    )
    async with _http_client() as client:
        response = await client.get(url)
    return _STATUS_OK if response.status_code == 200 else _STATUS_UNAVAILABLE


async def _probe_litellm() -> str:
    """LiteLLM 轻量探活：GET base_url，任意 HTTP 响应即存活。

    只证明网关进程在服务 HTTP（连接、TLS、进程存活），不耦合其路由表与鉴权策略——
    根路径 404/401 也算可用；连接失败/超时上抛交 _run_probe 落 unavailable。
    """
    url = _validate_outbound_url(litellm_settings.base_url)
    async with _http_client() as client:
        await client.get(url)
    return _STATUS_OK


async def _run_probe(probe: Callable[[], Awaitable[str]]) -> str:
    """单探针统一包装：独立限时 + 异常兜底——任何失败都落 unavailable，绝不外抛。"""
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_S):
            return await probe()
    except TimeoutError:
        # 独立超时路径：该探针限时内未完成即判不可用（其余探针不受影响）
        return _STATUS_UNAVAILABLE
    except Exception:  # noqa: BLE001 —— 探针失败语义 = 组件 unavailable（就绪端点只答 200/503）
        return _STATUS_UNAVAILABLE


router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """存活探针（Liveness Probe）：只证明进程能响应请求，不依赖外部服务（api.md §1.1）。

    K8s 每 30 秒调用一次，连续失败 3 次判定 Pod 死亡并重启；因此这里绝不能加
    数据库/外部服务检查——依赖抖动会误杀健康的 Pod。
    """
    return {"status": _STATUS_OK}


@router.get("/health/ready")
async def health_ready() -> JSONResponse:
    """就绪探针（Readiness Probe）：四组件并行探活，任一不可用 503（api.md §1.2）。

    K8s 每 10 秒调用一次，连续失败 3 次将该 Pod 从 Service 转发列表摘除（不重启，
    仅不接收新流量），待恢复后自动重新加入。四探针 gather 并发、各自独立限时——
    单组件抖动不拉长整体探测时延；失败组件明细在 components 里逐字段可见。
    """
    probes: dict[str, Callable[[], Awaitable[str]]] = {
        "db": _probe_db,
        "nacos": _probe_nacos,
        "minio": _probe_minio,
        "litellm": _probe_litellm,
    }
    statuses = await asyncio.gather(*(_run_probe(probe) for probe in probes.values()))
    components: dict[str, str] = dict(zip(probes.keys(), statuses, strict=True))
    components["sandbox"] = _SANDBOX_STATUS  # M4 预留：未启用即 disabled，非探活项

    if _STATUS_UNAVAILABLE in components.values():
        return JSONResponse(
            status_code=503,
            content={"status": _STATUS_UNAVAILABLE, "components": components},
        )
    return JSONResponse(
        status_code=200, content={"status": _STATUS_OK, "components": components}
    )
