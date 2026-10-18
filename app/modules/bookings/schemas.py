import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, field_validator

from app.core.currency import Currency
from app.modules.bookings.enums import (
    BalanceChargeState,
    BookingItemType,
    BookingStatus,
    PaymentPurpose,
    PaymentSchedule,
    PaymentStatus,
    ReceiptType,
)
from app.modules.bookings.models import CancelledBy


class BookingCreateRequest(BaseModel):
    booking_calculation_id: uuid.UUID
    starts_at: datetime
    terms_accepted: bool

    @field_validator("terms_accepted")
    @classmethod
    def _must_accept_terms(cls, value: bool) -> bool:
        if not value:
            raise ValueError("terms_accepted must be true")
        return value


class BookingRescheduleRequest(BaseModel):
    starts_at: datetime


class BraiderBookingCancelRequest(BaseModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("reason must not be blank")
        return stripped


class BookingItemResponse(BaseModel):
    item_type: BookingItemType
    name: str | None
    quantity: int
    unit_amount: Decimal
    line_amount: Decimal
    is_required: bool


class BookingPaymentResponse(BaseModel):
    purpose: PaymentPurpose
    status: PaymentStatus
    amount: Decimal
    currency: Currency
    client_secret: str | None = None


class ReceiptSummaryResponse(BaseModel):
    id: uuid.UUID
    type: ReceiptType
    receipt_number: str
    issued_at: datetime
    amount_total: Decimal
    currency: Currency
    url: str


class BookingResponse(BaseModel):
    id: uuid.UUID
    reference: str
    status: BookingStatus
    braider_id: uuid.UUID
    braider_name: str
    customer_name: str
    style_id: uuid.UUID
    style_name: str
    duration_minutes: int
    is_mobile: bool
    client_address: str | None
    client_latitude: Decimal | None
    client_longitude: Decimal | None
    country: str
    currency: Currency
    starts_at: datetime
    ends_at: datetime
    items: list[BookingItemResponse]
    service_subtotal: Decimal
    travel_fee: Decimal
    subtotal: Decimal
    platform_fee: Decimal
    vat_on_service: Decimal
    vat_on_platform_fee: Decimal
    vat_total: Decimal
    total: Decimal
    deposit_amount: Decimal
    balance_amount: Decimal
    payment_schedule: PaymentSchedule
    cancellation_cutoff_at: datetime
    cancelled_at: datetime | None
    cancelled_by: CancelledBy | None
    cancellation_reason: str | None
    payments: list[BookingPaymentResponse]
    # The balance-charge lifecycle, so the customer app can tell a booking
    # whose automatic balance charge failed (attempts > 0 with a last error,
    # back to SCHEDULED for the next retry - FAILED is never persisted) apart
    # from one that's simply waiting for its first attempt.
    # `balance_charge_last_error_code` is Stripe's code from the latest failed
    # BALANCE attempt (e.g. `card_declined`, `authentication_required`).
    balance_charge_state: BalanceChargeState
    balance_charge_due_at: datetime | None
    balance_charge_attempts: int
    balance_charge_last_error: str | None
    balance_charge_last_error_code: str | None
    created_at: datetime


class BookingSummaryResponse(BaseModel):
    id: uuid.UUID
    reference: str
    status: BookingStatus
    braider_id: uuid.UUID
    braider_name: str
    customer_name: str
    style_name: str
    starts_at: datetime
    ends_at: datetime
    total: Decimal
    currency: Currency


class PaginatedBookingsResponse(BaseModel):
    items: list[BookingSummaryResponse]
    page: int
    page_size: int
    total_items: int
    total_pages: int
    has_next: bool
    has_previous: bool


class AdminBookingPaymentResponse(BaseModel):
    id: uuid.UUID
    purpose: PaymentPurpose
    status: PaymentStatus
    amount: Decimal
    amount_refunded: Decimal
    braider_share: Decimal
    is_refunded: bool
    currency: Currency
    stripe_payment_intent_id: str | None
    stripe_charge_id: str | None
    is_off_session: bool
    attempt_number: int
    failure_code: str | None
    failure_message: str | None
    created_at: datetime


class AdminBookingSummaryResponse(BaseModel):
    id: uuid.UUID
    reference: str
    status: BookingStatus
    customer_id: uuid.UUID
    customer_name: str
    customer_email: str
    braider_id: uuid.UUID
    braider_user_id: uuid.UUID | None
    braider_name: str
    braider_email: str | None
    style_id: uuid.UUID
    style_name: str
    starts_at: datetime
    ends_at: datetime
    total: Decimal
    currency: Currency
    country: str
    is_mobile: bool
    payment_schedule: PaymentSchedule
    created_at: datetime


class PaginatedAdminBookingsResponse(BaseModel):
    items: list[AdminBookingSummaryResponse]
    page: int
    page_size: int
    total_items: int
    total_pages: int
    has_next: bool
    has_previous: bool


class AdminBookingStatsActorResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID | None = None
    name: str
    email: str | None = None


class AdminBookingStatsResponse(BaseModel):
    braider: AdminBookingStatsActorResponse | None = None
    customer: AdminBookingStatsActorResponse | None = None
    total_bookings: int
    status_counts: dict[BookingStatus, int]
    completed_bookings: int
    upcoming_bookings: int
    declined_bookings: int
    pending_payment_bookings: int
    no_show_bookings: int
    disputed_bookings: int
    mobile_bookings: int
    salon_bookings: int
    unique_customers: int
    repeat_customers: int
    unique_braiders: int
    repeat_braiders: int
    total_booking_value: Decimal
    average_booking_value: Decimal
    service_subtotal: Decimal
    platform_fee_total: Decimal
    vat_total: Decimal
    total_amount_paid: Decimal
    total_amount_refunded: Decimal
    net_amount_paid: Decimal
    pending_payment_amount: Decimal
    total_amount_made_by_braider: Decimal
    total_amount_spent_by_customer: Decimal


class AdminRevenueChartPoint(BaseModel):
    bucket: datetime
    amount: Decimal
    bookings_count: int


class AdminRevenueChartResponse(BaseModel):
    braider: AdminBookingStatsActorResponse | None = None
    customer: AdminBookingStatsActorResponse | None = None
    interval: str
    metric: str
    currency: Currency
    points: list[AdminRevenueChartPoint]


class AdminBarChartPoint(BaseModel):
    key: str
    label: str
    bookings_count: int
    amount: Decimal


class AdminBarChartResponse(BaseModel):
    braider: AdminBookingStatsActorResponse | None = None
    customer: AdminBookingStatsActorResponse | None = None
    metric: str
    currency: Currency
    points: list[AdminBarChartPoint]


class AdminStyleChartSlice(BaseModel):
    style_id: uuid.UUID | None
    style_name: str
    bookings_count: int
    amount: Decimal
    share: Decimal


class AdminStylePieChartResponse(BaseModel):
    braider: AdminBookingStatsActorResponse | None = None
    customer: AdminBookingStatsActorResponse | None = None
    metric: str
    currency: Currency
    total_amount: Decimal
    slices: list[AdminStyleChartSlice]


class AdminPlatformFinancialsResponse(BaseModel):
    currency: Currency
    total_bookings: int
    completed_bookings: int
    total_booking_value: Decimal
    average_booking_value: Decimal
    service_subtotal: Decimal
    platform_fee_total: Decimal
    vat_total: Decimal
    total_amount_paid: Decimal
    total_amount_refunded: Decimal
    net_amount_paid: Decimal
    pending_payment_amount: Decimal
    braider_earnings: Decimal
    gross_margin_before_tax: Decimal
    estimated_profit_after_vat: Decimal


class AdminBookingResponse(AdminBookingSummaryResponse):
    duration_minutes: int
    client_address: str | None
    client_latitude: Decimal | None
    client_longitude: Decimal | None
    service_subtotal: Decimal
    travel_fee: Decimal
    subtotal: Decimal
    platform_fee: Decimal
    vat_on_service: Decimal
    vat_on_platform_fee: Decimal
    vat_total: Decimal
    deposit_amount: Decimal
    balance_amount: Decimal
    braider_share_total: Decimal
    braider_share_deposit: Decimal
    braider_share_balance: Decimal
    cancellation_cutoff_at: datetime
    hold_expires_at: datetime | None
    balance_charge_due_at: datetime | None
    balance_charge_state: BalanceChargeState
    confirmed_at: datetime | None
    cancelled_at: datetime | None
    cancelled_by: CancelledBy | None
    items: list[BookingItemResponse]
    payments: list[AdminBookingPaymentResponse]
    updated_at: datetime
