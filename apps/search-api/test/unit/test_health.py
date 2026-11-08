"""unit 层：双健康探针路由（app/api/v1/health.py，api.md §1——健康检查不走信封）。

覆盖点：
- GET /health 存活（§1.1）：恒 200 `{"status": "ok"}`，零外部依赖；
- GET /health/ready 就绪契约（§1.2）：四组件（db/nacos/minio/litellm）探活 + sandbox
  静态位（M4 预留，契约示例字段）——响应体逐字段对照契约示例；
- 状态码语义：全部可用 200，任一组件 unavailable 503；nacos 降级（缓存快照接替实时
  值）≠ 不可用——组件状态 degraded、整体仍 200（503001「不可达且无缓存」仅启动期，
  nacos-config.md §7 降级阶梯是设计内运行态）；
- 探针相互独立：gather 并发 + 每探针独立短超时（_PROBE_TIMEOUT_S），单探针超时/
  失败不阻塞其余探针；
- 失败语义：超时 / 任意异常 → 该组件 unavailable，端点自身永不抛错（就绪探针只答
  200/503，绝不出 500）；
- nacos 探针判定：全部 dataId L1 → ok；任一降至 L2/L3/L4 → degraded（判定依据 =
  AssetCenter.level，config_center 组装层的 degraded 判定锚点）；资产中心未启动 →
  unavailable；
- 出站探活安全闸口：目标仅取自 settings（minio endpoint+secure / litellm base_url），
  发请求前 scheme 必须 http/https；MinIO = GET /minio/health/live（200 才算可用）；
  LiteLLM = GET base_url 轻量探活（任意 HTTP 响应即存活——不耦合网关路由与鉴权）。

外部依赖在 unit 层全 mock（端点级替换四探针；HTTP 假体经 MockTransport 注入
_http_client 工厂缝）——零网络。
"""

import asyncio
from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.v1 import health
from app.config_center import DATA_IDS, DegradationLevel
from app.main import app

client = TestClient(app)

_PROBE_NAMES = ("_probe_db", "_probe_nacos", "_probe_minio", "_probe_litellm")
_COMPONENTS = ("db", "nacos", "minio", "litellm")


async def _ok_probe() -> str:
    return "ok"


def _patch_all_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """四探针全替换为恒 ok 假体（端点级契约测试的零网络基座）。"""
    for name in _PROBE_NAMES:
        monkeypatch.setattr(health, name, _ok_probe)


def _patch_http_client(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    """把出站 HTTP 客户端工厂换成 MockTransport 假体（探针级语义测试的零网络缝）。"""
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        health,
        "_http_client",
        lambda: httpx.AsyncClient(
            transport=transport, timeout=httpx.Timeout(health._PROBE_TIMEOUT_S)
        ),
    )


class _FakeCenter:
    """AssetCenter 假体：只暴露 level(data_id)（nacos 探针的唯一消费面）。"""

    def __init__(self, levels: dict[str, DegradationLevel]) -> None:
        self._levels = levels

    def level(self, data_id: str) -> DegradationLevel:
        return self._levels[data_id]


# ---- §1.1 GET /health：存活 ----


def test_health_liveness_returns_ok_without_envelope():
    """存活探针恒 200 且不走统一信封（§1 健康检查不走信封）。"""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---- §1.2 GET /health/ready：就绪契约 ----


def test_ready_all_ok_matches_contract_shape(monkeypatch: pytest.MonkeyPatch):
    """四组件全可用 → 200，响应体逐字段对照契约 §1.2 示例（含 sandbox 静态位）。"""
    _patch_all_ok(monkeypatch)
    resp = client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "components": {
            "db": "ok",
            "nacos": "ok",
            "minio": "ok",
            "litellm": "ok",
            "sandbox": "disabled",
        },
    }


