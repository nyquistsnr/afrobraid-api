import uuid

from arq import ArqRedis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache_tags import CUSTOMER_NAMES, REVIEWS, braider_tag, reviews_tag
from app.core.config import get_settings
from app.core.exceptions import BraiderNotFoundError, ReviewNotEligibleError, ReviewNotFoundError
from app.core.i18n import localize_field
from app.core.pagination import PaginatedData, PaginationParams
from app.core.query_cache import cached
from app.modules.braiders import repository as braiders_repo
from app.modules.braiders.models import BioSource
from app.modules.reviews import repository as reviews_repo
from app.modules.reviews.models import Review, ReviewStatus
from app.modules.reviews.schemas import (
    AdminReviewResponse,
    PublicReviewResponse,
    ReviewResponse,
    ReviewUpsertRequest,
)
from app.modules.reviews.tasks import TASK_TRANSLATE_REVIEW_COMMENT
from app.modules.users import repository as users_repo

settings = get_settings()


def _start_comment_translation(review: Review, locale: str, text: str) -> list[str]:
    """Same locale-fan-out as portfolio's caption handling: the locale the
    customer is currently using becomes the HUMAN source, the other
    supported locales are queued for translation."""
    setattr(review, f"comment_{locale}", text)
    setattr(review, f"comment_{locale}_source", BioSource.HUMAN)
    target_locales = [
        loc
        for loc in settings.supported_locales_list
        if loc != locale and getattr(review, f"comment_{loc}_source", None) != BioSource.HUMAN
    ]
    for loc in target_locales:
        setattr(review, f"comment_{loc}_source", BioSource.PENDING)
    return target_locales


def _clear_comment(review: Review) -> None:
    for loc in settings.supported_locales_list:
        setattr(review, f"comment_{loc}", None)
        setattr(review, f"comment_{loc}_source", None)


async def _enqueue_comment_translation(
    queue: ArqRedis,
    *,
    review_id: uuid.UUID,
    source_locale: str,
    source_text: str,
    target_locales: list[str],
) -> None:
    if not target_locales:
        return
    await queue.enqueue_job(
        TASK_TRANSLATE_REVIEW_COMMENT,
        review_id=str(review_id),
        source_locale=source_locale,
        source_text=source_text,
        target_locales=target_locales,
    )


def _to_response(review: Review) -> ReviewResponse:
    return ReviewResponse(
        id=review.id,
        braider_id=review.braider_id,
        rating=review.rating,
        comment_en=review.comment_en,
        comment_de=review.comment_de,
        comment_fr=review.comment_fr,
        comment_en_source=review.comment_en_source,
        comment_de_source=review.comment_de_source,
        comment_fr_source=review.comment_fr_source,
        status=review.status,
        created_at=review.created_at,
        updated_at=review.updated_at,
    )


def _to_public_response(review: Review, *, locale: str, customer_name: str) -> PublicReviewResponse:
    return PublicReviewResponse(
        id=review.id,
        customer_name=customer_name,
        rating=review.rating,
        comment=localize_field(review, "comment", locale),
        created_at=review.created_at,
        updated_at=review.updated_at,
    )


def _to_admin_response(
    review: Review, *, braider_name: str, customer_name: str
) -> AdminReviewResponse:
    return AdminReviewResponse(
        id=review.id,
        braider_id=review.braider_id,
        braider_name=braider_name,
        customer_id=review.customer_id,
        customer_name=customer_name,
        rating=review.rating,
        comment_en=review.comment_en,
        comment_de=review.comment_de,
        comment_fr=review.comment_fr,
        status=review.status,
        created_at=review.created_at,
        updated_at=review.updated_at,
    )


