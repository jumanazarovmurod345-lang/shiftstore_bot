"""Приём входящих апдейтов: быстрое идемпотентное сохранение в PostgreSQL."""

from __future__ import annotations

from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ProcessedUpdate
from app.queue.jobs import enqueue

KNOWN_TYPES = (
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "callback_query",
    "my_chat_member",
)


async def store_update(session: AsyncSession, update: dict[str, Any]) -> bool:
    """Сохраняет апдейт и ставит задачу на обработку. False — если это повтор."""
    update_id = int(update["update_id"])
    update_type = next((k for k in update if k != "update_id"), None)
    res = await session.execute(
        insert(ProcessedUpdate)
        .values(update_id=update_id, update_type=update_type)
        .on_conflict_do_nothing(index_elements=[ProcessedUpdate.update_id])
        .returning(ProcessedUpdate.update_id)
    )
    if res.first() is None:
        await session.rollback()
        return False
    await enqueue(session, "update", {"update": update}, dedup_key=f"update:{update_id}", max_attempts=5)
    await session.commit()
    return True
