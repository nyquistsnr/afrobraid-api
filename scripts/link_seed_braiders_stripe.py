"""Replaces seeded braiders' placeholder Stripe accounts (`acct_seed_...`, from
scripts.seed_braiders) with real, fully verified **test-mode** Connect accounts,
so test payments and transfers to them actually go through.

How KYC is skipped: each account is created as a Custom account (the platform
supplies every requirement over the API, instead of the braider going through
Stripe-hosted Express onboarding) using Stripe's magic test values -
  - DOB 1901-01-01                 -> identity verification succeeds
  - address line1 address_full_match -> address verification succeeds
  - file_identity_document_success -> ID document verification succeeds
  - Stripe's published test IBAN / UK sort code -> payouts succeed
  - tos_acceptance set by the platform (only allowed for Custom accounts)
so Stripe enables charges and payouts without any manual step.
See https://docs.stripe.com/connect/testing

Caveat: seeded accounts are Custom, not Express like real braiders, so the
braider "Stripe dashboard" login link (`create_login_link`) won't work for
them - everything else (booking, payment, transfers, refunds) does.

Refuses to run unless STRIPE_SECRET_KEY is a test key.

Idempotent: only touches rows still on an `acct_seed_` placeholder, and the
Stripe create call uses a per-braider idempotency key, so a crash between the
Stripe call and the DB commit doesn't create a duplicate account on re-run
(within Stripe's 24h idempotency window).

Run from the repo root:
    python -m scripts.link_seed_braiders_stripe                # link all
    python -m scripts.link_seed_braiders_stripe --limit 50     # try a few first
    python -m scripts.link_seed_braiders_stripe --refresh-only # re-sync status
    python -m scripts.link_seed_braiders_stripe --no-create    # adopt only, no creates
"""
import argparse
import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

import stripe
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.modules.braiders.models import BraiderProfile

# Imported for its side effect: sets stripe.api_key and the pinned
# stripe.api_version exactly as the app does.
from app.modules.braiders.payment_setup import client as _payment_setup_client  # noqa: F401
from app.modules.braiders.payment_setup.models import StripeConnectAccount
from app.modules.braiders.service_location.models import BraiderServiceLocation
from app.modules.users.models import User
from scripts.seed_braiders import is_seed_user

PLACEHOLDER_PREFIX = "acct_seed_"
BATCH_SIZE = 100
# Stripe caps account *creation* at <5/s on a test key (separate from, and much
# lower than, the general ~25 req/s limit) and rejects the excess outright.
CREATES_PER_SECOND = 3
# Fixed (not time.time()) so a retried create sends byte-identical params -
# Stripe rejects a reused idempotency key whose params differ.
TOS_ACCEPTED_AT = 1792281600  # 2026-10-18T00:00:00Z
# Bump to abandon keys Stripe has already locked to different params.
IDEMPOTENCY_KEY_VERSION = "v2"

# Stripe's published test bank accounts that always pay out successfully.
_TEST_EXTERNAL_ACCOUNTS: dict[str, dict[str, str]] = {
    "DE": {"country": "DE", "currency": "eur", "account_number": "DE89370400440532013000"},
    "FR": {"country": "FR", "currency": "eur", "account_number": "FR1420041010050500013M02606"},
    "GB": {
        "country": "GB",
        "currency": "gbp",
        "account_number": "00012345",
        "routing_number": "108800",
    },
}
_TEST_PHONES = {"DE": "+4915112345678", "FR": "+33612345678", "GB": "+447700900123"}


@dataclass
class SeedBraider:
    braider_id: uuid.UUID
    email: str
    first_name: str
    last_name: str
    business_name: str
    city: str
    postal_code: str
    country: str


def _create_account_sync(b: SeedBraider) -> stripe.Account:
    external = dict(_TEST_EXTERNAL_ACCOUNTS[b.country])
    return stripe.Account.create(
        type="custom",
        country=b.country,
        email=b.email,
        business_type="individual",
        capabilities={
            "card_payments": {"requested": True},
            "transfers": {"requested": True},
        },
        business_profile={
            "mcc": "7230",  # beauty and barber shops
            "name": b.business_name,
            "product_description": "Hair braiding services booked through AfroBraid.",
        },
        individual={
            "first_name": b.first_name,
            "last_name": b.last_name,
            "email": b.email,
            "phone": _TEST_PHONES[b.country],
            "dob": {"day": 1, "month": 1, "year": 1901},
            "address": {
                "line1": "address_full_match",
                "city": b.city,
                "postal_code": b.postal_code,
                "country": b.country,
            },
            "verification": {
                "document": {"front": "file_identity_document_success"},
                "additional_document": {"front": "file_identity_document_success"},
            },
        },
        external_account={"object": "bank_account", **external},
        tos_acceptance={"date": TOS_ACCEPTED_AT, "ip": "127.0.0.1"},
        metadata={"seed": "true", "braider_id": str(b.braider_id)},
        idempotency_key=f"seed-connect-{IDEMPOTENCY_KEY_VERSION}-{b.braider_id}",
    )


