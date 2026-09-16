import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.currency import Currency
from app.core.exceptions import (
    BookingCalculationAlreadyUsedError,
    BookingCalculationExpiredError,
    BookingCalculationNotFoundError,
    BraiderCountryMissingError,
    BraiderNotFoundError,
    BraiderStyleAddonInvalidError,
    BraiderStyleDurationMissingError,
    BraiderStyleNotFoundError,
    BraiderStyleVariationInvalidError,
    ClientLocationMissingError,
    MobileLocationOutOfRangeError,
    MobileServiceNotOfferedError,
    StyleNotFoundError,
)
from app.core.i18n import localize_field, t
from app.modules.bookings.calculations import repository as calculations_repo
from app.modules.bookings.calculations.models import BookingCalculation, BookingCalculationStatus
from app.modules.bookings.calculations.schemas import (
    BookingCalculationInput,
    BookingCalculationLineResponse,
    BookingCalculationPreviewResponse,
    BookingCalculationResponse,
    BookingCalculationUpdateRequest,
)
from app.modules.bookings.enums import BookingItemType
from app.modules.bookings.pricing import (
    PricedLine,
    PricingComponent,
    PricingResult,
    calculate_booking_price,
)
from app.modules.braiders.discovery import repository as discovery_repo
from app.modules.braiders.offerings import repository as offerings_repo
from app.modules.braiders.offerings.models import BraiderStyle
from app.modules.braiders.service_location import repository as service_location_repo
from app.modules.platform_settings.cache import get_effective_settings
from app.modules.styles import repository as styles_repo
from app.modules.styles.models import AddOn, Style, StyleVariation
from app.shared.geo import calculate_distance_km

settings = get_settings()

# (braider_style_addon_id, addon_id, addon row or None, price, is_required)
_AddonEntry = tuple[uuid.UUID, uuid.UUID, AddOn | None, Decimal, bool]


@dataclass(frozen=True)
class ResolvedInput:
    braider_id: uuid.UUID
    braider_user_id: uuid.UUID
    style: Style
    braider_style: BraiderStyle
    variation: StyleVariation | None
    variation_price: Decimal | None
    braider_style_variation_id: uuid.UUID | None
    addon_entries: list[_AddonEntry]
    is_mobile: bool
    client_address: str | None
    client_latitude: Decimal | None
    client_longitude: Decimal | None
    travel_fee: Decimal | None
    country: str


