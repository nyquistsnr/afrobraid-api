import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ChatMessageStatus(str, enum.Enum):
    SENT = "SENT"
    # Content matched the moderation filter (app.modules.chat.moderation) -
    # the plaintext is never stored, only a one-way hash for audit purposes.
    # Both participants see a canned policy notice instead of `body`.
    FLAGGED = "FLAGGED"


class ChatTranslationStatus(str, enum.Enum):
    # Either participant hasn't set a chat_locale, or both have the same one -
    # nothing to translate, unlike reviews/bios this never fans out to every
    # supported locale, only the two locales actually in play.
    NOT_APPLICABLE = "NOT_APPLICABLE"
    PENDING = "PENDING"
    DONE = "DONE"
    FAILED = "FAILED"


class ChatReportReason(str, enum.Enum):
    HARASSMENT = "HARASSMENT"
    INAPPROPRIATE_CONTENT = "INAPPROPRIATE_CONTENT"
    SPAM = "SPAM"
    SCAM_OR_FRAUD = "SCAM_OR_FRAUD"
    OFF_PLATFORM_SOLICITATION = "OFF_PLATFORM_SOLICITATION"
    OTHER = "OTHER"


class ChatReportStatus(str, enum.Enum):
    OPEN = "OPEN"
    UNDER_REVIEW = "UNDER_REVIEW"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"


class ChatThread(Base):
    """One conversation per booking, between that booking's customer and the
    braider's user account. Created lazily (get-or-create) the first time
    either side opens or messages it - see chat.repository.get_or_create_thread.
    Gating is on `bookings.confirmed_at IS NOT NULL` (deposit or full payment
    has succeeded at least once), checked in chat.service rather than here,
    and deliberately never re-locks on a later cancellation so a dispute can
    still be discussed.
    """

    __tablename__ = "chat_threads"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # No ON DELETE CASCADE on booking_id/customer_id/braider_user_id - this
    # class's own docstring says a thread must survive a booking's
    # cancellation "so a dispute can still be discussed"; cascading it off
    # the booking or either participant's account would silently destroy
    # that same dispute record (and any open ChatReport evidence with it).
    # Deleting a booking or user while a thread references it is refused at
    # the DB level instead.
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=False, unique=True
    )
    # Denormalized off the booking at thread-creation time purely to make
    # participant/access checks and inbox listing single-table lookups - the
    # booking itself remains the source of truth for who's on either side.
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    braider_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )

    customer_last_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    braider_last_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Cached off the latest message so the inbox list doesn't need a
    # per-thread subquery just to sort/preview. `last_message_preview` is
    # only ever a snippet of real SENT content - when the latest message was
    # FLAGGED, it's left null and `last_message_flagged` is set instead, so
    # the client renders its own localized "message withheld" label rather
    # than this backend baking English text into a cached column.
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_message_preview: Mapped[str | None] = mapped_column(String(280), nullable=True)
    last_message_flagged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ChatMessage(Base):
    """A single message. `body` only ever holds real plaintext for a SENT
    message - a FLAGGED one has `body IS NULL` and only `body_hash` (a
    salted one-way digest, see security.hash_content) survives, enforced by
    the check constraint below so a bug can never leak withheld content back
    out through this table."""

    __tablename__ = "chat_messages"
    __table_args__ = (
        CheckConstraint(
            "(status = 'FLAGGED' AND body IS NULL AND body_hash IS NOT NULL) OR "
            "(status = 'SENT' AND body IS NOT NULL AND body_hash IS NULL)",
            name="ck_chat_messages_flagged_body",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_threads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # No ON DELETE CASCADE - same reasoning as ChatThread above, a message
    # is evidence in the conversation regardless of the sender's account
    # status; only thread_id cascades, since a message has no meaning
    # outside its thread.
    sender_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    status: Mapped[ChatMessageStatus] = mapped_column(
        Enum(ChatMessageStatus, name="chat_message_status"), nullable=False, default=ChatMessageStatus.SENT
    )
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Sender's chat_locale (app.modules.users.models.User.chat_locale) at
    # send time, if they'd set one - what `body` is written in.
    body_locale: Mapped[str | None] = mapped_column(String(5), nullable=True)
    body_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Comma-separated violation type tags (see chat.moderation) for admin
    # auditing/metrics without ever exposing the flagged text itself.
    violation_types: Mapped[str | None] = mapped_column(String(255), nullable=True)

    translated_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    translated_locale: Mapped[str | None] = mapped_column(String(5), nullable=True)
    translation_status: Mapped[ChatTranslationStatus] = mapped_column(
        Enum(ChatTranslationStatus, name="chat_translation_status"),
        nullable=False,
        default=ChatTranslationStatus.NOT_APPLICABLE,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class ChatReport(Base):
    """Either participant in a thread reporting the other - abuse, spam,
    off-platform solicitation, etc. Optionally references the specific
    message that triggered it (nullable - a report can be about a pattern of
    behavior, not one message)."""

    __tablename__ = "chat_reports"
    __table_args__ = (
        CheckConstraint("reporter_id != reported_user_id", name="ck_chat_reports_not_self"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_threads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # No ON DELETE CASCADE - a report is moderation evidence that must
    # outlive either party's account, especially the accused's (deleting
    # your own account should not be a way to make an open report vanish).
    reporter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    reported_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True
    )
    reason: Mapped[ChatReportReason] = mapped_column(Enum(ChatReportReason, name="chat_report_reason"), nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[ChatReportStatus] = mapped_column(
        Enum(ChatReportStatus, name="chat_report_status"), nullable=False, index=True, default=ChatReportStatus.OPEN
    )
    admin_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
