import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import SelfBookingNotAllowedError
from app.core.redis import get_redis_client
from app.core.security import create_access_token
from app.modules.bookings import service as bookings_service
from app.modules.bookings.calculations.models import BookingCalculation, BookingCalculationStatus
from app.modules.bookings.enums import BookingStatus
from app.modules.bookings.models import Booking
from app.modules.bookings.schemas import BookingCreateRequest
from app.modules.braiders.offerings.models import BraiderStyle
from app.modules.styles.models import Style
from app.modules.users import repository as users_repo
from app.modules.users.models import UserType
from tests.conftest import FakeArqPool
from tests.helpers import create_user_with_token
from tests.modules.bookings.helpers import create_bookable_braider

pytestmark = pytest.mark.asyncio

CALC_URL = "/api/v1/booking-calculations"
BOOKINGS_URL = "/api/v1/bookings"


async def _customer_headers(db_session: AsyncSession) -> dict:
    _, token = await create_user_with_token(db_session, user_type=UserType.CUSTOMER)
    return {"Authorization": f"Bearer {token}"}


async def _create_calculation(client: AsyncClient, braider: dict, **overrides) -> dict:
    payload = {
        "braider_id": str(braider["braider_id"]),
        "style_id": str(braider["style_id"]),
        **overrides,
    }
    resp = await client.post(CALC_URL, json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def _iso(dt: datetime) -> str:
    return dt.isoformat()


async def test_create_booking_full_upfront(client: AsyncClient, db_session: AsyncSession):
    braider = await create_bookable_braider(db_session, base_price="180.00", country="AT")
    headers = await _customer_headers(db_session)
    calc = await _create_calculation(client, braider)
    starts_at = datetime.now(UTC) + timedelta(hours=10)

    resp = await client.post(
        BOOKINGS_URL,
        json={
            "booking_calculation_id": calc["id"],
            "starts_at": _iso(starts_at),
            "terms_accepted": True,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["status"] == "PENDING_PAYMENT"
    assert data["payment_schedule"] == "FULL_UPFRONT"
    assert data["total"] == calc["total"]
    assert len(data["payments"]) == 1
    assert data["payments"][0]["purpose"] == "FULL"
    assert data["payments"][0]["amount"] == calc["total"]
    assert data["payments"][0]["client_secret"] is not None
    assert data["payments"][0]["client_secret"].startswith("pi_test_")
    assert data["reference"].startswith("AB-")


async def test_create_booking_deposit_schedule(client: AsyncClient, db_session: AsyncSession):
    braider = await create_bookable_braider(db_session, base_price="180.00", country="AT")
    headers = await _customer_headers(db_session)
    calc = await _create_calculation(client, braider)
    starts_at = datetime.now(UTC) + timedelta(days=60)

    resp = await client.post(
        BOOKINGS_URL,
        json={
            "booking_calculation_id": calc["id"],
            "starts_at": _iso(starts_at),
            "terms_accepted": True,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["payment_schedule"] == "DEPOSIT_THEN_BALANCE"
    assert data["payments"][0]["purpose"] == "DEPOSIT"
    assert data["payments"][0]["amount"] == data["deposit_amount"]


async def test_create_booking_requires_terms_acceptance(client: AsyncClient, db_session: AsyncSession):
    braider = await create_bookable_braider(db_session, base_price="180.00", country="AT")
    headers = await _customer_headers(db_session)
    calc = await _create_calculation(client, braider)
    starts_at = datetime.now(UTC) + timedelta(hours=10)

    resp = await client.post(
        BOOKINGS_URL,
        json={
            "booking_calculation_id": calc["id"],
            "starts_at": _iso(starts_at),
            "terms_accepted": False,
        },
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


async def test_create_booking_no_connect_account_returns_409(
    client: AsyncClient, db_session: AsyncSession
):
    braider = await create_bookable_braider(db_session, base_price="180.00", country="AT", payable=False)
    headers = await _customer_headers(db_session)
    calc = await _create_calculation(client, braider)
    starts_at = datetime.now(UTC) + timedelta(hours=10)

    resp = await client.post(
        BOOKINGS_URL,
        json={
            "booking_calculation_id": calc["id"],
            "starts_at": _iso(starts_at),
            "terms_accepted": True,
        },
        headers=headers,
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "BRAIDER_NOT_PAYABLE"


async def test_create_booking_rejects_braider_booking_own_profile(
    client: AsyncClient, db_session: AsyncSession
):
    """A braider's user account is UserType.BRAIDER, so `_require_customer`
    already keeps them off this endpoint entirely. This exercises the
    service-level guard directly - the last line of defence should the
    two roles ever stop being mutually exclusive (e.g. an admin-initiated
    booking, or a future dual-role account)."""
    braider = await create_bookable_braider(db_session, base_price="180.00", country="AT")
    calc = await _create_calculation(client, braider)
    starts_at = datetime.now(UTC) + timedelta(hours=10)

    with pytest.raises(SelfBookingNotAllowedError):
        await bookings_service.create_booking(
            db_session,
            get_redis_client(),
            FakeArqPool(),
            user=braider["user"],
            data=BookingCreateRequest(
                booking_calculation_id=calc["id"],
                starts_at=starts_at,
                terms_accepted=True,
            ),
            locale="en",
        )


async def test_create_booking_price_drift_returns_409(client: AsyncClient, db_session: AsyncSession):
    braider = await create_bookable_braider(db_session, base_price="180.00", country="AT")
    headers = await _customer_headers(db_session)
    calc = await _create_calculation(client, braider)

    # Braider changes their price after the quote was issued but before the
    # customer confirms the booking.
    braider_style = await db_session.get(BraiderStyle, braider["braider_style_id"])
    braider_style.base_price = Decimal("999.00")
    await db_session.commit()

    starts_at = datetime.now(UTC) + timedelta(hours=10)
    resp = await client.post(
        BOOKINGS_URL,
        json={
            "booking_calculation_id": calc["id"],
            "starts_at": _iso(starts_at),
            "terms_accepted": True,
        },
        headers=headers,
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "BOOKING_PRICE_DRIFT"


async def test_create_booking_starts_in_past_returns_422(client: AsyncClient, db_session: AsyncSession):
    braider = await create_bookable_braider(db_session, base_price="180.00", country="AT")
    headers = await _customer_headers(db_session)
    calc = await _create_calculation(client, braider)
    starts_at = datetime.now(UTC) - timedelta(hours=1)

    resp = await client.post(
        BOOKINGS_URL,
        json={
            "booking_calculation_id": calc["id"],
            "starts_at": _iso(starts_at),
            "terms_accepted": True,
        },
        headers=headers,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "BOOKING_STARTS_IN_PAST"


async def test_create_booking_consumes_calculation(client: AsyncClient, db_session: AsyncSession):
    braider = await create_bookable_braider(db_session, base_price="180.00", country="AT")
    headers = await _customer_headers(db_session)
    calc = await _create_calculation(client, braider)
    starts_at = datetime.now(UTC) + timedelta(hours=10)

    resp = await client.post(
        BOOKINGS_URL,
        json={
            "booking_calculation_id": calc["id"],
            "starts_at": _iso(starts_at),
            "terms_accepted": True,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text

    calculation = await db_session.get(BookingCalculation, uuid.UUID(calc["id"]))
    await db_session.refresh(calculation)
    assert calculation.status == BookingCalculationStatus.CONSUMED
    assert calculation.consumed_by_booking_id is not None

    # Re-using the same (now consumed) calculation for a second booking must fail.
    resp2 = await client.post(
        BOOKINGS_URL,
        json={
            "booking_calculation_id": calc["id"],
            "starts_at": _iso(starts_at + timedelta(days=1)),
            "terms_accepted": True,
        },
        headers=headers,
    )
    assert resp2.status_code == 409, resp2.text
    assert resp2.json()["error"]["code"] == "BOOKING_CALCULATION_ALREADY_USED"


async def test_create_booking_snapshot_survives_later_catalog_changes(
    client: AsyncClient, db_session: AsyncSession
):
    braider = await create_bookable_braider(db_session, base_price="180.00", country="AT")
    headers = await _customer_headers(db_session)
    calc = await _create_calculation(client, braider)
    starts_at = datetime.now(UTC) + timedelta(hours=10)

    resp = await client.post(
        BOOKINGS_URL,
        json={
            "booking_calculation_id": calc["id"],
            "starts_at": _iso(starts_at),
            "terms_accepted": True,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    booking_id = resp.json()["data"]["id"]
    original_total = resp.json()["data"]["total"]

    braider_style = await db_session.get(BraiderStyle, braider["braider_style_id"])
    braider_style.base_price = Decimal("5.00")
    await db_session.commit()

    get_resp = await client.get(f"{BOOKINGS_URL}/{booking_id}", headers=headers)
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["data"]["total"] == original_total


async def test_get_booking_not_owner_returns_404(client: AsyncClient, db_session: AsyncSession):
    braider = await create_bookable_braider(db_session, base_price="180.00", country="AT")
    headers = await _customer_headers(db_session)
    calc = await _create_calculation(client, braider)
    starts_at = datetime.now(UTC) + timedelta(hours=10)
    resp = await client.post(
        BOOKINGS_URL,
        json={"booking_calculation_id": calc["id"], "starts_at": _iso(starts_at), "terms_accepted": True},
        headers=headers,
    )
    booking_id = resp.json()["data"]["id"]

    other_headers = await _customer_headers(db_session)
    other_resp = await client.get(f"{BOOKINGS_URL}/{booking_id}", headers=other_headers)
    assert other_resp.status_code == 404


async def test_list_bookings_for_customer(client: AsyncClient, db_session: AsyncSession):
    braider = await create_bookable_braider(db_session, base_price="180.00", country="AT")
    headers = await _customer_headers(db_session)
    calc = await _create_calculation(client, braider)
    starts_at = datetime.now(UTC) + timedelta(hours=10)
    await client.post(
        BOOKINGS_URL,
        json={"booking_calculation_id": calc["id"], "starts_at": _iso(starts_at), "terms_accepted": True},
        headers=headers,
    )

    resp = await client.get(BOOKINGS_URL, headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["total_items"] == 1
    assert data["items"][0]["reference"].startswith("AB-")


async def test_braider_can_list_and_get_own_booking(client: AsyncClient, db_session: AsyncSession):
    braider = await create_bookable_braider(db_session, base_price="180.00", country="AT")
    headers = await _customer_headers(db_session)
    calc = await _create_calculation(client, braider)
    starts_at = datetime.now(UTC) + timedelta(hours=10)
    resp = await client.post(
        BOOKINGS_URL,
        json={"booking_calculation_id": calc["id"], "starts_at": _iso(starts_at), "terms_accepted": True},
        headers=headers,
    )
    booking_id = resp.json()["data"]["id"]

    token, _ = create_access_token(user_id=braider["user"].id, user_type="BRAIDER")

    list_resp = await client.get(
        "/api/v1/braiders/me/bookings", headers={"Authorization": f"Bearer {token}"}
    )
    assert list_resp.status_code == 200, list_resp.text
    assert list_resp.json()["data"]["total_items"] == 1

    get_resp = await client.get(
        f"/api/v1/braiders/me/bookings/{booking_id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["data"]["reference"] == resp.json()["data"]["reference"]


async def _book(client: AsyncClient, headers: dict, braider: dict, starts_at: datetime) -> dict:
    calc = await _create_calculation(client, braider)
    resp = await client.post(
        BOOKINGS_URL,
        json={"booking_calculation_id": calc["id"], "starts_at": _iso(starts_at), "terms_accepted": True},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


async def test_booking_responses_include_braider_and_customer_name(
    client: AsyncClient, db_session: AsyncSession
):
    braider = await create_bookable_braider(
        db_session, business_name="Amina's Braids", base_price="180.00", country="AT"
    )
    headers = await _customer_headers(db_session)
    starts_at = datetime.now(UTC) + timedelta(hours=10)
    created = await _book(client, headers, braider, starts_at)
    assert created["braider_name"] == "Amina's Braids"
    assert created["customer_name"] == "Test User"

    detail_resp = await client.get(f"{BOOKINGS_URL}/{created['id']}", headers=headers)
    detail = detail_resp.json()["data"]
    assert detail["braider_name"] == "Amina's Braids"
    assert detail["customer_name"] == "Test User"

    list_resp = await client.get(BOOKINGS_URL, headers=headers)
    summary = list_resp.json()["data"]["items"][0]
    assert summary["braider_name"] == "Amina's Braids"
    assert summary["customer_name"] == "Test User"

    token, _ = create_access_token(user_id=braider["user"].id, user_type="BRAIDER")
    braider_headers = {"Authorization": f"Bearer {token}"}
    braider_detail = (
        await client.get(f"/api/v1/braiders/me/bookings/{created['id']}", headers=braider_headers)
    ).json()["data"]
    assert braider_detail["braider_name"] == "Amina's Braids"
    assert braider_detail["customer_name"] == "Test User"


async def test_bookings_filter_by_status(client: AsyncClient, db_session: AsyncSession):
    braider = await create_bookable_braider(db_session, base_price="180.00", country="AT")
    headers = await _customer_headers(db_session)
    starts_at = datetime.now(UTC) + timedelta(hours=10)
    pending = await _book(client, headers, braider, starts_at)

    confirmed = await _book(client, headers, braider, starts_at + timedelta(days=1))
    booking = await db_session.get(Booking, uuid.UUID(confirmed["id"]))
    booking.status = BookingStatus.CONFIRMED
    await db_session.commit()

    resp = await client.get(BOOKINGS_URL, params={"status": "CONFIRMED"}, headers=headers)
    ids = {item["id"] for item in resp.json()["data"]["items"]}
    assert ids == {confirmed["id"]}

    resp = await client.get(BOOKINGS_URL, params={"status": "PENDING_PAYMENT"}, headers=headers)
    ids = {item["id"] for item in resp.json()["data"]["items"]}
    assert ids == {pending["id"]}


async def test_bookings_filter_by_date_range(client: AsyncClient, db_session: AsyncSession):
    braider = await create_bookable_braider(db_session, base_price="180.00", country="AT")
    headers = await _customer_headers(db_session)
    soon = datetime.now(UTC) + timedelta(hours=10)
    far = datetime.now(UTC) + timedelta(days=40)
    near_booking = await _book(client, headers, braider, soon)
    await _book(client, headers, braider, far)

    resp = await client.get(
        BOOKINGS_URL,
        params={
            "date_from": soon.date().isoformat(),
            "date_to": soon.date().isoformat(),
        },
        headers=headers,
    )
    ids = {item["id"] for item in resp.json()["data"]["items"]}
    assert ids == {near_booking["id"]}

    bad_range = await client.get(
        BOOKINGS_URL,
        params={"date_from": far.date().isoformat(), "date_to": soon.date().isoformat()},
        headers=headers,
    )
    assert bad_range.status_code == 400
    assert bad_range.json()["error"]["code"] == "INVALID_BOOKING_DATE_RANGE"


async def test_bookings_search_by_style_and_braider_name(client: AsyncClient, db_session: AsyncSession):
    braider_a = await create_bookable_braider(
        db_session, business_name="Amina's Braids", base_price="180.00", country="AT"
    )
    braider_b = await create_bookable_braider(
        db_session, business_name="Zoe Styling", base_price="180.00", country="AT"
    )
    # Give braider_b's offering a distinctive, searchable style name.
    style = await db_session.get(Style, braider_b["style_id"])
    style.name_en = "Fulani Cornrows"
    await db_session.commit()

    headers = await _customer_headers(db_session)
    starts_at = datetime.now(UTC) + timedelta(hours=10)
    booking_a = await _book(client, headers, braider_a, starts_at)
    booking_b = await _book(client, headers, braider_b, starts_at + timedelta(days=1))

    by_braider_name = await client.get(BOOKINGS_URL, params={"search": "Amina"}, headers=headers)
    ids = {item["id"] for item in by_braider_name.json()["data"]["items"]}
    assert ids == {booking_a["id"]}

    by_style_name = await client.get(BOOKINGS_URL, params={"search": "Cornrows"}, headers=headers)
    ids = {item["id"] for item in by_style_name.json()["data"]["items"]}
    assert ids == {booking_b["id"]}


async def test_braider_bookings_search_by_style_and_customer_name(
    client: AsyncClient, db_session: AsyncSession
):
    braider = await create_bookable_braider(db_session, base_price="180.00", country="AT")

    customer_1 = await users_repo.create_user(
        db_session,
        first_name="Chidinma",
        last_name="Okafor",
        email=f"{uuid.uuid4()}@example.com",
        phone_number=None,
        password_hash=None,
        user_type=UserType.CUSTOMER,
        is_email_verified=True,
    )
    await db_session.commit()
    token_1, _ = create_access_token(user_id=customer_1.id, user_type="CUSTOMER")
    headers_1 = {"Authorization": f"Bearer {token_1}"}

    headers_2 = await _customer_headers(db_session)

    starts_at = datetime.now(UTC) + timedelta(hours=10)
    booking_1 = await _book(client, headers_1, braider, starts_at)
    booking_2 = await _book(client, headers_2, braider, starts_at + timedelta(days=1))

    braider_token, _ = create_access_token(user_id=braider["user"].id, user_type="BRAIDER")
    braider_headers = {"Authorization": f"Bearer {braider_token}"}

    by_customer_name = await client.get(
        "/api/v1/braiders/me/bookings", params={"search": "Chidinma"}, headers=braider_headers
    )
    ids = {item["id"] for item in by_customer_name.json()["data"]["items"]}
    assert ids == {booking_1["id"]}

    by_style_name = await client.get(
        "/api/v1/braiders/me/bookings", params={"search": "Knotless"}, headers=braider_headers
    )
    ids = {item["id"] for item in by_style_name.json()["data"]["items"]}
    assert ids == {booking_1["id"], booking_2["id"]}
