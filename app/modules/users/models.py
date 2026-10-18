import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class UserType(str, enum.Enum):
    CUSTOMER = "CUSTOMER"
    BRAIDER = "BRAIDER"
    ADMIN = "ADMIN"


class AuthProvider(str, enum.Enum):
    EMAIL = "EMAIL"
    GOOGLE = "GOOGLE"
    FACEBOOK = "FACEBOOK"
    TIKTOK = "TIKTOK"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    phone_number: Mapped[str | None] = mapped_column(
        String(32), unique=True, index=True, nullable=True
    )
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_type: Mapped[UserType] = mapped_column(Enum(UserType, name="user_type"), nullable=False, index=True)
    is_email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # `is_active=False` is currently only ever set by an admin suspension
    # (see users/admin_router.py) - there's no other deactivation path in
    # this codebase. It's already what login/social_login/refresh_access_token/
    # get_current_user gate on (app.modules.auth), so suspending reuses that
    # existing enforcement rather than adding a second, easy-to-forget check.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    suspension_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Cached rather than looked up per-booking - Stripe Customers live on the
    # platform account (see bookings/payments), so this is a 1:1 mirror, not
    # something that needs its own table.
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Explicitly opted-into language for chat translation (app.modules.chat) -
    # deliberately separate from the request-scoped `?lang=`/Accept-Language
    # locale (app.core.i18n): chat only translates a message when BOTH
    # participants have set this, and only ever between those two languages,
    # never fanned out to every supported locale like reviews/bios are.
    chat_locale: Mapped[str | None] = mapped_column(String(5), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    auth_identities: Mapped[list["AuthIdentity"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class AuthIdentity(Base):
    __tablename__ = "auth_identities"
    __table_args__ = (
        UniqueConstraint("provider", "provider_user_id", name="uq_auth_identity_provider_user"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[AuthProvider] = mapped_column(
        Enum(AuthProvider, name="auth_provider"), nullable=False
    )
    provider_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="auth_identities")
