import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import (
    CannotSuspendSelfError,
    InvalidChatLocaleError,
    PhoneAlreadyExistsError,
    UserNotFoundError,
)
from app.core.i18n import get_current_locale
from app.core.pagination import PaginatedData, PaginationParams
from app.modules.braiders import repository as braiders_repo
from app.modules.notifications import service as notifications_service
from app.modules.notifications.models import NotificationType
from app.modules.users import repository as users_repo
from app.modules.users.models import UserType
from app.modules.users.schemas import (
    AdminUserResponse,
    AdminUserStatsResponse,
    UserProfileUpdateRequest,
    UserPublic,
)

settings = get_settings()


def _to_admin_user_response(
    user, *, braider_ids_by_user_id: dict[uuid.UUID, uuid.UUID] | None = None
) -> AdminUserResponse:
    response = AdminUserResponse.model_validate(user)
    if user.user_type == UserType.BRAIDER and braider_ids_by_user_id is not None:
        response.braider_id = braider_ids_by_user_id.get(user.id)
    return response


async def get_profile(db: AsyncSession, user_id: uuid.UUID) -> UserPublic:
    user = await users_repo.get_user_by_id(db, user_id)
    return UserPublic.model_validate(user)


async def update_profile(
    db: AsyncSession, user_id: uuid.UUID, *, data: UserProfileUpdateRequest
) -> UserPublic:
    user = await users_repo.get_user_by_id(db, user_id)
    assert user is not None

    updates = data.model_dump(exclude_unset=True)

    if "phone_number" in updates:
        phone_number = updates["phone_number"]
        if phone_number and phone_number != user.phone_number:
            existing = await users_repo.get_user_by_phone(db, phone_number)
            if existing and existing.id != user.id:
                raise PhoneAlreadyExistsError()
        user.phone_number = phone_number

    if "first_name" in updates:
        user.first_name = updates["first_name"]
    if "last_name" in updates:
        user.last_name = updates["last_name"]

    if "chat_locale" in updates:
        chat_locale = updates["chat_locale"]
        if chat_locale is not None and chat_locale not in settings.supported_locales_list:
            raise InvalidChatLocaleError()
        user.chat_locale = chat_locale

    if updates:
        locale = get_current_locale()
        notification = await notifications_service.create(
            db,
            user_id=user.id,
            type=NotificationType.PROFILE_UPDATED,
            title_key="notifications.profile_updated_title",
            body_key="notifications.profile_updated_body",
        )
        await db.commit()
        await db.refresh(user)
        await db.refresh(notification)
        await notifications_service.publish_realtime(notification, locale=locale)
    else:
        await db.commit()
        await db.refresh(user)

    return UserPublic.model_validate(user)


# --- Admin -------------------------------------------------------------


async def list_admin_users(
    db: AsyncSession, *, user_type: UserType | None, params: PaginationParams
) -> PaginatedData[AdminUserResponse]:
    items, meta = await users_repo.list_users(db, user_type=user_type, params=params)
    braider_ids_by_user_id = await braiders_repo.list_profile_ids_by_user_ids(
        db, [u.id for u in items if u.user_type == UserType.BRAIDER]
    )
    return PaginatedData(
        items=[
            _to_admin_user_response(u, braider_ids_by_user_id=braider_ids_by_user_id)
            for u in items
        ],
        pagination=meta,
    )


async def get_admin_user_stats(
    db: AsyncSession, *, user_type: UserType | None
) -> AdminUserStatsResponse:
    counts = await users_repo.count_users_by_active(db, user_type=user_type)
    active = counts.get(True, 0)
    inactive = counts.get(False, 0)
    return AdminUserStatsResponse(total=active + inactive, active=active, inactive=inactive)


async def suspend_user(
    db: AsyncSession, *, admin_user_id: uuid.UUID, user_id: uuid.UUID, reason: str | None
) -> AdminUserResponse:
    if admin_user_id == user_id:
        raise CannotSuspendSelfError()

    user = await users_repo.get_user_by_id(db, user_id)
    if user is None:
        raise UserNotFoundError()

    user.is_active = False
    user.suspension_reason = reason
    user.suspended_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(user)
    braider_ids_by_user_id = await braiders_repo.list_profile_ids_by_user_ids(db, [user.id])
    return _to_admin_user_response(user, braider_ids_by_user_id=braider_ids_by_user_id)


async def unsuspend_user(db: AsyncSession, *, user_id: uuid.UUID) -> AdminUserResponse:
    user = await users_repo.get_user_by_id(db, user_id)
    if user is None:
        raise UserNotFoundError()

    user.is_active = True
    user.suspension_reason = None
    user.suspended_at = None

    await db.commit()
    await db.refresh(user)
    braider_ids_by_user_id = await braiders_repo.list_profile_ids_by_user_ids(db, [user.id])
    return _to_admin_user_response(user, braider_ids_by_user_id=braider_ids_by_user_id)
