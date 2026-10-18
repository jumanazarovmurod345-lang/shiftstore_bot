"""Пакет автоматизированных тестов для Telegram-бота магазина."""

from __future__ import annotations

import asyncio
from decimal import Decimal
import numpy as np
import pytest
from sqlalchemy import select

from app.catalog.parser import KeyValueCaptionParser
from app.catalog.service import PostInput, PhotoRef, ingest_post
from app.db.models import User, Product, ProductImage, Request, Job, ProcessedUpdate
from app.imaging import phash, hamming
from app.intake import store_update
from app.queue.jobs import enqueue, claim, complete, fail
from app.search.matcher import Thresholds, decide_embedding, decide_phash
from app.services.users import save_own_contact, ForeignContactError, upsert_user
from app.services.requests import create_request, get_request, apply_status
from PIL import Image


class FakeUser:
    def __init__(self, id: int, first_name: str = "Test", last_name: str | None = "User", username: str | None = "testuser"):
        self.id = id
        self.first_name = first_name
        self.last_name = last_name
        self.username = username


# ==============================================================================
# 1. Регистрация со своим и чужим контактом
# ==============================================================================
@pytest.mark.asyncio
async def test_user_contact_registration(session):
    u = FakeUser(1001, "Иван", "Иванов", "ivan")
    
    # Чужой контакт должен вызывать ошибку ForeignContactError
    with pytest.raises(ForeignContactError):
        await save_own_contact(session, u, chat_id=1001, contact_user_id=9999, phone="+998901112233")
    
    # Свой контакт успешно регистрирует
    user, is_first = await save_own_contact(session, u, chat_id=1001, contact_user_id=1001, phone="+998901112233")
    assert is_first is True
    assert user.phone == "+998901112233"
    assert user.registered_at is not None

    # Повторное обновление номера
    user2, is_first2 = await save_own_contact(session, u, chat_id=1001, contact_user_id=1001, phone="+998909998877")
    assert is_first2 is False
    assert user2.phone == "+998909998877"


# ==============================================================================
# 2. Создание и обновление товара, сборка альбома
# ==============================================================================
@pytest.mark.asyncio
async def test_catalog_post_and_album(session):
    parser = KeyValueCaptionParser()
    channel_id = -100111222333

    # Сообщение 1 в альбоме (без подписи)
    inp1 = PostInput(
        source_chat_id=channel_id,
        source_message_id=501,
        caption=None,
        photo=PhotoRef("file_id_1", "unique_1", 800, 800),
        media_group_id="album_100",
        channel_username="myshop",
    )
    res1 = await ingest_post(session, inp1, parser)
    assert res1 is not None
    assert res1.created is True
    product_id = res1.product_id

    # Сообщение 2 в альбоме (с полной подписью)
    caption_text = (
        "Название: Кроссовки Nike Air Max\n"
        "Артикул: NK-001\n"
        "Цена: 650000 UZS\n"
        "Размеры: 40, 41, 42, 43\n"
        "Наличие: в наличии"
    )
    inp2 = PostInput(
        source_chat_id=channel_id,
        source_message_id=502,
        caption=caption_text,
        photo=PhotoRef("file_id_2", "unique_2", 800, 800),
        media_group_id="album_100",
        channel_username="myshop",
    )
    res2 = await ingest_post(session, inp2, parser)
    assert res2.product_id == product_id

    # Проверяем в БД: один товар, два фото, распознанные поля
    prod = await session.get(Product, product_id)
    assert prod.name == "Кроссовки Nike Air Max"
    assert prod.sku == "NK-001"
    assert prod.price == Decimal("650000.00")
    assert prod.currency == "UZS"
    assert prod.sizes == ["40", "41", "42", "43"]
    assert len(prod.images) == 2

    # Редактирование поста (обновление цены)
    edit_caption = (
        "Название: Кроссовки Nike Air Max\n"
        "Артикул: NK-001\n"
        "Цена: 700000 UZS\n"
        "Размеры: 41, 42\n"
        "Наличие: мало"
    )
    inp2_edit = PostInput(
        source_chat_id=channel_id,
        source_message_id=502,
        caption=edit_caption,
        photo=PhotoRef("file_id_2", "unique_2", 800, 800),
        media_group_id="album_100",
        channel_username="myshop",
    )
    await ingest_post(session, inp2_edit, parser)
    await session.refresh(prod)
    assert prod.price == Decimal("700000.00")
    assert prod.sizes == ["41", "42"]