@pytest.mark.parametrize("failing_probe", _PROBE_NAMES)
def test_ready_single_component_unavailable_returns_503(
    monkeypatch: pytest.MonkeyPatch, failing_probe: str
):
    """任一组件失败 → 503；该组件 unavailable、其余组件不受牵连（§1.2）。"""

    async def _boom() -> str:
        raise RuntimeError(f"{failing_probe} 故障")

    _patch_all_ok(monkeypatch)
    monkeypatch.setattr(health, failing_probe, _boom)
    resp = client.get("/health/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unavailable"
    components = body["components"]
    failing_component = failing_probe.removeprefix("_probe_")
    assert components[failing_component] == "unavailable"
    assert components["sandbox"] == "disabled"
    others = [c for c in _COMPONENTS if c != failing_component]
    assert all(components[c] == "ok" for c in others)


def test_ready_probe_timeout_marks_component_unavailable(
    monkeypatch: pytest.MonkeyPatch,
):
    """超时路径：单探针超过独立超时 → 该组件 unavailable → 503（端点不 500）。"""

    async def _slow() -> str:
        await asyncio.sleep(10)

    _patch_all_ok(monkeypatch)
    monkeypatch.setattr(health, "_probe_minio", _slow)
    monkeypatch.setattr(health, "_PROBE_TIMEOUT_S", 0.02)
    resp = client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["components"]["minio"] == "unavailable"


def test_ready_probes_do_not_block_each_other(monkeypatch: pytest.MonkeyPatch):
    """探针相互独立：db 探针等待的 release 事件由 litellm 探针在自身启动后置位——
    只有 gather 并发调度才能让两者都在超时窗口内完成（串行则 db 必超时 → 503）。"""
    release = asyncio.Event()

    async def _wait_for_release() -> str:
        await release.wait()
        return "ok"

    async def _release_then_ok() -> str:
        await asyncio.sleep(0.05)
        release.set()
        return "ok"

    monkeypatch.setattr(health, "_probe_db", _wait_for_release)
    monkeypatch.setattr(health, "_probe_litellm", _release_then_ok)
    monkeypatch.setattr(health, "_probe_nacos", _ok_probe)
    monkeypatch.setattr(health, "_probe_minio", _ok_probe)
    monkeypatch.setattr(health, "_PROBE_TIMEOUT_S", 0.5)
    resp = client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["components"]["db"] == "ok"


def test_ready_nacos_degraded_keeps_200(monkeypatch: pytest.MonkeyPatch):
    """nacos 降级 ≠ 不可用：组件状态 degraded、整体仍 200（降级阶梯是设计内运行态）。"""

    async def _degraded() -> str:
        return "degraded"

    _patch_all_ok(monkeypatch)
    monkeypatch.setattr(health, "_probe_nacos", _degraded)
    resp = client.get("/health/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["components"]["nacos"] == "degraded"


# ---- nacos 探针语义（判定依据 = AssetCenter.level，§7 降级阶梯）----


async def test_probe_nacos_ok_when_all_data_ids_l1(monkeypatch: pytest.MonkeyPatch):
    """五个 dataId 全部 L1 实时 → ok。"""
    monkeypatch.setattr(
        health,
        "get_asset_center",
        lambda: _FakeCenter({d: DegradationLevel.L1 for d in DATA_IDS}),
    )
    assert await health._probe_nacos() == "ok"


@pytest.mark.parametrize("degraded_level", list(DegradationLevel)[1:])
async def test_probe_nacos_degraded_when_any_data_id_below_l1(
    monkeypatch: pytest.MonkeyPatch, degraded_level: DegradationLevel
):
    """任一 dataId 降至 L2/L3/L4 → 整体 degraded（§7 逐 dataId 降级、就绪取最弱态）。"""
    levels = {d: DegradationLevel.L1 for d in DATA_IDS}
    levels[next(iter(DATA_IDS))] = degraded_level
    monkeypatch.setattr(health, "get_asset_center", lambda: _FakeCenter(levels))
    assert await health._probe_nacos() == "degraded"


async def test_probe_nacos_unavailable_when_center_not_started(
    monkeypatch: pytest.MonkeyPatch,
):
    """资产中心未启动（get_asset_center fail-fast）→ 探针经统一包装落 unavailable。"""

    def _not_started() -> None:
        raise RuntimeError("配置中心尚未启动")

    monkeypatch.setattr(health, "get_asset_center", _not_started)
    assert await health._run_probe(health._probe_nacos) == "unavailable"


# ---- minio 探针语义（GET /minio/health/live，200 才算可用）----


async def test_probe_minio_hits_live_endpoint_with_scheme_from_settings(
    monkeypatch: pytest.MonkeyPatch,
):
    """secure=False → http scheme；URL = {scheme}://{endpoint}/minio/health/live。"""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200)

    _patch_http_client(monkeypatch, handler)
    monkeypatch.setattr(health.minio_settings, "endpoint", "127.0.0.1:9100")
    monkeypatch.setattr(health.minio_settings, "secure", False)
    assert await health._probe_minio() == "ok"
    assert seen["url"] == "http://127.0.0.1:9100/minio/health/live"


async def test_probe_minio_uses_https_when_secure(monkeypatch: pytest.MonkeyPatch):
    """secure=True → https scheme（scheme 由 settings 派生并发前校验）。"""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200)

    _patch_http_client(monkeypatch, handler)
    monkeypatch.setattr(health.minio_settings, "endpoint", "127.0.0.1:9100")
    monkeypatch.setattr(health.minio_settings, "secure", True)
    assert await health._probe_minio() == "ok"
    assert seen["url"] == "https://127.0.0.1:9100/minio/health/live"


