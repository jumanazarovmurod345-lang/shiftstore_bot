"""Обработчики фоновых задач очереди."""

from __future__ import annotations

import io
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from html import escape

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Update
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.bot import texts
from app.context import AppContext
from app.db.models import Product, ProductImage, SearchLog
from app.imaging import InvalidImageError, load_image, phash, prepare_for_embedding
from app.queue.jobs import ClaimedJob
from app.search.embeddings import EmbeddingError, EmbeddingNotConfigured
from app.search.matcher import MatchDecision, decide_embedding, decide_phash
from app.services.requests import create_request, get_request, render_admin_text, request_keyboard

log = logging.getLogger(__name__)

JobHandler = Callable[[AppContext, ClaimedJob], Awaitable[None]]


class PermanentJobError(Exception):
    """Ошибка, которую бессмысленно повторять."""


async def download_bytes(ctx: AppContext, file_id: str) -> bytes:
    """Скачивает файл в память. Временный URL Telegram нигде не сохраняется."""
    max_bytes = ctx.settings.max_image_bytes
    try:
        tg_file = await ctx.bot.get_file(file_id)
    except TelegramBadRequest as exc:
        if "too big" in str(exc).lower():
            raise InvalidImageError("too_large") from exc
        raise
    if tg_file.file_size and tg_file.file_size > max_bytes:
        raise InvalidImageError("too_large")
    if not tg_file.file_path:
        raise InvalidImageError("corrupted")
    buf = io.BytesIO()
    await ctx.bot.download_file(tg_file.file_path, destination=buf, timeout=60)
    return buf.getvalue()


# ---------------------------------------------------------------- update
async def handle_update(ctx: AppContext, job: ClaimedJob) -> None:
    assert ctx.dp is not None
    upd = Update.model_validate(job.payload["update"], context={"bot": ctx.bot})
    await ctx.dp.feed_update(ctx.bot, upd, ctx=ctx)


# ---------------------------------------------------------------- index_image
async def handle_index_image(ctx: AppContext, job: ClaimedJob) -> None:
    image_id = int(job.payload["image_id"])
    fuid = job.payload["file_unique_id"]
    async with ctx.sessionmaker() as s:
        img = await s.get(ProductImage, image_id)
        if img is None or img.file_unique_id != fuid:
            log.info("index job stale, skip", extra={"image_id": image_id})
            return
        file_id = img.file_id
        img.index_status = "processing"
        await s.commit()

    try:
        data = await download_bytes(ctx, file_id)
        loaded = load_image(data, ctx.settings.max_image_bytes)
    except InvalidImageError as exc:
        raise PermanentJobError(f"invalid image: {exc}") from exc
    values: dict[str, object] = {"phash": phash(loaded.image)}
    if ctx.embeddings_enabled:
        jpeg = prepare_for_embedding(loaded.image, ctx.settings.image_embedding_max_side)
        try:
            (vec,) = await ctx.provider.embed_images([jpeg], "document")
        except EmbeddingNotConfigured as exc:
            values.update(index_error=str(exc)[:500])
        else:
            values.update(
                embedding=vec.tobytes(),
                embedding_dim=int(vec.shape[0]),
                embedding_model=ctx.provider.model,
                index_error=None,
            )
    else:
        values.update(index_error="эмбеддинги отключены: только pHash")
    async with ctx.sessionmaker() as s:
        await s.execute(
            update(ProductImage)
            .where(ProductImage.id == image_id, ProductImage.file_unique_id == fuid)
            .values(index_status="ready", indexed_at=datetime.now(UTC), **values)
        )
        await s.commit()
    log.info("image indexed", extra={"image_id": image_id, "embedding": "embedding" in values})


async def on_index_failed(ctx: AppContext, job: ClaimedJob, error: str) -> None:
    async with ctx.sessionmaker() as s:
        await s.execute(
            update(ProductImage)
            .where(
                ProductImage.id == int(job.payload["image_id"]),
                ProductImage.file_unique_id == job.payload["file_unique_id"],
            )
            .values(index_status="failed", index_error=error[:500])
        )
        await s.commit()


