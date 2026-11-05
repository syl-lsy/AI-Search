"""业务逻辑层基座：BaseService（共享会话）+ PageResult（泛型分页结果）。

用法约定（详见 references/service-layer.md）：
- 业务 service 一律继承 `BaseService`，由 api 层用依赖注入的会话构造
  （`CategoryService(db).list_categories(...)`），不自己建会话/引擎；
- **DTO 进 DTO 出**：service 方法入参/返回值都是 `app/schemas/` 里的 Pydantic DTO，
  ORM 模型不出 service 层（`XxxResponse.model_validate(orm_obj)` 就地转换）；
- **只 flush 不 commit**：增删改在 service 内 `await self.db.flush()`（拿到自增 id、
  触发约束校验），事务提交/回滚交给请求级会话依赖 `get_db` 统一处理；
- 请求内要做非库耗时工作（调外部 SDK/对象存储）前，按「短事务模式」显式
  `await self.db.commit()` + `await self.db.close()` 先释放数据库连接。
"""
from dataclasses import dataclass
from typing import Generic, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")


@dataclass
class PageResult(Generic[T]):
    """分页查询结果：service 层分页方法统一返回本类型（items 为 DTO 列表）。

    例：`async def list_categories(self, *, page: int, page_size: int) -> PageResult[CategoryResponse]`
    """

    items: list[T]
    total: int
    page: int
    page_size: int


class BaseService:
    """业务逻辑基类：所有 service 继承此类并共享同一 AsyncSession。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