async def resolve_calculation_input(db: AsyncSession, data: BookingCalculationInput) -> ResolvedInput:
    """Re-derives and re-authorizes a calculator input from scratch - visible
    braider, active braider_style, valid variation/addons, mobile radius
    check, country resolution. Public because `bookings/service.py` reuses
    this exact path (constructing a `BookingCalculationInput` from a
    persisted `BookingCalculation`'s stored ids) to re-validate a DRAFT
    calculation at booking-creation time rather than trusting the frozen
    snapshot - a braider's location, radius, or menu may have changed since
    the quote was created."""
    profile = await discovery_repo.get_visible_profile_by_id(db, data.braider_id)
    if profile is None:
        raise BraiderNotFoundError()

    braider_style = await offerings_repo.get_braider_style_by_style_id(
        db, profile.id, data.style_id
    )
    if braider_style is None or not braider_style.is_active:
        raise BraiderStyleNotFoundError()
    if braider_style.duration_minutes is None:
        raise BraiderStyleDurationMissingError()

    style = await styles_repo.get_style_by_id(db, data.style_id)
    if style is None:
        raise StyleNotFoundError()

    variation: StyleVariation | None = None
    variation_price: Decimal | None = None
    if data.braider_style_variation_id is not None:
        braider_variations = await offerings_repo.list_braider_style_variations(
            db, braider_style.id
        )
        matched = next(
            (v for v in braider_variations if v.id == data.braider_style_variation_id), None
        )
        if matched is None:
            raise BraiderStyleVariationInvalidError()
        variation = await styles_repo.get_style_variation_by_id(db, matched.style_variation_id)
        if variation is None:
            raise BraiderStyleVariationInvalidError()
        variation_price = matched.price

    requested_addon_ids = set(data.braider_style_addon_ids)
    braider_addons = await offerings_repo.list_braider_style_addons(db, braider_style.id)
    by_id = {a.id: a for a in braider_addons}
    if not requested_addon_ids.issubset(by_id.keys()):
        raise BraiderStyleAddonInvalidError()

    # Required add-ons are always included, even if the client didn't select
    # them - they aren't deselectable.
    selected_rows = [
        row for row in braider_addons if row.id in requested_addon_ids or row.is_required
    ]
    addon_catalog = await discovery_repo.get_addons_by_ids(
        db, [row.addon_id for row in selected_rows]
    )
    addon_entries: list[_AddonEntry] = [
        (row.id, row.addon_id, addon_catalog.get(row.addon_id), row.price, row.is_required)
        for row in selected_rows
    ]

    location = await service_location_repo.get_by_braider_id(db, profile.id)
    if location is None or location.country is None:
        raise BraiderCountryMissingError()

    travel_fee: Decimal | None = None
    if data.is_mobile:
        if not location.offers_mobile:
            raise MobileServiceNotOfferedError()
        if data.client_latitude is None or data.client_longitude is None:
            raise ClientLocationMissingError()
        travel_fee = location.travel_fee
        if (
            location.travel_radius_km is not None
            and location.latitude is not None
            and location.longitude is not None
        ):
            distance = calculate_distance_km(
                lat1=location.latitude,
                lon1=location.longitude,
                lat2=data.client_latitude,
                lon2=data.client_longitude,
            )
            if distance > location.travel_radius_km:
                raise MobileLocationOutOfRangeError()

    client_address = data.client_address if data.is_mobile else None
    client_latitude = data.client_latitude if data.is_mobile else None
    client_longitude = data.client_longitude if data.is_mobile else None

    return ResolvedInput(
        braider_id=profile.id,
        braider_user_id=profile.user_id,
        style=style,
        braider_style=braider_style,
        variation=variation,
        variation_price=variation_price,
        braider_style_variation_id=data.braider_style_variation_id,
        addon_entries=addon_entries,
        is_mobile=data.is_mobile,
        client_address=client_address,
        client_latitude=client_latitude,
        client_longitude=client_longitude,
        travel_fee=travel_fee,
        country=location.country,
    )


async def price_resolved_input(
    resolved: ResolvedInput,
    db: AsyncSession,
    redis: Redis,
    *,
    starts_at: datetime | None = None,
) -> PricingResult:
    effective_settings = await get_effective_settings(db, redis, country=resolved.country)

    if resolved.variation is not None:
        service_component = PricingComponent(
            item_type=BookingItemType.VARIATION,
            amount=resolved.variation_price or Decimal("0.00"),
            source_style_id=resolved.style.id,
            source_style_variation_id=resolved.variation.id,
        )
    else:
        service_component = PricingComponent(
            item_type=BookingItemType.SERVICE,
            amount=resolved.braider_style.base_price,
            source_style_id=resolved.style.id,
        )

    addon_components = [
        PricingComponent(
            item_type=BookingItemType.ADDON,
            amount=price,
            is_required=is_required,
            source_addon_id=addon_id,
            source_braider_style_addon_id=braider_style_addon_id,
        )
        for braider_style_addon_id, addon_id, _addon, price, is_required in resolved.addon_entries
    ]

    travel_component = None
    if resolved.is_mobile:
        travel_component = PricingComponent(
            item_type=BookingItemType.TRAVEL, amount=resolved.travel_fee or Decimal("0.00")
        )

    return calculate_booking_price(
        service_component=service_component,
        addon_components=addon_components,
        travel_component=travel_component,
        currency=Currency.EUR,
        settings=effective_settings,
        now=datetime.now(UTC),
        starts_at=starts_at,
    )


