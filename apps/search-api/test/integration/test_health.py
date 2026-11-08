"""integration 层：就绪探针真库验证（api.md §1.2 就绪契约的真实依赖面）。

- DB 探针走真实 PostgreSQL（.env.test DB_ 组，conftest 导入期强注入）：验证
  SELECT 1 探活路径 + 契约响应形态在真实 app（真实中间件链、真实引擎）上成立；
- nacos / minio / litellm 三个非库探针在进程内替换为恒 ok 假体：真实外联探活属
  e2e/运维面——integration 不依赖 Nacos/MinIO/LiteLLM 存活（缺凭证/服务不在时
  不让命令红掉）；资产中心未启动（B2 才接 lifespan）经假体绕开 fail-fast。

缺 .env.test / 缺 DB_ 凭证 / 测试库不可达时经 test_db fixture pytest.skip（带 reason）。
"""

import pytest

from app.api.v1 import health


async def _ok() -> str:
    return "ok"


@pytest.mark.requires_db
async def test_health_ready_real_db_returns_200(
    async_client, monkeypatch, test_db
):
    """真库 + 非库探针假体 → 200，响应体逐字段对照契约 §1.2（db 组件为真探活结果）。"""
    for name in ("_probe_nacos", "_probe_minio", "_probe_litellm"):
        monkeypatch.setattr(health, name, _ok)
    resp = await async_client.get("/health/ready")
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
