import logging
import random
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from arq import ArqRedis
from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.currency import Currency, currency_for_country
from app.core.exceptions import (
    BookingCalculationAlreadyUsedError,
    BookingCalculationExpiredError,
    BookingCalculationNotFoundError,
    BookingCancellationWindowClosedError,
    BookingNoShowNotAllowedError,
    BookingNotCancellableError,
    BookingNotFoundError,
    BookingNotReschedulableError,
    BookingPaymentNotResumableError,
    BookingPriceDriftError,
    BookingRescheduleWindowClosedError,
    BookingSlotUnavailableError,
    BookingStartsInPastError,
    BraiderNotFoundError,
    BraiderNotPayableError,
    InvalidBookingDateRangeError,
    SelfBookingNotAllowedError,
    UserNotFoundError,
)
from app.core.i18n import localize_field
from app.core.i18n import t as translate
from app.core.money import from_minor_units, to_minor_units
from app.core.pagination import PaginationMeta, PaginationParams
from app.modules.bookings import payouts
from app.modules.bookings import repository as bookings_repo
from app.modules.bookings.calculations import repository as calculations_repo
from app.modules.bookings.calculations import service as calculations_service
from app.modules.bookings.calculations.models import BookingCalculationStatus
from app.modules.bookings.calculations.schemas import BookingCalculationInput
from app.modules.bookings.enums import (
    BalanceChargeState,
    BookingItemType,
    BookingStatus,
    PaymentPurpose,
    PaymentSchedule,
    RefundStatus,
)
from app.modules.bookings.models import Booking, BookingItem, BookingPayment, CancelledBy
from app.modules.bookings.payments import client as payments_client
from app.modules.bookings.pricing import PricedLine
from app.modules.bookings.receipts import repository as receipts_repo
from app.modules.bookings.receipts import service as receipts_service
from app.modules.bookings.schemas import (
    AdminBarChartPoint,
    AdminBarChartResponse,
    AdminBookingPaymentResponse,
    AdminBookingResponse,
    AdminBookingStatsActorResponse,
    AdminBookingStatsResponse,
    AdminBookingSummaryResponse,
    AdminPlatformFinancialsResponse,
    AdminRevenueChartPoint,
    AdminRevenueChartResponse,
    AdminStyleChartSlice,
    AdminStylePieChartResponse,
    BookingCreateRequest,
    BookingItemResponse,
    BookingPaymentResponse,
    BookingRescheduleRequest,
    BookingResponse,
    BookingSummaryResponse,
    PaginatedAdminBookingsResponse,
    PaginatedBookingsResponse,
    ReceiptSummaryResponse,
)
from app.modules.bookings.tasks import (
    TASK_SEND_BOOKING_CANCELLED_BY_BRAIDER_EMAIL,
    TASK_SEND_BOOKING_CANCELLED_BY_BRAIDER_NOTIFICATION,
    TASK_SEND_BOOKING_CANCELLED_BY_CUSTOMER_EMAIL,
    TASK_SEND_BOOKING_CANCELLED_BY_CUSTOMER_NOTIFICATION,
    TASK_SEND_BOOKING_RESCHEDULED_EMAIL,
    TASK_SEND_BOOKING_RESCHEDULED_NOTIFICATION,
)
from app.modules.braiders import repository as braiders_repo
from app.modules.braiders.availability import repository as availability_repo
from app.modules.braiders.payment_setup import repository as payment_setup_repo
from app.modules.styles import repository as styles_repo
from app.modules.styles.models import AddOn, Style, StyleVariation
from app.modules.users import repository as users_repo
from app.modules.users.models import User, UserType

settings = get_settings()
logger = logging.getLogger("app.modules.bookings")

_REFERENCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I
_DEFAULT_CHART_WINDOW_DAYS = 90
_DEFAULT_TOP_STYLES_LIMIT = 8
_ZERO = Decimal("0.00")
_HUNDRED = Decimal("100")


def _generate_reference() -> str:
    return "AB-" + "".join(random.choices(_REFERENCE_ALPHABET, k=6))


def _label_triple(key: str) -> tuple[str, str, str]:
    return translate(key, "en"), translate(key, "de"), translate(key, "fr")


def _line_name_triple(
    line: PricedLine,
    *,
    style: Style,
    variation: StyleVariation | None,
    addon_catalog: dict[uuid.UUID, AddOn],
) -> tuple[str | None, str | None, str | None]:
    """All three locales at once (unlike the calculator's single-locale
    `_line_name`) - a booking's items are a permanent receipt snapshot, not
    a live per-request display, so every supported language is captured up
    front rather than only whichever locale happened to make the request."""
    if line.item_type == BookingItemType.SERVICE:
        return style.name_en, style.name_de, style.name_fr
    if line.item_type == BookingItemType.VARIATION and variation is not None:
        return variation.name_en, variation.name_de, variation.name_fr
    if line.item_type == BookingItemType.ADDON and line.source_addon_id is not None:
        addon = addon_catalog.get(line.source_addon_id)
        return (addon.name_en, addon.name_de, addon.name_fr) if addon else (None, None, None)
    if line.item_type == BookingItemType.TRAVEL:
        return _label_triple("booking_calculation.travel_fee_label")
    if line.item_type == BookingItemType.PLATFORM_FEE:
        return _label_triple("booking_calculation.platform_fee_label")
    if line.item_type == BookingItemType.VAT_SERVICE:
        return _label_triple("booking_calculation.vat_service_label")
    if line.item_type == BookingItemType.VAT_PLATFORM_FEE:
        return _label_triple("booking_calculation.vat_platform_fee_label")
    return None, None, None


