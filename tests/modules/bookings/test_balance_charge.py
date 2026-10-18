import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.bookings import repository as bookings_repo
from app.modules.bookings.enums import BalanceChargeState, BookingStatus, PaymentPurpose, PaymentStatus
from app.modules.bookings.models import Booking, BookingPayment
from app.modules.bookings.payments import client as payments_client
from app.modules.bookings.payments.client import PaymentIntentResult, StripeApiError, StripeCardError
from app.modules.bookings.tasks import (
    TASK_CHARGE_BOOKING_BALANCE,
    TASK_SEND_BOOKING_CANCELLED_NO_PAYMENT_EMAIL,
    TASK_SEND_BALANCE_PAYMENT_FAILED_EMAIL,
    TASK_SEND_PAYMENT_NOTIFICATION,
    TASK_SEND_PAYMENT_RECEIPT_EMAIL,
    charge_booking_balance_task,
    send_balance_payment_failed_email_task,
    send_booking_cancelled_no_payment_email_task,
)
from app.modules.bookings import cron as bookings_cron
from app.modules.users.models import UserType
from tests.helpers import create_user_with_token
from tests.modules.bookings.helpers import create_bookable_braider

pytestmark = pytest.mark.asyncio

CALC_URL = "/api/v1/booking-calculations"
BOOKINGS_URL = "/api/v1/bookings"


