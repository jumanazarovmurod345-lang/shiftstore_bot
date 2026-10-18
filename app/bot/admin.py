"""Администрирование через Telegram: заявки, ответы клиентам, импорт и управление каталогом.

Права проверяются по ADMIN_IDS в КАЖДОМ обработчике (и для кнопок, и для команд).
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, ForceReply, Message, MessageOriginChannel
from sqlalchemy import func, or_, select

from app.bot import texts
from app.bot.channel import apply_post, photo_of
from app.catalog.service import PostInput, find_product, set_active
from app.context import AppContext
from app.db.models import AdminReply, Product, ProductImage, Request
from app.queue.jobs import enqueue, stats as job_stats
from app.services.requests import apply_status, get_request, render_admin_text, request_keyboard

log = logging.getLogger(__name__)

router = Router(name="admin")

ADMIN_HELP = (
    "<b>Команды администратора</b>\n"
    "/requests — открытые заявки\n"
    "/product &lt;номер|ссылка&gt; — карточка товара\n"
    "/deactivate &lt;номер|ссылка&gt; — скрыть товар из поиска (например, пост удалён)\n"
    "/activate &lt;номер|ссылка&gt; — вернуть товар в поиск\n"
    "/reindex [номер] — доиндексировать фото (без номера — все неиндексированные)\n"
    "/stats — состояние каталога и очереди\n"
    "/import — как импортировать старые посты\n"
    "/chatid — ID текущего чата\n"
    "/cancel — отменить ввод ответа клиенту"
)

IMPORT_HELP = (
    "<b>Импорт существующих товаров</b>\n"
    "Bot API не даёт прочитать историю канала, поэтому старые посты нужно переслать боту.\n\n"
    "1. Откройте канал магазина, выделите посты (альбомы пересылайте целиком).\n"
    "2. «Переслать» → этот личный чат с ботом. Не включайте «скрыть отправителя».\n"
    "3. Бот проверит, что пост из канала магазина, сохранит ссылку на оригинал и проиндексирует фото.\n"
    "4. Через несколько секунд придёт сводка по каждому товару.\n\n"
    "Повторная пересылка того же поста дубликат не создаст — товар обновится.\n"
    "Если в канале включён запрет копирования/пересылки, пересылка недоступна: временно "
    "отключите ограничение или используйте импорт через MTProto (см. README)."
)


def _is_admin(ctx: AppContext, user_id: int | None) -> bool:
    return ctx.is_admin(user_id)


async def _deny(message: Message) -> None:
    await message.answer("⛔ Команда доступна только администраторам.")


# ------------------------------------------------------------------ ответы через ForceReply
def _is_reply_to_bot_prompt(message: Message) -> bool:
    r = message.reply_to_message
    return bool(r and r.from_user and r.from_user.is_bot and r.text and "по заявке #" in r.text)


@router.message(F.reply_to_message, _is_reply_to_bot_prompt)
async def on_admin_reply_text(message: Message, ctx: AppContext) -> None:
    assert message.from_user is not None and message.reply_to_message is not None
    if not _is_admin(ctx, message.from_user.id):
        return
    async with ctx.sessionmaker() as s:
        reply = (
            await s.execute(
                select(AdminReply)
                .where(
                    AdminReply.prompt_chat_id == message.chat.id,
                    AdminReply.prompt_message_id == message.reply_to_message.message_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if reply is None:
            return
        if reply.status != "awaiting":
            await message.reply("Этот запрос на ответ уже обработан. Нажмите «Ответить через бота» ещё раз.")
            return
        req = await get_request(s, reply.request_id)
        assert req is not None
        try:
            await ctx.bot.send_message(
                req.user.chat_id, f"💬 Сообщение от администратора по вашей заявке #{req.id}:"
            )
            await ctx.bot.copy_message(req.user.chat_id, message.chat.id, message.message_id)
        except TelegramForbiddenError:
            reply.status = "failed"
            await s.commit()
            await message.reply(
                "❌ Клиент заблокировал бота. Свяжитесь с ним по телефону: "
                f"{escape(req.user.phone or 'не указан')}"
            )
            return
        except TelegramBadRequest as exc:
            reply.status = "failed"
            await s.commit()
            await message.reply(f"❌ Не удалось отправить: {escape(str(exc))[:200]}")
            return
        reply.status = "sent"
        reply.text = message.text or message.caption
        reply.admin_id = message.from_user.id
        reply.sent_at = datetime.now(UTC)
        await s.commit()
    log.info("admin reply sent", extra={"request_id": reply.request_id, "admin_id": message.from_user.id})
    await message.reply("✅ Сообщение отправлено клиенту.")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, ctx: AppContext) -> None:
    if not _is_admin(ctx, message.from_user and message.from_user.id):
        return
    async with ctx.sessionmaker() as s:
        rows = (
            await s.execute(
                select(AdminReply).where(
                    AdminReply.admin_id == message.from_user.id,  # type: ignore[union-attr]
                    AdminReply.status == "awaiting",
                )
            )
        ).scalars().all()
        for r in rows:
            r.status = "cancelled"
        await s.commit()
    await message.answer("Ввод ответа отменён." if rows else "Нечего отменять.")


# ------------------------------------------------------------------ кнопки заявки
@router.callback_query(F.data.startswith("rq:"))
async def on_request_button(cb: CallbackQuery, ctx: AppContext) -> None:
    if not _is_admin(ctx, cb.from_user.id):
        await cb.answer("⛔ Недостаточно прав", show_alert=True)
        return
    try:
        _, action, raw_id = (cb.data or "").split(":")
        request_id = int(raw_id)
    except ValueError:
        await cb.answer("Некорректная кнопка", show_alert=True)
        return
    if action not in {"reply", "take", "close", "reopen"}:
        await cb.answer()
        return
    async with ctx.sessionmaker() as s:
        req = await get_request(s, request_id, lock=True)
        if req is None:
            await cb.answer("Заявка не найдена", show_alert=True)
            return
        changed = apply_status(req, action, cb.from_user.id)
        if action == "reply":
            chat_id = cb.message.chat.id if cb.message else cb.from_user.id
            prompt = await ctx.bot.send_message(
                chat_id,
                f"✍️ Ответ клиенту по заявке #{req.id}. Напишите сообщение <b>ответом на это сообщение</b> "
                "(можно текст или фото). /cancel — отмена.",
                reply_markup=ForceReply(selective=True, input_field_placeholder="Сообщение клиенту"),
            )
            s.add(
                AdminReply(
                    request_id=req.id,
                    admin_id=cb.from_user.id,
                    prompt_chat_id=chat_id,
                    prompt_message_id=prompt.message_id,
                )
            )
        await s.commit()
        if changed and req.admin_message_id and cb.message:
            try:
                await ctx.bot.edit_message_text(
                    render_admin_text(req, ctx.tz),
                    chat_id=cb.message.chat.id,
                    message_id=req.admin_message_id,
                    reply_markup=request_keyboard(req),
                    disable_web_page_preview=True,
                )
            except TelegramBadRequest:
                log.info("admin message not modified", extra={"request_id": req.id})
    log.info("request action", extra={"request_id": request_id, "action": action, "admin_id": cb.from_user.id})
    await cb.answer({"take": "Взято в работу", "close": "Заявка закрыта", "reopen": "Заявка открыта",
                     "reply": "Введите ответ"}[action])


# ------------------------------------------------------------------ команды
@router.message(Command("admin"))
async def cmd_admin(message: Message, ctx: AppContext) -> None:
    if not _is_admin(ctx, message.from_user and message.from_user.id):
        return await _deny(message)
    await message.answer(ADMIN_HELP)


@router.message(Command("chatid"))
async def cmd_chatid(message: Message, ctx: AppContext) -> None:
    await message.answer(f"ID этого чата: <code>{message.chat.id}</code>")


@router.message(Command("import"))
async def cmd_import(message: Message, ctx: AppContext) -> None:
    if not _is_admin(ctx, message.from_user and message.from_user.id):
        return await _deny(message)
    await message.answer(IMPORT_HELP)


@router.message(Command("requests"))
async def cmd_requests(message: Message, ctx: AppContext) -> None:
    if not _is_admin(ctx, message.from_user and message.from_user.id):
        return await _deny(message)
    async with ctx.sessionmaker() as s:
        rows = (
            await s.execute(
                select(Request).where(Request.status != "closed").order_by(Request.id.desc()).limit(20)
            )
        ).scalars().all()
    if not rows:
        await message.answer("Открытых заявок нет.")
        return
    lines = ["<b>Открытые заявки</b> (последние 20):"]
    for r in rows:
        when = r.created_at.astimezone(ctx.tz).strftime("%d.%m %H:%M")
        delivered = "" if r.notified_at else " ⚠️ не доставлена в админ-чат"
        lines.append(f"#{r.id} · {texts.STATUSES.get(r.status)} · {texts.REASONS.get(r.reason)} · {when}{delivered}")
    await message.answer("\n".join(lines))


@router.message(Command("stats"))
async def cmd_stats(message: Message, ctx: AppContext) -> None:
    if not _is_admin(ctx, message.from_user and message.from_user.id):
        return await _deny(message)
    async with ctx.sessionmaker() as s:
        active = (await s.execute(select(func.count()).where(Product.is_active.is_(True)))).scalar_one()
        inactive = (await s.execute(select(func.count()).where(Product.is_active.is_(False)))).scalar_one()
        img_rows = (
            await s.execute(select(ProductImage.index_status, func.count()).group_by(ProductImage.index_status))
        ).all()
        no_emb = (
            await s.execute(
                select(func.count()).where(
                    ProductImage.is_active.is_(True),
                    or_(ProductImage.embedding.is_(None), ProductImage.embedding_model != ctx.provider.model),
                )
            )
        ).scalar_one()
        q = await job_stats(s)
    imgs = ", ".join(f"{k}: {v}" for k, v in img_rows) or "нет"
    provider = (
        f"{ctx.provider.name} / {escape(ctx.provider.model)}" if ctx.embeddings_enabled
        else "⚠️ отключён (нет ключа) — работает только поиск копий по pHash"
    )
    await message.answer(
        f"<b>Каталог</b>: активных товаров {active}, скрытых {inactive}\n"
        f"<b>Фото</b>: {imgs}\nБез эмбеддинга текущей модели: {no_emb}\n"
        f"<b>Провайдер</b>: {provider}\n"
        f"<b>Пороги</b>: threshold={ctx.settings.match_threshold}, margin={ctx.settings.match_margin}, "
        f"min={ctx.settings.match_min_score}, phash≤{ctx.settings.phash_max_distance}\n"
        f"<b>Очередь</b>: {q}"
    )


@router.message(Command("product"))
async def cmd_product(message: Message, command: CommandObject, ctx: AppContext) -> None:
    if not _is_admin(ctx, message.from_user and message.from_user.id):
        return await _deny(message)
    if not command.args:
        await message.answer("Использование: /product 12 или /product https://t.me/канал/123")
        return
    async with ctx.sessionmaker() as s:
        p = await find_product(s, command.args, ctx.settings.channel_id)
    if p is None:
        await message.answer("Товар не найден.")
        return
    statuses = ", ".join(f"{i.source_message_id}:{i.index_status}" for i in p.images)
    await message.answer(
        texts.product_card(p.name, p.price, p.currency, p.sizes, p.availability, p.sku).replace(
            "✅ <b>Товар найден!</b>", f"<b>Товар #{p.id}</b> {'🟢 активен' if p.is_active else '⚫️ скрыт'}"
        )
        + f"\n\nПост: {p.post_url or '—'}\nФото (message_id:статус): {statuses}",
        disable_web_page_preview=True,
    )


async def _toggle(message: Message, command: CommandObject, ctx: AppContext, active: bool) -> None:
    if not _is_admin(ctx, message.from_user and message.from_user.id):
        return await _deny(message)
    if not command.args:
        await message.answer("Укажите номер товара или ссылку на пост.")
        return
    async with ctx.sessionmaker() as s:
        p = await find_product(s, command.args, ctx.settings.channel_id)
        if p is None:
            await message.answer("Товар не найден.")
            return
        await set_active(s, p.id, active, message.from_user.id)  # type: ignore[union-attr]
        await s.commit()
    ctx.index_cache.invalidate()
    log.info("product toggled", extra={"product_id": p.id, "active": active})
    await message.answer(f"Товар #{p.id} {'возвращён в поиск' if active else 'скрыт из поиска'}.")


@router.message(Command("deactivate"))
async def cmd_deactivate(message: Message, command: CommandObject, ctx: AppContext) -> None:
    await _toggle(message, command, ctx, False)


@router.message(Command("activate"))
async def cmd_activate(message: Message, command: CommandObject, ctx: AppContext) -> None:
    await _toggle(message, command, ctx, True)


@router.message(Command("reindex"))
async def cmd_reindex(message: Message, command: CommandObject, ctx: AppContext) -> None:
    if not _is_admin(ctx, message.from_user and message.from_user.id):
        return await _deny(message)
    stamp = int(time.time())
    async with ctx.sessionmaker() as s:
        stmt = select(ProductImage).where(ProductImage.is_active.is_(True))
        if command.args:
            p = await find_product(s, command.args, ctx.settings.channel_id)
            if p is None:
                await message.answer("Товар не найден.")
                return
            stmt = stmt.where(ProductImage.product_id == p.id)
        else:
            conds = [ProductImage.index_status == "failed", ProductImage.phash.is_(None)]
            if ctx.embeddings_enabled:
                conds += [
                    ProductImage.embedding.is_(None),
                    ProductImage.embedding_model.is_distinct_from(ctx.provider.model),
                ]
            stmt = stmt.where(or_(*conds))
        images = (await s.execute(stmt)).scalars().all()
        for img in images:
            img.index_status = "pending"
            await enqueue(
                s,
                "index_image",
                {"image_id": img.id, "file_unique_id": img.file_unique_id},
                dedup_key=f"index:{img.id}:{img.file_unique_id}:{stamp}",
            )
        await s.commit()
    await message.answer(f"Поставлено на индексацию фото: {len(images)}.")


# ------------------------------------------------------------------ импорт пересылкой
@router.message(F.chat.type == "private", F.forward_origin)
async def on_forward(message: Message, ctx: AppContext) -> None:
    assert message.from_user is not None
    if not _is_admin(ctx, message.from_user.id):
        # Обычный клиент переслал пост — обрабатываем как поиск по фото.
        from app.bot.user import on_photo, on_other

        if message.photo or message.document:
            await on_photo(message, ctx)
        else:
            await on_other(message, ctx)
        return
    origin = message.forward_origin
    if not isinstance(origin, MessageOriginChannel) or origin.chat.id != ctx.settings.channel_id:
        await message.reply(
            "Этот пост переслан не из канала магазина (CHANNEL_ID) — импорт пропущен."
        )
        return
    photo = photo_of(message)
    if photo is None:
        if not message.media_group_id:
            await message.reply("В посте нет фото — импорт пропущен.")
        return
    inp = PostInput(
        source_chat_id=origin.chat.id,
        source_message_id=origin.message_id,
        caption=message.caption,
        photo=photo,
        media_group_id=None,
        forward_group_key=(
            f"fwd:{message.chat.id}:{message.media_group_id}" if message.media_group_id else None
        ),
        channel_username=origin.chat.username,
        source="import",
    )
    await apply_post(ctx, inp, notify_chat_id=message.chat.id)