async def create_booking(
    db: AsyncSession,
    redis: Redis,
    queue: ArqRedis,
    *,
    user: User,
    data: BookingCreateRequest,
    locale: str,
) -> BookingResponse:
    calculation = await calculations_repo.get_calculation_by_id(db, data.booking_calculation_id)
    if calculation is None:
        raise BookingCalculationNotFoundError()
    if calculation.status != BookingCalculationStatus.DRAFT:
        raise BookingCalculationAlreadyUsedError()
    if calculation.expires_at <= datetime.now(UTC):
        raise BookingCalculationExpiredError()

    now = datetime.now(UTC)
    if data.starts_at <= now:
        raise BookingStartsInPastError()

    addons = await calculations_repo.list_addons(db, calculation.id)
    calc_input = BookingCalculationInput(
        braider_id=calculation.braider_id,
        style_id=calculation.style_id,
        braider_style_variation_id=calculation.braider_style_variation_id,
        braider_style_addon_ids=[a.braider_style_addon_id for a in addons],
        is_mobile=calculation.is_mobile,
        client_address=calculation.client_address,
        client_latitude=calculation.client_latitude,
        client_longitude=calculation.client_longitude,
    )
    # Re-runs every check the calculator itself performs (visibility, active
    # style, mobile radius, country resolution) against *current* state - a
    # calculation is only ever an input echo, never authorization on its own.
    resolved = await calculations_service.resolve_calculation_input(db, calc_input)

    if resolved.braider_user_id == user.id:
        raise SelfBookingNotAllowedError()

    connect_account = await payment_setup_repo.get_by_braider_id(db, resolved.braider_id)
    if connect_account is None or not (
        connect_account.charges_enabled and connect_account.payouts_enabled
    ):
        raise BraiderNotPayableError()

    result = await calculations_service.price_resolved_input(
        resolved, db, redis, starts_at=data.starts_at
    )

    # Price drift guard: the catalog or effective settings moved between the
    # calculation and now. Never silently charge a different total than what
    # the customer saw and confirmed - they must fetch a fresh quote.
    if to_minor_units(result.total) != to_minor_units(calculation.total):
        raise BookingPriceDriftError()

    availability_settings = await availability_repo.get_settings_by_braider_id(db, resolved.braider_id)
    buffer_minutes = availability_settings.buffer_minutes if availability_settings else 0
    braider_timezone = availability_settings.timezone if availability_settings else "UTC"

    duration = timedelta(minutes=resolved.braider_style.duration_minutes)
    ends_at = data.starts_at + duration
    blocked_from = data.starts_at
    blocked_until = ends_at + timedelta(minutes=buffer_minutes)

    currency = currency_for_country(resolved.country)
    cancellation_cutoff_at = data.starts_at - timedelta(hours=settings.booking_cancellation_cutoff_hours)

    balance_charge_due_at: datetime | None = None
    balance_charge_state = BalanceChargeState.NOT_APPLICABLE
    if result.payment_schedule == PaymentSchedule.DEPOSIT_THEN_BALANCE:
        balance_charge_due_at = cancellation_cutoff_at + timedelta(
            minutes=settings.booking_balance_charge_grace_minutes
        )
        balance_charge_state = BalanceChargeState.SCHEDULED

    stripe_customer_id = user.stripe_customer_id
    if stripe_customer_id is None:
        stripe_customer_id = await payments_client.create_customer(
            email=user.email, name=f"{user.first_name} {user.last_name or ''}".strip()
        )
        user.stripe_customer_id = stripe_customer_id
        await db.flush()

    reference = _generate_reference()

    try:
        booking = await bookings_repo.create_booking(
            db,
            reference=reference,
            customer_id=user.id,
            braider_id=resolved.braider_id,
            booking_calculation_id=calculation.id,
            braider_style_id=resolved.braider_style.id,
            style_id=resolved.style.id,
            style_variation_id=resolved.variation.id if resolved.variation else None,
            braider_style_variation_id=resolved.braider_style_variation_id,
            is_mobile=resolved.is_mobile,
            country=resolved.country,
            client_address=resolved.client_address,
            client_latitude=resolved.client_latitude,
            client_longitude=resolved.client_longitude,
            currency=currency,
            duration_minutes=resolved.braider_style.duration_minutes,
            starts_at=data.starts_at,
            ends_at=ends_at,
            braider_timezone=braider_timezone,
            blocked_from=blocked_from,
            blocked_until=blocked_until,
            service_subtotal=result.service_subtotal,
            travel_fee=result.travel_fee,
            subtotal=result.subtotal,
            platform_fee_type=result.platform_fee_type,
            platform_fee_value=result.platform_fee_value,
            platform_fee=result.platform_fee,
            vat_service_type=result.vat_service_type,
            vat_service_value=result.vat_service_value,
            vat_on_service=result.vat_on_service,
            vat_platform_fee_type=result.vat_platform_fee_type,
            vat_platform_fee_value=result.vat_platform_fee_value,
            vat_on_platform_fee=result.vat_on_platform_fee,
            vat_total=result.vat_total,
            total=result.total,
            deposit_type=result.deposit_type,
            deposit_value=result.deposit_value,
            deposit_amount=result.deposit_amount,
            balance_amount=result.balance_amount,
            payment_schedule=result.payment_schedule,
            braider_share_total=result.braider_share_total,
            braider_share_deposit=result.braider_share_deposit,
            braider_share_balance=result.braider_share_balance,
            hold_expires_at=now + timedelta(minutes=settings.booking_hold_minutes),
            cancellation_cutoff_at=cancellation_cutoff_at,
            balance_charge_due_at=balance_charge_due_at,
            balance_charge_state=balance_charge_state,
            stripe_customer_id=stripe_customer_id,
            locale=locale,
            terms_version=settings.booking_terms_version,
            terms_accepted_at=now,
        )
    except IntegrityError as exc:
        await db.rollback()
        if getattr(getattr(exc, "orig", None), "sqlstate", None) == "23P01":
            raise BookingSlotUnavailableError() from exc
        raise

    addon_catalog = {
        addon_id: addon for _, addon_id, addon, _, _ in resolved.addon_entries if addon is not None
    }
    for line in result.lines:
        name_en, name_de, name_fr = _line_name_triple(
            line, style=resolved.style, variation=resolved.variation, addon_catalog=addon_catalog
        )
        await bookings_repo.add_item(
            db,
            booking_id=booking.id,
            item_type=line.item_type,
            name_en=name_en,
            name_de=name_de,
            name_fr=name_fr,
            unit_amount=line.amount,
            line_amount=line.amount,
            is_required=line.is_required,
            vat_rate=line.vat_rate,
            source_style_id=line.source_style_id,
            source_style_variation_id=line.source_style_variation_id,
            source_addon_id=line.source_addon_id,
            source_braider_style_addon_id=line.source_braider_style_addon_id,
        )

    purpose = (
        PaymentPurpose.FULL
        if result.payment_schedule == PaymentSchedule.FULL_UPFRONT
        else PaymentPurpose.DEPOSIT
    )
    amount = result.total if purpose == PaymentPurpose.FULL else result.deposit_amount
    braider_share = (
        result.braider_share_total if purpose == PaymentPurpose.FULL else result.braider_share_deposit
    )
    transfer_group = f"booking_{booking.id}"

    intent = await payments_client.create_payment_intent(
        amount_minor=to_minor_units(amount),
        currency=currency.value,
        customer_id=stripe_customer_id,
        metadata={"booking_id": str(booking.id), "purpose": purpose.value},
        off_session_setup=(result.payment_schedule == PaymentSchedule.DEPOSIT_THEN_BALANCE),
    )

    payment = await bookings_repo.create_payment(
        db,
        booking_id=booking.id,
        purpose=purpose,
        amount_minor=to_minor_units(amount),
        currency=currency,
        braider_share_minor=to_minor_units(braider_share),
        stripe_payment_intent_id=intent.id,
        idempotency_key=f"booking:{booking.id}:{purpose.value}:1",
        is_off_session=(result.payment_schedule == PaymentSchedule.DEPOSIT_THEN_BALANCE),
        transfer_group=transfer_group,
    )

    calculation.status = BookingCalculationStatus.CONSUMED
    calculation.consumed_by_booking_id = booking.id
    await db.flush()

    await db.commit()

    return await _to_response(db, booking, locale=locale, client_secret=intent.client_secret, payment=payment)


async def _to_response(
    db: AsyncSession,
    booking: Booking,
    *,
    locale: str,
    client_secret: str | None = None,
    payment: BookingPayment | None = None,
) -> BookingResponse:
    items = await bookings_repo.list_items(db, booking.id)
    payments = await bookings_repo.list_payments(db, booking.id) if payment is None else [payment]
    style = await styles_repo.get_style_by_id(db, booking.style_id)
    style_name = (localize_field(style, "name", locale) or style.name_en) if style else ""
    braider_names = await braiders_repo.list_display_names(db, [booking.braider_id])
    customer_names = await users_repo.list_full_names(db, [booking.customer_id])

    return BookingResponse(
        id=booking.id,
        reference=booking.reference,
        status=booking.status,
        braider_id=booking.braider_id,
        braider_name=braider_names.get(booking.braider_id, ""),
        customer_name=customer_names.get(booking.customer_id, ""),
        style_id=booking.style_id,
        style_name=style_name,
        duration_minutes=booking.duration_minutes,
        is_mobile=booking.is_mobile,
        client_address=booking.client_address,
        client_latitude=booking.client_latitude,
        client_longitude=booking.client_longitude,
        country=booking.country,
        currency=booking.currency,
        starts_at=booking.starts_at,
        ends_at=booking.ends_at,
        items=[_item_to_response(item, locale) for item in items],
        service_subtotal=booking.service_subtotal,
        travel_fee=booking.travel_fee,
        subtotal=booking.subtotal,
        platform_fee=booking.platform_fee,
        vat_on_service=booking.vat_on_service,
        vat_on_platform_fee=booking.vat_on_platform_fee,
        vat_total=booking.vat_total,
        total=booking.total,
        deposit_amount=booking.deposit_amount,
        balance_amount=booking.balance_amount,
        payment_schedule=booking.payment_schedule,
        cancellation_cutoff_at=booking.cancellation_cutoff_at,
        cancelled_at=booking.cancelled_at,
        cancelled_by=booking.cancelled_by,
        cancellation_reason=booking.cancellation_reason,
        payments=[
            BookingPaymentResponse(
                purpose=p.purpose,
                status=p.status,
                amount=from_minor_units(p.amount_minor),
                currency=p.currency,
                client_secret=client_secret if payment is not None and p.id == payment.id else None,
            )
            for p in payments
        ],
        created_at=booking.created_at,
    )


