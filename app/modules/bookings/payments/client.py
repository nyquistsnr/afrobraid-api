"""Raw Stripe SDK wrapper for the platform account's payment flow (customers
and PaymentIntents) - separate from `braiders/payment_setup/client.py`,
which talks to Connect accounts.

Short-circuits entirely in the test environment and returns deterministic
fakes, exactly as `shared/translation/client.py` does - this is what lets
every booking test run without mocking Stripe (see the plan's verification
section: "the highest-leverage test decision").
"""

import asyncio
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import stripe

from app.core.config import get_settings

settings = get_settings()
stripe.api_key = settings.stripe_secret_key
# Pinned so Stripe can't reshape request/response/webhook payloads under this
# app on their own release schedule (design correction #8).
stripe.api_version = settings.stripe_api_version

_IS_TEST_ENV = settings.environment == "test"


class StripeApiError(Exception):
    pass


class StripeWebhookSignatureError(Exception):
    pass


class StripeCardError(Exception):
    """An off-session charge's CardError - raised synchronously by
    `charge_off_session` (design correction #13: off-session failure is not
    a webhook-driven state, Stripe returns/raises it inline). `code` is
    Stripe's decline/error code (e.g. `card_declined`, `expired_card`,
    `authentication_required`) and is what the balance-charge retry ladder
    branches on."""

    def __init__(self, *, code: str | None, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class PaymentIntentResult:
    id: str
    client_secret: str
    # Only populated by charge_off_session, whose confirm=True call resolves
    # synchronously - create_payment_intent's on-session flow only learns
    # the charge id later, from the payment_intent.succeeded webhook. It's
    # what a later Transfer.create's source_transaction needs.
    charge_id: str | None = None


@dataclass(frozen=True)
class RefundResult:
    id: str
    status: str


@dataclass(frozen=True)
class TransferResult:
    id: str


@dataclass(frozen=True)
class ReversalResult:
    id: str


def _create_customer_sync(*, email: str, name: str) -> str:
    customer = stripe.Customer.create(email=email, name=name)
    return customer.id


def _create_payment_intent_sync(
    *,
    amount_minor: int,
    currency: str,
    customer_id: str,
    metadata: dict[str, str],
    off_session_setup: bool,
) -> PaymentIntentResult:
    # Deliberately no `payment_method_types` here - omitting it turns on
    # Stripe's dynamic payment methods, which shows/ranks whichever methods
    # are enabled in the Dashboard (Settings > Payment methods) for this
    # customer's currency/location/amount, e.g. PayPal for EU-based
    # customers, with no backend changes needed to add or remove one.
    kwargs: dict[str, Any] = {
        "amount": amount_minor,
        "currency": currency.lower(),
        "customer": customer_id,
        "metadata": metadata,
    }
    if off_session_setup:
        # Persists the payment method for a later off-session charge (the
        # balance, months out) - the mandate disclosure this requires is a
        # network requirement, shown client-side before confirmation (design
        # correction #13), not something this backend call can express.
        kwargs["setup_future_usage"] = "off_session"
    intent = stripe.PaymentIntent.create(**kwargs)
    return PaymentIntentResult(id=intent.id, client_secret=intent.client_secret)


async def create_customer(*, email: str, name: str) -> str:
    if _IS_TEST_ENV:
        return f"cus_test_{uuid.uuid4().hex[:24]}"
    try:
        return await asyncio.to_thread(_create_customer_sync, email=email, name=name)
    except stripe.error.StripeError as exc:
        raise StripeApiError(f"Stripe customer creation failed: {exc}") from exc


async def create_payment_intent(
    *,
    amount_minor: int,
    currency: str,
    customer_id: str,
    metadata: dict[str, str],
    off_session_setup: bool = False,
) -> PaymentIntentResult:
    if _IS_TEST_ENV:
        pi_id = f"pi_test_{uuid.uuid4().hex[:24]}"
        return PaymentIntentResult(id=pi_id, client_secret=f"{pi_id}_secret_test")
    try:
        return await asyncio.to_thread(
            _create_payment_intent_sync,
            amount_minor=amount_minor,
            currency=currency,
            customer_id=customer_id,
            metadata=metadata,
            off_session_setup=off_session_setup,
        )
    except stripe.error.StripeError as exc:
        raise StripeApiError(f"Stripe payment intent creation failed: {exc}") from exc


def _charge_off_session_sync(
    *,
    amount_minor: int,
    currency: str,
    customer_id: str,
    payment_method_id: str,
    metadata: dict[str, str],
    idempotency_key: str,
) -> PaymentIntentResult:
    # Pins payment_method_types to the saved method's own type rather than
    # leaving dynamic payment methods on (which would need a return_url for
    # redirect-capable methods) or hardcoding ["card"] (which rejects a
    # PayPal/Link/SEPA method the deposit legitimately saved). Whatever the
    # deposit saved under setup_future_usage="off_session" is reusable by
    # construction - Stripe only offers reusable methods with that flag.
    payment_method = stripe.PaymentMethod.retrieve(payment_method_id)
    try:
        intent = stripe.PaymentIntent.create(
            amount=amount_minor,
            currency=currency.lower(),
            customer=customer_id,
            payment_method=payment_method_id,
            payment_method_types=[payment_method.type],
            off_session=True,
            confirm=True,
            metadata=metadata,
            idempotency_key=idempotency_key,
        )
    except stripe.error.CardError as exc:
        error = getattr(exc, "error", None)
        raise StripeCardError(
            code=getattr(error, "code", None), message=exc.user_message or str(exc)
        ) from exc
    return PaymentIntentResult(
        id=intent.id, client_secret=intent.client_secret, charge_id=getattr(intent, "latest_charge", None)
    )


async def charge_off_session(
    *,
    amount_minor: int,
    currency: str,
    customer_id: str,
    payment_method_id: str,
    metadata: dict[str, str],
    idempotency_key: str,
) -> PaymentIntentResult:
    if _IS_TEST_ENV:
        pi_id = f"pi_test_{uuid.uuid4().hex[:24]}"
        return PaymentIntentResult(
            id=pi_id, client_secret=f"{pi_id}_secret_test", charge_id=f"ch_test_{uuid.uuid4().hex[:16]}"
        )
    try:
        return await asyncio.to_thread(
            _charge_off_session_sync,
            amount_minor=amount_minor,
            currency=currency,
            customer_id=customer_id,
            payment_method_id=payment_method_id,
            metadata=metadata,
            idempotency_key=idempotency_key,
        )
    except StripeCardError:
        raise
    except stripe.error.StripeError as exc:
        raise StripeApiError(f"Stripe off-session charge failed: {exc}") from exc


def _create_refund_sync(
    *, payment_intent_id: str, amount_minor: int, metadata: dict[str, str], idempotency_key: str
) -> RefundResult:
    refund = stripe.Refund.create(
        payment_intent=payment_intent_id,
        amount=amount_minor,
        metadata=metadata,
        idempotency_key=idempotency_key,
    )
    return RefundResult(id=refund.id, status=refund.status)


async def create_refund(
    *, payment_intent_id: str, amount_minor: int, metadata: dict[str, str], idempotency_key: str
) -> RefundResult:
    """Refunds are keyed by payment_intent (not charge) - Stripe resolves
    the underlying charge itself, so this needs no `stripe_charge_id` on
    our side, unlike create_transfer below."""
    if _IS_TEST_ENV:
        return RefundResult(id=f"re_test_{uuid.uuid4().hex[:24]}", status="succeeded")
    try:
        return await asyncio.to_thread(
            _create_refund_sync,
            payment_intent_id=payment_intent_id,
            amount_minor=amount_minor,
            metadata=metadata,
            idempotency_key=idempotency_key,
        )
    except stripe.error.StripeError as exc:
        raise StripeApiError(f"Stripe refund failed: {exc}") from exc


def _create_transfer_sync(
    *,
    amount_minor: int,
    currency: str,
    destination_account_id: str,
    source_charge_id: str,
    transfer_group: str,
    metadata: dict[str, str],
    idempotency_key: str,
) -> TransferResult:
    transfer = stripe.Transfer.create(
        amount=amount_minor,
        currency=currency.lower(),
        destination=destination_account_id,
        # Ties the transfer to a specific already-captured charge - funds
        # are available immediately, no negative-balance risk on the
        # connected account (plan's Stripe sequences section).
        source_transaction=source_charge_id,
        transfer_group=transfer_group,
        metadata=metadata,
        idempotency_key=idempotency_key,
    )
    return TransferResult(id=transfer.id)


async def create_transfer(
    *,
    amount_minor: int,
    currency: str,
    destination_account_id: str,
    source_charge_id: str,
    transfer_group: str,
    metadata: dict[str, str],
    idempotency_key: str,
) -> TransferResult:
    if _IS_TEST_ENV:
        return TransferResult(id=f"tr_test_{uuid.uuid4().hex[:24]}")
    try:
        return await asyncio.to_thread(
            _create_transfer_sync,
            amount_minor=amount_minor,
            currency=currency,
            destination_account_id=destination_account_id,
            source_charge_id=source_charge_id,
            transfer_group=transfer_group,
            metadata=metadata,
            idempotency_key=idempotency_key,
        )
    except stripe.error.StripeError as exc:
        raise StripeApiError(f"Stripe transfer failed: {exc}") from exc


def _reverse_transfer_sync(*, transfer_id: str, idempotency_key: str) -> ReversalResult:
    reversal = stripe.Transfer.create_reversal(transfer_id, idempotency_key=idempotency_key)
    return ReversalResult(id=reversal.id)


async def reverse_transfer(*, transfer_id: str, idempotency_key: str) -> ReversalResult:
    if _IS_TEST_ENV:
        return ReversalResult(id=f"trr_test_{uuid.uuid4().hex[:24]}")
    try:
        return await asyncio.to_thread(
            _reverse_transfer_sync, transfer_id=transfer_id, idempotency_key=idempotency_key
        )
    except stripe.error.StripeError as exc:
        raise StripeApiError(f"Stripe transfer reversal failed: {exc}") from exc


def _retrieve_payment_intent_sync(payment_intent_id: str) -> Any:
    return stripe.PaymentIntent.retrieve(payment_intent_id)


async def retrieve_payment_intent(payment_intent_id: str) -> Any:
    """Reconciliation's core lookup - the real current status of a
    PaymentIntent, independent of whether its webhook ever arrived. No
    test-env short-circuit fake here (unlike the create_* functions):
    there's no sensible generic default for "what did Stripe actually
    decide", so reconciliation tests monkeypatch this directly, same
    convention as every other alternate-path test in this module."""
    try:
        return await asyncio.to_thread(_retrieve_payment_intent_sync, payment_intent_id)
    except stripe.error.StripeError as exc:
        raise StripeApiError(f"Stripe payment intent retrieval failed: {exc}") from exc


def _cancel_payment_intent_sync(payment_intent_id: str) -> Any:
    return stripe.PaymentIntent.cancel(payment_intent_id, cancellation_reason="abandoned")


async def cancel_payment_intent(payment_intent_id: str) -> Any:
    """Cancels a still-confirmable PaymentIntent (requires_payment_method /
    requires_confirmation / requires_action). Stripe rejects the cancel once
    an intent is processing or succeeded, which surfaces here as
    StripeApiError - callers rely on that as the guard against canceling a
    payment that just went through."""
    if _IS_TEST_ENV:
        return SimpleNamespace(id=payment_intent_id, status="canceled")
    try:
        return await asyncio.to_thread(_cancel_payment_intent_sync, payment_intent_id)
    except stripe.error.StripeError as exc:
        raise StripeApiError(f"Stripe payment intent cancel failed: {exc}") from exc


def _retrieve_payments_event_sync(event_id: str) -> Any:
    return stripe.Event.retrieve(event_id)


async def retrieve_payments_event(event_id: str) -> Any:
    """Re-fetches a webhook event by id for retry_webhook_events_cron -
    fresher and more reliably shaped than trying to reconstruct dot-access
    attributes from the stored JSON payload. Scoped to the platform
    account's own events (payment_intent.*, charge.dispute.created); the
    Connect account's webhook stream isn't part of this reconciliation."""
    try:
        return await asyncio.to_thread(_retrieve_payments_event_sync, event_id)
    except stripe.error.StripeError as exc:
        raise StripeApiError(f"Stripe event retrieval failed: {exc}") from exc


def construct_payments_webhook_event(payload: bytes, sig_header: str | None) -> dict[str, Any]:
    if not sig_header:
        raise StripeWebhookSignatureError("Missing Stripe-Signature header")
    try:
        return stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_payments_webhook_secret
        )
    except (ValueError, stripe.error.SignatureVerificationError) as exc:
        raise StripeWebhookSignatureError(str(exc)) from exc
