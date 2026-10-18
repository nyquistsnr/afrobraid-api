import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import Integer, cast, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.config import get_settings
from app.core.currency import Currency
from app.core.pagination import PaginationMeta, PaginationParams, paginate
from app.modules.bookings.enums import (
    CALENDAR_BLOCKING_STATUSES,
    DECLINED_BOOKING_STATUSES,
    UPCOMING_BOOKING_STATUSES,
    BalanceChargeState,
    BookingItemType,
    BookingStatus,
    PaymentPurpose,
    PaymentSchedule,
    PaymentStatus,
    TransferStatus,
)
from app.modules.bookings.models import (
    Booking,
    BookingItem,
    BookingPayment,
    BookingRefund,
    BookingTransfer,
    BookingTransferReversal,
)
from app.modules.braiders.models import BraiderProfile
from app.modules.platform_settings.models import SettingValueType
from app.modules.styles.models import Style
from app.modules.users.models import User


async def get_booking_by_id(db: AsyncSession, booking_id: uuid.UUID) -> Booking | None:
    return await db.get(Booking, booking_id)


async def list_bookings_by_ids(
    db: AsyncSession, booking_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Booking]:
    if not booking_ids:
        return {}
    result = await db.execute(select(Booking).where(Booking.id.in_(booking_ids)))
    return {booking.id: booking for booking in result.scalars().all()}


async def get_booking_by_id_for_update(db: AsyncSession, booking_id: uuid.UUID) -> Booking | None:
    """Same as `get_booking_by_id` but takes a row lock - used by reschedule
    to serialize against the balance-charge cron and concurrent
    reschedule/cancel calls racing on the same booking."""
    result = await db.execute(
        select(Booking).where(Booking.id == booking_id).with_for_update()
    )
    return result.scalar_one_or_none()


async def get_booking_by_calculation_id(
    db: AsyncSession, booking_calculation_id: uuid.UUID
) -> Booking | None:
    result = await db.execute(
        select(Booking).where(Booking.booking_calculation_id == booking_calculation_id)
    )
    return result.scalar_one_or_none()


async def list_items(db: AsyncSession, booking_id: uuid.UUID) -> list[BookingItem]:
    result = await db.execute(select(BookingItem).where(BookingItem.booking_id == booking_id))
    return list(result.scalars().all())


async def list_payments(db: AsyncSession, booking_id: uuid.UUID) -> list[BookingPayment]:
    result = await db.execute(
        select(BookingPayment)
        .where(BookingPayment.booking_id == booking_id)
        .order_by(BookingPayment.created_at)
    )
    return list(result.scalars().all())


async def get_payment_by_id(db: AsyncSession, payment_id: uuid.UUID) -> BookingPayment | None:
    return await db.get(BookingPayment, payment_id)


async def list_succeeded_payments(db: AsyncSession, booking_id: uuid.UUID) -> list[BookingPayment]:
    result = await db.execute(
        select(BookingPayment).where(
            BookingPayment.booking_id == booking_id, BookingPayment.status == PaymentStatus.SUCCEEDED
        )
    )
    return list(result.scalars().all())


async def create_refund(
    db: AsyncSession,
    *,
    booking_id: uuid.UUID,
    booking_payment_id: uuid.UUID,
    amount_minor: int,
    currency: Currency,
    idempotency_key: str,
) -> BookingRefund:
    refund = BookingRefund(
        booking_id=booking_id,
        booking_payment_id=booking_payment_id,
        amount_minor=amount_minor,
        currency=currency,
        idempotency_key=idempotency_key,
    )
    db.add(refund)
    await db.flush()
    return refund


async def create_transfer(
    db: AsyncSession,
    *,
    booking_id: uuid.UUID,
    booking_payment_id: uuid.UUID,
    destination_account_id: str,
    amount_minor: int,
    currency: Currency,
    transfer_group: str,
    idempotency_key: str,
) -> BookingTransfer:
    transfer = BookingTransfer(
        booking_id=booking_id,
        booking_payment_id=booking_payment_id,
        destination_account_id=destination_account_id,
        amount_minor=amount_minor,
        currency=currency,
        transfer_group=transfer_group,
        idempotency_key=idempotency_key,
    )
    db.add(transfer)
    await db.flush()
    return transfer


async def list_active_transfers_for_booking(db: AsyncSession, booking_id: uuid.UUID) -> list[BookingTransfer]:
    """PENDING/SUCCEEDED transfers only - what a braider cancellation needs
    to reverse. In current codebase state this is only ever populated by
    the customer-cancellation deposit-share release, since the general
    completion payout (Phase 5) doesn't exist yet - kept generic so it
    still does the right thing once that lands."""
    result = await db.execute(
        select(BookingTransfer).where(
            BookingTransfer.booking_id == booking_id,
            BookingTransfer.status.in_([TransferStatus.PENDING, TransferStatus.SUCCEEDED]),
        )
    )
    return list(result.scalars().all())


async def get_payment_by_stripe_intent_id(
    db: AsyncSession, stripe_payment_intent_id: str
) -> BookingPayment | None:
    result = await db.execute(
        select(BookingPayment).where(
            BookingPayment.stripe_payment_intent_id == stripe_payment_intent_id
        )
    )
    return result.scalar_one_or_none()


async def get_pending_payment(
    db: AsyncSession, booking_id: uuid.UUID, purpose: PaymentPurpose
) -> BookingPayment | None:
    result = await db.execute(
        select(BookingPayment).where(
            BookingPayment.booking_id == booking_id,
            BookingPayment.purpose == purpose,
            BookingPayment.status == PaymentStatus.PENDING,
        )
    )
    return result.scalars().first()


async def list_stale_pending_payments(
    db: AsyncSession, *, older_than: datetime, limit: int
) -> list[BookingPayment]:
    """Reconciliation's target set (design correction #7's safety net) -
    a payment whose PaymentIntent was created but has sat PENDING past
    `reconcile_pending_payment_after_minutes` with no webhook (success or
    failure) ever having landed."""
    result = await db.execute(
        select(BookingPayment)
        .where(
            BookingPayment.status == PaymentStatus.PENDING,
            BookingPayment.stripe_payment_intent_id.is_not(None),
            BookingPayment.created_at <= older_than,
        )
        .limit(limit)
    )
    return list(result.scalars().all())


async def list_bookings_with_abandoned_on_session_balance(
    db: AsyncSession, *, idle_since: datetime, limit: int
) -> list[uuid.UUID]:
    """Candidates for release_abandoned_balance_payment: bookings still
    IN_PROGRESS on a customer-initiated (on-session) BALANCE payment that
    hasn't been touched since `idle_since` - `updated_at` moves whenever
    the customer resumes it (POST /pay) or a decline webhook lands. Only a
    candidate list; the release re-checks everything under FOR UPDATE."""
    result = await db.execute(
        select(Booking.id)
        .join(BookingPayment, BookingPayment.booking_id == Booking.id)
        .where(
            Booking.balance_charge_state == BalanceChargeState.IN_PROGRESS,
            BookingPayment.purpose == PaymentPurpose.BALANCE,
            BookingPayment.is_off_session.is_(False),
            BookingPayment.status.in_((PaymentStatus.PENDING, PaymentStatus.FAILED)),
            BookingPayment.stripe_payment_intent_id.is_not(None),
            BookingPayment.updated_at <= idle_since,
        )
        .distinct()
        .limit(limit)
    )
    return list(result.scalars().all())


