"""models 路由（api.md §3.1）：chat 模型切换器数据（REQ-010）。

读 Nacos model.mapping AssetSnapshot 的 chat.options/default 返回 options+default；
资产不可用（未启动/快照缺失）→ ConfigCenterException 503001 经 get_model_mapping
依赖自然冒泡（路由层零捕获）。

label 口径（api.md §3.1 v1.6）：options[].label 为**服务端常量**——key→品牌显示名
的映射在后端一处常量定义（本文件 _MODEL_LABELS），随 chat.options 键增删同步维护；
Nacos 数据只管 key→LiteLLM 别名（nacos-config.md §5），label 不进 Nacos，前端不写死、
不做任何拼接或回退逻辑（直接渲染返回值）。embedding/rerank/vision/light/memory 键
不进此端点（PRD：不提供前端切换）。
"""
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.dependencies import get_model_mapping
from app.schemas import ModelListResponse, ModelOption

router = APIRouter(prefix="/api/v1")

# 服务端常量（api.md §3.1 v1.6 示例值）：key → 品牌显示名；chat.options 新增键时
# 在此同步维护。未收录键兜底显示 key 本身（不拼不猜品牌名，前端直接渲染）。
_MODEL_LABELS: dict[str, str] = {
    "qwen": "Qwen",
    "glm": "GLM",
    "deepseek": "DeepSeek",
}


@router.get("/models", response_model=ModelListResponse)
async def list_models(
    mapping: Annotated[Mapping[str, Any], Depends(get_model_mapping)],
) -> ModelListResponse:
    """chat 模型切换器数据（api.md §3.1）：零 LLM 调用、零 DB 访问（快照内存直读）。"""
    chat = mapping.get("chat") or {}
    options: dict[str, str] = dict(chat.get("options") or {})
    return ModelListResponse(
        options=[
            ModelOption(key=key, label=_MODEL_LABELS.get(key, key)) for key in options
        ],
        default=str(chat.get("default", "")),
    )
