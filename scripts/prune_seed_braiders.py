"""Deletes seeded braiders (`braiderNNNNN@mailinator.com`, from
scripts.seed_braiders) that you don't want to keep:
  --keep N       keep only the first N (`braider00001`..`braiderNNNNN`)
  --linked-only  keep only braiders whose Stripe test account is real and
                 fully enabled (charges + payouts) - i.e. drop everyone
                 scripts.link_seed_braiders_stripe couldn't link
Both can be combined.

Only ever touches users matching the exact seed email pattern (never other
mailinator.com users). Deleting the user cascades
to their profile, onboarding status, location, availability, styles,
portfolio rows, and StripeConnectAccount row. A seed braider that has any
bookings, reviews, or chat threads is kept and reported instead (those FKs
are NO ACTION - deleting would either fail or destroy real test activity).

Run from the repo root:
    python -m scripts.prune_seed_braiders --keep 1000 --dry-run
    python -m scripts.prune_seed_braiders --linked-only
"""
import argparse
import asyncio

from sqlalchemy import and_, delete, exists, or_, select

from app.core.database import AsyncSessionLocal
from app.modules.bookings.models import Booking
from app.modules.braiders.models import BraiderProfile
from app.modules.braiders.payment_setup.models import StripeConnectAccount
from app.modules.chat.models import ChatThread
from app.modules.reviews.models import Review
from app.modules.users.models import User
from scripts.seed_braiders import is_seed_user, seed_email


async def main(keep: int | None, linked_only: bool, dry_run: bool) -> None:
    kept_emails = {seed_email(i) for i in range(1, keep + 1)} if keep is not None else None

    async with AsyncSessionLocal() as db:
        has_activity = or_(
            exists().where(Booking.braider_id == BraiderProfile.id),
            exists().where(Review.braider_id == BraiderProfile.id),
            exists().where(ChatThread.braider_user_id == User.id),
        )
        rows = (
            await db.execute(
                select(
                    User.id,
                    User.email,
                    has_activity.label("has_activity"),
                    exists()
                    .where(
                        and_(
                            StripeConnectAccount.braider_id == BraiderProfile.id,
                            StripeConnectAccount.stripe_account_id.not_like("acct_seed_%"),
                            StripeConnectAccount.charges_enabled,
                            StripeConnectAccount.payouts_enabled,
                        )
                    )
                    .label("is_linked"),
                )
                .outerjoin(BraiderProfile, BraiderProfile.user_id == User.id)
                .where(is_seed_user())
            )
        ).all()

        def wanted(r) -> bool:
            if kept_emails is not None and r.email not in kept_emails:
                return False
            return r.is_linked or not linked_only

        to_delete = [r.id for r in rows if not wanted(r) and not r.has_activity]
        blocked = [r.email for r in rows if not wanted(r) and r.has_activity]
        kept = sum(1 for r in rows if wanted(r))

        print(f"Seed braiders: {len(rows)} total, keeping {kept}, deleting {len(to_delete)}.")
        if blocked:
            print(f"Keeping {len(blocked)} extra with bookings/reviews/chats: {blocked[:10]}...")
        if dry_run:
            print("Dry run - nothing deleted.")
            return

        for start in range(0, len(to_delete), 1000):
            await db.execute(delete(User).where(User.id.in_(to_delete[start : start + 1000])))
        await db.commit()

    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", type=int, default=None)
    parser.add_argument("--linked-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.keep is None and not args.linked_only:
        parser.error("pass --keep N and/or --linked-only")
    asyncio.run(main(args.keep, args.linked_only, args.dry_run))