async def list_pending_payments_for_booking(db: AsyncSession, booking_id: uuid.UUID) -> list[BookingPayment]:
    """Admin-triggered reconciliation for one booking - unlike the cron,
    ignores the age threshold since an admin explicitly asked for this
    right now."""
    result = await db.execute(
        select(BookingPayment).where(
            BookingPayment.booking_id == booking_id,
            BookingPayment.status == PaymentStatus.PENDING,
            BookingPayment.stripe_payment_intent_id.is_not(None),
        )
    )
    return list(result.scalars().all())


def _apply_common_filters(
    stmt,
    *,
    status: BookingStatus | None,
    date_from: date | None,
    date_to: date | None,
):
    """Shared status/appointment-date filters for both the customer's and
    the braider's booking lists. Dates bound `starts_at` (the appointment
    time), not `created_at` - what a user wants to browse by is when the
    appointment is, not when they booked it. Bounds are whole UTC calendar
    days, inclusive on both ends."""
    if status is not None:
        stmt = stmt.where(Booking.status == status)
    if date_from is not None:
        stmt = stmt.where(Booking.starts_at >= datetime.combine(date_from, time.min, tzinfo=UTC))
    if date_to is not None:
        stmt = stmt.where(
            Booking.starts_at < datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=UTC)
        )
    return stmt


async def list_bookings_for_customer(
    db: AsyncSession,
    customer_id: uuid.UUID,
    *,
    params: PaginationParams,
    status: BookingStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    search: str | None = None,
) -> tuple[list[Booking], object]:
    stmt = select(Booking).where(Booking.customer_id == customer_id)
    stmt = _apply_common_filters(stmt, status=status, date_from=date_from, date_to=date_to)

    if search:
        pattern = f"%{search}%"
        braider_user = aliased(User)
        stmt = (
            stmt.join(Style, Style.id == Booking.style_id)
            .join(BraiderProfile, BraiderProfile.id == Booking.braider_id)
            .join(braider_user, braider_user.id == BraiderProfile.user_id)
            .where(
                or_(
                    Style.name_en.ilike(pattern),
                    Style.name_de.ilike(pattern),
                    Style.name_fr.ilike(pattern),
                    BraiderProfile.business_name.ilike(pattern),
                    braider_user.first_name.ilike(pattern),
                    braider_user.last_name.ilike(pattern),
                )
            )
        )

    stmt = stmt.order_by(Booking.starts_at.desc())
    return await paginate(db, stmt, params)


async def list_bookings_for_braider(
    db: AsyncSession,
    braider_id: uuid.UUID,
    *,
    params: PaginationParams,
    status: BookingStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    search: str | None = None,
) -> tuple[list[Booking], object]:
    stmt = select(Booking).where(Booking.braider_id == braider_id)
    stmt = _apply_common_filters(stmt, status=status, date_from=date_from, date_to=date_to)

    if search:
        pattern = f"%{search}%"
        customer_user = aliased(User)
        stmt = (
            stmt.join(Style, Style.id == Booking.style_id)
            .join(customer_user, customer_user.id == Booking.customer_id)
            .where(
                or_(
                    Style.name_en.ilike(pattern),
                    Style.name_de.ilike(pattern),
                    Style.name_fr.ilike(pattern),
                    customer_user.first_name.ilike(pattern),
                    customer_user.last_name.ilike(pattern),
                )
            )
        )

    stmt = stmt.order_by(Booking.starts_at.desc())
    return await paginate(db, stmt, params)


async def get_booking_stats_for_braider(
    db: AsyncSession,
    braider_id: uuid.UUID,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, int]:
    """Single grouped-count query behind the braider's booking-stats tile:
    total, completed, declined (DECLINED_BOOKING_STATUSES), and upcoming
    (UPCOMING_BOOKING_STATUSES). Bounds `starts_at`, same convention as
    `_apply_common_filters`."""
    stmt = select(Booking.status, func.count()).where(Booking.braider_id == braider_id)
    stmt = _apply_common_filters(stmt, status=None, date_from=date_from, date_to=date_to)
    stmt = stmt.group_by(Booking.status)
    result = await db.execute(stmt)
    counts_by_status: dict[BookingStatus, int] = {row[0]: row[1] for row in result.all()}

    return {
        "total_bookings": sum(counts_by_status.values()),
        "completed": counts_by_status.get(BookingStatus.COMPLETED, 0),
        "declined": sum(counts_by_status.get(s, 0) for s in DECLINED_BOOKING_STATUSES),
        "upcoming": sum(counts_by_status.get(s, 0) for s in UPCOMING_BOOKING_STATUSES),
    }


async def get_booking_timeseries_for_braider(
    db: AsyncSession,
    braider_id: uuid.UUID,
    *,
    date_from: date,
    date_to: date,
    interval: str,
    statuses: list[BookingStatus] | None,
) -> list[tuple[datetime, BookingStatus, int]]:
    """One row per (bucket, status) with a non-zero count, grouped by
    `date_trunc(interval, starts_at)` - the raw material for a multi-line
    graph, one line per status. `interval` must already be validated to one
    of 'day'/'week'/'month' by the caller (it's interpolated as a SQL
    identifier via `date_trunc`, not a bound parameter)."""
    bucket = func.date_trunc(interval, Booking.starts_at).label("bucket")
    stmt = (
        select(bucket, Booking.status, func.count())
        .where(Booking.braider_id == braider_id)
        .group_by(bucket, Booking.status)
        .order_by(bucket)
    )
    stmt = _apply_common_filters(stmt, status=None, date_from=date_from, date_to=date_to)
    if statuses:
        stmt = stmt.where(Booking.status.in_(statuses))
    result = await db.execute(stmt)
    return [(row[0], row[1], row[2]) for row in result.all()]


def _apply_payment_date_filters(stmt, *, date_from: date | None, date_to: date | None):
    """Bounds `BookingPayment.created_at` - when the charge/refund actually
    happened - mirroring `_apply_common_filters`'s whole-UTC-day convention.
    This is a cash-flow view, not a schedule view, so it deliberately does
    not bound the booking's appointment date."""
    if date_from is not None:
        stmt = stmt.where(BookingPayment.created_at >= datetime.combine(date_from, time.min, tzinfo=UTC))
    if date_to is not None:
        stmt = stmt.where(
            BookingPayment.created_at
            < datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=UTC)
        )
    return stmt


def _apply_booking_created_date_filters(
    stmt, *, created_from: date | None, created_to: date | None
):
    if created_from is not None:
        stmt = stmt.where(
            Booking.created_at >= datetime.combine(created_from, time.min, tzinfo=UTC)
        )
    if created_to is not None:
        stmt = stmt.where(
            Booking.created_at
            < datetime.combine(created_to + timedelta(days=1), time.min, tzinfo=UTC)
        )
    return stmt


