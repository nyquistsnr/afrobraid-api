from typing import Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

ItemT = TypeVar("ItemT")


class PaginationParams:
    def __init__(
        self,
        page: int = Query(1, ge=1, description="1-indexed page number"),
        page_size: int = Query(20, ge=1, le=100, description="Items per page (max 100)"),
    ) -> None:
        self.page = page
        self.page_size = page_size

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


class PaginationMeta(BaseModel):
    page: int
    page_size: int
    total_items: int
    total_pages: int
    has_next: bool
    has_previous: bool


class PaginatedData(BaseModel, Generic[ItemT]):
    items: list[ItemT]
    pagination: PaginationMeta


async def paginate(
    db: AsyncSession, stmt: Select, params: PaginationParams
) -> tuple[list, PaginationMeta]:
    """Runs a COUNT alongside a LIMIT/OFFSET'd copy of `stmt`.

    `order_by(None)` on the count subquery drops any ORDER BY from `stmt` -
    irrelevant to a COUNT and some backends reject it in a subquery.

    The page is fetched first: a non-empty, partially filled page is the last
    one, so the total is known without a second (often costlier) COUNT query.
    """
    result = await db.execute(stmt.limit(params.page_size).offset(params.offset))
    items = list(result.scalars().all())

    if len(items) < params.page_size and (items or params.offset == 0):
        total_items = params.offset + len(items)
    else:
        total_items = (
            await db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
        ) or 0

    total_pages = -(-total_items // params.page_size) if total_items else 0

    meta = PaginationMeta(
        page=params.page,
        page_size=params.page_size,
        total_items=total_items,
        total_pages=total_pages,
        has_next=params.page < total_pages,
        has_previous=params.page > 1,
    )
    return items, meta