# ---------------------------------------------------------------- search_photo
async def _count_unindexed(ctx: AppContext) -> int:
    async with ctx.sessionmaker() as s:
        return int(
            (
                await s.execute(
                    select(func.count(ProductImage.id))
                    .join(Product, Product.id == ProductImage.product_id)
                    .where(
                        Product.is_active.is_(True),
                        ProductImage.is_active.is_(True),
                        ProductImage.index_status.in_(("pending", "processing")),
                    )
                )
            ).scalar_one()
        )


async def _send_match(ctx: AppContext, chat_id: int, reply_to: int, decision: MatchDecision) -> None:
    assert decision.best is not None
    async with ctx.sessionmaker() as s:
        product = await s.get(Product, decision.best.product_id)
        image = await s.get(ProductImage, decision.best.image_id)
    assert product is not None and image is not None
    caption = texts.product_card(
        product.name,
        product.price,
        product.currency,
        product.sizes,
        product.availability,
        product.sku,
        product.post_url,
    )
    kb = None
    if product.post_url:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=texts.VIEW_IN_CHANNEL, url=product.post_url)]]
        )
    try:
        await ctx.bot.send_photo(
            chat_id, image.file_id, caption=caption, reply_markup=kb, reply_to_message_id=reply_to
        )
    except TelegramBadRequest:
        # file_id недействителен (например, бот сменился) — отправляем карточку без фото.
        await ctx.bot.send_message(chat_id, caption, reply_markup=kb, reply_to_message_id=reply_to)


async def _log_search(ctx: AppContext, p: dict, d: MatchDecision | None, outcome: str, request_id: int | None) -> None:
    try:
        async with ctx.sessionmaker() as s:
            s.add(
                SearchLog(
                    user_id=p["user_db_id"],
                    photo_file_unique_id=p.get("file_unique_id"),
                    outcome=outcome,
                    stage=d.stage if d else None,
                    top1_product_id=d.best.product_id if d and d.best else None,
                    top1_score=d.best.score if d and d.best else None,
                    top2_product_id=d.second.product_id if d and d.second else None,
                    top2_score=d.second.score if d and d.second else None,
                    phash_distance=d.best.phash_distance if d and d.best else None,
                    model=ctx.provider.model,
                    request_id=request_id,
                )
            )
            await s.commit()
    except SQLAlchemyError:
        log.warning("search log write failed", exc_info=True)


async def _escalate(ctx: AppContext, p: dict, reason: str, d: MatchDecision | None) -> int:
    candidates = None
    if d and d.candidates:
        async with ctx.sessionmaker() as s:
            names = dict(
                (
                    await s.execute(
                        select(Product.id, Product.name).where(
                            Product.id.in_([c.product_id for c in d.candidates])
                        )
                    )
                ).all()
            )
        candidates = [
            {"product_id": c.product_id, "score": round(c.score, 4), "name": names.get(c.product_id)}
            for c in d.candidates
        ]
    async with ctx.sessionmaker() as s:
        request_id = await create_request(
            s,
            user_id=p["user_db_id"],
            source_chat_id=p["chat_id"],
            source_message_id=p["message_id"],
            photo_file_id=p["file_id"],
            photo_file_unique_id=p.get("file_unique_id"),
            photo_is_document=bool(p.get("is_document")),
            client_caption=p.get("caption"),
            reason=reason,
            candidates=candidates,
        )
        await s.commit()
    return request_id


