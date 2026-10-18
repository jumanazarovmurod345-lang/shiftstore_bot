"""Тесты сценариев пользователя и администратора по ТЗ:
1. Регистрация с username (без запроса телефона)
2. Регистрация без username (запрос телефона с 2 кнопками)
3. Предоставление своего контакта (сохранение, phone_shared=True, подтверждение)
4. Предоставление чужого контакта (отклонение)
5. Отказ от предоставления контакта (сохранение отказа, объяснение, доступ к поиску)
6. Повторный запуск /start (без повторного запроса номера)
7. Поиск товара: найденный товар (карточка, фото, цена, размеры, ссылка)
8. Поиск товара: ненайденный товар (сообщение клиенту и заявка администратору)
9. Уведомление администратора: проверка всех обязательных полей
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from aiogram.types import (
    Chat,
    Contact,
    KeyboardButton,
    Message,
    PhotoSize,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    User as TgUser,
)

from app.bot import texts
from app.bot.user import cmd_start, on_contact, on_photo, on_refuse_phone, phone_choice_keyboard
from app.db.models import Product, ProductImage, Request, User
from app.search.matcher import Candidate, MatchDecision
from app.services.requests import render_admin_text
from app.services.users import ForeignContactError, normalize_phone
from app.tasks import _send_match


class DummyAsyncContextManager:
    def __init__(self, obj):
        self.obj = obj

    async def __aenter__(self):
        return self.obj

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


def make_dummy_session(user_record: User | None = None):
    session = AsyncMock()
    # Хранилище пользователя в памяти
    storage = {"user": user_record}

    async def mock_get(model, ident, **kwargs):
        if model is User:
            return storage["user"]
        return None

    session.get = mock_get

    async def mock_execute(stmt, **kwargs):
        mock_result = MagicMock()
        # Для execute(select(User)...)
        mock_result.scalar_one_or_none.return_value = storage["user"]
        mock_result.scalar_one.return_value = 1
        return mock_result

    session.execute = mock_execute
    session.commit = AsyncMock()
    session.flush = AsyncMock()
    return session, storage


def make_dummy_context(session):
    ctx = MagicMock()
    ctx.sessionmaker = MagicMock(return_value=DummyAsyncContextManager(session))
    ctx.bot = AsyncMock()
    ctx.settings = MagicMock()
    ctx.settings.max_image_bytes = 10 * 1024 * 1024
    ctx.tz = ZoneInfo("Asia/Tashkent")
    return ctx


# ==============================================================================
# 1. Сценарий: Пользователь с username нажимает /start
# ==============================================================================
@pytest.mark.asyncio
async def test_scenario_start_with_username():
    user_db = User(
        id=1,
        telegram_id=1001,
        chat_id=1001,
        first_name="Иван",
        last_name="Иванов",
        username="ivan_tg",
        phone=None,
        phone_shared=False,
        phone_refused=False,
    )
    session, _ = make_dummy_session(user_db)
    ctx = make_dummy_context(session)

    # Мокируем upsert_user для изоляции от PostgreSQL диалекта
    with pytest.MonkeyPatch.context() as mp:
        async def mock_upsert(s, tg, chat_id):
            return user_db
        mp.setattr("app.bot.user.upsert_user", mock_upsert)

        message = AsyncMock(spec=Message)
        message.answer = AsyncMock()
        message.from_user = TgUser(id=1001, is_bot=False, first_name="Иван", last_name="Иванов", username="ivan_tg")
        message.chat = Chat(id=1001, type="private")

        await cmd_start(message, ctx)

        # Проверяем, что бот приветствует без запроса телефона
        message.answer.assert_called_once()
        args, kwargs = message.answer.call_args
        assert args[0] == texts.WELCOME_WITH_USERNAME
        assert isinstance(kwargs["reply_markup"], ReplyKeyboardRemove)


# ==============================================================================
# 2. Сценарий: Пользователь БЕЗ username нажимает /start первый раз
# ==============================================================================
@pytest.mark.asyncio
async def test_scenario_start_without_username_initial():
    user_db = User(
        id=2,
        telegram_id=2002,
        chat_id=2002,
        first_name="Анна",
        last_name=None,
        username=None,
        phone=None,
        phone_shared=False,
        phone_refused=False,
    )
    session, _ = make_dummy_session(user_db)
    ctx = make_dummy_context(session)

    with pytest.MonkeyPatch.context() as mp:
        async def mock_upsert(s, tg, chat_id):
            return user_db
        mp.setattr("app.bot.user.upsert_user", mock_upsert)

        message = AsyncMock(spec=Message)
        message.answer = AsyncMock()
        message.from_user = TgUser(id=2002, is_bot=False, first_name="Анна", username=None)
        message.chat = Chat(id=2002, type="private")

        await cmd_start(message, ctx)

        message.answer.assert_called_once()
        args, kwargs = message.answer.call_args
        assert args[0] == texts.WELCOME_ASK_PHONE_OPTIONAL
        assert "Чтобы нам было легче связаться с тобой" in args[0]

        # Проверяем 2 кнопки: «📱 Поделиться контактом» и «❌ Не хочу давать номер»
        kb = kwargs["reply_markup"]
        assert isinstance(kb, ReplyKeyboardMarkup)
        assert len(kb.keyboard) == 2
        btn_contact = kb.keyboard[0][0]
        btn_refuse = kb.keyboard[1][0]
        assert btn_contact.text == "📱 Поделиться контактом"
        assert btn_contact.request_contact is True
        assert btn_refuse.text == "❌ Не хочу давать номер"


# ==============================================================================
# 3. Сценарий: Клиент делится своим контактом
# ==============================================================================
@pytest.mark.asyncio
async def test_scenario_share_own_contact_success():
    user_db = User(
        id=2,
        telegram_id=2002,
        chat_id=2002,
        first_name="Анна",
        username=None,
        phone=None,
        phone_shared=False,
    )
    session, _ = make_dummy_session(user_db)
    ctx = make_dummy_context(session)

    with pytest.MonkeyPatch.context() as mp:
        async def mock_save_contact(s, tg, chat_id, contact_user_id, phone):
            if contact_user_id is not None and contact_user_id != tg.id:
                raise ForeignContactError
            user_db.phone = normalize_phone(phone)
            user_db.phone_shared = True
            return user_db, True
        mp.setattr("app.bot.user.save_own_contact", mock_save_contact)

        message = AsyncMock(spec=Message)
        message.answer = AsyncMock()
        message.from_user = TgUser(id=2002, is_bot=False, first_name="Анна")
        message.chat = Chat(id=2002, type="private")
        message.contact = Contact(phone_number="+998901234567", first_name="Анна", user_id=2002)

        await on_contact(message, ctx)

        assert user_db.phone == "+998901234567"
        assert user_db.phone_shared is True
        message.answer.assert_called_once()
        args, kwargs = message.answer.call_args
        assert args[0] == texts.CONTACT_SAVED
        assert isinstance(kwargs["reply_markup"], ReplyKeyboardRemove)


# ==============================================================================
# 4. Сценарий: Клиент пытается отправить чужой контакт
# ==============================================================================
@pytest.mark.asyncio
async def test_scenario_share_foreign_contact_rejected():
    user_db = User(
        id=2,
        telegram_id=2002,
        chat_id=2002,
        first_name="Анна",
        username=None,
        phone=None,
        phone_shared=False,
    )
    session, _ = make_dummy_session(user_db)
    ctx = make_dummy_context(session)

    with pytest.MonkeyPatch.context() as mp:
        async def mock_save_contact(s, tg, chat_id, contact_user_id, phone):
            if contact_user_id is not None and contact_user_id != tg.id:
                raise ForeignContactError
            return user_db, False
        mp.setattr("app.bot.user.save_own_contact", mock_save_contact)

        message = AsyncMock(spec=Message)
        message.answer = AsyncMock()
        message.from_user = TgUser(id=2002, is_bot=False, first_name="Анна")
        message.chat = Chat(id=2002, type="private")
        # user_id контакта 9999 не совпадает с tg.id 2002
        message.contact = Contact(phone_number="+998909999999", first_name="Другой", user_id=9999)

        await on_contact(message, ctx)

        # Контакт не сохранён, выводится предупреждение
        assert user_db.phone is None
        message.answer.assert_called_once()
        args, _ = message.answer.call_args
        assert args[0] == texts.FOREIGN_CONTACT


# ==============================================================================
# 5. Сценарий: Клиент нажимает «❌ Не хочу давать номер»
# ==============================================================================
@pytest.mark.asyncio
async def test_scenario_refuse_phone():
    user_db = User(
        id=3,
        telegram_id=3003,
        chat_id=3003,
        first_name="Олег",
        username=None,
        phone=None,
        phone_shared=False,
        phone_refused=False,
    )
    session, _ = make_dummy_session(user_db)
    ctx = make_dummy_context(session)

    with pytest.MonkeyPatch.context() as mp:
        async def mock_refuse(s, tg, chat_id):
            user_db.phone_refused = True
            return user_db
        mp.setattr("app.bot.user.refuse_phone", mock_refuse)

        message = AsyncMock(spec=Message)
        message.answer = AsyncMock()
        message.from_user = TgUser(id=3003, is_bot=False, first_name="Олег")
        message.chat = Chat(id=3003, type="private")

        await on_refuse_phone(message, ctx)

        assert user_db.phone_refused is True
        message.answer.assert_called_once()
        args, kwargs = message.answer.call_args
        assert args[0] == texts.PHONE_REFUSED_EXPLANATION
        assert "У тебя нет Telegram username (@username)" in args[0]
        assert "Ты всё равно можешь пользоваться поиском" in args[0]
        assert isinstance(kwargs["reply_markup"], ReplyKeyboardRemove)


# ==============================================================================
# 6. Сценарий: Повторный /start после сохранения номера или отказа
# ==============================================================================
@pytest.mark.asyncio
async def test_scenario_repeated_start_does_not_ask_again():
    # Пользователь без username, но уже с phone_refused=True
    user_db = User(
        id=3,
        telegram_id=3003,
        chat_id=3003,
        first_name="Олег",
        username=None,
        phone=None,
        phone_shared=False,
        phone_refused=True,
    )
    session, _ = make_dummy_session(user_db)
    ctx = make_dummy_context(session)

    with pytest.MonkeyPatch.context() as mp:
        async def mock_upsert(s, tg, chat_id):
            return user_db
        mp.setattr("app.bot.user.upsert_user", mock_upsert)

        message = AsyncMock(spec=Message)
        message.answer = AsyncMock()
        message.from_user = TgUser(id=3003, is_bot=False, first_name="Олег", username=None)
        message.chat = Chat(id=3003, type="private")

        await cmd_start(message, ctx)

        message.answer.assert_called_once()
        args, kwargs = message.answer.call_args
        # Показывает приветствие С возвращением! Без кнопок запроса телефона
        assert args[0] == texts.WELCOME_BACK
        assert isinstance(kwargs["reply_markup"], ReplyKeyboardRemove)


# ==============================================================================
# 7. Сценарий: Отправка фото разрешена даже без номера телефона
# ==============================================================================
@pytest.mark.asyncio
async def test_scenario_photo_search_allowed_without_phone():
    user_db = User(
        id=3,
        telegram_id=3003,
        chat_id=3003,
        first_name="Олег",
        username=None,
        phone=None,
        phone_shared=False,
        phone_refused=True,
    )
    session, _ = make_dummy_session(user_db)
    ctx = make_dummy_context(session)

    with pytest.MonkeyPatch.context() as mp:
        async def mock_get_user(s, tg_id):
            return user_db
        async def mock_enqueue(s, kind, payload, **kwargs):
            return True
        mp.setattr("app.bot.user.get_user_by_tg", mock_get_user)
        mp.setattr("app.bot.user.enqueue", mock_enqueue)

        message = AsyncMock(spec=Message)
        message.answer = AsyncMock()
        message.from_user = TgUser(id=3003, is_bot=False, first_name="Олег")
        message.chat = Chat(id=3003, type="private")
        message.media_group_id = None
        message.message_id = 55
        message.photo = [PhotoSize(file_id="p1", file_unique_id="u1", width=100, height=100, file_size=5000)]
        message.document = None
        message.caption = None

        await on_photo(message, ctx)

        # Сообщение о поиске отправлено, пользователь не заблокирован
        message.answer.assert_called_once()
        args, _ = message.answer.call_args
        assert texts.SEARCHING in args[0]


# ==============================================================================
# 8. Сценарий: Найденный товар (отправка карточки с фото, названием, ценой, размерами, ссылкой)
# ==============================================================================
@pytest.mark.asyncio
async def test_scenario_send_match_card():
    prod = Product(
        id=5,
        name="Худи Shift Оверсайз",
        price=Decimal("450000.00"),
        currency="UZS",
        sizes=["M", "L", "XL"],
        availability="в наличии",
        sku="HD-01",
        post_url="https://t.me/shiftchannel/100",
    )
    img = ProductImage(id=15, product_id=5, file_id="tg_file_hoodie", is_active=True)

    session = AsyncMock()
    async def mock_get(model, ident, **kwargs):
        if model is Product:
            return prod
        if model is ProductImage:
            return img
        return None
    session.get = mock_get
    ctx = make_dummy_context(session)

    decision = MatchDecision(
        outcome="match",
        stage="phash",
        best=Candidate(product_id=5, image_id=15, score=1.0),
    )

    await _send_match(ctx, chat_id=1001, reply_to=77, decision=decision)

    ctx.bot.send_photo.assert_called_once()
    args, kwargs = ctx.bot.send_photo.call_args
    assert args[0] == 1001  # chat_id
    assert args[1] == "tg_file_hoodie"  # file_id из каталога
    caption = kwargs["caption"]
    assert "Худи Shift Оверсайз" in caption
    assert "450 000 UZS" in caption
    assert "M, L, XL" in caption
    assert "HD-01" in caption
    assert "https://t.me/shiftchannel/100" in caption


# ==============================================================================
# 9. Сценарий: Заявка администратору при ненайденном товаре
# ==============================================================================
def test_scenario_admin_notification_fields():
    tz = ZoneInfo("Asia/Tashkent")

    # Пользователь добровольно поделился контактом
    u_with_phone = User(
        id=1,
        telegram_id=987654321,
        chat_id=987654321,
        first_name="Дмитрий",
        last_name="Смирнов",
        username="dmitry_sm",
        phone="+998905554433",
        phone_shared=True,
    )
    req1 = Request(
        id=20,
        user=u_with_phone,
        created_at=datetime(2026, 10, 15, 14, 30, tzinfo=UTC),
        status="new",
        reason="not_found",
    )
    text1 = render_admin_text(req1, tz)

    # Проверка всех обязательных пунктов из ТЗ:
    # 1. заголовок
    assert "🔎 <b>Новая заявка на поиск товара</b>" in text1
    # 2. Telegram user ID
    assert "<b>Telegram user ID:</b> <code>987654321</code>" in text1
    # 3. username в формате @username
    assert "<b>Username:</b> @dmitry_sm" in text1
    # 4. имя и фамилия
    assert "<b>Имя и фамилия:</b> Дмитрий Смирнов" in text1
    # 5. номер телефона (поделился добровольно)
    assert "<b>Номер телефона:</b> +998905554433" in text1
    # 6. статус: Нужно найти товар вручную
    assert "<b>Статус:</b> Нужно найти товар вручную" in text1
    # 7. дата и время
    assert "<b>Дата и время заявки:</b>" in text1

    # Пользователь без username и без номера телефона
    u_no_data = User(
        id=2,
        telegram_id=11223344,
        chat_id=11223344,
        first_name=None,
        last_name=None,
        username=None,
        phone="+998900000000",
        phone_shared=False,  # НЕ делился добровольно!
    )
    req2 = Request(
        id=21,
        user=u_no_data,
        created_at=datetime(2026, 10, 15, 14, 30, tzinfo=UTC),
        status="new",
        reason="not_found",
    )
    text2 = render_admin_text(req2, tz)

    # Телефон НЕ должен отображаться, так как phone_shared=False
    assert "Номер телефона:" not in text2
    assert "+998900000000" not in text2
    # Обязательное предупреждение
    assert "⚠️ <b>Нет username и номера телефона — связаться будет сложнее.</b>" in text2
