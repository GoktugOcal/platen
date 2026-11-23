from __future__ import annotations

import asyncio

from app.db import Database
from app.devices import FakePrinterBackend, FakeScannerBackend
from app.jobs import JobCancelled, JobRunner
from app.models import Job, JobStatus

from .conftest import make_settings


def _runner(tmp_path, pipelines):
    settings = make_settings(tmp_path, max_concurrent_jobs=2)
    db = Database(settings.db_path)
    return db, JobRunner(db, settings, FakePrinterBackend(), FakeScannerBackend(), pipelines)


def test_success_failure_and_cancel(tmp_path):
    calls = []

    async def ok(ctx):
        calls.append(ctx.job_id)
        ctx.update(pages=7)

    async def boom(ctx):
        raise RuntimeError("paper jam")

    async def cancel(ctx):
        raise JobCancelled()

    db, runner = _runner(tmp_path, {"print": [ok], "scan": [ok, boom]})

    async def main():
        runner.start()
        a = db.add_job(Job(kind="print", device="p"))
        b = db.add_job(Job(kind="scan", device="s"))
        c = db.add_job(Job(kind="print", device="p"))
        runner.submit(a.id)
        runner.submit(b.id)
        runner.submit(c.id, [cancel])
        await runner.wait_idle()
        return a.id, b.id, c.id

    a, b, c = asyncio.run(main())
    job_a, job_b, job_c = db.get_job(a), db.get_job(b), db.get_job(c)
    assert job_a.status == JobStatus.DONE and job_a.pages == 7
    assert job_a.started_at and job_a.finished_at
    assert job_b.status == JobStatus.FAILED and job_b.error == "paper jam"
    assert job_c.status == JobStatus.CANCELLED
    assert calls == [a, b]


def test_cancelled_before_start_is_skipped(tmp_path):
    ran = []

    async def step(ctx):
        ran.append(ctx.job_id)

    db, runner = _runner(tmp_path, {"print": [step]})

    async def main():
        runner.start()
        job = db.add_job(Job(kind="print", device="p", status=JobStatus.CANCELLED))
        runner.submit(job.id)
        await runner.wait_idle()

    asyncio.run(main())
    assert ran == []


def test_device_lock_serialises(tmp_path):
    db, runner = _runner(tmp_path, {})
    active = []
    peak = []

    async def use(device):
        async with runner.device_lock(device):
            active.append(device)
            peak.append(active.count("scanner"))
            await asyncio.sleep(0.01)
            active.remove(device)

    async def main():
        await asyncio.gather(*(use("scanner") for _ in range(3)), use("other"))

    asyncio.run(main())
    assert max(peak) == 1


def test_recover_after_restart(tmp_path):
    resumed = []

    async def resume(ctx):
        resumed.append(ctx.job_id)

    async def full(ctx):
        resumed.append("full:" + ctx.job_id)

    db, runner = _runner(tmp_path, {"print": [full]})
    queued = db.add_job(Job(kind="print", device="p", status=JobStatus.QUEUED))
    printing = db.add_job(Job(kind="print", device="p", status=JobStatus.PRINTING, cups_job_id=4))
    running = db.add_job(Job(kind="print", device="p", status=JobStatus.RUNNING))

    async def main():
        runner.start()
        runner.recover([resume])
        await runner.wait_idle()

    asyncio.run(main())
    assert sorted(resumed) == sorted([f"full:{queued.id}", printing.id])
    assert db.get_job(running.id).status == JobStatus.FAILED
    assert db.get_job(printing.id).status == JobStatus.DONE
