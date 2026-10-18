"""Пул воркеров фоновой очереди с ограниченным параллелизмом и graceful shutdown."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import uuid

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.context import AppContext
from app.queue import jobs
from app.tasks import FINAL_FAILURE_HOOKS, HANDLERS, JobHandler, PermanentJobError

log = logging.getLogger(__name__)

RETENTION_DAYS = 14


class Worker:
    def __init__(
        self,
        ctx: AppContext,
        handlers: dict[str, JobHandler] | None = None,
        concurrency: int | None = None,
        lease_seconds: int | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.ctx = ctx
        self.handlers = handlers or HANDLERS
        self.concurrency = concurrency or ctx.settings.worker_concurrency
        self.lease = lease_seconds or ctx.settings.job_lease_seconds
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self._stopping = asyncio.Event()
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    def wake(self) -> None:
        self._wake.set()

    async def start(self) -> None:
        self._stopping.clear()
        for i in range(self.concurrency):
            self._tasks.append(asyncio.create_task(self._loop(i), name=f"worker-{i}"))
        self._tasks.append(asyncio.create_task(self._maintenance(), name="worker-maintenance"))
        log.info("worker started", extra={"worker_id": self.worker_id, "concurrency": self.concurrency})

    async def stop(self, grace_seconds: float) -> None:
        self._stopping.set()
        self._wake.set()
        if not self._tasks:
            return
        _, pending = await asyncio.wait(self._tasks, timeout=grace_seconds)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        log.info("worker stopped", extra={"cancelled": len(pending)})

    async def _loop(self, slot: int) -> None:
        backoff = 1.0
        while not self._stopping.is_set():
            try:
                did_work = await self.run_once()
                backoff = 1.0
            except SQLAlchemyError:
                log.warning("queue db error, backing off", extra={"slot": slot, "delay": backoff}, exc_info=True)
                await self._sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            if not did_work:
                self._wake.clear()
                await self._sleep(1.0)

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)

    async def run_once(self) -> bool:
        """Захватывает и выполняет одну задачу. False — очередь пуста."""
        async with self.ctx.sessionmaker() as s:
            job = await jobs.claim(s, self.worker_id, self.lease)
        if job is None:
            return False
        await self._execute(job)
        return True

    async def _execute(self, job: jobs.ClaimedJob) -> None:
        handler = self.handlers.get(job.kind)
        hb = asyncio.create_task(self._heartbeat(job.id))
        log_extra = {"job_id": job.id, "kind": job.kind, "attempt": job.attempts}
        try:
            if handler is None:
                raise PermanentJobError(f"unknown job kind {job.kind}")
            await handler(self.ctx, job)
        except asyncio.CancelledError:
            hb.cancel()
            with contextlib.suppress(Exception):
                async with self.ctx.sessionmaker() as s:
                    await jobs.release(s, job, self.worker_id)
            log.warning("job released on shutdown", extra=log_extra)
            raise
        except TelegramRetryAfter as exc:
            await self._fail(job, f"retry_after {exc.retry_after}", retry_after=float(exc.retry_after) + 1)
        except (PermanentJobError, TelegramForbiddenError) as exc:
            await self._fail(job, f"{type(exc).__name__}: {exc}", permanent=True)
        except Exception as exc:  # noqa: BLE001 - любая ошибка -> повтор с backoff
            await self._fail(job, f"{type(exc).__name__}: {exc}")
        else:
            async with self.ctx.sessionmaker() as s:
                ok = await jobs.complete(s, job.id, self.worker_id)
            if not ok:
                log.warning("job lease lost before completion", extra=log_extra)
        finally:
            hb.cancel()

    async def _fail(self, job: jobs.ClaimedJob, error: str, **kw: object) -> None:
        async with self.ctx.sessionmaker() as s:
            result = await jobs.fail(s, job, self.worker_id, error, **kw)  # type: ignore[arg-type]
        level = logging.ERROR if result == "failed" else logging.WARNING
        log.log(level, "job failed", extra={"job_id": job.id, "kind": job.kind, "attempt": job.attempts,
                                            "result": result, "error": error[:300]})
        if result == "failed" and job.kind in FINAL_FAILURE_HOOKS:
            try:
                await FINAL_FAILURE_HOOKS[job.kind](self.ctx, job, error)
            except Exception:  # noqa: BLE001
                log.exception("final failure hook error", extra={"job_id": job.id})

    async def _heartbeat(self, job_id: int) -> None:
        interval = max(5.0, self.lease / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                async with self.ctx.sessionmaker() as s:
                    await jobs.heartbeat(s, job_id, self.worker_id, self.lease)
            except SQLAlchemyError:
                log.warning("heartbeat failed", extra={"job_id": job_id})

    async def _maintenance(self) -> None:
        """Удаление старых завершённых задач и записей дедупликации апдейтов."""
        while not self._stopping.is_set():
            try:
                async with self.ctx.sessionmaker() as s:
                    await s.execute(
                        text(
                            "DELETE FROM jobs WHERE status = 'done' "
                            "AND finished_at < now() - make_interval(days => :d)"
                        ),
                        {"d": RETENTION_DAYS},
                    )
                    await s.execute(
                        text(
                            "DELETE FROM processed_updates "
                            "WHERE received_at < now() - make_interval(days => :d)"
                        ),
                        {"d": RETENTION_DAYS},
                    )
                    await s.commit()
            except SQLAlchemyError:
                log.warning("maintenance failed", exc_info=True)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=3600)
