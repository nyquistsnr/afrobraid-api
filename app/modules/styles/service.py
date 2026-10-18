import re
import uuid
from collections.abc import Awaitable, Callable

from arq import ArqRedis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.cache_tags import CATALOG
from app.core.config import get_settings
from app.core.exceptions import (
    AddOnNotFoundError,
    EntityInUseError,
    InvalidStyleImageUploadError,
    MaxStyleImagesReachedError,
    StyleCategoryNotFoundError,
    StyleImageNotFoundError,
    StyleNotFoundError,
    StyleVariationNotFoundError,
)
from app.core.i18n import localize_field
from app.core.pagination import PaginatedData, PaginationParams, paginate
from app.core.query_cache import cached
from app.modules.styles import repository as styles_repo
from app.modules.styles.models import (
    AddOn,
    Style,
    StyleCategory,
    StyleImage,
    StyleVariation,
    TranslationSource,
)
from app.modules.styles.schemas import (
    AddOnCreateRequest,
    AddOnPublicResponse,
    AddOnResponse,
    AddOnUpdateRequest,
    StyleCategoryCreateRequest,
    StyleCategoryPublicResponse,
    StyleCategoryResponse,
    StyleCategoryUpdateRequest,
    StyleCreateRequest,
    StyleImageConfirmRequest,
    StyleImageResponse,
    StyleImageUploadUrlRequest,
    StyleImageUploadUrlResponse,
    StylePublicResponse,
    StyleResponse,
    StyleUpdateRequest,
    StyleVariationCreateRequest,
    StyleVariationPublicResponse,
    StyleVariationResponse,
    StyleVariationUpdateRequest,
)
from app.modules.styles.tasks import TASK_TRANSLATE_STYLE_TEXT

settings = get_settings()

_MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024
_ALLOWED_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
_EXTENSION_BY_CONTENT_TYPE = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
_UPLOAD_URL_EXPIRES_IN_SECONDS = 300
_MAX_IMAGES_PER_STYLE = 6

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-") or "item"


async def _unique_slug(base_text: str, *, lookup: Callable[[str], Awaitable[object | None]]) -> str:
    slug = _slugify(base_text)
    candidate = slug
    suffix = 2
    while await lookup(candidate) is not None:
        candidate = f"{slug}-{suffix}"
        suffix += 1
    return candidate


def _start_translation(entity: object, field: str, source_text: str) -> list[str]:
    """Admin catalog content is always authored in English; this seeds the
    `{field}_en`/`{field}_en_source` pair and marks every other supported
    locale PENDING, mirroring `braiders.service.update_business_info`'s bio
    handling but with a fixed source locale instead of a request-derived one."""
    setattr(entity, f"{field}_en", source_text)
    setattr(entity, f"{field}_en_source", TranslationSource.HUMAN)
    target_locales = [loc for loc in settings.supported_locales_list if loc != "en"]
    for loc in target_locales:
        setattr(entity, f"{field}_{loc}_source", TranslationSource.PENDING)
    return target_locales


async def _enqueue_translation(
    queue: ArqRedis,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    field: str,
    source_text: str,
    target_locales: list[str],
) -> None:
    if not target_locales:
        return
    await queue.enqueue_job(
        TASK_TRANSLATE_STYLE_TEXT,
        entity_type=entity_type,
        entity_id=str(entity_id),
        field=field,
        source_locale="en",
        source_text=source_text,
        target_locales=target_locales,
    )


def _to_category_response(category: StyleCategory) -> StyleCategoryResponse:
    return StyleCategoryResponse(
        id=category.id,
        slug=category.slug,
        name_en=category.name_en,
        name_de=category.name_de,
        name_fr=category.name_fr,
        name_en_source=category.name_en_source,
        name_de_source=category.name_de_source,
        name_fr_source=category.name_fr_source,
        display_order=category.display_order,
    )


def _to_image_response(image: StyleImage) -> StyleImageResponse:
    return StyleImageResponse(
        id=image.id, url=storage.build_public_url(image.object_key), position=image.position
    )


