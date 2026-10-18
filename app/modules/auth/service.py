import uuid
from datetime import UTC, datetime

from arq import ArqRedis
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import (
    AdminInviteEmailMismatchError,
    AdminInviteInvalidError,
    AdminSignupBlockedError,
    EmailAlreadyExistsError,
    EmailNotVerifiedError,
    InvalidCredentialsError,
    InvalidOtpError,
    InvalidRefreshTokenError,
    OtpExpiredError,
    PhoneAlreadyExistsError,
    SocialAuthError,
    TooManyOtpAttemptsError,
    UserNotActiveError,
    UserTypeRequiredError,
)
from app.core.i18n import get_current_locale, t
from app.core.pagination import PaginatedData, PaginationParams
from app.core.rate_limit import check_rate_limit
from app.core.security import (
    create_access_token,
    generate_opaque_token,
    generate_otp_code,
    hash_password,
    hash_token,
    verify_password,
)
from app.core.storage import build_public_url
from app.modules.auth import repository as auth_repo
from app.modules.auth.models import AdminInvite, OtpPurpose
from app.modules.auth.schemas import (
    AdminInviteAcceptRequest,
    AdminInviteAcceptSocialRequest,
    AdminInviteListItem,
    AdminInviteRequest,
    AdminInviteResponse,
    AdminInviteStatsResponse,
    AdminInviteStatus,
    AdminSocialLoginRequest,
    AuthTokenResponse,
    BraiderAuthProfile,
    BraiderOnboardingSummary,
    ForgotPasswordRequest,
    LoginRequest,
    LogoutRequest,
    MessageResponse,
    RefreshTokenRequest,
    ResendVerificationRequest,
    ResetPasswordRequest,
    SignupEmailRequest,
    SignupResponse,
    SocialLoginRequest,
    VerifyEmailRequest,
)
from app.modules.auth.social import verify_social_token
from app.modules.auth.tasks import TASK_SEND_ADMIN_INVITE_EMAIL, TASK_SEND_OTP_EMAIL
from app.modules.braiders import repository as braiders_repo
from app.modules.braiders.completion import compute_current_step
from app.modules.braiders.models import OnboardingStep
from app.modules.notifications import service as notifications_service
from app.modules.notifications.models import NotificationType
from app.modules.users import repository as users_repo
from app.modules.users.models import AuthProvider, User, UserType

settings = get_settings()

MAX_OTP_ATTEMPTS = 5


async def _issue_token_pair(
    db: AsyncSession, user: User, *, remember_me: bool = False
) -> tuple[str, str, int]:
    access_expire_minutes = (
        settings.remember_me_access_token_expire_minutes
        if remember_me
        else settings.access_token_expire_minutes
    )
    refresh_expire_days = (
        settings.remember_me_refresh_token_expire_days
        if remember_me
        else settings.refresh_token_expire_days
    )

    access_token, _jti = create_access_token(
        user_id=user.id, user_type=user.user_type.value, expire_minutes=access_expire_minutes
    )
    raw_refresh_token = generate_opaque_token()
    await auth_repo.create_refresh_token(
        db,
        user_id=user.id,
        token_hash=hash_token(raw_refresh_token),
        expire_days=refresh_expire_days,
        remember_me=remember_me,
    )
    expires_in = access_expire_minutes * 60
    return access_token, raw_refresh_token, expires_in


async def _get_braider_auth_profile(db: AsyncSession, user_id: uuid.UUID) -> BraiderAuthProfile:
    profile = await braiders_repo.get_profile_by_user_id(db, user_id)
    status = await braiders_repo.get_onboarding_status_by_user_id(db, user_id)
    current_step = compute_current_step(status) if status else OnboardingStep.BUSINESS_INFO
    return BraiderAuthProfile(
        business_name=profile.business_name if profile else None,
        logo_url=build_public_url(profile.logo_object_key)
        if profile and profile.logo_object_key
        else None,
        onboarding=BraiderOnboardingSummary(
            current_step=current_step,
            completed_at=status.completed_at if status else None,
        ),
    )


