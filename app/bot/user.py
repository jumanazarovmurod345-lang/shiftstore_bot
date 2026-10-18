"""Клиентские сценарии: регистрация по контакту/username и приём фото для поиска."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove

from app.bot import texts
from app.context import AppContext
from app.imaging import ALLOWED_MIME
from app.queue.jobs import enqueue
from app.services.users import (
    ForeignContactError,
    get_user_by_tg,
    refuse_phone,
    save_own_contact,
    upsert_user,
)

log = logging.getLogger(__name__)

router = Router(name="user")
router.message.filter(F.chat.type == "private")


def phone_choice_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=texts.SHARE_CONTACT_BUTTON, request_contact=True)],
            [KeyboardButton(text=texts.REFUSE_PHONE_BUTTON)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


@router.message(CommandStart())
async def cmd_start(message: Message, ctx: AppContext) -> None:
    assert message.from_user is not None
    async with ctx.sessionmaker() as s:
        user = await upsert_user(s, message.from_user, message.chat.id)
        if user.username:
            if user.registered_at is None:
                user.registered_at = datetime.now(UTC)
            await s.commit()
            await message.answer(texts.WELCOME_WITH_USERNAME, reply_markup=ReplyKeyboardRemove())
            return

        # Если username отсутствует, но данные уже сохранены (поделился контактом или отказался)
        if user.phone_shared or user.phone_refused or user.phone:
            if user.registered_at is None:
                user.registered_at = datetime.now(UTC)
            await s.commit()
            await message.answer(texts.WELCOME_BACK, reply_markup=ReplyKeyboardRemove())
            return

        # Username отсутствует и данные ещё не сохранены — показываем предложение поделиться номером
        await s.commit()
        await message.answer(texts.WELCOME_ASK_PHONE_OPTIONAL, reply_markup=phone_choice_keyboard())


@router.message(F.text == texts.REFUSE_PHONE_BUTTON)
async def on_refuse_phone(message: Message, ctx: AppContext) -> None:
    assert message.from_user is not None
    async with ctx.sessionmaker() as s:
        await refuse_phone(s, message.from_user, message.chat.id)
        await s.commit()
    log.info("phone refused by user", extra={"tg_id": message.from_user.id})
    await message.answer(texts.PHONE_REFUSED_EXPLANATION, reply_markup=ReplyKeyboardRemove())


@router.message(Command("phone"))
async def cmd_phone(message: Message, ctx: AppContext) -> None:
    await message.answer(texts.PHONE_UPDATE_ASK, reply_markup=phone_choice_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message, ctx: AppContext) -> None:
    await message.answer(texts.HELP)


@router.message(Command("myid"))
async def cmd_myid(message: Message, ctx: AppContext) -> None:
    assert message.from_user is not None
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


@router.message(F.contact)
async def on_contact(message: Message, ctx: AppContext) -> None:
    assert message.from_user is not None and message.contact is not None
    try:
        async with ctx.sessionmaker() as s:
            _, first = await save_own_contact(
                s, message.from_user, message.chat.id, message.contact.user_id, message.contact.phone_number
            )
            await s.commit()
    except ForeignContactError:
        log.info("foreign contact rejected", extra={"tg_id": message.from_user.id})
        await message.answer(texts.FOREIGN_CONTACT, reply_markup=phone_choice_keyboard())
        return
    log.info("phone saved voluntarily", extra={"tg_id": message.from_user.id, "first": first})
    await message.answer(texts.CONTACT_SAVED, reply_markup=ReplyKeyboardRemove())


def _extract_image(message: Message) -> tuple[str, str, bool, int | None] | None:
    if message.photo:
        p = message.photo[-1]
        return p.file_id, p.file_unique_id, False, p.file_size
    doc = message.document
    if doc and (doc.mime_type or "").lower() in ALLOWED_MIME:
        return doc.file_id, doc.file_unique_id, True, doc.file_size
    return None


@router.message(F.photo | F.document)
async def on_photo(message: Message, ctx: AppContext) -> None:
    assert message.from_user is not None
    image = _extract_image(message)
    if image is None:
        await message.answer(texts.BAD_IMAGE["format"])
        return
    file_id, file_unique_id, is_document, size = image
    if size and size > ctx.settings.max_image_bytes:
        mb = int(ctx.settings.max_image_bytes / 1024 / 1024)
        await message.answer(texts.BAD_IMAGE["too_large"].format(mb=mb))
        return
    async with ctx.sessionmaker() as s:
        user = await get_user_by_tg(s, message.from_user.id)
        if user is None:
            user = await upsert_user(s, message.from_user, message.chat.id)
            if user.username and user.registered_at is None:
                user.registered_at = datetime.now(UTC)

        dedup = (
            f"search:mg:{message.chat.id}:{message.media_group_id}"
            if message.media_group_id
            else f"search:{message.chat.id}:{message.message_id}"
        )
        created = await enqueue(
            s,
            "search_photo",
            {
                "user_db_id": user.id,
                "chat_id": message.chat.id,
                "message_id": message.message_id,
                "file_id": file_id,
                "file_unique_id": file_unique_id,
                "is_document": is_document,
                "caption": message.caption,
            },
            dedup_key=dedup,
            max_attempts=3,
        )
        if not created:
            return  # остальные фото того же альбома
        # Сообщение отправляется до commit: если отправка упадёт, задача не появится
        # и апдейт будет обработан повторно.
        text = texts.SEARCHING
        if message.media_group_id:
            text += "\n" + texts.ALBUM_FIRST_ONLY
        await message.answer(text)
        await s.commit()
    log.info("search enqueued", extra={"tg_id": message.from_user.id})


@router.message()
async def on_other(message: Message, ctx: AppContext) -> None:
    assert message.from_user is not None
    async with ctx.sessionmaker() as s:
        user = await get_user_by_tg(s, message.from_user.id)
        if user is None:
            user = await upsert_user(s, message.from_user, message.chat.id)
            await s.commit()
    if not user.username and not user.phone_shared and not user.phone_refused and not user.phone:
        await message.answer(texts.WELCOME_ASK_PHONE_OPTIONAL, reply_markup=phone_choice_keyboard())
    else:
        await message.answer(texts.SEND_PHOTO_HINT)

