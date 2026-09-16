import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.i18n import t

logger = logging.getLogger("app")


class AppError(Exception):
    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "APP_ERROR"
    message_key: str = "errors.generic"

    def __init__(self, **format_kwargs: Any) -> None:
        self.format_kwargs = format_kwargs
        super().__init__(self.code)


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "NOT_FOUND"
    message_key = "errors.not_found"


class InvalidCredentialsError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "INVALID_CREDENTIALS"
    message_key = "auth.invalid_credentials"


class EmailNotVerifiedError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "EMAIL_NOT_VERIFIED"
    message_key = "auth.email_not_verified"


class EmailAlreadyExistsError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "EMAIL_ALREADY_EXISTS"
    message_key = "auth.email_already_exists"


class PhoneAlreadyExistsError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "PHONE_ALREADY_EXISTS"
    message_key = "auth.phone_already_exists"


class InvalidOtpError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_OTP"
    message_key = "auth.invalid_otp"


class OtpExpiredError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "OTP_EXPIRED"
    message_key = "auth.otp_expired"


class TooManyOtpAttemptsError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "TOO_MANY_OTP_ATTEMPTS"
    message_key = "auth.too_many_otp_attempts"


class InvalidRefreshTokenError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "INVALID_REFRESH_TOKEN"
    message_key = "auth.invalid_refresh_token"


class InvalidAccessTokenError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "INVALID_ACCESS_TOKEN"
    message_key = "auth.invalid_access_token"


class UserNotActiveError(AppError):
    """Raised for any deactivated account, admin-suspended or otherwise.
    `reason` is only ever set for a suspension (see users/admin_router.py) -
    when present it's interpolated straight into the login error message so
    the user sees why without any frontend change, following the same
    raise-time-kwargs pattern as RateLimitedError below."""

    status_code = status.HTTP_403_FORBIDDEN
    code = "USER_NOT_ACTIVE"

    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason
        if reason:
            self.message_key = "auth.user_suspended_with_reason"
            super().__init__(reason=reason)
        else:
            self.message_key = "auth.user_not_active"
            super().__init__()


class UserNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "USER_NOT_FOUND"
    message_key = "users.not_found"


class CannotSuspendSelfError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "CANNOT_SUSPEND_SELF"
    message_key = "users.cannot_suspend_self"


class ForbiddenRoleError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "FORBIDDEN"
    message_key = "auth.forbidden"


class AdminSignupBlockedError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "ADMIN_SIGNUP_BLOCKED"
    message_key = "auth.admin_signup_blocked"


class AdminInviteInvalidError(AppError):
    """Covers every "can't use this invite" case (unknown token, expired,
    already accepted, revoked) - deliberately one generic error rather than
    distinguishing them, so an invite link doesn't leak its own state to
    whoever is holding it."""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "ADMIN_INVITE_INVALID"
    message_key = "auth.admin_invite_invalid"


class AdminInviteEmailMismatchError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "ADMIN_INVITE_EMAIL_MISMATCH"
    message_key = "auth.admin_invite_email_mismatch"


class SocialAuthError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "SOCIAL_AUTH_FAILED"
    message_key = "auth.social_auth_failed"


class UnsupportedProviderError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "UNSUPPORTED_PROVIDER"
    message_key = "auth.unsupported_provider"


class UserTypeRequiredError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "USER_TYPE_REQUIRED"
    message_key = "auth.user_type_required"


class InvalidLogoUploadError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_LOGO_UPLOAD"
    message_key = "braider.invalid_logo_upload"


class LogoNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "LOGO_NOT_FOUND"
    message_key = "braider.logo_not_found"


class BraiderNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "BRAIDER_NOT_FOUND"
    message_key = "braider.not_found"


class InvalidSearchLocationError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_SEARCH_LOCATION"
    message_key = "braider.invalid_search_location"


class InvalidSearchDateRangeError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_SEARCH_DATE_RANGE"
    message_key = "braider.invalid_search_date_range"


class InvalidSearchPriceRangeError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_SEARCH_PRICE_RANGE"
    message_key = "braider.invalid_search_price_range"


class InvalidSearchRatingRangeError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_SEARCH_RATING_RANGE"
    message_key = "braider.invalid_search_rating_range"


class InvalidTrendingLocationError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_TRENDING_LOCATION"
    message_key = "braider.invalid_trending_location"