def _apply_admin_booking_filters(
    stmt,
    *,
    status: BookingStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
    customer_id: uuid.UUID | None = None,
    braider_id: uuid.UUID | None = None,
    country: str | None = None,
    currency: Currency | None = None,
    is_mobile: bool | None = None,
    payment_schedule: PaymentSchedule | None = None,
    search: str | None = None,
):
    stmt = _apply_common_filters(stmt, status=status, date_from=date_from, date_to=date_to)
    stmt = _apply_booking_created_date_filters(
        stmt, created_from=created_from, created_to=created_to
    )

    if customer_id is not None:
        stmt = stmt.where(Booking.customer_id == customer_id)
    if braider_id is not None:
        stmt = stmt.where(Booking.braider_id == braider_id)
    if country is not None:
        stmt = stmt.where(Booking.country == country.upper())
    if currency is not None:
        stmt = stmt.where(Booking.currency == currency)
    if is_mobile is not None:
        stmt = stmt.where(Booking.is_mobile == is_mobile)
    if payment_schedule is not None:
        stmt = stmt.where(Booking.payment_schedule == payment_schedule)

    if search:
        pattern = f"%{search.strip()}%"
        customer_user = aliased(User)
        braider_user = aliased(User)
        stmt = (
            stmt.join(Style, Style.id == Booking.style_id)
            .join(customer_user, customer_user.id == Booking.customer_id)
            .join(BraiderProfile, BraiderProfile.id == Booking.braider_id)
            .join(braider_user, braider_user.id == BraiderProfile.user_id)
            .where(
                or_(
                    Booking.reference.ilike(pattern),
                    Style.name_en.ilike(pattern),
                    Style.name_de.ilike(pattern),
                    Style.name_fr.ilike(pattern),
                    customer_user.first_name.ilike(pattern),
                    customer_user.last_name.ilike(pattern),
                    customer_user.email.ilike(pattern),
                    func.concat(
                        customer_user.first_name,
                        " ",
                        func.coalesce(customer_user.last_name, ""),
                    ).ilike(pattern),
                    BraiderProfile.business_name.ilike(pattern),
                    braider_user.first_name.ilike(pattern),
                    braider_user.last_name.ilike(pattern),
                    braider_user.email.ilike(pattern),
                    func.concat(
                        braider_user.first_name,
                        " ",
                        func.coalesce(braider_user.last_name, ""),
                    ).ilike(pattern),
                )
            )
        )

    return stmt


async def list_bookings_for_admin(
    db: AsyncSession,
    *,
    params: PaginationParams,
    status: BookingStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
    customer_id: uuid.UUID | None = None,
    braider_id: uuid.UUID | None = None,
    country: str | None = None,
    currency: Currency | None = None,
    is_mobile: bool | None = None,
    payment_schedule: PaymentSchedule | None = None,
    search: str | None = None,
) -> tuple[list[Booking], PaginationMeta]:
    stmt = _apply_admin_booking_filters(
        select(Booking),
        status=status,
        date_from=date_from,
        date_to=date_to,
        created_from=created_from,
        created_to=created_to,
        customer_id=customer_id,
        braider_id=braider_id,
        country=country,
        currency=currency,
        is_mobile=is_mobile,
        payment_schedule=payment_schedule,
        search=search,
    )
    stmt = stmt.order_by(Booking.created_at.desc(), Booking.starts_at.desc())
    return await paginate(db, stmt, params)


async def get_booking_stats_for_admin(
    db: AsyncSession,
    *,
    status: BookingStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
    payment_date_from: date | None = None,
    payment_date_to: date | None = None,
    customer_id: uuid.UUID | None = None,
    braider_id: uuid.UUID | None = None,
    country: str | None = None,
    currency: Currency | None = None,
    is_mobile: bool | None = None,
    payment_schedule: PaymentSchedule | None = None,
    search: str | None = None,
) -> dict:
    filtered = _apply_admin_booking_filters(
        select(Booking),
        status=status,
        date_from=date_from,
        date_to=date_to,
        created_from=created_from,
        created_to=created_to,
        customer_id=customer_id,
        braider_id=braider_id,
        country=country,
        currency=currency,
        is_mobile=is_mobile,
        payment_schedule=payment_schedule,
        search=search,
    ).cte("filtered_bookings")

    def _repeat_count(column):
        return (
            select(func.count())
            .select_from(select(column).group_by(column).having(func.count() > 1).subquery())
            .scalar_subquery()
        )

    # One round trip for status counts, totals and repeat counts (previously four).
    status_columns = [
        func.count().filter(filtered.c.status == s).label(f"status_{s.name}") for s in BookingStatus
    ]
    summary = (
        await db.execute(
            select(
                *status_columns,
                func.coalesce(func.sum(filtered.c.total), 0),
                func.coalesce(func.sum(filtered.c.service_subtotal), 0),
                func.coalesce(func.sum(filtered.c.platform_fee), 0),
                func.coalesce(func.sum(filtered.c.vat_total), 0),
                func.coalesce(func.sum(cast(filtered.c.is_mobile, Integer)), 0),
                func.count(func.distinct(filtered.c.customer_id)),
                func.count(func.distinct(filtered.c.braider_id)),
                _repeat_count(filtered.c.customer_id),
                _repeat_count(filtered.c.braider_id),
            )
        )
    ).one()
    status_count = len(status_columns)
    counts_by_status: dict[BookingStatus, int] = {
        s: summary[i] for i, s in enumerate(BookingStatus) if summary[i]
    }
    totals_row = summary[status_count : status_count + 7]
    repeated_customers = summary[status_count + 7] or 0
    repeated_braiders = summary[status_count + 8] or 0

    payment_stmt = (
        select(
            BookingPayment.status,
            func.coalesce(func.sum(BookingPayment.amount_minor), 0),
            func.coalesce(func.sum(BookingPayment.amount_refunded_minor), 0),
            func.coalesce(func.sum(BookingPayment.braider_share_minor), 0),
        )
        .join(filtered, filtered.c.id == BookingPayment.booking_id)
        .group_by(BookingPayment.status)
    )
    payment_stmt = _apply_payment_date_filters(
        payment_stmt, date_from=payment_date_from, date_to=payment_date_to
    )
    payment_rows = (await db.execute(payment_stmt)).all()

    total_bookings = sum(counts_by_status.values())
    total_booking_value = totals_row[0] or Decimal("0.00")
    return {
        "total_bookings": total_bookings,
        "status_counts": counts_by_status,
        "completed_bookings": counts_by_status.get(BookingStatus.COMPLETED, 0),
        "upcoming_bookings": sum(counts_by_status.get(s, 0) for s in UPCOMING_BOOKING_STATUSES),
        "declined_bookings": sum(counts_by_status.get(s, 0) for s in DECLINED_BOOKING_STATUSES),
        "pending_payment_bookings": counts_by_status.get(BookingStatus.PENDING_PAYMENT, 0),
        "no_show_bookings": counts_by_status.get(BookingStatus.NO_SHOW, 0),
        "disputed_bookings": counts_by_status.get(BookingStatus.DISPUTED, 0),
        "mobile_bookings": totals_row[4] or 0,
        "salon_bookings": total_bookings - (totals_row[4] or 0),
        "unique_customers": totals_row[5] or 0,
        "repeat_customers": repeated_customers,
        "unique_braiders": totals_row[6] or 0,
        "repeat_braiders": repeated_braiders,
        "total_booking_value": total_booking_value,
        "average_booking_value": total_booking_value / total_bookings
        if total_bookings
        else Decimal("0.00"),
        "service_subtotal": totals_row[1] or Decimal("0.00"),
        "platform_fee_total": totals_row[2] or Decimal("0.00"),
        "vat_total": totals_row[3] or Decimal("0.00"),
        "total_paid_minor": sum(r[1] for r in payment_rows if r[0] == PaymentStatus.SUCCEEDED),
        "total_refunded_minor": sum(r[2] for r in payment_rows),
        "pending_payment_amount_minor": sum(
            r[1] for r in payment_rows if r[0] == PaymentStatus.PENDING
        ),
        "braider_earnings_minor": sum(
            r[3] for r in payment_rows if r[0] == PaymentStatus.SUCCEEDED
        ),
    }


