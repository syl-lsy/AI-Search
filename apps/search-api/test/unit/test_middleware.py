"""unit 层：统一成功信封中间件 + 请求日志中间件（app/core/middleware/）。

覆盖点：
- `/api/` 路径的成功响应包装为 `{"code": "0", "data": ..., "message": "ok"}`；
- 非 `/api/` 路径（/health）不包装；
- 错误响应（异常处理器置 `exception_handled` 标志）不被二次包装，顶层即 `{code, message}`；
- `is_stream` 标志 / 204 / HEAD 请求跳过包装；
- 包装后保留原状态码与响应头（含多值头：双 Set-Cookie 逐条保留），
  且每个响应都带 `X-Process-Time`（毫秒数字）；
- 注册表顺序（process_time → logging → response → cors）与类式中间件挂载；
- **请求日志「每请求恰一条」**：成功请求产出 SUCCESS 记录（带 uuid4 request_id、
  真实状态码、method/path）；异常请求不出 SUCCESS 记录（由全局处理器按级落）。

测试路由由模块级 autouse fixture 挂载、跑完移除，不污染共享 app
（对齐 test_exception_handlers.py 的 _remove_test_route 清理纪律）。
loguru 捕获 fixture（log_records）见 test/unit/conftest.py。
"""
import json
import uuid

import pytest
from fastapi import APIRouter, Request, Response
from fastapi.testclient import TestClient

from app.exception import NotFoundException
from app.main import app

router = APIRouter()

# 未包装时的响应体，HEAD 用例用它推算原 content-length
_ECHO_BODY = '{"hello":"world"}'


@router.get("/api/_test_echo")
async def _echo() -> dict[str, str]:
    return {"hello": "world"}


@router.head("/api/_test_head")
async def _head() -> dict[str, str]:
    """专用 HEAD 路由（FastAPI 不会给 GET 自动挂 HEAD，须显式声明）。"""
    return {"hello": "world"}


@router.get("/api/_test_echo_headers")
async def _echo_headers(response: Response) -> dict[str, str]:
    response.headers["X-Custom"] = "kept"
    return {"hello": "world"}


@router.get("/api/_test_cookies")
async def _cookies(response: Response) -> dict[str, str]:
    """连设两个 cookie：响应须带两条 Set-Cookie（多值头不得被折叠）。"""
    response.set_cookie("a", "1")
    response.set_cookie("b", "2")
    return {"hello": "world"}


@router.get("/api/_test_not_found")
async def _not_found() -> dict[str, str]:
    raise NotFoundException(message="商品不存在")


@router.get("/api/_test_no_content", status_code=204)
async def _no_content() -> None:
    return None


@router.get("/api/_test_stream")
async def _stream(request: Request) -> dict[str, str]:
    """流式端点示意：真实流式响应应自设 is_stream 标志，避免被拼成整块 JSON。"""
    request.state.is_stream = True
    return {"chunk": "raw"}


_TEMP_PATHS = (
    "/api/_test_echo",
    "/api/_test_head",
    "/api/_test_echo_headers",
    "/api/_test_cookies",
    "/api/_test_not_found",
    "/api/_test_no_content",
    "/api/_test_stream",
)


@pytest.fixture(scope="module", autouse=True)
def _temp_api_routes():
    """模块内挂载测试路由，跑完按路径移除，共享 app 恢复原样。"""
    app.include_router(router)
    yield
    app.router.routes = [
        r for r in app.router.routes if getattr(r, "path", None) not in _TEMP_PATHS
    ]


# ---- 统一成功信封 ----

def test_api_success_response_is_wrapped():
    resp = TestClient(app).get("/api/_test_echo")
    assert resp.status_code == 200
    assert resp.json() == {
        "code": "0",
        "data": {"hello": "world"},
        "message": "ok",  # api.md §0 成功信封：message 固定 "ok"
    }


def test_non_api_path_is_not_wrapped():
    resp = TestClient(app).get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---- 跳过条件 ----

def test_error_response_is_not_double_wrapped():
    """异常处理器置 exception_handled 标志后，错误响应保持 {code, message} 单层结构。"""
    resp = TestClient(app).get("/api/_test_not_found")
    assert resp.status_code == 404
    body = resp.json()
    assert body == {"code": "404001", "message": "商品不存在"}
    assert "data" not in body  # 未被成功信封二次包装


def test_stream_marked_response_is_not_wrapped():
    resp = TestClient(app).get("/api/_test_stream")
    assert resp.status_code == 200
    assert resp.json() == {"chunk": "raw"}


def test_head_request_is_not_wrapped():
    """HEAD 无响应体可包装：用 content-length 区分——被换成信封 JSON 时长度会变。"""
    resp = TestClient(app).head("/api/_test_head")
    assert resp.status_code == 200
    assert resp.headers["content-length"] == str(len(_ECHO_BODY))
    assert resp.content == b""


def test_no_content_status_is_not_wrapped():
    resp = TestClient(app).get("/api/_test_no_content")
    assert resp.status_code == 204
    assert resp.content == b""


# ---- 响应头 ----

def test_headers_preserved_and_process_time_added():
    resp = TestClient(app).get("/api/_test_echo_headers")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"hello": "world"}
    assert resp.headers["x-custom"] == "kept"  # 自定义头随原响应保留
    assert resp.headers["content-type"].startswith("application/json")
    # X-Process-Time 是毫秒数字字符串
    assert float(resp.headers["x-process-time"]) >= 0


