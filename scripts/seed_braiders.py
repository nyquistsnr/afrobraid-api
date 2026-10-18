"""Seeds fake braider accounts with fully completed onboarding, spread across
Germany, France, and the United Kingdom, for search/discovery and load testing.

Bypasses the real signup/OTP/Veriff/Stripe-webhook flows entirely - every
`*_completed_at` field on BraiderOnboardingStatus is set directly (same
approach `tests/helpers.py` / `tests/modules/braiders/test_service_location.py`
use to fake onboarding progress in tests), and a placeholder
StripeConnectAccount row (`acct_seed_...`) stands in for a real Connect
account. Nothing here hits Stripe or Veriff - run
`python -m scripts.link_seed_braiders_stripe` afterwards to swap the
placeholders for real, verified test-mode Connect accounts.

Rows are built as plain ORM objects and flushed in batches instead of going
through the per-row repository helpers (each of which flushes), otherwise
thousands of braiders would take ~15 round trips each.

Idempotent: safe to re-run - skips any index whose seed email already exists,
and each index always generates the same braider (per-index RNG).

Run from the repo root:
    python -m scripts.seed_braiders                 # 1,000 braiders
    python -m scripts.seed_braiders --count 10000
"""
import argparse
import asyncio
import random
import uuid
from datetime import time
from decimal import Decimal
from typing import TypedDict

from sqlalchemy import ColumnElement, select

from app.core.database import AsyncSessionLocal
from app.core.security import hash_password
from app.modules.braiders.availability.models import (
    BraiderAvailabilitySettings,
    BraiderWeeklyAvailability,
    DayOfWeek,
)
from app.modules.braiders.completion import mark_step_complete
from app.modules.braiders.models import (
    BioSource,
    BraiderOnboardingStatus,
    BraiderProfile,
    Gender,
    OnboardingStep,
)
from app.modules.braiders.offerings.models import BraiderStyle
from app.modules.braiders.payment_setup.models import StripeConnectAccount
from app.modules.braiders.portfolio.models import PortfolioImage
from app.modules.braiders.service_location.models import BraiderServiceLocation, LocationType
from app.modules.styles.models import Style
from app.modules.users.models import User, UserType

SEED_PASSWORD = "SeedBraider123!"
SEED_EMAIL_DOMAIN = "mailinator.com"
# Matched by exact pattern, never by domain alone - real testers sign up with
# mailinator.com too, and the prune/link scripts must never touch them.
SEED_EMAIL_REGEX = r"^braider[0-9]{5}@mailinator\.com$"
DEFAULT_COUNT = 1_000
BATCH_SIZE = 500

# Same real uploaded image reused for every seeded braider's logo and cover
# photo - not per-braider content, just a placeholder so search results and
# profile cards render something instead of blank space.
SHARED_LOGO_KEY = "braiders/877177d1-1e8b-47b6-a96d-81a008b2653f/logo/442d42f0-94ec-46de-8476-9315a73ca435.jpg"
SHARED_PORTFOLIO_KEY = (
    "braiders/877177d1-1e8b-47b6-a96d-81a008b2653f/portfolio/144ca43d-811d-426e-91fc-21d2df1b0d2f.jpg"
)


class CitySeed(TypedDict):
    city: str
    country: str
    postal_code: str
    latitude: float
    longitude: float
    weight: int  # relative share of that country's braiders


def _c(city: str, country: str, postal_code: str, lat: float, lng: float, weight: int) -> CitySeed:
    return {
        "city": city, "country": country, "postal_code": postal_code,
        "latitude": lat, "longitude": lng, "weight": weight,
    }