def _admin_payment_amount_column(metric: str):
    if metric == "BRAIDER_EARNINGS":
        return BookingPayment.braider_share_minor
    return BookingPayment.amount_minor


def _admin_payment_amount_subquery(
    *,
    filtered,
    metric: str,
    payment_date_from: date | None = None,
    payment_date_to: date | None = None,
):
    amount_column = _admin_payment_amount_column(metric)
    stmt = (
        select(
            BookingPayment.booking_id.label("booking_id"),
            func.coalesce(func.sum(amount_column), 0).label("amount_minor"),
        )
        .where(BookingPayment.status == PaymentStatus.SUCCEEDED)
        # Only aggregate payments for the bookings being charted, not the whole table.
        .where(BookingPayment.booking_id.in_(select(filtered.c.id)))
        .group_by(BookingPayment.booking_id)
    )
    stmt = _apply_payment_date_filters(
        stmt, date_from=payment_date_from, date_to=payment_date_to
    )
    return stmt.subquery()


async def get_admin_revenue_timeseries(
    db: AsyncSession,
    *,
    metric: str,
    date_from: date,
    date_to: date,
    interval: str,
    payment_date_from: date | None = None,
    payment_date_to: date | None = None,
    customer_id: uuid.UUID | None = None,
    braider_id: uuid.UUID | None = None,
    country: str | None = None,
    currency: Currency | None = None,
    is_mobile: bool | None = None,
    payment_schedule: PaymentSchedule | None = None,
    search: str | None = None,
) -> list[tuple[datetime, int, int]]:
    filtered = _apply_admin_booking_filters(
        select(Booking),
        date_from=date_from,
        date_to=date_to,
        customer_id=customer_id,
        braider_id=braider_id,
        country=country,
        currency=currency,
        is_mobile=is_mobile,
        payment_schedule=payment_schedule,
        search=search,
    ).subquery()
    payment_amounts = _admin_payment_amount_subquery(
        filtered=filtered,
        metric=metric,
        payment_date_from=payment_date_from,
        payment_date_to=payment_date_to,
    )
    bucket = func.date_trunc(interval, filtered.c.starts_at).label("bucket")
    stmt = (
        select(
            bucket,
            func.coalesce(func.sum(payment_amounts.c.amount_minor), 0),
            func.count(func.distinct(filtered.c.id)),
        )
        .outerjoin(payment_amounts, payment_amounts.c.booking_id == filtered.c.id)
        .group_by(bucket)
        .order_by(bucket)
    )
    rows = (await db.execute(stmt)).all()
    return [(row[0], row[1], row[2]) for row in rows]


async def get_admin_bookings_by_weekday(
    db: AsyncSession,
    *,
    metric: str,
    date_from: date | None = None,
    date_to: date | None = None,
    payment_date_from: date | None = None,
    payment_date_to: date | None = None,
    customer_id: uuid.UUID | None = None,
    braider_id: uuid.UUID | None = None,
    country: str | None = None,
    currency: Currency | None = None,
    is_mobile: bool | None = None,
    payment_schedule: PaymentSchedule | None = None,
    search: str | None = None,
) -> list[tuple[int, int, int]]:
    filtered = _apply_admin_booking_filters(
        select(Booking),
        date_from=date_from,
        date_to=date_to,
        customer_id=customer_id,
        braider_id=braider_id,
        country=country,
        currency=currency,
        is_mobile=is_mobile,
        payment_schedule=payment_schedule,
        search=search,
    ).subquery()
    payment_amounts = _admin_payment_amount_subquery(
        filtered=filtered,
        metric=metric,
        payment_date_from=payment_date_from,
        payment_date_to=payment_date_to,
    )
    weekday = func.extract("isodow", filtered.c.starts_at).label("weekday")
    stmt = (
        select(
            weekday,
            func.count(func.distinct(filtered.c.id)),
            func.coalesce(func.sum(payment_amounts.c.amount_minor), 0),
        )
        .outerjoin(payment_amounts, payment_amounts.c.booking_id == filtered.c.id)
        .group_by(weekday)
        .order_by(weekday)
    )
    rows = (await db.execute(stmt)).all()
    return [(int(row[0]), row[1], row[2]) for row in rows]


async def get_admin_status_breakdown(
    db: AsyncSession,
    *,
    metric: str,
    date_from: date | None = None,
    date_to: date | None = None,
    payment_date_from: date | None = None,
    payment_date_to: date | None = None,
    customer_id: uuid.UUID | None = None,
    braider_id: uuid.UUID | None = None,
    country: str | None = None,
    currency: Currency | None = None,
    is_mobile: bool | None = None,
    payment_schedule: PaymentSchedule | None = None,
    search: str | None = None,
) -> list[tuple[BookingStatus, int, int]]:
    filtered = _apply_admin_booking_filters(
        select(Booking),
        date_from=date_from,
        date_to=date_to,
        customer_id=customer_id,
        braider_id=braider_id,
        country=country,
        currency=currency,
        is_mobile=is_mobile,
        payment_schedule=payment_schedule,
        search=search,
    ).subquery()
    payment_amounts = _admin_payment_amount_subquery(
        filtered=filtered,
        metric=metric,
        payment_date_from=payment_date_from,
        payment_date_to=payment_date_to,
    )
    stmt = (
        select(
            filtered.c.status,
            func.count(func.distinct(filtered.c.id)),
            func.coalesce(func.sum(payment_amounts.c.amount_minor), 0),
        )
        .outerjoin(payment_amounts, payment_amounts.c.booking_id == filtered.c.id)
        .group_by(filtered.c.status)
        .order_by(filtered.c.status)
    )
    rows = (await db.execute(stmt)).all()
    return [(row[0], row[1], row[2]) for row in rows]