async def _create_deposit_booking(
    client: AsyncClient, db_session: AsyncSession
) -> tuple[uuid.UUID, str]:
    """Books far enough out to get DEPOSIT_THEN_BALANCE, then simulates the
    deposit's webhook confirmation directly against the DB (bypassing
    Stripe) so the booking lands CONFIRMED with balance_charge_state
    SCHEDULED - the state every balance-charge test starts from. Returns
    (booking_id, customer_access_token)."""
    braider = await create_bookable_braider(db_session, base_price="180.00", country="AT")
    _, token = await create_user_with_token(db_session, user_type=UserType.CUSTOMER)
    headers = {"Authorization": f"Bearer {token}"}

    calc_resp = await client.post(
        CALC_URL, json={"braider_id": str(braider["braider_id"]), "style_id": str(braider["style_id"])}
    )
    calc = calc_resp.json()["data"]
    starts_at = datetime.now(UTC) + timedelta(days=60)

    resp = await client.post(
        BOOKINGS_URL,
        json={
            "booking_calculation_id": calc["id"],
            "starts_at": starts_at.isoformat(),
            "terms_accepted": True,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    booking_id = uuid.UUID(resp.json()["data"]["id"])

    booking = await db_session.get(Booking, booking_id)
    payment_result = await db_session.execute(
        select(BookingPayment).where(BookingPayment.booking_id == booking_id)
    )
    payment = payment_result.scalars().one()
    payment.status = PaymentStatus.SUCCEEDED
    booking.status = BookingStatus.CONFIRMED
    booking.confirmed_at = datetime.now(UTC)
    booking.stripe_payment_method_id = f"pm_test_{uuid.uuid4().hex[:16]}"
    await db_session.commit()

    return booking_id, token


async def _make_due(db_session: AsyncSession, booking_id: uuid.UUID) -> None:
    booking = await db_session.get(Booking, booking_id)
    booking.balance_charge_due_at = datetime.now(UTC) - timedelta(minutes=5)
    await db_session.commit()


async def test_sweep_claims_due_balance_charges(
    client: AsyncClient, db_session: AsyncSession, fake_queue
):
    booking_id, _token = await _create_deposit_booking(client, db_session)
    await _make_due(db_session, booking_id)

    await bookings_cron.sweep_balance_charges_cron({"redis": fake_queue})

    job_kwargs = fake_queue.last_job_kwargs(TASK_CHARGE_BOOKING_BALANCE)
    assert job_kwargs["booking_id"] == str(booking_id)

    booking = await db_session.get(Booking, booking_id)
    await db_session.refresh(booking)
    assert booking.balance_charge_state == BalanceChargeState.DUE


async def test_sweep_ignores_not_yet_due_bookings(
    client: AsyncClient, db_session: AsyncSession, fake_queue
):
    booking_id, _token = await _create_deposit_booking(client, db_session)

    await bookings_cron.sweep_balance_charges_cron({"redis": fake_queue})

    assert fake_queue.jobs == []
    booking = await db_session.get(Booking, booking_id)
    await db_session.refresh(booking)
    assert booking.balance_charge_state == BalanceChargeState.SCHEDULED


async def test_charge_booking_balance_task_succeeds(
    client: AsyncClient, db_session: AsyncSession, fake_queue, monkeypatch
):
    booking_id, _token = await _create_deposit_booking(client, db_session)
    await _make_due(db_session, booking_id)

    claimed = await bookings_repo.claim_due_balance_charges(db_session, limit=10)
    await db_session.commit()
    assert booking_id in claimed

    fake_pi_id = f"pi_test_{uuid.uuid4().hex[:16]}"

    async def fake_charge_off_session(**kwargs):
        return PaymentIntentResult(id=fake_pi_id, client_secret=f"{fake_pi_id}_secret")

    monkeypatch.setattr(payments_client, "charge_off_session", fake_charge_off_session)

    await charge_booking_balance_task({"redis": fake_queue}, booking_id=str(booking_id))

    booking = await db_session.get(Booking, booking_id)
    await db_session.refresh(booking)
    assert booking.balance_charge_state == BalanceChargeState.SUCCEEDED
    assert booking.balance_charge_attempts == 1

    result = await db_session.execute(
        select(BookingPayment).where(
            BookingPayment.booking_id == booking_id, BookingPayment.purpose == PaymentPurpose.BALANCE
        )
    )
    balance_payment = result.scalars().one()
    assert balance_payment.status == PaymentStatus.SUCCEEDED
    assert balance_payment.stripe_payment_intent_id == fake_pi_id

    fake_queue.last_job_kwargs(TASK_SEND_PAYMENT_RECEIPT_EMAIL)
    fake_queue.last_job_kwargs(TASK_SEND_PAYMENT_NOTIFICATION)


async def test_charge_booking_balance_task_schedules_retry_on_decline(
    client: AsyncClient, db_session: AsyncSession, fake_queue, monkeypatch
):
    booking_id, _token = await _create_deposit_booking(client, db_session)
    await _make_due(db_session, booking_id)
    await bookings_repo.claim_due_balance_charges(db_session, limit=10)
    await db_session.commit()

    async def fake_charge_off_session(**kwargs):
        raise StripeCardError(code="card_declined", message="Your card was declined.")

    monkeypatch.setattr(payments_client, "charge_off_session", fake_charge_off_session)

    await charge_booking_balance_task({"redis": fake_queue}, booking_id=str(booking_id))

    booking = await db_session.get(Booking, booking_id)
    await db_session.refresh(booking)
    assert booking.status == BookingStatus.CONFIRMED
    assert booking.balance_charge_state == BalanceChargeState.SCHEDULED
    assert booking.balance_charge_attempts == 1
    assert booking.balance_charge_due_at > datetime.now(UTC) + timedelta(hours=1)

    result = await db_session.execute(
        select(BookingPayment).where(
            BookingPayment.booking_id == booking_id, BookingPayment.purpose == PaymentPurpose.BALANCE
        )
    )
    balance_payment = result.scalars().one()
    assert balance_payment.status == PaymentStatus.FAILED
    assert balance_payment.failure_code == "card_declined"

    job_kwargs = fake_queue.last_job_kwargs(TASK_SEND_BALANCE_PAYMENT_FAILED_EMAIL)
    assert job_kwargs["booking_id"] == str(booking_id)
    assert job_kwargs["failure_code"] == "card_declined"


async def test_charge_booking_balance_task_cancels_after_ladder_exhausted(
    client: AsyncClient, db_session: AsyncSession, fake_queue, monkeypatch
):
    booking_id, _token = await _create_deposit_booking(client, db_session)

    async def fake_charge_off_session(**kwargs):
        raise StripeCardError(code="card_declined", message="Your card was declined.")

    monkeypatch.setattr(payments_client, "charge_off_session", fake_charge_off_session)

    # Pre-exhaust the ladder: 3 prior failed attempts already recorded.
    booking = await db_session.get(Booking, booking_id)
    booking.balance_charge_attempts = 3
    booking.balance_charge_due_at = datetime.now(UTC) - timedelta(minutes=5)
    await db_session.commit()

    await bookings_repo.claim_due_balance_charges(db_session, limit=10)
    await db_session.commit()

    await charge_booking_balance_task({"redis": fake_queue}, booking_id=str(booking_id))

    booking = await db_session.get(Booking, booking_id)
    await db_session.refresh(booking)
    assert booking.status == BookingStatus.CANCELLED_NO_PAYMENT
    assert booking.balance_charge_state == BalanceChargeState.ABANDONED
    assert booking.cancelled_at is not None

    fake_queue.last_job_kwargs(TASK_SEND_BOOKING_CANCELLED_NO_PAYMENT_EMAIL)


async def test_charge_booking_balance_task_noop_when_not_due(
    client: AsyncClient, db_session: AsyncSession, fake_queue, monkeypatch
):
    """No FOR UPDATE claim happened (state is still SCHEDULED, not DUE) -
    e.g. a stray/duplicate task delivery - must be a safe no-op."""
    booking_id, _token = await _create_deposit_booking(client, db_session)

    async def fail_if_called(**kwargs):
        raise AssertionError("Stripe should not be called for a non-DUE booking")

    monkeypatch.setattr(payments_client, "charge_off_session", fail_if_called)

    await charge_booking_balance_task({"redis": fake_queue}, booking_id=str(booking_id))

    booking = await db_session.get(Booking, booking_id)
    await db_session.refresh(booking)
    assert booking.balance_charge_state == BalanceChargeState.SCHEDULED
    assert fake_queue.jobs == []


async def test_send_balance_payment_failed_email_task_runs(
    client: AsyncClient, db_session: AsyncSession, fake_queue
):
    booking_id, _token = await _create_deposit_booking(client, db_session)
    await send_balance_payment_failed_email_task(
        {"redis": fake_queue},
        booking_id=str(booking_id),
        failure_code="authentication_required",
        failure_message="Your bank requires you to authenticate this payment.",
    )
    # No assertion error means the email + notification pipeline ran clean.


async def test_send_booking_cancelled_no_payment_email_task_runs(
    client: AsyncClient, db_session: AsyncSession, fake_queue
):
    booking_id, _token = await _create_deposit_booking(client, db_session)
    await send_booking_cancelled_no_payment_email_task({"redis": fake_queue}, booking_id=str(booking_id))


async def test_resume_payment_creates_on_session_intent(
    client: AsyncClient, db_session: AsyncSession
):
    booking_id, token = await _create_deposit_booking(client, db_session)
    booking = await db_session.get(Booking, booking_id)

    resp = await client.post(
        f"{BOOKINGS_URL}/{booking_id}/pay", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    balance_payment = next(p for p in data["payments"] if p["purpose"] == "BALANCE")
    assert balance_payment["client_secret"] is not None

    await db_session.refresh(booking)
    assert booking.balance_charge_state == BalanceChargeState.IN_PROGRESS
    assert booking.balance_charge_attempts == 1


async def test_booking_response_exposes_failed_balance_charge(
    client: AsyncClient, db_session: AsyncSession, fake_queue, monkeypatch
):
    """The customer app's failed-payment banner keys off these fields - a
    declined automatic charge goes back to SCHEDULED (FAILED is never
    persisted), so attempts/last_error/last_error_code are what mark it."""
    booking_id, token = await _create_deposit_booking(client, db_session)
    await _make_due(db_session, booking_id)
    await bookings_repo.claim_due_balance_charges(db_session, limit=10)
    await db_session.commit()

    async def fake_charge_off_session(**kwargs):
        raise StripeCardError(code="authentication_required", message="Your bank requires authentication.")

    monkeypatch.setattr(payments_client, "charge_off_session", fake_charge_off_session)
    await charge_booking_balance_task({"redis": fake_queue}, booking_id=str(booking_id))

    resp = await client.get(f"{BOOKINGS_URL}/{booking_id}", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["balance_charge_state"] == "SCHEDULED"
    assert data["balance_charge_attempts"] == 1
    assert data["balance_charge_last_error"] == "Your bank requires authentication."
    assert data["balance_charge_last_error_code"] == "authentication_required"
    assert data["balance_charge_due_at"] is not None


class _FakeIntent:
    def __init__(self, status: str, client_secret: str = "pi_reuse_secret") -> None:
        self.status = status
        self.client_secret = client_secret


async def test_resume_payment_reuses_declined_on_session_intent(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """Regression: a declined/abandoned on-session attempt left the booking
    IN_PROGRESS and every later POST /pay 409'd, so the customer could never
    retry with another card."""
    booking_id, token = await _create_deposit_booking(client, db_session)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post(f"{BOOKINGS_URL}/{booking_id}/pay", headers=headers)
    assert resp.status_code == 200, resp.text

    # The decline's webhook marked the on-session payment FAILED.
    result = await db_session.execute(
        select(BookingPayment).where(
            BookingPayment.booking_id == booking_id, BookingPayment.purpose == PaymentPurpose.BALANCE
        )
    )
    balance_payment = result.scalars().one()
    balance_payment.status = PaymentStatus.FAILED
    balance_payment.failure_code = "card_declined"
    await db_session.commit()

    async def fake_retrieve(pi_id):
        assert pi_id == balance_payment.stripe_payment_intent_id
        return _FakeIntent("requires_payment_method")

    monkeypatch.setattr(payments_client, "retrieve_payment_intent", fake_retrieve)

    resp = await client.post(f"{BOOKINGS_URL}/{booking_id}/pay", headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    balance = next(p for p in data["payments"] if p["purpose"] == "BALANCE")
    assert balance["client_secret"] == "pi_reuse_secret"
    assert balance["status"] == "PENDING"

    booking = await db_session.get(Booking, booking_id)
    await db_session.refresh(booking)
    assert booking.balance_charge_state == BalanceChargeState.IN_PROGRESS
    assert booking.balance_charge_attempts == 1
    result = await db_session.execute(
        select(BookingPayment).where(
            BookingPayment.booking_id == booking_id, BookingPayment.purpose == PaymentPurpose.BALANCE
        )
    )
    assert len(result.scalars().all()) == 1


async def test_resume_payment_replaces_canceled_on_session_intent(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    booking_id, token = await _create_deposit_booking(client, db_session)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post(f"{BOOKINGS_URL}/{booking_id}/pay", headers=headers)
    assert resp.status_code == 200, resp.text

    async def fake_retrieve(pi_id):
        return _FakeIntent("canceled")

    monkeypatch.setattr(payments_client, "retrieve_payment_intent", fake_retrieve)

    resp = await client.post(f"{BOOKINGS_URL}/{booking_id}/pay", headers=headers)
    assert resp.status_code == 200, resp.text

    result = await db_session.execute(
        select(BookingPayment)
        .where(BookingPayment.booking_id == booking_id, BookingPayment.purpose == PaymentPurpose.BALANCE)
        .order_by(BookingPayment.attempt_number)
    )
    first, second = result.scalars().all()
    assert first.status == PaymentStatus.CANCELED
    assert second.status == PaymentStatus.PENDING
    assert second.attempt_number == 2


async def test_resume_payment_rejects_while_off_session_charge_in_flight(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    booking_id, token = await _create_deposit_booking(client, db_session)
    booking = await db_session.get(Booking, booking_id)
    booking.balance_charge_state = BalanceChargeState.IN_PROGRESS
    await db_session.commit()

    async def fail_if_called(pi_id):
        raise AssertionError("no on-session intent to look up")

    monkeypatch.setattr(payments_client, "retrieve_payment_intent", fail_if_called)

    resp = await client.post(f"{BOOKINGS_URL}/{booking_id}/pay", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "BOOKING_PAYMENT_NOT_RESUMABLE"


async def test_resume_payment_rejects_already_paid_balance(
    client: AsyncClient, db_session: AsyncSession
):
    booking_id, token = await _create_deposit_booking(client, db_session)
    booking = await db_session.get(Booking, booking_id)
    booking.balance_charge_state = BalanceChargeState.SUCCEEDED
    await db_session.commit()

    resp = await client.post(f"{BOOKINGS_URL}/{booking_id}/pay", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "BOOKING_PAYMENT_NOT_RESUMABLE"


async def test_resume_payment_rejects_full_upfront_booking(
    client: AsyncClient, db_session: AsyncSession
):
    braider = await create_bookable_braider(db_session, base_price="180.00", country="AT")
    _, token = await create_user_with_token(db_session, user_type=UserType.CUSTOMER)
    headers = {"Authorization": f"Bearer {token}"}
    calc_resp = await client.post(
        CALC_URL, json={"braider_id": str(braider["braider_id"]), "style_id": str(braider["style_id"])}
    )
    calc = calc_resp.json()["data"]
    starts_at = datetime.now(UTC) + timedelta(hours=10)
    resp = await client.post(
        BOOKINGS_URL,
        json={
            "booking_calculation_id": calc["id"],
            "starts_at": starts_at.isoformat(),
            "terms_accepted": True,
        },
        headers=headers,
    )
    booking_id = resp.json()["data"]["id"]

    resp = await client.post(f"{BOOKINGS_URL}/{booking_id}/pay", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "BOOKING_PAYMENT_NOT_RESUMABLE"


@pytest.mark.parametrize("pm_type", ["card", "paypal"])
async def test_off_session_charge_uses_saved_payment_method_type(monkeypatch, pm_type):
    """Regression: the balance charge used to hardcode payment_method_types=["card"],
    so a deposit paid (and saved) via PayPal could never be charged off-session."""
    captured: dict = {}

    class FakePaymentMethod:
        type = pm_type

    class FakeIntent:
        id = "pi_fake"
        client_secret = "pi_fake_secret"
        latest_charge = "ch_fake"

    def fake_create(**kwargs):
        captured.update(kwargs)
        return FakeIntent()

    monkeypatch.setattr(payments_client.stripe.PaymentMethod, "retrieve", lambda pm_id: FakePaymentMethod())
    monkeypatch.setattr(payments_client.stripe.PaymentIntent, "create", fake_create)

    result = payments_client._charge_off_session_sync(
        amount_minor=10336,
        currency="EUR",
        customer_id="cus_fake",
        payment_method_id="pm_fake",
        metadata={},
        idempotency_key="k",
    )

    assert captured["payment_method_types"] == [pm_type]
    assert captured["off_session"] is True
    assert result.charge_id == "ch_fake"


async def _start_abandoned_on_session_payment(
    client: AsyncClient, db_session: AsyncSession, *, declined: bool = False, idle_minutes: int = 120
) -> tuple[uuid.UUID, BookingPayment, str]:
    """POST /pay, then age the on-session BALANCE payment past the abandon
    threshold (optionally after a decline webhook marked it FAILED)."""
    booking_id, token = await _create_deposit_booking(client, db_session)
    resp = await client.post(f"{BOOKINGS_URL}/{booking_id}/pay", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text

    result = await db_session.execute(
        select(BookingPayment).where(
            BookingPayment.booking_id == booking_id, BookingPayment.purpose == PaymentPurpose.BALANCE
        )
    )
    payment = result.scalars().one()
    if declined:
        payment.status = PaymentStatus.FAILED
        payment.failure_code = "card_declined"
        payment.failure_message = "Your card was declined."
    payment.updated_at = datetime.now(UTC) - timedelta(minutes=idle_minutes)
    await db_session.commit()
    return booking_id, payment, token


def _patch_intent(monkeypatch, status: str, *, cancel_error: bool = False) -> list[str]:
    canceled: list[str] = []

    async def fake_retrieve(pi_id):
        return _FakeIntent(status)

    async def fake_cancel(pi_id):
        if cancel_error:
            raise StripeApiError("This PaymentIntent could not be canceled because it has a status of succeeded.")
        canceled.append(pi_id)
        return _FakeIntent("canceled")

    monkeypatch.setattr(payments_client, "retrieve_payment_intent", fake_retrieve)
    monkeypatch.setattr(payments_client, "cancel_payment_intent", fake_cancel)
    return canceled


async def test_release_abandoned_early_payment_keeps_original_schedule(
    client: AsyncClient, db_session: AsyncSession, fake_queue, monkeypatch
):
    """Customer chose to pay early, then walked away: the intent is canceled
    and the automatic charge goes ahead on its original date, no email."""
    booking_id, payment, _token = await _start_abandoned_on_session_payment(client, db_session)
    booking = await db_session.get(Booking, booking_id)
    original_due_at = booking.balance_charge_due_at
    canceled = _patch_intent(monkeypatch, "requires_payment_method")

    await bookings_cron.release_abandoned_balance_payments_cron({"redis": fake_queue})

    assert canceled == [payment.stripe_payment_intent_id]
    await db_session.refresh(booking)
    await db_session.refresh(payment)
    assert booking.status == BookingStatus.CONFIRMED
    assert booking.balance_charge_state == BalanceChargeState.SCHEDULED
    assert booking.balance_charge_due_at == original_due_at
    assert payment.status == PaymentStatus.CANCELED
    assert fake_queue.jobs == []


async def test_release_declined_payment_schedules_next_retry(
    client: AsyncClient, db_session: AsyncSession, fake_queue, monkeypatch
):
    booking_id, payment, _token = await _start_abandoned_on_session_payment(client, db_session, declined=True)
    booking = await db_session.get(Booking, booking_id)
    booking.balance_charge_due_at = datetime.now(UTC) - timedelta(hours=3)
    await db_session.commit()
    _patch_intent(monkeypatch, "requires_payment_method")

    await bookings_cron.release_abandoned_balance_payments_cron({"redis": fake_queue})

    await db_session.refresh(booking)
    await db_session.refresh(payment)
    assert booking.balance_charge_state == BalanceChargeState.SCHEDULED
    assert booking.balance_charge_due_at > datetime.now(UTC) + timedelta(hours=1)
    assert booking.balance_charge_last_error == "Your card was declined."
    assert payment.status == PaymentStatus.FAILED  # keeps the decline code for the banner
    job_kwargs = fake_queue.last_job_kwargs(TASK_SEND_BALANCE_PAYMENT_FAILED_EMAIL)
    assert job_kwargs["failure_code"] == "card_declined"

    # Now SCHEDULED again, so the regular sweeper picks it up once due.
    booking.balance_charge_due_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()
    await bookings_cron.sweep_balance_charges_cron({"redis": fake_queue})
    assert fake_queue.last_job_kwargs(TASK_CHARGE_BOOKING_BALANCE)["booking_id"] == str(booking_id)


async def test_release_abandoned_payment_cancels_booking_when_ladder_exhausted(
    client: AsyncClient, db_session: AsyncSession, fake_queue, monkeypatch
):
    booking_id, payment, _token = await _start_abandoned_on_session_payment(client, db_session)
    booking = await db_session.get(Booking, booking_id)
    booking.balance_charge_due_at = datetime.now(UTC) - timedelta(hours=3)
    payment.attempt_number = 4  # past the 3-step ladder
    payment.updated_at = datetime.now(UTC) - timedelta(hours=2)  # this write would otherwise reset it
    await db_session.commit()
    _patch_intent(monkeypatch, "requires_action")

    await bookings_cron.release_abandoned_balance_payments_cron({"redis": fake_queue})

    await db_session.refresh(booking)
    assert booking.status == BookingStatus.CANCELLED_NO_PAYMENT
    assert booking.balance_charge_state == BalanceChargeState.ABANDONED
    assert booking.cancelled_at is not None
    fake_queue.last_job_kwargs(TASK_SEND_BOOKING_CANCELLED_NO_PAYMENT_EMAIL)


async def test_release_skips_recently_active_payment(
    client: AsyncClient, db_session: AsyncSession, fake_queue, monkeypatch
):
    booking_id, _payment, _token = await _start_abandoned_on_session_payment(client, db_session, idle_minutes=5)
    canceled = _patch_intent(monkeypatch, "requires_payment_method")

    await bookings_cron.release_abandoned_balance_payments_cron({"redis": fake_queue})

    assert canceled == []
    booking = await db_session.get(Booking, booking_id)
    await db_session.refresh(booking)
    assert booking.balance_charge_state == BalanceChargeState.IN_PROGRESS


@pytest.mark.parametrize("intent_status", ["processing", "succeeded"])
async def test_release_never_touches_settling_intent(
    client: AsyncClient, db_session: AsyncSession, fake_queue, monkeypatch, intent_status
):
    booking_id, _payment, _token = await _start_abandoned_on_session_payment(client, db_session)
    canceled = _patch_intent(monkeypatch, intent_status)

    await bookings_cron.release_abandoned_balance_payments_cron({"redis": fake_queue})

    assert canceled == []
    booking = await db_session.get(Booking, booking_id)
    await db_session.refresh(booking)
    assert booking.balance_charge_state == BalanceChargeState.IN_PROGRESS
    assert fake_queue.jobs == []


async def test_release_leaves_booking_when_cancel_races_a_confirmation(
    client: AsyncClient, db_session: AsyncSession, fake_queue, monkeypatch
):
    """Stripe refuses the cancel because the customer confirmed between our
    retrieve and cancel - nothing must change (the webhook settles it)."""
    booking_id, payment, _token = await _start_abandoned_on_session_payment(client, db_session)
    _patch_intent(monkeypatch, "requires_action", cancel_error=True)

    await bookings_cron.release_abandoned_balance_payments_cron({"redis": fake_queue})

    booking = await db_session.get(Booking, booking_id)
    await db_session.refresh(booking)
    await db_session.refresh(payment)
    assert booking.balance_charge_state == BalanceChargeState.IN_PROGRESS
    assert payment.status == PaymentStatus.PENDING


async def test_release_ignores_off_session_charge_in_flight(
    client: AsyncClient, db_session: AsyncSession, fake_queue, monkeypatch
):
    booking_id, _token = await _create_deposit_booking(client, db_session)
    booking = await db_session.get(Booking, booking_id)
    booking.balance_charge_state = BalanceChargeState.IN_PROGRESS
    await db_session.commit()

    async def fail_if_called(pi_id):
        raise AssertionError("an off-session charge must not be looked up or canceled")

    monkeypatch.setattr(payments_client, "retrieve_payment_intent", fail_if_called)

    await bookings_cron.release_abandoned_balance_payments_cron({"redis": fake_queue})

    await db_session.refresh(booking)
    assert booking.balance_charge_state == BalanceChargeState.IN_PROGRESS


async def test_resume_payment_restarts_abandon_clock(
    client: AsyncClient, db_session: AsyncSession, fake_queue, monkeypatch
):
    booking_id, _payment, token = await _start_abandoned_on_session_payment(client, db_session)
    canceled = _patch_intent(monkeypatch, "requires_payment_method")

    # The customer comes back and resumes just before the cron runs.
    resp = await client.post(f"{BOOKINGS_URL}/{booking_id}/pay", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text

    await bookings_cron.release_abandoned_balance_payments_cron({"redis": fake_queue})

    assert canceled == []
    booking = await db_session.get(Booking, booking_id)
    await db_session.refresh(booking)
    assert booking.balance_charge_state == BalanceChargeState.IN_PROGRESS
