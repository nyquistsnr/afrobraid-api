import uuid
from datetime import date

from arq import ArqRedis
from fastapi import APIRouter, Depends, Query, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.pagination import PaginationParams
from app.core.queue import get_task_queue
from app.core.redis import get_redis
from app.core.response import APIResponse
from app.modules.auth.dependencies import get_current_user
from app.modules.bookings import service
from app.modules.bookings.enums import BookingStatus
from app.modules.bookings.schemas import (
    BookingCreateRequest,
    BookingRescheduleRequest,
    BookingResponse,
    PaginatedBookingsResponse,
    ReceiptSummaryResponse,
)
from app.modules.users.models import User

router = APIRouter(prefix="/api/v1/bookings", tags=["Bookings"])

# Any authenticated user can book - a BRAIDER or ADMIN account is still a
# person who can want a style done, not just UserType.CUSTOMER ones. The
# self-booking guard in service.create_booking is what stops a braider from
# booking their own profile.
_require_customer = get_current_user


def _locale(request: Request) -> str:
    return getattr(request.state, "locale", "en")


@router.post(
    "",
    response_model=APIResponse[BookingResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Create a booking from a price quote",
    description=(
        "Consumes a DRAFT booking calculation and an appointment time. "
        "Everything else (braider, style, add-ons, mobile address) comes "
        "from the calculation, not this request. Re-validates and re-prices "
        "against current state - if the total drifted from the quote you "
        "saw, this 409s and you should fetch a fresh calculation."
    ),
)
async def create_booking(
    payload: BookingCreateRequest,
    request: Request,
    user: User = Depends(_require_customer),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    queue: ArqRedis = Depends(get_task_queue),
) -> APIResponse[BookingResponse]:
    result = await service.create_booking(db, redis, queue, user=user, data=payload, locale=_locale(request))
    return APIResponse(data=result)


@router.get(
    "",
    response_model=APIResponse[PaginatedBookingsResponse],
    summary="List your bookings",
    description=(
        "Filter by `status`, by appointment date with `date_from`/`date_to` "
        "(bounds `starts_at`, not when the booking was made; either side "
        "can be given alone), and free-text `search` (matches the style "
        "name or the braider's business/personal name)."
    ),
)
async def list_bookings(
    request: Request,
    status_filter: BookingStatus | None = Query(default=None, alias="status"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    search: str | None = Query(default=None),
    params: PaginationParams = Depends(),
    user: User = Depends(_require_customer),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[PaginatedBookingsResponse]:
    result = await service.list_bookings(
        db,
        user=user,
        params=params,
        locale=_locale(request),
        status=status_filter,
        date_from=date_from,
        date_to=date_to,
        search=search,
    )
    return APIResponse(data=result)


@router.get(
    "/today-count",
    response_model=APIResponse[int],
    summary="Total bookings made today",
    description="Public, no auth required. No params. Counts every booking created "
    "today (UTC calendar day), regardless of status.",
)
async def get_bookings_today_count(db: AsyncSession = Depends(get_db)) -> APIResponse[int]:
    result = await service.count_bookings_today(db)
    return APIResponse(data=result)


@router.get("/{booking_id}", response_model=APIResponse[BookingResponse], summary="Get a booking")
async def get_booking(
    booking_id: uuid.UUID,
    request: Request,
    user: User = Depends(_require_customer),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[BookingResponse]:
    result = await service.get_booking(db, booking_id, user=user, locale=_locale(request))
    return APIResponse(data=result)


@router.post(
    "/{booking_id}/reschedule",
    response_model=APIResponse[BookingResponse],
    summary="Reschedule a confirmed booking for free",
    description=(
        "Free at any time up until 24h before the current appointment "
        "(`cancellation_cutoff_at`); rejected inside that window. Only "
        "CONFIRMED bookings are eligible."
    ),
)
async def reschedule_booking(
    booking_id: uuid.UUID,
    payload: BookingRescheduleRequest,
    request: Request,
    user: User = Depends(_require_customer),
    db: AsyncSession = Depends(get_db),
    queue: ArqRedis = Depends(get_task_queue),
) -> APIResponse[BookingResponse]:
    result = await service.reschedule_booking(
        db, queue, booking_id, user=user, data=payload, locale=_locale(request)
    )
    return APIResponse(data=result)


@router.post(
    "/{booking_id}/pay",
    response_model=APIResponse[BookingResponse],
    summary="Resume an outstanding balance payment",
    description=(
        "Creates a fresh, on-session PaymentIntent for the "
        "remaining balance and returns its client_secret to confirm - for "
        "when the scheduled off-session charge needs a customer-present 3DS "
        "confirmation, or the customer just wants to pay before the next "
        "scheduled attempt. Calling it again after an abandoned or declined "
        "on-session attempt returns that same PaymentIntent's client_secret "
        "so the customer can retry with another payment method. The secret "
        "is on the BALANCE entry in `payments`. 409s if there's no "
        "outstanding balance to resume (already paid, not a "
        "deposit-then-balance booking, or an automatic charge is in flight)."
    ),
)
async def resume_booking_payment(
    booking_id: uuid.UUID,
    request: Request,
    user: User = Depends(_require_customer),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[BookingResponse]:
    result = await service.resume_booking_payment(db, booking_id, user=user, locale=_locale(request))
    return APIResponse(data=result)


@router.post(
    "/{booking_id}/cancel",
    response_model=APIResponse[BookingResponse],
    summary="Cancel a confirmed booking",
    description=(
        "Free at any time up until 24h before the appointment "
        "(`cancellation_cutoff_at`); rejected inside that window. The "
        "deposit (or full amount already captured) is non-refundable - "
        "only CONFIRMED bookings are eligible."
    ),
)
async def cancel_booking(
    booking_id: uuid.UUID,
    request: Request,
    user: User = Depends(_require_customer),
    db: AsyncSession = Depends(get_db),
    queue: ArqRedis = Depends(get_task_queue),
) -> APIResponse[BookingResponse]:
    result = await service.cancel_booking_by_customer(db, queue, booking_id, user=user, locale=_locale(request))
    return APIResponse(data=result)


@router.get(
    "/{booking_id}/receipts",
    response_model=APIResponse[list[ReceiptSummaryResponse]],
    summary="List a booking's receipts",
    description=(
        "Every invoice (one per succeeded payment) and credit note (one "
        "per refund) issued for this booking, oldest first. `url` points "
        "at the public, no-auth receipt page."
    ),
)
async def list_booking_receipts(
    booking_id: uuid.UUID,
    user: User = Depends(_require_customer),
    db: AsyncSession = Depends(get_db),
) -> APIResponse[list[ReceiptSummaryResponse]]:
    result = await service.list_booking_receipts(db, booking_id, user=user)
    return APIResponse(data=result)