def _apply(row: StripeConnectAccount, account: Any) -> None:
    """Same field mapping as payment_setup.service._apply_account, minus the
    per-row commit and onboarding-status update (the seed already completed
    every onboarding step)."""
    row.stripe_account_id = account.id
    row.charges_enabled = bool(account.charges_enabled)
    row.payouts_enabled = bool(account.payouts_enabled)
    row.details_submitted = bool(account.details_submitted)
    requirements = getattr(account, "requirements", None)
    row.disabled_reason = getattr(requirements, "disabled_reason", None) if requirements else None
    currently_due = getattr(requirements, "currently_due", None) if requirements else None
    row.requirements_currently_due = json.dumps(currently_due) if currently_due is not None else None
    row.last_webhook_payload = json.dumps(account.to_dict())


_MAX_ATTEMPTS = 20


class _RateLimiter:
    """Spaces calls at least `interval` seconds apart across all coroutines,
    and adapts it: Stripe's account-creation throttle tightens after a burst,
    so a fixed rate either crawls or keeps getting rejected."""

    def __init__(self, rate: float) -> None:
        self._min_interval = 1 / rate
        self._interval = self._min_interval
        self._next = 0.0
        self._lock = asyncio.Lock()

    @property
    def rate(self) -> float:
        return 1 / self._interval

    def slow_down(self) -> None:
        self._interval = min(self._interval * 1.5, 10.0)

    def speed_up(self) -> None:
        self._interval = max(self._interval * 0.98, self._min_interval)

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self._interval
        if delay > 0:
            await asyncio.sleep(delay)


async def _run_batch(
    rows: list[tuple[StripeConnectAccount, Any]],
    fn,
    semaphore: asyncio.Semaphore,
    limiter: _RateLimiter | None = None,
) -> list[tuple[StripeConnectAccount, Any]]:
    async def one(row: StripeConnectAccount, arg: Any) -> tuple[StripeConnectAccount, Any]:
        async with semaphore:
            for attempt in range(_MAX_ATTEMPTS):
                if limiter is not None:
                    await limiter.wait()
                try:
                    result = await asyncio.to_thread(fn, arg)
                    if limiter is not None:
                        limiter.speed_up()
                    return row, result
                except stripe.error.StripeError as exc:
                    retryable = isinstance(
                        exc, stripe.error.RateLimitError | stripe.error.APIConnectionError
                    ) or "too quickly" in str(exc)
                    if retryable and attempt < _MAX_ATTEMPTS - 1:
                        if limiter is not None:
                            limiter.slow_down()
                            print(f"  ~ rate limited, slowing to {limiter.rate:.2f}/s")
                        await asyncio.sleep(min(2 * (attempt + 1), 15))
                        continue
                    return row, exc
            raise AssertionError("unreachable")

    return await asyncio.gather(*(one(row, arg) for row, arg in rows))


def _existing_seed_accounts_sync() -> dict[str, Any]:
    """Seed accounts already in Stripe, by braider_id - e.g. ones created by
    an interrupted run whose DB commit never happened. Prefers a fully
    enabled account when a braider has more than one."""
    by_braider: dict[str, Any] = {}
    for account in stripe.Account.list(limit=100).auto_paging_iter():
        metadata = account.metadata or {}
        braider_id = metadata.get("braider_id")
        if metadata.get("seed") != "true" or not braider_id:
            continue
        current = by_braider.get(braider_id)
        if current is None or (
            account.charges_enabled and account.payouts_enabled
            and not (current.charges_enabled and current.payouts_enabled)
        ):
            by_braider[braider_id] = account
    return by_braider