def _item_to_response(item: BookingItem, locale: str) -> BookingItemResponse:
    name = localize_field(item, "name", locale) or item.name_en
    return BookingItemResponse(
        item_type=item.item_type,
        name=name,
        quantity=item.quantity,
        unit_amount=item.unit_amount,
        line_amount=item.line_amount,
        is_required=item.is_required,
    )


async def count_bookings_today(db: AsyncSession) -> int:
    return await bookings_repo.count_bookings_created_today(db)


async def get_booking(db: AsyncSession, booking_id: uuid.UUID, *, user: User, locale: str) -> BookingResponse:
    booking = await bookings_repo.get_booking_by_id(db, booking_id)
    if booking is None or booking.customer_id != user.id:
        raise BookingNotFoundError()
    return await _to_response(db, booking, locale=locale)


def _receipt_url(public_token: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/api/v1/receipts/{public_token}"


async def list_booking_receipts(
    db: AsyncSession, booking_id: uuid.UUID, *, user: User
) -> list[ReceiptSummaryResponse]:
    booking = await bookings_repo.get_booking_by_id(db, booking_id)
    if booking is None or booking.customer_id != user.id:
        raise BookingNotFoundError()
    receipts = await receipts_repo.list_receipts_for_booking(db, booking.id)
    return [
        ReceiptSummaryResponse(
            id=r.id,
            type=r.type,
            receipt_number=r.receipt_number,
            issued_at=r.issued_at,
            amount_total=r.amount_total,
            currency=r.currency,
            url=_receipt_url(r.public_token),
        )
        for r in receipts
    ]


async def reschedule_booking(
    db: AsyncSession,
    queue: ArqRedis,
    booking_id: uuid.UUID,
    *,
    user: User,
    data: BookingRescheduleRequest,
    locale: str,
) -> BookingResponse:
    booking = await bookings_repo.get_booking_by_id_for_update(db, booking_id)
    if booking is None or booking.customer_id != user.id:
        raise BookingNotFoundError()

    if booking.status != BookingStatus.CONFIRMED:
        raise BookingNotReschedulableError()

    now = datetime.now(UTC)
    if now >= booking.cancellation_cutoff_at:
        raise BookingRescheduleWindowClosedError()
    if data.starts_at <= now:
        raise BookingStartsInPastError()

    old_starts_at = booking.starts_at

    availability_settings = await availability_repo.get_settings_by_braider_id(db, booking.braider_id)
    buffer_minutes = availability_settings.buffer_minutes if availability_settings else 0
    braider_timezone = availability_settings.timezone if availability_settings else booking.braider_timezone

    duration = timedelta(minutes=booking.duration_minutes)
    ends_at = data.starts_at + duration
    blocked_from = data.starts_at
    blocked_until = ends_at + timedelta(minutes=buffer_minutes)

    cancellation_cutoff_at = data.starts_at - timedelta(hours=settings.booking_cancellation_cutoff_hours)
    balance_charge_due_at: datetime | None = None
    if booking.payment_schedule == PaymentSchedule.DEPOSIT_THEN_BALANCE:
        balance_charge_due_at = cancellation_cutoff_at + timedelta(
            minutes=settings.booking_balance_charge_grace_minutes
        )

    try:
        await bookings_repo.update_booking_schedule(
            db,
            booking,
            starts_at=data.starts_at,
            ends_at=ends_at,
            braider_timezone=braider_timezone,
            blocked_from=blocked_from,
            blocked_until=blocked_until,
            cancellation_cutoff_at=cancellation_cutoff_at,
            balance_charge_due_at=balance_charge_due_at,
        )
    except IntegrityError as exc:
        await db.rollback()
        if getattr(getattr(exc, "orig", None), "sqlstate", None) == "23P01":
            raise BookingSlotUnavailableError() from exc
        raise

    await db.commit()

    await queue.enqueue_job(
        TASK_SEND_BOOKING_RESCHEDULED_EMAIL,
        booking_id=str(booking.id),
        old_starts_at=old_starts_at.isoformat(),
    )
    await queue.enqueue_job(
        TASK_SEND_BOOKING_RESCHEDULED_NOTIFICATION,
        booking_id=str(booking.id),
    )

    return await _to_response(db, booking, locale=locale)


async def resume_booking_payment(
    db: AsyncSession, booking_id: uuid.UUID, *, user: User, locale: str
) -> BookingResponse:
    """`POST /{id}/pay` - lets the customer complete an outstanding balance
    on-session, either because the off-session ladder hit
    `authentication_required` (needs a customer-present 3DS challenge the
    backend cannot do for them) or simply because they don't want to wait
    for the next scheduled attempt. Deliberately scoped to BALANCE only:
    FULL/DEPOSIT failures at booking time never persisted a booking at all
    (create_booking's PaymentIntent creation isn't wrapped in a DB
    transaction that survives it), so there is nothing to resume there.

    Marks the charge IN_PROGRESS so the sweeper cron won't also fire an
    off-session attempt concurrently. If the customer abandons this flow
    without confirming, the booking stays IN_PROGRESS until a reconciliation
    sweep (Phase 7, not yet built) reclaims it - a known, accepted gap for
    now, no worse than an abandoned checkout hold before it.
    """
    booking = await bookings_repo.get_booking_by_id_for_update(db, booking_id)
    if booking is None or booking.customer_id != user.id:
        raise BookingNotFoundError()

    if (
        booking.payment_schedule != PaymentSchedule.DEPOSIT_THEN_BALANCE
        or booking.status != BookingStatus.CONFIRMED
        or booking.balance_charge_state not in (BalanceChargeState.SCHEDULED, BalanceChargeState.DUE)
    ):
        raise BookingPaymentNotResumableError()

    attempt_number = booking.balance_charge_attempts + 1
    idempotency_key = f"booking:{booking.id}:balance:{attempt_number}"
    transfer_group = f"booking_{booking.id}"

    intent = await payments_client.create_payment_intent(
        amount_minor=to_minor_units(booking.balance_amount),
        currency=booking.currency.value,
        customer_id=booking.stripe_customer_id,
        metadata={"booking_id": str(booking.id), "purpose": PaymentPurpose.BALANCE.value},
        off_session_setup=False,
    )

    payment = await bookings_repo.create_payment(
        db,
        booking_id=booking.id,
        purpose=PaymentPurpose.BALANCE,
        amount_minor=to_minor_units(booking.balance_amount),
        currency=booking.currency,
        braider_share_minor=to_minor_units(booking.braider_share_balance),
        stripe_payment_intent_id=intent.id,
        idempotency_key=idempotency_key,
        is_off_session=False,
        transfer_group=transfer_group,
        attempt_number=attempt_number,
    )

    booking.balance_charge_attempts = attempt_number
    booking.balance_charge_state = BalanceChargeState.IN_PROGRESS
    await db.flush()
    await db.commit()

    return await _to_response(db, booking, locale=locale, client_secret=intent.client_secret, payment=payment)


async def cancel_booking_by_customer(
    db: AsyncSession, queue: ArqRedis, booking_id: uuid.UUID, *, user: User, locale: str
) -> BookingResponse:
    """Confirmed product decision: the deposit (or, for a FULL_UPFRONT
    booking cancelled right at the cutoff edge, the whole captured amount)
    is non-refundable - but the braider's proportional share of whatever
    succeeded is still theirs, so it's released to them immediately rather
    than waiting for the normal completion payout (which will never fire,
    since this booking never reaches COMPLETED). Zero Stripe calls on the
    cancellation itself (design correction: the slot falls out of the
    exclusion predicate the moment status leaves CALENDAR_BLOCKING_STATUSES)
    - only the braider-share transfer touches Stripe."""
    booking = await bookings_repo.get_booking_by_id_for_update(db, booking_id)
    if booking is None or booking.customer_id != user.id:
        raise BookingNotFoundError()
    if booking.status != BookingStatus.CONFIRMED:
        raise BookingNotCancellableError()

    now = datetime.now(UTC)
    if now >= booking.cancellation_cutoff_at:
        raise BookingCancellationWindowClosedError()

    booking.status = BookingStatus.CANCELLED_BY_CUSTOMER
    booking.cancelled_at = now
    booking.cancelled_by = CancelledBy.CUSTOMER
    if booking.balance_charge_state in (BalanceChargeState.SCHEDULED, BalanceChargeState.DUE):
        booking.balance_charge_state = BalanceChargeState.ABANDONED
    await db.flush()

    connect_account = await payment_setup_repo.get_by_braider_id(db, booking.braider_id)
    await payouts.release_braider_shares(db, booking, connect_account=connect_account)

    await db.commit()

    await queue.enqueue_job(TASK_SEND_BOOKING_CANCELLED_BY_CUSTOMER_EMAIL, booking_id=str(booking.id))
    await queue.enqueue_job(TASK_SEND_BOOKING_CANCELLED_BY_CUSTOMER_NOTIFICATION, booking_id=str(booking.id))

    return await _to_response(db, booking, locale=locale)


async def cancel_booking_by_braider(
    db: AsyncSession, queue: ArqRedis, booking_id: uuid.UUID, *, user_id: uuid.UUID, reason: str, locale: str
) -> BookingResponse:
    """No cutoff - a braider can cancel at any time up to COMPLETED, unlike
    the customer's 24h window (confirmed product decision). Full refund of
    every succeeded payment, deposit included; reverses any transfer
    already made for this booking first (defensive - in the current
    codebase state that list is always empty, since the only thing that
    creates a transfer before Phase 5's completion payout is a customer
    cancellation, and the two are mutually exclusive terminal states).
    Platform absorbs Stripe's non-returned processing fee - an accounting
    fact, not something this code enforces."""
    profile = await braiders_repo.get_profile_by_user_id(db, user_id)
    booking = await bookings_repo.get_booking_by_id_for_update(db, booking_id)
    if booking is None or profile is None or booking.braider_id != profile.id:
        raise BookingNotFoundError()
    if booking.status != BookingStatus.CONFIRMED:
        raise BookingNotCancellableError()

    now = datetime.now(UTC)
    booking.status = BookingStatus.CANCELLED_BY_BRAIDER
    booking.cancelled_at = now
    booking.cancelled_by = CancelledBy.BRAIDER
    booking.cancellation_reason = reason
    if booking.balance_charge_state in (BalanceChargeState.SCHEDULED, BalanceChargeState.DUE):
        booking.balance_charge_state = BalanceChargeState.ABANDONED
    await db.flush()

    await payouts.reverse_transfers_for_booking(db, booking)

    succeeded_payments = await bookings_repo.list_succeeded_payments(db, booking.id)
    for payment in succeeded_payments:
        refundable_minor = payment.amount_minor - payment.amount_refunded_minor
        if refundable_minor <= 0 or payment.stripe_payment_intent_id is None:
            continue
        idempotency_key = f"booking:{booking.id}:refund:{payment.id}"
        try:
            result = await payments_client.create_refund(
                payment_intent_id=payment.stripe_payment_intent_id,
                amount_minor=refundable_minor,
                metadata={"booking_id": str(booking.id), "booking_payment_id": str(payment.id)},
                idempotency_key=idempotency_key,
            )
        except payments_client.StripeApiError as exc:
            refund = await bookings_repo.create_refund(
                db,
                booking_id=booking.id,
                booking_payment_id=payment.id,
                amount_minor=refundable_minor,
                currency=payment.currency,
                idempotency_key=idempotency_key,
            )
            refund.status = RefundStatus.FAILED
            refund.failure_message = str(exc)[:500]
            logger.warning(
                "Refund failed for booking %s payment %s: %s", booking.reference, payment.id, exc
            )
            continue
        refund = await bookings_repo.create_refund(
            db,
            booking_id=booking.id,
            booking_payment_id=payment.id,
            amount_minor=refundable_minor,
            currency=payment.currency,
            idempotency_key=idempotency_key,
        )
        refund.status = RefundStatus.SUCCEEDED
        refund.stripe_refund_id = result.id
        payment.amount_refunded_minor += refundable_minor
        await db.flush()
        # A refund without a corresponding credit note leaves the original
        # invoice's VAT owed under §14c UStG even though the money went
        # back - same transaction as the refund itself.
        await receipts_service.issue_credit_note(db, booking=booking, payment=payment, refund=refund)

    await db.commit()

    await queue.enqueue_job(TASK_SEND_BOOKING_CANCELLED_BY_BRAIDER_EMAIL, booking_id=str(booking.id))
    await queue.enqueue_job(TASK_SEND_BOOKING_CANCELLED_BY_BRAIDER_NOTIFICATION, booking_id=str(booking.id))

    return await _to_response(db, booking, locale=locale)


async def mark_booking_no_show(
    db: AsyncSession, booking_id: uuid.UUID, *, user_id: uuid.UUID, locale: str
) -> BookingResponse:
    """Braider-only - only they're in a position to know whether the
    customer actually showed up. No refund, no payout change: NO_SHOW
    joins COMPLETED as an equally-payable terminal state (already true of
    release_due_payouts_cron's eligibility query since Phase 5), so the
    braider is still paid in full - the deposit/balance already collected
    isn't customer-fault-forgiven the way a customer cancellation is."""
    profile = await braiders_repo.get_profile_by_user_id(db, user_id)
    booking = await bookings_repo.get_booking_by_id_for_update(db, booking_id)
    if booking is None or profile is None or booking.braider_id != profile.id:
        raise BookingNotFoundError()

    if booking.status not in (BookingStatus.CONFIRMED, BookingStatus.IN_PROGRESS):
        raise BookingNoShowNotAllowedError()
    if datetime.now(UTC) < booking.starts_at:
        raise BookingNoShowNotAllowedError()

    booking.status = BookingStatus.NO_SHOW
    await db.commit()

    return await _to_response(db, booking, locale=locale)


async def get_braider_booking(
    db: AsyncSession, booking_id: uuid.UUID, *, user_id: uuid.UUID, locale: str
) -> BookingResponse:
    profile = await braiders_repo.get_profile_by_user_id(db, user_id)
    booking = await bookings_repo.get_booking_by_id(db, booking_id)
    if booking is None or profile is None or booking.braider_id != profile.id:
        raise BookingNotFoundError()
    return await _to_response(db, booking, locale=locale)


def _to_summary(
    booking: Booking,
    *,
    style_names: dict[uuid.UUID, str],
    braider_names: dict[uuid.UUID, str],
    customer_names: dict[uuid.UUID, str],
) -> BookingSummaryResponse:
    return BookingSummaryResponse(
        id=booking.id,
        reference=booking.reference,
        status=booking.status,
        braider_id=booking.braider_id,
        braider_name=braider_names.get(booking.braider_id, ""),
        customer_name=customer_names.get(booking.customer_id, ""),
        style_name=style_names.get(booking.style_id, ""),
        starts_at=booking.starts_at,
        ends_at=booking.ends_at,
        total=booking.total,
        currency=booking.currency,
    )


async def _style_names_for(db: AsyncSession, bookings: list[Booking], *, locale: str) -> dict[uuid.UUID, str]:
    styles = await styles_repo.list_styles_by_ids(db, list({b.style_id for b in bookings}))
    return {
        style_id: localize_field(style, "name", locale) or style.name_en
        for style_id, style in styles.items()
    }


def _validate_date_range(date_from: date | None, date_to: date | None) -> None:
    if date_from is not None and date_to is not None and date_to < date_from:
        raise InvalidBookingDateRangeError()


async def list_bookings(
    db: AsyncSession,
    *,
    user: User,
    params: PaginationParams,
    locale: str,
    status: BookingStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    search: str | None = None,
) -> PaginatedBookingsResponse:
    _validate_date_range(date_from, date_to)
    items, meta = await bookings_repo.list_bookings_for_customer(
        db, user.id, params=params, status=status, date_from=date_from, date_to=date_to, search=search
    )
    style_names = await _style_names_for(db, items, locale=locale)
    braider_names = await braiders_repo.list_display_names(db, [b.braider_id for b in items])
    customer_names = await users_repo.list_full_names(db, [b.customer_id for b in items])
    return _paginated_response(items, meta, style_names, braider_names, customer_names)


async def list_braider_bookings(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    params: PaginationParams,
    locale: str,
    status: BookingStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    search: str | None = None,
) -> PaginatedBookingsResponse:
    _validate_date_range(date_from, date_to)
    profile = await braiders_repo.get_profile_by_user_id(db, user_id)
    if profile is None:
        return PaginatedBookingsResponse(
            items=[],
            page=params.page,
            page_size=params.page_size,
            total_items=0,
            total_pages=0,
            has_next=False,
            has_previous=False,
        )
    items, meta = await bookings_repo.list_bookings_for_braider(
        db, profile.id, params=params, status=status, date_from=date_from, date_to=date_to, search=search
    )
    style_names = await _style_names_for(db, items, locale=locale)
    braider_names = await braiders_repo.list_display_names(db, [b.braider_id for b in items])
    customer_names = await users_repo.list_full_names(db, [b.customer_id for b in items])
    return _paginated_response(items, meta, style_names, braider_names, customer_names)


def _to_admin_summary(
    booking: Booking,
    *,
    style_names: dict[uuid.UUID, str],
    customer_info: dict[uuid.UUID, dict[str, str]],
    braider_info: dict[uuid.UUID, dict[str, str | uuid.UUID]],
) -> AdminBookingSummaryResponse:
    customer = customer_info.get(booking.customer_id, {})
    braider = braider_info.get(booking.braider_id, {})
    return AdminBookingSummaryResponse(
        id=booking.id,
        reference=booking.reference,
        status=booking.status,
        customer_id=booking.customer_id,
        customer_name=str(customer.get("name", "")),
        customer_email=str(customer.get("email", "")),
        braider_id=booking.braider_id,
        braider_user_id=braider.get("user_id") if isinstance(braider.get("user_id"), uuid.UUID) else None,
        braider_name=str(braider.get("name", "")),
        braider_email=str(braider.get("email", "")) if braider.get("email") is not None else None,
        style_id=booking.style_id,
        style_name=style_names.get(booking.style_id, ""),
        starts_at=booking.starts_at,
        ends_at=booking.ends_at,
        total=booking.total,
        currency=booking.currency,
        country=booking.country,
        is_mobile=booking.is_mobile,
        payment_schedule=booking.payment_schedule,
        created_at=booking.created_at,
    )


async def _admin_context_for(
    db: AsyncSession, bookings: list[Booking], *, locale: str
) -> tuple[
    dict[uuid.UUID, str],
    dict[uuid.UUID, dict[str, str]],
    dict[uuid.UUID, dict[str, str | uuid.UUID]],
]:
    style_names = await _style_names_for(db, bookings, locale=locale)
    customer_info = await users_repo.list_admin_user_info(db, [b.customer_id for b in bookings])
    braider_info = await braiders_repo.list_admin_display_info(db, [b.braider_id for b in bookings])
    return style_names, customer_info, braider_info


async def list_admin_bookings(
    db: AsyncSession,
    *,
    params: PaginationParams,
    locale: str,
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
) -> PaginatedAdminBookingsResponse:
    _validate_date_range(date_from, date_to)
    _validate_date_range(created_from, created_to)
    items, meta = await bookings_repo.list_bookings_for_admin(
        db,
        params=params,
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
    style_names, customer_info, braider_info = await _admin_context_for(
        db, items, locale=locale
    )
    return PaginatedAdminBookingsResponse(
        items=[
            _to_admin_summary(
                b,
                style_names=style_names,
                customer_info=customer_info,
                braider_info=braider_info,
            )
            for b in items
        ],
        page=meta.page,
        page_size=meta.page_size,
        total_items=meta.total_items,
        total_pages=meta.total_pages,
        has_next=meta.has_next,
        has_previous=meta.has_previous,
    )


async def _admin_braider_actor(
    db: AsyncSession, braider_id: uuid.UUID
) -> AdminBookingStatsActorResponse:
    profile = await braiders_repo.get_profile_by_id(db, braider_id)
    if profile is None:
        raise BraiderNotFoundError()
    info = (await braiders_repo.list_admin_display_info(db, [braider_id])).get(braider_id, {})
    return AdminBookingStatsActorResponse(
        id=braider_id,
        user_id=info.get("user_id") if isinstance(info.get("user_id"), uuid.UUID) else profile.user_id,
        name=str(info.get("name", "")),
        email=str(info.get("email", "")) if info.get("email") is not None else None,
    )


async def _admin_customer_actor(
    db: AsyncSession, customer_id: uuid.UUID
) -> AdminBookingStatsActorResponse:
    customer = await users_repo.get_user_by_id(db, customer_id)
    if customer is None or customer.user_type != UserType.CUSTOMER:
        raise UserNotFoundError()
    return AdminBookingStatsActorResponse(
        id=customer.id,
        name=f"{customer.first_name} {customer.last_name or ''}".strip(),
        email=customer.email,
    )


async def list_admin_bookings_for_braider(
    db: AsyncSession,
    *,
    braider_id: uuid.UUID,
    params: PaginationParams,
    locale: str,
    status: BookingStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
    customer_id: uuid.UUID | None = None,
    country: str | None = None,
    currency: Currency | None = None,
    is_mobile: bool | None = None,
    payment_schedule: PaymentSchedule | None = None,
    search: str | None = None,
) -> PaginatedAdminBookingsResponse:
    await _admin_braider_actor(db, braider_id)
    if customer_id is not None:
        await _admin_customer_actor(db, customer_id)
    return await list_admin_bookings(
        db,
        params=params,
        locale=locale,
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


async def list_admin_bookings_for_customer(
    db: AsyncSession,
    *,
    customer_id: uuid.UUID,
    params: PaginationParams,
    locale: str,
    status: BookingStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
    braider_id: uuid.UUID | None = None,
    country: str | None = None,
    currency: Currency | None = None,
    is_mobile: bool | None = None,
    payment_schedule: PaymentSchedule | None = None,
    search: str | None = None,
) -> PaginatedAdminBookingsResponse:
    await _admin_customer_actor(db, customer_id)
    if braider_id is not None:
        await _admin_braider_actor(db, braider_id)
    return await list_admin_bookings(
        db,
        params=params,
        locale=locale,
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


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _percent_decimal(part: Decimal, whole: Decimal) -> Decimal:
    if whole == 0:
        return _ZERO
    return (part * _HUNDRED / whole).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


async def _admin_chart_actors(
    db: AsyncSession, *, braider_id: uuid.UUID | None, customer_id: uuid.UUID | None
) -> tuple[AdminBookingStatsActorResponse | None, AdminBookingStatsActorResponse | None]:
    braider = await _admin_braider_actor(db, braider_id) if braider_id is not None else None
    customer = await _admin_customer_actor(db, customer_id) if customer_id is not None else None
    return braider, customer


def _chart_metric(*, braider_id: uuid.UUID | None) -> str:
    return "BRAIDER_EARNINGS" if braider_id is not None else "CUSTOMER_SPEND"


def _chart_metric_label(*, braider_id: uuid.UUID | None) -> str:
    return "braider_earnings" if braider_id is not None else "customer_spend"


def _resolved_chart_dates(date_from: date | None, date_to: date | None) -> tuple[date, date]:
    resolved_to = date_to or datetime.now(UTC).date()
    resolved_from = date_from or (resolved_to - timedelta(days=_DEFAULT_CHART_WINDOW_DAYS))
    return resolved_from, resolved_to


async def get_admin_booking_stats(
    db: AsyncSession,
    *,
    braider_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
    status: BookingStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
    payment_date_from: date | None = None,
    payment_date_to: date | None = None,
    country: str | None = None,
    currency: Currency | None = None,
    is_mobile: bool | None = None,
    payment_schedule: PaymentSchedule | None = None,
    search: str | None = None,
) -> AdminBookingStatsResponse:
    _validate_date_range(date_from, date_to)
    _validate_date_range(created_from, created_to)
    _validate_date_range(payment_date_from, payment_date_to)

    braider = await _admin_braider_actor(db, braider_id) if braider_id is not None else None
    customer = await _admin_customer_actor(db, customer_id) if customer_id is not None else None

    stats = await bookings_repo.get_booking_stats_for_admin(
        db,
        status=status,
        date_from=date_from,
        date_to=date_to,
        created_from=created_from,
        created_to=created_to,
        payment_date_from=payment_date_from,
        payment_date_to=payment_date_to,
        customer_id=customer_id,
        braider_id=braider_id,
        country=country,
        currency=currency,
        is_mobile=is_mobile,
        payment_schedule=payment_schedule,
        search=search,
    )
    total_paid = from_minor_units(stats["total_paid_minor"])
    total_refunded = from_minor_units(stats["total_refunded_minor"])
    net_paid = total_paid - total_refunded
    return AdminBookingStatsResponse(
        braider=braider,
        customer=customer,
        total_bookings=stats["total_bookings"],
        status_counts=stats["status_counts"],
        completed_bookings=stats["completed_bookings"],
        upcoming_bookings=stats["upcoming_bookings"],
        declined_bookings=stats["declined_bookings"],
        pending_payment_bookings=stats["pending_payment_bookings"],
        no_show_bookings=stats["no_show_bookings"],
        disputed_bookings=stats["disputed_bookings"],
        mobile_bookings=stats["mobile_bookings"],
        salon_bookings=stats["salon_bookings"],
        unique_customers=stats["unique_customers"],
        repeat_customers=stats["repeat_customers"],
        unique_braiders=stats["unique_braiders"],
        repeat_braiders=stats["repeat_braiders"],
        total_booking_value=_money(stats["total_booking_value"]),
        average_booking_value=_money(stats["average_booking_value"]),
        service_subtotal=_money(stats["service_subtotal"]),
        platform_fee_total=_money(stats["platform_fee_total"]),
        vat_total=_money(stats["vat_total"]),
        total_amount_paid=total_paid,
        total_amount_refunded=total_refunded,
        net_amount_paid=net_paid,
        pending_payment_amount=from_minor_units(stats["pending_payment_amount_minor"]),
        total_amount_made_by_braider=from_minor_units(stats["braider_earnings_minor"]),
        total_amount_spent_by_customer=net_paid,
    )


async def get_admin_platform_overview(
    db: AsyncSession,
    *,
    status: BookingStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
    payment_date_from: date | None = None,
    payment_date_to: date | None = None,
    country: str | None = None,
    currency: Currency | None = None,
    is_mobile: bool | None = None,
    payment_schedule: PaymentSchedule | None = None,
    search: str | None = None,
) -> AdminBookingStatsResponse:
    return await get_admin_booking_stats(
        db,
        status=status,
        date_from=date_from,
        date_to=date_to,
        created_from=created_from,
        created_to=created_to,
        payment_date_from=payment_date_from,
        payment_date_to=payment_date_to,
        country=country,
        currency=currency,
        is_mobile=is_mobile,
        payment_schedule=payment_schedule,
        search=search,
    )


async def get_admin_platform_financials(
    db: AsyncSession,
    *,
    status: BookingStatus | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
    payment_date_from: date | None = None,
    payment_date_to: date | None = None,
    country: str | None = None,
    currency: Currency | None = None,
    is_mobile: bool | None = None,
    payment_schedule: PaymentSchedule | None = None,
    search: str | None = None,
) -> AdminPlatformFinancialsResponse:
    overview = await get_admin_platform_overview(
        db,
        status=status,
        date_from=date_from,
        date_to=date_to,
        created_from=created_from,
        created_to=created_to,
        payment_date_from=payment_date_from,
        payment_date_to=payment_date_to,
        country=country,
        currency=currency,
        is_mobile=is_mobile,
        payment_schedule=payment_schedule,
        search=search,
    )
    gross_margin_before_tax = overview.net_amount_paid - overview.total_amount_made_by_braider
    estimated_profit_after_vat = gross_margin_before_tax - overview.vat_total
    return AdminPlatformFinancialsResponse(
        currency=Currency.EUR,
        total_bookings=overview.total_bookings,
        completed_bookings=overview.completed_bookings,
        total_booking_value=overview.total_booking_value,
        average_booking_value=overview.average_booking_value,
        service_subtotal=overview.service_subtotal,
        platform_fee_total=overview.platform_fee_total,
        vat_total=overview.vat_total,
        total_amount_paid=overview.total_amount_paid,
        total_amount_refunded=overview.total_amount_refunded,
        net_amount_paid=overview.net_amount_paid,
        pending_payment_amount=overview.pending_payment_amount,
        braider_earnings=overview.total_amount_made_by_braider,
        gross_margin_before_tax=_money(gross_margin_before_tax),
        estimated_profit_after_vat=_money(estimated_profit_after_vat),
    )


async def get_admin_revenue_chart(
    db: AsyncSession,
    *,
    braider_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    payment_date_from: date | None = None,
    payment_date_to: date | None = None,
    interval: Literal["day", "week", "month"],
    country: str | None = None,
    currency: Currency | None = None,
    is_mobile: bool | None = None,
    payment_schedule: PaymentSchedule | None = None,
    search: str | None = None,
) -> AdminRevenueChartResponse:
    _validate_date_range(date_from, date_to)
    _validate_date_range(payment_date_from, payment_date_to)
    resolved_from, resolved_to = _resolved_chart_dates(date_from, date_to)
    braider, customer = await _admin_chart_actors(
        db, braider_id=braider_id, customer_id=customer_id
    )
    rows = await bookings_repo.get_admin_revenue_timeseries(
        db,
        metric=_chart_metric(braider_id=braider_id),
        date_from=resolved_from,
        date_to=resolved_to,
        interval=interval,
        payment_date_from=payment_date_from,
        payment_date_to=payment_date_to,
        customer_id=customer_id,
        braider_id=braider_id,
        country=country,
        currency=currency,
        is_mobile=is_mobile,
        payment_schedule=payment_schedule,
        search=search,
    )
    return AdminRevenueChartResponse(
        braider=braider,
        customer=customer,
        interval=interval,
        metric=_chart_metric_label(braider_id=braider_id),
        currency=Currency.EUR,
        points=[
            AdminRevenueChartPoint(
                bucket=bucket,
                amount=from_minor_units(amount_minor),
                bookings_count=bookings_count,
            )
            for bucket, amount_minor, bookings_count in rows
        ],
    )


async def get_admin_platform_revenue_chart(
    db: AsyncSession,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    payment_date_from: date | None = None,
    payment_date_to: date | None = None,
    interval: Literal["day", "week", "month"],
    country: str | None = None,
    currency: Currency | None = None,
    is_mobile: bool | None = None,
    payment_schedule: PaymentSchedule | None = None,
    search: str | None = None,
) -> AdminRevenueChartResponse:
    _validate_date_range(date_from, date_to)
    _validate_date_range(payment_date_from, payment_date_to)
    resolved_from, resolved_to = _resolved_chart_dates(date_from, date_to)
    rows = await bookings_repo.get_admin_revenue_timeseries(
        db,
        metric="CUSTOMER_SPEND",
        date_from=resolved_from,
        date_to=resolved_to,
        interval=interval,
        payment_date_from=payment_date_from,
        payment_date_to=payment_date_to,
        country=country,
        currency=currency,
        is_mobile=is_mobile,
        payment_schedule=payment_schedule,
        search=search,
    )
    return AdminRevenueChartResponse(
        interval=interval,
        metric="platform_gmv",
        currency=Currency.EUR,
        points=[
            AdminRevenueChartPoint(
                bucket=bucket,
                amount=from_minor_units(amount_minor),
                bookings_count=bookings_count,
            )
            for bucket, amount_minor, bookings_count in rows
        ],
    )


async def get_admin_weekday_chart(
    db: AsyncSession,
    *,
    braider_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    payment_date_from: date | None = None,
    payment_date_to: date | None = None,
    country: str | None = None,
    currency: Currency | None = None,
    is_mobile: bool | None = None,
    payment_schedule: PaymentSchedule | None = None,
    search: str | None = None,
) -> AdminBarChartResponse:
    _validate_date_range(date_from, date_to)
    _validate_date_range(payment_date_from, payment_date_to)
    braider, customer = await _admin_chart_actors(
        db, braider_id=braider_id, customer_id=customer_id
    )
    rows = await bookings_repo.get_admin_bookings_by_weekday(
        db,
        metric=_chart_metric(braider_id=braider_id),
        date_from=date_from,
        date_to=date_to,
        payment_date_from=payment_date_from,
        payment_date_to=payment_date_to,
        customer_id=customer_id,
        braider_id=braider_id,
        country=country,
        currency=currency,
        is_mobile=is_mobile,
        payment_schedule=payment_schedule,
        search=search,
    )
    by_weekday = {weekday: (count, amount_minor) for weekday, count, amount_minor in rows}
    labels = {
        1: "Monday",
        2: "Tuesday",
        3: "Wednesday",
        4: "Thursday",
        5: "Friday",
        6: "Saturday",
        7: "Sunday",
    }
    return AdminBarChartResponse(
        braider=braider,
        customer=customer,
        metric=f"{_chart_metric_label(braider_id=braider_id)}_by_weekday",
        currency=Currency.EUR,
        points=[
            AdminBarChartPoint(
                key=str(weekday),
                label=labels[weekday],
                bookings_count=by_weekday.get(weekday, (0, 0))[0],
                amount=from_minor_units(by_weekday.get(weekday, (0, 0))[1]),
            )
            for weekday in range(1, 8)
        ],
    )


async def get_admin_platform_weekday_chart(
    db: AsyncSession,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    payment_date_from: date | None = None,
    payment_date_to: date | None = None,
    country: str | None = None,
    currency: Currency | None = None,
    is_mobile: bool | None = None,
    payment_schedule: PaymentSchedule | None = None,
    search: str | None = None,
) -> AdminBarChartResponse:
    chart = await get_admin_weekday_chart(
        db,
        date_from=date_from,
        date_to=date_to,
        payment_date_from=payment_date_from,
        payment_date_to=payment_date_to,
        country=country,
        currency=currency,
        is_mobile=is_mobile,
        payment_schedule=payment_schedule,
        search=search,
    )
    chart.metric = "platform_gmv_by_weekday"
    return chart


async def get_admin_status_chart(
    db: AsyncSession,
    *,
    braider_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    payment_date_from: date | None = None,
    payment_date_to: date | None = None,
    country: str | None = None,
    currency: Currency | None = None,
    is_mobile: bool | None = None,
    payment_schedule: PaymentSchedule | None = None,
    search: str | None = None,
) -> AdminBarChartResponse:
    _validate_date_range(date_from, date_to)
    _validate_date_range(payment_date_from, payment_date_to)
    braider, customer = await _admin_chart_actors(
        db, braider_id=braider_id, customer_id=customer_id
    )
    rows = await bookings_repo.get_admin_status_breakdown(
        db,
        metric=_chart_metric(braider_id=braider_id),
        date_from=date_from,
        date_to=date_to,
        payment_date_from=payment_date_from,
        payment_date_to=payment_date_to,
        customer_id=customer_id,
        braider_id=braider_id,
        country=country,
        currency=currency,
        is_mobile=is_mobile,
        payment_schedule=payment_schedule,
        search=search,
    )
    amounts = {status: (count, amount_minor) for status, count, amount_minor in rows}
    return AdminBarChartResponse(
        braider=braider,
        customer=customer,
        metric=f"{_chart_metric_label(braider_id=braider_id)}_by_status",
        currency=Currency.EUR,
        points=[
            AdminBarChartPoint(
                key=status.value,
                label=status.value,
                bookings_count=amounts.get(status, (0, 0))[0],
                amount=from_minor_units(amounts.get(status, (0, 0))[1]),
            )
            for status in BookingStatus
        ],
    )


async def get_admin_platform_status_chart(
    db: AsyncSession,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    payment_date_from: date | None = None,
    payment_date_to: date | None = None,
    country: str | None = None,
    currency: Currency | None = None,
    is_mobile: bool | None = None,
    payment_schedule: PaymentSchedule | None = None,
    search: str | None = None,
) -> AdminBarChartResponse:
    chart = await get_admin_status_chart(
        db,
        date_from=date_from,
        date_to=date_to,
        payment_date_from=payment_date_from,
        payment_date_to=payment_date_to,
        country=country,
        currency=currency,
        is_mobile=is_mobile,
        payment_schedule=payment_schedule,
        search=search,
    )
    chart.metric = "platform_gmv_by_status"
    return chart


async def get_admin_style_pie_chart(
    db: AsyncSession,
    *,
    braider_id: uuid.UUID | None = None,
    customer_id: uuid.UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    payment_date_from: date | None = None,
    payment_date_to: date | None = None,
    country: str | None = None,
    currency: Currency | None = None,
    is_mobile: bool | None = None,
    payment_schedule: PaymentSchedule | None = None,
    search: str | None = None,
    locale: str,
    limit: int = _DEFAULT_TOP_STYLES_LIMIT,
) -> AdminStylePieChartResponse:
    _validate_date_range(date_from, date_to)
    _validate_date_range(payment_date_from, payment_date_to)
    braider, customer = await _admin_chart_actors(
        db, braider_id=braider_id, customer_id=customer_id
    )
    rows = await bookings_repo.get_admin_style_breakdown(
        db,
        metric=_chart_metric(braider_id=braider_id),
        date_from=date_from,
        date_to=date_to,
        payment_date_from=payment_date_from,
        payment_date_to=payment_date_to,
        customer_id=customer_id,
        braider_id=braider_id,
        country=country,
        currency=currency,
        is_mobile=is_mobile,
        payment_schedule=payment_schedule,
        search=search,
    )
    total_amount = from_minor_units(sum(amount_minor for _, _, _, amount_minor in rows))
    slices = [
        AdminStyleChartSlice(
            style_id=style_id,
            style_name=localize_field(style, "name", locale) or style.name_en,
            bookings_count=count,
            amount=from_minor_units(amount_minor),
            share=_percent_decimal(from_minor_units(amount_minor), total_amount),
        )
        for style_id, style, count, amount_minor in rows[:limit]
    ]
    if len(rows) > limit:
        other_count = sum(count for _, _, count, _ in rows[limit:])
        other_amount = from_minor_units(sum(amount_minor for _, _, _, amount_minor in rows[limit:]))
        slices.append(
            AdminStyleChartSlice(
                style_id=None,
                style_name=translate("dashboard.style_other_label", locale),
                bookings_count=other_count,
                amount=other_amount,
                share=_percent_decimal(other_amount, total_amount),
            )
        )
    return AdminStylePieChartResponse(
        braider=braider,
        customer=customer,
        metric=f"{_chart_metric_label(braider_id=braider_id)}_by_style",
        currency=Currency.EUR,
        total_amount=total_amount,
        slices=slices,
    )


async def get_admin_platform_style_pie_chart(
    db: AsyncSession,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    payment_date_from: date | None = None,
    payment_date_to: date | None = None,
    country: str | None = None,
    currency: Currency | None = None,
    is_mobile: bool | None = None,
    payment_schedule: PaymentSchedule | None = None,
    search: str | None = None,
    locale: str,
    limit: int = _DEFAULT_TOP_STYLES_LIMIT,
) -> AdminStylePieChartResponse:
    chart = await get_admin_style_pie_chart(
        db,
        date_from=date_from,
        date_to=date_to,
        payment_date_from=payment_date_from,
        payment_date_to=payment_date_to,
        country=country,
        currency=currency,
        is_mobile=is_mobile,
        payment_schedule=payment_schedule,
        search=search,
        locale=locale,
        limit=limit,
    )
    chart.metric = "platform_gmv_by_style"
    return chart


async def get_admin_platform_country_chart(
    db: AsyncSession,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    payment_date_from: date | None = None,
    payment_date_to: date | None = None,
    country: str | None = None,
    currency: Currency | None = None,
    is_mobile: bool | None = None,
    payment_schedule: PaymentSchedule | None = None,
    search: str | None = None,
) -> AdminBarChartResponse:
    _validate_date_range(date_from, date_to)
    _validate_date_range(payment_date_from, payment_date_to)
    rows = await bookings_repo.get_admin_country_breakdown(
        db,
        metric="CUSTOMER_SPEND",
        date_from=date_from,
        date_to=date_to,
        payment_date_from=payment_date_from,
        payment_date_to=payment_date_to,
        country=country,
        currency=currency,
        is_mobile=is_mobile,
        payment_schedule=payment_schedule,
        search=search,
    )
    return AdminBarChartResponse(
        metric="platform_gmv_by_country",
        currency=Currency.EUR,
        points=[
            AdminBarChartPoint(
                key=country_code,
                label=country_code,
                bookings_count=bookings_count,
                amount=from_minor_units(amount_minor),
            )
            for country_code, bookings_count, amount_minor in rows
        ],
    )


async def get_admin_booking(
    db: AsyncSession, booking_id: uuid.UUID, *, locale: str
) -> AdminBookingResponse:
    booking = await bookings_repo.get_booking_by_id(db, booking_id)
    if booking is None:
        raise BookingNotFoundError()

    style_names, customer_info, braider_info = await _admin_context_for(
        db, [booking], locale=locale
    )
    summary = _to_admin_summary(
        booking,
        style_names=style_names,
        customer_info=customer_info,
        braider_info=braider_info,
    )
    items = await bookings_repo.list_items(db, booking.id)
    payments = await bookings_repo.list_payments(db, booking.id)
    return AdminBookingResponse(
        **summary.model_dump(),
        duration_minutes=booking.duration_minutes,
        client_address=booking.client_address,
        client_latitude=booking.client_latitude,
        client_longitude=booking.client_longitude,
        service_subtotal=booking.service_subtotal,
        travel_fee=booking.travel_fee,
        subtotal=booking.subtotal,
        platform_fee=booking.platform_fee,
        vat_on_service=booking.vat_on_service,
        vat_on_platform_fee=booking.vat_on_platform_fee,
        vat_total=booking.vat_total,
        deposit_amount=booking.deposit_amount,
        balance_amount=booking.balance_amount,
        braider_share_total=booking.braider_share_total,
        braider_share_deposit=booking.braider_share_deposit,
        braider_share_balance=booking.braider_share_balance,
        cancellation_cutoff_at=booking.cancellation_cutoff_at,
        hold_expires_at=booking.hold_expires_at,
        balance_charge_due_at=booking.balance_charge_due_at,
        balance_charge_state=booking.balance_charge_state,
        confirmed_at=booking.confirmed_at,
        cancelled_at=booking.cancelled_at,
        cancelled_by=booking.cancelled_by,
        items=[_item_to_response(item, locale) for item in items],
        payments=[
            AdminBookingPaymentResponse(
                id=p.id,
                purpose=p.purpose,
                status=p.status,
                amount=from_minor_units(p.amount_minor),
                amount_refunded=from_minor_units(p.amount_refunded_minor),
                braider_share=from_minor_units(p.braider_share_minor),
                is_refunded=p.amount_refunded_minor > 0,
                currency=p.currency,
                stripe_payment_intent_id=p.stripe_payment_intent_id,
                stripe_charge_id=p.stripe_charge_id,
                is_off_session=p.is_off_session,
                attempt_number=p.attempt_number,
                failure_code=p.failure_code,
                failure_message=p.failure_message,
                created_at=p.created_at,
            )
            for p in payments
        ],
        updated_at=booking.updated_at,
    )


def _paginated_response(
    items: list[Booking],
    meta: PaginationMeta,
    style_names: dict[uuid.UUID, str],
    braider_names: dict[uuid.UUID, str],
    customer_names: dict[uuid.UUID, str],
) -> PaginatedBookingsResponse:
    return PaginatedBookingsResponse(
        items=[
            _to_summary(
                b, style_names=style_names, braider_names=braider_names, customer_names=customer_names
            )
            for b in items
        ],
        page=meta.page,
        page_size=meta.page_size,
        total_items=meta.total_items,
        total_pages=meta.total_pages,
        has_next=meta.has_next,
        has_previous=meta.has_previous,
    )
