"""Usage statistics computed from the jobs table (no external metrics system)."""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .models import Job, JobKind, JobStatus, SupplySample

RANGES = {"7d": 7, "30d": 30, "90d": 90, "365d": 365}
RANGE_LABELS = {"7d": "Last 7 days", "30d": "Last 30 days", "90d": "Last 90 days",
                "365d": "Last 12 months", "mtd": "This month"}


def local_now() -> datetime:
    return datetime.now().astimezone()


def resolve_range(key: str, now: datetime | None = None) -> tuple[str, datetime, datetime]:
    """(key, start, end) in local time; unknown keys fall back to 30 days."""
    now = now or local_now()
    end = now
    if key == "mtd":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        key = key if key in RANGES else "30d"
        start = (now - timedelta(days=RANGES[key] - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
    return key, start, end


def bucket_unit(start: datetime, end: datetime) -> str:
    days = (end - start).days + 1
    return "day" if days <= 92 else "month"


def bucket_key(d: date, unit: str) -> date:
    return d if unit == "day" else d.replace(day=1)


def bucket_keys(start: datetime, end: datetime, unit: str) -> list[date]:
    keys, d = [], start.date()
    while d <= end.date():
        k = bucket_key(d, unit)
        if not keys or keys[-1] != k:
            keys.append(k)
        d += timedelta(days=1)
    return keys


def _local_date(ts: datetime) -> date:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone().date()


def sheets_for(job: Job) -> tuple[int, int]:
    """(printed page impressions, paper sheets) for a finished print job."""
    pages = job.pages or 0
    copies = max(job.copies, 1)
    impressions = pages * copies
    if job.duplex and job.duplex.startswith("two-sided"):
        return impressions, math.ceil(pages / 2) * copies
    return impressions, impressions


@dataclass
class Stats:
    range_key: str
    start: datetime
    end: datetime
    unit: str
    printed_pages: int = 0
    print_jobs: int = 0
    scanned_pages: int = 0
    scan_jobs: int = 0
    sheets: int = 0
    sheets_saved: int = 0
    duplex_pages: int = 0
    simplex_pages: int = 0
    color_pages: int = 0
    mono_pages: int = 0
    default_color_pages: int = 0
    failed_jobs: int = 0
    cancelled_jobs: int = 0
    finished_jobs: int = 0
    unknown_page_jobs: int = 0
    series: list[dict] = field(default_factory=list)
    by_printer: list[dict] = field(default_factory=list)
    by_user: list[dict] = field(default_factory=list)
    top_errors: list[dict] = field(default_factory=list)

    @property
    def failure_rate(self) -> float | None:
        return self.failed_jobs / self.finished_jobs if self.finished_jobs else None


def compute(jobs: list[Job], range_key: str, start: datetime, end: datetime) -> Stats:
    unit = bucket_unit(start, end)
    st = Stats(range_key, start, end, unit)
    buckets = {k: {"printed": 0, "scanned": 0} for k in bucket_keys(start, end, unit)}
    printers: dict[str, int] = defaultdict(int)
    users: dict[str, dict] = defaultdict(lambda: {"printed": 0, "scanned": 0, "jobs": 0})
    errors: Counter = Counter()

    for job in jobs:
        if job.status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
            st.finished_jobs += 1
        if job.status == JobStatus.FAILED:
            st.failed_jobs += 1
            errors[(job.error or "unknown error").split("\n")[0][:160]] += 1
        elif job.status == JobStatus.CANCELLED:
            st.cancelled_jobs += 1
        if job.status != JobStatus.DONE:
            continue
        key = bucket_key(_local_date(job.created_at), unit)
        u = users[job.user]
        u["jobs"] += 1
        if job.kind == JobKind.PRINT:
            st.print_jobs += 1
            if job.pages is None:
                st.unknown_page_jobs += 1
            impressions, sheets = sheets_for(job)
            st.printed_pages += impressions
            st.sheets += sheets
            st.sheets_saved += impressions - sheets
            if job.duplex and job.duplex.startswith("two-sided"):
                st.duplex_pages += impressions
            else:
                st.simplex_pages += impressions
            if job.color_mode == "color":
                st.color_pages += impressions
            elif job.color_mode == "monochrome":
                st.mono_pages += impressions
            else:
                st.default_color_pages += impressions
            printers[job.device_label or job.device] += impressions
            u["printed"] += impressions
            if key in buckets:
                buckets[key]["printed"] += impressions
        elif job.kind == JobKind.SCAN:
            st.scan_jobs += 1
            st.scanned_pages += job.pages or 0
            u["scanned"] += job.pages or 0
            if key in buckets:
                buckets[key]["scanned"] += job.pages or 0

    st.series = [{"date": k.isoformat(), **v} for k, v in buckets.items()]
    st.by_printer = sorted(({"name": n, "pages": p} for n, p in printers.items()),
                           key=lambda r: -r["pages"])
    st.by_user = sorted(({"user": n, **v} for n, v in users.items()),
                        key=lambda r: (-r["printed"], -r["scanned"], r["user"]))
    st.top_errors = [{"error": e, "count": c} for e, c in errors.most_common(5)]
    return st


def supply_history(samples: list[SupplySample]) -> list[dict]:
    """Group samples into one series per (printer, marker)."""
    series: dict[tuple[str, str], dict] = {}
    for s in samples:
        item = series.setdefault((s.printer, s.marker), {
            "printer": s.printer, "marker": s.marker, "color": s.color, "points": []})
        ts = s.sampled_at if s.sampled_at.tzinfo else s.sampled_at.replace(tzinfo=timezone.utc)
        item["points"].append({"t": ts.isoformat(), "level": s.level})
    return list(series.values())