async def get_admin_style_breakdown(
    db: AsyncSession,
    *,
    metric: str,
    date_from: date | None = None,
    date_to: date | None = None,
    payment_date_from: date | None = None,
    payment_date_to: date | None = None,
    customer_id: uuid.UUID | None = None,
    braider_id: uuid.UUID | None = None,
    country: str | None = None,
    currency: Currency | None = None,
    is_mobile: bool | None = None,
    payment_schedule: PaymentSchedule | None = None,
    search: str | None = None,
) -> list[tuple[uuid.UUID, Style, int, int]]:
    filtered = _apply_admin_booking_filters(
        select(Booking),
        date_from=date_from,
        date_to=date_to,
        customer_id=customer_id,
        braider_id=braider_id,
        country=country,
        currency=currency,
        is_mobile=is_mobile,
        payment_schedule=payment_schedule,
        search=search,
    ).subquery()
    payment_amounts = _admin_payment_amount_subquery(
        filtered=filtered,
        metric=metric,
        payment_date_from=payment_date_from,
        payment_date_to=payment_date_to,
    )
    amount = func.coalesce(func.sum(payment_amounts.c.amount_minor), 0)
    stmt = (
        select(filtered.c.style_id, Style, func.count(func.distinct(filtered.c.id)), amount)
        .join(Style, Style.id == filtered.c.style_id)
        .outerjoin(payment_amounts, payment_amounts.c.booking_id == filtered.c.id)
        .group_by(filtered.c.style_id, Style.id)
        .order_by(amount.desc(), func.count(func.distinct(filtered.c.id)).desc())
    )
    rows = (await db.execute(stmt)).all()
    return [(row[0], row[1], row[2], row[3]) for row in rows]


async def get_admin_country_breakdown(
    db: AsyncSession,
    *,
    metric: str,
    date_from: date | None = None,
    date_to: date | None = None,
    payment_date_from: date | None = None,
    payment_date_to: date | None = None,
    country: str | None = None,
    currency: Currency | None = None,
    is_mobile: bool | None = None,
    payment_schedule: PaymentSchedule | None = None,
    search: str | None = None,
) -> list[tuple[str, int, int]]:
    filtered = _apply_admin_booking_filters(
        select(Booking),
        date_from=date_from,
        date_to=date_to,
        country=country,
        currency=currency,
        is_mobile=is_mobile,
        payment_schedule=payment_schedule,
        search=search,
    ).subquery()
    payment_amounts = _admin_payment_amount_subquery(
        filtered=filtered,
        metric=metric,
        payment_date_from=payment_date_from,
        payment_date_to=payment_date_to,
    )
    amount = func.coalesce(func.sum(payment_amounts.c.amount_minor), 0)
    stmt = (
        select(filtered.c.country, func.count(func.distinct(filtered.c.id)), amount)
        .outerjoin(payment_amounts, payment_amounts.c.booking_id == filtered.c.id)
        .group_by(filtered.c.country)
        .order_by(amount.desc(), func.count(func.distinct(filtered.c.id)).desc())
    )
    rows = (await db.execute(stmt)).all()
    return [(row[0], row[1], row[2]) for row in rows]


