"""unit 层：KB IR 存储（M3 批次 1 / B16——data-model.md §1.7 :270 键约定、§1.18 :543
存储落位、§1.18 :544 装载钉注；TDD 红绿，unit 全假体零网络零 DB）。

钉住的语义（冻结契约，逐条对应测试）：
- **对象键约定**（§1.7 :270 / §1.18 :543）：`kb/{user_id}/{doc_id}/ir.json` 与
  `kb/{user_id}/{doc_id}/original.{ext}`（URL 类型存抓取正文 md——键函数纯形态化，
  ext 允许带/不带前导点、统一小写归一；白名单 ∈ {pdf,md,txt,docx}
  （api.md §4.1）外 → ValueError 先于任何 IO——评审 P3 对象键卫生面）；
- **kb 桶**：`minio_settings.bucket_kb`（app/core/config.py MinioSettings）；
- **装载钉注**（§1.18 :544）：`get_ir_json` 返回前**必须**由 children 数组序
  `rebuild_siblings` 重建 prev/next（下钻链路内置，调用方零负担）；
- **失败语义**（save_snapshot.SnapshotError 先例）：IO 面失败归一 `IrStorageError`
  （原异常 raise from 链入）；标识非法（非 UUID）→ ValueError（编程错误，先于任何 IO）；
- **惰性工厂**：SDK 运行期 importlib 解析（save_snapshot._default_minio_client_factory /
  object_cleanup 同款）——Minio 构造为离线操作，unit 可直测工厂产出形态。
"""

import importlib
import uuid
from typing import Any

import pytest

from app.core.config import minio_settings
from app.rag.ir_schema import IrNode, rebuild_siblings, serialize_ir
from app.rag.storage import (
    IrStorageError,
    get_ir_json,
    get_kb_minio_client,
    ir_object_key,
    original_object_key,
    put_ir_json,
)


class _FakeResponse:
    """minio get_object 返回的 urlopen 形态（.read()）。"""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        pass

    def release_conn(self) -> None:
        pass


class _FakeMinio:
    """内存假体：记录 put 面、供给 get 面（零网络）。"""

    def __init__(self) -> None:
        self.buckets: set[str] = set()
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_calls: list[tuple[str, str, int, str | None]] = []

    def bucket_exists(self, bucket: str) -> bool:
        return bucket in self.buckets

    def make_bucket(self, bucket: str) -> None:
        self.buckets.add(bucket)

    def put_object(
        self,
        bucket: str,
        key: str,
        stream: Any,
        length: int,
        content_type: str | None = None,
    ) -> None:
        data = stream.read(length)
        self.objects[(bucket, key)] = data
        self.put_calls.append((bucket, key, length, content_type))

    def get_object(self, bucket: str, key: str) -> _FakeResponse:
        if (bucket, key) not in self.objects:
            raise KeyError(f"object not found: {key}")
        return _FakeResponse(self.objects[(bucket, key)])

    def remove_object(self, bucket: str, key: str) -> None:
        self.objects.pop((bucket, key), None)


def _tree() -> IrNode:
    doc = IrNode(
        id="fs:node:d1",
        type="document",
        title="根文档",
        doc_kind="docx",
        catalog_ids=["fs:node:anc"],
        catalog_path=["祖先"],
        loc={"origin": "feishu", "space_id": "7708", "node_token": "tokd1"},
        meta={"raw_type": "docx"},
    )
    a = IrNode(id="fs:blk:a", parent_id="fs:node:d1", type="paragraph", text="甲", meta={"raw_type": 2})
    b = IrNode(id="fs:blk:b", parent_id="fs:node:d1", type="paragraph", text="乙", meta={"raw_type": 2})
    doc.children = [a, b]
    return doc


# ---------------------------------------------------------------------------
# 对象键纯函数（§1.7 :270）
# ---------------------------------------------------------------------------


def test_ir_object_key_exact_shape() -> None:
    u, d = str(uuid.uuid4()), str(uuid.uuid4())
    assert ir_object_key(u, d) == f"kb/{u}/{d}/ir.json"


def test_original_object_key_exact_shape_with_and_without_leading_dot() -> None:
    u, d = str(uuid.uuid4()), str(uuid.uuid4())
    assert original_object_key(u, d, "pdf") == f"kb/{u}/{d}/original.pdf"
    assert original_object_key(u, d, ".pdf") == f"kb/{u}/{d}/original.pdf"


def test_original_object_key_normalizes_case_and_strips_dot() -> None:
    """ext 统一小写 + 去前导点归一（白名单比对前的卫生归一）。"""
    u, d = str(uuid.uuid4()), str(uuid.uuid4())
    assert original_object_key(u, d, ".PDF") == f"kb/{u}/{d}/original.pdf"
    assert original_object_key(u, d, "MD") == f"kb/{u}/{d}/original.md"


@pytest.mark.parametrize("bad_ext", ["exe", "sh", ".tar.gz", "pdf/../../x", "pdf.", "", " "])
def test_original_object_key_rejects_ext_outside_whitelist(bad_ext: str) -> None:
    """白名单外（含含路径分隔/多余点的非常规 ext）→ ValueError 先于任何 IO
    （api.md §4.1 扩展名白名单；评审 P3——防拼出含路径分隔的对象键）。"""
    with pytest.raises(ValueError, match="白名单"):
        original_object_key(str(uuid.uuid4()), str(uuid.uuid4()), bad_ext)


# ---------------------------------------------------------------------------
# 惰性工厂（save_snapshot 先例形态）
# ---------------------------------------------------------------------------