def _hash_ip(ip: str) -> str:
    return hashlib.sha256((settings.client_ip_hash_salt + ip).encode()).hexdigest()


def _line_name(
    line: PricedLine,
    *,
    style: Style,
    variation: StyleVariation | None,
    addon_catalog: dict[uuid.UUID, AddOn],
    locale: str,
) -> str | None:
    if line.item_type == BookingItemType.SERVICE:
        return localize_field(style, "name", locale) or style.name_en
    if line.item_type == BookingItemType.VARIATION and variation is not None:
        return localize_field(variation, "name", locale) or variation.name_en
    if line.item_type == BookingItemType.ADDON and line.source_addon_id is not None:
        addon = addon_catalog.get(line.source_addon_id)
        return (localize_field(addon, "name", locale) or addon.name_en) if addon else None
    if line.item_type == BookingItemType.TRAVEL:
        return t("booking_calculation.travel_fee_label", locale)
    if line.item_type == BookingItemType.PLATFORM_FEE:
        return t("booking_calculation.platform_fee_label", locale)
    if line.item_type == BookingItemType.VAT_SERVICE:
        return t("booking_calculation.vat_service_label", locale)
    if line.item_type == BookingItemType.VAT_PLATFORM_FEE:
        return t("booking_calculation.vat_platform_fee_label", locale)
    return None


def _pricing_result_items(
    result: PricingResult,
    *,
    style: Style,
    variation: StyleVariation | None,
    addon_catalog: dict[uuid.UUID, AddOn],
    locale: str,
) -> list[BookingCalculationLineResponse]:
    return [
        BookingCalculationLineResponse(
            item_type=line.item_type,
            name=_line_name(
                line, style=style, variation=variation, addon_catalog=addon_catalog, locale=locale
            ),
            unit_amount=line.amount,
            line_amount=line.amount,
            is_required=line.is_required,
        )
        for line in result.lines
    ]


def _to_preview_response(
    resolved: ResolvedInput,
    result: PricingResult,
    *,
    addon_catalog: dict[uuid.UUID, AddOn],
    locale: str,
) -> BookingCalculationPreviewResponse:
    return BookingCalculationPreviewResponse(
        currency=result.currency,
        braider_id=resolved.braider_id,
        style_id=resolved.style.id,
        style_name=localize_field(resolved.style, "name", locale) or resolved.style.name_en,
        duration_minutes=resolved.braider_style.duration_minutes,
        is_mobile=resolved.is_mobile,
        client_address=resolved.client_address,
        client_latitude=resolved.client_latitude,
        client_longitude=resolved.client_longitude,
        items=_pricing_result_items(
            result,
            style=resolved.style,
            variation=resolved.variation,
            addon_catalog=addon_catalog,
            locale=locale,
        ),
        service_subtotal=result.service_subtotal,
        travel_fee=result.travel_fee,
        subtotal=result.subtotal,
        platform_fee=result.platform_fee,
        vat_on_service=result.vat_on_service,
        vat_on_platform_fee=result.vat_on_platform_fee,
        vat_rate=result.vat_service_value,
        country=resolved.country,
        vat_total=result.vat_total,
        total=result.total,
        deposit_amount=result.deposit_amount,
        balance_amount=result.balance_amount,
        deposit_note=t("booking_calculation.deposit_note", locale),
    )


def _to_response(
    calculation: BookingCalculation, *, preview: BookingCalculationPreviewResponse
) -> BookingCalculationResponse:
    return BookingCalculationResponse(
        **preview.model_dump(),
        id=calculation.id,
        status=calculation.status,
        expires_at=calculation.expires_at,
    )


