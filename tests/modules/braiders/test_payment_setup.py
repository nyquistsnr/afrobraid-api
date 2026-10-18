import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.braiders.payment_setup import service as payment_setup_service
from app.modules.braiders.payment_setup import webhook as payment_setup_webhook
from app.modules.braiders.payment_setup.client import (
    StripeApiError,
    StripeNoExpressDashboardError,
    StripeWebhookSignatureError,
)
from app.modules.users.models import UserType
from tests.helpers import create_user_with_token

pytestmark = pytest.mark.asyncio

ACCOUNT_LINK_URL = "/api/v1/braiders/onboarding/payment-setup/account-link"
STATUS_URL = "/api/v1/braiders/onboarding/payment-setup/status"
REFRESH_URL = "/api/v1/braiders/onboarding/payment-setup/refresh"
DASHBOARD_LINK_URL = "/api/v1/braiders/onboarding/payment-setup/dashboard-link"
ONBOARDING_STATUS_URL = "/api/v1/braiders/onboarding/status"
WEBHOOK_URL = "/api/v1/webhooks/stripe"


class _FakeRequirements:
    def __init__(self, *, currently_due=None, disabled_reason=None):
        self.currently_due = currently_due or []
        self.disabled_reason = disabled_reason


class _FakeStripeAccount:
    def __init__(
        self,
        account_id,
        *,
        charges_enabled=False,
        payouts_enabled=False,
        details_submitted=False,
        requirements=None,
    ):
        self.id = account_id
        self.charges_enabled = charges_enabled
        self.payouts_enabled = payouts_enabled
        self.details_submitted = details_submitted
        self.requirements = requirements or _FakeRequirements()

    def to_dict(self):
        return {"id": self.id, "charges_enabled": self.charges_enabled}


class _FakeEvent:
    def __init__(self, event_type, data_object):
        self.type = event_type
        self.data = type("Data", (), {"object": data_object})()


async def _set_country(client: AsyncClient, headers: dict, country: str = "DE") -> None:
    resp = await client.put(
        "/api/v1/braiders/onboarding/service-location", json={"country": country}, headers=headers
    )
    assert resp.status_code == 200, resp.text


async def _create_account(client: AsyncClient, headers: dict, monkeypatch, account_id: str) -> None:
    await _set_country(client, headers)

    async def fake_create_connect_account(email, country):
        assert country == "DE"
        return account_id

    async def fake_create_account_link(acc_id, *, refresh_url, return_url):
        return f"https://connect.stripe.com/setup/{acc_id}"

    monkeypatch.setattr(payment_setup_service, "create_connect_account", fake_create_connect_account)
    monkeypatch.setattr(payment_setup_service, "create_account_link", fake_create_account_link)

    resp = await client.post(ACCOUNT_LINK_URL, headers=headers)
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["onboarding_url"] == f"https://connect.stripe.com/setup/{account_id}"


