import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.modules.styles.models import AddOn, Style, StyleCategory, StyleImage, StyleVariation


async def create_category(
    db: AsyncSession, *, slug: str, name_en: str, display_order: int
) -> StyleCategory:
    category = StyleCategory(slug=slug, name_en=name_en, display_order=display_order)
    db.add(category)
    await db.flush()
    return category


async def get_category_by_id(db: AsyncSession, category_id: uuid.UUID) -> StyleCategory | None:
    return await db.get(StyleCategory, category_id)


async def get_category_by_slug(db: AsyncSession, slug: str) -> StyleCategory | None:
    result = await db.execute(select(StyleCategory).where(StyleCategory.slug == slug))
    return result.scalar_one_or_none()


async def list_categories(db: AsyncSession) -> list[StyleCategory]:
    result = await db.execute(select(StyleCategory).order_by(StyleCategory.display_order))
    return list(result.scalars().all())


async def delete_category(db: AsyncSession, category: StyleCategory) -> None:
    await db.delete(category)
    await db.flush()


async def create_style(
    db: AsyncSession,
    *,
    category_id: uuid.UUID | None,
    slug: str,
    name_en: str,
    description_en: str | None,
    created_by: uuid.UUID | None,
) -> Style:
    style = Style(
        category_id=category_id,
        slug=slug,
        name_en=name_en,
        description_en=description_en,
        created_by=created_by,
    )
    db.add(style)
    await db.flush()
    return style


async def get_style_by_id(db: AsyncSession, style_id: uuid.UUID) -> Style | None:
    return await db.get(Style, style_id)


async def get_style_by_slug(db: AsyncSession, slug: str) -> Style | None:
    result = await db.execute(select(Style).where(Style.slug == slug))
    return result.scalar_one_or_none()


async def delete_style(db: AsyncSession, style: Style) -> None:
    await db.delete(style)
    await db.flush()


def build_style_list_stmt(
    *, category_id: uuid.UUID | None, search: str | None, only_active: bool
) -> Select:
    stmt = select(Style)
    if only_active:
        stmt = stmt.where(Style.is_active.is_(True))
    if category_id is not None:
        stmt = stmt.where(Style.category_id == category_id)
    if search:
        stmt = stmt.where(Style.name_en.ilike(f"%{search}%"))
    return stmt.order_by(Style.name_en)


async def create_style_image(
    db: AsyncSession, *, style_id: uuid.UUID, object_key: str, position: int
) -> StyleImage:
    image = StyleImage(style_id=style_id, object_key=object_key, position=position)
    db.add(image)
    await db.flush()
    return image


async def get_style_image_by_id(db: AsyncSession, image_id: uuid.UUID) -> StyleImage | None:
    return await db.get(StyleImage, image_id)


async def list_style_images(db: AsyncSession, style_id: uuid.UUID) -> list[StyleImage]:
    result = await db.execute(
        select(StyleImage).where(StyleImage.style_id == style_id).order_by(StyleImage.position)
    )
    return list(result.scalars().all())


async def list_style_images_by_style_ids(
    db: AsyncSession, style_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[StyleImage]]:
    by_style: dict[uuid.UUID, list[StyleImage]] = {style_id: [] for style_id in style_ids}
    if not style_ids:
        return by_style
    result = await db.execute(
        select(StyleImage)
        .where(StyleImage.style_id.in_(style_ids))
        .order_by(StyleImage.style_id, StyleImage.position)
    )
    for image in result.scalars().all():
        by_style[image.style_id].append(image)
    return by_style


async def delete_style_image(db: AsyncSession, image: StyleImage) -> None:
    await db.delete(image)
    await db.flush()


async def create_style_variation(
    db: AsyncSession, *, style_id: uuid.UUID, name_en: str, display_order: int
) -> StyleVariation:
    variation = StyleVariation(style_id=style_id, name_en=name_en, display_order=display_order)
    db.add(variation)
    await db.flush()
    return variation


async def get_style_variation_by_id(
    db: AsyncSession, variation_id: uuid.UUID
) -> StyleVariation | None:
    return await db.get(StyleVariation, variation_id)


async def list_style_variations(
    db: AsyncSession, style_id: uuid.UUID, *, only_active: bool = False
) -> list[StyleVariation]:
    stmt = select(StyleVariation).where(StyleVariation.style_id == style_id)
    if only_active:
        stmt = stmt.where(StyleVariation.is_active.is_(True))
    result = await db.execute(stmt.order_by(StyleVariation.display_order))
    return list(result.scalars().all())


async def list_style_variations_by_style_ids(
    db: AsyncSession, style_ids: list[uuid.UUID], *, only_active: bool = False
) -> dict[uuid.UUID, list[StyleVariation]]:
    by_style: dict[uuid.UUID, list[StyleVariation]] = {style_id: [] for style_id in style_ids}
    if not style_ids:
        return by_style
    stmt = select(StyleVariation).where(StyleVariation.style_id.in_(style_ids))
    if only_active:
        stmt = stmt.where(StyleVariation.is_active.is_(True))
    result = await db.execute(stmt.order_by(StyleVariation.style_id, StyleVariation.display_order))
    for variation in result.scalars().all():
        by_style[variation.style_id].append(variation)
    return by_style


async def list_styles_by_ids(db: AsyncSession, style_ids: list[uuid.UUID]) -> dict[uuid.UUID, Style]:
    if not style_ids:
        return {}
    result = await db.execute(select(Style).where(Style.id.in_(style_ids)))
    return {style.id: style for style in result.scalars().all()}


async def delete_style_variation(db: AsyncSession, variation: StyleVariation) -> None:
    await db.delete(variation)
    await db.flush()


async def create_addon(
    db: AsyncSession, *, slug: str, name_en: str, suggested_price: Decimal | None
) -> AddOn:
    addon = AddOn(slug=slug, name_en=name_en, suggested_price=suggested_price)
    db.add(addon)
    await db.flush()
    return addon


async def get_addon_by_id(db: AsyncSession, addon_id: uuid.UUID) -> AddOn | None:
    return await db.get(AddOn, addon_id)


async def get_addon_by_slug(db: AsyncSession, slug: str) -> AddOn | None:
    result = await db.execute(select(AddOn).where(AddOn.slug == slug))
    return result.scalar_one_or_none()


def build_addon_list_stmt(*, only_active: bool) -> Select:
    stmt = select(AddOn)
    if only_active:
        stmt = stmt.where(AddOn.is_active.is_(True))
    return stmt.order_by(AddOn.name_en)


async def delete_addon(db: AsyncSession, addon: AddOn) -> None:
    await db.delete(addon)
    await db.flush()
