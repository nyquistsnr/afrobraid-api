import math
import random
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.cache_tags import BRAIDER_ACCOUNTS, BRAIDERS, CATALOG, braider_tag
from app.core.exceptions import (
    BraiderNotFoundError,
    InvalidSearchDateRangeError,
    InvalidSearchLocationError,
    InvalidSearchPriceRangeError,
    InvalidSearchRatingRangeError,
    InvalidTrendingLocationError,
)
from app.core.i18n import localize_field
from app.core.pagination import PaginatedData, PaginationMeta, PaginationParams
from app.core.query_cache import cached
from app.modules.braiders.discovery import repository as discovery_repo
from app.modules.braiders.discovery.schemas import (
    BraiderDetailResponse,
    BraiderLocationResponse,
    BraiderOfferedStyleResponse,
    BraiderSearchItemResponse,
    BraiderStyleAddonPublicResponse,
    BraiderStyleVariationPublicResponse,
    MatchedStyleResponse,
    PortfolioImagePublicResponse,
)
from app.modules.braiders.offerings import repository as offerings_repo
from app.modules.braiders.offerings.models import BraiderStyle
from app.modules.braiders.portfolio import repository as portfolio_repo
from app.modules.braiders.service_location import repository as service_location_repo
from app.modules.braiders.service_location.models import BraiderServiceLocation, LocationType
from app.modules.styles import repository as styles_repo
from app.modules.styles.models import Style

_MAX_DATE_RANGE_DAYS = 90
_MAX_SEARCH_STYLES = 5
# Applied when lat/lng are given but radius_km isn't, so "near me" searches
# can't surface braiders continents away just because nothing closer matched.
_DEFAULT_SEARCH_RADIUS_KM = 100
_EARTH_RADIUS_M = 6_371_000
# How far back a braider counts as "new" (New Braiders list, and the
# recency boost in Recommended For You).
_NEW_BRAIDER_WINDOW_DAYS = 30
# Rolling window of recent booking activity that "Trending" ranks on.
_TRENDING_WINDOW_DAYS = 30
# Max jitter offset for HOME_STUDIO/mobile-only coordinates - close enough to
# be useful on a map, far enough not to pinpoint a home address.
_HOME_LOCATION_FUZZ_RADIUS_M = 400

# Listings change whenever any braider (or the style catalogue) does - those
# writes invalidate them instantly (app.core.cache_tags), so the TTL is only
# the backstop for time-window drift (the "new" window) and booking counts
# (trending), which deliberately don't invalidate.
_LISTING_CACHE_TTL_SECONDS = 10 * 60
_TRENDING_CACHE_TTL_SECONDS = 5 * 60
_DETAIL_CACHE_TTL_SECONDS = 60 * 60


def _listing_tags(_args) -> list[str]:
    return [CATALOG, BRAIDERS, BRAIDER_ACCOUNTS]


def _fuzz_coordinates(lat: Decimal, lng: Decimal) -> tuple[Decimal, Decimal]:
    """Offsets a coordinate by up to `_HOME_LOCATION_FUZZ_RADIUS_M` meters in a
    random direction, freshly drawn on every call - the same braider's fuzzed
    point moves between cache fills instead of staying fixed forever, so
    repeated queries can't be used to triangulate the real one. (Discovery
    responses are cached, so within one cache entry's lifetime the point is
    stable - which only makes averaging harder, never easier.) The sqrt on the radial draw makes
    the offset uniform over the disc's area instead of clustering near the true
    point."""
    u1 = random.random()
    u2 = random.random()

    radius_m = _HOME_LOCATION_FUZZ_RADIUS_M * math.sqrt(u1)
    bearing = 2 * math.pi * u2

    lat_rad = math.radians(float(lat))
    delta_lat_deg = math.degrees((radius_m * math.cos(bearing)) / _EARTH_RADIUS_M)
    delta_lng_deg = math.degrees(
        (radius_m * math.sin(bearing)) / (_EARTH_RADIUS_M * math.cos(lat_rad))
    )

    return (
        Decimal(str(round(float(lat) + delta_lat_deg, 6))),
        Decimal(str(round(float(lng) + delta_lng_deg, 6))),
    )


