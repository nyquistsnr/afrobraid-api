from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = "development"
    debug: bool = False

    database_url: str
    db_echo: bool = False
    redis_url: str
    # Kill switch for the read-through query cache (app.core.query_cache) -
    # flip off to send every cached read straight to Postgres again.
    query_cache_enabled: bool = True

    @field_validator("database_url")
    @classmethod
    def normalize_database_url(cls, v: str) -> str:
        if v.startswith("postgres://"):
            v = "postgresql+asyncpg://" + v[len("postgres://") :]
        elif v.startswith("postgresql://"):
            v = "postgresql+asyncpg://" + v[len("postgresql://") :]
        return v.replace("sslmode=", "ssl=")

    secret_key: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    remember_me_access_token_expire_minutes: int = 43200  # 30 days
    refresh_token_expire_days: int = 30
    remember_me_refresh_token_expire_days: int = 90

    otp_expire_minutes: int = 10
    otp_length: int = 6

    admin_invite_expire_hours: int = 72

    resend_api_key: str
    email_from: str

    supported_locales: str = "en,fr,de"
    default_locale: str = "en"

    google_client_id: str = ""
    facebook_app_id: str = ""
    facebook_app_secret: str = ""
    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""

    cors_origins: str = "http://localhost:3000"
    frontend_url: str = "http://localhost:3000"
    customer_frontend_url: str = ""
    braider_frontend_url: str = ""
    admin_frontend_url: str = ""

    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket_name: str = ""
    r2_endpoint_url: str = ""
    r2_public_base_url: str = ""

    deepl_api_key: str = ""
    deepl_api_url: str = "https://api-free.deepl.com/v2/translate"

    veriff_api_key: str = ""
    veriff_secret_key: str = ""
    veriff_api_url: str = "https://stationapi.veriff.com/v1"
    veriff_callback_url: str = ""

    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_connect_refresh_url: str = ""
    stripe_connect_return_url: str = ""
    stripe_payments_webhook_secret: str = ""
    stripe_api_version: str = "2024-06-20"

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_verify_service_sid: str = ""
    twilio_phone_number: str = ""

    phone_verification_bypass_enabled: bool = False
    phone_verification_bypass_code: str = "000000"

    booking_calculation_ttl_hours: int = 2
    booking_full_payment_threshold_hours: int = 24
    booking_full_payment_margin_hours: int = 2
    client_ip_hash_salt: str = ""

    booking_hold_minutes: int = 30
    booking_cancellation_cutoff_hours: int = 24
    booking_balance_charge_grace_minutes: int = 45
    booking_terms_version: str = "1.0"
    # Retry ladder for a failed off-session balance charge: hours after
    # attempt N before attempt N+1 is due. Exhausted (or a next attempt
    # would land past the hard deadline below) -> CANCELLED_NO_PAYMENT.
    booking_balance_retry_offsets_hours: list[int] = [2, 6, 12]
    booking_balance_hard_deadline_hours_before_start: int = 4
    # A customer-initiated (on-session, POST /bookings/{id}/pay) balance
    # attempt idle this long is treated as abandoned: its PaymentIntent is
    # canceled and the booking goes back to the retry ladder above, so the
    # IN_PROGRESS it set can't stall the automatic charge forever.
    booking_balance_on_session_abandon_after_minutes: int = 60
    # How long after a booking's appointment ends before the braider's
    # payout releases - covers the near-term dispute window (plan design
    # correction #6); full chargeback protection is the transfer-reversal
    # path, not this delay.
    booking_payout_release_delay_hours: int = 48

    # This API serves the receipt HTML directly (GET /api/v1/receipts/{token}),
    # not the frontend - public_base_url is what that link is built against.
    public_base_url: str = "http://localhost:8000"

    # Placeholder legal-entity details for the receipt template's seller
    # block - deliberately dummy values (see plan's flagged item #1: needs
    # a tax advisor and real company details before this goes live).
    company_legal_name: str = "Afrobraid GmbH"
    company_address: str = "Musterstraße 1, 10115 Berlin, Germany"
    company_vat_number: str = "DE000000000"

    # Phase 7 hardening: reconciliation is the safety net for a webhook
    # that never arrived (design correction #7) - a payment PENDING longer
    # than this, with a Stripe intent already created, gets its real
    # status re-fetched directly rather than waiting forever.
    reconcile_pending_payment_after_minutes: int = 60
    # A webhook event stuck at RECEIVED longer than this means processing
    # crashed mid-way; FAILED events are always eligible. Capped by
    # webhook_max_retry_attempts so a permanently broken event doesn't
    # retry forever.
    webhook_retry_after_minutes: int = 15
    webhook_max_retry_attempts: int = 5

    hf_api_key: str = ""
    hf_provider: str = "auto"
    hf_model_id: str = "black-forest-labs/FLUX.1-Kontext-dev"
    hf_request_timeout_seconds: int = 120
    tryon_max_pending_per_user: int = 3

    @property
    def supported_locales_list(self) -> list[str]:
        return [loc.strip() for loc in self.supported_locales.split(",") if loc.strip()]

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
