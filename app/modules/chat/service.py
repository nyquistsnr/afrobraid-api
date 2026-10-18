import uuid
from datetime import UTC, datetime

from arq import ArqRedis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import ws
from app.core.config import get_settings
from app.core.exceptions import (
    BookingNotFoundError,
    ChatAccessDeniedError,
    ChatMessageNotFoundError,
    ChatNotAvailableError,
    ChatReportNotFoundError,
    ChatThreadNotFoundError,
)
from app.core.i18n import t
from app.core.pagination import PaginatedData, PaginationParams
from app.core.security import hash_content
from app.modules.bookings import repository as bookings_repo
from app.modules.braiders import repository as braiders_repo
from app.modules.chat import repository as chat_repo
from app.modules.chat.models import (
    ChatMessage,
    ChatMessageStatus,
    ChatReportStatus,
    ChatThread,
    ChatTranslationStatus,
)
from app.modules.chat.moderation import detect_violations
from app.modules.chat.schemas import (
    AdminChatReportResponse,
    AdminChatReportUpdateRequest,
    ChatMessageCreateRequest,
    ChatMessageResponse,
    ChatReportCreateRequest,
    ChatReportResponse,
    ChatThreadResponse,
)
from app.modules.chat.tasks import TASK_TRANSLATE_CHAT_MESSAGE
from app.modules.notifications import service as notifications_service
from app.modules.notifications.models import NotificationType
from app.modules.users import repository as users_repo
from app.shared.links import build_braider_frontend_url, build_customer_frontend_url

settings = get_settings()


async def _get_thread_or_403(db: AsyncSession, thread_id: uuid.UUID, user_id: uuid.UUID) -> ChatThread:
    thread = await chat_repo.get_thread_by_id(db, thread_id)
    if thread is None:
        raise ChatThreadNotFoundError()
    if user_id not in (thread.customer_id, thread.braider_user_id):
        raise ChatAccessDeniedError()
    return thread


def _other_participant_id(thread: ChatThread, user_id: uuid.UUID) -> uuid.UUID:
    return thread.braider_user_id if user_id == thread.customer_id else thread.customer_id


def _to_message_response(message: ChatMessage, *, locale: str) -> ChatMessageResponse:
    violation_notice = (
        t("chat.message_flagged_notice", locale) if message.status == ChatMessageStatus.FLAGGED else None
    )
    return ChatMessageResponse(
        id=message.id,
        thread_id=message.thread_id,
        sender_id=message.sender_id,
        status=message.status,
        body=message.body,
        body_locale=message.body_locale,
        translated_body=message.translated_body,
        translated_locale=message.translated_locale,
        violation_notice=violation_notice,
        created_at=message.created_at,
    )


async def _to_thread_response(
    db: AsyncSession, thread: ChatThread, *, requester_id: uuid.UUID
) -> ChatThreadResponse:
    other_id = _other_participant_id(thread, requester_id)
    names = await users_repo.list_full_names(db, [other_id])
    unread_count = await chat_repo.count_unread(db, thread, requester_id)
    return ChatThreadResponse(
        id=thread.id,
        booking_id=thread.booking_id,
        other_participant_id=other_id,
        other_participant_name=names.get(other_id, ""),
        last_message_at=thread.last_message_at,
        last_message_preview=thread.last_message_preview,
        last_message_flagged=thread.last_message_flagged,
        unread_count=unread_count,
        created_at=thread.created_at,
    )


async def get_or_create_thread_for_booking(
    db: AsyncSession, *, booking_id: uuid.UUID, user_id: uuid.UUID
) -> ChatThreadResponse:
    booking = await bookings_repo.get_booking_by_id(db, booking_id)
    if booking is None:
        raise BookingNotFoundError()

    profile = await braiders_repo.get_profile_by_id(db, booking.braider_id)
    if profile is None or user_id not in (booking.customer_id, profile.user_id):
        raise ChatAccessDeniedError()

    if booking.confirmed_at is None:
        raise ChatNotAvailableError()

    thread = await chat_repo.get_thread_by_booking(db, booking_id)
    if thread is None:
        thread = await chat_repo.create_thread(
            db, booking_id=booking_id, customer_id=booking.customer_id, braider_user_id=profile.user_id
        )
        await db.commit()
        await db.refresh(thread)

    return await _to_thread_response(db, thread, requester_id=user_id)