def _to_variation_response(variation: StyleVariation) -> StyleVariationResponse:
    return StyleVariationResponse(
        id=variation.id,
        name_en=variation.name_en,
        name_de=variation.name_de,
        name_fr=variation.name_fr,
        name_en_source=variation.name_en_source,
        name_de_source=variation.name_de_source,
        name_fr_source=variation.name_fr_source,
        display_order=variation.display_order,
        is_active=variation.is_active,
    )


def _to_style_response(
    style: Style, *, images: list[StyleImage], variations: list[StyleVariation]
) -> StyleResponse:
    return StyleResponse(
        id=style.id,
        slug=style.slug,
        category_id=style.category_id,
        name_en=style.name_en,
        name_de=style.name_de,
        name_fr=style.name_fr,
        name_en_source=style.name_en_source,
        name_de_source=style.name_de_source,
        name_fr_source=style.name_fr_source,
        description_en=style.description_en,
        description_de=style.description_de,
        description_fr=style.description_fr,
        description_en_source=style.description_en_source,
        description_de_source=style.description_de_source,
        description_fr_source=style.description_fr_source,
        is_active=style.is_active,
        images=[_to_image_response(i) for i in images],
        variations=[_to_variation_response(v) for v in variations],
    )


def _to_addon_response(addon: AddOn) -> AddOnResponse:
    return AddOnResponse(
        id=addon.id,
        slug=addon.slug,
        name_en=addon.name_en,
        name_de=addon.name_de,
        name_fr=addon.name_fr,
        name_en_source=addon.name_en_source,
        name_de_source=addon.name_de_source,
        name_fr_source=addon.name_fr_source,
        suggested_price=addon.suggested_price,
        is_active=addon.is_active,
    )


# --- Locale-filtered (public/display) responses -----------------------------
#
# Admin/authoring endpoints (above) always expose every language variant so
# the catalog can be edited/completed. Consumer-facing reads instead resolve
# to a single string via `localize_field`, so the frontend never has to carry
# per-locale fallback logic itself.


def _to_category_public_response(
    category: StyleCategory, *, locale: str
) -> StyleCategoryPublicResponse:
    return StyleCategoryPublicResponse(
        id=category.id,
        slug=category.slug,
        name=localize_field(category, "name", locale) or category.name_en,
        display_order=category.display_order,
    )


def _to_variation_public_response(
    variation: StyleVariation, *, locale: str
) -> StyleVariationPublicResponse:
    return StyleVariationPublicResponse(
        id=variation.id,
        name=localize_field(variation, "name", locale) or variation.name_en,
        display_order=variation.display_order,
        is_active=variation.is_active,
    )


def _to_style_public_response(
    style: Style, *, images: list[StyleImage], variations: list[StyleVariation], locale: str
) -> StylePublicResponse:
    return StylePublicResponse(
        id=style.id,
        slug=style.slug,
        category_id=style.category_id,
        name=localize_field(style, "name", locale) or style.name_en,
        description=localize_field(style, "description", locale),
        is_active=style.is_active,
        images=[_to_image_response(i) for i in images],
        variations=[_to_variation_public_response(v, locale=locale) for v in variations],
    )


def _to_addon_public_response(addon: AddOn, *, locale: str) -> AddOnPublicResponse:
    return AddOnPublicResponse(
        id=addon.id,
        slug=addon.slug,
        name=localize_field(addon, "name", locale) or addon.name_en,
        suggested_price=addon.suggested_price,
        is_active=addon.is_active,
    )


# The public catalogue only changes when an admin edits it (or a translation
# lands), and every such write invalidates it instantly - see
# app.core.cache_tags - so it can sit in Redis for a long time.
_CATALOG_CACHE_TTL_SECONDS = 6 * 60 * 60


def _catalog_tags(_args) -> list[str]:
    return [CATALOG]


# --- Categories ---------------------------------------------------------