CITIES: list[CitySeed] = [
    # Germany
    _c("Berlin", "DE", "10115", 52.520008, 13.404954, 12),
    _c("Hamburg", "DE", "20095", 53.551086, 9.993682, 7),
    _c("Munich", "DE", "80331", 48.135125, 11.581981, 6),
    _c("Cologne", "DE", "50667", 50.937531, 6.960279, 5),
    _c("Frankfurt", "DE", "60311", 50.110924, 8.682127, 5),
    _c("Stuttgart", "DE", "70173", 48.775846, 9.182932, 3),
    _c("Dusseldorf", "DE", "40213", 51.227741, 6.773456, 3),
    _c("Dortmund", "DE", "44135", 51.513587, 7.465298, 3),
    _c("Essen", "DE", "45127", 51.455643, 7.011555, 3),
    _c("Leipzig", "DE", "04109", 51.339695, 12.373075, 3),
    _c("Bremen", "DE", "28195", 53.079296, 8.801694, 2),
    _c("Dresden", "DE", "01067", 51.050409, 13.737262, 2),
    _c("Hanover", "DE", "30159", 52.375892, 9.732010, 2),
    _c("Nuremberg", "DE", "90402", 49.452030, 11.076750, 2),
    _c("Duisburg", "DE", "47051", 51.434407, 6.762329, 2),
    _c("Bochum", "DE", "44787", 51.481845, 7.216236, 1),
    _c("Wuppertal", "DE", "42103", 51.256213, 7.150764, 1),
    _c("Bielefeld", "DE", "33602", 52.030228, 8.532471, 1),
    _c("Bonn", "DE", "53111", 50.737430, 7.098207, 1),
    _c("Munster", "DE", "48143", 51.960665, 7.626135, 1),
    _c("Mannheim", "DE", "68159", 49.487459, 8.466039, 1),
    _c("Karlsruhe", "DE", "76133", 49.006890, 8.403653, 1),
    _c("Augsburg", "DE", "86150", 48.370545, 10.897790, 1),
    _c("Wiesbaden", "DE", "65183", 50.078218, 8.239761, 1),
    _c("Aachen", "DE", "52062", 50.775346, 6.083887, 1),
    _c("Kiel", "DE", "24103", 54.323293, 10.122765, 1),
    _c("Mainz", "DE", "55116", 49.992862, 8.247253, 1),
    _c("Freiburg", "DE", "79098", 47.999008, 7.842104, 1),
    # France
    _c("Paris", "FR", "75011", 48.856600, 2.352200, 14),
    _c("Marseille", "FR", "13001", 43.296500, 5.369800, 5),
    _c("Lyon", "FR", "69002", 45.764000, 4.835700, 5),
    _c("Toulouse", "FR", "31000", 43.604700, 1.444200, 3),
    _c("Nice", "FR", "06000", 43.710200, 7.261900, 2),
    _c("Nantes", "FR", "44000", 47.218400, -1.553600, 2),
    _c("Strasbourg", "FR", "67000", 48.573400, 7.752300, 2),
    _c("Montpellier", "FR", "34000", 43.610800, 3.876700, 2),
    _c("Bordeaux", "FR", "33000", 44.837800, -0.579200, 2),
    _c("Lille", "FR", "59000", 50.629200, 3.057300, 3),
    _c("Rennes", "FR", "35000", 48.117300, -1.677800, 1),
    _c("Reims", "FR", "51100", 49.258300, 4.031700, 1),
    _c("Le Havre", "FR", "76600", 49.494400, 0.107900, 1),
    _c("Saint-Etienne", "FR", "42000", 45.439700, 4.387200, 1),
    _c("Toulon", "FR", "83000", 43.124200, 5.928000, 1),
    _c("Grenoble", "FR", "38000", 45.188500, 5.724500, 1),
    _c("Dijon", "FR", "21000", 47.322000, 5.041500, 1),
    _c("Angers", "FR", "49000", 47.478400, -0.563200, 1),
    _c("Nimes", "FR", "30000", 43.836700, 4.360100, 1),
    _c("Clermont-Ferrand", "FR", "63000", 45.777200, 3.087000, 1),
    _c("Le Mans", "FR", "72000", 48.006100, 0.199600, 1),
    _c("Aix-en-Provence", "FR", "13100", 43.529700, 5.447400, 1),
    _c("Brest", "FR", "29200", 48.390400, -4.486100, 1),
    _c("Tours", "FR", "37000", 47.394100, 0.684800, 1),
    _c("Amiens", "FR", "80000", 49.894100, 2.295800, 1),
    _c("Limoges", "FR", "87000", 45.833600, 1.261100, 1),
    _c("Saint-Denis", "FR", "93200", 48.936200, 2.357400, 3),
    _c("Montreuil", "FR", "93100", 48.863800, 2.448500, 2),
    _c("Argenteuil", "FR", "95100", 48.947200, 2.247000, 1),
    # United Kingdom
    _c("London", "GB", "EC1A 1BB", 51.507351, -0.127758, 16),
    _c("Croydon", "GB", "CR0 1NX", 51.376165, -0.098234, 2),
    _c("Birmingham", "GB", "B1 1AA", 52.486243, -1.890401, 5),
    _c("Manchester", "GB", "M1 1AE", 53.480759, -2.242631, 5),
    _c("Leeds", "GB", "LS1 1UR", 53.800755, -1.549077, 3),
    _c("Glasgow", "GB", "G1 1XQ", 55.864237, -4.251806, 3),
    _c("Liverpool", "GB", "L1 8JQ", 53.408371, -2.991573, 3),
    _c("Bristol", "GB", "BS1 4DJ", 51.454513, -2.587910, 2),
    _c("Sheffield", "GB", "S1 2HE", 53.381129, -1.470085, 2),
    _c("Edinburgh", "GB", "EH1 1YZ", 55.953252, -3.188267, 2),
    _c("Leicester", "GB", "LE1 5YA", 52.636878, -1.139759, 2),
    _c("Coventry", "GB", "CV1 5FB", 52.406822, -1.519693, 1),
    _c("Nottingham", "GB", "NG1 5FS", 52.954783, -1.158109, 2),
    _c("Newcastle upon Tyne", "GB", "NE1 7RU", 54.978252, -1.617780, 1),
    _c("Cardiff", "GB", "CF10 1EP", 51.481581, -3.179090, 1),
    _c("Belfast", "GB", "BT1 5GS", 54.597285, -5.930120, 1),
    _c("Southampton", "GB", "SO14 7DU", 50.909700, -1.404351, 1),
    _c("Brighton", "GB", "BN1 2RE", 50.822530, -0.137163, 1),
    _c("Reading", "GB", "RG1 1JX", 51.454265, -0.978130, 1),
    _c("Luton", "GB", "LU1 2LJ", 51.878670, -0.420025, 1),
    _c("Milton Keynes", "GB", "MK9 3EP", 52.040622, -0.759417, 1),
    _c("Bradford", "GB", "BD1 1HY", 53.795984, -1.759398, 1),
    _c("Wolverhampton", "GB", "WV1 1SH", 52.586973, -2.128820, 1),
    _c("Northampton", "GB", "NN1 1DE", 52.240477, -0.902656, 1),
    _c("Plymouth", "GB", "PL1 2AA", 50.375456, -4.142656, 1),
]

