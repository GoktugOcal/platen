from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Connection, make_url
from sqlmodel import Session, SQLModel, create_engine, func, select

from .models import (
    Delivery,
    Job,
    JobStatus,
    Role,
    ScanPage,
    SchemaVersion,
    User,
)

# Ordered schema migrations for changes create_all() cannot do (new columns,
# renames). Index i upgrades version i -> i+1. Tables are created by create_all.
MIGRATIONS: list[Callable[[Connection], None]] = []


class Database:
    """Small repository over SQLite (default) or Postgres. Methods are short and sync."""

    def __init__(self, url: str):
        parsed = make_url(url)
        self.is_sqlite = parsed.get_backend_name() == "sqlite"
        if self.is_sqlite:
            if parsed.database:
                Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
            self.engine = create_engine(url, connect_args={"check_same_thread": False})

            @event.listens_for(self.engine, "connect")
            def _pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        else:
            self.engine = create_engine(url, pool_pre_ping=True)

        SQLModel.metadata.create_all(self.engine)
        self._migrate()
        # Serialise writes; SQLite allows one writer and this avoids "database is locked".
        self._write_lock = threading.Lock()

    def _migrate(self) -> None:
        with Session(self.engine) as s:
            row = s.get(SchemaVersion, 1)
            if row is None:
                # Fresh database (or v0.1, which had no new columns to add).
                s.add(SchemaVersion(id=1, version=len(MIGRATIONS)))
                s.commit()
                return
            current = row.version
        for version in range(current, len(MIGRATIONS)):
            with self.engine.begin() as conn:
                MIGRATIONS[version](conn)
            with Session(self.engine) as s:
                row = s.get(SchemaVersion, 1)
                row.version = version + 1
                s.add(row)
                s.commit()

    def ping(self) -> bool:
        with Session(self.engine) as s:
            s.exec(select(1)).one()
        return True

    def _session(self) -> Session:
        return Session(self.engine, expire_on_commit=False)

    def _update(self, model, key, fields: dict[str, Any]):
        with self._write_lock, self._session() as s:
            obj = s.get(model, key)
            if obj is None:
                raise KeyError(key)
            for name, value in fields.items():
                setattr(obj, name, value)
            s.add(obj)
            s.commit()
            return obj

    def _add(self, obj):
        with self._write_lock, self._session() as s:
            s.add(obj)
            s.commit()
            return obj

    # --- jobs ---------------------------------------------------------------

    def add_job(self, job: Job) -> Job:
        return self._add(job)

    def get_job(self, job_id: str) -> Job | None:
        with self._session() as s:
            return s.get(Job, job_id)

    def update_job(self, job_id: str, **fields: Any) -> Job:
        return self._update(Job, job_id, fields)

    def list_jobs(
        self,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 200,
        user: str | None = None,
    ) -> list[Job]:
        stmt = select(Job).order_by(Job.created_at.desc()).limit(limit)
        if kind:
            stmt = stmt.where(Job.kind == kind)
        if status:
            stmt = stmt.where(Job.status == status)
        if user:
            stmt = stmt.where(Job.user == user)
        with self._session() as s:
            return list(s.exec(stmt))

    def jobs_with_status(self, *statuses: str) -> list[Job]:
        with self._session() as s:
            return list(s.exec(select(Job).where(Job.status.in_(statuses))))

    def jobs_to_purge(self, finished_before: datetime) -> list[Job]:
        stmt = select(Job).where(
            Job.files_purged == False,  # noqa: E712
            Job.finished_at != None,  # noqa: E711
            Job.finished_at < finished_before,
        )
        with self._session() as s:
            return list(s.exec(stmt))

    def stale_scan_sessions(self, created_before: datetime) -> list[Job]:
        stmt = select(Job).where(Job.status == JobStatus.OPEN, Job.created_at < created_before)
        with self._session() as s:
            return list(s.exec(stmt))

    # --- scan pages ---------------------------------------------------------

    def list_pages(self, job_id: str) -> list[ScanPage]:
        stmt = select(ScanPage).where(ScanPage.job_id == job_id).order_by(ScanPage.position)
        with self._session() as s:
            return list(s.exec(stmt))

    def get_page(self, job_id: str, page_id: str) -> ScanPage | None:
        with self._session() as s:
            page = s.get(ScanPage, page_id)
            return page if page and page.job_id == job_id else None

    def add_page(self, job_id: str, path: str, thumb_path: str) -> ScanPage:
        with self._write_lock, self._session() as s:
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
        return self._update(ScanPage, page_id, fields)

    def delete_page(self, page_id: str) -> None:
        with self._write_lock, self._session() as s:
            page = s.get(ScanPage, page_id)
            if page is not None:
                s.delete(page)
                s.commit()

    # --- users --------------------------------------------------------------

    def count_users(self) -> int:
        with self._session() as s:
            return s.exec(select(func.count()).select_from(User)).one()

    def count_active_admins(self) -> int:
        stmt = select(func.count()).select_from(User).where(
            User.role == Role.ADMIN, User.active == True  # noqa: E712
        )
        with self._session() as s:
            return s.exec(stmt).one()

    def add_user(self, user: User) -> User:
        return self._add(user)

    def get_user(self, user_id: int) -> User | None:
        with self._session() as s:
            return s.get(User, user_id)

    def get_user_by_name(self, username: str) -> User | None:
        with self._session() as s:
            return s.exec(select(User).where(User.username == username)).first()

    def get_user_by_token_hash(self, token_hash: str) -> User | None:
        if not token_hash:
            return None
        with self._session() as s:
            return s.exec(select(User).where(User.api_token_hash == token_hash)).first()

    def list_users(self) -> list[User]:
        with self._session() as s:
            return list(s.exec(select(User).order_by(User.username)))

    def update_user(self, user_id: int, **fields: Any) -> User:
        return self._update(User, user_id, fields)

    # --- deliveries ---------------------------------------------------------

    def add_delivery(self, delivery: Delivery) -> Delivery:
        return self._add(delivery)

    def get_delivery(self, delivery_id: str) -> Delivery | None:
        with self._session() as s:
            return s.get(Delivery, delivery_id)

    def list_deliveries(self, job_id: str) -> list[Delivery]:
        stmt = select(Delivery).where(Delivery.job_id == job_id).order_by(Delivery.created_at)
        with self._session() as s:
            return list(s.exec(stmt))

    def update_delivery(self, delivery_id: str, **fields: Any) -> Delivery:
        return self._update(Delivery, delivery_id, fields)
