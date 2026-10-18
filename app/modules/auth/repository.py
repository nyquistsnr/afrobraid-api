import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import PaginationMeta, PaginationParams, paginate
from app.modules.auth.models import AdminInvite, OtpCode, OtpPurpose, RefreshToken


async def create_otp_code(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    code_hash: str,
    purpose: OtpPurpose,
    expire_minutes: int,
) -> OtpCode:
    otp = OtpCode(
        user_id=user_id,
        code_hash=code_hash,
        purpose=purpose,
        expires_at=datetime.now(UTC) + timedelta(minutes=expire_minutes),
    )
    db.add(otp)
    await db.flush()
    return otp


async def get_latest_active_otp(
    db: AsyncSession, *, user_id: uuid.UUID, purpose: OtpPurpose
) -> OtpCode | None:
    result = await db.execute(
        select(OtpCode)
        .where(
            OtpCode.user_id == user_id,
            OtpCode.purpose == purpose,
            OtpCode.consumed_at.is_(None),
        )
        .order_by(OtpCode.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def invalidate_active_otps(
    db: AsyncSession, *, user_id: uuid.UUID, purpose: OtpPurpose
) -> None:
    result = await db.execute(
        select(OtpCode).where(
            OtpCode.user_id == user_id,
            OtpCode.purpose == purpose,
            OtpCode.consumed_at.is_(None),
        )
    )
    now = datetime.now(UTC)
    for otp in result.scalars():
        otp.consumed_at = now
    await db.flush()


async def create_refresh_token(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    token_hash: str,
    expire_days: int,
    remember_me: bool = False,
) -> RefreshToken:
    token = RefreshToken(
        user_id=user_id,
        token_hash=token_hash,
        remember_me=remember_me,
        expires_at=datetime.now(UTC) + timedelta(days=expire_days),
    )
    db.add(token)
    await db.flush()
    return token


async def get_refresh_token_by_hash(db: AsyncSession, token_hash: str) -> RefreshToken | None:
    result = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    return result.scalar_one_or_none()


async def revoke_refresh_token(db: AsyncSession, token: RefreshToken) -> None:
    token.revoked_at = datetime.now(UTC)
    await db.flush()


async def revoke_all_refresh_tokens_for_user(db: AsyncSession, *, user_id: uuid.UUID) -> None:
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
        )
    )
    now = datetime.now(UTC)
    for token in result.scalars():
        token.revoked_at = now
    await db.flush()


async def create_admin_invite(
    db: AsyncSession,
    *,
    email: str,
    token_hash: str,
    invited_by_user_id: uuid.UUID,
    expire_hours: int,
) -> AdminInvite:
    invite = AdminInvite(
        email=email,
        token_hash=token_hash,
        invited_by_user_id=invited_by_user_id,
        expires_at=datetime.now(UTC) + timedelta(hours=expire_hours),
    )
    db.add(invite)
    await db.flush()
    return invite


async def get_admin_invite_by_token_hash(db: AsyncSession, token_hash: str) -> AdminInvite | None:
    result = await db.execute(select(AdminInvite).where(AdminInvite.token_hash == token_hash))
    return result.scalar_one_or_none()


async def invalidate_active_admin_invites_for_email(db: AsyncSession, *, email: str) -> None:
    result = await db.execute(
        select(AdminInvite).where(
            AdminInvite.email == email,
            AdminInvite.accepted_at.is_(None),
            AdminInvite.revoked_at.is_(None),
        )
    )
    now = datetime.now(UTC)
    for invite in result.scalars():
        invite.revoked_at = now
    await db.flush()


async def list_admin_invites(
    db: AsyncSession, *, params: PaginationParams
) -> tuple[list[AdminInvite], PaginationMeta]:
    stmt = select(AdminInvite).order_by(AdminInvite.created_at.desc())
    return await paginate(db, stmt, params)


async def count_admin_invites_by_status(db: AsyncSession) -> dict[str, int]:
    """Counts mirror `_admin_invite_status` precedence: accepted, then revoked, then expired."""
    now = datetime.now(UTC)
    not_accepted = AdminInvite.accepted_at.is_(None)
    not_revoked = AdminInvite.revoked_at.is_(None)
    result = await db.execute(
        select(
            func.count(),
            func.count().filter(AdminInvite.accepted_at.is_not(None)),
            func.count().filter(not_accepted, AdminInvite.revoked_at.is_not(None)),
            func.count().filter(not_accepted, not_revoked, AdminInvite.expires_at < now),
        )
    )
    total, accepted, revoked, expired = result.one()
    return {
        "total": total,
        "accepted": accepted,
        "revoked": revoked,
        "expired": expired,
        "pending": total - accepted - revoked - expired,
    }