COUNTRIES = ["DE", "FR", "GB"]
_CITIES_BY_COUNTRY = {code: [c for c in CITIES if c["country"] == code] for code in COUNTRIES}

_TIMEZONES = {"DE": "Europe/Berlin", "FR": "Europe/Paris", "GB": "Europe/London"}
_STREETS = {
    "DE": ["Hauptstrasse", "Bahnhofstrasse", "Schillerstrasse", "Goethestrasse", "Lindenallee"],
    "FR": ["Rue de la Republique", "Rue Victor Hugo", "Avenue Jean Jaures", "Rue Principale", "Boulevard Voltaire"],
    "GB": ["High Street", "Station Road", "Church Lane", "Victoria Road", "Market Street"],
}

_FIRST_NAMES = [
    "Amara", "Fatou", "Aissatou", "Chiamaka", "Ngozi", "Aminata", "Adaeze", "Zainab",
    "Khadija", "Awa", "Bintou", "Adama", "Efe", "Yetunde", "Folake", "Ijeoma",
    "Mariama", "Rokia", "Coumba", "Nadia", "Habiba", "Salimata", "Rahma", "Aicha",
    "Oumou", "Djeneba", "Halima", "Fanta", "Sira", "Kadiatou", "Abena", "Akosua",
    "Esi", "Nana", "Ama", "Funmilayo", "Temitope", "Chidinma", "Nneka", "Wanjiru",
    "Achieng", "Makena", "Zuri", "Imani", "Nia", "Ayana", "Thandiwe", "Lerato",
]
_LAST_NAMES = [
    "Diallo", "Diop", "Traore", "Bamba", "Toure", "Camara", "Sow", "Kone",
    "Cisse", "Balde", "Sy", "Fall", "Ndiaye", "Sangare", "Keita", "Okafor",
    "Adeyemi", "Mensah", "Owusu", "Boateng", "Asante", "Okonkwo", "Eze", "Nwosu",
    "Kamau", "Otieno", "Mwangi", "Dlamini", "Nkosi", "Abebe",
]
_BUSINESS_SUFFIXES = [
    "Braids", "Braid Bar", "Hair Studio", "Braiding Lounge", "Hair Atelier",
    "Braid House", "Beauty Studio", "Braids & Co", "Hair Loft", "Tress Studio",
]

