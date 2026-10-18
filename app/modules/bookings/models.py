import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.currency import Currency
from app.core.database import Base
from app.modules.bookings.enums import (
    BalanceChargeState,
    BookingItemType,
    BookingStatus,
    BraiderVatStatus,
    PaymentPurpose,
    PaymentSchedule,
    PaymentStatus,
    RefundStatus,
    TransferStatus,
)
from app.modules.platform_settings.models import SettingValueType


class CancelledBy(str, enum.Enum):
    CUSTOMER = "CUSTOMER"
    BRAIDER = "BRAIDER"
    SYSTEM = "SYSTEM"


class Booking(Base):
    """The reservation itself. Everything priced is snapshotted from the
    consumed `BookingCalculation` at creation time (never re-read from the
    live catalog later) so a receipt or dispute years from now reflects
    exactly what the customer agreed to pay.

    Double-booking is prevented at the database level, not in application
    code - see the `ex_bookings_no_overlap` GIST exclusion constraint added
    in this table's migration (not expressible via the SQLAlchemy ORM's
    plain constraint types, same reasoning as booking_calculations' partial
    cleanup index). `blocked_from`/`blocked_until` (= starts_at / ends_at +
    the braider's buffer_minutes at booking time) is what that constraint
    actually ranges over, not starts_at/ends_at directly.
    """

    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint(
            "service_subtotal >= 0 AND travel_fee >= 0 AND subtotal >= 0 AND "
            "platform_fee_value >= 0 AND platform_fee >= 0 AND "
            "vat_service_value >= 0 AND vat_platform_fee_value >= 0 AND "
            "vat_on_service >= 0 AND vat_on_platform_fee >= 0 AND vat_total >= 0 AND "
            "total >= 0 AND deposit_value >= 0 AND deposit_amount >= 0 AND balance_amount >= 0 AND "
            "braider_share_total >= 0 AND braider_share_deposit >= 0 AND braider_share_balance >= 0 AND "
            "subtotal = service_subtotal + travel_fee AND "
            "total = subtotal + platform_fee + vat_total AND "
            "vat_total = vat_on_service + vat_on_platform_fee AND "
            "deposit_amount + balance_amount = total AND "
            "braider_share_deposit + braider_share_balance = braider_share_total AND "
            "ends_at > starts_at AND blocked_until > blocked_from",
            name="ck_bookings_amounts",
        ),
        UniqueConstraint("reference", name="uq_bookings_reference"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reference: Mapped[str] = mapped_column(String(12), nullable=False)

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    braider_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("braider_profiles.id"),
        nullable=False,
        index=True,
    )
    booking_calculation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("booking_calculations.id"), nullable=False, unique=True
    )

    braider_style_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("braider_styles.id"), nullable=False
    )
    style_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("styles.id"), nullable=False)
    style_variation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("style_variations.id"), nullable=True
    )
    braider_style_variation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    is_mobile: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    country: Mapped[str] = mapped_column(String(2), nullable=False)
    client_address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    client_latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    client_longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    currency: Mapped[Currency] = mapped_column(Enum(Currency, name="currency"), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)

    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    braider_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    blocked_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    blocked_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    service_subtotal: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    travel_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("0.00"))
    subtotal: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    platform_fee_type: Mapped[SettingValueType] = mapped_column(
        Enum(SettingValueType, name="setting_value_type"), nullable=False
    )
    platform_fee_value: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    platform_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    vat_service_type: Mapped[SettingValueType] = mapped_column(
        Enum(SettingValueType, name="setting_value_type"), nullable=False
    )
    vat_service_value: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    vat_on_service: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    vat_platform_fee_type: Mapped[SettingValueType] = mapped_column(
        Enum(SettingValueType, name="setting_value_type"), nullable=False
    )
    vat_platform_fee_value: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    vat_on_platform_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    vat_total: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    deposit_type: Mapped[SettingValueType] = mapped_column(
        Enum(SettingValueType, name="setting_value_type"), nullable=False
    )
    deposit_value: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    deposit_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    balance_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    payment_schedule: Mapped[PaymentSchedule] = mapped_column(
        Enum(PaymentSchedule, name="payment_schedule"), nullable=False
    )

    braider_share_total: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    braider_share_deposit: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    braider_share_balance: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

    # Snapshotted for receipts (design correction #5 / plan flagged item #1)
    # - no collection UI exists yet, so this is UNKNOWN for every booking
    # today. Kept on the booking (not looked up live from the braider at
    # receipt time) so a two-year-old receipt reflects what applied then,
    # not whatever the braider's status happens to be now.
    braider_vat_status: Mapped[BraiderVatStatus] = mapped_column(
        Enum(BraiderVatStatus, name="braider_vat_status"),
        nullable=False,
        default=BraiderVatStatus.UNKNOWN,
    )
    braider_vat_number: Mapped[str | None] = mapped_column(String(32), nullable=True)

    status: Mapped[BookingStatus] = mapped_column(
        Enum(BookingStatus, name="booking_status"),
        nullable=False,
        default=BookingStatus.PENDING_PAYMENT,
        index=True,
    )

    hold_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    balance_charge_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    balance_charge_state: Mapped[BalanceChargeState] = mapped_column(
        Enum(BalanceChargeState, name="balance_charge_state"),
        nullable=False,
        default=BalanceChargeState.NOT_APPLICABLE,
    )
    balance_charge_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    balance_charge_last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_by: Mapped[CancelledBy | None] = mapped_column(
        Enum(CancelledBy, name="cancelled_by"), nullable=True
    )
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    stripe_dispute_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    disputed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set the moment a dispute lands (design correction #6) - checked by
    # release_due_payouts_cron so a booking mid-dispute never has its
    # payout released out from under the reversal attempt. Never cleared
    # automatically; a resolved dispute is an admin/Phase-7 action.
    payouts_frozen: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stripe_payment_method_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    locale: Mapped[str] = mapped_column(String(5), nullable=False)
    terms_version: Mapped[str] = mapped_column(String(20), nullable=False)
    terms_accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class BookingItem(Base):
    """One line of a booking's frozen price breakdown - including the fee
    and VAT lines, not just the service/addons. `name_en/de/fr` and every
    `source_*_id` are snapshots with no FK (mirroring
    booking_calculation_addons): a braider deleting a style or add-on years
    later must never alter or break a historical booking/receipt."""

    __tablename__ = "booking_items"
    __table_args__ = (CheckConstraint("quantity > 0 AND unit_amount >= 0 AND line_amount >= 0", name="ck_booking_items_amounts"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_type: Mapped[BookingItemType] = mapped_column(
        Enum(BookingItemType, name="booking_item_type"), nullable=False
    )
    name_en: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name_de: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name_fr: Mapped[str | None] = mapped_column(String(255), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unit_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    line_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    vat_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    source_style_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_style_variation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_addon_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_braider_style_addon_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class BookingPayment(Base):
    """A mirror of one Stripe PaymentIntent for this booking. Money here is
    integer minor units (see app.core.money) - unlike `bookings` itself,
    this table must be byte-exact against what Stripe actually moved, not
    rounded Decimal display math.

    At most one row per (booking_id, purpose) may ever reach SUCCEEDED - see
    the partial unique index added in this table's migration (not
    expressible via the ORM) - so a failed balance attempt can be retried
    (new row, incremented attempt_number) while still making a double
    successful charge for the same purpose impossible at the DB level.
    """

    __tablename__ = "booking_payments"
    __table_args__ = (
        CheckConstraint(
            "amount_minor >= 0 AND braider_share_minor >= 0 AND "
            "amount_refunded_minor >= 0 AND amount_transferred_minor >= 0 AND attempt_number > 0",
            name="ck_booking_payments_amounts",
        ),
        UniqueConstraint("idempotency_key", name="uq_booking_payments_idempotency_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    purpose: Mapped[PaymentPurpose] = mapped_column(Enum(PaymentPurpose, name="payment_purpose"), nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="payment_status"), nullable=False, default=PaymentStatus.PENDING, index=True
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[Currency] = mapped_column(Enum(Currency, name="currency"), nullable=False)
    braider_share_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount_refunded_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    amount_transferred_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    stripe_charge_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    is_off_session: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    transfer_group: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    failure_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class BookingRefund(Base):
    """A mirror of one Stripe Refund against a `booking_payments` row - a
    braider cancellation refunds every succeeded payment on the booking in
    full (deposit included), so there can be more than one refund per
    booking but at most one per payment (Stripe itself would reject a
    second full refund on an already-fully-refunded PaymentIntent)."""

    __tablename__ = "booking_refunds"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="ck_booking_refunds_amount"),
        UniqueConstraint("idempotency_key", name="uq_booking_refunds_idempotency_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    booking_payment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("booking_payments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[RefundStatus] = mapped_column(
        Enum(RefundStatus, name="refund_status"), nullable=False, default=RefundStatus.PENDING
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[Currency] = mapped_column(Enum(Currency, name="currency"), nullable=False)
    stripe_refund_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class BookingTransfer(Base):
    """A mirror of one Stripe Transfer moving a braider's share out of the
    platform account for a specific `booking_payments` row -
    `source_transaction=<that payment's charge>` is what makes the funds
    available immediately (no negative-balance risk on the connected
    account). The partial unique index (in this table's migration) caps it
    at one PENDING/SUCCEEDED transfer per payment - a REVERSED one frees the
    slot back up only in the sense that a *new* transfer would need a new
    row, never reusing this one."""

    __tablename__ = "booking_transfers"
    __table_args__ = (CheckConstraint("amount_minor > 0", name="ck_booking_transfers_amount"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    booking_payment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("booking_payments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    destination_account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[TransferStatus] = mapped_column(
        Enum(TransferStatus, name="transfer_status"), nullable=False, default=TransferStatus.PENDING
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[Currency] = mapped_column(Enum(Currency, name="currency"), nullable=False)
    transfer_group: Mapped[str] = mapped_column(String(64), nullable=False)
    stripe_transfer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    failure_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class BookingTransferReversal(Base):
    """Design correction #6 - a dedicated audit row for reversing a
    `booking_transfers` row on `charge.dispute.created`, distinct from the
    plain `status=REVERSED` flip a braider cancellation does on its own
    (rare, defensive, same-request path). Disputes land 120+ days out,
    after the braider has usually already been paid - this table is what
    lets that reversal attempt (and its failure, if the connected
    account's balance can't cover the clawback) be tracked independently
    of the original transfer."""

    __tablename__ = "booking_transfer_reversals"
    __table_args__ = (CheckConstraint("amount_minor > 0", name="ck_booking_transfer_reversals_amount"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    booking_transfer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("booking_transfers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[TransferStatus] = mapped_column(
        Enum(TransferStatus, name="transfer_status"), nullable=False, default=TransferStatus.PENDING
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[Currency] = mapped_column(Enum(Currency, name="currency"), nullable=False)
    stripe_reversal_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    failure_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