async def upsert_review(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    braider_id: uuid.UUID,
    data: ReviewUpsertRequest,
    locale: str,
    queue: ArqRedis,
) -> ReviewResponse:
    profile = await braiders_repo.get_profile_by_id(db, braider_id)
    if profile is None:
        raise BraiderNotFoundError()

    if not await reviews_repo.has_eligible_booking(db, user_id, braider_id):
        raise ReviewNotEligibleError()

    review = await reviews_repo.get_by_customer_and_braider(db, user_id, braider_id)
    if review is None:
        review = await reviews_repo.create_review(
            db, customer_id=user_id, braider_id=braider_id, rating=data.rating
        )
    else:
        review.rating = data.rating

    target_locales: list[str] = []
    if data.comment is None:
        _clear_comment(review)
        review.status = ReviewStatus.APPROVED
    elif data.comment != getattr(review, f"comment_{locale}", None):
        target_locales = _start_comment_translation(review, locale, data.comment)
        review.status = ReviewStatus.PENDING

    await db.flush()
    await reviews_repo.recompute_braider_rating(db, braider_id)
    await db.commit()
    await db.refresh(review)

    if target_locales:
        await _enqueue_comment_translation(
            queue,
            review_id=review.id,
            source_locale=locale,
            source_text=data.comment or "",
            target_locales=target_locales,
        )

    return _to_response(review)


async def get_my_review(
    db: AsyncSession, *, user_id: uuid.UUID, braider_id: uuid.UUID
) -> ReviewResponse:
    review = await reviews_repo.get_by_customer_and_braider(db, user_id, braider_id)
    if review is None:
        raise ReviewNotFoundError()
    return _to_response(review)


@cached(
    name="reviews.list_public_reviews",
    ttl_seconds=60 * 60,
    tags=lambda a: [
        REVIEWS,
        CUSTOMER_NAMES,
        reviews_tag(a["braider_id"]),
        braider_tag(a["braider_id"]),
    ],
)
async def list_public_reviews(
    db: AsyncSession, braider_id: uuid.UUID, *, params: PaginationParams, locale: str
) -> PaginatedData[PublicReviewResponse]:
    profile = await braiders_repo.get_profile_by_id(db, braider_id)
    if profile is None:
        raise BraiderNotFoundError()

    items, meta = await reviews_repo.list_approved_for_braider(db, braider_id, params=params)
    customer_names = await users_repo.list_full_names(db, [r.customer_id for r in items])
    return PaginatedData(
        items=[
            _to_public_response(r, locale=locale, customer_name=customer_names.get(r.customer_id, ""))
            for r in items
        ],
        pagination=meta,
    )


async def list_admin_reviews(
    db: AsyncSession, *, params: PaginationParams, status: ReviewStatus | None
) -> PaginatedData[AdminReviewResponse]:
    items, meta = await reviews_repo.list_for_admin(db, params=params, status=status)
    braider_names = await braiders_repo.list_display_names(db, [r.braider_id for r in items])
    customer_names = await users_repo.list_full_names(db, [r.customer_id for r in items])
    return PaginatedData(
        items=[
            _to_admin_response(
                r,
                braider_name=braider_names.get(r.braider_id, ""),
                customer_name=customer_names.get(r.customer_id, ""),
            )
            for r in items
        ],
        pagination=meta,
    )


async def _moderate(db: AsyncSession, review_id: uuid.UUID, *, status: ReviewStatus) -> AdminReviewResponse:
    review = await reviews_repo.get_by_id(db, review_id)
    if review is None:
        raise ReviewNotFoundError()

    review.status = status
    await db.flush()
    await reviews_repo.recompute_braider_rating(db, review.braider_id)
    await db.commit()
    await db.refresh(review)

    braider_names = await braiders_repo.list_display_names(db, [review.braider_id])
    customer_names = await users_repo.list_full_names(db, [review.customer_id])
    return _to_admin_response(
        review,
        braider_name=braider_names.get(review.braider_id, ""),
        customer_name=customer_names.get(review.customer_id, ""),
    )


async def approve_review(db: AsyncSession, review_id: uuid.UUID) -> AdminReviewResponse:
    return await _moderate(db, review_id, status=ReviewStatus.APPROVED)


async def reject_review(db: AsyncSession, review_id: uuid.UUID) -> AdminReviewResponse:
    return await _moderate(db, review_id, status=ReviewStatus.REJECTED)
