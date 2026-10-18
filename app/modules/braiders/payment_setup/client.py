import asyncio
from typing import Any

import stripe

from app.core.config import get_settings

settings = get_settings()
stripe.api_key = settings.stripe_secret_key
# Pinned so Stripe can't reshape request/response/webhook payloads under this
# app on their own release schedule (design correction #8). `stripe.api_version`
# is a single process-wide setting on the SDK module, so setting it here also
# covers every other Stripe client in the app (see bookings/payments/client.py).
stripe.api_version = settings.stripe_api_version


class StripeApiError(Exception):
    pass


class StripeNoExpressDashboardError(StripeApiError):
    """The account has no Express Dashboard to log into - e.g. a Custom
    account, which is what the seeded test braiders use."""


class StripeWebhookSignatureError(Exception):
    pass


def _create_connect_account_sync(email: str, country: str) -> str:
    account = stripe.Account.create(
        type="express",
        email=email,
        country=country,
        capabilities={
            "card_payments": {"requested": True},
            "transfers": {"requested": True},
        },
    )
    return account.id


def _create_account_link_sync(
    account_id: str, *, refresh_url: str, return_url: str, link_type: str = "account_onboarding"
) -> str:
    link = stripe.AccountLink.create(
        account=account_id,
        refresh_url=refresh_url,
        return_url=return_url,
        type=link_type,
    )
    return link.url


def _retrieve_account_sync(account_id: str) -> stripe.Account:
    return stripe.Account.retrieve(account_id)


def _create_login_link_sync(account_id: str) -> str:
    link = stripe.Account.create_login_link(account_id)
    return link.url


async def create_connect_account(email: str, country: str) -> str:
    try:
        return await asyncio.to_thread(_create_connect_account_sync, email, country)
    except stripe.error.StripeError as exc:
        raise StripeApiError(f"Stripe account creation failed: {exc}") from exc


async def create_account_link(
    account_id: str, *, refresh_url: str, return_url: str, link_type: str = "account_onboarding"
) -> str:
    try:
        return await asyncio.to_thread(
            _create_account_link_sync,
            account_id,
            refresh_url=refresh_url,
            return_url=return_url,
            link_type=link_type,
        )
    except stripe.error.StripeError as exc:
        raise StripeApiError(f"Stripe account link creation failed: {exc}") from exc


async def retrieve_account(account_id: str) -> stripe.Account:
    try:
        return await asyncio.to_thread(_retrieve_account_sync, account_id)
    except stripe.error.StripeError as exc:
        raise StripeApiError(f"Stripe account retrieval failed: {exc}") from exc


async def create_login_link(account_id: str) -> str:
    try:
        return await asyncio.to_thread(_create_login_link_sync, account_id)
    except stripe.error.InvalidRequestError as exc:
        if "Express Dashboard" in str(exc):
            raise StripeNoExpressDashboardError(str(exc)) from exc
        raise StripeApiError(f"Stripe login link creation failed: {exc}") from exc
    except stripe.error.StripeError as exc:
        raise StripeApiError(f"Stripe login link creation failed: {exc}") from exc


def construct_webhook_event(payload: bytes, sig_header: str | None) -> dict[str, Any]:
    if not sig_header:
        raise StripeWebhookSignatureError("Missing Stripe-Signature header")
    try:
        return stripe.Webhook.construct_event(payload, sig_header, settings.stripe_webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError) as exc:
        raise StripeWebhookSignatureError(str(exc)) from exc
