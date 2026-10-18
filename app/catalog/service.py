"""Каталог: создание/обновление товаров из постов канала и пересланных постов.

Договорённость MVP: один пост или один альбом (media_group_id) = один товар.
Альбом собирается инкрементально в БД: каждое сообщение альбома приходит
отдельным апдейтом (в любом порядке, возможно после перезапуска), подпись может
быть только у одного из них. Повторные апдейты не создают дублей благодаря
уникальным ключам (source_chat_id, group_key) и (source_chat_id, source_message_id).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog.parser import CaptionParser
from app.db.models import Product, ProductImage

log = logging.getLogger(__name__)


@dataclass(slots=True)
class PhotoRef:
    file_id: str
    file_unique_id: str
    width: int | None = None
    height: int | None = None


@dataclass(slots=True)
class PostInput:
    source_chat_id: int
    source_message_id: int
    caption: str | None
    photo: PhotoRef | None
    media_group_id: str | None = None  # оригинальный id альбома в канале
    forward_group_key: str | None = None  # для пересланных альбомов: "fwd:<chat>:<mgid>"
    channel_username: str | None = None
    source: str = "channel"  # channel | import


@dataclass(slots=True)
class IngestResult:
    product_id: int
    image_id: int | None
    needs_index: bool
    file_unique_id: str | None
    created: bool


def build_post_url(chat_id: int, message_id: int, username: str | None) -> str | None:
    if username:
        return f"https://t.me/{username}/{message_id}"
    s = str(chat_id)
    if s.startswith("-100"):
        # Ссылка на приватный канал откроется только у его подписчиков.
        return f"https://t.me/c/{s[4:]}/{message_id}"
    return None


_LINK_RE = re.compile(r"t\.me/(?:c/(\d+)|([A-Za-z0-9_]{4,}))/(\d+)")


def parse_post_link(text: str) -> int | None:
    """Возвращает message_id из ссылки t.me/<username>/<id> или t.me/c/<id>/<id>."""
    m = _LINK_RE.search(text)
    return int(m.group(3)) if m else None


def _group_key(inp: PostInput) -> str:
    if inp.forward_group_key:
        return inp.forward_group_key
    if inp.media_group_id:
        return f"mg:{inp.media_group_id}"
    return f"msg:{inp.source_message_id}"


async def ingest_post(session: AsyncSession, inp: PostInput, parser: CaptionParser) -> IngestResult | None:
    """Идемпотентно применяет пост (новый или отредактированный) к каталогу.

    Вызывающий отвечает за commit.
    """
    existing_image = (
        await session.execute(
            select(ProductImage).where(
                ProductImage.source_chat_id == inp.source_chat_id,
                ProductImage.source_message_id == inp.source_message_id,
            )
        )
    ).scalar_one_or_none()

    product: Product | None = None
    created = False
    if existing_image is not None:
        product = await session.get(Product, existing_image.product_id, with_for_update=True)
    else:
        if inp.photo is None:
            # Текстовый пост без фото (или фото заменено на видео) — не товар.
            return None
        key = _group_key(inp)
        res = await session.execute(
            insert(Product)
            .values(
                source_chat_id=inp.source_chat_id,
                group_key=key,
                media_group_id=inp.media_group_id,
                first_message_id=inp.source_message_id,
                source=inp.source,
                post_url=build_post_url(inp.source_chat_id, inp.source_message_id, inp.channel_username),
            )
            .on_conflict_do_nothing(constraint="uq_products_source_group")
            .returning(Product.id)
        )
        created = res.first() is not None
        product = (
            await session.execute(
                select(Product)
                .where(Product.source_chat_id == inp.source_chat_id, Product.group_key == key)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
    assert product is not None

    # --- Ссылка на пост: всегда на первое сообщение альбома ---
    if product.first_message_id is None or inp.source_message_id < product.first_message_id:
        product.first_message_id = inp.source_message_id
        product.post_url = build_post_url(inp.source_chat_id, inp.source_message_id, inp.channel_username)
    elif inp.channel_username and product.post_url is None:
        product.post_url = build_post_url(inp.source_chat_id, product.first_message_id, inp.channel_username)

    # --- Подпись ---
    caption = (inp.caption or "").strip() or None
    is_album = not product.group_key.startswith("msg:")
    holds_caption = product.caption_message_id == inp.source_message_id
    if caption is not None or holds_caption or (not is_album):
        if caption != product.raw_caption or product.caption_message_id is None:
            parsed = parser.parse(caption)
            product.raw_caption = caption
            product.caption_message_id = inp.source_message_id if caption else None
            product.name = parsed.name
            product.sku = parsed.sku
            product.price = parsed.price
            product.currency = parsed.currency
            product.sizes = parsed.sizes
            product.availability = parsed.availability
            product.in_stock = parsed.in_stock

    # --- Фото ---
    image_id: int | None = None
    needs_index = False
    fuid: str | None = None
    if inp.photo is not None:
        fuid = inp.photo.file_unique_id
        if existing_image is None:
            res = await session.execute(
                insert(ProductImage)
                .values(
                    product_id=product.id,
                    source_chat_id=inp.source_chat_id,
                    source_message_id=inp.source_message_id,
                    file_id=inp.photo.file_id,
                    file_unique_id=fuid,
                    width=inp.photo.width,
                    height=inp.photo.height,
                    index_status="pending",
                )
                .on_conflict_do_nothing(constraint="uq_images_source_message")
                .returning(ProductImage.id)
            )
            row = res.first()
            if row is not None:
                image_id, needs_index = row[0], True
            else:  # конкурентная вставка того же сообщения
                existing_image = (
                    await session.execute(
                        select(ProductImage).where(
                            ProductImage.source_chat_id == inp.source_chat_id,
                            ProductImage.source_message_id == inp.source_message_id,
                        )
                    )
                ).scalar_one()
        if existing_image is not None:
            image_id = existing_image.id
            if existing_image.file_unique_id != fuid:
                # Фото заменено — сбрасываем индекс.
                existing_image.file_unique_id = fuid
                existing_image.phash = None
                existing_image.embedding = None
                existing_image.embedding_dim = None
                existing_image.embedding_model = None
                existing_image.index_status = "pending"
                existing_image.index_error = None
                needs_index = True
            existing_image.file_id = inp.photo.file_id  # file_id может обновляться
            existing_image.width = inp.photo.width
            existing_image.height = inp.photo.height
            if not existing_image.is_active:
                existing_image.is_active = True
                needs_index = needs_index or existing_image.index_status != "ready"
    elif existing_image is not None:
        # Медиа в сообщении заменено не на фото — фото больше не участвует в поиске.
        existing_image.is_active = False

    product.updated_at = datetime.now(UTC)
    await session.flush()
    return IngestResult(product.id, image_id, needs_index, fuid, created)


async def find_product(session: AsyncSession, ref: str, channel_id: int) -> Product | None:
    """Поиск товара по номеру (#12 / 12) или по ссылке на пост канала."""
    ref = ref.strip()
    msg_id = parse_post_link(ref)
    if msg_id is not None:
        image = (
            await session.execute(
                select(ProductImage).where(
                    ProductImage.source_chat_id == channel_id,
                    ProductImage.source_message_id == msg_id,
                )
            )
        ).scalar_one_or_none()
        if image is None:
            return (
                await session.execute(
                    select(Product).where(
                        Product.source_chat_id == channel_id, Product.first_message_id == msg_id
                    )
                )
            ).scalar_one_or_none()
        return await session.get(Product, image.product_id)
    digits = ref.lstrip("#")
    if digits.isdigit():
        return await session.get(Product, int(digits))
    return None


async def set_active(session: AsyncSession, product_id: int, active: bool, admin_id: int) -> None:
    await session.execute(
        update(Product)
        .where(Product.id == product_id)
        .values(
            is_active=active,
            deactivated_at=None if active else datetime.now(UTC),
            deactivated_by=None if active else admin_id,
            updated_at=datetime.now(UTC),
        )
    )
