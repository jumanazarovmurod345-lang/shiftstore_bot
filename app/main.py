"""FastAPI-приложение: webhook, healthcheck, жизненный цикл фоновых компонентов."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.bot.dispatcher import ALLOWED_UPDATES, build_dispatcher
from app.catalog.parser import get_caption_parser
from app.config import Settings, get_settings
from app.context import AppContext
from app.db.session import make_engine, make_sessionmaker
from app.intake import store_update
from app.logging_setup import setup_logging
from app.polling import run_polling
from app.search.embeddings import make_provider
from app.worker import Worker

log = logging.getLogger(__name__)

MAX_UPDATE_BYTES = 1_000_000


async def register_webhook(bot: Bot, settings: Settings, stop: asyncio.Event) -> None:
    """setWebhook с повторами и backoff; не валит приложение при временной ошибке."""
    assert settings.webhook_secret is not None
    delay = 2.0
    while not stop.is_set():
        try:
            await bot.set_webhook(
                url=settings.webhook_url,
                secret_token=settings.webhook_secret.get_secret_value(),
                allowed_updates=ALLOWED_UPDATES,
                drop_pending_updates=False,
                max_connections=40,
            )
            log.info("webhook registered", extra={"url": settings.webhook_url})
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("webhook registration failed", extra={"error": type(exc).__name__, "delay": delay})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay + random.uniform(0, 1))
            delay = min(delay * 2, 300)


def create_app(settings: Settings | None = None, *, start_background: bool = True) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        setup_logging(settings.log_level, settings.secrets())
        engine = make_engine(settings)
        sessionmaker = make_sessionmaker(engine)
        bot = Bot(settings.bot_token.get_secret_value(), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        ctx = AppContext(
            settings=settings,
            bot=bot,
            sessionmaker=sessionmaker,
            provider=make_provider(settings),
            parser=get_caption_parser(settings.caption_parser),
        )
        ctx.dp = build_dispatcher()
        worker = Worker(ctx)
        stop = asyncio.Event()
        bg: list[asyncio.Task[None]] = []
        app.state.ctx, app.state.worker, app.state.engine = ctx, worker, engine
        log.info(
            "starting",
            extra={"mode": settings.bot_mode, "provider": ctx.provider.name, "model": ctx.provider.model},
        )
        if start_background:
            await worker.start()
            if settings.bot_mode == "webhook":
                bg.append(asyncio.create_task(register_webhook(bot, settings, stop)))
            else:
                bg.append(asyncio.create_task(run_polling(ctx, worker, stop)))
        try:
            yield
        finally:
            log.info("shutting down")
            stop.set()
            for t in bg:
                t.cancel()
            await asyncio.gather(*bg, return_exceptions=True)
            await worker.stop(settings.shutdown_grace_seconds)
            await ctx.provider.aclose()
            await bot.session.close()
            await engine.dispose()
            log.info("shutdown complete")

    app = FastAPI(title="shop-photo-bot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health(request: Request) -> JSONResponse:
        try:
            async with request.app.state.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except (SQLAlchemyError, OSError):
            return JSONResponse({"status": "degraded", "db": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok", "db": "ok", "mode": settings.bot_mode})

    @app.post("/telegram/webhook")
    async def telegram_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: str | None = Header(default=None),
    ) -> JSONResponse:
        if settings.bot_mode != "webhook" or settings.webhook_secret is None:
            return JSONResponse({"ok": False}, status_code=404)
        expected = settings.webhook_secret.get_secret_value().encode()
        if not x_telegram_bot_api_secret_token or not hmac.compare_digest(
            x_telegram_bot_api_secret_token.encode(), expected
        ):
            log.warning("webhook: bad secret token")
            return JSONResponse({"ok": False}, status_code=401)
        body = await request.body()
        if len(body) > MAX_UPDATE_BYTES:
            return JSONResponse({"ok": False}, status_code=413)
        try:
            data = await request.json()
            int(data["update_id"])
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"ok": False}, status_code=400)
        ctx: AppContext = request.app.state.ctx
        try:
            async with ctx.sessionmaker() as s:
                stored = await store_update(s, data)
        except (SQLAlchemyError, OSError):
            # Telegram повторит доставку позже — апдейт не потеряется.
            log.error("webhook: database unavailable", exc_info=True)
            return JSONResponse({"ok": False}, status_code=503)
        if stored:
            request.app.state.worker.wake()
        return JSONResponse({"ok": True})

    return app