async def _stored_preview_response(
    db: AsyncSession, calculation: BookingCalculation, *, locale: str
) -> BookingCalculationPreviewResponse:
    """Reconstructs the display breakdown from the persisted snapshot -
    used by GET, which deliberately does not recompute (the calculation's
    numbers are frozen at creation/last PATCH, not re-priced live)."""
    addons = await calculations_repo.list_addons(db, calculation.id)
    style = await styles_repo.get_style_by_id(db, calculation.style_id)
    variation = (
        await styles_repo.get_style_variation_by_id(db, calculation.style_variation_id)
        if calculation.style_variation_id is not None
        else None
    )
    addon_catalog = await discovery_repo.get_addons_by_ids(db, [a.addon_id for a in addons])

    items: list[BookingCalculationLineResponse] = []

    addons_total = sum((a.price for a in addons), Decimal("0.00"))
    service_amount = calculation.service_subtotal - addons_total
    if calculation.style_variation_id is not None and variation is not None:
        items.append(
            BookingCalculationLineResponse(
                item_type=BookingItemType.VARIATION,
                name=localize_field(variation, "name", locale) or variation.name_en,
                unit_amount=service_amount,
                line_amount=service_amount,
            )
        )
    else:
        items.append(
            BookingCalculationLineResponse(
                item_type=BookingItemType.SERVICE,
                name=(localize_field(style, "name", locale) or style.name_en) if style else None,
                unit_amount=service_amount,
                line_amount=service_amount,
            )
        )

    for addon_row in sorted(addons, key=lambda a: not a.is_required):
        addon = addon_catalog.get(addon_row.addon_id)
        items.append(
            BookingCalculationLineResponse(
                item_type=BookingItemType.ADDON,
                name=(localize_field(addon, "name", locale) or addon.name_en) if addon else None,
                unit_amount=addon_row.price,
                line_amount=addon_row.price,
                is_required=addon_row.is_required,
            )
        )

    if calculation.travel_fee > 0:
        items.append(
            BookingCalculationLineResponse(
                item_type=BookingItemType.TRAVEL,
                name=t("booking_calculation.travel_fee_label", locale),
                unit_amount=calculation.travel_fee,
                line_amount=calculation.travel_fee,
            )
        )

    items.append(
        BookingCalculationLineResponse(
            item_type=BookingItemType.PLATFORM_FEE,
            name=t("booking_calculation.platform_fee_label", locale),
            unit_amount=calculation.platform_fee,
            line_amount=calculation.platform_fee,
        )
    )
    items.append(
        BookingCalculationLineResponse(
            item_type=BookingItemType.VAT_SERVICE,
            name=t("booking_calculation.vat_service_label", locale),
            unit_amount=calculation.vat_on_service,
            line_amount=calculation.vat_on_service,
        )
    )
    items.append(
        BookingCalculationLineResponse(
            item_type=BookingItemType.VAT_PLATFORM_FEE,
            name=t("booking_calculation.vat_platform_fee_label", locale),
            unit_amount=calculation.vat_on_platform_fee,
            line_amount=calculation.vat_on_platform_fee,
        )
    )

    return BookingCalculationPreviewResponse(
        currency=calculation.currency,
        braider_id=calculation.braider_id,
        style_id=calculation.style_id,
        style_name=(localize_field(style, "name", locale) or style.name_en) if style else "",
        duration_minutes=calculation.duration_minutes,
        is_mobile=calculation.is_mobile,
        items=items,
        service_subtotal=calculation.service_subtotal,
        travel_fee=calculation.travel_fee,
        subtotal=calculation.subtotal,
        platform_fee=calculation.platform_fee,
        vat_on_service=calculation.vat_on_service,
        vat_on_platform_fee=calculation.vat_on_platform_fee,
        vat_rate=calculation.vat_service_value,
        country=calculation.country,
        vat_total=calculation.vat_total,
        total=calculation.total,
        deposit_amount=calculation.deposit_amount,
        balance_amount=calculation.balance_amount,
        deposit_note=t("booking_calculation.deposit_note", locale),
    )


