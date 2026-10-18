"""Replaces the placeholder test images (one shared logo, portfolio photo and
style photo reused everywhere) with real braid photos: style-accurate images
for every style in the catalog, and a braid/salon logo plus a portfolio of
the braider's own offered styles for every seeded braider.

Photos are free-to-use Pexels images (https://www.pexels.com/license/ -
commercial use allowed, no attribution required), each hand-checked against
the style it's filed under. They're downloaded once, resized, and uploaded
to a `seed/library/` prefix no DB row ever points at. Every style / braider
then gets its own server-side R2 copy under the same key layout real uploads
use (`styles/{id}/images/...`, `braiders/{id}/logo|portfolio/...`) - because
replacing a logo or deleting a portfolio photo deletes its R2 object, a key
shared across braiders would let one braider wipe it for everyone else.

Only rows still pointing at the known placeholder keys are touched, so real
uploads are never overwritten and re-running is a no-op. Old placeholder
objects in R2 are left alone.

Run from the repo root (after seed_styles + seed_braiders):
    python -m scripts.seed_braider_images
"""
import asyncio
import hashlib
import io
import random
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
from PIL import Image, ImageOps
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.core.storage import get_r2_client, head_object, put_object
from app.modules.braiders.models import BioSource, BraiderProfile
from app.modules.braiders.offerings.models import BraiderStyle
from app.modules.braiders.portfolio.models import PortfolioImage
from app.modules.styles.models import Style, StyleImage

# styles.created_by is a string ForeignKey("users.id") - SQLAlchemy can't
# resolve it unless the users table has been registered on Base.metadata
# somewhere first (same reason alembic/env.py and app/worker.py import this).
from app.modules.users import models as users_models  # noqa: F401,E402
from scripts.seed_braiders import SHARED_LOGO_KEY, SHARED_PORTFOLIO_KEY
from scripts.seed_test_style_image import TEST_OBJECT_KEY

LIBRARY_PREFIX = "seed/library"
PORTFOLIO_SIZE = 3
_PHOTO_MAX_EDGE = 1200
_LOGO_EDGE = 600

# Pexels photo ids, per style slug, each checked to actually show that style.
STYLE_PHOTOS: dict[str, list[int]] = {
    "knotless-braids-classic": [11515382, 2474255, 35334311, 13767165, 3897426, 18071210],
    "knotless-braids-bohemian": [32220970, 7624045, 5301538, 5301531, 28383173],
    "box-braids-classic": [31653967, 7759977, 19177101, 5722771, 33603048, 4902065, 7607808],
    "box-braids-bohemian": [25752048, 24984386, 24984341, 37843899, 34191088],
    "cornrows-straight-back": [33664383, 10825829, 11728787, 32426309, 30050895],
    "cornrows-feed-in": [32426283, 4495857, 32228162, 33664380, 11269006],
    "passion-twists-classic": [17764877, 7193040, 36297542, 33469601, 11482130],
    "passion-twists-bob": [30091519, 30091515, 30091518, 17764874],
    "faux-locs-classic": [21602032, 21602034, 39193736, 17895857, 33600135, 15234303],
    "faux-locs-goddess": [12530818, 15242871, 20643408],
    "fulani-braids-classic": [14883868, 4953278, 19743922, 29496369, 5706984, 40319895],
    "fulani-braids-half-up": [9224340, 7229369, 7190007, 31473242, 33351978],
}

# Hair-focused braid close-ups and salon/braiding-in-progress shots, rather
# than faces presented as the (fictional) braider.
LOGO_PHOTOS: list[int] = [
    36441633, 15576674, 33475847, 36587243, 11268995, 11269004, 31473242, 34191088,
    5301538, 5301531, 7951589, 11482130, 33664383, 10825829, 32426309, 30755166,
    3252637, 36362913, 18579806, 7624555, 7190007, 28383173, 40302511, 11269032,
]

_ALL_PORTFOLIO_PHOTOS = sorted({p for photos in STYLE_PHOTOS.values() for p in photos})
_PHOTO_STYLE = {p: slug for slug, photos in STYLE_PHOTOS.items() for p in photos}


def _library_key(kind: str, photo_id: int) -> str:
    return f"{LIBRARY_PREFIX}/{kind}/{photo_id}.jpg"


def _prepare(photo_id: int, kind: str) -> None:
    """Downloads one Pexels photo, resizes it, and uploads it to the library
    (skipped if already there)."""
    key = _library_key(kind, photo_id)
    if head_object(key) is not None:
        return
    response = httpx.get(
        f"https://images.pexels.com/photos/{photo_id}/pexels-photo-{photo_id}.jpeg",
        params={"auto": "compress", "cs": "tinysrgb", "w": "1600"},
        timeout=60,
        follow_redirects=True,
    )
    response.raise_for_status()
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(response.content))).convert("RGB")
    if kind == "logo":
        # Square, top-biased crop so the braids (not the background) fill the avatar.
        image = ImageOps.fit(image, (_LOGO_EDGE, _LOGO_EDGE), centering=(0.5, 0.35))
    else:
        image.thumbnail((_PHOTO_MAX_EDGE, _PHOTO_MAX_EDGE))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=82, optimize=True, progressive=True)
    put_object(key, buffer.getvalue(), content_type="image/jpeg")


