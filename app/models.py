from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum

from sqlmodel import Field, SQLModel


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp (SQLModel stores and returns UTC)."""
    return datetime.now(timezone.utc)


class JobKind(StrEnum):
    PRINT = "print"
    SCAN = "scan"


class JobStatus(StrEnum):
    OPEN = "open"            # scan session still collecting pages
    QUEUED = "queued"
    RUNNING = "running"
    PRINTING = "printing"    # handed to CUPS, waiting for completion
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_STATUSES = {JobStatus.OPEN, JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.PRINTING}
FINAL_STATUSES = {JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED}


class Job(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    kind: str = Field(index=True)
    status: str = Field(default=JobStatus.QUEUED, index=True)
    user: str = "admin"

    device: str                       # CUPS queue name or SANE device id
    device_label: str = ""

    filename: str | None = None       # original upload name / produced PDF name
    file_path: str | None = None      # absolute path of the stored file
    file_size: int | None = None

    pages: int | None = None          # pages per copy (after page range)
    copies: int = 1
    page_range: str | None = None
    duplex: str | None = None         # IPP "sides" value
    color_mode: str | None = None     # print: color/monochrome, scan: SANE mode
    dpi: int | None = None

    cups_job_id: int | None = None
    error: str | None = None

    created_at: datetime = Field(default_factory=utcnow, index=True)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    files_purged: bool = False

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES


class Role(StrEnum):
    ADMIN = "admin"
    USER = "user"


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    role: str = Role.USER
    email: str = ""
    paperless_token: str = ""
    allowed_printers: str = ""        # comma-separated CUPS queue names; empty = all
    api_token_hash: str = Field(default="", index=True)
    session_epoch: int = 0            # bump to invalidate existing sessions
    quota_pages: int = 0              # printed pages per period; 0 = unlimited
    quota_period: str = "month"       # month | week
    active: bool = True
    created_at: datetime = Field(default_factory=utcnow)
    last_login_at: datetime | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    @property
    def printer_list(self) -> list[str]:
        return [p.strip() for p in self.allowed_printers.split(",") if p.strip()]

    def may_use_printer(self, name: str) -> bool:
        allowed = self.printer_list
        return not allowed or name in allowed


class DeliveryKind(StrEnum):
    EMAIL = "email"
    PAPERLESS = "paperless"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    DONE = "done"
    FAILED = "failed"


class Delivery(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    job_id: str = Field(foreign_key="job.id", index=True)
    kind: str
    target: str = ""                  # email address or Paperless URL
    options: str = "{}"               # JSON: title, tags, correspondent, document_type
    status: str = DeliveryStatus.PENDING
    error: str | None = None
    result: str | None = None         # human-readable outcome
    result_url: str | None = None     # e.g. link to the Paperless document
    attempts: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None


class SupplySample(SQLModel, table=True):
    """Toner/ink level of one printer marker at one point in time."""

    __tablename__ = "supply_sample"

    id: int | None = Field(default=None, primary_key=True)
    printer: str = Field(index=True)
    marker: str
    color: str = ""
    level: int                        # percent; negative = unknown
    sampled_at: datetime = Field(default_factory=utcnow, index=True)


class SchemaVersion(SQLModel, table=True):
    __tablename__ = "schema_version"

    id: int = Field(default=1, primary_key=True)
    version: int = 0


class ScanPage(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    job_id: str = Field(foreign_key="job.id", index=True)
    position: int
    path: str
    thumb_path: str
    created_at: datetime = Field(default_factory=utcnow)
