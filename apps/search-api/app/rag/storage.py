"""KB IR 对象存储（M3 批次 1 / B16）——MinIO kb 桶读写 ir.json。

契约锚点：data-model.md §1.7 :270（`kb/{user_id}/{doc_id}/original.{ext}` 键约定）、
§1.18 :543（整树 JSON 落 MinIO `kb/{user_id}/{doc_id}/ir.json`，kb 桶与原件同级；
pg 不存整树 jsonb、不建节点表）、§1.18 :544（**装载钉注**：ir.json 无 prev/next，
下钻装载由 children 数组序 `rebuild_siblings` 重建——`get_ir_json` 内置此步）。

先例形态：`app/agent/tools/save_snapshot.py`——惰性 MinIO 工厂（SDK 运行期 importlib
解析）、自兜桶存在性（compose 无建桶初始化容器）、同步 SDK 调用 asyncio.to_thread 下放、
IO 面失败归一（SnapshotError → 本模块 IrStorageError，原异常 raise from 链入）；
标识非法 → ValueError（编程错误，先于任何 IO 不归一）。

凭据纪律：端点/密钥只从 `minio_settings` 单例（.env MINIO_* 组）读取，凭据零打印零
落盘、零硬编码（配置三分法：敏感 → 永远 .env）。
"""

import asyncio
import importlib
import io
import re
import uuid
from typing import Any

from app.core.config import minio_settings
from app.rag.ir_schema import IrNode, deserialize_ir, rebuild_siblings, serialize_ir

IR_JSON_CONTENT_TYPE = "application/json"


class IrStorageError(Exception):
    """ir.json 对象存储 IO 面失败归一（原异常经 raise from 链入；零内容回显）。"""


def ir_object_key(user_id: str, doc_id: str) -> str:
    """ir.json 对象键（§1.18 :543）：`kb/{user_id}/{doc_id}/ir.json`。"""
    return f"kb/{user_id}/{doc_id}/ir.json"


#: 原件扩展名白名单（api.md §4.1 :507「入口即校验：扩展名 ∈ pdf/md/txt/docx」；
#: URL 类型存抓取正文 md 同集）。评审 P3：ext 零校验可拼出含路径分隔的键——
#: MinIO 对象键为扁平字符串无服务端穿越风险，本断言纯对象键卫生面。
ORIGINAL_EXT_WHITELIST: frozenset[str] = frozenset({"pdf", "md", "txt", "docx"})


def original_object_key(user_id: str, doc_id: str, ext: str) -> str:
    """原件对象键（§1.7 :270）：`kb/{user_id}/{doc_id}/original.{ext}`（ext 带不带
    前导点均接受、统一小写归一；白名单外 → ValueError——编程错误先于任何 IO
    不归一，同 `_validate_ids` 口径；URL 类型存抓取正文 md）。"""
    normalized = ext.lstrip(".").lower()
    if normalized not in ORIGINAL_EXT_WHITELIST:
        raise ValueError(
            f"ext 须 ∈ {sorted(ORIGINAL_EXT_WHITELIST)}（api.md §4.1 白名单）：{ext!r}"
        )
    return f"kb/{user_id}/{doc_id}/original.{normalized}"


def get_kb_minio_client() -> Any:
    """MinIO 客户端惰性构造（save_snapshot._default_minio_client_factory /
    object_cleanup 同款：SDK 运行期 importlib 解析；Minio 构造为离线操作）。
    """
    minio = importlib.import_module("minio")
    return minio.Minio(
        minio_settings.endpoint,
        access_key=minio_settings.access_key.get_secret_value(),
        secret_key=minio_settings.secret_key.get_secret_value(),
        secure=minio_settings.secure,
    )


def _validate_ids(user_id: str, doc_id: str) -> None:
    """标识须为合法 UUID（kb_documents/用户 id 全局口径——ORM 客户端 uuid4）。"""
    try:
        uuid.UUID(str(user_id))
        uuid.UUID(str(doc_id))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"标识须为合法 UUID：{exc}") from exc


#: minio-py 7.x 无 BucketAlready* 独立异常类——两值为 S3Error 的 code 错误码
#: （评审 P3：bucket_exists→make_bucket 两步非原子，并发首写同桶时后到方收到
#: 此二码=桶已被并发方建成，非失败；类名兜底覆盖 SDK 演进面〔旧版独立异常类〕）
_BUCKET_ALREADY_EXISTS_CODES: frozenset[str] = frozenset(
    {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}
)


def _is_bucket_already_exists(exc: Exception) -> bool:
    """并发首写同桶竞态判定：S3 错误码=桶已被（本账号）建成 → 视为建桶成功。"""
    error_mod = importlib.import_module("minio.error")
    s3_error = getattr(error_mod, "S3Error", None)
    if s3_error is not None and isinstance(exc, s3_error):
        return getattr(exc, "code", None) in _BUCKET_ALREADY_EXISTS_CODES
    return type(exc).__name__ in _BUCKET_ALREADY_EXISTS_CODES


