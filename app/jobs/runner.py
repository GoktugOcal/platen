"""In-process job runner.

Every print or scan is a Job that runs a list of async steps. Steps only talk
to the JobContext, so the runner can later be swapped for arq/Redis without
changing them, and new steps (OCR, email, Paperless) slot into a pipeline list.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings
from ..db import Database
from ..devices import PrinterBackend, ScannerBackend
from ..models import FINAL_STATUSES, Job, JobStatus, utcnow

log = logging.getLogger(__name__)


class JobCancelled(Exception):
    pass


@dataclass
class JobContext:
    job_id: str
    db: Database
    settings: Settings
    printers: PrinterBackend
    scanners: ScannerBackend
    # Optional httpx transports per integration ("paperless", "gotenberg"); tests inject mocks.
    transports: dict[str, Any] = field(default_factory=dict)

    def job(self) -> Job:
        job = self.db.get_job(self.job_id)
        if job is None:
            raise KeyError(self.job_id)
        return job

    def update(self, **fields: Any) -> Job:
        return self.db.update_job(self.job_id, **fields)

    @property
    def job_dir(self) -> Path:
        return self.settings.jobs_dir / self.job_id


Step = Callable[[JobContext], Awaitable[None]]


class JobRunner:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        printers: PrinterBackend,
        scanners: ScannerBackend,
        pipelines: dict[str, list[Step]],
        transports: dict[str, Any] | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.printers = printers
        self.scanners = scanners
        self.pipelines = pipelines
        self.transports = transports or {}
        self._sem = asyncio.Semaphore(settings.max_concurrent_jobs)
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._tasks: set[asyncio.Task] = set()
        self._running = False

    @property
    def alive(self) -> bool:
        return self._running

    def device_lock(self, device: str) -> asyncio.Lock:
        """One operation at a time per physical device (e.g. a flatbed scan)."""
        return self._locks[device]

    def context(self, job_id: str) -> JobContext:
        return JobContext(job_id, self.db, self.settings, self.printers, self.scanners,
                          self.transports)

    def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def submit(self, job_id: str, steps: list[Step] | None = None) -> asyncio.Task:
        task = asyncio.create_task(self._run(job_id, steps), name=f"job-{job_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def spawn(self, coro: Awaitable[None], name: str) -> asyncio.Task:
        """Run extra background work (e.g. a delivery retry) tracked for shutdown."""
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _run(self, job_id: str, steps: list[Step] | None) -> None:
        async with self._sem:
            job = self.db.get_job(job_id)
            if job is None or job.status in FINAL_STATUSES:
                return
            steps = steps if steps is not None else self.pipelines[job.kind]
            if job.status == JobStatus.QUEUED:
                self.db.update_job(
                    job_id, status=JobStatus.RUNNING, started_at=job.started_at or utcnow()
                )
            ctx = self.context(job_id)
            try:
                for step in steps:
                    if ctx.job().status == JobStatus.CANCELLED:
                        raise JobCancelled()
                    log.debug("job %s: %s", job_id, step.__name__)
                    await step(ctx)
            except JobCancelled:
                self.db.update_job(job_id, status=JobStatus.CANCELLED, finished_at=utcnow())
                log.info("job %s cancelled", job_id)
            except asyncio.CancelledError:
                raise  # shutdown: leave state for recover() on next start
            except Exception as exc:  # noqa: BLE001 - recorded on the job
                log.exception("job %s failed", job_id)
                self.db.update_job(
                    job_id, status=JobStatus.FAILED, error=str(exc)[:1000] or type(exc).__name__,
                    finished_at=utcnow(),
                )
            else:
                if ctx.job().status not in FINAL_STATUSES:
                    self.db.update_job(job_id, status=JobStatus.DONE, finished_at=utcnow())
                    log.info("job %s done", job_id)

    def recover(self, resume_print: list[Step]) -> None:
        """Pick up jobs left behind by a restart."""
        for job in self.db.jobs_with_status(JobStatus.QUEUED):
            self.submit(job.id)
        for job in self.db.jobs_with_status(JobStatus.PRINTING):
            if job.cups_job_id is not None:
                self.submit(job.id, resume_print)
        for job in self.db.jobs_with_status(JobStatus.RUNNING):
            self.db.update_job(
                job.id, status=JobStatus.FAILED, error="interrupted by a restart",
                finished_at=utcnow(),
            )
