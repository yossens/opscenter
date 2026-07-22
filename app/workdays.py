"""Pure functions for business days and card-aging.

The module deliberately does not depend on FastAPI/sqlite/starlette: only the
stdlib ``datetime``. The stage-entry time is stored in UTC ISO-8601, while
business days and dates for the UI are computed/formatted in the laptop's local
timezone.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone


def _utc_now() -> str:
    """Current UTC time in ISO-8601 format (``YYYY-MM-DDTHH:MM:SS``)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _to_local_date(value: str | date | datetime) -> date:
    """Coerces the input to a local calendar date.

    ``str`` is treated as UTC ISO-8601 (``YYYY-MM-DDTHH:MM:SS``) and converted to
    the local timezone before taking the date; ``datetime`` likewise (a naive one
    is treated as UTC); ``date`` is returned as is.
    """
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone().date()
    return value


def _count_weekdays(start: date, end: date) -> int:
    """Number of Mon–Fri days in the range [start, end] inclusive."""
    if start > end:
        return 0
    total_days = (end - start).days + 1
    full_weeks, remainder = divmod(total_days, 7)
    count = full_weeks * 5
    start_weekday = start.weekday()  # 0=Mon … 6=Sun
    for offset in range(remainder):
        if (start_weekday + offset) % 7 < 5:
            count += 1
    return count


def workdays_since(from_date: str | date | datetime, to_date: date) -> int:
    """Number of business days (Mon–Fri) strictly after ``from_date`` through ``to_date`` inclusive.

    ``from_date`` — the stage-entry moment (UTC ISO string or date/datetime),
    ``to_date`` — the local reporting date. Entering on a Friday yields 0 on that
    same Friday and on Saturday/Sunday, and 1 on Monday.
    """
    entered = _to_local_date(from_date)
    return _count_weekdays(entered + timedelta(days=1), to_date)


def aging_level(days_in_stage: int, threshold_days: int) -> str:
    """Card-aging level: ``'ok'`` / ``'warn'`` / ``'overdue'``.

    ``'ok'``      if ``days < 0.8 * threshold``;
    ``'warn'``    if ``0.8 * threshold <= days <= threshold``;
    ``'overdue'`` if ``days > threshold``.
    """
    if days_in_stage > threshold_days:
        return "overdue"
    if days_in_stage >= 0.8 * threshold_days:
        return "warn"
    return "ok"


def format_date(d: date) -> str:
    """Date format for the UI: DD.MM.YYYY."""
    return f"{d.day:02d}.{d.month:02d}.{d.year:04d}"