class VeriffApiUnavailableError(AppError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "VERIFF_API_UNAVAILABLE"
    message_key = "veriff.api_unavailable"


class InvalidWebhookSignatureError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "INVALID_WEBHOOK_SIGNATURE"
    message_key = "veriff.invalid_webhook_signature"


class VeriffSessionNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "VERIFF_SESSION_NOT_FOUND"
    message_key = "veriff.session_not_found"


class PhoneVerificationApiUnavailableError(AppError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "PHONE_VERIFICATION_API_UNAVAILABLE"
    message_key = "phone_verification.api_unavailable"


class InvalidVerificationCodeError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_VERIFICATION_CODE"
    message_key = "phone_verification.invalid_code"


class StyleCategoryNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "STYLE_CATEGORY_NOT_FOUND"
    message_key = "styles.category_not_found"


class StyleNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "STYLE_NOT_FOUND"
    message_key = "styles.style_not_found"


class StyleNotActiveError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "STYLE_NOT_ACTIVE"
    message_key = "styles.style_not_active"


class StyleVariationNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "STYLE_VARIATION_NOT_FOUND"
    message_key = "styles.variation_not_found"


class AddOnNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "ADDON_NOT_FOUND"
    message_key = "styles.addon_not_found"


class InvalidStyleImageUploadError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_STYLE_IMAGE_UPLOAD"
    message_key = "styles.invalid_image_upload"


class StyleImageNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "STYLE_IMAGE_NOT_FOUND"
    message_key = "styles.image_not_found"


class MaxStyleImagesReachedError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "MAX_STYLE_IMAGES_REACHED"
    message_key = "styles.max_images_reached"


class EntityInUseError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "ENTITY_IN_USE"
    message_key = "styles.entity_in_use"


class BraiderStyleNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "BRAIDER_STYLE_NOT_FOUND"
    message_key = "braider_offerings.style_not_found"


class BraiderStyleAlreadyExistsError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "BRAIDER_STYLE_ALREADY_EXISTS"
    message_key = "braider_offerings.style_already_exists"


class BraiderStyleVariationInvalidError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_STYLE_VARIATION"
    message_key = "braider_offerings.invalid_variation"


class BraiderStyleAddonInvalidError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_ADDON"
    message_key = "braider_offerings.invalid_addon"


class AvailabilitySettingsNotConfiguredError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "AVAILABILITY_SETTINGS_NOT_CONFIGURED"
    message_key = "availability.settings_not_configured"


class InvalidTimezoneError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "INVALID_TIMEZONE"
    message_key = "availability.invalid_timezone"


class WeeklyWindowNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "WEEKLY_WINDOW_NOT_FOUND"
    message_key = "availability.window_not_found"


class InvalidAvailabilityWindowTimesError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "INVALID_AVAILABILITY_WINDOW_TIMES"
    message_key = "availability.invalid_window_times"


class OverlappingAvailabilityWindowError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "OVERLAPPING_AVAILABILITY_WINDOW"
    message_key = "availability.overlapping_window"


class AvailabilityExceptionNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "AVAILABILITY_EXCEPTION_NOT_FOUND"
    message_key = "availability.exception_not_found"


class OverlappingAvailabilityExceptionError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "OVERLAPPING_AVAILABILITY_EXCEPTION"
    message_key = "availability.overlapping_exception"


class BraiderStyleDurationMissingError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "BRAIDER_STYLE_DURATION_MISSING"
    message_key = "availability.style_duration_missing"


class StripeApiUnavailableError(AppError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "STRIPE_API_UNAVAILABLE"
    message_key = "payment_setup.api_unavailable"


class StripeAccountNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "STRIPE_ACCOUNT_NOT_FOUND"
    message_key = "payment_setup.account_not_found"


class StripeInvalidWebhookSignatureError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "STRIPE_INVALID_WEBHOOK_SIGNATURE"
    message_key = "payment_setup.invalid_webhook_signature"


class BraiderCountryRequiredError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "BRAIDER_COUNTRY_REQUIRED"
    message_key = "payment_setup.country_required"


class InvalidPortfolioImageUploadError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_PORTFOLIO_IMAGE_UPLOAD"
    message_key = "portfolio.invalid_image_upload"


class PortfolioImageNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "PORTFOLIO_IMAGE_NOT_FOUND"
    message_key = "portfolio.image_not_found"


class MaxPortfolioImagesReachedError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "MAX_PORTFOLIO_IMAGES_REACHED"
    message_key = "portfolio.max_images_reached"


class InvalidSettingValueError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_SETTING_VALUE"
    message_key = "platform_settings.invalid_percentage_value"


class InvalidCountryCodeError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "INVALID_COUNTRY_CODE"
    message_key = "platform_settings.invalid_country_code"


class CountryVatSettingsNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "COUNTRY_VAT_SETTINGS_NOT_FOUND"
    message_key = "platform_settings.country_vat_not_found"


class RateLimitedError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "RATE_LIMITED"
    message_key = "errors.rate_limited"

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(retry_after_seconds=retry_after_seconds)


class PricingInvariantError(AppError):
    """Raised when the pricing engine's own arithmetic invariants don't hold
    (e.g. line items don't sum to the total) - always a bug, never a user
    input problem, hence 500 rather than 4xx."""

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    code = "PRICING_INVARIANT_ERROR"
    message_key = "errors.internal_server_error"


class BookingCalculationNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "BOOKING_CALCULATION_NOT_FOUND"
    message_key = "booking_calculation.not_found"


class BookingCalculationExpiredError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "BOOKING_CALCULATION_EXPIRED"
    message_key = "booking_calculation.expired"


class BookingCalculationAlreadyUsedError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "BOOKING_CALCULATION_ALREADY_USED"
    message_key = "booking_calculation.already_used"


class MobileServiceNotOfferedError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "MOBILE_SERVICE_NOT_OFFERED"
    message_key = "booking_calculation.mobile_not_offered"


class ClientLocationMissingError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "CLIENT_LOCATION_MISSING"
    message_key = "booking_calculation.client_location_missing"


class MobileLocationOutOfRangeError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "MOBILE_LOCATION_OUT_OF_RANGE"
    message_key = "booking_calculation.mobile_location_out_of_range"


class BraiderCountryMissingError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "BRAIDER_COUNTRY_MISSING"
    message_key = "booking_calculation.braider_country_missing"


class BookingNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "BOOKING_NOT_FOUND"
    message_key = "booking.not_found"


class BookingStartsInPastError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "BOOKING_STARTS_IN_PAST"
    message_key = "booking.starts_in_past"


class SelfBookingNotAllowedError(AppError):
    """A braider can't book their own profile as a customer."""

    status_code = status.HTTP_403_FORBIDDEN
    code = "SELF_BOOKING_NOT_ALLOWED"
    message_key = "booking.self_booking_not_allowed"


class BraiderNotPayableError(AppError):
    """A braider without a fully-capable Stripe Connect account can't be
    booked - discovery already hides a braider who never finished payment
    setup, so in practice this only fires for one whose capabilities were
    later revoked by Stripe (payment_setup_completed_at doesn't un-set
    itself)."""

    status_code = status.HTTP_409_CONFLICT
    code = "BRAIDER_NOT_PAYABLE"
    message_key = "booking.braider_not_payable"


class BookingPriceDriftError(AppError):
    """The braider's catalog (price, duration, ...) or the effective
    platform settings changed between the calculation and the booking
    attempt. Never silently re-price - the customer must fetch a fresh
    quote and confirm the new total."""

    status_code = status.HTTP_409_CONFLICT
    code = "BOOKING_PRICE_DRIFT"
    message_key = "booking.price_drift"


class BookingSlotUnavailableError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "BOOKING_SLOT_UNAVAILABLE"
    message_key = "booking.slot_unavailable"


class BookingNotReschedulableError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "BOOKING_NOT_RESCHEDULABLE"
    message_key = "booking.not_reschedulable"


class BookingRescheduleWindowClosedError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "BOOKING_RESCHEDULE_WINDOW_CLOSED"
    message_key = "booking.reschedule_window_closed"


class BookingNotCancellableError(AppError):
    """Only CONFIRMED bookings can be cancelled - PENDING_PAYMENT ones have
    no successful payment yet (the hold just expires on its own) and every
    other status is already terminal or past the point cancellation makes
    sense."""

    status_code = status.HTTP_409_CONFLICT
    code = "BOOKING_NOT_CANCELLABLE"
    message_key = "booking.not_cancellable"


class BookingCancellationWindowClosedError(AppError):
    """Customer-only - the confirmed product decision is that a customer
    cannot cancel inside cancellation_cutoff_at (24h before the
    appointment). Braiders have no such window (they can always cancel)."""

    status_code = status.HTTP_409_CONFLICT
    code = "BOOKING_CANCELLATION_WINDOW_CLOSED"
    message_key = "booking.cancellation_window_closed"


class BookingNoShowNotAllowedError(AppError):
    """Braider-only. Only CONFIRMED/IN_PROGRESS bookings whose appointment
    time has actually passed can be reported as a no-show - a braider
    can't preempt an appointment that hasn't happened yet, and an already
    COMPLETED/cancelled booking is past the point this applies."""

    status_code = status.HTTP_409_CONFLICT
    code = "BOOKING_NO_SHOW_NOT_ALLOWED"
    message_key = "booking.no_show_not_allowed"


class ReceiptNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "RECEIPT_NOT_FOUND"
    message_key = "receipt.not_found"


class InvalidDac7QuarterError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_DAC7_QUARTER"
    message_key = "dac7.invalid_quarter"


class WebhookEventNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "WEBHOOK_EVENT_NOT_FOUND"
    message_key = "payment.webhook_event_not_found"


class BookingPaymentNotResumableError(AppError):
    """POST /{id}/pay only makes sense when there's an outstanding balance
    that isn't already succeeded or mid-flight - e.g. FULL_UPFRONT bookings
    have nothing to resume, and a balance charge currently IN_PROGRESS must
    not be raced by a second, customer-initiated attempt."""

    status_code = status.HTTP_409_CONFLICT
    code = "BOOKING_PAYMENT_NOT_RESUMABLE"
    message_key = "booking.payment_not_resumable"


class TermsNotAcceptedError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "TERMS_NOT_ACCEPTED"
    message_key = "booking.terms_not_accepted"


class InvalidBookingDateRangeError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_BOOKING_DATE_RANGE"
    message_key = "booking.invalid_date_range"


class StripeWebhookMetadataMissingError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "STRIPE_WEBHOOK_METADATA_MISSING"
    message_key = "payment.webhook_metadata_missing"


class InvalidTryOnImageUploadError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_TRYON_IMAGE_UPLOAD"
    message_key = "tryon.invalid_image_upload"


class TryOnNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "TRYON_NOT_FOUND"
    message_key = "tryon.not_found"


class TryOnStyleOrDescriptionRequiredError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "TRYON_STYLE_OR_DESCRIPTION_REQUIRED"
    message_key = "tryon.style_or_description_required"


class TryOnStyleVariationInvalidError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "TRYON_STYLE_VARIATION_INVALID"
    message_key = "tryon.invalid_style_variation"


class MaxPendingTryOnsReachedError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "MAX_PENDING_TRYONS_REACHED"
    message_key = "tryon.max_pending_reached"


class ReviewNotEligibleError(AppError):
    """Raised when a customer tries to review a braider they've never had a
    successfully-paid booking with."""

    status_code = status.HTTP_403_FORBIDDEN
    code = "REVIEW_NOT_ELIGIBLE"
    message_key = "reviews.not_eligible"


class ReviewNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "REVIEW_NOT_FOUND"
    message_key = "reviews.not_found"


class InvalidChatLocaleError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "INVALID_CHAT_LOCALE"
    message_key = "users.invalid_chat_locale"


class ChatNotAvailableError(AppError):
    """Raised when a booking hasn't reached a successful payment yet - chat
    unlocks the moment `confirmed_at` is first set (deposit or full payment)
    and stays unlocked even if the booking is later cancelled, so a dispute
    can still be discussed."""

    status_code = status.HTTP_403_FORBIDDEN
    code = "CHAT_NOT_AVAILABLE"
    message_key = "chat.not_available"


class ChatThreadNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "CHAT_THREAD_NOT_FOUND"
    message_key = "chat.thread_not_found"


class ChatAccessDeniedError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "CHAT_ACCESS_DENIED"
    message_key = "chat.access_denied"


class ChatMessageNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "CHAT_MESSAGE_NOT_FOUND"
    message_key = "chat.message_not_found"


class ChatReportNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "CHAT_REPORT_NOT_FOUND"
    message_key = "chat.report_not_found"


class NotificationNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "NOTIFICATION_NOT_FOUND"
    message_key = "notifications.not_found"


class InvalidDateRangeError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "INVALID_DATE_RANGE"
    message_key = "errors.invalid_date_range"


def _locale_of(request: Request) -> str:
    return getattr(request.state, "locale", "en")


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    locale = _locale_of(request)
    headers = {}
    if isinstance(exc, RateLimitedError):
        headers["Retry-After"] = str(exc.retry_after_seconds)
    return JSONResponse(
        status_code=exc.status_code,
        headers=headers,
        content={
            "status": "error",
            "status_label": t("status.error", locale),
            "data": None,
            "error": {
                "code": exc.code,
                "message": t(exc.message_key, locale, **exc.format_kwargs),
            },
        },
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    locale = _locale_of(request)
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "status": "error",
            "status_label": t("status.error", locale),
            "data": None,
            "error": {
                "code": "VALIDATION_ERROR",
                "message": t("errors.validation_error", locale),
                "details": jsonable_encoder(exc.errors()),
            },
        },
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    locale = _locale_of(request)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "status": "error",
            "status_label": t("status.error", locale),
            "data": None,
            "error": {
                "code": "INTERNAL_SERVER_ERROR",
                "message": t("errors.internal_server_error", locale),
            },
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
