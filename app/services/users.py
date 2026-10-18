from __future__ import annotations

from datetime import UTC, datetime

from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User


def normalize_phone(raw: str) -> str:
    digits = "".join(ch for ch in raw if ch.isdigit())
    return "+" + digits if digits else raw


async def upsert_user(session: AsyncSession, tg: TgUser, chat_id: int) -> User:
    """Создаёт или обновляет профиль (имя/username меняются со временем)."""
    stmt = (
        insert(User)
        .values(
            telegram_id=tg.id,
            chat_id=chat_id,
            first_name=tg.first_name,
            last_name=tg.last_name,
            username=tg.username,
        )
        .on_conflict_do_update(
            index_elements=[User.telegram_id],
            set_={
                "chat_id": chat_id,
                "first_name": tg.first_name,
                "last_name": tg.last_name,
                "username": tg.username,
                "updated_at": datetime.now(UTC),
            },
        )
        .returning(User.id)
    )
    user_id = (await session.execute(stmt)).scalar_one()
    user = await session.get(User, user_id, populate_existing=True)
    assert user is not None
    return user


async def get_user_by_tg(session: AsyncSession, telegram_id: int) -> User | None:
    return (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()


class ForeignContactError(ValueError):
    pass


async def save_own_contact(
    session: AsyncSession, tg: TgUser, chat_id: int, contact_user_id: int | None, phone: str
) -> tuple[User, bool]:
    """Сохраняет номер, только если контакт принадлежит самому отправителю.

    Возвращает (user, is_first_registration).
    """
    if contact_user_id is not None and contact_user_id != tg.id:
        raise ForeignContactError
    user = await upsert_user(session, tg, chat_id)
    now = datetime.now(UTC)
    first = user.registered_at is None
    user.phone = normalize_phone(phone)
    user.phone_shared = True
    user.phone_refused = False
    user.phone_updated_at = now
    if first:
        user.registered_at = now
    await session.flush()
    return user, first


async def refuse_phone(session: AsyncSession, tg: TgUser, chat_id: int) -> User:
    """Отмечает отказ от предоставления номера телефона."""
    user = await upsert_user(session, tg, chat_id)
    user.phone_refused = True
    if user.registered_at is None:
        user.registered_at = datetime.now(UTC)
    await session.flush()
    return user

