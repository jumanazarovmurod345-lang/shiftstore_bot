"""Заявки администратору: создание, оформление, статусы."""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.bot import texts
from app.db.models import Request, User
from app.queue.jobs import enqueue

NOTIFY_MAX_ATTEMPTS = 40  # с backoff до 15 минут ≈ 8+ часов повторов


async def create_request(
    session: AsyncSession,
    *,
    user_id: int,
    source_chat_id: int,
    source_message_id: int,
    photo_file_id: str,
    photo_file_unique_id: str | None,
    photo_is_document: bool,
    client_caption: str | None,
    reason: str,
    candidates: list[dict[str, Any]] | None,
) -> int:
    """Идемпотентно создаёт заявку (одна на сообщение клиента) и задачу уведомления."""
    res = await session.execute(
        insert(Request)
        .values(
            user_id=user_id,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            photo_file_id=photo_file_id,
            photo_file_unique_id=photo_file_unique_id,
            photo_is_document=photo_is_document,
            client_caption=client_caption,
            reason=reason,
            candidates=candidates,
        )
        .on_conflict_do_nothing(constraint="uq_requests_source_message")
        .returning(Request.id)
    )
    row = res.first()
    if row is None:
        request_id = (
            await session.execute(
                select(Request.id).where(
                    Request.source_chat_id == source_chat_id,
                    Request.source_message_id == source_message_id,
                )
            )
        ).scalar_one()
    else:
        request_id = row[0]
    await enqueue(
        session,
        "notify_admin",
        {"request_id": request_id},
        dedup_key=f"notify:{request_id}",
        max_attempts=NOTIFY_MAX_ATTEMPTS,
    )
    return request_id


async def get_request(session: AsyncSession, request_id: int, lock: bool = False) -> Request | None:
    stmt = select(Request).options(joinedload(Request.user)).where(Request.id == request_id)
    if lock:
        stmt = stmt.with_for_update(of=Request)
    return (await session.execute(stmt)).scalar_one_or_none()


def profile_link(user: User) -> str:
    links = [f'<a href="tg://user?id={user.telegram_id}">открыть профиль</a>']
    if user.username:
        links.append(f"https://t.me/{escape(user.username)}")
    return " · ".join(links)


def render_admin_text(req: Request, tz: ZoneInfo) -> str:
    u = req.user
    name_parts = [p for p in (u.first_name, u.last_name) if p]
    full_name = " ".join(name_parts) if name_parts else "не указано"
    username_str = f"@{escape(u.username)}" if u.username else None
    has_voluntary_phone = bool(getattr(u, "phone_shared", False) and u.phone)
    phone_str = escape(u.phone) if has_voluntary_phone else None
    created = req.created_at.astimezone(tz).strftime("%d.%m.%Y %H:%M")

    lines = [
        "🔎 <b>Новая заявка на поиск товара</b>",
        "",
        "<b>Статус:</b> Нужно найти товар вручную",
        f"<b>Telegram user ID:</b> <code>{u.telegram_id}</code>",
        f"<b>Username:</b> {username_str if username_str else 'не указан'}",
        f"<b>Имя и фамилия:</b> {escape(full_name)}",
    ]
    if phone_str:
        lines.append(f"<b>Номер телефона:</b> {phone_str}")
    lines.append(f"<b>Дата и время заявки:</b> {created}")

    if not u.username and not has_voluntary_phone:
        lines.append("")
        lines.append("⚠️ <b>Нет username и номера телефона — связаться будет сложнее.</b>")

    if req.client_caption:
        lines += ["", f"💬 <b>Вопрос клиента:</b> {escape(req.client_caption[:1500])}"]
    if req.candidates:
        lines += ["", "<i>Похожие товары из каталога:</i>"]
        for c in req.candidates[:3]:
            name = escape(c.get("name") or "без названия")
            score = c.get("score", 0.0)
            lines.append(f"• #{c['product_id']} «{name}» — {score:.2f}")
    if req.assigned_admin_id:
        lines += ["", f"Ответственный: <code>{req.assigned_admin_id}</code>"]
    return "\n".join(lines)



def request_keyboard(req: Request) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="✍️ Ответить через бота", callback_data=f"rq:reply:{req.id}")]
    ]
    if req.status == "new":
        rows.append([InlineKeyboardButton(text="🟡 В работу", callback_data=f"rq:take:{req.id}")])
    if req.status != "closed":
        rows.append([InlineKeyboardButton(text="✅ Закрыть", callback_data=f"rq:close:{req.id}")])
    else:
        rows.append([InlineKeyboardButton(text="♻️ Открыть снова", callback_data=f"rq:reopen:{req.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def apply_status(req: Request, action: str, admin_id: int) -> bool:
    """Меняет статус по действию администратора. True — если что-то изменилось."""
    before = (req.status, req.assigned_admin_id)
    if action == "take" and req.status != "closed":
        req.status, req.assigned_admin_id = "in_progress", admin_id
    elif action == "close":
        req.status, req.closed_at = "closed", datetime.now(UTC)
        req.assigned_admin_id = req.assigned_admin_id or admin_id
    elif action == "reopen" and req.status == "closed":
        req.status, req.closed_at = "in_progress", None
    elif action == "reply" and req.status == "new":
        req.status, req.assigned_admin_id = "in_progress", admin_id
    return before != (req.status, req.assigned_admin_id)
