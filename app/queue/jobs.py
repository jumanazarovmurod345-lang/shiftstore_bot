"""Надёжная очередь задач в PostgreSQL.

* Захват задачи — ``FOR UPDATE SKIP LOCKED``: одну задачу не обработают два воркера.
* Аренда (lease) с продлением heartbeat'ом: если процесс упал, задача станет
  доступна снова после ``locked_until`` и будет выполнена повторно.
* Ограниченное число попыток с экспоненциальным backoff и jitter.
* ``dedup_key`` — идемпотентная постановка задач.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

OnConflict = Literal["ignore", "reschedule", "rearm", "upsert"]


@dataclass(slots=True)
class ClaimedJob:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


async def enqueue(
    session: AsyncSession,
    kind: str,
    payload: dict[str, Any],
    *,
    dedup_key: str | None = None,
    delay_seconds: float = 0.0,
    max_attempts: int = 8,
    on_conflict: OnConflict = "ignore",
) -> bool:
    """Ставит задачу в очередь (в рамках транзакции вызывающего). True — если задача
    создана или переактивирована.

    on_conflict (если задача с таким dedup_key уже есть):
      * ignore     — ничего не делать;
      * reschedule — для ожидающей задачи сдвинуть run_at (debounce) и обновить payload;
      * rearm      — завершённую/упавшую задачу снова сделать pending;
      * upsert     — reschedule для ожидающей + rearm для завершённой.
    """
    params = {
        "kind": kind,
        "payload": json.dumps(payload, ensure_ascii=False, default=str),
        "dedup_key": dedup_key,
        "delay": delay_seconds,
        "max_attempts": max_attempts,
    }
    base = """
        INSERT INTO jobs (kind, payload, dedup_key, status, attempts, max_attempts, run_at)
        VALUES (:kind, CAST(:payload AS JSONB), :dedup_key, 'pending', 0, :max_attempts,
                now() + make_interval(secs => :delay))
    """
    if dedup_key is None or on_conflict == "ignore":
        sql = base + " ON CONFLICT (dedup_key) DO NOTHING RETURNING id"
    elif on_conflict == "reschedule":
        sql = base + """
            ON CONFLICT (dedup_key) DO UPDATE
               SET run_at = EXCLUDED.run_at, payload = EXCLUDED.payload, updated_at = now()
             WHERE jobs.status = 'pending'
            RETURNING id
        """
    else:  # rearm
        sql = base + """
            ON CONFLICT (dedup_key) DO UPDATE
               SET status = 'pending', attempts = 0, run_at = EXCLUDED.run_at,
                   payload = EXCLUDED.payload, last_error = NULL, finished_at = NULL,
                   updated_at = now()
             WHERE jobs.status IN ('done', 'failed')
            RETURNING id
        """
    if on_conflict == "upsert" and dedup_key is not None:
        sql = base + """
            ON CONFLICT (dedup_key) DO UPDATE
               SET run_at = EXCLUDED.run_at,
                   payload = CASE WHEN jobs.status = 'pending'
                                   AND (jobs.payload->>'created') = 'true'
                                  THEN EXCLUDED.payload || '{"created": true}'::jsonb
                                  ELSE EXCLUDED.payload END,
                   attempts = CASE WHEN jobs.status = 'pending' THEN jobs.attempts ELSE 0 END,
                   status = 'pending', last_error = NULL, finished_at = NULL, updated_at = now()
             WHERE jobs.status IN ('pending', 'done', 'failed')
            RETURNING id
        """
    row = (await session.execute(text(sql), params)).first()
    return row is not None


async def claim(session: AsyncSession, worker_id: str, lease_seconds: int) -> ClaimedJob | None:
    row = (
        await session.execute(
            text(
                """
                UPDATE jobs
                   SET status = 'running', attempts = attempts + 1, locked_by = :w,
                       locked_until = now() + make_interval(secs => :lease), updated_at = now()
                 WHERE id = (
                        SELECT id FROM jobs
                         WHERE (status = 'pending' AND run_at <= now())
                            OR (status = 'running' AND locked_until < now())
                         ORDER BY run_at, id
                         LIMIT 1
                         FOR UPDATE SKIP LOCKED)
                RETURNING id, kind, payload, attempts, max_attempts
                """
            ),
            {"w": worker_id, "lease": lease_seconds},
        )
    ).first()
    await session.commit()
    if row is None:
        return None
    payload = row.payload if isinstance(row.payload, dict) else json.loads(row.payload)
    return ClaimedJob(row.id, row.kind, payload, row.attempts, row.max_attempts)


async def heartbeat(session: AsyncSession, job_id: int, worker_id: str, lease_seconds: int) -> bool:
    res = await session.execute(
        text(
            "UPDATE jobs SET locked_until = now() + make_interval(secs => :lease) "
            "WHERE id = :id AND locked_by = :w AND status = 'running'"
        ),
        {"id": job_id, "w": worker_id, "lease": lease_seconds},
    )
    await session.commit()
    return res.rowcount == 1


async def complete(session: AsyncSession, job_id: int, worker_id: str) -> bool:
    res = await session.execute(
        text(
            "UPDATE jobs SET status = 'done', finished_at = now(), locked_by = NULL, "
            "locked_until = NULL, updated_at = now() "
            "WHERE id = :id AND locked_by = :w AND status = 'running'"
        ),
        {"id": job_id, "w": worker_id},
    )
    await session.commit()
    return res.rowcount == 1


def backoff_seconds(attempt: int, base: float = 2.0, cap: float = 900.0) -> float:
    delay = min(cap, base * (2 ** max(0, attempt - 1)))
    return delay * random.uniform(0.8, 1.2)


async def fail(
    session: AsyncSession,
    job: ClaimedJob,
    worker_id: str,
    error: str,
    *,
    retry_after: float | None = None,
    permanent: bool = False,
) -> Literal["retry", "failed"]:
    final = permanent or job.attempts >= job.max_attempts
    delay = retry_after if retry_after is not None else backoff_seconds(job.attempts)
    await session.execute(
        text(
            """
            UPDATE jobs
               SET status = :status, last_error = :err, locked_by = NULL, locked_until = NULL,
                   run_at = now() + make_interval(secs => :delay),
                   finished_at = CASE WHEN :final THEN now() ELSE NULL END,
                   updated_at = now()
             WHERE id = :id AND locked_by = :w
            """
        ),
        {
            "status": "failed" if final else "pending",
            "err": error[:2000],
            "delay": delay,
            "final": final,
            "id": job.id,
            "w": worker_id,
        },
    )
    await session.commit()
    return "failed" if final else "retry"


async def release(session: AsyncSession, job: ClaimedJob, worker_id: str) -> None:
    """Вернуть задачу в очередь без штрафа (graceful shutdown)."""
    await session.execute(
        text(
            "UPDATE jobs SET status = 'pending', attempts = GREATEST(attempts - 1, 0), "
            "locked_by = NULL, locked_until = NULL, run_at = now(), updated_at = now() "
            "WHERE id = :id AND locked_by = :w AND status = 'running'"
        ),
        {"id": job.id, "w": worker_id},
    )
    await session.commit()


async def stats(session: AsyncSession) -> dict[str, int]:
    rows = (await session.execute(text("SELECT status, count(*) FROM jobs GROUP BY status"))).all()
    return {r[0]: int(r[1]) for r in rows}

