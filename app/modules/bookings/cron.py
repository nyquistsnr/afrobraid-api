import logging
from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.modules.bookings import repository as bookings_repo
from app.modules.bookings.tasks import (
    TASK_CHARGE_BOOKING_BALANCE,
    TASK_RELEASE_BOOKING_PAYOUT,
    release_abandoned_balance_payment,
)

logger = logging.getLogger("app.cron.bookings")
settings = get_settings()

_EXPIRE_BATCH_LIMIT = 5000
_BALANCE_SWEEP_BATCH_LIMIT = 500
_LIFECYCLE_BATCH_LIMIT = 2000
_PAYOUT_SWEEP_BATCH_LIMIT = 500


async def expire_booking_holds_cron(ctx: dict) -> None:
    """Releases a PENDING_PAYMENT booking's calendar hold once its
    hold_expires_at has passed with no successful payment - runs every
    minute, matching the plan's `expire_booking_holds` cron."""
    async with AsyncSessionLocal() as db:
        expired = await bookings_repo.expire_stale_holds(db, limit=_EXPIRE_BATCH_LIMIT)
        await db.commit()
        if expired:
            logger.info("Expired %d stale PENDING_PAYMENT booking holds", expired)


async def sweep_balance_charges_cron(ctx: dict) -> None:
    """The primary balance driver (plan: `sweep_balance_charges`, every 5m).
    Claims every booking whose deposit-then-balance schedule has come due
    (SCHEDULED -> DUE, batched and atomic - see claim_due_balance_charges)
    and enqueues one charge_booking_balance_task per booking. Deliberately
    does no Stripe I/O itself - that happens in the task, off the cron's
    critical path, so a slow/stuck charge can't back up the whole sweep."""
    async with AsyncSessionLocal() as db:
        claimed = await bookings_repo.claim_due_balance_charges(db, limit=_BALANCE_SWEEP_BATCH_LIMIT)
        await db.commit()
    for booking_id in claimed:
        await ctx["redis"].enqueue_job(TASK_CHARGE_BOOKING_BALANCE, booking_id=str(booking_id))
    if claimed:
        logger.info("Enqueued %d due balance charges", len(claimed))


async def release_abandoned_balance_payments_cron(ctx: dict) -> None:
    """Hands bookings stuck IN_PROGRESS on an abandoned customer-initiated
    balance payment back to the retry ladder (every 15m) - see
    release_abandoned_balance_payment. Each booking gets its own
    try/except so one Stripe error can't stall the batch."""
    idle_since = datetime.now(UTC) - timedelta(minutes=settings.booking_balance_on_session_abandon_after_minutes)
    async with AsyncSessionLocal() as db:
        candidates = await bookings_repo.list_bookings_with_abandoned_on_session_balance(
            db, idle_since=idle_since, limit=_BALANCE_SWEEP_BATCH_LIMIT
        )
        await db.rollback()
        released = 0
        for booking_id in candidates:
            try:
                if await release_abandoned_balance_payment(ctx, db, booking_id, idle_since=idle_since):
                    released += 1
            except Exception:
                logger.exception("Releasing abandoned balance payment failed for booking %s", booking_id)
                await db.rollback()
    if candidates:
        logger.info("Released %d of %d abandoned on-session balance payments", released, len(candidates))


async def start_due_bookings_cron(ctx: dict) -> None:
    """CONFIRMED -> IN_PROGRESS once starts_at has passed (plan:
    `start_due_bookings`, every 15m). A plain bulk flip, no Stripe I/O."""
    async with AsyncSessionLocal() as db:
        started = await bookings_repo.start_due_bookings(db, limit=_LIFECYCLE_BATCH_LIMIT)
        await db.commit()
        if started:
            logger.info("Started %d due bookings", started)


async def complete_due_bookings_cron(ctx: dict) -> None:
    """IN_PROGRESS -> COMPLETED once ends_at has passed (plan:
    `complete_due_bookings`, every 15m). Payout eligibility is computed
    off ends_at directly (see release_due_payouts_cron), so this doesn't
    need to touch Stripe or stamp a separate completion timestamp."""
    async with AsyncSessionLocal() as db:
        completed = await bookings_repo.complete_due_bookings(db, limit=_LIFECYCLE_BATCH_LIMIT)
        await db.commit()
        if completed:
            logger.info("Completed %d due bookings", completed)


async def release_due_payouts_cron(ctx: dict) -> None:
    """Braiders get paid (plan: `release_due_payouts`, every 15m).
    COMPLETED/NO_SHOW, not payouts_frozen, ends_at +
    booking_payout_release_delay_hours (48h) has passed, and still has
    money to move - see list_bookings_due_for_payout for why this doesn't
    need a claim/lock step the way the balance sweeper does. Enqueues one
    release_booking_payout_task per booking, keeping Stripe I/O off the
    cron's critical path."""
    async with AsyncSessionLocal() as db:
        due = await bookings_repo.list_bookings_due_for_payout(db, limit=_PAYOUT_SWEEP_BATCH_LIMIT)
    for booking_id in due:
        await ctx["redis"].enqueue_job(TASK_RELEASE_BOOKING_PAYOUT, booking_id=str(booking_id))
    if due:
        logger.info("Enqueued %d due payouts", len(due))