async def list_categories(db: AsyncSession) -> list[StyleCategoryResponse]:
    categories = await styles_repo.list_categories(db)
    return [_to_category_response(c) for c in categories]


@cached(name="styles.list_categories_public", ttl_seconds=_CATALOG_CACHE_TTL_SECONDS, tags=_catalog_tags)
async def list_categories_public(
    db: AsyncSession, *, locale: str
) -> list[StyleCategoryPublicResponse]:
    categories = await styles_repo.list_categories(db)
    return [_to_category_public_response(c, locale=locale) for c in categories]


async def create_category(
    db: AsyncSession, *, data: StyleCategoryCreateRequest, queue: ArqRedis
) -> StyleCategoryResponse:
    slug = await _unique_slug(data.name, lookup=lambda s: styles_repo.get_category_by_slug(db, s))
    category = await styles_repo.create_category(
        db, slug=slug, name_en="", display_order=data.display_order
    )
    target_locales = _start_translation(category, "name", data.name)
    await db.commit()
    await _enqueue_translation(
        queue,
        entity_type="style_category",
        entity_id=category.id,
        field="name",
        source_text=data.name,
        target_locales=target_locales,
    )
    return _to_category_response(category)


async def update_category(
    db: AsyncSession,
    category_id: uuid.UUID,
    *,
    data: StyleCategoryUpdateRequest,
    queue: ArqRedis,
) -> StyleCategoryResponse:
    category = await styles_repo.get_category_by_id(db, category_id)
    if category is None:
        raise StyleCategoryNotFoundError()

    target_locales: list[str] = []
    if data.name is not None and data.name != category.name_en:
        target_locales = _start_translation(category, "name", data.name)
    if data.display_order is not None:
        category.display_order = data.display_order
    await db.commit()

    await _enqueue_translation(
        queue,
        entity_type="style_category",
        entity_id=category.id,
        field="name",
        source_text=data.name or category.name_en,
        target_locales=target_locales,
    )
    return _to_category_response(category)


async def delete_category(db: AsyncSession, category_id: uuid.UUID) -> None:
    category = await styles_repo.get_category_by_id(db, category_id)
    if category is None:
        raise StyleCategoryNotFoundError()
    try:
        await styles_repo.delete_category(db, category)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise EntityInUseError() from None


# --- Styles ---------------------------------------------------------------


async def get_style(db: AsyncSession, style_id: uuid.UUID) -> StyleResponse:
    style = await styles_repo.get_style_by_id(db, style_id)
    if style is None:
        raise StyleNotFoundError()
    images = await styles_repo.list_style_images(db, style.id)
    variations = await styles_repo.list_style_variations(db, style.id)
    return _to_style_response(style, images=images, variations=variations)


@cached(name="styles.get_style_public", ttl_seconds=_CATALOG_CACHE_TTL_SECONDS, tags=_catalog_tags)
async def get_style_public(db: AsyncSession, style_id: uuid.UUID, *, locale: str) -> StylePublicResponse:
    style = await styles_repo.get_style_by_id(db, style_id)
    if style is None:
        raise StyleNotFoundError()
    images = await styles_repo.list_style_images(db, style.id)
    variations = await styles_repo.list_style_variations(db, style.id)
    return _to_style_public_response(style, images=images, variations=variations, locale=locale)


async def _load_style_children(db: AsyncSession, styles: list, *, only_active: bool):
    """Two batched queries for a whole page of styles instead of two per style."""
    style_ids = [style.id for style in styles]
    images = await styles_repo.list_style_images_by_style_ids(db, style_ids)
    variations = await styles_repo.list_style_variations_by_style_ids(
        db, style_ids, only_active=only_active
    )
    return images, variations