async def handle_search(ctx: AppContext, job: ClaimedJob) -> None:
    p = job.payload
    chat_id, message_id = int(p["chat_id"]), int(p["message_id"])
    decision: MatchDecision | None = None
    try:
        data = await download_bytes(ctx, p["file_id"])
        loaded = load_image(data, ctx.settings.max_image_bytes)
        del data
        query_hash = phash(loaded.image)
        async with ctx.sessionmaker() as s:
            index = await ctx.index_cache.get(s, ctx.provider.model)
        pids, iids, dists = index.phash_distances(query_hash)
        decision = decide_phash(pids, iids, dists, ctx.settings.phash_max_distance)
        if decision is None:
            if not ctx.embeddings_enabled:
                raise EmbeddingNotConfigured("визуальный поиск не настроен")
            jpeg = prepare_for_embedding(loaded.image, ctx.settings.image_embedding_max_side)
            (qvec,) = await ctx.provider.embed_images([jpeg], "query")
            decision = decide_embedding(
                index.emb_product_ids, index.emb_image_ids, index.cosine(qvec), ctx.thresholds
            )
    except InvalidImageError as exc:
        mb = int(ctx.settings.max_image_bytes / 1024 / 1024)
        await ctx.bot.send_message(
            chat_id, texts.BAD_IMAGE.get(str(exc), texts.BAD_IMAGE["corrupted"]).format(mb=mb),
            reply_to_message_id=message_id,
        )
        return
    except EmbeddingNotConfigured:
        log.error("search unavailable: embeddings not configured")
        await _technical_problem(ctx, p)
        return
    except (EmbeddingError, SQLAlchemyError, OSError, ValueError) as exc:
        if job.attempts < job.max_attempts:
            raise  # повтор с backoff
        log.error("search failed after retries", extra={"error": type(exc).__name__})
        await _technical_problem(ctx, p)
        return

    if decision.outcome == "match":
        await _send_match(ctx, chat_id, message_id, decision)
        await _log_search(ctx, p, decision, "match", None)
        log.info(
            "search match",
            extra={"stage": decision.stage, "product_id": decision.best.product_id if decision.best else None},
        )
        return

    unindexed = await _count_unindexed(ctx)
    if unindexed:
        reason = "index_incomplete"
        request_id = await _escalate(ctx, p, reason, decision)
        await ctx.bot.send_message(chat_id, texts.TEMP_PROBLEM, reply_to_message_id=message_id)
    else:
        reason = decision.outcome  # not_found | uncertain
        request_id = await _escalate(ctx, p, reason, decision)
        await ctx.bot.send_message(chat_id, texts.NOT_FOUND, reply_to_message_id=message_id)
    await _log_search(ctx, p, decision, reason, request_id)
    log.info("search escalated", extra={"reason": reason, "request_id": request_id})


async def _technical_problem(ctx: AppContext, p: dict) -> None:
    try:
        request_id = await _escalate(ctx, p, "search_unavailable", None)
    except SQLAlchemyError:
        log.error("cannot store request: database unavailable", exc_info=True)
        await ctx.bot.send_message(p["chat_id"], texts.TEMP_PROBLEM_NO_ADMIN)
        return
    await ctx.bot.send_message(p["chat_id"], texts.TEMP_PROBLEM, reply_to_message_id=p["message_id"])
    await _log_search(ctx, p, None, "search_unavailable", request_id)


# ---------------------------------------------------------------- notify_admin
async def handle_notify_admin(ctx: AppContext, job: ClaimedJob) -> None:
    try:
        await _notify_admin(ctx, job)
    except TelegramForbiddenError as exc:
        # Бот удалён из админ-чата: не считаем это окончательной ошибкой — повторяем,
        # пока администратор не вернёт бота. Заявка в БД сохраняется в любом случае.
        raise RuntimeError(f"admin chat forbidden: {exc}") from exc


