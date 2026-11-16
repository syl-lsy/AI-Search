"""unit 层：线程对象 best-effort 清理（app/services/object_cleanup.py）。

覆盖点（api.md §2.11 v1.18 / D-04）：
- 前缀口径 = `snapshots/{user_id}/{thread_id}/`（data-model §1.6 object_key 格式）；
- 有对象 → list_objects + remove_objects（桶取 minio_settings.bucket_snapshot）；
- **minio-py `remove_objects` 是惰性 generator**：调用只构造迭代器、迭代才发 HTTP
  （官方用法 `for err in client.remove_objects(...)`）——假体按同形态建模
  （invocations/iterations 双计数），生产实现必须强制消费，否则清理静默零操作；
- **载荷形态必须是 DeleteObject**：minio-py 7.2.x 的 `DeleteRequest.toxml()` 对每项调
  `obj.toxml(element)`（src/minio/deleteobjects.py）——传 dict 会在消费点
  `AttributeError: 'dict' object has no attribute 'toxml'`；`DeleteObject` 只在
  `minio.deleteobjects` 子模块导出（包根无）。假体按 `.name` **属性**取键并要求
  `toxml` 可调用，dict/裸命名空间载荷直接红（形态漂移锁进测试；SDK 未安装期用同形态
  假模块注入 `DeleteObject`）；
- 迭代产出的 `DeleteError`（逐对象失败）→ 逐条 WARN（带 object key / code / message）；
- 无对象 → 跳过 remove（零副作用）；
- 任何失败（客户端构造/列举/删除）只 WARN 不抛——best-effort 边界；
- 缺省工厂在 minio SDK 未安装期同样落 WARN 分支（不阻断删除主流程）。
零网络：MinIO 客户端全部假体注入。
"""
from types import SimpleNamespace

import pytest

from app.core.config import minio_settings
from app.services.object_cleanup import MinioThreadObjectPurger

USER_ID = "00000000-0000-4000-8000-000000000001"
THREAD_ID = "7d2f0a1e-0000-4000-8000-000000000002"


class _Obj:
    def __init__(self, name: str) -> None:
        self.object_name = name


class _DeleteError:
    """minio-py DeleteError 形态（code/message/name）——remove_objects 逐条 yield。"""

    def __init__(self, name: str, code: str, message: str) -> None:
        self.name = name
        self.code = code
        self.message = message


