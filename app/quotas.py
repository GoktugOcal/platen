"""Per-user page quotas (printed pages per week or month)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .db import Database
from .models import User

PERIODS = {"month": "this month", "week": "this week"}


class QuotaExceeded(Exception):
    pass


def period_start(period: str, now: datetime | None = None) -> datetime:
    """Start of the current week (Monday) or month in local time, as UTC."""
    now = (now or datetime.now().astimezone())
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        start = midnight - timedelta(days=midnight.weekday())
    else:
        start = midnight.replace(day=1)
    return start.astimezone(timezone.utc)


@dataclass
class QuotaStatus:
    limit: int                 # 0 = unlimited
    period: str
    used: int

    @property
    def unlimited(self) -> bool:
        return self.limit <= 0

    @property
    def remaining(self) -> int | None:
        return None if self.unlimited else self.limit - self.used

    @property
    def percent(self) -> int:
        return 0 if self.unlimited else min(100, round(100 * self.used / self.limit))

    @property
    def period_label(self) -> str:
        return PERIODS.get(self.period, "this month")


def status(db: Database, user: User, exclude_job: str | None = None) -> QuotaStatus:
    if user.quota_pages <= 0:
        return QuotaStatus(0, user.quota_period, 0)
    used = db.printed_pages_since(user.username, period_start(user.quota_period),
                                  exclude_job=exclude_job)
    return QuotaStatus(user.quota_pages, user.quota_period, used)


def check(db: Database, user: User, pages: int, mode: str,
          exclude_job: str | None = None) -> QuotaStatus:
    """Raise QuotaExceeded when ``pages`` more would go over the quota (block mode)."""
    qs = status(db, user, exclude_job)
    if qs.unlimited or mode != "block":
        return qs
    if qs.remaining <= 0:
        raise QuotaExceeded(
            f"Your print quota of {qs.limit} pages {qs.period_label} is used up.")
    if pages > qs.remaining:
        raise QuotaExceeded(
            f"This job needs {pages} pages but only {qs.remaining} of your "
            f"{qs.limit} pages are left {qs.period_label}.")
    return qs