def test_multiple_set_cookie_headers_are_preserved():
    """回归：信封重建响应时多值头逐条搬运——dict(headers) 折叠会让第二条 Set-Cookie 静默丢失。"""
    resp = TestClient(app).get("/api/_test_cookies")
    assert resp.json()["code"] == "0"

    set_cookies = resp.headers.get_list("set-cookie")
    assert len(set_cookies) == 2
    assert any(c.startswith("a=1") for c in set_cookies)
    assert any(c.startswith("b=2") for c in set_cookies)


def test_process_time_header_on_non_api_path():
    """/health 不包装但同样带耗时头（中间件顺序：process_time 在内层先写头）。"""
    resp = TestClient(app).get("/health")
    assert float(resp.headers["x-process-time"]) >= 0


def test_wrapped_body_is_valid_json_of_original():
    """信封 data 与原响应体同构（json.loads 直通，不做额外序列化变换）。"""
    resp = TestClient(app).get("/api/_test_echo")
    envelope = json.loads(resp.text)
    assert envelope["data"] == json.loads(_ECHO_BODY)


# ---- 注册表对类式中间件的行为 ----

def test_middleware_registry_order_includes_logging():
    """注册表四项且顺序固定：process_time → logging → response → cors（越靠后越靠外）。

    cors（CORS）必须最靠外：预检 OPTIONS 在此短路（不进信封/日志内层），
    业务错误响应也带 CORS 头（前端才能读到错误体）；
    response（统一信封）在 cors 之内：最靠外的「业务中间件」，最后拿到响应
    才能读到 process_time 写的响应头并对响应体做信封包装；
    logging（请求日志）在 response 里面、process_time 外面：call_next 返回后
    立即记（含真实状态码），且不越过信封层改动响应体。
    """
    from app.core.middleware import MIDDLEWARES
    from app.core.middleware import cors
    from app.core.middleware import logging as logging_middleware
    from app.core.middleware import process_time, response

    assert MIDDLEWARES == [
        process_time.MIDDLEWARE,
        logging_middleware.MIDDLEWARE,
        response.MIDDLEWARE,
        cors.MIDDLEWARE,
    ]


# ---- CORS 中间件：读通用组 cors_origins，注册表最外层 ----

def test_cors_allows_whitelisted_origin():
    """白名单源（通用组默认 localhost:5173 双源）响应带 Access-Control-Allow-Origin。"""
    resp = TestClient(app).get("/health", headers={"Origin": "http://localhost:5173"})
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_cors_rejects_unknown_origin():
    """非白名单源不带 CORS 头（由浏览器侧拦截——CORS 协议语义，服务端照常应答）。"""
    resp = TestClient(app).get("/health", headers={"Origin": "https://evil.example"})
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


def test_cors_preflight_short_circuits_before_envelope():
    """预检 OPTIONS 在 CORS 层短路：不进 /api/ 信封（PlainText "OK" 不得被包成 JSON）。

    若 cors 被挪到信封内层，/api/ 路径的预检响应会被统一信封改写
    （json.loads("OK") 失败兜底成 500001）——按原生预检响应形态守住注册表顺序。
    """
    resp = TestClient(app).options(
        "/api/_test_echo",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text == "OK"


def test_register_middleware_supports_class_middleware(monkeypatch):
    """类式中间件（如 CORSMiddleware）走 add_middleware：用 CORS 头验证真实挂载。"""
    from fastapi import FastAPI
    from starlette.middleware.cors import CORSMiddleware

    from app.core.middleware import register_middleware

    fresh = FastAPI()

    @fresh.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"pong": True}

    monkeypatch.setattr(
        "app.core.middleware.MIDDLEWARES",
        [(CORSMiddleware, {"allow_origins": ["https://client.example"]})],
    )
    register_middleware(fresh)

    resp = TestClient(fresh).get(
        "/ping", headers={"Origin": "https://client.example"}
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "https://client.example"


# ---- 请求日志中间件：每请求恰一条请求日志 ----

def test_success_request_emits_exactly_one_success_log(log_records):
    """成功请求走 logging 中间件：SUCCESS 记录带 uuid4 request_id 与补齐的真实状态码。"""
    resp = TestClient(app).get("/health")
    assert resp.status_code == 200

    success = [r for r in log_records if r["level"].name == "SUCCESS"]
    assert len(success) == 1
    record = success[0]
    assert record["message"] == "请求完成"
    assert uuid.UUID(record["extra"]["request_id"]).version == 4  # request_id 是 uuid4
    assert record["extra"]["status_code"] == 200  # 中间件在 call_next 返回后补齐
    assert record["extra"]["method"] == "GET"
    assert record["extra"]["path"] == "/health"


def test_exception_request_does_not_emit_success_log(log_records):
    """异常请求不记 SUCCESS：同一个 request_log 由全局处理器按级落（业务异常 → warning）。"""
    resp = TestClient(app).get("/api/_test_not_found")
    assert resp.status_code == 404

    assert [r for r in log_records if r["level"].name == "SUCCESS"] == []
    warnings = [r for r in log_records if r["level"].name == "WARNING"]
    assert len(warnings) == 1
    assert warnings[0]["message"] == "业务异常: 商品不存在"
    assert warnings[0]["extra"]["status_code"] == 404
    assert warnings[0]["extra"]["path"] == "/api/_test_not_found"
    assert warnings[0]["exception"] is None  # 预期控制流不带栈