_STYLE_SLUGS = [
    "knotless-braids-classic", "knotless-braids-bohemian", "box-braids-classic",
    "box-braids-bohemian", "cornrows-feed-in", "passion-twists-classic",
    "faux-locs-classic", "fulani-braids-classic",
]

_WEEKLY_WINDOWS = [
    (DayOfWeek.MONDAY, time(9, 0), time(17, 0)),
    (DayOfWeek.TUESDAY, time(9, 0), time(17, 0)),
    (DayOfWeek.WEDNESDAY, time(9, 0), time(17, 0)),
    (DayOfWeek.THURSDAY, time(9, 0), time(17, 0)),
    (DayOfWeek.FRIDAY, time(9, 0), time(17, 0)),
    (DayOfWeek.SATURDAY, time(10, 0), time(14, 0)),
]

_ALL_STEPS = [
    OnboardingStep.BUSINESS_INFO,
    OnboardingStep.PHONE_VERIFICATION,
    OnboardingStep.VERIFF,
    OnboardingStep.SERVICE_TYPE,
    OnboardingStep.PORTFOLIO,
    OnboardingStep.SERVICE_LOCATION,
    OnboardingStep.AVAILABILITY,
    OnboardingStep.PAYMENT_SETUP,
]


def seed_email(index: int) -> str:
    return f"braider{index:05d}@{SEED_EMAIL_DOMAIN}"


def is_seed_user() -> ColumnElement[bool]:
    return User.email.regexp_match(SEED_EMAIL_REGEX)


def _pick_city(rng: random.Random, index: int) -> CitySeed:
    # Round-robin the country so DE/FR/GB end up with ~1/3 each, then a
    # population-weighted city inside it.
    cities = _CITIES_BY_COUNTRY[COUNTRIES[index % len(COUNTRIES)]]
    return rng.choices(cities, weights=[c["weight"] for c in cities], k=1)[0]


