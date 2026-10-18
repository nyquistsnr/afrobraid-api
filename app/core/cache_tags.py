"""Which cache tags a database write invalidates.

Every cached read (see `app.core.query_cache.cached`) declares the tags it
depends on; every committed write bumps the tags its rows map to here, which
instantly orphans every cache entry built on the old data. The rules key on
table *names* and read attributes off the flushed objects, so this module
imports no models (and can't create an import cycle with them).

Tags are deliberately granular where it's cheap to be: a braider editing
their own profile only invalidates *their* detail page and the listings,
not every other braider's detail page. Anything we can't pin to a single
braider falls back to a coarse tag - over-invalidating costs a cache miss,
under-invalidating would serve stale data, so the fallback is always the
broader one.

Bookings are intentionally absent: they change constantly and only feed the
"trending" ranking, which tolerates its short TTL instead.
"""

from collections.abc import Callable
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.orm import Session

# Style catalogue: categories, styles, images, variations, add-ons.
CATALOG = "catalog"
# Every braider listing (search/new/trending/top-rated/recommended).
BRAIDERS = "braiders"
# Braider-visibility changes we can't pin to one profile id (onboarding,
# suspension, bulk updates) - every braider detail page depends on it.
BRAIDER_ACCOUNTS = "braider_accounts"
# Every public review list - only bumped by bulk review updates.
REVIEWS = "reviews"
# Display names shown next to reviews.
CUSTOMER_NAMES = "customer_names"


def braider_tag(braider_id: Any) -> str:
    return f"braider:{braider_id}"


def reviews_tag(braider_id: Any) -> str:
    return f"reviews:{braider_id}"


# A rule gets the session, the flushed object (None for a bulk
# UPDATE/DELETE/INSERT statement, where we don't know which rows changed)
# and whether it was "insert", "update" or "delete".
Rule = Callable[[Session, Any | None, str], set[str]]


def _static(*tags: str) -> Rule:
    return lambda _session, _obj, _op: set(tags)


def _per_braider(column: str) -> Rule:
    def rule(_session: Session, obj: Any | None, _op: str) -> set[str]:
        braider_id = getattr(obj, column, None) if obj is not None else None
        if braider_id is None:
            return {BRAIDERS, BRAIDER_ACCOUNTS}
        return {BRAIDERS, braider_tag(braider_id)}

    return rule


def _find_loaded(session: Session, table_name: str, pk: Any, sample: Any) -> Any | None:
    """Looks `pk` up in the session's identity map only - never queries,
    since this runs inside a flush."""
    for mapper in type(sample).registry.mappers:
        if mapper.local_table.name == table_name:
            key = mapper.identity_key_from_primary_key((pk,))
            return session.identity_map.get(key)
    return None


def _per_braider_style(session: Session, obj: Any | None, _op: str) -> set[str]:
    """Variation/add-on rows only carry `braider_style_id`. The owning
    `BraiderStyle` is almost always already loaded (the offerings service
    fetches it to check ownership), so resolve the braider from the identity
    map and only fall back to the coarse tag when it isn't."""
    if obj is not None:
        braider_style = _find_loaded(session, "braider_styles", obj.braider_style_id, obj)
        if braider_style is not None:
            return {BRAIDERS, braider_tag(braider_style.braider_id)}
    return {BRAIDERS, BRAIDER_ACCOUNTS}


_BRAIDER_VISIBLE_USER_COLUMNS = ("is_active", "user_type", "first_name", "last_name")
_NAME_COLUMNS = ("first_name", "last_name")


def _users(_session: Session, obj: Any | None, op: str) -> set[str]:
    if obj is None or op == "delete":
        return {BRAIDERS, BRAIDER_ACCOUNTS, CUSTOMER_NAMES}
    if op == "insert":
        # A brand-new user isn't in any listing or review yet - they only
        # show up once onboarding/a review row is written, which bumps then.
        return set()

    attrs = inspect(obj).attrs
    changed = {name for name in _BRAIDER_VISIBLE_USER_COLUMNS if attrs[name].history.has_changes()}
    tags: set[str] = set()
    if changed & set(_NAME_COLUMNS):
        tags.add(CUSTOMER_NAMES)
    if changed and _was_or_is_braider(attrs["user_type"].history):
        tags |= {BRAIDERS, BRAIDER_ACCOUNTS}
    return tags


def _was_or_is_braider(history: Any) -> bool:
    values = [*history.added, *history.unchanged, *history.deleted]
    if not values:
        return True  # user_type wasn't loaded - assume the broader case
    return any(getattr(v, "value", v) == "BRAIDER" for v in values)


def _reviews(_session: Session, obj: Any | None, _op: str) -> set[str]:
    if obj is None:
        return {REVIEWS}
    return {reviews_tag(obj.braider_id)}


def _braider_profiles(_session: Session, obj: Any | None, _op: str) -> set[str]:
    if obj is None:
        return {BRAIDERS, BRAIDER_ACCOUNTS}
    return {BRAIDERS, braider_tag(obj.id)}


RULES: dict[str, Rule] = {
    "style_categories": _static(CATALOG),
    "styles": _static(CATALOG),
    "style_images": _static(CATALOG),
    "style_variations": _static(CATALOG),
    "addons": _static(CATALOG),
    "braider_profiles": _braider_profiles,
    "braider_onboarding_statuses": _static(BRAIDERS, BRAIDER_ACCOUNTS),
    "braider_service_locations": _per_braider("braider_id"),
    "braider_styles": _per_braider("braider_id"),
    "braider_style_variations": _per_braider_style,
    "braider_style_addons": _per_braider_style,
    "portfolio_images": _per_braider("braider_id"),
    "braider_availability_settings": _per_braider("braider_id"),
    "braider_weekly_availability": _per_braider("braider_id"),
    "braider_availability_exceptions": _per_braider("braider_id"),
    "stripe_connect_accounts": _per_braider("braider_id"),
    "users": _users,
    "reviews": _reviews,
}


def tags_for(session: Session, table_name: str, obj: Any | None, op: str) -> set[str]:
    rule = RULES.get(table_name)
    if rule is None:
        return set()
    return rule(session, obj, op)
