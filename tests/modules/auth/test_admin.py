import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth import social as auth_social
from app.modules.auth.social.base import SocialProfile
from app.modules.auth.tasks import TASK_SEND_ADMIN_INVITE_EMAIL
from app.modules.users.models import AuthProvider, User, UserType
from tests.helpers import create_user_with_token
from tests.modules.auth.helpers import signup_and_verify

pytestmark = pytest.mark.asyncio

INVITE_URL = "/api/v1/admin/auth/invites"
ACCEPT_URL = "/api/v1/admin/auth/invites/accept"
ACCEPT_SOCIAL_URL = "/api/v1/admin/auth/invites/accept/social/google"
ADMIN_LOGIN_URL = "/api/v1/admin/auth/login"
ADMIN_SOCIAL_URL = "/api/v1/admin/auth/social/google"


async def _invite(client: AsyncClient, db_session: AsyncSession, fake_queue, *, email: str) -> str:
    _, token = await create_user_with_token(db_session, user_type=UserType.ADMIN)
    resp = await client.post(
        INVITE_URL, json={"email": email}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 201, resp.text
    return fake_queue.last_job_kwargs(TASK_SEND_ADMIN_INVITE_EMAIL)["token"]


def _patch_google(monkeypatch: pytest.MonkeyPatch, profile: SocialProfile) -> None:
    async def fake_verify(token: str) -> SocialProfile:
        return profile

    monkeypatch.setitem(auth_social._VERIFIERS, AuthProvider.GOOGLE, fake_verify)


async def test_non_admin_cannot_invite(client: AsyncClient, db_session: AsyncSession):
    _, token = await create_user_with_token(db_session, user_type=UserType.CUSTOMER)
    resp = await client.post(
        INVITE_URL, json={"email": "new-admin@example.com"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


async def test_unauthenticated_cannot_invite(client: AsyncClient):
    resp = await client.post(INVITE_URL, json={"email": "new-admin@example.com"})
    assert resp.status_code == 401


async def test_invite_accept_email_creates_admin_and_logs_in(
    client: AsyncClient, db_session: AsyncSession, fake_queue
):
    raw_token = await _invite(client, db_session, fake_queue, email="new-admin@example.com")

    resp = await client.post(
        ACCEPT_URL,
        json={
            "token": raw_token,
            "first_name": "Nyla",
            "last_name": "Admin",
            "password": "Password123",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["email"] == "new-admin@example.com"
    assert body["user_type"] == "ADMIN"
    assert "access_token" in body

    result = await db_session.execute(select(User).where(User.email == "new-admin@example.com"))
    user = result.scalar_one()
    assert user.user_type == UserType.ADMIN
    assert user.is_email_verified is True


async def test_invite_accept_email_rejects_invalid_token(client: AsyncClient):
    resp = await client.post(
        ACCEPT_URL,
        json={"token": "not-a-real-token", "first_name": "Nyla", "password": "Password123"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "ADMIN_INVITE_INVALID"


async def test_invite_accept_email_cannot_be_reused(
    client: AsyncClient, db_session: AsyncSession, fake_queue
):
    raw_token = await _invite(client, db_session, fake_queue, email="new-admin@example.com")

    first = await client.post(
        ACCEPT_URL,
        json={"token": raw_token, "first_name": "Nyla", "password": "Password123"},
    )
    assert first.status_code == 200

    second = await client.post(
        ACCEPT_URL,
        json={"token": raw_token, "first_name": "Nyla", "password": "Password123"},
    )
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "ADMIN_INVITE_INVALID"


async def test_invite_accept_social_creates_admin(
    client: AsyncClient, db_session: AsyncSession, fake_queue, monkeypatch: pytest.MonkeyPatch
):
    raw_token = await _invite(client, db_session, fake_queue, email="new-admin@example.com")
    _patch_google(
        monkeypatch,
        SocialProfile(
            provider_user_id="google-sub-1",
            email="new-admin@example.com",
            first_name="Nyla",
            last_name="Admin",
            email_verified=True,
        ),
    )

    resp = await client.post(ACCEPT_SOCIAL_URL, json={"token": raw_token, "provider_token": "fake"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["user_type"] == "ADMIN"


async def test_invite_accept_social_rejects_email_mismatch(
    client: AsyncClient, db_session: AsyncSession, fake_queue, monkeypatch: pytest.MonkeyPatch
):
    raw_token = await _invite(client, db_session, fake_queue, email="new-admin@example.com")
    _patch_google(
        monkeypatch,
        SocialProfile(
            provider_user_id="google-sub-1",
            email="someone-else@example.com",
            first_name="Nyla",
            last_name="Admin",
            email_verified=True,
        ),
    )

    resp = await client.post(ACCEPT_SOCIAL_URL, json={"token": raw_token, "provider_token": "fake"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "ADMIN_INVITE_EMAIL_MISMATCH"


async def test_admin_login_succeeds_for_admin(client: AsyncClient, db_session: AsyncSession, fake_queue):
    raw_token = await _invite(client, db_session, fake_queue, email="new-admin@example.com")
    await client.post(
        ACCEPT_URL,
        json={"token": raw_token, "first_name": "Nyla", "password": "Password123"},
    )

    resp = await client.post(
        ADMIN_LOGIN_URL, json={"email": "new-admin@example.com", "password": "Password123"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["user_type"] == "ADMIN"


async def test_admin_login_rejects_non_admin(client: AsyncClient, fake_queue):
    await signup_and_verify(client, fake_queue, email="customer@example.com")

    resp = await client.post(
        ADMIN_LOGIN_URL, json={"email": "customer@example.com", "password": "Password123"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_admin_social_login_never_creates_a_user(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    _patch_google(
        monkeypatch,
        SocialProfile(
            provider_user_id="google-sub-1",
            email="brand-new@example.com",
            first_name="Nyla",
            last_name="Admin",
            email_verified=True,
        ),
    )

    resp = await client.post(ADMIN_SOCIAL_URL, json={"provider_token": "fake"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "SOCIAL_AUTH_FAILED"


async def test_admin_social_login_rejects_non_admin(
    client: AsyncClient, fake_queue, monkeypatch: pytest.MonkeyPatch
):
    existing = await signup_and_verify(client, fake_queue, email="customer@example.com")
    _patch_google(
        monkeypatch,
        SocialProfile(
            provider_user_id="google-sub-1",
            email="customer@example.com",
            first_name="Ada",
            last_name="Lovelace",
            email_verified=True,
        ),
    )

    resp = await client.post(ADMIN_SOCIAL_URL, json={"provider_token": "fake"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "SOCIAL_AUTH_FAILED"
    assert existing["user_type"] == "CUSTOMER"


async def test_list_invites_shows_pending_and_accepted(
    client: AsyncClient, db_session: AsyncSession, fake_queue
):
    _, token = await create_user_with_token(db_session, user_type=UserType.ADMIN)
    headers = {"Authorization": f"Bearer {token}"}

    pending_resp = await client.post(
        INVITE_URL, json={"email": "still-pending@example.com"}, headers=headers
    )
    assert pending_resp.status_code == 201

    raw_token = await _invite(client, db_session, fake_queue, email="accepted@example.com")
    await client.post(
        ACCEPT_URL,
        json={"token": raw_token, "first_name": "Nyla", "password": "Password123"},
    )

    list_resp = await client.get(INVITE_URL, headers=headers)
    assert list_resp.status_code == 200, list_resp.text
    items = list_resp.json()["data"]["items"]
    by_email = {item["email"]: item["status"] for item in items}
    assert by_email["still-pending@example.com"] == "PENDING"
    assert by_email["accepted@example.com"] == "ACCEPTED"


async def test_invite_stats_counts_pending_as_active(
    client: AsyncClient, db_session: AsyncSession, fake_queue
):
    _, token = await create_user_with_token(db_session, user_type=UserType.ADMIN)
    headers = {"Authorization": f"Bearer {token}"}

    pending_resp = await client.post(
        INVITE_URL, json={"email": "stats-pending@example.com"}, headers=headers
    )
    assert pending_resp.status_code == 201

    raw_token = await _invite(client, db_session, fake_queue, email="stats-accepted@example.com")
    await client.post(
        ACCEPT_URL,
        json={"token": raw_token, "first_name": "Nyla", "password": "Password123"},
    )

    resp = await client.get(f"{INVITE_URL}/stats", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {
        "total": 2,
        "active": 1,
        "inactive": 1,
        "accepted": 1,
        "expired": 0,
        "revoked": 0,
    }


async def test_list_invites_requires_admin(client: AsyncClient, db_session: AsyncSession):
    _, token = await create_user_with_token(db_session, user_type=UserType.CUSTOMER)
    resp = await client.get(INVITE_URL, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


async def test_invite_email_already_registered_rejected(
    client: AsyncClient, db_session: AsyncSession, fake_queue
):
    await signup_and_verify(client, fake_queue, email="taken@example.com")
    _, token = await create_user_with_token(db_session, user_type=UserType.ADMIN)

    resp = await client.post(
        INVITE_URL, json={"email": "taken@example.com"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "EMAIL_ALREADY_EXISTS"
