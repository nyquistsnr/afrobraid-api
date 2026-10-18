import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

from app.core import ws
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.i18n import LocaleMiddleware
from app.core.logging import configure_logging
from app.core.queue import create_arq_pool
from app.core.redis import get_redis_client
from app.modules.auth.admin_router import router as auth_admin_router
from app.modules.auth.router import router as auth_router
from app.modules.bookings.admin_router import router as admin_bookings_router
from app.modules.bookings.braider_router import router as braider_bookings_router
from app.modules.bookings.calculations.router import router as booking_calculations_router
from app.modules.bookings.dac7.router import router as dac7_router
from app.modules.bookings.dashboard.admin_router import router as admin_dashboard_router
from app.modules.bookings.dashboard.router import router as braider_dashboard_router
from app.modules.bookings.payments.admin_router import router as admin_payments_router
from app.modules.bookings.payments.braider_router import router as braider_payments_router
from app.modules.bookings.payments.webhook import router as stripe_payments_webhook_router
from app.modules.bookings.receipts.router import router as receipts_router
from app.modules.bookings.router import router as bookings_router
from app.modules.bookings.stats.router import router as braider_booking_stats_router
from app.modules.braiders.admin_router import router as braiders_admin_router
from app.modules.braiders.availability.router import (
    public_router as braider_availability_public_router,
)
from app.modules.braiders.availability.router import router as braider_availability_router
from app.modules.braiders.discovery.router import router as braider_discovery_router
from app.modules.braiders.offerings.router import router as braider_offerings_router
from app.modules.braiders.payment_setup.router import router as braider_payment_setup_router
from app.modules.braiders.payment_setup.webhook import router as stripe_webhook_router
from app.modules.braiders.phone_verification.router import router as phone_verification_router
from app.modules.braiders.portfolio.router import router as braider_portfolio_router
from app.modules.braiders.router import router as braiders_router
from app.modules.braiders.service_location.router import router as braider_service_location_router
from app.modules.braiders.veriff.router import router as veriff_router
from app.modules.braiders.veriff.webhook import router as veriff_webhook_router
from app.modules.chat.admin_router import router as chat_admin_router
from app.modules.chat.router import router as chat_router
from app.modules.contact.admin_router import router as contact_admin_router
from app.modules.contact.router import router as contact_router
from app.modules.media.router import router as media_router
from app.modules.notifications.router import router as notifications_router
from app.modules.platform_settings.router import router as platform_settings_router
from app.modules.realtime.router import router as realtime_router
from app.modules.reviews.admin_router import router as reviews_admin_router
from app.modules.reviews.router import router as reviews_router
from app.modules.styles.admin_router import router as styles_admin_router
from app.modules.styles.router import router as styles_router
from app.modules.tryon.router import router as tryon_router
from app.modules.users.admin_router import router as users_admin_router
from app.modules.users.router import router as users_router

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    app.state.arq_pool = await create_arq_pool()

    ws_redis_client = get_redis_client()
    listener_task = asyncio.create_task(ws.redis_listener(ws_redis_client))

    yield

    listener_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await listener_task
    await ws_redis_client.aclose()
    await app.state.arq_pool.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Afrobraid API",
        version="0.1.0",
        lifespan=lifespan,
        swagger_ui_parameters={
            "docExpansion": "none",  # all tags collapsed by default
            "defaultModelsExpandDepth": -1,  # hides the "Schemas" section entirely
        },
    )

    # Middlewares are applied outside-in in reverse of registration order,
    # so CORS is added last to stay the outermost layer.
    app.add_middleware(LocaleMiddleware)
    # List and dashboard payloads are JSON-heavy; compressing them cuts transfer time.
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)

    app.include_router(auth_router)
    app.include_router(auth_admin_router)
    app.include_router(users_router)
    app.include_router(users_admin_router)
    app.include_router(braiders_router)
    app.include_router(braiders_admin_router)
    app.include_router(phone_verification_router)
    app.include_router(veriff_router)
    app.include_router(veriff_webhook_router)
    app.include_router(media_router)
    app.include_router(platform_settings_router)
    app.include_router(styles_router)
    app.include_router(styles_admin_router)
    app.include_router(braider_offerings_router)
    app.include_router(braider_portfolio_router)
    app.include_router(braider_service_location_router)
    app.include_router(braider_availability_router)
    app.include_router(braider_availability_public_router)
    app.include_router(braider_discovery_router)
    app.include_router(braider_payment_setup_router)
    app.include_router(stripe_webhook_router)
    app.include_router(booking_calculations_router)
    app.include_router(bookings_router)
    app.include_router(receipts_router)
    app.include_router(dac7_router)
    app.include_router(admin_bookings_router)
    app.include_router(admin_dashboard_router)
    # Must precede braider_bookings_router: both mount at
    # /api/v1/braiders/me/bookings, and that router's GET "/{booking_id}"
    # would otherwise swallow this router's literal "/stats"/"/timeseries"
    # paths (Starlette matches route-by-route in registration order; a
    # path-shape match wins even if UUID conversion later fails as 422).
    app.include_router(braider_booking_stats_router)
    app.include_router(braider_bookings_router)
    app.include_router(admin_payments_router)
    app.include_router(braider_payments_router)
    app.include_router(braider_dashboard_router)
    app.include_router(stripe_payments_webhook_router)
    app.include_router(reviews_router)
    app.include_router(reviews_admin_router)
    app.include_router(tryon_router)
    app.include_router(chat_router)
    app.include_router(chat_admin_router)
    app.include_router(contact_admin_router)
    app.include_router(contact_router)
    app.include_router(notifications_router)
    app.include_router(realtime_router)

    @app.get("/health", tags=["Health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