async def _paginate_rows(db: AsyncSession, stmt, params: PaginationParams):
    """Same COUNT/LIMIT/OFFSET shape as `app.core.pagination.paginate`, but
    returns raw `Row`s instead of `.scalars()` - the search query selects
    multiple joined entities per row (profile, location, distance, ...),
    and `.scalars()` would silently keep only the first column."""
    total_items = (
        await db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    ) or 0

    result = await db.execute(stmt.limit(params.page_size).offset(params.offset))
    rows = result.all()

    total_pages = -(-total_items // params.page_size) if total_items else 0
    meta = PaginationMeta(
        page=params.page,
        page_size=params.page_size,
        total_items=total_items,
        total_pages=total_pages,
        has_next=params.page < total_pages,
        has_previous=params.page > 1,
    )
    return rows, meta


def _to_matched_style_response(
    braider_style: BraiderStyle, style: Style, locale: str
) -> MatchedStyleResponse:
    return MatchedStyleResponse(
        style_id=style.id,
        name=localize_field(style, "name", locale) or style.name_en,
        base_price=braider_style.base_price,
        duration_minutes=braider_style.duration_minutes,
    )


def _to_location_response(
    location: BraiderServiceLocation | None,
) -> BraiderLocationResponse | None:
    if location is None:
        return None
    show_exact_address = location.location_type == LocationType.SALON

    latitude, longitude = location.latitude, location.longitude
    if not show_exact_address and latitude is not None and longitude is not None:
        latitude, longitude = _fuzz_coordinates(latitude, longitude)

    return BraiderLocationResponse(
        location_type=location.location_type,
        salon_name=location.salon_name if show_exact_address else None,
        address_line1=location.address_line1 if show_exact_address else None,
        address_line2=location.address_line2 if show_exact_address else None,
        postal_code=location.postal_code if show_exact_address else None,
        city=location.city,
        country=location.country,
        latitude=latitude,
        longitude=longitude,
        offers_mobile=location.offers_mobile,
        travel_radius_km=location.travel_radius_km,
        travel_fee=location.travel_fee,
    )


async def _rows_to_items(
    db: AsyncSession,
    rows,
    *,
    locale: str,
    resolved_style_id: uuid.UUID | None = None,
) -> list[BraiderSearchItemResponse]:
    """Shared row -> response mapping for every braider-list endpoint
    (search, new, recommended, trending, top-rated). All of them select
    `(BraiderProfile, BraiderServiceLocation, distance_km, ...)` as the first
    three columns - `resolved_style_id` only applies to search, where two
    extra columns (BraiderStyle, Style) are appended per row."""
    braider_ids = [row.BraiderProfile.id for row in rows]
    covers = await discovery_repo.list_cover_photos(db, braider_ids)
    offered_styles_by_braider = await discovery_repo.list_offered_styles_for_braiders(
        db, braider_ids
    )

    items = []
    for row in rows:
        profile = row.BraiderProfile
        location = row.BraiderServiceLocation
        cover = covers.get(profile.id)

        matched_style = None
        if resolved_style_id is not None:
            matched_style = _to_matched_style_response(row.BraiderStyle, row.Style, locale)

        # Full menu (capped), so the frontend always has something to show even
        # when the search wasn't filtered to one style (matched_style is null then).
        styles = [
            _to_matched_style_response(braider_style, style, locale)
            for braider_style, style in offered_styles_by_braider.get(profile.id, [])
        ][:_MAX_SEARCH_STYLES]

        items.append(
            BraiderSearchItemResponse(
                id=profile.id,
                business_name=profile.business_name,
                logo_url=storage.build_public_url(profile.logo_object_key)
                if profile.logo_object_key
                else None,
                location=_to_location_response(location),
                distance_km=round(row.distance_km, 2) if row.distance_km is not None else None,
                cover_photo_url=storage.build_public_url(cover.object_key) if cover else None,
                matched_style=matched_style,
                styles=styles,
                rating=profile.average_rating,
            )
        )

    return items


@cached(name="braiders.search_braiders", ttl_seconds=_LISTING_CACHE_TTL_SECONDS, tags=_listing_tags)
async def search_braiders(
    db: AsyncSession,
    *,
    lat: float | None,
    lng: float | None,
    radius_km: float | None,
    style_id: uuid.UUID | None,
    style_slug: str | None,
    search: str | None,
    date_from: date | None,
    date_to: date | None,
    min_amount: Decimal | None,
    max_amount: Decimal | None,
    min_rate: Decimal | None,
    max_rate: Decimal | None,
    country_code: str | None,
    is_mobile: bool | None,
    params: PaginationParams,
    locale: str,
) -> PaginatedData[BraiderSearchItemResponse]:
    if (lat is None) != (lng is None):
        raise InvalidSearchLocationError()

    if (date_from is None) != (date_to is None):
        raise InvalidSearchDateRangeError(max_days=_MAX_DATE_RANGE_DAYS)
    if (
        date_from is not None
        and date_to is not None
        and (date_to < date_from or (date_to - date_from).days > _MAX_DATE_RANGE_DAYS)
    ):
        raise InvalidSearchDateRangeError(max_days=_MAX_DATE_RANGE_DAYS)

    if min_amount is not None and max_amount is not None and max_amount < min_amount:
        raise InvalidSearchPriceRangeError()

    if min_rate is not None and max_rate is not None and max_rate < min_rate:
        raise InvalidSearchRatingRangeError()

    resolved_radius_km = radius_km
    if lat is not None and lng is not None and resolved_radius_km is None:
        resolved_radius_km = _DEFAULT_SEARCH_RADIUS_KM

    resolved_style_id = style_id
    if resolved_style_id is None and style_slug:
        style = await styles_repo.get_style_by_slug(db, style_slug)
        resolved_style_id = style.id if style else uuid.uuid4()  # no match -> empty page

    stmt = discovery_repo.build_search_stmt(
        lat=lat,
        lng=lng,
        radius_km=resolved_radius_km,
        style_id=resolved_style_id,
        search=search,
        date_from=date_from,
        date_to=date_to,
        min_amount=min_amount,
        max_amount=max_amount,
        min_rate=min_rate,
        max_rate=max_rate,
        country_code=country_code,
        is_mobile=is_mobile,
    )
    rows, meta = await _paginate_rows(db, stmt, params)
    items = await _rows_to_items(db, rows, locale=locale, resolved_style_id=resolved_style_id)
    return PaginatedData(items=items, pagination=meta)


@cached(name="braiders.list_new_braiders", ttl_seconds=_LISTING_CACHE_TTL_SECONDS, tags=_listing_tags)
async def list_new_braiders(
    db: AsyncSession,
    *,
    lat: float | None,
    lng: float | None,
    radius_km: float | None,
    country_code: str | None,
    params: PaginationParams,
    locale: str,
) -> PaginatedData[BraiderSearchItemResponse]:
    if (lat is None) != (lng is None):
        raise InvalidSearchLocationError()

    since = datetime.now(UTC) - timedelta(days=_NEW_BRAIDER_WINDOW_DAYS)
    stmt = discovery_repo.build_new_braiders_stmt(
        since=since, lat=lat, lng=lng, radius_km=radius_km, country_code=country_code
    )
    rows, meta = await _paginate_rows(db, stmt, params)
    items = await _rows_to_items(db, rows, locale=locale)
    return PaginatedData(items=items, pagination=meta)


@cached(name="braiders.list_top_rated_braiders", ttl_seconds=_LISTING_CACHE_TTL_SECONDS, tags=_listing_tags)
async def list_top_rated_braiders(
    db: AsyncSession,
    *,
    lat: float | None,
    lng: float | None,
    radius_km: float | None,
    country_code: str | None,
    params: PaginationParams,
    locale: str,
) -> PaginatedData[BraiderSearchItemResponse]:
    if (lat is None) != (lng is None):
        raise InvalidSearchLocationError()

    stmt = discovery_repo.build_top_rated_stmt(
        lat=lat, lng=lng, radius_km=radius_km, country_code=country_code
    )
    rows, meta = await _paginate_rows(db, stmt, params)
    items = await _rows_to_items(db, rows, locale=locale)
    return PaginatedData(items=items, pagination=meta)


@cached(name="braiders.list_trending_braiders", ttl_seconds=_TRENDING_CACHE_TTL_SECONDS, tags=_listing_tags)
async def list_trending_braiders(
    db: AsyncSession,
    *,
    lat: float | None,
    lng: float | None,
    radius_km: float | None,
    country_code: str | None,
    params: PaginationParams,
    locale: str,
) -> PaginatedData[BraiderSearchItemResponse]:
    if (lat is None) != (lng is None):
        raise InvalidSearchLocationError()
    if not country_code and lat is None:
        raise InvalidTrendingLocationError()

    since = datetime.now(UTC) - timedelta(days=_TRENDING_WINDOW_DAYS)
    stmt = discovery_repo.build_trending_stmt(
        since=since, lat=lat, lng=lng, radius_km=radius_km, country_code=country_code
    )
    rows, meta = await _paginate_rows(db, stmt, params)
    items = await _rows_to_items(db, rows, locale=locale)
    return PaginatedData(items=items, pagination=meta)


@cached(name="braiders.list_recommended_braiders", ttl_seconds=_LISTING_CACHE_TTL_SECONDS, tags=_listing_tags)
async def list_recommended_braiders(
    db: AsyncSession,
    *,
    lat: float | None,
    lng: float | None,
    radius_km: float | None,
    country_code: str | None,
    params: PaginationParams,
    locale: str,
) -> PaginatedData[BraiderSearchItemResponse]:
    if (lat is None) != (lng is None):
        raise InvalidSearchLocationError()

    new_since = datetime.now(UTC) - timedelta(days=_NEW_BRAIDER_WINDOW_DAYS)
    stmt = discovery_repo.build_recommended_stmt(
        new_since=new_since, lat=lat, lng=lng, radius_km=radius_km, country_code=country_code
    )
    rows, meta = await _paginate_rows(db, stmt, params)
    items = await _rows_to_items(db, rows, locale=locale)
    return PaginatedData(items=items, pagination=meta)


@cached(
    name="braiders.get_braider_detail",
    ttl_seconds=_DETAIL_CACHE_TTL_SECONDS,
    tags=lambda a: [CATALOG, BRAIDER_ACCOUNTS, braider_tag(a["braider_id"])],
)
async def get_braider_detail(
    db: AsyncSession, braider_id: uuid.UUID, *, locale: str
) -> BraiderDetailResponse:
    profile = await discovery_repo.get_visible_profile_by_id(db, braider_id)
    if profile is None:
        raise BraiderNotFoundError()

    location = await service_location_repo.get_by_braider_id(db, profile.id)
    portfolio_images = await portfolio_repo.list_images(db, profile.id)
    offered = await discovery_repo.list_offered_styles(db, profile.id)

    styles_out = []
    for braider_style, style in offered:
        variations = await offerings_repo.list_braider_style_variations(db, braider_style.id)
        addons = await offerings_repo.list_braider_style_addons(db, braider_style.id)
        variation_rows = await discovery_repo.get_style_variations_by_ids(
            db, [v.style_variation_id for v in variations]
        )
        addon_rows = await discovery_repo.get_addons_by_ids(db, [a.addon_id for a in addons])

        styles_out.append(
            BraiderOfferedStyleResponse(
                style_id=style.id,
                slug=style.slug,
                name=localize_field(style, "name", locale) or style.name_en,
                base_price=braider_style.base_price,
                duration_minutes=braider_style.duration_minutes,
                variations=[
                    BraiderStyleVariationPublicResponse(
                        id=v.id,
                        name=(
                            localize_field(variation_rows[v.style_variation_id], "name", locale)
                            or variation_rows[v.style_variation_id].name_en
                        ),
                        price=v.price,
                    )
                    for v in variations
                    if v.style_variation_id in variation_rows
                ],
                addons=[
                    BraiderStyleAddonPublicResponse(
                        id=a.id,
                        name=(
                            localize_field(addon_rows[a.addon_id], "name", locale)
                            or addon_rows[a.addon_id].name_en
                        ),
                        price=a.price,
                        is_required=a.is_required,
                    )
                    for a in addons
                    if a.addon_id in addon_rows
                ],
            )
        )

    return BraiderDetailResponse(
        id=profile.id,
        business_name=profile.business_name,
        logo_url=storage.build_public_url(profile.logo_object_key)
        if profile.logo_object_key
        else None,
        bio=localize_field(profile, "bio", locale),
        gender=profile.gender,
        location=_to_location_response(location),
        portfolio=[
            PortfolioImagePublicResponse(
                id=image.id,
                url=storage.build_public_url(image.object_key),
                caption=localize_field(image, "caption", locale),
                position=image.position,
            )
            for image in portfolio_images
        ],
        styles=styles_out,
        rating=profile.average_rating,
    )