async def list_styles(
    db: AsyncSession,
    *,
    category_id: uuid.UUID | None,
    search: str | None,
    only_active: bool,
    params: PaginationParams,
) -> PaginatedData[StyleResponse]:
    stmt = styles_repo.build_style_list_stmt(
        category_id=category_id, search=search, only_active=only_active
    )
    styles, meta = await paginate(db, stmt, params)

    images, variations = await _load_style_children(db, styles, only_active=only_active)
    items = [
        _to_style_response(style, images=images[style.id], variations=variations[style.id])
        for style in styles
    ]
    return PaginatedData(items=items, pagination=meta)


@cached(name="styles.list_styles_public", ttl_seconds=_CATALOG_CACHE_TTL_SECONDS, tags=_catalog_tags)
async def list_styles_public(
    db: AsyncSession,
    *,
    category_id: uuid.UUID | None,
    search: str | None,
    only_active: bool,
    params: PaginationParams,
    locale: str,
) -> PaginatedData[StylePublicResponse]:
    stmt = styles_repo.build_style_list_stmt(
        category_id=category_id, search=search, only_active=only_active
    )
    styles, meta = await paginate(db, stmt, params)

    images, variations = await _load_style_children(db, styles, only_active=only_active)
    items = [
        _to_style_public_response(
            style, images=images[style.id], variations=variations[style.id], locale=locale
        )
        for style in styles
    ]
    return PaginatedData(items=items, pagination=meta)


async def create_style(
    db: AsyncSession, *, data: StyleCreateRequest, created_by: uuid.UUID, queue: ArqRedis
) -> StyleResponse:
    if data.category_id is not None:
        category = await styles_repo.get_category_by_id(db, data.category_id)
        if category is None:
            raise StyleCategoryNotFoundError()

    slug = await _unique_slug(data.name, lookup=lambda s: styles_repo.get_style_by_slug(db, s))
    style = await styles_repo.create_style(
        db,
        category_id=data.category_id,
        slug=slug,
        name_en="",
        description_en=None,
        created_by=created_by,
    )

    name_target_locales = _start_translation(style, "name", data.name)
    description_target_locales: list[str] = []
    if data.description:
        description_target_locales = _start_translation(style, "description", data.description)
    await db.commit()

    await _enqueue_translation(
        queue,
        entity_type="style",
        entity_id=style.id,
        field="name",
        source_text=data.name,
        target_locales=name_target_locales,
    )
    if data.description:
        await _enqueue_translation(
            queue,
            entity_type="style",
            entity_id=style.id,
            field="description",
            source_text=data.description,
            target_locales=description_target_locales,
        )
    return _to_style_response(style, images=[], variations=[])


async def update_style(
    db: AsyncSession, style_id: uuid.UUID, *, data: StyleUpdateRequest, queue: ArqRedis
) -> StyleResponse:
    style = await styles_repo.get_style_by_id(db, style_id)
    if style is None:
        raise StyleNotFoundError()

    if data.category_id is not None:
        category = await styles_repo.get_category_by_id(db, data.category_id)
        if category is None:
            raise StyleCategoryNotFoundError()
        style.category_id = data.category_id

    name_target_locales: list[str] = []
    if data.name is not None and data.name != style.name_en:
        name_target_locales = _start_translation(style, "name", data.name)

    description_target_locales: list[str] = []
    if data.description is not None and data.description != style.description_en:
        description_target_locales = _start_translation(style, "description", data.description)

    if data.is_active is not None:
        style.is_active = data.is_active

    await db.commit()

    await _enqueue_translation(
        queue,
        entity_type="style",
        entity_id=style.id,
        field="name",
        source_text=data.name or style.name_en,
        target_locales=name_target_locales,
    )
    if description_target_locales:
        await _enqueue_translation(
            queue,
            entity_type="style",
            entity_id=style.id,
            field="description",
            source_text=data.description or "",
            target_locales=description_target_locales,
        )

    images = await styles_repo.list_style_images(db, style.id)
    variations = await styles_repo.list_style_variations(db, style.id)
    return _to_style_response(style, images=images, variations=variations)


