"""模型清单域 DTO（api.md §3.1 GET /api/v1/models）：chat 模型切换器数据。

label 口径（api.md §3.1 v1.6）：options[].label 为服务端常量（key→品牌显示名映射
在后端一处常量定义，随 chat.options 键增删同步维护）；Nacos 数据只管 key→LiteLLM
别名（nacos-config.md §5），label 不进 Nacos，前端直接渲染返回值。
embedding/rerank/vision/light/memory 键不进此端点（PRD：不提供前端切换）。
"""
from pydantic import BaseModel, Field


class ModelOption(BaseModel):
    """模型选项（§3.1 data.options[]）：key 即建轮请求体 model 字段的合法值域。"""

    key: str = Field(description="模型键（chat.options 键域，随 Nacos 热更）")
    label: str = Field(description="品牌显示名（服务端常量，未收录键兜底显示 key）")


class ModelListResponse(BaseModel):
    """GET /models 响应 data（§3.1）：切换器选项集 + 缺省键。"""

    options: list[ModelOption] = Field(default_factory=list)
    default: str = Field(description="缺省模型键（chat.default，须存在于 options 键域）")