def _to_auth_response(
    user: User,
    *,
    access_token: str,
    refresh_token: str,
    expires_in: int,
    braider: BraiderAuthProfile | None = None,
) -> AuthTokenResponse:
    return AuthTokenResponse(
        id=user.id,
        first_name=user.first_name,
        last_name=user.last_name,
        email=user.email,
        phone_number=user.phone_number,
        user_type=user.user_type,
        chat_locale=user.chat_locale,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        braider=braider,
    )


async def _issue_and_send_otp(
    db: AsyncSession,
    queue: ArqRedis,
    *,
    user: User,
    purpose: OtpPurpose,
    locale: str,
) -> None:
    await auth_repo.invalidate_active_otps(db, user_id=user.id, purpose=purpose)
    code = generate_otp_code()
    await auth_repo.create_otp_code(
        db,
        user_id=user.id,
        code_hash=hash_token(code),
        purpose=purpose,
        expire_minutes=settings.otp_expire_minutes,
    )
    await db.commit()
    await queue.enqueue_job(
        TASK_SEND_OTP_EMAIL,
        to=user.email,
        first_name=user.first_name,
        code=code,
        minutes=settings.otp_expire_minutes,
        locale=locale,
        purpose=purpose.value,
    )


async def signup_email(
    db: AsyncSession, queue: ArqRedis, *, data: SignupEmailRequest, locale: str
) -> SignupResponse:

    if data.user_type == UserType.ADMIN.value:
        raise AdminSignupBlockedError()

    if await users_repo.get_user_by_email(db, data.email):
        raise EmailAlreadyExistsError()
    if data.phone_number and await users_repo.get_user_by_phone(db, data.phone_number):
        raise PhoneAlreadyExistsError()

    user = await users_repo.create_user(
        db,
        first_name=data.first_name,
        last_name=data.last_name,
        email=data.email,
        phone_number=data.phone_number,
        password_hash=hash_password(data.password),
        user_type=UserType(data.user_type),
        is_email_verified=False,
    )
    await users_repo.create_auth_identity(
        db, user_id=user.id, provider=AuthProvider.EMAIL, provider_user_id=str(user.id)
    )

    await _issue_and_send_otp(
        db, queue, user=user, purpose=OtpPurpose.EMAIL_VERIFICATION, locale=locale
    )

    return SignupResponse(message=t("auth.signup_success", locale), email=user.email)


async def verify_email(db: AsyncSession, *, data: VerifyEmailRequest) -> AuthTokenResponse:
    user = await users_repo.get_user_by_email(db, data.email)
    if not user:
        raise InvalidOtpError()

    if not user.is_email_verified:
        otp = await auth_repo.get_latest_active_otp(
            db, user_id=user.id, purpose=OtpPurpose.EMAIL_VERIFICATION
        )
        if not otp:
            raise InvalidOtpError()
        if otp.expires_at < datetime.now(UTC):
            raise OtpExpiredError()
        if otp.attempts >= MAX_OTP_ATTEMPTS:
            raise TooManyOtpAttemptsError()

        if hash_token(data.code) != otp.code_hash:
            otp.attempts += 1
            await db.commit()
            raise InvalidOtpError()

        otp.consumed_at = datetime.now(UTC)
        user.is_email_verified = True

    access_token, refresh_token, expires_in = await _issue_token_pair(db, user)
    await db.commit()
    return _to_auth_response(
        user, access_token=access_token, refresh_token=refresh_token, expires_in=expires_in
    )


async def resend_verification(
    db: AsyncSession,
    queue: ArqRedis,
    redis: Redis,
    *,
    data: ResendVerificationRequest,
    locale: str,
) -> MessageResponse:
    await check_rate_limit(
        redis, key=f"otp_resend:{data.email.lower()}", limit=3, window_seconds=300
    )

    user = await users_repo.get_user_by_email(db, data.email)
    if user and not user.is_email_verified:
        await _issue_and_send_otp(
            db, queue, user=user, purpose=OtpPurpose.EMAIL_VERIFICATION, locale=locale
        )

    return MessageResponse(message=t("auth.verification_resent", locale))