# ==============================================================================
# 3. Отсутствие дублей при повторном update
# ==============================================================================
@pytest.mark.asyncio
async def test_update_idempotency(session):
    update_data = {
        "update_id": 999888777,
        "message": {
            "message_id": 12,
            "date": 1700000000,
            "chat": {"id": 1001, "type": "private"},
            "text": "test"
        }
    }
    # Первый приём
    saved1 = await store_update(session, update_data)
    assert saved1 is True

    # Повторный приём того же update_id
    saved2 = await store_update(session, update_data)
    assert saved2 is False

    # В таблице processed_updates ровно 1 запись
    res = await session.execute(select(ProcessedUpdate).where(ProcessedUpdate.update_id == 999888777))
    assert len(res.scalars().all()) == 1


# ==============================================================================
# 4. Поиск: уверенное, неоднозначное совпадение и pHash
# ==============================================================================
def test_search_matching_logic():
    # Тест pHash
    img1 = Image.new("RGB", (100, 100), color="red")
    img2 = Image.new("RGB", (100, 100), color="red")
    h1 = phash(img1)
    h2 = phash(img2)
    assert hamming(h1, h2) == 0

    product_ids = np.array([1, 2], dtype=np.int64)
    image_ids = np.array([10, 20], dtype=np.int64)
    dists = np.array([2, 25], dtype=np.int64)
    decision = decide_phash(product_ids, image_ids, dists, max_distance=6)
    assert decision is not None
    assert decision.outcome == "match"
    assert decision.stage == "phash"
    assert decision.best.product_id == 1

    # Тест эмбеддингов
    t = Thresholds(match_threshold=0.86, match_margin=0.03, match_min_score=0.70, phash_max_distance=6)
    
    # 1. Уверенное совпадение: score >= 0.86 и margin >= 0.03
    scores_clear = np.array([0.92, 0.80], dtype=np.float32)
    dec1 = decide_embedding(product_ids, image_ids, scores_clear, t)
    assert dec1.outcome == "match"
    assert dec1.best.product_id == 1

    # 2. Неоднозначное совпадение (слишком близкий второй кандидат)
    scores_close = np.array([0.89, 0.88], dtype=np.float32)
    dec2 = decide_embedding(product_ids, image_ids, scores_close, t)
    assert dec2.outcome == "uncertain"
    assert dec2.best.product_id == 1

    # 3. Ниже порога уверенности, но выше min_score
    scores_mid = np.array([0.75, 0.60], dtype=np.float32)
    dec3 = decide_embedding(product_ids, image_ids, scores_mid, t)
    assert dec3.outcome == "uncertain"

    # 4. Совсем низкий результат
    scores_low = np.array([0.50, 0.40], dtype=np.float32)
    dec4 = decide_embedding(product_ids, image_ids, scores_low, t)
    assert dec4.outcome == "not_found"


# ==============================================================================
# 5. Передача заявки администратору с контактом клиента
# ==============================================================================
@pytest.mark.asyncio
async def test_admin_request_flow(session):
    u = FakeUser(2001, "Сергей", None, "sergey_tg")
    user, _ = await save_own_contact(session, u, chat_id=2001, contact_user_id=2001, phone="+998931234567")
    
    req_id = await create_request(
        session,
        user_id=user.id,
        source_chat_id=2001,
        source_message_id=33,
        photo_file_id="tg_photo_file_id",
        photo_file_unique_id="tg_unique",
        photo_is_document=False,
        client_caption="Есть ли в наличии 42 размер?",
        reason="uncertain",
        candidates=[{"product_id": 1, "score": 0.87, "name": "Кроссовки"}],
    )
    await session.commit()

    req = await get_request(session, req_id)
    assert req is not None
    assert req.user.phone == "+998931234567"
    assert req.status == "new"

    # Смена статуса: администратор берет в работу
    apply_status(req, "take", admin_id=999)
    assert req.status == "in_progress"
    assert req.assigned_admin_id == 999

    # Закрытие заявки
    apply_status(req, "close", admin_id=999)
    assert req.status == "closed"
    assert req.closed_at is not None