def _ensure_bucket_and_put(client: Any, object_key: str, payload: str) -> None:
    """建桶（缺失时，compose 无建桶初始化容器——工具侧自兜）+ 全量对象写入。

    并发首写同桶竞态兜底（评审 P3，B17 并行入库流水线前置）：桶已存在类错误
    视为建桶成功继续写，其余异常原样上抛（调用方归一 IrStorageError）。
    """
    _ensure_bucket_and_put_bytes(client, object_key, payload.encode("utf-8"), IR_JSON_CONTENT_TYPE)


def _ensure_bucket_and_put_bytes(client: Any, object_key: str, data: bytes, content_type: str) -> None:
    """建桶 + 字节载荷写入（put_kb_object 与 ir.json 共用的底层通道）。"""
    bucket = minio_settings.bucket_kb
    if not client.bucket_exists(bucket):
        try:
            client.make_bucket(bucket)
        except Exception as exc:
            if not _is_bucket_already_exists(exc):
                raise
    client.put_object(
        bucket,
        object_key,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type,
    )


def _get_object_bytes(client: Any, object_key: str) -> bytes:
    """全量对象读取（同步 SDK 面，调用方 to_thread 下放）。"""
    response = client.get_object(minio_settings.bucket_kb, object_key)
    try:
        return response.read()
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
        release = getattr(response, "release_conn", None)
        if callable(release):
            release()


async def put_ir_json(user_id: str, doc_id: str, root: IrNode, *, client: Any | None = None) -> str:
    """整树序列化（无 prev/next——ir_schema 装载钉注）后落 kb 桶；返回对象键。

    client 注入面供测试假体/复用既有连接；缺省走 `get_kb_minio_client`（生产形态）。
    IO 失败 → IrStorageError（原异常链入）；标识非法 → ValueError（先于任何 IO）。
    """
    _validate_ids(user_id, doc_id)
    object_key = ir_object_key(user_id, doc_id)
    payload = serialize_ir(root)
    minio_client = client if client is not None else get_kb_minio_client()
    try:
        await asyncio.to_thread(_ensure_bucket_and_put, minio_client, object_key, payload)
    except Exception as exc:
        raise IrStorageError(f"ir.json 写入失败：{type(exc).__name__}（key={object_key}）") from exc
    return object_key


async def get_ir_json(user_id: str, doc_id: str, *, client: Any | None = None) -> IrNode:
    """拉取单文档 ir.json → IrNode。

    **装载钉注（§1.18 :544）内置**：返回前已由 children 数组序 `rebuild_siblings`
    重建 prev/next 兄弟指针——下钻链路直接消费，调用方零负担。
    IO 失败 → IrStorageError；标识非法 → ValueError。
    """
    _validate_ids(user_id, doc_id)
    object_key = ir_object_key(user_id, doc_id)
    minio_client = client if client is not None else get_kb_minio_client()
    try:
        raw = await asyncio.to_thread(_get_object_bytes, minio_client, object_key)
    except Exception as exc:
        raise IrStorageError(f"ir.json 读取失败：{type(exc).__name__}（key={object_key}）") from exc
    node = deserialize_ir(raw)
    return rebuild_siblings(node)


#: kb 桶通用键卫生（B20 附件/图片素材通道）：`kb/{uuid}/{uuid}/<尾段非空>`——前两段
#: 必须为合法 UUID（与 ir_object_key/original_object_key 同口径；尾段由调用方保证无
#: 穿越形态，feishu_client.image_material_object_key 另有 block_id 白名单校验）
_KB_KEY_PREFIX_RE = re.compile(r"^kb/([^/]+)/([^/]+)/.+")


async def put_kb_object(
    object_key: str,
    data: bytes,
    *,
    content_type: str = "application/octet-stream",
    client: Any | None = None,
) -> str:
    """kb 桶通用字节写入口（B20 附件/图片素材落 MinIO 通道——键由调用方按既有
    约定构造：附件 `original_object_key`、图片素材 `kb/{u}/{d}/img/{block_id}.{ext}`）。

    键前两段须为合法 UUID（键卫生，先于任何 IO 归一 ValueError）；
    IO 失败 → IrStorageError（原异常链入）。client 注入面供测试假体/复用既有连接。
    """
    m = _KB_KEY_PREFIX_RE.match(object_key)
    if m is None:
        raise ValueError(f"kb 桶键须为 kb/{{user_id}}/{{doc_id}}/<尾段> 形态：{object_key!r}")
    _validate_ids(m.group(1), m.group(2))
    minio_client = client if client is not None else get_kb_minio_client()
    try:
        await asyncio.to_thread(_ensure_bucket_and_put_bytes, minio_client, object_key, data, content_type)
    except Exception as exc:
        raise IrStorageError(f"kb 对象写入失败：{type(exc).__name__}（key={object_key}）") from exc
    return object_key