async def list_my_threads(
    db: AsyncSession, *, user_id: uuid.UUID, params: PaginationParams
) -> PaginatedData[ChatThreadResponse]:
    items, meta = await chat_repo.list_threads_for_user(db, user_id, params=params)
    other_ids = [_other_participant_id(t, user_id) for t in items]
    names = await users_repo.list_full_names(db, other_ids)
    unread_counts = await chat_repo.get_unread_counts(db, [t.id for t in items], user_id)

    responses = [
        ChatThreadResponse(
            id=thread.id,
            booking_id=thread.booking_id,
            other_participant_id=_other_participant_id(thread, user_id),
            other_participant_name=names.get(_other_participant_id(thread, user_id), ""),
            last_message_at=thread.last_message_at,
            last_message_preview=thread.last_message_preview,
            last_message_flagged=thread.last_message_flagged,
            unread_count=unread_counts.get(thread.id, 0),
            created_at=thread.created_at,
        )
        for thread in items
    ]
    return PaginatedData(items=responses, pagination=meta)


async def list_messages(
    db: AsyncSession, *, thread_id: uuid.UUID, user_id: uuid.UUID, params: PaginationParams, locale: str
) -> PaginatedData[ChatMessageResponse]:
    thread = await _get_thread_or_403(db, thread_id, user_id)
    items, meta = await chat_repo.list_messages_for_thread(db, thread.id, params=params)
    return PaginatedData(items=[_to_message_response(m, locale=locale) for m in items], pagination=meta)


async def send_message(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    sender_id: uuid.UUID,
    data: ChatMessageCreateRequest,
    request_locale: str,
    queue: ArqRedis,
) -> ChatMessageResponse:
    thread = await _get_thread_or_403(db, thread_id, sender_id)

    sender = await users_repo.get_user_by_id(db, sender_id)
    assert sender is not None
    recipient_id = _other_participant_id(thread, sender_id)
    recipient = await users_repo.get_user_by_id(db, recipient_id)
    assert recipient is not None

    violations = detect_violations(data.body)
    now = datetime.now(UTC)

    if violations:
        message = await chat_repo.create_message(
            db,
            thread_id=thread.id,
            sender_id=sender_id,
            status=ChatMessageStatus.FLAGGED,
            body=None,
            body_locale=None,
            body_hash=hash_content(data.body),
            violation_types=",".join(violations),
        )
        thread.last_message_preview = None
        thread.last_message_flagged = True
    else:
        message = await chat_repo.create_message(
            db,
            thread_id=thread.id,
            sender_id=sender_id,
            status=ChatMessageStatus.SENT,
            body=data.body,
            body_locale=sender.chat_locale,
            body_hash=None,
            violation_types=None,
        )
        thread.last_message_preview = data.body[:280]
        thread.last_message_flagged = False

    thread.last_message_at = now

    # Only ever translate between the two locales these two participants
    # have actually chosen for chat - never fanned out to every supported
    # locale like reviews/bios are (see users.models.User.chat_locale).
    target_locale: str | None = None
    if (
        message.status == ChatMessageStatus.SENT
        and sender.chat_locale
        and recipient.chat_locale
        and sender.chat_locale != recipient.chat_locale
    ):
        message.translation_status = ChatTranslationStatus.PENDING
        target_locale = recipient.chat_locale

    is_flagged = message.status == ChatMessageStatus.FLAGGED
    # Computed here (rather than alongside sender_response/recipient_response
    # below) because the notification's {link} is baked in at creation time,
    # not re-derived from the reader's locale on every read like title/body.
    recipient_locale = recipient.chat_locale or settings.default_locale
    chat_link = (
        build_customer_frontend_url(locale=recipient_locale, path=f"chat/{thread.id}")
        if recipient_id == thread.customer_id
        else build_braider_frontend_url(locale=recipient_locale, path=f"chat/{thread.id}")
    )
    notification = await notifications_service.create(
        db,
        user_id=recipient_id,
        type=NotificationType.CHAT_MESSAGE_FLAGGED if is_flagged else NotificationType.CHAT_NEW_MESSAGE,
        title_key="notifications.chat_message_flagged_title"
        if is_flagged
        else "notifications.chat_new_message_title",
        body_key="notifications.chat_message_flagged_body" if is_flagged else "notifications.chat_new_message_body",
        body_params={
            "sender_name": f"{sender.first_name} {sender.last_name or ''}".strip(),
            "link": chat_link,
        },
        related_type="chat_thread",
        related_id=thread.id,
    )

    await db.commit()
    await db.refresh(message)
    await db.refresh(thread)
    await db.refresh(notification)

    if target_locale:
        await queue.enqueue_job(
            TASK_TRANSLATE_CHAT_MESSAGE,
            message_id=str(message.id),
            thread_id=str(thread.id),
            source_locale=sender.chat_locale,
            target_locale=target_locale,
            customer_id=str(thread.customer_id),
            braider_user_id=str(thread.braider_user_id),
        )

    # Each participant gets the push rendered in their own best-known locale -
    # the sender's current request locale, and the recipient's chat_locale (or
    # the platform default if they haven't set one) since there's no live
    # request to read a locale off of for them.
    sender_response = _to_message_response(message, locale=request_locale)
    recipient_response = _to_message_response(message, locale=recipient_locale)

    await ws.publish_event(
        sender_id,
        {"type": "chat_message", "thread_id": str(thread.id), "message": sender_response.model_dump(mode="json")},
    )
    await ws.publish_event(
        recipient_id,
        {"type": "chat_message", "thread_id": str(thread.id), "message": recipient_response.model_dump(mode="json")},
    )
    await notifications_service.publish_realtime(notification, locale=recipient_locale)

    return sender_response


