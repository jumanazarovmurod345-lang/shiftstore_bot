"""Режим long polling для локальной разработки.

Апдейты проходят тот же путь, что и в webhook-режиме: сохраняются в PostgreSQL и
обрабатываются очередью. Polling не запустится, пока у бота установлен webhook
(чтобы случайно не «украсть» апдейты у продакшена), если не задано
POLLING_DELETE_WEBHOOK=true. Используйте для разработки отдельного тестового бота.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING


from app.bot.dispatcher import ALLOWED_UPDATES
from app.context import AppContext
from app.intake import store_update

if TYPE_CHECKING:
    from app.worker import Worker

log = logging.getLogger(__name__)


async def run_polling(ctx: AppContext, worker: Worker, stop: asyncio.Event) -> None:
    bot = ctx.bot
    info = await bot.get_webhook_info()
    if info.url:
        if not ctx.settings.polling_delete_webhook:
            log.error(
                "webhook is set for this bot; polling refused. Use a separate dev bot "
                "or set POLLING_DELETE_WEBHOOK=true",
            )
            return
        await bot.delete_webhook(drop_pending_updates=False)
        log.warning("webhook deleted for polling mode")
    offset: int | None = None
    delay = 1.0
    log.info("polling started")
    while not stop.is_set():
        try:
            updates = await bot.get_updates(offset=offset, timeout=25, allowed_updates=ALLOWED_UPDATES)
            for upd in updates:
                raw = upd.model_dump(mode="json", by_alias=True, exclude_none=True)
                async with ctx.sessionmaker() as s:
                    await store_update(s, raw)
                offset = upd.update_id + 1
            if updates:
                worker.wake()
            delay = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - сеть, 409 Conflict, 5xx, БД
            log.warning("polling error", extra={"error": type(exc).__name__, "delay": delay})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
            delay = min(delay * 2, 60)
