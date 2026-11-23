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


class ScanPage(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    job_id: str = Field(foreign_key="job.id", index=True)
    position: int
    path: str
    thumb_path: str
    created_at: datetime = Field(default_factory=utcnow)
