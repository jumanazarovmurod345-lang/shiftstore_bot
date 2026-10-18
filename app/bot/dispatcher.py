"""Сборка диспетчера aiogram."""

from __future__ import annotations

from aiogram import Dispatcher

from app.bot import admin, channel, user


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    # Порядок важен: канал -> админ (ответы, импорт, команды) -> клиент.
    dp.include_routers(channel.router, admin.router, user.router)
    return dp


ALLOWED_UPDATES = [
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "callback_query",
    "my_chat_member",
]