def _copy(source_key: str, dest_key: str) -> None:
    get_r2_client().copy_object(
        Bucket=get_settings().r2_bucket_name,
        Key=dest_key,
        CopySource={"Bucket": get_settings().r2_bucket_name, "Key": source_key},
        ContentType="image/jpeg",
        MetadataDirective="REPLACE",
    )


def _run_parallel(fn, jobs: list[tuple]) -> None:
    with ThreadPoolExecutor(max_workers=16) as pool:
        for future in [pool.submit(fn, *job) for job in jobs]:
            future.result()  # re-raise the first failure


async def main() -> None:
    print("Preparing photo library in R2...")
    library_jobs = [(p, "portfolio") for p in _ALL_PORTFOLIO_PHOTOS] + [(p, "logo") for p in LOGO_PHOTOS]
    await asyncio.to_thread(_run_parallel, _prepare, library_jobs)
    print(f"  {len(library_jobs)} library photos ready.")

    async with AsyncSessionLocal() as db:
        # --- Style catalog images -------------------------------------------
        copies: list[tuple[str, str]] = []
        styles = list((await db.execute(select(Style).where(Style.slug.in_(STYLE_PHOTOS)))).scalars())
        styles_updated = 0
        for style in styles:
            images = list(
                (await db.execute(select(StyleImage).where(StyleImage.style_id == style.id))).scalars()
            )
            if any(img.object_key != TEST_OBJECT_KEY for img in images):
                continue  # has real (admin-uploaded or already-seeded) photos - leave it alone
            await db.execute(delete(StyleImage).where(StyleImage.style_id == style.id))
            for position, photo_id in enumerate(STYLE_PHOTOS[style.slug]):
                key = f"styles/{style.id}/images/seed-{uuid.uuid4()}.jpg"
                copies.append((_library_key("portfolio", photo_id), key))
                db.add(StyleImage(style_id=style.id, object_key=key, position=position))
            styles_updated += 1
        await asyncio.to_thread(_run_parallel, _copy, copies)
        await db.commit()
        print(f"Styles: {styles_updated} updated with real photos ({len(copies)} images).")
        style_names = {s.slug: s for s in styles}

        # --- Braider logos + portfolios --------------------------------------
        offered_slugs: dict[uuid.UUID, list[str]] = {}
        for braider_id, slug in await db.execute(
            select(BraiderStyle.braider_id, Style.slug).join(Style, Style.id == BraiderStyle.style_id)
        ):
            offered_slugs.setdefault(braider_id, []).append(slug)

        profiles = list(
            (
                await db.execute(
                    select(BraiderProfile).where(BraiderProfile.logo_object_key == SHARED_LOGO_KEY)
                )
            ).scalars()
        )
        placeholder_portfolio_ids = set(
            (
                await db.execute(
                    select(PortfolioImage.braider_id).where(
                        PortfolioImage.object_key == SHARED_PORTFOLIO_KEY
                    )
                )
            ).scalars()
        )
        portfolio_profiles = list(
            (
                await db.execute(
                    select(BraiderProfile).where(BraiderProfile.id.in_(placeholder_portfolio_ids))
                )
            ).scalars()
        ) if placeholder_portfolio_ids else []

        copies = []
        for profile in profiles:
            rng = random.Random(int(hashlib.sha256(profile.id.bytes).hexdigest(), 16))
            key = f"braiders/{profile.id}/logo/seed-{uuid.uuid4()}.jpg"
            copies.append((_library_key("logo", rng.choice(LOGO_PHOTOS)), key))
            profile.logo_object_key = key

        for profile in portfolio_profiles:
            rng = random.Random(int(hashlib.sha256(profile.id.bytes).hexdigest(), 16) + 1)
            pool = sorted(
                {p for slug in offered_slugs.get(profile.id, []) for p in STYLE_PHOTOS.get(slug, [])}
            ) or _ALL_PORTFOLIO_PHOTOS
            picks = rng.sample(pool, k=min(PORTFOLIO_SIZE, len(pool)))
            await db.execute(
                delete(PortfolioImage).where(
                    PortfolioImage.braider_id == profile.id,
                    PortfolioImage.object_key == SHARED_PORTFOLIO_KEY,
                )
            )
            # Real portfolio photos (if any) keep their slots; seeded ones go after.
            taken = set(
                (
                    await db.execute(
                        select(PortfolioImage.position).where(PortfolioImage.braider_id == profile.id)
                    )
                ).scalars()
            )
            position = 0
            for photo_id in picks:
                while position in taken:
                    position += 1
                key = f"braiders/{profile.id}/portfolio/seed-{uuid.uuid4()}.jpg"
                copies.append((_library_key("portfolio", photo_id), key))
                style = style_names.get(_PHOTO_STYLE[photo_id])
                db.add(
                    PortfolioImage(
                        braider_id=profile.id,
                        object_key=key,
                        position=position,
                        caption_en=style.name_en if style else None,
                        caption_de=style.name_de if style else None,
                        caption_fr=style.name_fr if style else None,
                        caption_en_source=BioSource.HUMAN if style else None,
                        caption_de_source=BioSource.MACHINE if style else None,
                        caption_fr_source=BioSource.MACHINE if style else None,
                    )
                )
                position += 1

        print(f"Copying {len(copies)} braider images in R2...")
        await asyncio.to_thread(_run_parallel, _copy, copies)
        await db.commit()
        print(
            f"Braiders: {len(profiles)} logos replaced, "
            f"{len(portfolio_profiles)} portfolios replaced."
        )


if __name__ == "__main__":
    asyncio.run(main())