# ==============================================================================
# 6. Восстановление задач очереди после сбоя
# ==============================================================================
@pytest.mark.asyncio
async def test_job_queue_recovery(session):
    # Добавляем задачу
    await enqueue(session, "test_job", {"data": 123}, dedup_key="job_recovery_1")
    await session.commit()

    # Захватываем воркером
    job1 = await claim(session, worker_id="worker_A", lease_seconds=10)
    assert job1 is not None
    assert job1.attempts == 1

    # Имитируем сбой выполнения с повтором
    res = await fail(session, job1, worker_id="worker_A", error="temporary connection error", retry_after=0.0)
    assert res == "retry"

    # Задача снова доступна для захвата другим воркером
    job2 = await claim(session, worker_id="worker_B", lease_seconds=10)
    assert job2 is not None
    assert job2.attempts == 2

    # Успешное завершение
    done = await complete(session, job2.id, worker_id="worker_B")
    assert done is True


# ==============================================================================
# 7. Проверка прав администратора и webhook secret
# ==============================================================================
def test_admin_and_webhook_security():
    from app.config import Settings
    import hmac

    s = Settings(
        BOT_TOKEN="1234:FakeToken",
        CHANNEL_ID=-1001,
        ADMIN_CHAT_ID=-1002,
        ADMIN_IDS="111, 222, 333",
        BOT_MODE="webhook",
        APP_BASE_URL="https://example.com",
        WEBHOOK_SECRET="super-secret-key-12345",
        DATABASE_URL="postgresql://postgres@localhost:5432/db",
    )
    # Проверка списка администраторов
    assert 111 in s.admin_ids
    assert 222 in s.admin_ids
    assert 999 not in s.admin_ids

    # Проверка Webhook Secret токена
    secret_token = s.webhook_secret.get_secret_value()
    assert hmac.compare_digest("super-secret-key-12345", secret_token) is True
    assert hmac.compare_digest("wrong-token", secret_token) is False


# ==============================================================================
# 8. Настройка ADMIN_ID через переменные окружения
# ==============================================================================
def test_admin_id_env_configuration():
    from app.config import Settings
    import pytest

    # Задание только ADMIN_ID
    s = Settings(
        BOT_TOKEN="1234:FakeToken",
        CHANNEL_ID=-1001,
        ADMIN_ID=777888999,
        BOT_MODE="webhook",
        APP_BASE_URL="https://example.com",
        WEBHOOK_SECRET="super-secret-key-12345",
        DATABASE_URL="postgresql://postgres@localhost:5432/db",
    )
    assert s.admin_id == 777888999
    assert s.admin_chat_id == 777888999
    assert 777888999 in s.admin_ids

    # Ошибка если ADMIN_ID не настроен
    with pytest.raises(ValueError, match="ADMIN_ID не настроен"):
        Settings(
            _env_file=None,
            BOT_TOKEN="1234:FakeToken",
            CHANNEL_ID=-1001,
            BOT_MODE="webhook",
            APP_BASE_URL="https://example.com",
            WEBHOOK_SECRET="super-secret-key-12345",
            DATABASE_URL="postgresql://postgres@localhost:5432/db",
        )