async def _notify_new_login(db: AsyncSession, user: User) -> None:
    locale = get_current_locale()
    notification = await notifications_service.create(
        db,
        user_id=user.id,
        type=NotificationType.NEW_LOGIN,
        title_key="notifications.new_login_title",
        body_key="notifications.new_login_body",
    )
    await db.commit()
    await db.refresh(notification)
    await notifications_service.publish_realtime(notification, locale=locale)


async def _finish_login(
    db: AsyncSession, user: User, *, remember_me: bool = False
) -> AuthTokenResponse:
    access_token, refresh_token, expires_in = await _issue_token_pair(
        db, user, remember_me=remember_me
    )
    await db.commit()
    await _notify_new_login(db, user)

    braider = None
    if user.user_type == UserType.BRAIDER:
        braider = await _get_braider_auth_profile(db, user.id)

    return _to_auth_response(
        user,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        braider=braider,
    )


async def _authenticate_email_credentials(
    db: AsyncSession, redis: Redis, *, data: LoginRequest
) -> User:
    await check_rate_limit(
        redis, key=f"login_attempts:{data.email.lower()}", limit=10, window_seconds=900
    )

    user = await users_repo.get_user_by_email(db, data.email)
    if not user or not user.password_hash or not verify_password(data.password, user.password_hash):
        raise InvalidCredentialsError()

    if not user.is_active:
        raise UserNotActiveError(reason=user.suspension_reason)

    if not user.is_email_verified:
        raise EmailNotVerifiedError()

    return user


async def login(db: AsyncSession, redis: Redis, *, data: LoginRequest) -> AuthTokenResponse:
    user = await _authenticate_email_credentials(db, redis, data=data)
    return await _finish_login(db, user, remember_me=data.remember_me)


async def admin_login(db: AsyncSession, redis: Redis, *, data: LoginRequest) -> AuthTokenResponse:
    user = await _authenticate_email_credentials(db, redis, data=data)
    if user.user_type != UserType.ADMIN:
        raise InvalidCredentialsError()
    return await _finish_login(db, user, remember_me=data.remember_me)


async def _resolve_social_user(
    db: AsyncSession,
    *,
    provider: AuthProvider,
    provider_token: str,
    allow_create: bool,
    user_type_for_create: str | None = None,
) -> User:
    profile = await verify_social_token(provider, provider_token)

    identity = await users_repo.get_identity(db, provider, profile.provider_user_id)
    if identity:
        user = await users_repo.get_user_by_id(db, identity.user_id)
        if not user:
            raise SocialAuthError()
        return user

    user = await users_repo.get_user_by_email(db, profile.email)
    if user:
        await users_repo.create_auth_identity(
            db, user_id=user.id, provider=provider, provider_user_id=profile.provider_user_id
        )
        if profile.email_verified and not user.is_email_verified:
            user.is_email_verified = True
        return user

    if not allow_create:
        raise SocialAuthError()

    if not user_type_for_create:
        raise UserTypeRequiredError()
    if user_type_for_create == UserType.ADMIN.value:
        raise AdminSignupBlockedError()

    user = await users_repo.create_user(
        db,
        first_name=profile.first_name,
        last_name=profile.last_name,
        email=profile.email,
        phone_number=None,
        password_hash=None,
        user_type=UserType(user_type_for_create),
        is_email_verified=profile.email_verified,
    )
    await users_repo.create_auth_identity(
        db, user_id=user.id, provider=provider, provider_user_id=profile.provider_user_id
    )
    return user


async def social_login(
    db: AsyncSession, *, provider: AuthProvider, data: SocialLoginRequest
) -> AuthTokenResponse:
    user = await _resolve_social_user(
        db,
        provider=provider,
        provider_token=data.provider_token,
        allow_create=True,
        user_type_for_create=data.user_type,
    )
    if not user.is_active:
        raise UserNotActiveError(reason=user.suspension_reason)
    return await _finish_login(db, user)


async def admin_social_login(
    db: AsyncSession, *, provider: AuthProvider, data: AdminSocialLoginRequest
) -> AuthTokenResponse:
    user = await _resolve_social_user(
        db, provider=provider, provider_token=data.provider_token, allow_create=False
    )
    if not user.is_active:
        raise UserNotActiveError(reason=user.suspension_reason)
    if user.user_type != UserType.ADMIN:
        raise SocialAuthError()
    return await _finish_login(db, user)