def _build_braider(
    index: int, password_hash: str, style_ids: dict[str, uuid.UUID]
) -> tuple[User, list[object], list[object]]:
    """Returns (user, rows that FK the user, rows that FK the profile) so the
    caller can flush them in dependency order."""
    rng = random.Random(index)  # same braider every re-run for a given index
    city = _pick_city(rng, index)
    country = city["country"]

    first_name = rng.choice(_FIRST_NAMES)
    last_name = rng.choice(_LAST_NAMES)
    business_name = f"{first_name}'s {rng.choice(_BUSINESS_SUFFIXES)}"

    user = User(
        id=uuid.uuid4(),
        first_name=first_name,
        last_name=last_name,
        email=seed_email(index),
        phone_number=None,
        password_hash=password_hash,
        user_type=UserType.BRAIDER,
        is_email_verified=True,
    )

    profile = BraiderProfile(
        id=uuid.uuid4(),
        user_id=user.id,
        business_name=business_name,
        gender=rng.choice(list(Gender)),
        logo_object_key=SHARED_LOGO_KEY,
        bio_en=f"{business_name} - professional braiding in {city['city']}.",
        bio_de=f"{business_name} - professionelles Braiding in {city['city']}.",
        bio_fr=f"{business_name} - tressage professionnel a {city['city']}.",
        bio_en_source=BioSource.HUMAN,
        bio_de_source=BioSource.MACHINE,
        bio_fr_source=BioSource.MACHINE,
    )

    status = BraiderOnboardingStatus(user_id=user.id)
    for step in _ALL_STEPS:
        mark_step_complete(status, step)

    # ~4km of jitter so braiders in one city don't all stack on one pin.
    latitude = city["latitude"] + rng.uniform(-0.04, 0.04)
    longitude = city["longitude"] + rng.uniform(-0.06, 0.06)

    profile_rows: list[object] = [
        PortfolioImage(braider_id=profile.id, object_key=SHARED_PORTFOLIO_KEY, position=0),
        BraiderServiceLocation(
            braider_id=profile.id,
            location_type=rng.choice([LocationType.SALON, LocationType.HOME_STUDIO]),
            salon_name=business_name,
            address_line1=f"{rng.randint(1, 150)} {rng.choice(_STREETS[country])}",
            city=city["city"],
            postal_code=city["postal_code"],
            country=country,
            latitude=Decimal(f"{latitude:.6f}"),
            longitude=Decimal(f"{longitude:.6f}"),
            offers_mobile=rng.random() < 0.7,
            travel_radius_km=rng.choice([5, 10, 15, 20, 30]),
            travel_fee=Decimal(rng.choice(["0.00", "10.00", "15.00", "20.00"])),
        ),
        BraiderAvailabilitySettings(braider_id=profile.id, timezone=_TIMEZONES[country]),
        *(
            BraiderWeeklyAvailability(
                braider_id=profile.id, day_of_week=day, start_time=start, end_time=end
            )
            for day, start, end in _WEEKLY_WINDOWS
        ),
        # Placeholder until scripts.link_seed_braiders_stripe swaps it for a
        # real test-mode Connect account.
        StripeConnectAccount(
            braider_id=profile.id,
            stripe_account_id=f"acct_seed_{uuid.uuid4().hex[:16]}",
            charges_enabled=True,
            payouts_enabled=True,
            details_submitted=True,
        ),
    ]
    for slug in rng.sample(_STYLE_SLUGS, k=rng.randint(2, 4)):
        style_id = style_ids.get(slug)
        if style_id is None:
            continue  # styles catalog not seeded yet - skip, don't fail the whole braider
        profile_rows.append(
            BraiderStyle(
                braider_id=profile.id,
                style_id=style_id,
                base_price=Decimal(rng.choice(["45.00", "60.00", "75.00", "90.00", "120.00"])),
                duration_minutes=rng.choice([60, 90, 120, 150, 180]),
            )
        )

    return user, [profile, status], profile_rows


async def main(count: int) -> None:
    # Hashed once and shared - hashing 10k+ passwords individually would
    # dominate the run time, and every seed login uses the same password anyway.
    password_hash = hash_password(SEED_PASSWORD)

    async with AsyncSessionLocal() as db:
        existing = set(
            (
                await db.execute(
                    select(User.email).where(is_seed_user())
                )
            ).scalars()
        )
        # Plain ids, not Style objects - those would be expired/detached after
        # the first batch's commit + expunge_all.
        style_ids = {
            slug: style_id
            for slug, style_id in await db.execute(
                select(Style.slug, Style.id).where(Style.slug.in_(_STYLE_SLUGS))
            )
        }
        if not style_ids:
            print("! No styles found - run `python -m scripts.seed_styles` first for offerings.")

        todo = [i for i in range(1, count + 1) if seed_email(i) not in existing]
        print(f"{count - len(todo)} of {count} already exist, creating {len(todo)}...")

        by_country: dict[str, int] = {}
        for start in range(0, len(todo), BATCH_SIZE):
            users: list[object] = []
            user_children: list[object] = []
            profile_children: list[object] = []
            for index in todo[start : start + BATCH_SIZE]:
                user, uc, pc = _build_braider(index, password_hash, style_ids)
                users.append(user)
                user_children.extend(uc)
                profile_children.extend(pc)
                country = COUNTRIES[index % len(COUNTRIES)]
                by_country[country] = by_country.get(country, 0) + 1

            # No relationship() between these models, so flush parents first
            # rather than relying on the unit of work to order the inserts.
            db.add_all(users)
            await db.flush()
            db.add_all(user_children)
            await db.flush()
            db.add_all(profile_children)
            await db.commit()
            db.expunge_all()  # keep the identity map from growing to 150k+ objects
            print(f"  + {min(start + BATCH_SIZE, len(todo))}/{len(todo)}")

    print(f"\nDone: {len(todo)} new braider(s) created.")
    print(f"New braiders by country: {by_country}")
    print(
        f"All seeded logins use password: {SEED_PASSWORD!r}, "
        f"email pattern braiderNNNNN@{SEED_EMAIL_DOMAIN}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    asyncio.run(main(parser.parse_args().count))