# ==============================================================================
# 9. Форматирование карточки товара (не выдумывать цены и размеры)
# ==============================================================================
def test_product_card_formatting():
    from app.bot.texts import product_card

    # Полная информация о товаре
    card_full = product_card(
        name="Куртка зимняя Shift",
        price=Decimal("1200000.00"),
        currency="UZS",
        sizes=["S", "M", "L"],
        availability="в наличии",
        sku="JK-2026",
        post_url="https://t.me/shiftstore/42",
    )
    assert "Куртка зимняя Shift" in card_full
    assert "1 200 000 UZS" in card_full
    assert "S, M, L" in card_full
    assert "JK-2026" in card_full
    assert "https://t.me/shiftstore/42" in card_full

    # Неполная информация: цена и размеры отсутствуют -> «уточняется» / «уточняются»
    card_missing = product_card(
        name=None,
        price=None,
        currency=None,
        sizes=None,
    )
    assert "уточняется" in card_missing
    assert "уточняются" in card_missing
    assert "Название:</b> <i>уточняется</i>" in card_missing


# ==============================================================================
# 10. Форматирование уведомления администратору по ТЗ
# ==============================================================================
def test_admin_notification_text_formatting():
    from app.services.requests import render_admin_text
    from app.db.models import Request, User
    from zoneinfo import ZoneInfo
    from datetime import datetime, UTC

    tz = ZoneInfo("Asia/Tashkent")

    # Сценарий 1: Клиент с username и добровольно переданным номером телефона
    u1 = User(
        id=1,
        telegram_id=1001,
        chat_id=1001,
        first_name="Иван",
        last_name="Иванов",
        username="ivan_tg",
        phone="+998901112233",
        phone_shared=True,
    )
    req1 = Request(
        id=10,
        user=u1,
        created_at=datetime(2026, 10, 15, 12, 0, tzinfo=UTC),
        client_caption="Есть ли размер XL?",
        status="new",
        reason="not_found",
    )
    text1 = render_admin_text(req1, tz)
    assert "🔎 <b>Новая заявка на поиск товара</b>" in text1
    assert "<b>Статус:</b> Нужно найти товар вручную" in text1
    assert "<b>Telegram user ID:</b> <code>1001</code>" in text1
    assert "<b>Username:</b> @ivan_tg" in text1
    assert "<b>Имя и фамилия:</b> Иван Иванов" in text1
    assert "<b>Номер телефона:</b> +998901112233" in text1
    assert "<b>Дата и время заявки:</b>" in text1
    assert "Нет username и номера телефона" not in text1

    # Сценарий 2: Клиент БЕЗ username и БЕЗ номера телефона
    u2 = User(
        id=2,
        telegram_id=2002,
        chat_id=2002,
        first_name="Анна",
        last_name=None,
        username=None,
        phone=None,
        phone_shared=False,
    )
    req2 = Request(
        id=11,
        user=u2,
        created_at=datetime(2026, 10, 15, 12, 0, tzinfo=UTC),
        status="new",
        reason="not_found",
    )
    text2 = render_admin_text(req2, tz)
    assert "<b>Username:</b> не указан" in text2
    assert "Номер телефона:" not in text2  # номер телефона не выводится
    assert "⚠️ <b>Нет username и номера телефона — связаться будет сложнее.</b>" in text2

    # Сценарий 3: Клиент С username, но БЕЗ номера телефона
    u3 = User(
        id=3,
        telegram_id=3003,
        chat_id=3003,
        first_name="Петр",
        last_name=None,
        username="petr_tg",
        phone=None,
        phone_shared=False,
    )
    req3 = Request(
        id=12,
        user=u3,
        created_at=datetime(2026, 10, 15, 12, 0, tzinfo=UTC),
        status="new",
        reason="uncertain",
    )
    text3 = render_admin_text(req3, tz)
    assert "<b>Username:</b> @petr_tg" in text3
    assert "Номер телефона:" not in text3
    # Так как username есть, предупреждения о сложности связи нет
    assert "Нет username и номера телефона" not in text3


# ==============================================================================
# 11. Текст для клиента при ненайденном товаре
# ==============================================================================
def test_client_not_found_message_exact_text():
    from app.bot import texts

    expected = (
        "К сожалению, мы не нашли этот товар в нашем каталоге 😔\n\n"
        "Мы передали фотографию администратору. Он попробует найти эту модель и свяжется с тобой, если сможет её найти.\n\n"
        "Спасибо за ожидание! ❤️"
    )
    assert texts.NOT_FOUND == expected