async def refresh_access_token(
    db: AsyncSession, *, data: RefreshTokenRequest
) -> AuthTokenResponse:
    token_hash = hash_token(data.refresh_token)
    stored = await auth_repo.get_refresh_token_by_hash(db, token_hash)
    if not stored or stored.revoked_at is not None or stored.expires_at < datetime.now(UTC):
        raise InvalidRefreshTokenError()

    user = await users_repo.get_user_by_id(db, stored.user_id)
    if not user or not user.is_active:
        raise InvalidRefreshTokenError()

    await auth_repo.revoke_refresh_token(db, stored)
    access_token, new_refresh_token, expires_in = await _issue_token_pair(
        db, user, remember_me=stored.remember_me
    )
    await db.commit()
    return _to_auth_response(
        user, access_token=access_token, refresh_token=new_refresh_token, expires_in=expires_in
    )


async def logout(db: AsyncSession, *, data: LogoutRequest, locale: str) -> MessageResponse:
    token_hash = hash_token(data.refresh_token)
    stored = await auth_repo.get_refresh_token_by_hash(db, token_hash)
    if stored and stored.revoked_at is None:
        await auth_repo.revoke_refresh_token(db, stored)
        await db.commit()
    return MessageResponse(message=t("auth.logged_out", locale))


async def forgot_password(
    db: AsyncSession,
    queue: ArqRedis,
    redis: Redis,
    *,
    data: ForgotPasswordRequest,
    locale: str,
) -> MessageResponse:
    await check_rate_limit(
        redis, key=f"password_reset:{data.email.lower()}", limit=3, window_seconds=900
    )

    user = await users_repo.get_user_by_email(db, data.email)
    if user and user.is_active:
        await _issue_and_send_otp(
            db, queue, user=user, purpose=OtpPurpose.PASSWORD_RESET, locale=locale
        )

    return MessageResponse(message=t("auth.password_reset_requested", locale))


async def reset_password(
    db: AsyncSession, *, data: ResetPasswordRequest, locale: str
) -> MessageResponse:
    user = await users_repo.get_user_by_email(db, data.email)
    if not user:
        raise InvalidOtpError()

    otp = await auth_repo.get_latest_active_otp(
        db, user_id=user.id, purpose=OtpPurpose.PASSWORD_RESET
    )
    if not otp:
        raise InvalidOtpError()
    if otp.expires_at < datetime.now(UTC):
        raise OtpExpiredError()
    if otp.attempts >= MAX_OTP_ATTEMPTS:
        raise TooManyOtpAttemptsError()

    if hash_token(data.code) != otp.code_hash:
        otp.attempts += 1
        await db.commit()
        raise InvalidOtpError()

    otp.consumed_at = datetime.now(UTC)
    user.password_hash = hash_password(data.new_password)
    await auth_repo.revoke_all_refresh_tokens_for_user(db, user_id=user.id)
    await db.commit()

    notification = await notifications_service.create(
        db,
        user_id=user.id,
        type=NotificationType.PASSWORD_CHANGED,
        title_key="notifications.password_changed_title",
        body_key="notifications.password_changed_body",
    )
    await db.commit()
    await db.refresh(notification)
    await notifications_service.publish_realtime(notification, locale=locale)

    return MessageResponse(message=t("auth.password_reset_success", locale))


def _admin_invite_status(invite: AdminInvite) -> AdminInviteStatus:
    if invite.accepted_at is not None:
        return AdminInviteStatus.ACCEPTED
    if invite.revoked_at is not None:
        return AdminInviteStatus.REVOKED
    if invite.expires_at < datetime.now(UTC):
        return AdminInviteStatus.EXPIRED
    return AdminInviteStatus.PENDING