async def link_placeholders(limit: int | None, concurrency: int, create: bool) -> None:
    semaphore = asyncio.Semaphore(concurrency)
    limiter = _RateLimiter(CREATES_PER_SECOND)
    async with AsyncSessionLocal() as db:
        query = (
            select(StripeConnectAccount, User, BraiderProfile, BraiderServiceLocation)
            .join(BraiderProfile, BraiderProfile.id == StripeConnectAccount.braider_id)
            .join(User, User.id == BraiderProfile.user_id)
            .join(BraiderServiceLocation, BraiderServiceLocation.braider_id == BraiderProfile.id)
            .where(
                is_seed_user(),
                StripeConnectAccount.stripe_account_id.like(f"{PLACEHOLDER_PREFIX}%"),
            )
            .order_by(User.email)
        )
        if limit is not None:
            query = query.limit(limit)
        results = (await db.execute(query)).all()

        work: list[tuple[StripeConnectAccount, SeedBraider]] = []
        skipped = 0
        for row, user, profile, location in results:
            country = (location.country or "").upper()
            if country not in _TEST_EXTERNAL_ACCOUNTS:
                # e.g. the original LU/ES seed braiders - not in the target markets.
                skipped += 1
                continue
            work.append(
                (
                    row,
                    SeedBraider(
                        braider_id=profile.id,
                        email=user.email,
                        first_name=user.first_name,
                        last_name=user.last_name or "Braider",
                        business_name=profile.business_name or f"{user.first_name} Braids",
                        city=location.city or "",
                        postal_code=location.postal_code or "",
                        country=country,
                    ),
                )
            )

        # Adopt accounts Stripe already has for these braiders before creating
        # any - creation is capped in test mode, reads aren't.
        existing = await asyncio.to_thread(_existing_seed_accounts_sync)
        to_create: list[tuple[StripeConnectAccount, SeedBraider]] = []
        adopted = 0
        for row, braider in work:
            account = existing.get(str(braider.braider_id))
            if account is not None:
                _apply(row, account)
                adopted += 1
            else:
                to_create.append((row, braider))
        await db.commit()
        work = to_create if create else []
        print(f"Adopted {adopted} existing Stripe account(s).")

        print(f"Linking {len(work)} placeholder account(s) to real Stripe test accounts"
              f" ({skipped} skipped: country not DE/FR/GB)...")
        linked = failed = 0
        for start in range(0, len(work), BATCH_SIZE):
            batch = work[start : start + BATCH_SIZE]
            for row, result in await _run_batch(batch, _create_account_sync, semaphore, limiter):
                if isinstance(result, Exception):
                    failed += 1
                    print(f"  ! {row.braider_id}: {result}")
                    continue
                _apply(row, result)
                linked += 1
            await db.commit()
            print(f"  + {min(start + BATCH_SIZE, len(work))}/{len(work)}"
                  f" (linked {linked}, failed {failed})")

    print(f"\nLinked {linked}, failed {failed}.")


async def refresh_statuses(concurrency: int) -> None:
    """Re-reads every seeded braider's real Stripe account and stores the
    current charges/payouts flags - capabilities can sit in `pending` for a
    few seconds after creation, so creation-time flags may lag."""
    semaphore = asyncio.Semaphore(concurrency)
    async with AsyncSessionLocal() as db:
        rows = list(
            (
                await db.execute(
                    select(StripeConnectAccount)
                    .join(BraiderProfile, BraiderProfile.id == StripeConnectAccount.braider_id)
                    .join(User, User.id == BraiderProfile.user_id)
                    .where(
                        is_seed_user(),
                        StripeConnectAccount.stripe_account_id.not_like(f"{PLACEHOLDER_PREFIX}%"),
                    )
                )
            ).scalars()
        )
        print(f"Refreshing status of {len(rows)} linked account(s)...")
        enabled = not_enabled = failed = 0
        for start in range(0, len(rows), BATCH_SIZE):
            batch = [(row, row.stripe_account_id) for row in rows[start : start + BATCH_SIZE]]
            for row, result in await _run_batch(batch, stripe.Account.retrieve, semaphore):
                if isinstance(result, Exception):
                    failed += 1
                    print(f"  ! {row.stripe_account_id}: {result}")
                    continue
                _apply(row, result)
                if row.charges_enabled and row.payouts_enabled:
                    enabled += 1
                else:
                    not_enabled += 1
                    print(f"  ~ {row.stripe_account_id} not enabled yet,"
                          f" due: {row.requirements_currently_due}")
            await db.commit()
            print(f"  + {min(start + BATCH_SIZE, len(rows))}/{len(rows)}")

    print(f"\nFully enabled: {enabled}, not yet enabled: {not_enabled}, errors: {failed}.")
    if not_enabled:
        print("Re-run with --refresh-only in a minute; pending capabilities usually settle quickly.")


async def main(limit: int | None, concurrency: int, refresh_only: bool, create: bool) -> None:
    key = get_settings().stripe_secret_key
    if not key.startswith(("sk_test_", "rk_test_")):
        raise SystemExit("Refusing to run: STRIPE_SECRET_KEY is not a Stripe *test* key.")

    # Retries are done in _run_batch, behind the rate limiter - the SDK's own
    # retries fire immediately and each one counts as another creation
    # attempt, which keeps re-tripping Stripe's account-creation limit.
    stripe.max_network_retries = 0

    if not refresh_only:
        await link_placeholders(limit, concurrency, create)
        await asyncio.sleep(5)  # give freshly requested capabilities a moment to activate
    await refresh_statuses(concurrency)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=None, help="Only link the first N placeholders.")
    parser.add_argument("--concurrency", type=int, default=8, help="Parallel Stripe requests.")
    parser.add_argument(
        "--refresh-only", action="store_true", help="Skip linking; just re-sync status from Stripe."
    )
    parser.add_argument(
        "--no-create",
        action="store_true",
        help="Only adopt accounts Stripe already has; don't create new ones (test-mode creation is capped).",
    )
    args = parser.parse_args()
    asyncio.run(main(args.limit, args.concurrency, args.refresh_only, not args.no_create))