async def _notify_admin(ctx: AppContext, job: ClaimedJob) -> None:
    request_id = int(job.payload["request_id"])
    admin_chat = ctx.settings.admin_chat_id
    async with ctx.sessionmaker() as s:
        req = await get_request(s, request_id)
        if req is None or req.notified_at is not None:
            return
        photo_msg_id = req.admin_photo_message_id
        text = render_admin_text(req, ctx.tz)
        kb = request_keyboard(req)
        send = ctx.bot.send_document if req.photo_is_document else ctx.bot.send_photo

        if photo_msg_id is None:
            # Если текст укладывается в лимит подписи фото Telegram (1024 симв.),
            # отправляем одним сообщением: фото + все данные заявки + кнопки
            if len(text) <= 1024:
                try:
                    msg = await send(admin_chat, req.photo_file_id, caption=text, reply_markup=kb)
                    photo_msg_id = msg.message_id
                    req.admin_photo_message_id = photo_msg_id
                    req.admin_message_id = msg.message_id
                    req.notified_at = datetime.now(UTC)
                    await s.commit()
                    log.info("admin notified with unified photo card", extra={"request_id": request_id})
                    return
                except TelegramBadRequest as exc:
                    if "chat not found" in str(exc).lower():
                        raise
                    log.warning("single message photo send failed, fallback to separate messages", extra={"request_id": request_id})

            # Если текст длинный или отправка единым сообщением не прошла — отправляем фото, затем текст
            try:
                msg = await send(admin_chat, req.photo_file_id, caption="🔎 Новая заявка на поиск товара")
                photo_msg_id = msg.message_id
            except TelegramBadRequest as exc:
                if "chat not found" in str(exc).lower():
                    raise
                log.warning("admin photo send failed", extra={"request_id": request_id})
                photo_msg_id = 0
            req.admin_photo_message_id = photo_msg_id
            await s.commit()

        if photo_msg_id == 0:
            text += "\n\n⚠️ Фото клиента не удалось переслать."

        msg = await ctx.bot.send_message(
            admin_chat,
            text,
            reply_markup=kb,
            reply_to_message_id=photo_msg_id or None,
            disable_web_page_preview=True,
        )
        req.admin_message_id = msg.message_id
        req.notified_at = datetime.now(UTC)
        await s.commit()
    log.info("admin notified", extra={"request_id": request_id})


# ---------------------------------------------------------------- product_ready
async def handle_product_ready(ctx: AppContext, job: ClaimedJob) -> None:
    """Сводка администратору после (до)сборки товара — помогает заметить ошибки разбора."""
    chat_id = job.payload.get("notify_chat_id")
    if not chat_id:
        return
    async with ctx.sessionmaker() as s:
        product = await s.get(Product, int(job.payload["product_id"]))
        if product is None:
            return
        n_images = sum(1 for i in product.images if i.is_active)
    missing = [
        label
        for label, value in (("название", product.name), ("цена", product.price), ("размеры", product.sizes))
        if not value
    ]
    lines = [
        f"{'🆕 Добавлен' if job.payload.get('created') else '✏️ Обновлён'} товар #{product.id}",
        f"Название: {escape(product.name) if product.name else '—'}",
        f"Цена: {texts.format_price(product.price, product.currency)}",
        f"Размеры: {texts.format_sizes(product.sizes)}",
        f"Фото: {n_images}",
    ]
    if product.post_url:
        lines.append(f"Пост: {product.post_url}")
    if missing:
        lines.append(f"⚠️ Не распознано: {', '.join(missing)} — клиенту будет предложено уточнить у администратора.")
    try:
        await ctx.bot.send_message(chat_id, "\n".join(lines), disable_web_page_preview=True)
    except TelegramForbiddenError:
        log.warning("product summary: chat forbidden", extra={"chat_id": chat_id})


HANDLERS: dict[str, JobHandler] = {
    "update": handle_update,
    "index_image": handle_index_image,
    "search_photo": handle_search,
    "notify_admin": handle_notify_admin,
    "product_ready": handle_product_ready,
}

FINAL_FAILURE_HOOKS: dict[str, Callable[[AppContext, ClaimedJob, str], Awaitable[None]]] = {
    "index_image": on_index_failed,
}

__all__ = ["HANDLERS", "FINAL_FAILURE_HOOKS", "PermanentJobError"]