async def preview(
    db: AsyncSession, redis: Redis, *, data: BookingCalculationInput, locale: str
) -> BookingCalculationPreviewResponse:
    resolved = await resolve_calculation_input(db, data)
    result = await price_resolved_input(resolved, db, redis)
    addon_catalog = {
        addon_id: addon for _, addon_id, addon, _, _ in resolved.addon_entries if addon is not None
    }
    return _to_preview_response(resolved, result, addon_catalog=addon_catalog, locale=locale)


async def create_calculation(
    db: AsyncSession,
    redis: Redis,
    *,
    data: BookingCalculationInput,
    locale: str,
    user_id: uuid.UUID | None,
    client_ip: str | None,
) -> BookingCalculationResponse:
    resolved = await resolve_calculation_input(db, data)
    result = await price_resolved_input(resolved, db, redis)

    expires_at = datetime.now(UTC) + timedelta(hours=settings.booking_calculation_ttl_hours)

    calculation = await calculations_repo.create_calculation(
        db,
        braider_id=resolved.braider_id,
        braider_style_id=resolved.braider_style.id,
        style_id=resolved.style.id,
        style_variation_id=resolved.variation.id if resolved.variation else None,
        braider_style_variation_id=resolved.braider_style_variation_id,
        is_mobile=resolved.is_mobile,
        country=resolved.country,
        client_address=resolved.client_address,
        client_latitude=resolved.client_latitude,
        client_longitude=resolved.client_longitude,
        currency=result.currency,
        duration_minutes=resolved.braider_style.duration_minutes,
        service_subtotal=result.service_subtotal,
        travel_fee=result.travel_fee,
        subtotal=result.subtotal,
        platform_fee_type=result.platform_fee_type,
        platform_fee_value=result.platform_fee_value,
        platform_fee=result.platform_fee,
        vat_service_type=result.vat_service_type,
        vat_service_value=result.vat_service_value,
        vat_on_service=result.vat_on_service,
        vat_platform_fee_type=result.vat_platform_fee_type,
        vat_platform_fee_value=result.vat_platform_fee_value,
        vat_on_platform_fee=result.vat_on_platform_fee,
        vat_total=result.vat_total,
        total=result.total,
        deposit_type=result.deposit_type,
        deposit_value=result.deposit_value,
        deposit_amount=result.deposit_amount,
        balance_amount=result.balance_amount,
        expires_at=expires_at,
        created_by_user_id=user_id,
        client_ip_hash=_hash_ip(client_ip) if client_ip else None,
    )

    for braider_style_addon_id, addon_id, _addon, price, is_required in resolved.addon_entries:
        await calculations_repo.add_addon(
            db,
            booking_calculation_id=calculation.id,
            braider_style_addon_id=braider_style_addon_id,
            addon_id=addon_id,
            price=price,
            is_required=is_required,
        )

    await db.commit()

    addon_catalog = {
        addon_id: addon for _, addon_id, addon, _, _ in resolved.addon_entries if addon is not None
    }
    preview_response = _to_preview_response(resolved, result, addon_catalog=addon_catalog, locale=locale)
    return _to_response(calculation, preview=preview_response)


async def get_calculation(
    db: AsyncSession, calculation_id: uuid.UUID, *, locale: str
) -> BookingCalculationResponse:
    calculation = await calculations_repo.get_calculation_by_id(db, calculation_id)
    if calculation is None:
        raise BookingCalculationNotFoundError()

    preview_response = await _stored_preview_response(db, calculation, locale=locale)
    return _to_response(calculation, preview=preview_response)


