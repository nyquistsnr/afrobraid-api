import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import (
    BraiderCountryRequiredError,
    StripeAccountNotFoundError,
    StripeApiUnavailableError,
)
from app.modules.braiders import repository as braiders_repo
from app.modules.braiders.completion import mark_step_complete
from app.modules.braiders.models import OnboardingStep
from app.modules.braiders.payment_setup import repository as payment_setup_repo
from app.modules.braiders.payment_setup.client import (
    StripeApiError,
    StripeNoExpressDashboardError,
    create_account_link,
    create_connect_account,
    create_login_link,
    retrieve_account,
)
from app.modules.braiders.payment_setup.models import StripeConnectAccount
from app.modules.braiders.payment_setup.schemas import (
    AccountLinkResponse,
    DashboardLinkResponse,
    PaymentSetupStatusResponse,
)
from app.modules.braiders.service_location import repository as service_location_repo
from app.modules.users.models import User

settings = get_settings()


async def _apply_account(db: AsyncSession, account: StripeConnectAccount, stripe_account: Any) -> None:
    account.charges_enabled = bool(stripe_account.charges_enabled)
    account.payouts_enabled = bool(stripe_account.payouts_enabled)
    account.details_submitted = bool(stripe_account.details_submitted)
    requirements = getattr(stripe_account, "requirements", None)
    account.disabled_reason = getattr(requirements, "disabled_reason", None) if requirements else None
    currently_due = getattr(requirements, "currently_due", None) if requirements else None
    account.requirements_currently_due = json.dumps(currently_due) if currently_due is not None else None
    account.last_webhook_payload = json.dumps(stripe_account.to_dict())
    await db.flush()

    if account.charges_enabled and account.payouts_enabled:
        profile = await braiders_repo.get_profile_by_id(db, account.braider_id)
        if profile is not None:
            status = await braiders_repo.get_onboarding_status_by_user_id(db, profile.user_id)
            if status is None:
                status = await braiders_repo.create_onboarding_status_for_user(db, profile.user_id)
            mark_step_complete(status, OnboardingStep.PAYMENT_SETUP)

    await db.commit()


def _to_status_response(account: StripeConnectAccount | None) -> PaymentSetupStatusResponse:
    if account is None:
        return PaymentSetupStatusResponse(has_account=False)
    requirements_currently_due = (
        json.loads(account.requirements_currently_due) if account.requirements_currently_due else []
    )
    return PaymentSetupStatusResponse(
        has_account=True,
        stripe_account_id=account.stripe_account_id,
        charges_enabled=account.charges_enabled,
        payouts_enabled=account.payouts_enabled,
        details_submitted=account.details_submitted,
        disabled_reason=account.disabled_reason,
        requirements_currently_due=requirements_currently_due,
        is_complete=account.charges_enabled and account.payouts_enabled,
    )


async def start_onboarding(db: AsyncSession, user: User) -> AccountLinkResponse:
    profile = await braiders_repo.get_profile_by_user_id(db, user.id)
    if profile is None:
        profile = await braiders_repo.create_profile_for_user(db, user.id)

    account = await payment_setup_repo.get_by_braider_id(db, profile.id)
    if account is None:
        location = await service_location_repo.get_by_braider_id(db, profile.id)
        if location is None or not location.country:
            raise BraiderCountryRequiredError()

        try:
            stripe_account_id = await create_connect_account(
                user.email, location.country.upper()
            )
        except StripeApiError as exc:
            raise StripeApiUnavailableError() from exc
        account = await payment_setup_repo.create_for_braider(
            db, braider_id=profile.id, stripe_account_id=stripe_account_id
        )
        await db.commit()

    try:
        onboarding_url = await create_account_link(
            account.stripe_account_id,
            refresh_url=settings.stripe_connect_refresh_url,
            return_url=settings.stripe_connect_return_url,
        )
    except StripeApiError as exc:
        raise StripeApiUnavailableError() from exc

    return AccountLinkResponse(onboarding_url=onboarding_url)


async def get_dashboard_link(db: AsyncSession, user_id: uuid.UUID) -> DashboardLinkResponse:
    profile = await braiders_repo.get_profile_by_user_id(db, user_id)
    account = await payment_setup_repo.get_by_braider_id(db, profile.id) if profile else None
    if account is None:
        raise StripeAccountNotFoundError()

    try:
        dashboard_url = await create_login_link(account.stripe_account_id)
    except StripeNoExpressDashboardError:
        # Custom accounts (the seeded test braiders) have no Express Dashboard.
        # Stripe's hosted account_update page - payout bank account and
        # personal details - is the closest equivalent, same response shape.
        try:
            dashboard_url = await create_account_link(
                account.stripe_account_id,
                refresh_url=settings.stripe_connect_refresh_url,
                return_url=settings.stripe_connect_return_url,
                link_type="account_update",
            )
        except StripeApiError as exc:
            raise StripeApiUnavailableError() from exc
    except StripeApiError as exc:
        raise StripeApiUnavailableError() from exc

    return DashboardLinkResponse(dashboard_url=dashboard_url)


async def get_status(db: AsyncSession, user_id: uuid.UUID) -> PaymentSetupStatusResponse:
    profile = await braiders_repo.get_profile_by_user_id(db, user_id)
    account = await payment_setup_repo.get_by_braider_id(db, profile.id) if profile else None
    return _to_status_response(account)


async def refresh_status(db: AsyncSession, user_id: uuid.UUID) -> PaymentSetupStatusResponse:
    profile = await braiders_repo.get_profile_by_user_id(db, user_id)
    account = await payment_setup_repo.get_by_braider_id(db, profile.id) if profile else None
    if account is None:
        raise StripeAccountNotFoundError()

    try:
        stripe_account = await retrieve_account(account.stripe_account_id)
    except StripeApiError as exc:
        raise StripeApiUnavailableError() from exc

    await _apply_account(db, account, stripe_account)
    return _to_status_response(account)


async def handle_webhook(db: AsyncSession, event: Any) -> None:
    if event.type != "account.updated":
        return

    stripe_account = event.data.object
    account = await payment_setup_repo.get_by_stripe_account_id(db, stripe_account.id)
    if account is None:
        return

    await _apply_account(db, account, stripe_account)
