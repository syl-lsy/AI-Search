"""图内信号 schema 与发射助手（B2 桩期约定——「图内信号 → TurnRunner 翻译」契约）。

承载形态（langgraph 1.x 惯例）：节点内经 `get_stream_writer()` 向 custom 流发射
dict 信号；TurnRunner 统一 `main_graph.astream(stream_mode=["messages","updates",
"custom"], subgraphs=True)`（ARCH §6.1）消费，custom 通道的 payload 按 `type`
翻译为 SSE 事件（EventBus publish + turn_events 落库）：

- `stage`   → SSE `stage` 事件（api.md §7.2；stage_key/status 枚举同契约）；
- `sources` → SSE `sources` 事件 + sources 表落库；
- `images`  → SSE `images` 事件 + turn_events 落库（A6，REQ-004——轮内至多一次、
  ≤3 张（OQ-6）、无图不发；条目 url 恒在、其余键可省——api.md v1.12）；
- `token`   → SSE `token` 事件，**不落库**（ADR-003 第 4 条）。

演进边界（B3/B4）：done 由 TurnRunner 落库终态后自发布（图不发 done）；
B4 接真模型后 synthesis/writer 的 token/thinking 增量改经 messages 流
（§6.1 messages 过滤规则），翻译器对 `type=token` 信号与 messages 增量归一；
路由节点亚毫秒、零 LLM·IO、不发任何信号。
"""

from typing import Any

from langgraph.config import get_stream_writer

STAGE = "stage"
SOURCES = "sources"
IMAGES = "images"
TOKEN = "token"


def emit_stage(
    stage_key: str,
    status: str,
    *,
    label: str | None = None,
    progress: dict[str, Any] | None = None,
    detail: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """发射 stage 信号（stage_key：rewrite|search|read|reflect|plan|write|code_exec）。

    label/progress/detail/extra 为 §7.2 stage 可选业务字段（B3 rewrite 起 extra 用于
    兜底留痕与 memory_directive 留档）；TurnRunner 吸收 progress/detail/extra 进
    turns.timing.stages 并透传 §2.5（B2 _absorb_stage 已支持）。
    """
    payload: dict[str, Any] = {"type": STAGE, "stage_key": stage_key, "status": status}
    if label is not None:
        payload["label"] = label
    if progress is not None:
        payload["progress"] = progress
    if detail is not None:
        payload["detail"] = detail
    if extra is not None:
        payload["extra"] = extra
    get_stream_writer()(payload)


def emit_sources(batch: list[dict[str, Any]], total: int) -> None:
    """发射 sources 信号（batch 条目结构与 api.md §7.2 sources 事件一致）。"""
    get_stream_writer()({"type": SOURCES, "batch": batch, "total": total})


def emit_images(images: list[dict[str, Any]]) -> None:
    """发射 images 信号（A6，REQ-004：api.md §7.2 图片卡片行——条目 url 恒在、
    source_url/title/width/height 键可省；调用方保证非空与 ≤3（OQ-6）——空集不发
    事件由 fuse_rerank 选图面裁决，本函数不做过滤）。"""
    get_stream_writer()({"type": IMAGES, "images": images})


def emit_token(delta: str) -> None:
    """发射 token 信号（Markdown 增量片段，直接拼接渲染；不入 turn_events）。"""
    get_stream_writer()({"type": TOKEN, "delta": delta})