async def test_probe_minio_unavailable_on_non_200(monkeypatch: pytest.MonkeyPatch):
    """/minio/health/live 非 200 → unavailable（MinIO 存活端点语义：200 才是 live）。"""
    _patch_http_client(monkeypatch, lambda request: httpx.Response(503))
    assert await health._run_probe(health._probe_minio) == "unavailable"


async def test_probe_minio_unavailable_on_connect_error(
    monkeypatch: pytest.MonkeyPatch,
):
    """连接失败 → unavailable（异常统一包装，探针不外抛）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _patch_http_client(monkeypatch, handler)
    assert await health._run_probe(health._probe_minio) == "unavailable"


# ---- litellm 探针语义（轻量探活：任意 HTTP 响应即存活）----


async def test_probe_litellm_ok_on_any_http_response(monkeypatch: pytest.MonkeyPatch):
    """GET base_url 得到任意 HTTP 响应（含 404——网关路由/鉴权拒绝）即算存活：
    轻量探活只证明网关进程在服务 HTTP，不耦合其路由表与鉴权策略。"""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(404)

    _patch_http_client(monkeypatch, handler)
    assert await health._probe_litellm() == "ok"
    assert seen["url"].rstrip("/") == str(health.litellm_settings.base_url).rstrip("/")


async def test_probe_litellm_unavailable_on_connect_error(
    monkeypatch: pytest.MonkeyPatch,
):
    """连接失败 → unavailable。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _patch_http_client(monkeypatch, handler)
    assert await health._run_probe(health._probe_litellm) == "unavailable"


# ---- db 探针语义（移植自旧内联实现守卫：探活语句必须是 select(1) 表达式文本）----


class _FakeConnection:
    """最小异步连接：记录探针发过的语句并支持 async with。"""

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def scalar(self, statement: object, *args: object) -> int:
        # TextClause 取 .text 属性（str() 需要方言）；Select 等表达式对象 str() 即编译文本
        self.statements.append(str(getattr(statement, "text", statement)))
        return 1

    async def __aenter__(self) -> "_FakeConnection":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


class _FakeEngine:
    def __init__(self) -> None:
        self.connection = _FakeConnection()

    def connect(self) -> _FakeConnection:
        return self.connection


async def test_probe_db_sends_select_one_expression(
    monkeypatch: pytest.MonkeyPatch,
):
    """DB 探活发的是 select(1) 表达式编译文本（安全口径：禁原始 SQL 字符串形态）。"""
    from sqlalchemy import select

    engine = _FakeEngine()
    monkeypatch.setattr(health, "get_engine", lambda: engine)
    assert await health._probe_db() == "ok"
    assert engine.connection.statements == [str(select(1))]


# ---- 出站安全闸口（scheme 必须 http/https）----


def test_validate_outbound_url_rejects_non_http_scheme():
    """出站 URL scheme 校验：非 http/https 拒绝（探针发请求前的最后一道闸）。"""
    with pytest.raises(ValueError):
        health._validate_outbound_url("ftp://127.0.0.1:9100/minio/health/live")
    with pytest.raises(ValueError):
        health._validate_outbound_url("file:///etc/passwd")


def test_validate_outbound_url_passes_http_https_through():
    """http/https 原样放行（不归一化——保持 settings 原值供路径拼接）。"""
    assert (
        health._validate_outbound_url("https://127.0.0.1:4100")
        == "https://127.0.0.1:4100"
    )
    assert (
        health._validate_outbound_url("http://127.0.0.1:9100/x")
        == "http://127.0.0.1:9100/x"
    )
