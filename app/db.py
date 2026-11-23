from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, func, select

from .models import Job, JobStatus, ScanPage


class Database:
    """Small repository over SQLite. All methods are short and synchronous."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            f"sqlite:///{path}", connect_args={"check_same_thread": False}
        )

        @event.listens_for(self.engine, "connect")
        def _pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        SQLModel.metadata.create_all(self.engine)
        # Serialise writes; SQLite allows one writer and this avoids "database is locked".
        self._write_lock = threading.Lock()

    def ping(self) -> bool:
        with Session(self.engine) as s:
            s.exec(select(1)).one()
        return True

    # --- jobs ---------------------------------------------------------------

    def add_job(self, job: Job) -> Job:
        with self._write_lock, Session(self.engine, expire_on_commit=False) as s:
            s.add(job)
            s.commit()
            return job

    def get_job(self, job_id: str) -> Job | None:
        with Session(self.engine, expire_on_commit=False) as s:
            return s.get(Job, job_id)

    def update_job(self, job_id: str, **fields: Any) -> Job:
        with self._write_lock, Session(self.engine, expire_on_commit=False) as s:
            job = s.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            for key, value in fields.items():
                setattr(job, key, value)
            s.add(job)
            s.commit()
            return job

    def list_jobs(
        self, kind: str | None = None, status: str | None = None, limit: int = 200
    ) -> list[Job]:
        stmt = select(Job).order_by(Job.created_at.desc()).limit(limit)
        if kind:
            stmt = stmt.where(Job.kind == kind)
        if status:
            stmt = stmt.where(Job.status == status)
        with Session(self.engine, expire_on_commit=False) as s:
            return list(s.exec(stmt))

    def jobs_with_status(self, *statuses: str) -> list[Job]:
        with Session(self.engine, expire_on_commit=False) as s:
            return list(s.exec(select(Job).where(Job.status.in_(statuses))))

    def jobs_to_purge(self, finished_before: datetime) -> list[Job]:
        stmt = select(Job).where(
            Job.files_purged == False,  # noqa: E712
            Job.finished_at != None,  # noqa: E711
            Job.finished_at < finished_before,
        )
        with Session(self.engine, expire_on_commit=False) as s:
            return list(s.exec(stmt))

    def stale_scan_sessions(self, created_before: datetime) -> list[Job]:
        stmt = select(Job).where(Job.status == JobStatus.OPEN, Job.created_at < created_before)
        with Session(self.engine, expire_on_commit=False) as s:
            return list(s.exec(stmt))

    # --- scan pages ---------------------------------------------------------

    def list_pages(self, job_id: str) -> list[ScanPage]:
        stmt = select(ScanPage).where(ScanPage.job_id == job_id).order_by(ScanPage.position)
        with Session(self.engine, expire_on_commit=False) as s:
            return list(s.exec(stmt))

    def get_page(self, job_id: str, page_id: str) -> ScanPage | None:
        with Session(self.engine, expire_on_commit=False) as s:
            page = s.get(ScanPage, page_id)
            return page if page and page.job_id == job_id else None

    def add_page(self, job_id: str, path: str, thumb_path: str) -> ScanPage:
        with self._write_lock, Session(self.engine, expire_on_commit=False) as s:
            last = s.exec(
                select(func.max(ScanPage.position)).where(ScanPage.job_id == job_id)
            ).one()
            page = ScanPage(
                job_id=job_id, position=(last or 0) + 1, path=path, thumb_path=thumb_path
            )
            s.add(page)
            s.commit()
            return page

    def update_page(self, page_id: str, **fields: Any) -> ScanPage:
        with self._write_lock, Session(self.engine, expire_on_commit=False) as s:
            page = s.get(ScanPage, page_id)
            if page is None:
                raise KeyError(page_id)
            for key, value in fields.items():
                setattr(page, key, value)
            s.add(page)
            s.commit()
            return page

    def delete_page(self, page_id: str) -> None:
        with self._write_lock, Session(self.engine) as s:
            page = s.get(ScanPage, page_id)
            if page is not None:
                s.delete(page)
                s.commit()