async def list_admin_invites(
    db: AsyncSession, *, params: PaginationParams
) -> PaginatedData[AdminInviteListItem]:
    items, meta = await auth_repo.list_admin_invites(db, params=params)
    return PaginatedData(
        items=[
            AdminInviteListItem(
                id=invite.id,
                email=invite.email,
                status=_admin_invite_status(invite),
                invited_by_user_id=invite.invited_by_user_id,
                created_at=invite.created_at,
                expires_at=invite.expires_at,
                accepted_at=invite.accepted_at,
                revoked_at=invite.revoked_at,
            )
            for invite in items
        ],
        pagination=meta,
    )


async def get_admin_invite_stats(db: AsyncSession) -> AdminInviteStatsResponse:
    counts = await auth_repo.count_admin_invites_by_status(db)
    return AdminInviteStatsResponse(
        total=counts["total"],
        active=counts["pending"],
        inactive=counts["total"] - counts["pending"],
        accepted=counts["accepted"],
        expired=counts["expired"],
        revoked=counts["revoked"],
    )


async def _get_valid_admin_invite(db: AsyncSession, *, token: str) -> AdminInvite:
    invite = await auth_repo.get_admin_invite_by_token_hash(db, hash_token(token))
    if not invite or invite.revoked_at is not None or invite.accepted_at is not None:
        raise AdminInviteInvalidError()
    if invite.expires_at < datetime.now(UTC):
        raise AdminInviteInvalidError()
    return invite


async def invite_admin(
    db: AsyncSession,
    queue: ArqRedis,
    *,
    inviter: User,
    data: AdminInviteRequest,
    locale: str,
) -> AdminInviteResponse:
    email = data.email.lower()
    if await users_repo.get_user_by_email(db, email):
        raise EmailAlreadyExistsError()

    await auth_repo.invalidate_active_admin_invites_for_email(db, email=email)

    raw_token = generate_opaque_token()
    await auth_repo.create_admin_invite(
        db,
        email=email,
        token_hash=hash_token(raw_token),
        invited_by_user_id=inviter.id,
        expire_hours=settings.admin_invite_expire_hours,
    )
    await db.commit()

    await queue.enqueue_job(
        TASK_SEND_ADMIN_INVITE_EMAIL,
        to=email,
        token=raw_token,
        minutes=settings.admin_invite_expire_hours * 60,
        locale=locale,
    )

    return AdminInviteResponse(message=t("auth.admin_invite_sent", locale), email=email)


async def accept_admin_invite_email(
    db: AsyncSession, *, data: AdminInviteAcceptRequest
) -> AuthTokenResponse:
    invite = await _get_valid_admin_invite(db, token=data.token)
    if await users_repo.get_user_by_email(db, invite.email):
        raise EmailAlreadyExistsError()

    user = await users_repo.create_user(
        db,
        first_name=data.first_name,
        last_name=data.last_name,
        email=invite.email,
        phone_number=None,
        password_hash=hash_password(data.password),
        user_type=UserType.ADMIN,
        is_email_verified=True,
    )
    await users_repo.create_auth_identity(
        db, user_id=user.id, provider=AuthProvider.EMAIL, provider_user_id=str(user.id)
    )
    invite.accepted_at = datetime.now(UTC)
    await db.flush()

    return await _finish_login(db, user)


async def accept_admin_invite_social(
    db: AsyncSession, *, provider: AuthProvider, data: AdminInviteAcceptSocialRequest
) -> AuthTokenResponse:
    invite = await _get_valid_admin_invite(db, token=data.token)
    profile = await verify_social_token(provider, data.provider_token)

    if profile.email.lower() != invite.email:
        raise AdminInviteEmailMismatchError()
    if await users_repo.get_identity(db, provider, profile.provider_user_id):
        raise AdminInviteInvalidError()
    if await users_repo.get_user_by_email(db, invite.email):
        raise EmailAlreadyExistsError()

    user = await users_repo.create_user(
        db,
        first_name=profile.first_name,
        last_name=profile.last_name,
        email=invite.email,
        phone_number=None,
        password_hash=None,
        user_type=UserType.ADMIN,
        is_email_verified=True,
    )
    await users_repo.create_auth_identity(
        db, user_id=user.id, provider=provider, provider_user_id=profile.provider_user_id
    )
    invite.accepted_at = datetime.now(UTC)
    await db.flush()

    return await _finish_login(db, user)