async def test_account_link_creates_account_first_time(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    _, token = await create_user_with_token(db_session, user_type=UserType.BRAIDER)
    headers = {"Authorization": f"Bearer {token}"}
    await _set_country(client, headers)

    calls = {"created": 0}

    async def fake_create_connect_account(email, country):
        calls["created"] += 1
        return "acct_123"

    async def fake_create_account_link(acc_id, *, refresh_url, return_url):
        return f"https://connect.stripe.com/setup/{acc_id}"

    monkeypatch.setattr(payment_setup_service, "create_connect_account", fake_create_connect_account)
    monkeypatch.setattr(payment_setup_service, "create_account_link", fake_create_account_link)

    first = await client.post(ACCOUNT_LINK_URL, headers=headers)
    assert first.status_code == 201, first.text
    second = await client.post(ACCOUNT_LINK_URL, headers=headers)
    assert second.status_code == 201, second.text

    # Account is only ever created once - the second call just mints a fresh link.
    assert calls["created"] == 1


async def test_account_link_requires_country(client: AsyncClient, db_session: AsyncSession):
    _, token = await create_user_with_token(db_session, user_type=UserType.BRAIDER)
    resp = await client.post(ACCOUNT_LINK_URL, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "BRAIDER_COUNTRY_REQUIRED"


async def test_status_empty_before_starting(client: AsyncClient, db_session: AsyncSession):
    _, token = await create_user_with_token(db_session, user_type=UserType.BRAIDER)
    resp = await client.get(STATUS_URL, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["has_account"] is False
    assert data["is_complete"] is False


async def test_refresh_requires_existing_account(client: AsyncClient, db_session: AsyncSession):
    _, token = await create_user_with_token(db_session, user_type=UserType.BRAIDER)
    resp = await client.post(REFRESH_URL, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "STRIPE_ACCOUNT_NOT_FOUND"


async def test_dashboard_link_requires_existing_account(
    client: AsyncClient, db_session: AsyncSession
):
    _, token = await create_user_with_token(db_session, user_type=UserType.BRAIDER)
    resp = await client.post(DASHBOARD_LINK_URL, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "STRIPE_ACCOUNT_NOT_FOUND"


async def test_dashboard_link_works_even_before_onboarding_completes(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    _, token = await create_user_with_token(db_session, user_type=UserType.BRAIDER)
    headers = {"Authorization": f"Bearer {token}"}
    await _create_account(client, headers, monkeypatch, "acct_dash")

    async def fake_create_login_link(account_id):
        assert account_id == "acct_dash"
        return f"https://connect.stripe.com/app/express#{account_id}/balance"

    monkeypatch.setattr(payment_setup_service, "create_login_link", fake_create_login_link)

    resp = await client.post(DASHBOARD_LINK_URL, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["dashboard_url"] == "https://connect.stripe.com/app/express#acct_dash/balance"


async def test_dashboard_link_falls_back_to_account_update_without_express_dashboard(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    _, token = await create_user_with_token(db_session, user_type=UserType.BRAIDER)
    headers = {"Authorization": f"Bearer {token}"}
    await _create_account(client, headers, monkeypatch, "acct_custom")

    async def fake_create_login_link(account_id):
        raise StripeNoExpressDashboardError("does not have access to the Express Dashboard")

    async def fake_create_account_link(acc_id, *, refresh_url, return_url, link_type="account_onboarding"):
        assert link_type == "account_update"
        return f"https://connect.stripe.com/setup/c/{acc_id}"

    monkeypatch.setattr(payment_setup_service, "create_login_link", fake_create_login_link)
    monkeypatch.setattr(payment_setup_service, "create_account_link", fake_create_account_link)

    resp = await client.post(DASHBOARD_LINK_URL, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["dashboard_url"] == "https://connect.stripe.com/setup/c/acct_custom"


async def test_dashboard_link_other_stripe_errors_are_502(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    _, token = await create_user_with_token(db_session, user_type=UserType.BRAIDER)
    headers = {"Authorization": f"Bearer {token}"}
    await _create_account(client, headers, monkeypatch, "acct_down")

    async def fake_create_login_link(account_id):
        raise StripeApiError("boom")

    monkeypatch.setattr(payment_setup_service, "create_login_link", fake_create_login_link)

    resp = await client.post(DASHBOARD_LINK_URL, headers=headers)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "STRIPE_API_UNAVAILABLE"


async def test_refresh_applies_enabled_state_and_completes_step(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    _, token = await create_user_with_token(db_session, user_type=UserType.BRAIDER)
    headers = {"Authorization": f"Bearer {token}"}
    await _create_account(client, headers, monkeypatch, "acct_456")

    fake_account = _FakeStripeAccount(
        "acct_456", charges_enabled=True, payouts_enabled=True, details_submitted=True
    )

    async def fake_retrieve_account(account_id):
        assert account_id == "acct_456"
        return fake_account

    monkeypatch.setattr(payment_setup_service, "retrieve_account", fake_retrieve_account)

    resp = await client.post(REFRESH_URL, headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["charges_enabled"] is True
    assert data["payouts_enabled"] is True
    assert data["is_complete"] is True

    status_resp = await client.get(ONBOARDING_STATUS_URL, headers=headers)
    status_data = status_resp.json()["data"]
    assert status_data["payment_setup_completed_at"] is not None


async def test_refresh_surfaces_outstanding_requirements(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    _, token = await create_user_with_token(db_session, user_type=UserType.BRAIDER)
    headers = {"Authorization": f"Bearer {token}"}
    await _create_account(client, headers, monkeypatch, "acct_789")

    fake_account = _FakeStripeAccount(
        "acct_789",
        charges_enabled=False,
        payouts_enabled=False,
        requirements=_FakeRequirements(currently_due=["individual.dob.day", "external_account"]),
    )

    async def fake_retrieve_account(account_id):
        return fake_account

    monkeypatch.setattr(payment_setup_service, "retrieve_account", fake_retrieve_account)

    resp = await client.post(REFRESH_URL, headers=headers)
    data = resp.json()["data"]
    assert data["is_complete"] is False
    assert "individual.dob.day" in data["requirements_currently_due"]


async def test_webhook_applies_account_updated_event(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    _, token = await create_user_with_token(db_session, user_type=UserType.BRAIDER)
    headers = {"Authorization": f"Bearer {token}"}
    await _create_account(client, headers, monkeypatch, "acct_webhook")

    fake_account = _FakeStripeAccount(
        "acct_webhook", charges_enabled=True, payouts_enabled=True, details_submitted=True
    )
    fake_event = _FakeEvent("account.updated", fake_account)

    def fake_construct_webhook_event(payload, sig_header):
        return fake_event

    monkeypatch.setattr(
        payment_setup_webhook, "construct_webhook_event", fake_construct_webhook_event
    )

    resp = await client.post(WEBHOOK_URL, json={}, headers={"Stripe-Signature": "t=1,v1=fake"})
    assert resp.status_code == 200, resp.text

    status_resp = await client.get(STATUS_URL, headers=headers)
    assert status_resp.json()["data"]["is_complete"] is True


async def test_webhook_rejects_invalid_signature(client: AsyncClient, monkeypatch):
    def fake_construct_webhook_event(payload, sig_header):
        raise StripeWebhookSignatureError("bad signature")

    monkeypatch.setattr(
        payment_setup_webhook, "construct_webhook_event", fake_construct_webhook_event
    )

    resp = await client.post(WEBHOOK_URL, json={}, headers={"Stripe-Signature": "bad"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "STRIPE_INVALID_WEBHOOK_SIGNATURE"
