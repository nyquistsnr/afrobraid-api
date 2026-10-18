import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.modules.braiders.models import BioSource


class ReviewStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class Review(Base):
    """A customer's rating + optional written review of a braider.

    One row per (customer, braider) - a customer can only ever have one
    review per braider, editable in place rather than accumulating new rows
    (see the unique constraint below). `rating` counts toward the braider's
    cached average immediately on write; the written comment additionally
    needs `status == APPROVED` before it's shown on the public list -
    editing the comment resets status back to PENDING for re-moderation.
    A REJECTED review's rating is excluded from the average too (see
    reviews.repository.recompute_braider_rating), giving admins a way to
    fully discount a bad-faith review rather than just hiding its text.
    """

    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("customer_id", "braider_id", name="uq_reviews_customer_braider"),
        CheckConstraint("rating >= 1 AND rating <= 5", name="ck_reviews_rating_range"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    braider_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("braider_profiles.id"),
        nullable=False,
        index=True,
    )
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    comment_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment_de: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment_fr: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment_en_source: Mapped[BioSource | None] = mapped_column(
        Enum(BioSource, name="bio_source"), nullable=True
    )
    comment_de_source: Mapped[BioSource | None] = mapped_column(
        Enum(BioSource, name="bio_source"), nullable=True
    )
    comment_fr_source: Mapped[BioSource | None] = mapped_column(
        Enum(BioSource, name="bio_source"), nullable=True
    )
    status: Mapped[ReviewStatus] = mapped_column(
        Enum(ReviewStatus, name="review_status"), nullable=False, index=True, default=ReviewStatus.PENDING
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