def test_lazy_factory_builds_offline_client_with_settings() -> None:
    """Minio 构造是离线操作：工厂可直测——SDK 运行期 importlib 解析 + settings 单例
    注入构造成功即锁工厂形态（凭据/端点接线由 integration 默认路径真连接兜底）。"""
    minio = importlib.import_module("minio")
    client = get_kb_minio_client()
    assert isinstance(client, minio.Minio)


# ---------------------------------------------------------------------------
# put_ir_json / get_ir_json（假体注入，锁 bucket/key/序列化面/失败归一）
# ---------------------------------------------------------------------------


async def test_put_ir_json_writes_kb_bucket_and_exact_key() -> None:
    fake = _FakeMinio()
    u, d = str(uuid.uuid4()), str(uuid.uuid4())
    key = await put_ir_json(u, d, _tree(), client=fake)
    assert key == ir_object_key(u, d)
    assert fake.buckets == {minio_settings.bucket_kb}  # 桶=kb 桶（config.py bucket_kb）
    bucket, obj_key, length, content_type = fake.put_calls[0]
    assert (bucket, obj_key) == (minio_settings.bucket_kb, key)
    assert content_type == "application/json"
    assert length > 0


async def test_put_payload_has_no_sibling_pointers() -> None:
    tree = _tree()
    rebuild_siblings(tree)
    fake = _FakeMinio()
    await put_ir_json(str(uuid.uuid4()), str(uuid.uuid4()), tree, client=fake)
    payload = b"".join(fake.objects.values()).decode("utf-8")
    assert '"prev"' not in payload and '"next"' not in payload


async def test_get_ir_json_roundtrips_and_rebuilds_siblings() -> None:
    fake = _FakeMinio()
    u, d = str(uuid.uuid4()), str(uuid.uuid4())
    tree = _tree()
    rebuild_siblings(tree)
    await put_ir_json(u, d, tree, client=fake)
    back = await get_ir_json(u, d, client=fake)
    assert back.model_dump() == tree.model_dump()
    # 装载钉注（§1.18 :544）：get_ir_json 返回前已由 children 数组序重建 prev/next
    assert back.children[0].next is back.children[1]
    assert back.children[1].prev is back.children[0]


async def test_get_ir_json_serves_stored_bytes_via_serialize() -> None:
    fake = _FakeMinio()
    u, d = str(uuid.uuid4()), str(uuid.uuid4())
    tree = _tree()
    await put_ir_json(u, d, tree, client=fake)
    stored = fake.objects[(minio_settings.bucket_kb, ir_object_key(u, d))]
    assert serialize_ir(tree).encode("utf-8") == stored


async def test_put_failure_normalized_to_ir_storage_error() -> None:
    class _BoomClient:
        def bucket_exists(self, bucket: str) -> bool:
            return False

        def make_bucket(self, bucket: str) -> None:
            raise OSError("boom")

    with pytest.raises(IrStorageError) as excinfo:
        await put_ir_json(str(uuid.uuid4()), str(uuid.uuid4()), _tree(), client=_BoomClient())
    assert excinfo.value.__cause__ is not None  # 原异常链入


class _RaceBucketClient:
    """并发首写同桶竞态假体：bucket_exists=False + make_bucket 抛桶已存在错误
    （评审 P3——bucket_exists→make_bucket 两步非原子，后到方须继续写不失败）。"""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.put_calls: list[tuple[str, str]] = []
        self.objects: dict[tuple[str, str], bytes] = {}

    def bucket_exists(self, bucket: str) -> bool:
        return False

    def make_bucket(self, bucket: str) -> None:
        raise self._exc

    def put_object(
        self,
        bucket: str,
        key: str,
        stream: Any,
        length: int,
        content_type: str | None = None,
    ) -> None:
        self.objects[(bucket, key)] = stream.read(length)
        self.put_calls.append((bucket, key))


async def test_put_survives_bucket_already_owned_by_you_race() -> None:
    """minio-py 7.x 形态：桶已存在=S3Error.code（BucketAlreadyOwnedByYou）→
    视为建桶成功继续 put，零异常零 IrStorageError。"""
    error_mod = importlib.import_module("minio.error")
    race = _RaceBucketClient(
        error_mod.S3Error(None, "BucketAlreadyOwnedByYou", "msg", "res", "rid", "hid")
    )
    u, d = str(uuid.uuid4()), str(uuid.uuid4())
    key = await put_ir_json(u, d, _tree(), client=race)
    assert race.put_calls == [(minio_settings.bucket_kb, key)]  # 竞态后照常写入


async def test_put_survives_legacy_class_named_bucket_already_exists() -> None:
    class BucketAlreadyExists(Exception):  # 旧版 minio-py 独立异常类形态（类名兜底路径）
        pass

    race = _RaceBucketClient(BucketAlreadyExists("bucket already exists"))
    u, d = str(uuid.uuid4()), str(uuid.uuid4())
    key = await put_ir_json(u, d, _tree(), client=race)
    assert race.put_calls == [(minio_settings.bucket_kb, key)]


async def test_get_missing_object_normalized_to_ir_storage_error() -> None:
    fake = _FakeMinio()
    fake.buckets.add(minio_settings.bucket_kb)
    with pytest.raises(IrStorageError):
        await get_ir_json(str(uuid.uuid4()), str(uuid.uuid4()), client=fake)


@pytest.mark.parametrize("bad", ["", "not-a-uuid", "kb/doc"])
async def test_invalid_identifiers_rejected_before_io(bad: str) -> None:
    fake = _FakeMinio()
    with pytest.raises(ValueError):
        await put_ir_json(bad, str(uuid.uuid4()), _tree(), client=fake)
    with pytest.raises(ValueError):
        await get_ir_json(bad, str(uuid.uuid4()), client=fake)
    assert not fake.objects  # 零网络副作用
