import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.pagination import PaginatedData, PaginationParams
from app.core.response import APIResponse
from app.modules.auth.dependencies import require_roles
from app.modules.users import service
from app.modules.users.models import User, UserType
from app.modules.users.schemas import (
    AdminUserResponse,
    AdminUserStatsResponse,
    SuspendUserRequest,
)

router = APIRouter(prefix="/api/v1/admin/users", tags=["Admin - Users"])

_require_admin = require_roles(UserType.ADMIN)


@router.get(
    "",
    response_model=APIResponse[PaginatedData[AdminUserResponse]],
    summary="List users",
    description="Filter by `user_type` (CUSTOMER, BRAIDER, or ADMIN); omit for every user.",
)
async def list_users(
    user_type: UserType | None = Query(default=None),
    params: PaginationParams = Depends(),
    user: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[PaginatedData[AdminUserResponse]]:
    result = await service.list_admin_users(db, user_type=user_type, params=params)
    return APIResponse(data=result)


@router.get(
    "/stats",
    response_model=APIResponse[AdminUserStatsResponse],
    summary="Count users by active status",
    description=(
        "Total, active and inactive (suspended) user counts. Filter by `user_type` "
        "(CUSTOMER, BRAIDER, or ADMIN); omit to count every user."
    ),
)
async def user_stats(
    user_type: UserType | None = Query(default=None),
    user: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[AdminUserStatsResponse]:
    result = await service.get_admin_user_stats(db, user_type=user_type)
    return APIResponse(data=result)


@router.post(
    "/{user_id}/suspend",
    response_model=APIResponse[AdminUserResponse],
    summary="Suspend a user",
    description=(
        "Blocks login immediately (existing sessions are also cut off on their next "
        "refresh). `reason` is optional and, if given, is shown to the user the next "
        "time they try to log in - no frontend change needed, it's injected straight "
        "into the login error message."
    ),
)
async def suspend_user(
    user_id: uuid.UUID,
    payload: SuspendUserRequest,
    user: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[AdminUserResponse]:
    result = await service.suspend_user(
        db, admin_user_id=user.id, user_id=user_id, reason=payload.reason
    )
    return APIResponse(data=result)


@router.post(
    "/{user_id}/unsuspend",
    response_model=APIResponse[AdminUserResponse],
    summary="Lift a user's suspension",
)
async def unsuspend_user(
    user_id: uuid.UUID,
    user: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[AdminUserResponse]:
    result = await service.unsuspend_user(db, user_id=user_id)
    return APIResponse(data=result)