async def get_payment_stats_for_braider(
    db: AsyncSession,
    braider_id: uuid.UUID,
    *,
    status: PaymentStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[str, int]:
    """Money-in/out summary in minor units: total received (SUCCEEDED
    payments), total refunded, and pending (not yet settled). Net revenue is
    derived by the caller (received - refunded) rather than computed here,
    since it needs both regardless of any `status` filter applied."""
    stmt = (
        select(
            BookingPayment.status,
            func.coalesce(func.sum(BookingPayment.amount_minor), 0),
            func.coalesce(func.sum(BookingPayment.amount_refunded_minor), 0),
        )
        .join(Booking, Booking.id == BookingPayment.booking_id)
        .where(Booking.braider_id == braider_id)
        .group_by(BookingPayment.status)
    )
    if status is not None:
        stmt = stmt.where(BookingPayment.status == status)
    stmt = _apply_payment_date_filters(stmt, date_from=date_from, date_to=date_to)

    result = await db.execute(stmt)
    rows = result.all()

    return {
        "total_received_minor": sum(
            r[1] for r in rows if r[0] == PaymentStatus.SUCCEEDED
        ),
        "total_refunded_minor": sum(r[2] for r in rows),
        "pending_minor": sum(r[1] for r in rows if r[0] == PaymentStatus.PENDING),
    }


async def list_payments_for_braider(
    db: AsyncSession,
    braider_id: uuid.UUID,
    *,
    params: PaginationParams,
    purpose: PaymentPurpose | None = None,
    status: PaymentStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> tuple[list[BookingPayment], PaginationMeta]:
    stmt = (
        select(BookingPayment)
        .join(Booking, Booking.id == BookingPayment.booking_id)
        .where(Booking.braider_id == braider_id)
    )
    if purpose is not None:
        stmt = stmt.where(BookingPayment.purpose == purpose)
    if status is not None:
        stmt = stmt.where(BookingPayment.status == status)
    stmt = _apply_payment_date_filters(stmt, date_from=date_from, date_to=date_to)
    stmt = stmt.order_by(BookingPayment.created_at.desc())
    return await paginate(db, stmt, params)


async def list_payments_for_admin(
    db: AsyncSession,
    *,
    params: PaginationParams,
    purpose: PaymentPurpose | None = None,
    status: PaymentStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    booking_date_from: date | None = None,
    booking_date_to: date | None = None,
    customer_id: uuid.UUID | None = None,
    braider_id: uuid.UUID | None = None,
    booking_id: uuid.UUID | None = None,
    currency: Currency | None = None,
    is_refunded: bool | None = None,
    search: str | None = None,
) -> tuple[list[BookingPayment], PaginationMeta]:
    stmt = select(BookingPayment).join(Booking, Booking.id == BookingPayment.booking_id)

    if purpose is not None:
        stmt = stmt.where(BookingPayment.purpose == purpose)
    if status is not None:
        stmt = stmt.where(BookingPayment.status == status)
    if customer_id is not None:
        stmt = stmt.where(Booking.customer_id == customer_id)
    if braider_id is not None:
        stmt = stmt.where(Booking.braider_id == braider_id)
    if booking_id is not None:
        stmt = stmt.where(BookingPayment.booking_id == booking_id)
    if currency is not None:
        stmt = stmt.where(BookingPayment.currency == currency)
    if is_refunded is not None:
        if is_refunded:
            stmt = stmt.where(BookingPayment.amount_refunded_minor > 0)
        else:
            stmt = stmt.where(BookingPayment.amount_refunded_minor == 0)

    stmt = _apply_payment_date_filters(stmt, date_from=date_from, date_to=date_to)
    stmt = _apply_common_filters(
        stmt, status=None, date_from=booking_date_from, date_to=booking_date_to
    )

    if search:
        pattern = f"%{search.strip()}%"
        customer_user = aliased(User)
        braider_user = aliased(User)
        stmt = (
            stmt.join(customer_user, customer_user.id == Booking.customer_id)
            .join(BraiderProfile, BraiderProfile.id == Booking.braider_id)
            .join(braider_user, braider_user.id == BraiderProfile.user_id)
            .where(
                or_(
                    Booking.reference.ilike(pattern),
                    BookingPayment.stripe_payment_intent_id.ilike(pattern),
                    BookingPayment.stripe_charge_id.ilike(pattern),
                    customer_user.first_name.ilike(pattern),
                    customer_user.last_name.ilike(pattern),
                    customer_user.email.ilike(pattern),
                    func.concat(
                        customer_user.first_name,
                        " ",
                        func.coalesce(customer_user.last_name, ""),
                    ).ilike(pattern),
                    BraiderProfile.business_name.ilike(pattern),
                    braider_user.first_name.ilike(pattern),
                    braider_user.last_name.ilike(pattern),
                    braider_user.email.ilike(pattern),
                    func.concat(
                        braider_user.first_name,
                        " ",
                        func.coalesce(braider_user.last_name, ""),
                    ).ilike(pattern),
                )
            )
        )

    stmt = stmt.order_by(BookingPayment.created_at.desc(), BookingPayment.id.desc())
    return await paginate(db, stmt, params)


async def get_dashboard_overview_for_braider(
    db: AsyncSession,
    braider_id: uuid.UUID,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict:
    """Everything behind the dashboard's overview tiles in three queries:
    per-status booking counts, per-customer booking counts (for
    unique/repeat customer counts), and braider-share revenue from
    succeeded payments. All three bound `starts_at` via
    `_apply_common_filters`, so "revenue" here means revenue attributed to
    appointments in range, not cash collected in range (that's the
    payments/stats endpoint's job, which bounds `created_at` instead)."""
    status_stmt = select(Booking.status, func.count()).where(Booking.braider_id == braider_id)
    status_stmt = _apply_common_filters(status_stmt, status=None, date_from=date_from, date_to=date_to)
    status_stmt = status_stmt.group_by(Booking.status)
    counts_by_status: dict[BookingStatus, int] = {
        row[0]: row[1] for row in (await db.execute(status_stmt)).all()
    }

    customer_stmt = select(Booking.customer_id, func.count()).where(Booking.braider_id == braider_id)
    customer_stmt = _apply_common_filters(customer_stmt, status=None, date_from=date_from, date_to=date_to)
    customer_stmt = customer_stmt.group_by(Booking.customer_id)
    customer_rows = (await db.execute(customer_stmt)).all()

    revenue_stmt = (
        select(func.coalesce(func.sum(BookingPayment.braider_share_minor), 0))
        .join(Booking, Booking.id == BookingPayment.booking_id)
        .where(Booking.braider_id == braider_id, BookingPayment.status == PaymentStatus.SUCCEEDED)
    )
    revenue_stmt = _apply_common_filters(revenue_stmt, status=None, date_from=date_from, date_to=date_to)
    revenue_minor = (await db.execute(revenue_stmt)).scalar() or 0

    return {
        "total_bookings": sum(counts_by_status.values()),
        "completed": counts_by_status.get(BookingStatus.COMPLETED, 0),
        "cancelled": sum(counts_by_status.get(s, 0) for s in DECLINED_BOOKING_STATUSES),
        "no_show": counts_by_status.get(BookingStatus.NO_SHOW, 0),
        "upcoming": sum(counts_by_status.get(s, 0) for s in UPCOMING_BOOKING_STATUSES),
        "unique_customers": len(customer_rows),
        "repeat_customers": sum(1 for _, count in customer_rows if count > 1),
        "revenue_minor": revenue_minor,
    }


async def get_revenue_timeseries_for_braider(
    db: AsyncSession,
    braider_id: uuid.UUID,
    *,
    date_from: date,
    date_to: date,
    interval: str,
) -> list[tuple[datetime, int, int]]:
    """One row per bucket: `date_trunc(interval, starts_at)`, summed
    braider-share revenue (minor units) from succeeded payments, and count
    of distinct bookings with at least one succeeded payment in that
    bucket - the raw material for the revenue line graph. `interval` must
    already be validated to 'day'/'week'/'month' by the caller, same
    caveat as `get_booking_timeseries_for_braider`."""
    bucket = func.date_trunc(interval, Booking.starts_at).label("bucket")
    stmt = (
        select(
            bucket,
            func.coalesce(func.sum(BookingPayment.braider_share_minor), 0),
            func.count(func.distinct(Booking.id)),
        )
        .join(BookingPayment, BookingPayment.booking_id == Booking.id)
        .where(Booking.braider_id == braider_id, BookingPayment.status == PaymentStatus.SUCCEEDED)
        .group_by(bucket)
        .order_by(bucket)
    )
    stmt = _apply_common_filters(stmt, status=None, date_from=date_from, date_to=date_to)
    result = await db.execute(stmt)
    return [(row[0], row[1], row[2]) for row in result.all()]


async def get_bookings_by_weekday_for_braider(
    db: AsyncSession,
    braider_id: uuid.UUID,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[tuple[int, int, Decimal]]:
    """Booking count and braider-share revenue grouped by ISO weekday of
    `starts_at` (1=Monday..7=Sunday) - the bar-chart view of which days a
    braider is busiest. Only counts bookings that actually occupied the
    calendar (`CALENDAR_BLOCKING_STATUSES`) - a cancelled/expired hold
    never really happened on that day."""
    weekday = func.extract("isodow", Booking.starts_at).label("weekday")
    stmt = (
        select(weekday, func.count(), func.coalesce(func.sum(Booking.braider_share_total), 0))
        .where(Booking.braider_id == braider_id, Booking.status.in_(CALENDAR_BLOCKING_STATUSES))
        .group_by(weekday)
        .order_by(weekday)
    )
    stmt = _apply_common_filters(stmt, status=None, date_from=date_from, date_to=date_to)
    result = await db.execute(stmt)
    return [(int(row[0]), row[1], row[2]) for row in result.all()]


async def get_style_breakdown_for_braider(
    db: AsyncSession,
    braider_id: uuid.UUID,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[tuple[uuid.UUID, Style, int, Decimal]]:
    """Booking count and braider-share revenue grouped by style - the
    pie-chart view of which styles earn the most. Ordered by revenue
    descending so the caller can take the top N and fold the rest into an
    'Other' slice. Same `CALENDAR_BLOCKING_STATUSES` convention as the
    weekday breakdown."""
    revenue = func.coalesce(func.sum(Booking.braider_share_total), 0)
    stmt = (
        select(Booking.style_id, Style, func.count(), revenue)
        .join(Style, Style.id == Booking.style_id)
        .where(Booking.braider_id == braider_id, Booking.status.in_(CALENDAR_BLOCKING_STATUSES))
        .group_by(Booking.style_id, Style.id)
        .order_by(revenue.desc())
    )
    stmt = _apply_common_filters(stmt, status=None, date_from=date_from, date_to=date_to)
    result = await db.execute(stmt)
    return [(row[0], row[1], row[2], row[3]) for row in result.all()]


async def list_booking_references(db: AsyncSession, booking_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not booking_ids:
        return {}
    result = await db.execute(select(Booking.id, Booking.reference).where(Booking.id.in_(booking_ids)))
    return {row.id: row.reference for row in result.all()}


async def list_blocked_ranges(
    db: AsyncSession, braider_id: uuid.UUID, *, range_start: datetime, range_end: datetime
) -> list[tuple[datetime, datetime]]:
    """Every (blocked_from, blocked_until) span that currently occupies this
    braider's calendar and overlaps [range_start, range_end) - used by
    `compute_available_slots` to subtract already-booked time. Mirrors the
    `ex_bookings_no_overlap` exclusion constraint's own status filter
    (CALENDAR_BLOCKING_STATUSES) so a slot this returns as "free" really is
    insertable."""
    result = await db.execute(
        select(Booking.blocked_from, Booking.blocked_until).where(
            Booking.braider_id == braider_id,
            Booking.status.in_(CALENDAR_BLOCKING_STATUSES),
            Booking.blocked_from < range_end,
            Booking.blocked_until > range_start,
        )
    )
    return [(row.blocked_from, row.blocked_until) for row in result.all()]


async def count_bookings_created_today(db: AsyncSession) -> int:
    """Every booking whose `created_at` falls within today's UTC calendar
    day, regardless of status - a simple "how many booking attempts came in
    today" count, not filtered to paid/confirmed ones."""
    today = datetime.now(UTC).date()
    start = datetime.combine(today, time.min, tzinfo=UTC)
    end = start + timedelta(days=1)
    return (
        await db.scalar(
            select(func.count()).select_from(Booking).where(
                Booking.created_at >= start, Booking.created_at < end
            )
        )
    ) or 0


async def expire_stale_holds(db: AsyncSession, *, limit: int) -> int:
    """Flips PENDING_PAYMENT bookings past their hold_expires_at to EXPIRED,
    in batches - EXPIRED isn't in CALENDAR_BLOCKING_STATUSES, so this is what
    actually releases the exclusion-constraint hold on the slot for an
    abandoned checkout (payment never completed)."""
    result = await db.execute(
        update(Booking)
        .where(
            Booking.id.in_(
                select(Booking.id)
                .where(
                    Booking.status == BookingStatus.PENDING_PAYMENT,
                    Booking.hold_expires_at.is_not(None),
                    Booking.hold_expires_at < datetime.now(UTC),
                )
                .limit(limit)
            )
        )
        .values(status=BookingStatus.EXPIRED)
    )
    return result.rowcount or 0


async def claim_due_balance_charges(db: AsyncSession, *, limit: int) -> list[uuid.UUID]:
    """Atomically flips SCHEDULED -> DUE for every CONFIRMED booking whose
    balance_charge_due_at has passed, in batches, and returns the claimed
    ids. The bulk UPDATE...WHERE id IN (SELECT ... LIMIT) is what makes the
    claim atomic across concurrent sweeper runs (same idiom as
    expire_stale_holds) - a second sweep tick can't re-claim a row this one
    already flipped to DUE, so exactly one charge_booking_balance_task gets
    enqueued per booking per due date."""
    result = await db.execute(
        update(Booking)
        .where(
            Booking.id.in_(
                select(Booking.id)
                .where(
                    Booking.status == BookingStatus.CONFIRMED,
                    Booking.balance_charge_state == BalanceChargeState.SCHEDULED,
                    Booking.balance_charge_due_at.is_not(None),
                    Booking.balance_charge_due_at <= datetime.now(UTC),
                )
                .limit(limit)
            )
        )
        .values(balance_charge_state=BalanceChargeState.DUE)
        .returning(Booking.id)
    )
    return list(result.scalars().all())


async def start_due_bookings(db: AsyncSession, *, limit: int) -> int:
    """CONFIRMED -> IN_PROGRESS once starts_at has passed - a plain bulk
    flip (same idiom as expire_stale_holds), since both statuses are
    already CALENDAR_BLOCKING_STATUSES so the exclusion constraint sees no
    change worth objecting to."""
    result = await db.execute(
        update(Booking)
        .where(
            Booking.id.in_(
                select(Booking.id)
                .where(Booking.status == BookingStatus.CONFIRMED, Booking.starts_at <= datetime.now(UTC))
                .limit(limit)
            )
        )
        .values(status=BookingStatus.IN_PROGRESS)
    )
    return result.rowcount or 0


async def complete_due_bookings(db: AsyncSession, *, limit: int) -> int:
    """IN_PROGRESS -> COMPLETED once ends_at has passed. Payout eligibility
    is computed straight off ends_at (see list_bookings_due_for_payout), not
    off when this cron happened to run, so a delayed tick here doesn't
    delay the payout clock."""
    result = await db.execute(
        update(Booking)
        .where(
            Booking.id.in_(
                select(Booking.id)
                .where(Booking.status == BookingStatus.IN_PROGRESS, Booking.ends_at <= datetime.now(UTC))
                .limit(limit)
            )
        )
        .values(status=BookingStatus.COMPLETED)
    )
    return result.rowcount or 0


async def list_bookings_due_for_payout(db: AsyncSession, *, limit: int) -> list[uuid.UUID]:
    """COMPLETED/NO_SHOW, not payouts_frozen (an open dispute), ends_at +
    booking_payout_release_delay_hours has passed, and at least one
    succeeded payment still has money to move (amount_transferred_minor ==
    0, braider_share_minor > 0). No claim/lock step here, unlike
    claim_due_balance_charges - the transfer's idempotency_key is
    deterministic per (booking, payment) rather than attempt-numbered, so
    Stripe itself de-duplicates a concurrent double-enqueue; the DB partial
    unique index (uq_booking_transfers_active_per_payment) is the second
    line of defense."""
    threshold = datetime.now(UTC) - timedelta(hours=get_settings().booking_payout_release_delay_hours)
    stmt = (
        select(Booking.id)
        .where(
            Booking.status.in_([BookingStatus.COMPLETED, BookingStatus.NO_SHOW]),
            Booking.payouts_frozen.is_(False),
            Booking.ends_at <= threshold,
            Booking.id.in_(
                select(BookingPayment.booking_id).where(
                    BookingPayment.status == PaymentStatus.SUCCEEDED,
                    BookingPayment.amount_transferred_minor == 0,
                    BookingPayment.braider_share_minor > 0,
                )
            ),
        )
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_payment_by_stripe_charge_id(db: AsyncSession, stripe_charge_id: str) -> BookingPayment | None:
    result = await db.execute(
        select(BookingPayment).where(BookingPayment.stripe_charge_id == stripe_charge_id)
    )
    return result.scalars().first()


async def create_transfer_reversal(
    db: AsyncSession,
    *,
    booking_id: uuid.UUID,
    booking_transfer_id: uuid.UUID,
    amount_minor: int,
    currency: Currency,
    idempotency_key: str,
) -> BookingTransferReversal:
    reversal = BookingTransferReversal(
        booking_id=booking_id,
        booking_transfer_id=booking_transfer_id,
        amount_minor=amount_minor,
        currency=currency,
        idempotency_key=idempotency_key,
    )
    db.add(reversal)
    await db.flush()
    return reversal


async def create_booking(
    db: AsyncSession,
    *,
    reference: str,
    customer_id: uuid.UUID,
    braider_id: uuid.UUID,
    booking_calculation_id: uuid.UUID,
    braider_style_id: uuid.UUID,
    style_id: uuid.UUID,
    style_variation_id: uuid.UUID | None,
    braider_style_variation_id: uuid.UUID | None,
    is_mobile: bool,
    country: str,
    client_address: str | None,
    client_latitude: Decimal | None,
    client_longitude: Decimal | None,
    currency: Currency,
    duration_minutes: int,
    starts_at: datetime,
    ends_at: datetime,
    braider_timezone: str,
    blocked_from: datetime,
    blocked_until: datetime,
    service_subtotal: Decimal,
    travel_fee: Decimal,
    subtotal: Decimal,
    platform_fee_type: SettingValueType,
    platform_fee_value: Decimal,
    platform_fee: Decimal,
    vat_service_type: SettingValueType,
    vat_service_value: Decimal,
    vat_on_service: Decimal,
    vat_platform_fee_type: SettingValueType,
    vat_platform_fee_value: Decimal,
    vat_on_platform_fee: Decimal,
    vat_total: Decimal,
    total: Decimal,
    deposit_type: SettingValueType,
    deposit_value: Decimal,
    deposit_amount: Decimal,
    balance_amount: Decimal,
    payment_schedule: PaymentSchedule,
    braider_share_total: Decimal,
    braider_share_deposit: Decimal,
    braider_share_balance: Decimal,
    hold_expires_at: datetime | None,
    cancellation_cutoff_at: datetime,
    balance_charge_due_at: datetime | None,
    balance_charge_state: BalanceChargeState,
    stripe_customer_id: str | None,
    locale: str,
    terms_version: str,
    terms_accepted_at: datetime,
) -> Booking:
    booking = Booking(
        reference=reference,
        customer_id=customer_id,
        braider_id=braider_id,
        booking_calculation_id=booking_calculation_id,
        braider_style_id=braider_style_id,
        style_id=style_id,
        style_variation_id=style_variation_id,
        braider_style_variation_id=braider_style_variation_id,
        is_mobile=is_mobile,
        country=country,
        client_address=client_address,
        client_latitude=client_latitude,
        client_longitude=client_longitude,
        currency=currency,
        duration_minutes=duration_minutes,
        starts_at=starts_at,
        ends_at=ends_at,
        braider_timezone=braider_timezone,
        blocked_from=blocked_from,
        blocked_until=blocked_until,
        service_subtotal=service_subtotal,
        travel_fee=travel_fee,
        subtotal=subtotal,
        platform_fee_type=platform_fee_type,
        platform_fee_value=platform_fee_value,
        platform_fee=platform_fee,
        vat_service_type=vat_service_type,
        vat_service_value=vat_service_value,
        vat_on_service=vat_on_service,
        vat_platform_fee_type=vat_platform_fee_type,
        vat_platform_fee_value=vat_platform_fee_value,
        vat_on_platform_fee=vat_on_platform_fee,
        vat_total=vat_total,
        total=total,
        deposit_type=deposit_type,
        deposit_value=deposit_value,
        deposit_amount=deposit_amount,
        balance_amount=balance_amount,
        payment_schedule=payment_schedule,
        braider_share_total=braider_share_total,
        braider_share_deposit=braider_share_deposit,
        braider_share_balance=braider_share_balance,
        status=BookingStatus.PENDING_PAYMENT,
        hold_expires_at=hold_expires_at,
        cancellation_cutoff_at=cancellation_cutoff_at,
        balance_charge_due_at=balance_charge_due_at,
        balance_charge_state=balance_charge_state,
        stripe_customer_id=stripe_customer_id,
        locale=locale,
        terms_version=terms_version,
        terms_accepted_at=terms_accepted_at,
    )
    db.add(booking)
    await db.flush()
    return booking


async def update_booking_schedule(
    db: AsyncSession,
    booking: Booking,
    *,
    starts_at: datetime,
    ends_at: datetime,
    braider_timezone: str,
    blocked_from: datetime,
    blocked_until: datetime,
    cancellation_cutoff_at: datetime,
    balance_charge_due_at: datetime | None,
) -> Booking:
    booking.starts_at = starts_at
    booking.ends_at = ends_at
    booking.braider_timezone = braider_timezone
    booking.blocked_from = blocked_from
    booking.blocked_until = blocked_until
    booking.cancellation_cutoff_at = cancellation_cutoff_at
    booking.balance_charge_due_at = balance_charge_due_at
    await db.flush()
    return booking


async def add_item(
    db: AsyncSession,
    *,
    booking_id: uuid.UUID,
    item_type: BookingItemType,
    name_en: str | None,
    name_de: str | None,
    name_fr: str | None,
    quantity: int = 1,
    unit_amount: Decimal,
    line_amount: Decimal,
    is_required: bool = False,
    vat_rate: Decimal | None = None,
    source_style_id: uuid.UUID | None = None,
    source_style_variation_id: uuid.UUID | None = None,
    source_addon_id: uuid.UUID | None = None,
    source_braider_style_addon_id: uuid.UUID | None = None,
) -> BookingItem:
    item = BookingItem(
        booking_id=booking_id,
        item_type=item_type,
        name_en=name_en,
        name_de=name_de,
        name_fr=name_fr,
        quantity=quantity,
        unit_amount=unit_amount,
        line_amount=line_amount,
        is_required=is_required,
        vat_rate=vat_rate,
        source_style_id=source_style_id,
        source_style_variation_id=source_style_variation_id,
        source_addon_id=source_addon_id,
        source_braider_style_addon_id=source_braider_style_addon_id,
    )
    db.add(item)
    await db.flush()
    return item


async def create_payment(
    db: AsyncSession,
    *,
    booking_id: uuid.UUID,
    purpose: PaymentPurpose,
    amount_minor: int,
    currency: Currency,
    braider_share_minor: int,
    stripe_payment_intent_id: str | None,
    idempotency_key: str,
    is_off_session: bool,
    transfer_group: str,
    attempt_number: int = 1,
) -> BookingPayment:
    payment = BookingPayment(
        booking_id=booking_id,
        purpose=purpose,
        status=PaymentStatus.PENDING,
        amount_minor=amount_minor,
        currency=currency,
        braider_share_minor=braider_share_minor,
        stripe_payment_intent_id=stripe_payment_intent_id,
        idempotency_key=idempotency_key,
        is_off_session=is_off_session,
        transfer_group=transfer_group,
        attempt_number=attempt_number,
    )
    db.add(payment)
    await db.flush()
    return payment


async def get_dac7_aggregates(db: AsyncSession, *, period_start: datetime, period_end: datetime):
    """Per (braider, country, currency) totals for DAC7/PStTG reporting -
    "relevant activities" (bookings) that actually delivered, bounded by
    `ends_at` (when the service was rendered, not when it was booked or
    paid). COMPLETED and NO_SHOW both count - a no-show still earned the
    braider their share. Grouped by country/currency too since a braider
    can operate in more than one (the plan's per-booking country/currency
    snapshot already supports this)."""
    stmt = (
        select(
            Booking.braider_id,
            Booking.country,
            Booking.currency,
            func.count(Booking.id).label("booking_count"),
            func.sum(Booking.braider_share_total).label("gross_consideration"),
            func.sum(Booking.platform_fee).label("platform_fees_withheld"),
        )
        .where(
            Booking.status.in_([BookingStatus.COMPLETED, BookingStatus.NO_SHOW]),
            Booking.ends_at >= period_start,
            Booking.ends_at < period_end,
        )
        .group_by(Booking.braider_id, Booking.country, Booking.currency)
    )
    result = await db.execute(stmt)
    return result.all()
