"""Посты канала магазина (channel_post / edited_channel_post) и служебные события."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import ChatMemberUpdated, Message

from app.catalog.service import PhotoRef, PostInput, ingest_post
from app.context import AppContext
from app.queue.jobs import enqueue

log = logging.getLogger(__name__)

router = Router(name="channel")


def photo_of(message: Message) -> PhotoRef | None:
    if message.photo:
        p = message.photo[-1]
        return PhotoRef(p.file_id, p.file_unique_id, p.width, p.height)
    doc = message.document
    if doc and (doc.mime_type or "").lower() in {"image/jpeg", "image/png", "image/webp"}:
        return PhotoRef(doc.file_id, doc.file_unique_id)
    return None


async def apply_post(ctx: AppContext, inp: PostInput, notify_chat_id: int | None) -> int | None:
    """Применяет пост к каталогу и ставит задачи индексации/сводки. Возвращает product_id."""
    async with ctx.sessionmaker() as s:
        result = await ingest_post(s, inp, ctx.parser)
        if result is None:
            await s.rollback()
            return None
        if result.needs_index and result.image_id is not None:
            await enqueue(
                s,
                "index_image",
                {"image_id": result.image_id, "file_unique_id": result.file_unique_id},
                dedup_key=f"index:{result.image_id}:{result.file_unique_id}",
                max_attempts=8,
            )
        if notify_chat_id:
            # debounce: сводка уходит один раз после того, как альбом «затих» на 6 секунд
            await enqueue(
                s,
                "product_ready",
                {"product_id": result.product_id, "notify_chat_id": notify_chat_id, "created": result.created},
                dedup_key=f"product_ready:{result.product_id}",
                delay_seconds=6,
                on_conflict="upsert",
                max_attempts=3,
            )
        await s.commit()
    log.info(
        "catalog post applied",
        extra={"product_id": result.product_id, "message_id": inp.source_message_id,
               "created": result.created, "reindex": result.needs_index},
    )
    return result.product_id


@router.channel_post()
@router.edited_channel_post()
async def on_channel_post(message: Message, ctx: AppContext) -> None:
    if message.chat.id != ctx.settings.channel_id:
        log.info("post from foreign channel ignored", extra={"chat_id": message.chat.id})
        return
    inp = PostInput(
        source_chat_id=message.chat.id,
        source_message_id=message.message_id,
        caption=message.caption if message.caption is not None else message.text,
        photo=photo_of(message),
        media_group_id=message.media_group_id,
        channel_username=message.chat.username,
        source="channel",
    )
    notify = ctx.settings.admin_chat_id if ctx.settings.notify_admin_on_product else None
    await apply_post(ctx, inp, notify)


@router.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated, ctx: AppContext) -> None:
    """Подсказка при добавлении бота в канал: показываем ID канала администраторам."""
    if event.chat.type != "channel":
        return
    status = event.new_chat_member.status
    log.info("bot membership changed", extra={"chat_id": event.chat.id, "status": status})
    is_shop = event.chat.id == ctx.settings.channel_id
    title = event.chat.title or ""
    if status == "administrator":
        text = (
            f"✅ Бот назначен администратором канала «{title}» (ID <code>{event.chat.id}</code>)."
            + ("" if is_shop else f"\n⚠️ Это не канал из CHANNEL_ID. Если это канал магазина — "
               f"укажите CHANNEL_ID={event.chat.id} и перезапустите сервис.")
        )
    elif status in ("left", "kicked", "member"):
        text = f"⚠️ Бот больше не администратор канала «{title}» (ID <code>{event.chat.id}</code>). " \
               "Новые посты не будут попадать в каталог."
    else:
        return
    await ctx.bot.send_message(ctx.settings.admin_chat_id, text)