class _FakeDeleteObject:
    """`minio.deleteobjects.DeleteObject` 最小形态：`name` 属性 + 可调用 `toxml`。

    真 SDK 的 `toxml(element)` 由 `DeleteRequest.toxml()` 对每项调用——假体只要求可调用
    （载荷形态契约），不模拟 XML 生成。
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def toxml(self, element: object) -> object:  # noqa: ARG002 —— 形态占位
        return element


@pytest.fixture(autouse=True)
def _fake_minio_deleteobjects(monkeypatch: pytest.MonkeyPatch):
    """minio SDK 未随 0002 落地（运行依赖无 minio）：为 `minio.deleteobjects` 提供同形态假模块。"""
    import app.services.object_cleanup as module

    real_import = module.importlib.import_module
    fake_deleteobjects = SimpleNamespace(DeleteObject=_FakeDeleteObject)

    def _import(name: str):
        if name == "minio.deleteobjects":
            return fake_deleteobjects
        return real_import(name)

    monkeypatch.setattr(module.importlib, "import_module", _import)


class _FakeMinioClient:
    """minio-py 客户端假体：`remove_objects` 为**惰性 generator**（与真 SDK 同形态）。

    真 SDK 的 `remove_objects` 是生成器函数：调用只返回迭代器，批量删除的 HTTP 请求在
    首次迭代时才发出。假体用 `remove_invocations`（调用数）与 `remove_iterations`
    （真的发请求数）两个计数把这两件事分开——否则「不迭代也删除」会被测试锁成假绿。

    载荷按 `.name` **属性**取键并校验 `toxml` 可调用：与真 SDK 的 `toxml` 消费路径同形态
    （dict 载荷 → `AttributeError`，测试即红——防「假体接受 dict、真 SDK 报错」的漂移）。
    """

    def __init__(
        self,
        *,
        objects: list[str],
        fail_list: bool = False,
        delete_errors: list[tuple[str, str, str]] | None = None,
    ) -> None:
        self.objects = objects
        self.fail_list = fail_list
        self.list_calls: list[tuple[str, str, bool]] = []
        self.remove_invocations = 0
        self.remove_iterations = 0
        self.sent_requests: list[tuple[str, list[str]]] = []
        self._delete_errors = list(delete_errors or [])

    def list_objects(self, bucket: str, prefix: str, recursive: bool):
        self.list_calls.append((bucket, prefix, recursive))
        if self.fail_list:
            raise RuntimeError("minio list down")
        return [_Obj(name) for name in self.objects]

    def remove_objects(self, bucket: str, objects: list[object]):
        self.remove_invocations += 1

        def _gen():
            # 迭代点 = 真 SDK 的 HTTP 发出点（也是 DeleteRequest.toxml 的消费点）
            self.remove_iterations += 1
            keys: list[str] = []
            for obj in objects:
                name = obj.name  # type: ignore[attr-defined] —— 非 DeleteObject 载荷在此红
                if not callable(getattr(obj, "toxml", None)):
                    raise TypeError(
                        "minio remove_objects 载荷必须是 DeleteObject（带 toxml），"
                        f"收到 {type(obj).__name__}"
                    )
                keys.append(name)
            self.sent_requests.append((bucket, keys))
            for name, code, message in self._delete_errors:
                yield _DeleteError(name, code, message)

        return _gen()


def test_fake_remove_objects_is_lazy_until_iterated():
    """假体形态自证（对齐 minio-py）：不迭代零请求、迭代后才发请求。"""
    client = _FakeMinioClient(objects=["a.md"])

    iterator = client.remove_objects("bucket", [_FakeDeleteObject("a.md")])
    assert client.remove_invocations == 1
    assert client.remove_iterations == 0  # 未迭代 → 零请求
    assert client.sent_requests == []

    assert list(iterator) == []  # 迭代（无错误）
    assert client.remove_iterations == 1
    assert client.sent_requests == [("bucket", ["a.md"])]


def test_fake_remove_objects_rejects_non_delete_object_payload():
    """形态断言：dict / 裸命名空间载荷被假体拒收（真 SDK 在 toxml 处同样报错）。"""
    client = _FakeMinioClient(objects=["a.md"])

    with pytest.raises(AttributeError):  # dict 无 .name（真 SDK 是 toxml 缺失）
        list(client.remove_objects("bucket", [{"name": "a.md"}]))
    with pytest.raises(TypeError):  # 有 name 但无 toxml → 明确拒收
        list(client.remove_objects("bucket", [SimpleNamespace(name="a.md")]))
    assert client.remove_iterations == 2  # 两次都在迭代点触发拒收（未静默通过）


async def test_purger_lists_and_removes_objects_under_thread_prefix():
    client = _FakeMinioClient(objects=["a.md", "b.md"])
    purger = MinioThreadObjectPurger(client_factory=lambda: client)

    await purger.purge_thread_objects(user_id=USER_ID, thread_id=THREAD_ID)

    # 前缀含 user_id/thread_id 双段（REQ-011 前缀口径，防跨用户误删）
    assert client.list_calls == [
        (minio_settings.bucket_snapshot, f"snapshots/{USER_ID}/{THREAD_ID}/", True)
    ]
    # P1：生产实现必须迭代 generator（不迭代 = 真 SDK 下静默零操作）
    assert client.remove_invocations == 1
    assert client.remove_iterations == 1
    assert client.sent_requests == [
        (minio_settings.bucket_snapshot, ["a.md", "b.md"])
    ]


async def test_purger_skips_remove_when_no_objects():
    client = _FakeMinioClient(objects=[])
    purger = MinioThreadObjectPurger(client_factory=lambda: client)

    await purger.purge_thread_objects(user_id=USER_ID, thread_id=THREAD_ID)

    assert client.remove_invocations == 0  # 空前缀零删除调用


async def test_purger_logs_warn_per_delete_error(log_records):
    """逐对象 DeleteError（迭代产出）→ 逐条 WARN（带 object key 与 error message）。"""
    key = f"snapshots/{USER_ID}/{THREAD_ID}/deadbeef.md"
    client = _FakeMinioClient(
        objects=[key],
        delete_errors=[(key, "AccessDenied", "Access Denied.")],
    )
    purger = MinioThreadObjectPurger(client_factory=lambda: client)

    await purger.purge_thread_objects(user_id=USER_ID, thread_id=THREAD_ID)

    # 删除请求确实发出（迭代发生）且逐条错误被记录
    assert client.remove_iterations == 1
    warnings = [r for r in log_records if r["level"].name == "WARNING"]
    assert len(warnings) == 1, warnings
    message = warnings[0]["message"]
    assert key in message and "AccessDenied" in message and "Access Denied." in message


async def test_purger_failure_logs_warn_without_raising(log_records):
    client = _FakeMinioClient(objects=[], fail_list=True)
    purger = MinioThreadObjectPurger(client_factory=lambda: client)

    await purger.purge_thread_objects(user_id=USER_ID, thread_id=THREAD_ID)

    warnings = [r for r in log_records if r["level"].name == "WARNING"]
    assert warnings, "清理失败必须落 WARN（best-effort 可观测）"
    assert THREAD_ID in warnings[0]["extra"].get("entity_id", "")


async def test_purger_client_factory_failure_is_swallowed(log_records):
    def _broken_factory():
        raise RuntimeError("minio 凭据未配置")

    purger = MinioThreadObjectPurger(client_factory=_broken_factory)

    await purger.purge_thread_objects(user_id=USER_ID, thread_id=THREAD_ID)  # 不抛

    assert [r for r in log_records if r["level"].name == "WARNING"]


@pytest.mark.parametrize("missing_sdk", [True, False])
async def test_default_factory_never_raises(log_records, monkeypatch, missing_sdk):
    """缺省工厂边界：SDK 未安装（ImportError）与 SDK 就绪但端点不可达均只 WARN。"""
    import app.services.object_cleanup as module

    if missing_sdk:

        def _raise_import(name: str):
            raise ModuleNotFoundError(f"No module named {name!r}")

        monkeypatch.setattr(module.importlib, "import_module", _raise_import)
        purger = MinioThreadObjectPurger()
    else:
        purger = MinioThreadObjectPurger(
            client_factory=lambda: _FakeMinioClient(objects=[], fail_list=True)
        )

    await purger.purge_thread_objects(user_id=USER_ID, thread_id=THREAD_ID)  # 不抛

    assert [r for r in log_records if r["level"].name == "WARNING"]


async def test_default_factory_constructs_client_from_settings(monkeypatch, log_records):
    """缺省工厂装配：endpoint/凭据/secure 全部取自 core/config 单例（密钥零入码）。"""
    import app.services.object_cleanup as module

    seen: dict[str, object] = {}

    class _Minio:
        def __init__(self, endpoint, *, access_key, secret_key, secure):
            seen.update(
                endpoint=endpoint,
                access_key=access_key,
                secret_key=secret_key,
                secure=secure,
            )

        def list_objects(self, bucket: str, prefix: str, recursive: bool):
            return []

        def remove_objects(self, bucket: str, names: list[dict[str, str]]):
            raise AssertionError("空前缀不应调用 remove_objects")

    class _FakeModule:
        Minio = _Minio

    monkeypatch.setattr(module.importlib, "import_module", lambda name: _FakeModule)
    purger = MinioThreadObjectPurger()

    await purger.purge_thread_objects(user_id=USER_ID, thread_id=THREAD_ID)

    assert seen["endpoint"] == minio_settings.endpoint
    assert seen["secure"] is minio_settings.secure
    # SecretStr 解包为明文后才交给 SDK（此处只断言类型，不落断言值——密钥不入产物）
    assert isinstance(seen["access_key"], str) and isinstance(seen["secret_key"], str)
    assert not [r for r in log_records if r["level"].name == "WARNING"]
