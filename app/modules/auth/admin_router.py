from typing import Literal

from arq import ArqRedis
from fastapi import APIRouter, Depends, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.pagination import PaginatedData, PaginationParams
from app.core.queue import get_task_queue
from app.core.rate_limit import ip_rate_limiter
from app.core.redis import get_redis
from app.core.response import APIResponse
from app.modules.auth import service
from app.modules.auth.dependencies import require_roles
from app.modules.auth.schemas import (
    AdminInviteAcceptRequest,
    AdminInviteAcceptSocialRequest,
    AdminInviteListItem,
    AdminInviteRequest,
    AdminInviteResponse,
    AdminInviteStatsResponse,
    AdminSocialLoginRequest,
    AuthTokenResponse,
    LoginRequest,
)
from app.modules.users.models import AuthProvider, User, UserType

router = APIRouter(prefix="/api/v1/admin/auth", tags=["Admin - Auth"])

_require_admin = require_roles(UserType.ADMIN)

SocialProviderPath = Literal["google", "facebook", "tiktok"]
_PROVIDER_MAP = {
    "google": AuthProvider.GOOGLE,
    "facebook": AuthProvider.FACEBOOK,
    "tiktok": AuthProvider.TIKTOK,
}


def _locale(request: Request) -> str:
    return getattr(request.state, "locale", "en")


@router.post(
    "/invites",
    response_model=APIResponse[AdminInviteResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Invite a new admin",
    description="Admin-only. Sends an invite link to `email`; the recipient completes "
    "account creation via /invites/accept or /invites/accept/social/{provider}. This is "
    "the only way an ADMIN account is ever created.",
)
async def invite_admin(
    payload: AdminInviteRequest,
    request: Request,
    inviter: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
    queue: ArqRedis = Depends(get_task_queue),
) -> APIResponse[AdminInviteResponse]:
    result = await service.invite_admin(
        db, queue, inviter=inviter, data=payload, locale=_locale(request)
    )
    return APIResponse(data=result)


@router.get(
    "/invites",
    response_model=APIResponse[PaginatedData[AdminInviteListItem]],
    summary="List admin invites",
    description="Admin-only. Shows every invite ever sent, newest first - including ones "
    "still awaiting acceptance (`status: PENDING`), so you can see who's been invited but "
    "hasn't signed up yet.",
)
async def list_admin_invites(
    params: PaginationParams = Depends(),
    admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[PaginatedData[AdminInviteListItem]]:
    result = await service.list_admin_invites(db, params=params)
    return APIResponse(data=result)


@router.get(
    "/invites/stats",
    response_model=APIResponse[AdminInviteStatsResponse],
    summary="Count admin invites by status",
    description="Admin-only. `active` is invites still awaiting acceptance (PENDING); "
    "`inactive` is everything accepted, expired or revoked.",
)
async def admin_invite_stats(
    admin: User = Depends(_require_admin),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[AdminInviteStatsResponse]:
    result = await service.get_admin_invite_stats(db)
    return APIResponse(data=result)


@router.post(
    "/invites/accept",
    response_model=APIResponse[AuthTokenResponse],
    dependencies=[
        Depends(ip_rate_limiter(key_prefix="admin-invite-accept", limit=10, window_seconds=3600))
    ],
    summary="Accept an admin invite via email/password",
)
async def accept_admin_invite(
    payload: AdminInviteAcceptRequest,
    db: AsyncSession = Depends(get_db),
) -> APIResponse[AuthTokenResponse]:
    result = await service.accept_admin_invite_email(db, data=payload)
    return APIResponse(data=result)


@router.post(
    "/invites/accept/social/{provider}",
    response_model=APIResponse[AuthTokenResponse],
    dependencies=[
        Depends(
            ip_rate_limiter(key_prefix="admin-invite-accept-social", limit=10, window_seconds=3600)
        )
    ],
    summary="Accept an admin invite via a social provider",
    description="`provider`'s verified email must match the email the invite was sent to.",
)
async def accept_admin_invite_social(
    provider: SocialProviderPath,
    payload: AdminInviteAcceptSocialRequest,
    db: AsyncSession = Depends(get_db),
) -> APIResponse[AuthTokenResponse]:
    result = await service.accept_admin_invite_social(
        db, provider=_PROVIDER_MAP[provider], data=payload
    )
    return APIResponse(data=result)


@router.post(
    "/login",
    response_model=APIResponse[AuthTokenResponse],
    dependencies=[Depends(ip_rate_limiter(key_prefix="admin-login", limit=20, window_seconds=900))],
    summary="Admin email/password login",
    description="Only succeeds for a user with user_type ADMIN - any other account gets the "
    "same INVALID_CREDENTIALS error as a wrong password, so this endpoint never reveals "
    "whether an email belongs to a non-admin account.",
)
async def admin_login(
    payload: LoginRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> APIResponse[AuthTokenResponse]:
    result = await service.admin_login(db, redis, data=payload)
    return APIResponse(data=result)


@router.post(
    "/social/{provider}",
    response_model=APIResponse[AuthTokenResponse],
    dependencies=[
        Depends(ip_rate_limiter(key_prefix="admin-social-login", limit=20, window_seconds=3600))
    ],
    summary="Admin social login",
    description="Never creates an account - only signs in an existing ADMIN user already "
    "linked to this provider or email. New admins are created via /invites/accept/social.",
)
async def admin_social_login(
    provider: SocialProviderPath,
    payload: AdminSocialLoginRequest,
    db: AsyncSession = Depends(get_db),
) -> APIResponse[AuthTokenResponse]:
    result = await service.admin_social_login(db, provider=_PROVIDER_MAP[provider], data=payload)
    return APIResponse(data=result)