async def update_calculation(
    db: AsyncSession,
    redis: Redis,
    calculation_id: uuid.UUID,
    *,
    data: BookingCalculationUpdateRequest,
    locale: str,
) -> BookingCalculationResponse:
    calculation = await calculations_repo.get_calculation_by_id(db, calculation_id)
    if calculation is None:
        raise BookingCalculationNotFoundError()
    if calculation.status != BookingCalculationStatus.DRAFT:
        raise BookingCalculationAlreadyUsedError()
    if calculation.expires_at <= datetime.now(UTC):
        raise BookingCalculationExpiredError()

    fields_set = data.model_fields_set

    if "braider_style_addon_ids" in fields_set:
        addon_ids = data.braider_style_addon_ids or []
    else:
        existing_addons = await calculations_repo.list_addons(db, calculation.id)
        addon_ids = [a.braider_style_addon_id for a in existing_addons]

    merged_input = BookingCalculationInput(
        braider_id=data.braider_id if "braider_id" in fields_set else calculation.braider_id,
        style_id=data.style_id if "style_id" in fields_set else calculation.style_id,
        braider_style_variation_id=(
            data.braider_style_variation_id
            if "braider_style_variation_id" in fields_set
            else calculation.braider_style_variation_id
        ),
        braider_style_addon_ids=addon_ids,
        is_mobile=data.is_mobile if "is_mobile" in fields_set else calculation.is_mobile,
        client_address=data.client_address if "client_address" in fields_set else calculation.client_address,
        client_latitude=data.client_latitude if "client_latitude" in fields_set else calculation.client_latitude,
        client_longitude=data.client_longitude if "client_longitude" in fields_set else calculation.client_longitude,
    )

    resolved = await resolve_calculation_input(db, merged_input)
    result = await price_resolved_input(resolved, db, redis)

    calculation.braider_id = resolved.braider_id
    calculation.braider_style_id = resolved.braider_style.id
    calculation.style_id = resolved.style.id
    calculation.style_variation_id = resolved.variation.id if resolved.variation else None
    calculation.braider_style_variation_id = resolved.braider_style_variation_id
    calculation.is_mobile = resolved.is_mobile
    calculation.country = resolved.country
    calculation.client_address = resolved.client_address
    calculation.client_latitude = resolved.client_latitude
    calculation.client_longitude = resolved.client_longitude
    calculation.currency = result.currency
    calculation.duration_minutes = resolved.braider_style.duration_minutes
    calculation.service_subtotal = result.service_subtotal
    calculation.travel_fee = result.travel_fee
    calculation.subtotal = result.subtotal
    calculation.platform_fee_type = result.platform_fee_type
    calculation.platform_fee_value = result.platform_fee_value
    calculation.platform_fee = result.platform_fee
    calculation.vat_service_type = result.vat_service_type
    calculation.vat_service_value = result.vat_service_value
    calculation.vat_on_service = result.vat_on_service
    calculation.vat_platform_fee_type = result.vat_platform_fee_type
    calculation.vat_platform_fee_value = result.vat_platform_fee_value
    calculation.vat_on_platform_fee = result.vat_on_platform_fee
    calculation.vat_total = result.vat_total
    calculation.total = result.total
    calculation.deposit_type = result.deposit_type
    calculation.deposit_value = result.deposit_value
    calculation.deposit_amount = result.deposit_amount
    calculation.balance_amount = result.balance_amount

    await calculations_repo.delete_addons(db, calculation.id)
    for braider_style_addon_id, addon_id, _addon, price, is_required in resolved.addon_entries:
        await calculations_repo.add_addon(
            db,
            booking_calculation_id=calculation.id,
            braider_style_addon_id=braider_style_addon_id,
            addon_id=addon_id,
            price=price,
            is_required=is_required,
        )

    await db.commit()

    addon_catalog = {
        addon_id: addon for _, addon_id, addon, _, _ in resolved.addon_entries if addon is not None
    }
    preview_response = _to_preview_response(resolved, result, addon_catalog=addon_catalog, locale=locale)
    return _to_response(calculation, preview=preview_response)


async def delete_calculation(db: AsyncSession, calculation_id: uuid.UUID) -> None:
    calculation = await calculations_repo.get_calculation_by_id(db, calculation_id)
    if calculation is None:
        raise BookingCalculationNotFoundError()
    if calculation.status != BookingCalculationStatus.DRAFT:
        raise BookingCalculationAlreadyUsedError()

    await calculations_repo.delete_calculation(db, calculation)
    await db.commit()
