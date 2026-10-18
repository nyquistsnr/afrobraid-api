import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import query_cache
from app.core.cache_tags import BRAIDERS, CATALOG, CUSTOMER_NAMES
from app.core.config import get_settings
from app.core.redis import get_redis_client
from app.modules.styles.models import StyleCategory
from app.modules.users.models import UserType
from tests.helpers import create_user_with_token

pytestmark = pytest.mark.asyncio

CATEGORIES_URL = "/api/v1/style-categories"


def _counting_fn(tag: str = "test-tag"):
    calls = {"n": 0}

    @query_cache.cached(name="test.fn", ttl_seconds=60, tags=lambda a: [tag])
    async def fn(db, *, value: int) -> dict[str, int]:
        calls["n"] += 1
        await asyncio.sleep(0.01)
        return {"value": value, "call": calls["n"]}

    return fn, calls


async def _tag_token(tag: str) -> str | None:
    redis = get_redis_client()
    return await redis.get(f"cache:tag:v1:{tag}")


async def test_second_call_is_served_from_cache():
    fn, calls = _counting_fn()

    first = await fn(None, value=1)
    second = await fn(None, value=1)

    assert first == second == {"value": 1, "call": 1}
    assert calls["n"] == 1


async def test_different_arguments_get_different_entries():
    fn, calls = _counting_fn()

    await fn(None, value=1)
    await fn(None, value=2)

    assert calls["n"] == 2


async def test_bumping_a_tag_invalidates_dependent_entries():
    fn, calls = _counting_fn()

    await fn(None, value=1)
    await query_cache.bump_tags(["test-tag"])
    result = await fn(None, value=1)

    assert result["call"] == 2


async def test_evicted_tag_does_not_resurrect_old_entries():
    fn, calls = _counting_fn()

    await fn(None, value=1)
    await query_cache.bump_tags(["test-tag"])
    await fn(None, value=1)
    # Simulate Redis evicting the tag key under memory pressure.
    await get_redis_client().delete("cache:tag:v1:test-tag")
    result = await fn(None, value=1)

    assert result["call"] == 3


async def test_concurrent_misses_share_one_load():
    fn, calls = _counting_fn()

    results = await asyncio.gather(*(fn(None, value=1) for _ in range(10)))

    assert calls["n"] == 1
    assert all(r == results[0] for r in results)


async def test_exceptions_are_not_cached():
    calls = {"n": 0}

    @query_cache.cached(name="test.boom", ttl_seconds=60, tags=lambda a: ["test-tag"])
    async def boom(db) -> dict:
        calls["n"] += 1
        raise ValueError("nope")

    for _ in range(2):
        with pytest.raises(ValueError):
            await boom(None)
    assert calls["n"] == 2


async def test_falls_back_to_db_when_redis_is_down(monkeypatch):
    class BrokenRedis:
        async def mget(self, *_a, **_kw):
            raise ConnectionError("redis down")

    monkeypatch.setattr(query_cache, "get_redis_client", lambda: BrokenRedis())
    fn, calls = _counting_fn()

    assert (await fn(None, value=1))["value"] == 1
    assert (await fn(None, value=1))["value"] == 1
    assert calls["n"] == 2


async def test_kill_switch_bypasses_cache(monkeypatch):
    monkeypatch.setattr(get_settings(), "query_cache_enabled", False)
    fn, calls = _counting_fn()

    await fn(None, value=1)
    await fn(None, value=1)

    assert calls["n"] == 2


async def test_committing_a_catalog_write_invalidates_public_endpoint(
    client: AsyncClient, db_session: AsyncSession
):
    category = StyleCategory(slug="knotless", name_en="Knotless", display_order=0)
    db_session.add(category)
    await db_session.commit()

    first = await client.get(CATEGORIES_URL)
    assert [c["name"] for c in first.json()["data"]] == ["Knotless"]

    category.name_en = "Knotless Braids"
    await db_session.commit()

    second = await client.get(CATEGORIES_URL)
    assert [c["name"] for c in second.json()["data"]] == ["Knotless Braids"]


async def test_bulk_update_statement_invalidates(client: AsyncClient, db_session: AsyncSession):
    db_session.add(StyleCategory(slug="twists", name_en="Twists", display_order=0))
    await db_session.commit()
    await client.get(CATEGORIES_URL)

    await db_session.execute(update(StyleCategory).values(name_en="Senegalese Twists"))
    await db_session.commit()

    response = await client.get(CATEGORIES_URL)
    assert [c["name"] for c in response.json()["data"]] == ["Senegalese Twists"]


async def test_rolled_back_write_does_not_invalidate(db_session: AsyncSession):
    await query_cache.bump_tags([CATALOG])
    before = await _tag_token(CATALOG)

    db_session.add(StyleCategory(slug="locs", name_en="Locs", display_order=0))
    await db_session.flush()
    await db_session.rollback()

    assert await _tag_token(CATALOG) == before


async def test_irrelevant_user_update_keeps_braider_listings_cached(db_session: AsyncSession):
    user, _ = await create_user_with_token(db_session, user_type=UserType.BRAIDER)
    await query_cache.bump_tags([BRAIDERS, CUSTOMER_NAMES])
    before = (await _tag_token(BRAIDERS), await _tag_token(CUSTOMER_NAMES))

    user.stripe_customer_id = "cus_123"
    await db_session.commit()
    assert (await _tag_token(BRAIDERS), await _tag_token(CUSTOMER_NAMES)) == before

    user.is_active = False
    await db_session.commit()
    assert await _tag_token(BRAIDERS) != before[0]
    assert await _tag_token(CUSTOMER_NAMES) == before[1]


async def test_customer_rename_only_invalidates_names(db_session: AsyncSession):
    customer, _ = await create_user_with_token(db_session, user_type=UserType.CUSTOMER)
    await query_cache.bump_tags([BRAIDERS, CUSTOMER_NAMES])
    braiders_before = await _tag_token(BRAIDERS)
    names_before = await _tag_token(CUSTOMER_NAMES)

    customer.first_name = "Ada"
    await db_session.commit()

    assert await _tag_token(BRAIDERS) == braiders_before
    assert await _tag_token(CUSTOMER_NAMES) != names_before