async def mark_read(db: AsyncSession, *, thread_id: uuid.UUID, user_id: uuid.UUID) -> ChatThreadResponse:
    thread = await _get_thread_or_403(db, thread_id, user_id)
    chat_repo.mark_read(thread, user_id=user_id, at=datetime.now(UTC))
    await db.commit()
    await db.refresh(thread)
    return await _to_thread_response(db, thread, requester_id=user_id)


async def create_report(
    db: AsyncSession, *, thread_id: uuid.UUID, reporter_id: uuid.UUID, data: ChatReportCreateRequest
) -> ChatReportResponse:
    thread = await _get_thread_or_403(db, thread_id, reporter_id)
    reported_user_id = _other_participant_id(thread, reporter_id)

    if data.message_id is not None:
        message = await chat_repo.get_message_by_id(db, data.message_id)
        if message is None or message.thread_id != thread.id:
            raise ChatMessageNotFoundError()

    report = await chat_repo.create_report(
        db,
        thread_id=thread.id,
        reporter_id=reporter_id,
        reported_user_id=reported_user_id,
        message_id=data.message_id,
        reason=data.reason,
        details=data.details,
    )
    await db.commit()
    await db.refresh(report)
    return ChatReportResponse(
        id=report.id,
        thread_id=report.thread_id,
        reported_user_id=report.reported_user_id,
        reason=report.reason,
        status=report.status,
        created_at=report.created_at,
    )


async def _to_admin_report_response(db: AsyncSession, report) -> AdminChatReportResponse:
    thread = await chat_repo.get_thread_by_id(db, report.thread_id)
    assert thread is not None
    names = await users_repo.list_full_names(db, [report.reporter_id, report.reported_user_id])
    return _build_admin_report_response(report, booking_id=thread.booking_id, names=names)


def _build_admin_report_response(
    report, *, booking_id: uuid.UUID, names: dict[uuid.UUID, str]
) -> AdminChatReportResponse:
    return AdminChatReportResponse(
        id=report.id,
        thread_id=report.thread_id,
        booking_id=booking_id,
        reporter_id=report.reporter_id,
        reporter_name=names.get(report.reporter_id, ""),
        reported_user_id=report.reported_user_id,
        reported_user_name=names.get(report.reported_user_id, ""),
        message_id=report.message_id,
        reason=report.reason,
        details=report.details,
        status=report.status,
        admin_notes=report.admin_notes,
        created_at=report.created_at,
        updated_at=report.updated_at,
    )


async def list_admin_reports(
    db: AsyncSession, *, params: PaginationParams, status: ChatReportStatus | None
) -> PaginatedData[AdminChatReportResponse]:
    items, meta = await chat_repo.list_reports_for_admin(db, params=params, status=status)
    # Two batched lookups for the whole page instead of two queries per report.
    threads = await chat_repo.list_threads_by_ids(db, list({r.thread_id for r in items}))
    names = await users_repo.list_full_names(
        db, list({uid for r in items for uid in (r.reporter_id, r.reported_user_id)})
    )
    return PaginatedData(
        items=[
            _build_admin_report_response(r, booking_id=threads[r.thread_id].booking_id, names=names)
            for r in items
        ],
        pagination=meta,
    )


async def update_report(
    db: AsyncSession, *, report_id: uuid.UUID, data: AdminChatReportUpdateRequest
) -> AdminChatReportResponse:
    report = await chat_repo.get_report_by_id(db, report_id)
    if report is None:
        raise ChatReportNotFoundError()

    report.status = data.status
    if data.admin_notes is not None:
        report.admin_notes = data.admin_notes

    await db.commit()
    await db.refresh(report)
    return await _to_admin_report_response(db, report)
