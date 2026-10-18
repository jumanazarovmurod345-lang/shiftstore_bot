"""Все пользовательские тексты (русский язык)."""

from __future__ import annotations

from decimal import Decimal
from html import escape

WELCOME_WITH_USERNAME = (
    "Добро пожаловать в ShiftStore! 👋\n\n"
    "Отправьте мне фотографию одежды или скриншот, и я найду этот товар в каталоге нашего канала, "
    "покажу цену и доступные размеры."
)
WELCOME_ASK_PHONE_OPTIONAL = (
    "Чтобы нам было легче связаться с тобой, поделись номером телефона. Это необязательно."
)
SHARE_CONTACT_BUTTON = "📱 Поделиться контактом"
SHARE_PHONE_BUTTON = SHARE_CONTACT_BUTTON  # backward compatibility alias
REFUSE_PHONE_BUTTON = "❌ Не хочу давать номер"

CONTACT_SAVED = (
    "Спасибо! Твой контакт сохранён. Теперь ты можешь отправлять фотографии товаров для поиска в каталоге."
)
REGISTERED = CONTACT_SAVED
PHONE_REFUSED_EXPLANATION = (
    "У тебя нет Telegram username (@username), поэтому администратору будет сложнее связаться с тобой. "
    "Если товар не найдётся, мы можем не суметь быстро уточнить детали. Ты всё равно можешь пользоваться поиском."
)
WELCOME_BACK = (
    "С возвращением в ShiftStore! 👋\n\n"
    "Отправьте фотографию одежды или скриншот, чтобы узнать цену и доступные размеры."
)
PHONE_UPDATE_ASK = "Нажмите кнопку ниже, чтобы поделиться актуальным номером телефона."
PHONE_UPDATED = "Номер телефона обновлён. Можете отправить фотографию товара."
FOREIGN_CONTACT = (
    "Пожалуйста, отправьте именно свой контакт с помощью кнопки «📱 Поделиться контактом». "
    "Чужие контакты не принимаются."
)
SEARCHING = "🔎 Ищу товар в нашем каталоге…"
NOT_FOUND = (
    "К сожалению, мы не нашли этот товар в нашем каталоге 😔\n\n"
    "Мы передали фотографию администратору. Он попробует найти эту модель и свяжется с тобой, если сможет её найти.\n\n"
    "Спасибо за ожидание! ❤️"
)
TEMP_PROBLEM = (
    "⚠️ Сейчас поиск временно недоступен по техническим причинам. Ваша заявка передана "
    "администратору — он попробует найти товар и свяжется с вами."
)
TEMP_PROBLEM_NO_ADMIN = (
    "⚠️ Сейчас поиск временно недоступен по техническим причинам. Попробуйте позже "
    "или напишите администратору."
)
SEND_PHOTO_HINT = "Отправьте фотографию или скриншот одежды — я найду товар в каталоге нашего канала."
ALBUM_FIRST_ONLY = "Ищу по первой фотографии из альбома."
HELP = (
    "Как пользоваться ботом:\n"
    "1. Отправьте фото или скриншот одежды.\n"
    "2. Я найду товар в каталоге нашего Telegram-канала и покажу цену и размеры.\n"
    "3. Если товар не найден — администратор свяжется с вами.\n\n"
    "/phone — поделиться номером телефона\n"
    "/help — инструкция"
)
BAD_IMAGE = {
    "too_large": "Файл слишком большой. Максимальный размер — {mb} МБ.",
    "format": "Поддерживаются только изображения JPG, PNG и WEBP.",
    "corrupted": "Не удалось прочитать изображение. Попробуйте отправить другое фото.",
    "too_small": "Изображение слишком маленькое. Отправьте фото лучшего качества.",
    "too_many_pixels": "Изображение слишком большое по разрешению. Отправьте фото поменьше.",
    "empty": "Файл пуст. Попробуйте отправить фото ещё раз.",
}
VIEW_IN_CHANNEL = "Посмотреть в канале"
ASK_ADMIN = "уточняется"

REASONS = {
    "not_found": "товар не найден",
    "uncertain": "совпадение неуверенное",
    "search_unavailable": "поиск временно недоступен (технический сбой)",
    "index_incomplete": "индексация каталога не завершена",
}
STATUSES = {"new": "🆕 новая", "in_progress": "🟡 в работе", "closed": "✅ закрыта"}


def format_price(price: Decimal | None, currency: str | None) -> str:
    if price is None:
        return f"<i>{ASK_ADMIN}</i>"
    if price == price.to_integral_value():
        num = f"{int(price):,}".replace(",", " ")
    else:
        num = f"{price:,.2f}".replace(",", " ")
    return f"{num} {escape(currency)}" if currency else f"{num} <i>(валюта {ASK_ADMIN})</i>"


def format_sizes(sizes: list[str] | None) -> str:
    if not sizes:
        return f"<i>уточняются</i>"
    return escape(", ".join(sizes))


def product_card(
    name: str | None,
    price: Decimal | None,
    currency: str | None,
    sizes: list[str] | None,
    availability: str | None = None,
    sku: str | None = None,
    post_url: str | None = None,
) -> str:
    lines = ["✅ <b>Товар найден!</b>", ""]
    lines.append(f"<b>Название:</b> {escape(name)}" if name else f"<b>Название:</b> <i>{ASK_ADMIN}</i>")
    if sku:
        lines.append(f"<b>Артикул:</b> {escape(sku)}")
    lines.append(f"💰 <b>Цена:</b> {format_price(price, currency)}")
    lines.append(f"📏 <b>Доступные размеры:</b> {format_sizes(sizes)}")
    if availability:
        lines.append(f"📦 <b>Наличие:</b> {escape(availability)}")
    if post_url:
        lines.append(f"\n🔗 <a href=\"{escape(post_url)}\">Посмотреть в канале</a>")
    return "\n".join(lines)
