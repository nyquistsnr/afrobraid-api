import uuid
from datetime import datetime

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import PaginationParams, paginate
from app.modules.chat.models import ChatMessage, ChatReport, ChatReportStatus, ChatThread


async def get_thread_by_id(db: AsyncSession, thread_id: uuid.UUID) -> ChatThread | None:
    return await db.get(ChatThread, thread_id)


async def list_threads_by_ids(
    db: AsyncSession, thread_ids: list[uuid.UUID]
) -> dict[uuid.UUID, ChatThread]:
    if not thread_ids:
        return {}
    result = await db.execute(select(ChatThread).where(ChatThread.id.in_(thread_ids)))
    return {thread.id: thread for thread in result.scalars().all()}


async def get_thread_by_booking(db: AsyncSession, booking_id: uuid.UUID) -> ChatThread | None:
    result = await db.execute(select(ChatThread).where(ChatThread.booking_id == booking_id))
    return result.scalar_one_or_none()


async def create_thread(
    db: AsyncSession, *, booking_id: uuid.UUID, customer_id: uuid.UUID, braider_user_id: uuid.UUID
) -> ChatThread:
    thread = ChatThread(booking_id=booking_id, customer_id=customer_id, braider_user_id=braider_user_id)
    db.add(thread)
    await db.flush()
    return thread


async def list_threads_for_user(
    db: AsyncSession, user_id: uuid.UUID, *, params: PaginationParams
) -> tuple[list[ChatThread], object]:
    stmt = (
        select(ChatThread)
        .where(or_(ChatThread.customer_id == user_id, ChatThread.braider_user_id == user_id))
        .order_by(ChatThread.last_message_at.desc().nulls_last(), ChatThread.created_at.desc())
    )
    return await paginate(db, stmt, params)


async def get_unread_counts(
    db: AsyncSession, thread_ids: list[uuid.UUID], user_id: uuid.UUID
) -> dict[uuid.UUID, int]:
    """Bulk unread-message count per thread for `user_id` - one grouped query
    rather than a per-thread lookup. The "unread" cutoff is whichever side of
    the thread `user_id` is on (customer_last_read_at vs braider_last_read_at),
    picked per-row via CASE since a single page of threads can have the user
    on either side depending on the thread."""
    if not thread_ids:
        return {}

    cutoff = case(
        (ChatThread.customer_id == user_id, ChatThread.customer_last_read_at),
        (ChatThread.braider_user_id == user_id, ChatThread.braider_last_read_at),
    )
    stmt = (
        select(ChatMessage.thread_id, func.count(ChatMessage.id))
        .join(ChatThread, ChatThread.id == ChatMessage.thread_id)
        .where(
            ChatThread.id.in_(thread_ids),
            ChatMessage.sender_id != user_id,
            or_(cutoff.is_(None), ChatMessage.created_at > cutoff),
        )
        .group_by(ChatMessage.thread_id)
    )
    result = await db.execute(stmt)
    return {row[0]: row[1] for row in result.all()}


async def count_unread(db: AsyncSession, thread: ChatThread, user_id: uuid.UUID) -> int:
    last_read_at = (
        thread.customer_last_read_at if thread.customer_id == user_id else thread.braider_last_read_at
    )
    stmt = select(func.count(ChatMessage.id)).where(
        ChatMessage.thread_id == thread.id, ChatMessage.sender_id != user_id
    )
    if last_read_at is not None:
        stmt = stmt.where(ChatMessage.created_at > last_read_at)
    return (await db.scalar(stmt)) or 0


def mark_read(thread: ChatThread, *, user_id: uuid.UUID, at: datetime) -> None:
    if thread.customer_id == user_id:
        thread.customer_last_read_at = at
    else:
        thread.braider_last_read_at = at


async def create_message(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    sender_id: uuid.UUID,
    status,
    body: str | None,
    body_locale: str | None,
    body_hash: str | None,
    violation_types: str | None,
) -> ChatMessage:
    message = ChatMessage(
        thread_id=thread_id,
        sender_id=sender_id,
        status=status,
        body=body,
        body_locale=body_locale,
        body_hash=body_hash,
        violation_types=violation_types,
    )
    db.add(message)
    await db.flush()
    return message


async def get_message_by_id(db: AsyncSession, message_id: uuid.UUID) -> ChatMessage | None:
    return await db.get(ChatMessage, message_id)


async def list_messages_for_thread(
    db: AsyncSession, thread_id: uuid.UUID, *, params: PaginationParams
) -> tuple[list[ChatMessage], object]:
    stmt = (
        select(ChatMessage).where(ChatMessage.thread_id == thread_id).order_by(ChatMessage.created_at.desc())
    )
    return await paginate(db, stmt, params)


async def create_report(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    reporter_id: uuid.UUID,
    reported_user_id: uuid.UUID,
    message_id: uuid.UUID | None,
    reason,
    details: str | None,
) -> ChatReport:
    report = ChatReport(
        thread_id=thread_id,
        reporter_id=reporter_id,
        reported_user_id=reported_user_id,
        message_id=message_id,
        reason=reason,
        details=details,
    )
    db.add(report)
    await db.flush()
    return report


async def get_report_by_id(db: AsyncSession, report_id: uuid.UUID) -> ChatReport | None:
    return await db.get(ChatReport, report_id)


async def list_reports_for_admin(
    db: AsyncSession, *, params: PaginationParams, status: ChatReportStatus | None
) -> tuple[list[ChatReport], object]:
    stmt = select(ChatReport)
    if status is not None:
        stmt = stmt.where(ChatReport.status == status)
    stmt = stmt.order_by(ChatReport.created_at.desc())
    return await paginate(db, stmt, params)