async def delete_style(db: AsyncSession, style_id: uuid.UUID) -> None:
    style = await styles_repo.get_style_by_id(db, style_id)
    if style is None:
        raise StyleNotFoundError()
    object_keys = [image.object_key for image in await styles_repo.list_style_images(db, style_id)]
    try:
        await styles_repo.delete_style(db, style)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise EntityInUseError() from None
    for object_key in object_keys:
        storage.delete_object(object_key)


# --- Style images -----------------------------------------------------------


async def request_style_image_upload_url(
    db: AsyncSession, style_id: uuid.UUID, *, data: StyleImageUploadUrlRequest
) -> StyleImageUploadUrlResponse:
    style = await styles_repo.get_style_by_id(db, style_id)
    if style is None:
        raise StyleNotFoundError()

    existing_count = len(await styles_repo.list_style_images(db, style_id))
    if existing_count >= _MAX_IMAGES_PER_STYLE:
        raise MaxStyleImagesReachedError()

    extension = _EXTENSION_BY_CONTENT_TYPE[data.content_type]
    object_key = f"styles/{style_id}/images/{uuid.uuid4()}.{extension}"
    upload_url = storage.generate_presigned_upload_url(
        object_key, content_type=data.content_type, expires_in=_UPLOAD_URL_EXPIRES_IN_SECONDS
    )
    return StyleImageUploadUrlResponse(
        upload_url=upload_url, object_key=object_key, expires_in=_UPLOAD_URL_EXPIRES_IN_SECONDS
    )


async def confirm_style_image_upload(
    db: AsyncSession, style_id: uuid.UUID, *, data: StyleImageConfirmRequest
) -> StyleImageResponse:
    style = await styles_repo.get_style_by_id(db, style_id)
    if style is None:
        raise StyleNotFoundError()

    expected_prefix = f"styles/{style_id}/images/"
    if not data.object_key.startswith(expected_prefix):
        raise InvalidStyleImageUploadError()

    metadata = storage.head_object(data.object_key)
    if metadata is None:
        raise StyleImageNotFoundError()
    if (
        metadata.content_type not in _ALLOWED_IMAGE_CONTENT_TYPES
        or metadata.content_length > _MAX_IMAGE_SIZE_BYTES
    ):
        storage.delete_object(data.object_key)
        raise InvalidStyleImageUploadError()

    existing_images = await styles_repo.list_style_images(db, style_id)
    if len(existing_images) >= _MAX_IMAGES_PER_STYLE:
        storage.delete_object(data.object_key)
        raise MaxStyleImagesReachedError()

    next_position = max((img.position for img in existing_images), default=-1) + 1
    image = await styles_repo.create_style_image(
        db, style_id=style_id, object_key=data.object_key, position=next_position
    )
    await db.commit()
    return _to_image_response(image)


async def delete_style_image(db: AsyncSession, style_id: uuid.UUID, image_id: uuid.UUID) -> None:
    style = await styles_repo.get_style_by_id(db, style_id)
    if style is None:
        raise StyleNotFoundError()

    image = await styles_repo.get_style_image_by_id(db, image_id)
    if image is None or image.style_id != style_id:
        raise StyleImageNotFoundError()

    object_key = image.object_key
    await styles_repo.delete_style_image(db, image)
    await db.commit()
    storage.delete_object(object_key)


# --- Style variations ---------------------------------------------------


async def create_style_variation(
    db: AsyncSession, style_id: uuid.UUID, *, data: StyleVariationCreateRequest, queue: ArqRedis
) -> StyleVariationResponse:
    style = await styles_repo.get_style_by_id(db, style_id)
    if style is None:
        raise StyleNotFoundError()

    variation = await styles_repo.create_style_variation(
        db, style_id=style_id, name_en="", display_order=data.display_order
    )
    target_locales = _start_translation(variation, "name", data.name)
    await db.commit()

    await _enqueue_translation(
        queue,
        entity_type="style_variation",
        entity_id=variation.id,
        field="name",
        source_text=data.name,
        target_locales=target_locales,
    )
    return _to_variation_response(variation)


async def update_style_variation(
    db: AsyncSession,
    style_id: uuid.UUID,
    variation_id: uuid.UUID,
    *,
    data: StyleVariationUpdateRequest,
    queue: ArqRedis,
) -> StyleVariationResponse:
    variation = await styles_repo.get_style_variation_by_id(db, variation_id)
    if variation is None or variation.style_id != style_id:
        raise StyleVariationNotFoundError()

    target_locales: list[str] = []
    if data.name is not None and data.name != variation.name_en:
        target_locales = _start_translation(variation, "name", data.name)
    if data.display_order is not None:
        variation.display_order = data.display_order
    if data.is_active is not None:
        variation.is_active = data.is_active
    await db.commit()

    await _enqueue_translation(
        queue,
        entity_type="style_variation",
        entity_id=variation.id,
        field="name",
        source_text=data.name or variation.name_en,
        target_locales=target_locales,
    )
    return _to_variation_response(variation)


async def delete_style_variation(
    db: AsyncSession, style_id: uuid.UUID, variation_id: uuid.UUID
) -> None:
    variation = await styles_repo.get_style_variation_by_id(db, variation_id)
    if variation is None or variation.style_id != style_id:
        raise StyleVariationNotFoundError()
    try:
        await styles_repo.delete_style_variation(db, variation)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise EntityInUseError() from None


# --- Add-ons ----------------------------------------------------------------


async def list_addons(db: AsyncSession, *, only_active: bool) -> list[AddOnResponse]:
    stmt = styles_repo.build_addon_list_stmt(only_active=only_active)
    result = await db.execute(stmt)
    return [_to_addon_response(a) for a in result.scalars().all()]


@cached(name="styles.list_addons_public", ttl_seconds=_CATALOG_CACHE_TTL_SECONDS, tags=_catalog_tags)
async def list_addons_public(
    db: AsyncSession, *, only_active: bool, locale: str
) -> list[AddOnPublicResponse]:
    stmt = styles_repo.build_addon_list_stmt(only_active=only_active)
    result = await db.execute(stmt)
    return [_to_addon_public_response(a, locale=locale) for a in result.scalars().all()]


async def create_addon(
    db: AsyncSession, *, data: AddOnCreateRequest, queue: ArqRedis
) -> AddOnResponse:
    slug = await _unique_slug(data.name, lookup=lambda s: styles_repo.get_addon_by_slug(db, s))
    addon = await styles_repo.create_addon(
        db, slug=slug, name_en="", suggested_price=data.suggested_price
    )
    target_locales = _start_translation(addon, "name", data.name)
    await db.commit()

    await _enqueue_translation(
        queue,
        entity_type="addon",
        entity_id=addon.id,
        field="name",
        source_text=data.name,
        target_locales=target_locales,
    )
    return _to_addon_response(addon)


async def update_addon(
    db: AsyncSession, addon_id: uuid.UUID, *, data: AddOnUpdateRequest, queue: ArqRedis
) -> AddOnResponse:
    addon = await styles_repo.get_addon_by_id(db, addon_id)
    if addon is None:
        raise AddOnNotFoundError()

    target_locales: list[str] = []
    if data.name is not None and data.name != addon.name_en:
        target_locales = _start_translation(addon, "name", data.name)
    if data.suggested_price is not None:
        addon.suggested_price = data.suggested_price
    if data.is_active is not None:
        addon.is_active = data.is_active
    await db.commit()

    await _enqueue_translation(
        queue,
        entity_type="addon",
        entity_id=addon.id,
        field="name",
        source_text=data.name or addon.name_en,
        target_locales=target_locales,
    )
    return _to_addon_response(addon)


async def delete_addon(db: AsyncSession, addon_id: uuid.UUID) -> None:
    addon = await styles_repo.get_addon_by_id(db, addon_id)
    if addon is None:
        raise AddOnNotFoundError()
    try:
        await styles_repo.delete_addon(db, addon)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise EntityInUseError() from None
